import asyncio
import html
import os
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, quote

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from pydantic import BaseModel

from . import auth
from .core import connect, init_db, discover, get_job, cv_path, transition, log

auth.install_log_redaction()

scheduler = AsyncIOScheduler()
run_lock = asyncio.Lock()


async def scan():
    if run_lock.locked():
        log.info('Scan already running; skipping')
        return {'skipped': True}
    async with run_lock:
        return await discover()


async def telegram_loop():
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat:
        return
    offset = None
    async with httpx.AsyncClient(timeout=40) as client:
        while True:
            try:
                r = await client.get(f'https://api.telegram.org/bot{token}/getUpdates', params={'timeout': 25, 'offset': offset, 'allowed_updates': '["callback_query"]'})
                r.raise_for_status()
                for update in r.json().get('result', []):
                    offset = update['update_id'] + 1
                    cb = update.get('callback_query') or {}
                    if not cb:
                        continue
                    action, _, raw_id = (cb.get('data') or '').partition(':')
                    sender_chat = str((cb.get('message') or {}).get('chat', {}).get('id', ''))
                    answer = 'Unauthorized'
                    if sender_chat == str(chat) and action in ('approve', 'reject') and raw_id.isdigit():
                        try:
                            transition(int(raw_id), 'approved' if action == 'approve' else 'rejected', 'Telegram action')
                            answer = 'Draft approved (not submitted)' if action == 'approve' else 'Rejected'
                        except ValueError as exc:
                            answer = str(exc)
                    await client.post(f'https://api.telegram.org/bot{token}/answerCallbackQuery', json={'callback_query_id': cb['id'], 'text': answer[:190], 'show_alert': True})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Telegram polling error; retrying')
                await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.add_job(scan, 'interval', minutes=max(5, int(os.getenv('DISCOVERY_INTERVAL_MINUTES', '60'))), id='discovery', max_instances=1, coalesce=True)
    scheduler.start()
    tasks = [asyncio.create_task(scan()), asyncio.create_task(telegram_loop())]
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title='Job Hunter Agent', lifespan=lifespan)

NO_STORE = {'Cache-Control': 'no-store', 'Referrer-Policy': 'same-origin', 'X-Frame-Options': 'DENY'}


class StatusChange(BaseModel):
    status: str
    note: str = ''


def login_redirect(request: Request):
    target = request.url.path  # path only: never carry query strings (old ?token= links)
    return RedirectResponse(f'/login?next={quote(target, safe="/")}', status_code=303, headers=NO_STORE)


def login_page(next_path='/', error='', status_code=200):
    message = f'<p class="err">{html.escape(error)}</p>' if error else ''
    return HTMLResponse(f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Job Hunter Agent — Log in</title>
<style>body{{font:15px/1.5 system-ui,Arial;background:#f4f7fb;color:#213044;margin:0}}main{{max-width:380px;margin:12vh auto;padding:0 16px}}h1{{color:#163654;font-size:22px}}input,button{{width:100%;padding:10px;border:1px solid #c7d2df;border-radius:6px;margin:6px 0;box-sizing:border-box}}button{{cursor:pointer;background:#17476c;color:white}}.err{{color:#b42318}}.muted{{color:#64748b;font-size:13px}}</style></head>
<body><main><h1>Job Hunter Agent</h1>{message}<form method="post" action="/login"><label>Dashboard token<input name="token" type="password" autocomplete="current-password" required autofocus></label><input type="hidden" name="next" value="{html.escape(auth.safe_next(next_path))}"><button type="submit">Log in</button></form><p class="muted">The token is DASHBOARD_TOKEN from the VM's .env.</p></main></body></html>''', status_code=status_code, headers=NO_STORE)


@app.middleware('http')
async def reject_cross_site_posts(request: Request, call_next):
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and not auth.same_origin(request):
        return HTMLResponse('Cross-site request rejected', status_code=403)
    return await call_next(request)


@app.get('/health')
def health():
    return {'ok': True}


@app.get('/login', response_class=HTMLResponse)
def login_form(request: Request, next: str = '/'):
    if auth.is_authenticated(request):
        return RedirectResponse(auth.safe_next(next), status_code=303)
    auth.dashboard_token()  # 503 if not configured
    return login_page(next)


@app.post('/login')
async def login(request: Request):
    key = auth.client_key(request)
    raw = (await request.body())[:4096].decode('utf-8', 'replace')
    form = parse_qs(raw, keep_blank_values=True)
    next_path = auth.safe_next((form.get('next') or ['/'])[0])
    if auth.throttled(key):
        return login_page(next_path, 'Too many failed attempts. Try again in a few minutes.', 429)
    if not auth.check_token((form.get('token') or [''])[0]):
        auth.record_failure(key)
        log.warning('Dashboard login failed from %s', key)  # never log the submitted value
        return login_page(next_path, 'Invalid token.', 401)
    auth.reset_failures(key)
    response = RedirectResponse(next_path, status_code=303, headers=NO_STORE)
    response.set_cookie(auth.COOKIE_NAME, auth.new_session(), max_age=auth.SESSION_SECONDS,
                        path='/', httponly=True, secure=auth.cookie_secure(), samesite='lax')
    return response


@app.post('/logout')
def logout():
    response = RedirectResponse('/login', status_code=303, headers=NO_STORE)
    response.delete_cookie(auth.COOKIE_NAME, path='/', httponly=True,
                           secure=auth.cookie_secure(), samesite='lax')
    return response


@app.get('/api/jobs', dependencies=[Depends(auth.require_api_session)])
def jobs():
    with connect() as db:
        return [dict(x) for x in db.execute('SELECT id,source,company,title,location,url,apply_url,score,status,discovered_at FROM jobs ORDER BY discovered_at DESC LIMIT 500')]


@app.post('/api/scan', dependencies=[Depends(auth.require_api_session)])
async def trigger_scan():
    return await scan()


@app.post('/api/jobs/{job_id}/status', dependencies=[Depends(auth.require_api_session)])
def change_status(job_id: int, payload: StatusChange):
    try:
        transition(job_id, payload.status, payload.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {'ok': True}


@app.get('/api/jobs/{job_id}/cv')
def cv(job_id: int, request: Request):
    if not auth.is_authenticated(request):
        return login_redirect(request)
    if not get_job(job_id):
        raise HTTPException(404, 'Unknown job')
    path = cv_path(job_id)
    if not path.is_file():
        # CVs are never generated here; they are tailored interactively with Claude Code.
        return HTMLResponse(
            '<!doctype html><meta charset="utf-8"><title>No tailored CV</title>'
            '<body style="font:15px/1.5 system-ui,Arial;max-width:640px;margin:40px auto;padding:0 16px">'
            f'<h1>No tailored CV for job {job_id} yet</h1>'
            '<p>On the VM, in the project directory, run:</p>'
            f'<pre>.venv/bin/python -m app.tailor_cv --job-id {job_id}</pre>'
            '<p>then follow the printed Claude Code steps and finish with '
            f'<code>--job-id {job_id} --finalize</code>. See README.</p></body>',
            status_code=404, headers=NO_STORE)
    return FileResponse(path, media_type='text/html', headers=NO_STORE,
                        content_disposition_type='inline', filename=f'cv-job-{job_id}.html')


@app.get('/', response_class=HTMLResponse)
def index(request: Request):
    if not auth.is_authenticated(request):
        return login_redirect(request)
    return HTMLResponse(DASHBOARD_HTML, headers=NO_STORE)


DASHBOARD_HTML = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Job Hunter Agent</title>
<style>body{font:15px/1.5 system-ui,Arial;background:#f4f7fb;color:#213044;margin:0}main{max-width:1120px;margin:30px auto;padding:0 16px}h1{color:#163654}header{display:flex;flex-wrap:wrap;align-items:center;gap:10px}input,button,select{padding:9px;border:1px solid #c7d2df;border-radius:6px}button{cursor:pointer;background:#17476c;color:white}table{width:100%;border-collapse:collapse;background:white}th,td{padding:12px;border-bottom:1px solid #e5eaf0;text-align:left;vertical-align:top}a{color:#14628a}small{color:#64748b}.scroll{overflow-x:auto}.muted{color:#64748b}</style></head><body><main><h1>Job Hunter Agent</h1><header><button onclick="loadJobs()">Reload jobs</button><button onclick="scan()">Scan now</button><form method="post" action="/logout" style="margin-left:auto"><button type="submit">Log out</button></form></header><p id="notice" class="muted">Loading…</p><div class="scroll"><table><thead><tr><th>Job</th><th>Location</th><th>Match</th><th>Status</th><th>Actions</th></tr></thead><tbody id="jobs"></tbody></table></div></main>
<script>
const notice=s=>document.getElementById('notice').textContent=s;
async function request(path,options={}){const r=await fetch(path,{...options,credentials:'same-origin',headers:{'Content-Type':'application/json',...(options.headers||{})}});if(r.status===401){location.href='/login';throw new Error('Session expired')}if(!r.ok)throw new Error(await r.text());return r.json()}
async function loadJobs(){try{const data=await request('/api/jobs');const body=document.getElementById('jobs');body.replaceChildren();for(const j of data){const tr=document.createElement('tr');const cell=()=>{const td=document.createElement('td');tr.append(td);return td};const name=cell();const link=document.createElement('a');link.href=j.url;link.target='_blank';link.rel='noopener noreferrer';link.textContent=j.title;name.append(link,document.createElement('br'),document.createTextNode(j.company));cell().textContent=j.location||'—';cell().textContent=j.score+'/100';cell().textContent=j.status;const actions=cell();const cv=document.createElement('button');cv.textContent='HTML CV';cv.onclick=()=>window.open('/api/jobs/'+j.id+'/cv','_blank','noopener');actions.append(cv);for(const status of ['approved','needs_revision','rejected','applied','interview','closed']){const b=document.createElement('button');b.textContent=status.replace('_',' ');b.style.margin='3px';b.onclick=async()=>{let note='';if(status==='applied'){note=prompt('Enter submission confirmation/reference (required):')||'';if(!note.trim())return notice('Confirmation is required to record applied')}try{await request('/api/jobs/'+j.id+'/status',{method:'POST',body:JSON.stringify({status,note})});loadJobs()}catch(e){notice(e.message)}};actions.append(b)}body.append(tr)}notice('Loaded '+data.length+' jobs')}catch(e){notice(e.message)}}
async function scan(){notice('Scanning…');try{const r=await request('/api/scan',{method:'POST'});notice('Checked '+r.checked+' jobs, added '+r.new);await loadJobs()}catch(e){notice(e.message)}}
loadJobs();
</script></body></html>'''
