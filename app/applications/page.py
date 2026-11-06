"""Server-rendered review page for one application (dashboard, same login). Every value is HTML-escaped."""
import json
from html import escape

STATUS_TEXT = {
    'awaiting_cv': 'Waiting for the tailored CV', 'queued': 'Being prepared', 'submitting': 'Submitting',
    'manual_action_required': 'Ready for your review', 'dry_run': 'Ready for your review (dry run)',
    'blocked': 'Blocked', 'failed': 'Failed', 'cancelled': 'Cancelled',
    'submitted': 'Submitted (confirmation recorded)', 'confirmed': 'Confirmed by the employer',
}
CSS = """body{font:15px/1.5 system-ui,Arial;max-width:960px;margin:24px auto;padding:0 16px;color:#1d2733}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:22px 0 6px;border-bottom:1px solid #dde3ea}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #eef1f4;padding:5px 6px;text-align:left;vertical-align:top}
.ok{color:#1b6e2f}.missing,.needs_answer{color:#a1260d;font-weight:600}.optional{color:#6b7785}
.warn{background:#fff6e0;border-left:4px solid #e0a200;padding:6px 10px;margin:6px 0}.muted{color:#6b7785}
button,.btn{display:inline-block;margin:4px 6px 4px 0;padding:7px 12px;border:1px solid #2d5b8a;border-radius:5px;background:#2d5b8a;
color:#fff;text-decoration:none;font:inherit;cursor:pointer}.secondary{background:#fff;color:#2d5b8a}.danger{background:#8a1c1c;border-color:#8a1c1c}
code{background:#f3f5f7;padding:1px 4px;border-radius:3px}.copy{padding:1px 6px;margin:0 0 0 6px;font-size:12px}"""


def _value(row):
    value = row.get('value')
    if value is None:
        return ''
    if isinstance(value, list):
        return ', '.join(str(v) for v in value)
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    return str(value)


def render(app, package, actions):
    """app: applications row (+ title/employer); package: prepare.build_package() output or {}; actions: allowed set."""
    detail = json.loads(app['detail'] or '{}')
    job = package.get('job') or {'title': app['title'], 'employer': app['employer'] or app['company']}
    e = lambda v: escape(str(v if v is not None else ''), quote=True)
    out = [f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>Application {app["id"]}</title><style>{CSS}</style>',
           '<p><a href="/">← Dashboard</a></p>',
           f'<h1>{e(job.get("title"))}</h1><div class="muted">{e(job.get("employer"))} · {e(job.get("location") or "")}'
           f' · score {e(job.get("score") or "")}</div>',
           f'<p><b>Status:</b> {e(STATUS_TEXT.get(app["status"], app["status"]))}'
           + (f' — {e(detail.get("reason"))}' if detail.get('reason') else '') + '</p>']
    if app['status'] in ('submitted', 'confirmed'):
        out.append(f'<p><b>Confirmation:</b> {e(detail.get("confirmation") or app["external_id"])} '
                   f'<span class="muted">({e((app["submitted_at"] or "")[:16].replace("T", " "))} UTC)</span></p>')
    for warning in package.get('warnings') or []:
        out.append(f'<div class="warn">{e(warning)}</div>')
    avail = (package.get('availability') or {})
    open_text = {True: 'open', False: 'closed', None: 'not confirmed'}[avail.get('open')]
    cv = package.get('cv') or {}
    out += ['<h2>Application</h2><table>',
            f'<tr><th>Employer page</th><td><a href="{e(package.get("apply_page") or app["apply_url"])}" target="_blank" '
            f'rel="noopener noreferrer">{e(package.get("apply_page") or app["apply_url"])}</a></td></tr>',
            f'<tr><th>Vacancy</th><td>{e(open_text)} <span class="muted">({e(avail.get("checked_via") or "discovery")}, '
            f'checked {e((package.get("prepared_at") or "")[:16].replace("T", " "))} UTC)</span></td></tr>',
            f'<tr><th>Tailored CV</th><td><a href="{e(cv.get("html"))}" target="_blank" rel="noopener">HTML (review)</a> · '
            + (f'<a href="{e(cv.get("pdf"))}" download>PDF (upload this)</a>' if cv.get('pdf_current') else
               '<span class="missing">PDF missing or outdated</span>')
            + f' <span class="muted">version {e(cv.get("version") or "-")}</span></td></tr>',
            f'<tr><th>Submission</th><td>In your own browser{" — the form uses a CAPTCHA" if package.get("captcha") else ""}. '
            'Nothing is submitted automatically.</td></tr></table>']
    if package.get('missing') or package.get('questions'):
        out.append('<h2>Needs your input</h2><ul>')
        out += [f'<li class="missing">{e(m)}</li>' for m in package.get('missing') or []]
        out += [f'<li class="needs_answer">Question: {e(q)}</li>' for q in package.get('questions') or []]
        out.append('</ul><p class="muted">Add missing values or explicit `answers` to data/applicant_profile.yaml, then '
                   'press “Re-check”. Questions are never answered for you.</p>')
    if package.get('fields'):
        out.append('<h2>Form fields</h2><table><tr><th>Field</th><th>Required</th><th>Value to enter</th><th></th></tr>')
        for i, row in enumerate(package['fields']):
            value, status = _value(row), row.get('status')
            label = {'ok': 'ready', 'missing': 'missing', 'needs_answer': 'your answer needed', 'optional': 'optional'}[status]
            options = f'<div class="muted">options: {e(", ".join(map(str, row.get("options") or [])))}</div>' if row.get('options') else ''
            copy = (f'<button class="copy secondary" type="button" data-copy="{e(value)}">copy</button>'
                    if value and row.get('source') != 'cv_pdf' else '')
            out.append(f'<tr><td>{e(row.get("label"))}{options}</td><td>{"yes" if row.get("required") else "no"}</td>'
                       f'<td>{e(value)}{copy}</td><td class="{e(status)}">{e(label)}</td></tr>')
        out.append('</table>')
    for note in package.get('notes') or []:
        out.append(f'<p class="muted">{e(note)}</p>')
    prof = package.get('profile') or {}
    if prof.get('errors'):
        out.append('<p class="missing">Profile: ' + e('; '.join(prof['errors'])) + '</p>')
    out.append('<h2>Actions</h2><p>')
    if 'open' in actions:
        out.append(f'<a class="btn" href="/applications/{app["id"]}/open" target="_blank" rel="noopener">Open Application Page</a>')
    if cv.get('pdf_current'):
        out.append(f'<a class="btn secondary" href="{e(cv.get("pdf"))}" download>Download CV PDF</a>')
    if 'refresh' in actions:
        out.append('<button class="secondary" id="refresh">Re-check</button>')
    if 'cancel' in actions:
        out.append('<button class="danger" id="cancel">Cancel</button>')
    out.append('</p>')
    if 'submitted' in actions:
        out.append('<h2>Mark as Submitted</h2><p class="muted">Only after you submitted it yourself and received a confirmation.</p>'
                   '<form id="submitted"><input id="confirmation" size="60" maxlength="300" required '
                   'placeholder="Confirmation (reference number, email subject, ...)"><br>'
                   '<label><input type="checkbox" id="confirmed"> I submitted this application myself on the employer\'s site</label><br>'
                   '<button type="submit">Mark as Submitted</button></form>')
    out.append(f'<p id="msg" class="muted"></p><script>const id={int(app["id"])};' + """
async function post(path,body){const r=await fetch('/api/applications/'+id+'/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||('HTTP '+r.status));return d}
const msg=t=>document.getElementById('msg').textContent=t;
document.querySelectorAll('[data-copy]').forEach(b=>b.onclick=()=>navigator.clipboard.writeText(b.dataset.copy).then(()=>msg('Copied')));
const rf=document.getElementById('refresh');if(rf)rf.onclick=async()=>{msg('Re-checking...');try{await post('refresh');location.reload()}catch(e){msg(e.message)}};
const cn=document.getElementById('cancel');if(cn)cn.onclick=async()=>{if(!confirm('Cancel this application? Nothing has been submitted.'))return;try{await post('cancel');location.reload()}catch(e){msg(e.message)}};
const sf=document.getElementById('submitted');if(sf)sf.onsubmit=async ev=>{ev.preventDefault();try{await post('submitted',{confirmation:document.getElementById('confirmation').value,confirmed:document.getElementById('confirmed').checked});location.reload()}catch(e){msg(e.message)}};
</script>""")
    return ''.join(out)


def allowed_actions(app, prepared):
    actions = set()
    if app['status'] in ('awaiting_cv', 'queued', 'manual_action_required', 'dry_run', 'blocked', 'failed'):
        actions.add('cancel')
    if prepared:
        actions |= {'open', 'refresh', 'submitted'}
    return actions
