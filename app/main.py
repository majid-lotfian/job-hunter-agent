import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

from .core import connect, init_db, discover, make_cv, transition, log

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


def auth(token):
    expected = os.getenv('DASHBOARD_TOKEN')
    if not expected or expected == 'replace-with-a-long-random-secret':
        raise HTTPException(503, 'Set a secure DASHBOARD_TOKEN in .env')
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(401, 'Invalid dashboard token')


class StatusChange(BaseModel):
    status: str
    note: str = ''


@app.get('/health')
def health():
    return {'ok': True}


@app.get('/api/jobs')
def jobs(x_dashboard_token: str | None = Header(default=None)):
    auth(x_dashboard_token)
    with connect() as db:
        return [dict(x) for x in db.execute('SELECT id,source,company,title,location,url,apply_url,score,status,discovered_at FROM jobs ORDER BY discovered_at DESC LIMIT 500')]


@app.post('/api/scan')
async def trigger_scan(x_dashboard_token: str | None = Header(default=None)):
    auth(x_dashboard_token)
    return await scan()


@app.post('/api/jobs/{job_id}/status')
def change_status(job_id: int, payload: StatusChange, x_dashboard_token: str | None = Header(default=None)):
    auth(x_dashboard_token)
    try:
        transition(job_id, payload.status, payload.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {'ok': True}


@app.get('/api/jobs/{job_id}/cv')
def cv(job_id: int, token: str = Query(default='')):
    auth(token)
    try:
        path = make_cv(job_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type='text/html', filename=f'cv-job-{job_id}.html')


@app.get('/', response_class=HTMLResponse)
def index():
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Job Hunter Agent</title>
<style>body{font:15px/1.5 system-ui,Arial;background:#f4f7fb;color:#213044;margin:0}main{max-width:1120px;margin:30px auto;padding:0 16px}h1{color:#163654}header{display:flex;flex-wrap:wrap;align-items:center;gap:10px}input,button,select{padding:9px;border:1px solid #c7d2df;border-radius:6px}button{cursor:pointer;background:#17476c;color:white}table{width:100%;border-collapse:collapse;background:white}th,td{padding:12px;border-bottom:1px solid #e5eaf0;text-align:left;vertical-align:top}a{color:#14628a}small{color:#64748b}.scroll{overflow-x:auto}.muted{color:#64748b}</style></head><body><main><h1>Job Hunter Agent</h1><header><label>Dashboard token <input id="token" type="password" placeholder="Paste from .env"></label><button onclick="loadJobs()">Load jobs</button><button onclick="scan()">Scan now</button></header><p id="notice" class="muted">Your token stays in this browser tab; it is not saved to disk.</p><div class="scroll"><table><thead><tr><th>Job</th><th>Location</th><th>Match</th><th>Status</th><th>Actions</th></tr></thead><tbody id="jobs"></tbody></table></div></main>
<script>
const token=()=>document.getElementById('token').value;
const notice=s=>document.getElementById('notice').textContent=s;
async function request(path,options={}){const r=await fetch(path,{...options,headers:{'X-Dashboard-Token':token(),'Content-Type':'application/json',...(options.headers||{})}});if(!r.ok)throw new Error(await r.text());return r.json()}
async function loadJobs(){try{const data=await request('/api/jobs');const body=document.getElementById('jobs');body.replaceChildren();for(const j of data){const tr=document.createElement('tr');const cell=()=>{const td=document.createElement('td');tr.append(td);return td};const name=cell();const link=document.createElement('a');link.href=j.url;link.target='_blank';link.rel='noopener noreferrer';link.textContent=j.title;name.append(link,document.createElement('br'),document.createTextNode(j.company));cell().textContent=j.location||'—';cell().textContent=j.score+'/100';cell().textContent=j.status;const actions=cell();const cv=document.createElement('button');cv.textContent='HTML CV';cv.onclick=()=>{if(!token())return notice('Enter token');window.open('/api/jobs/'+j.id+'/cv?token='+encodeURIComponent(token()),'_blank')};actions.append(cv);for(const status of ['approved','needs_revision','rejected','applied','interview','closed']){const b=document.createElement('button');b.textContent=status.replace('_',' ');b.style.margin='3px';b.onclick=async()=>{let note='';if(status==='applied'){note=prompt('Enter submission confirmation/reference (required):')||'';if(!note.trim())return notice('Confirmation is required to record applied')}try{await request('/api/jobs/'+j.id+'/status',{method:'POST',body:JSON.stringify({status,note})});loadJobs()}catch(e){notice(e.message)}};actions.append(b)}body.append(tr)}notice('Loaded '+data.length+' jobs')}catch(e){notice(e.message)}}
async function scan(){notice('Scanning…');try{const r=await request('/api/scan',{method:'POST'});notice('Checked '+r.checked+' jobs, added '+r.new);await loadJobs()}catch(e){notice(e.message)}}
</script></body></html>'''
