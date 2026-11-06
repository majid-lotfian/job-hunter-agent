"""Approval workflows shared by Telegram and the dashboard.

Score tiers (CV_MIN_SCORE=70, APPROVAL_MIN_SCORE=40):
  >= 70  'auto_cv'    CV generated automatically; you get "Approve & Apply" / "Reject" once it is validated.
  40-69  'approval'   no automatic CV; you get "Approve CV & Apply" / "Reject". One approval authorizes CV
                      generation, validation and submission. Validation warnings/failures pause and notify.
  < 40   'reference'  kept in the dashboard only (an explicit dashboard action is still possible).

Every decision goes through decide(): one write transaction, an approvals row and an events row (audit trail).
Approvals are bound to a version token = hash(job content + CV file content), so a changed job or CV never
silently reuses an outdated approval. Applications are created only from an approval; app.applications submits.
"""
import hashlib
import json
import os
import re

from .core import connect, cv_path, dashboard_base, get_cv_job, log, now_iso, send_telegram, set_cv_job
from .tailor_cv import MARKER

APPROVE_APPLY, APPROVE_CV_APPLY, REJECT = 'approve_apply', 'approve_cv_apply', 'reject'
ACTION_CODES = {'aa': APPROVE_APPLY, 'ac': APPROVE_CV_APPLY, 'rj': REJECT}
CODE_FOR = {v: k for k, v in ACTION_CODES.items()}
LABELS = {APPROVE_APPLY: 'Approve & Apply', APPROVE_CV_APPLY: 'Approve CV & Apply', REJECT: 'Reject'}
CLOSED_JOB_STATUSES = ('rejected', 'applied', 'interview', 'closed')
LIVE_APP_STATES = ('awaiting_cv', 'queued', 'submitting', 'submitted', 'confirmed')


def _int_env(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def thresholds():
    return {'auto_cv': _int_env('CV_MIN_SCORE', 70), 'approval': _int_env('APPROVAL_MIN_SCORE', 40),
            'messages_per_batch': max(0, _int_env('TELEGRAM_ACTIONS_PER_BATCH', 8))}


def workflow_for(score, t=None):
    t = t or thresholds()
    return 'auto_cv' if score >= t['auto_cv'] else ('approval' if score >= t['approval'] else 'reference')


# --------------------------------------------------------------------------- versions

def _h(text, n=12):
    return hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:n]


def job_version(job):
    return _h('\x1f'.join(str(job[k] or '') for k in ('title', 'employer', 'company', 'description', 'apply_url', 'url')))


def cv_version(job_id):
    """Hash of the tailored CV file (None if there is no tailored CV)."""
    path = cv_path(job_id)
    if not path.is_file():
        return None
    data = path.read_bytes()
    return _h(data.decode('utf-8', 'replace'), 12) if MARKER.encode() in data else None


def action_token(job, cv_ver=None):
    return _h(f'{job_version(job)}:{cv_ver or "-"}', 10)


# --------------------------------------------------------------------------- state

def _job(db, job_id):
    return db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()


def _live_application(db, job_id):
    return db.execute(f"SELECT * FROM applications WHERE job_id=? AND status IN ({','.join('?' * len(LIVE_APP_STATES))}) "
                      "ORDER BY id DESC LIMIT 1", (job_id, *LIVE_APP_STATES)).fetchone()


def latest_application(db, job_id):
    return db.execute('SELECT * FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1', (job_id,)).fetchone()


def _event(db, job_id, action, note):
    db.execute('INSERT INTO events (job_id, action, note, created_at) VALUES (?,?,?,?)', (job_id, action, note[:500], now_iso()))


FINISHED_APP_STATES = ('manual_action_required', 'dry_run', 'blocked', 'failed')


def available_actions(job, cv_row, live_app, last_app=None):
    """Actions offered for a job right now (same answer for Telegram and the dashboard)."""
    if job['status'] in CLOSED_JOB_STATUSES or (job['availability'] or 'active') == 'closed':
        return []
    if live_app is not None:  # approved and not started yet: you can still cancel
        return [REJECT] if live_app['status'] in ('awaiting_cv', 'queued') else []
    if last_app is not None and last_app['status'] in FINISHED_APP_STATES \
            and not json.loads(last_app['detail'] or '{}').get('reapprove'):
        return []  # you approved; the outcome needs manual follow-up (apply via the URL), not a second approval
    cv_status = cv_row['status'] if cv_row else None
    if cv_status in ('ready', 'needs_review') and cv_version(job['id']):
        return [APPROVE_APPLY, REJECT]
    if cv_status in ('pending', 'generating', 'waiting_retry'):
        return [REJECT]
    return [APPROVE_CV_APPLY, REJECT]


def job_state(job_id):
    """Everything the dashboard/Telegram need to render a job's workflow (one call, read-only)."""
    with connect() as db:
        job = _job(db, job_id)
        if not job:
            return None
        cv_row = db.execute('SELECT * FROM cv_jobs WHERE job_id=?', (job_id,)).fetchone()
        live, last = _live_application(db, job_id), latest_application(db, job_id)
    cv_ver = cv_version(job_id)
    return {'workflow': workflow_for(job['score']), 'actions': available_actions(job, cv_row, live, last),
            'token': action_token(job, cv_ver), 'cv_version': cv_ver,
            'application_status': last['status'] if last else None,
            'application_detail': json.loads(last['detail'] or '{}') if last else {},
            'ai_keywords': [k for k in (job['ai_keywords'] or '').split(',') if k]}


# --------------------------------------------------------------------------- decisions

class Decision(dict):
    @property
    def ok(self):
        return self['result'] in ('approved', 'rejected', 'cv_queued')


def decide(job_id, action, *, channel, token, actor=None):
    """The single backend transition for Telegram and dashboard decisions. Idempotent and stale-safe."""
    if action not in LABELS:
        return Decision(result='invalid', message='Unknown action')
    from . import cv_pipeline  # lazy: cv_pipeline imports workflow lazily too
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        job = _job(db, job_id)
        if not job:
            return Decision(result='invalid', message='Unknown job')
        cv_row = db.execute('SELECT * FROM cv_jobs WHERE job_id=?', (job_id,)).fetchone()
        live = _live_application(db, job_id)
        last = latest_application(db, job_id)
        cv_ver = cv_version(job_id)
        current = action_token(job, cv_ver)
        if live is not None and action != REJECT:
            return Decision(result='already', message=f"Already approved - application {live['status'].replace('_', ' ')}")
        if job['status'] in CLOSED_JOB_STATUSES:
            return Decision(result='already', message=f"Job already {job['status']}")
        if token != current:
            return Decision(result='stale', message='This action is outdated (job or CV changed). Use the latest message or the dashboard.')
        allowed = available_actions(job, cv_row, live, last)
        if action not in allowed:
            return Decision(result='not_available', message=f'Not available now ({", ".join(LABELS[a] for a in allowed) or "no actions"})')
        jv = job_version(job)
        now = now_iso()
        if action == REJECT:
            db.execute("UPDATE approvals SET status='revoked' WHERE job_id=? AND status='active'", (job_id,))
            db.execute(f"UPDATE applications SET status='cancelled', updated_at=? WHERE job_id=? AND status IN ('awaiting_cv','queued')",
                       (now, job_id))
            if cv_row and cv_row['status'] in ('pending', 'waiting_retry'):
                db.execute("UPDATE cv_jobs SET status='cancelled', updated_at=? WHERE job_id=?", (now, job_id))
            db.execute('INSERT INTO approvals (job_id, action, channel, job_version, cv_version, status, actor, created_at) '
                       'VALUES (?,?,?,?,?,?,?,?)', (job_id, REJECT, channel, jv, cv_ver, 'consumed', actor, now))
            db.execute("UPDATE jobs SET status='rejected' WHERE id=?", (job_id,))
            _event(db, job_id, 'rejected', f'{channel}: rejected')
            return Decision(result='rejected', message='Rejected')
        approval_id = db.execute('INSERT INTO approvals (job_id, action, channel, job_version, cv_version, status, actor, created_at) '
                                 'VALUES (?,?,?,?,?,?,?,?)',
                                 (job_id, action, channel, jv, cv_ver if action == APPROVE_APPLY else None, 'active', actor, now)).lastrowid
        db.execute("UPDATE jobs SET status='approved' WHERE id=?", (job_id,))
        apply_url = job['apply_url'] or job['url']
        if action == APPROVE_APPLY:
            db.execute("INSERT INTO applications (job_id, approval_id, status, job_version, cv_version, apply_url, created_at, updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?)", (job_id, approval_id, 'queued', jv, cv_ver, apply_url, now, now))
            _event(db, job_id, 'approved', f'{channel}: Approve & Apply (CV {cv_ver})')
            return Decision(result='approved', message='Approved - application queued')
        db.execute("INSERT INTO applications (job_id, approval_id, status, job_version, apply_url, created_at, updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", (job_id, approval_id, 'awaiting_cv', jv, apply_url, now, now))
        _event(db, job_id, 'approved', f'{channel}: Approve CV & Apply')
    # outside the transaction: queue the CV with top priority (not counted in any automatic quota)
    row = get_cv_job(job_id)
    if row and row['status'] in ('pending', 'generating', 'waiting_retry'):
        set_cv_job(job_id, row['status'], reason='approved')  # keeps any retry cooldown; served first when due
    else:
        cv_pipeline.enqueue(job_id, 'approved', force=bool(row and row['status'] in ('ready', 'needs_review')))
        set_cv_job(job_id, 'pending', reason='approved')
    return Decision(result='cv_queued', message='Approved - generating CV, then applying')


def on_cv_finished(job_id, status):
    """Called by the CV worker after every CV outcome."""
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        job = _job(db, job_id)
        app = db.execute("SELECT * FROM applications WHERE job_id=? AND status='awaiting_cv' ORDER BY id DESC LIMIT 1",
                         (job_id,)).fetchone()
        now = now_iso()
        if app is None:
            if status in ('ready', 'needs_review'):  # auto-CV path: ask for Approve & Apply
                db.execute('UPDATE jobs SET action_notified_at=NULL WHERE id=?', (job_id,))
            return
        if job_version(job) != app['job_version']:
            _pause(db, app, now, 'The job posting changed after your approval - please review and approve again.')
            return
        if status == 'ready':
            cv_ver = cv_version(job_id)
            db.execute("UPDATE applications SET status='queued', cv_version=?, updated_at=? WHERE id=?", (cv_ver, now, app['id']))
            db.execute("UPDATE approvals SET cv_version=? WHERE id=?", (cv_ver, app['approval_id']))
            _event(db, job_id, 'cv_validated', f'CV {cv_ver} passed validation; application queued (approved earlier)')
        elif status == 'needs_review':
            _pause(db, app, now, 'CV validation raised warnings - review the CV, then use Approve & Apply.')
        else:
            _pause(db, app, now, 'CV generation failed - nothing was submitted.')


def _pause(db, app, now, reason):
    db.execute("UPDATE applications SET status='manual_action_required', last_error=?, detail=?, updated_at=?, notified_at=NULL "
               "WHERE id=?", (reason, json.dumps({'reason': reason, 'reapprove': True}), now, app['id']))
    db.execute("UPDATE approvals SET status='superseded' WHERE id=?", (app['approval_id'],))
    db.execute("UPDATE jobs SET action_notified_at=NULL WHERE id=?", (app['job_id'],))
    _event(db, app['job_id'], 'application_paused', reason)


# --------------------------------------------------------------------------- Telegram messages

def keyboard(job_id, actions, token):
    return {'inline_keyboard': [[{'text': LABELS[a], 'callback_data': f'wf:{CODE_FOR[a]}:{job_id}:{token}'} for a in actions]]}


def action_message(job, state, cv_row=None):
    kw = ', '.join(state['ai_keywords']) or 'none'
    cv = {'ready': 'ready for review', 'needs_review': 'needs review'}.get(cv_row['status'] if cv_row else None,
                                                                         (cv_row['status'] if cv_row else 'none').replace('_', ' '))
    app = (state['application_status'] or 'not started').replace('_', ' ')
    lines = [f"{job['score']}/100 · {job['title']} — {job['employer'] or job['company']}",
             f"{job['location_label'] or job['location'] or ''}",
             f'AI keywords: {kw}', f'CV: {cv} · Application: {app}']
    if cv_row and cv_row['status'] == 'needs_review' and cv_row['warnings']:
        lines.append('CV warnings: ' + '; '.join(cv_row['warnings'].splitlines())[:300])
    reason = state['application_detail'].get('reason')
    if reason:
        lines.append(f'Paused: {reason}')
    if state['cv_version']:
        lines.append(f"Review CV: {dashboard_base()}/api/jobs/{job['id']}/cv")
    lines.append(f"Apply URL: {job['apply_url'] or job['url']}")
    hint = {APPROVE_APPLY: 'Approve & Apply submits this CV version.',
            APPROVE_CV_APPLY: 'Approve CV & Apply = generate + validate the CV, then apply (no second approval).'}
    lines += [hint[a] for a in state['actions'] if a in hint]
    lines.append('Nothing is submitted without your approval.')
    return '\n'.join(x for x in lines if x)


def pending_action_jobs(limit=None):
    """Jobs waiting for a decision whose current version has not been announced yet, most important first."""
    t = thresholds()
    with connect() as db:
        rows = db.execute("""SELECT j.*, c.status AS cv_status FROM jobs j LEFT JOIN cv_jobs c ON c.job_id=j.id
                             WHERE COALESCE(j.availability,'active')='active' AND j.status NOT IN ('rejected','applied','interview','closed')
                               AND (j.score >= ? OR c.status IN ('ready','needs_review'))
                             ORDER BY CASE WHEN c.status IN ('ready','needs_review') THEN 0 ELSE 1 END, j.score DESC, j.id""",
                          (t['approval'],)).fetchall()
    out = []
    for job in rows:
        state = job_state(job['id'])
        decisions = [a for a in state['actions'] if a != REJECT]
        if not decisions:
            continue
        if job['score'] >= t['auto_cv'] and decisions == [APPROVE_CV_APPLY] and job['cv_status'] is None:
            continue  # auto-CV tier: wait for the automatic CV instead of asking first
        if job['cv_status'] in ('failed', 'cancelled'):
            continue  # reported by the CV failure digest; retry from the dashboard
        if job['action_notified_at'] and job['action_version'] == state['token']:
            continue
        out.append((job, state))
    return out[:limit] if limit else out


_last_summary = {'count': None}


async def send_pending_actions(sender=None):
    """Individual actionable messages (capped per batch) + one summary line for the rest."""
    send = sender or send_telegram
    t = thresholds()
    pending = pending_action_jobs()
    sent = 0
    for job, state in pending[:t['messages_per_batch']]:
        cv_row = get_cv_job(job['id'])
        try:
            delivered = await send(action_message(job, state, cv_row), keyboard(job['id'], state['actions'], state['token']))
        except Exception:
            log.exception('Telegram action message failed for job %s', job['id'])
            continue
        with connect() as db:
            db.execute('UPDATE jobs SET action_notified_at=?, action_version=? WHERE id=?', (now_iso(), state['token'], job['id']))
        sent += delivered is not False
    rest = len(pending) - min(len(pending), t['messages_per_batch'])
    if rest and rest != _last_summary['count']:
        try:
            await send(f'{rest} more job(s) await your decision - open the dashboard: {dashboard_base()}')
        except Exception:
            log.exception('Telegram summary failed')
    _last_summary['count'] = rest
    from .applications import send_application_updates
    sent += await send_application_updates(sender=send)
    return sent


def parse_callback(data):
    """'wf:aa:12:ab12cd34ef' -> (action, job_id, token) or None."""
    m = re.fullmatch(r'wf:(aa|ac|rj):(\d+):([0-9a-f]{10})', data or '')
    return (ACTION_CODES[m.group(1)], int(m.group(2)), m.group(3)) if m else None


def handle_telegram_callback(data, *, chat_id, user_id, allowed_chat, allowed_users=None):
    """Authorize and execute a Telegram button press. Returns (answer_text, remove_buttons)."""
    if str(chat_id) != str(allowed_chat) or (allowed_users and str(user_id) not in allowed_users):
        log.warning('Rejected Telegram callback from unauthorized chat/user')
        return 'Unauthorized', False
    parsed = parse_callback(data)
    if parsed is None:
        legacy, _, raw_id = (data or '').partition(':')
        if legacy == 'reject' and raw_id.isdigit():  # buttons from older messages: rejecting is always safe
            state = job_state(int(raw_id))
            if state is None:
                return 'Unknown job', True
            result = decide(int(raw_id), REJECT, channel='telegram', token=state['token'], actor=str(user_id))
            return result['message'], True
        if legacy == 'approve':
            return 'This button is from an older message. Use the new message or the dashboard to approve.', True
        return 'Unknown action', False
    action, job_id, token = parsed
    result = decide(job_id, action, channel='telegram', token=token, actor=str(user_id))
    return result['message'], result['result'] in ('approved', 'rejected', 'cv_queued', 'already', 'stale')
