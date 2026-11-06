"""Application submission: state machine, worker and notifications.

States: awaiting_cv -> queued -> submitting -> submitted | confirmed
                                           \\-> dry_run | manual_action_required | blocked | failed
        (cancelled when you reject first)
Rules:
- An application row exists only because of an explicit approval (app.workflow.decide).
- Before anything is attempted, the approval must still match the current job and CV versions.
- Only one live application per job (unique index) and per canonical apply URL (cross-source duplicates).
- APPLY_MODE: off (default) -> readiness check, then you apply manually; dry_run -> adapter builds the request but
  sends nothing; live -> real submission, only for live-capable adapters listed in APPLY_LIVE_ADAPTERS.
- A job becomes 'applied' only with an external confirmation id from the employer's system.
- An application left in 'submitting' by a crash/restart is never retried automatically (outcome unknown).
"""
import json
import os
from datetime import datetime, timedelta, timezone

from ..core import connect, cv_path, dashboard_base, log, now_iso, send_telegram, transition
from ..discovery.http import HttpClient
from ..discovery.normalize import canonical_url
from . import adapters
from .profile import load_profile

NOTIFY_STATES = ('manual_action_required', 'blocked', 'failed', 'submitted', 'confirmed', 'dry_run')


def settings():
    mode = os.getenv('APPLY_MODE', 'off').strip().lower()
    return {'mode': mode if mode in ('off', 'dry_run', 'live') else 'off',
            'live_adapters': {x.strip() for x in os.getenv('APPLY_LIVE_ADAPTERS', '').split(',') if x.strip()},
            'max_attempts': max(1, int(os.getenv('APPLY_MAX_ATTEMPTS', '3') or 3)),
            'retry_base': max(30, int(os.getenv('APPLY_RETRY_BASE_SECONDS', '300') or 300))}


def _set(db, app_id, status, **fields):
    fields['updated_at'] = now_iso()
    cols = ', '.join(f'{k}=?' for k in fields)
    db.execute(f'UPDATE applications SET status=?, {cols} WHERE id=?', (status, *fields.values(), app_id))


def _event(db, job_id, action, note):
    db.execute('INSERT INTO events (job_id, action, note, created_at) VALUES (?,?,?,?)', (job_id, action, note[:500], now_iso()))


def recover_after_restart():
    """Never resend: an interrupted submission may or may not have reached the employer."""
    with connect() as db:
        rows = db.execute("SELECT id, job_id FROM applications WHERE status='submitting'").fetchall()
        for r in rows:
            reason = 'Interrupted during submission - outcome unknown. Check your email/the portal before applying again.'
            _set(db, r['id'], 'manual_action_required', last_error=reason, detail=json.dumps({'reason': reason}), notified_at=None)
            _event(db, r['job_id'], 'application_uncertain', reason)
    return len(rows)


def claim_next():
    now = now_iso()
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("""SELECT a.id FROM applications a JOIN jobs j ON j.id=a.job_id
                            WHERE a.status='queued' AND (a.next_attempt_at IS NULL OR a.next_attempt_at<=?)
                            ORDER BY j.score DESC, a.id LIMIT 1""", (now,)).fetchone()
        if not row:
            return None
        db.execute("UPDATE applications SET status='submitting', attempts=attempts+1, updated_at=? WHERE id=?", (now, row['id']))
    return row['id']


def _cv_bundle(job_id):
    html = cv_path(job_id)
    pdf = html.with_suffix('.pdf')
    return {'html': html if html.is_file() else None, 'pdf': pdf if pdf.is_file() else None}


async def process(app_id, http, cfg=None):
    """Process one claimed application. Returns the final status."""
    from .. import workflow
    cfg = cfg or settings()
    with connect() as db:
        app = db.execute('SELECT * FROM applications WHERE id=?', (app_id,)).fetchone()
        job = db.execute('SELECT * FROM jobs WHERE id=?', (app['job_id'],)).fetchone()
        approval = db.execute('SELECT * FROM approvals WHERE id=?', (app['approval_id'],)).fetchone()

    def finish(status, message, **fields):
        with connect() as db:
            detail = {'reason': message, **{k: v for k, v in fields.pop('detail', {}).items() if v}}
            _set(db, app_id, status, last_error=message[:500] if status not in ('submitted', 'confirmed') else None,
                 detail=json.dumps(detail), notified_at=None, **fields)
            _event(db, job['id'], f'application_{status}', message)
            if approval is not None and status in ('manual_action_required', 'blocked', 'failed', 'dry_run'):
                db.execute("UPDATE approvals SET status='consumed', consumed_at=? WHERE id=?", (now_iso(), approval['id']))
        log.info('Application %s (job %s): %s - %s', app_id, job['id'], status, message)
        return status

    # 1. the approval must still match the job and CV that exist now
    if approval is None or approval['status'] != 'active':
        return finish('manual_action_required', 'Approval is no longer active - approve again.', detail={'reapprove': True})
    current_cv = workflow.cv_version(job['id'])
    if workflow.job_version(job) != app['job_version'] or (app['cv_version'] and app['cv_version'] != current_cv) or not current_cv:
        return finish('manual_action_required', 'The job or CV changed after your approval - review and approve again.',
                      detail={'reapprove': True})
    # 2. duplicate guard across sources: same canonical apply URL already submitted for another job
    canon = canonical_url(app['apply_url'])
    with connect() as db:
        db.execute('UPDATE applications SET canonical_apply_url=? WHERE id=?', (canon, app_id))
        dup = db.execute("SELECT job_id FROM applications WHERE canonical_apply_url=? AND id!=? AND status IN ('submitting','submitted','confirmed')",
                         (canon, app_id)).fetchone() if canon else None
    if dup:
        return finish('blocked', f"Already applied to this vacancy (job {dup['job_id']}) - duplicate prevented.")
    # 3. readiness (read-only)
    adapter = adapters.select_adapter(job)
    profile, cv = load_profile(), _cv_bundle(job['id'])
    plan = await adapter.prepare(http, job, profile, cv)
    detail = {'adapter': adapter.name, 'missing': plan.missing, 'fields': plan.fields}
    with connect() as db:
        db.execute('UPDATE applications SET adapter=?, mode=? WHERE id=?', (adapter.name, cfg['mode'], app_id))
    if plan.status == adapters.RETRY:
        return _retry_or_fail(app_id, app, job, plan.message, cfg)
    if plan.status in (adapters.MANUAL, adapters.BLOCKED):
        return finish(plan.status, plan.message, detail=detail)
    # 4. mode gates - nothing leaves the VM unless live mode + live-capable adapter + explicitly allowed
    if cfg['mode'] == 'off':
        return finish('manual_action_required', 'Ready, but automatic submission is not activated (APPLY_MODE=off) - apply manually.',
                      detail=detail)
    if cfg['mode'] == 'dry_run' or not adapter.live_capable or adapter.name not in cfg['live_adapters']:
        return finish('dry_run', f'Dry run OK via {adapter.name} - nothing was submitted. Apply manually.', detail=detail)
    # 5. live submission
    result = await adapter.submit(http, job, profile, cv, plan)
    if result.status in (adapters.SUBMITTED, adapters.CONFIRMED) and result.external_id:
        stamp = now_iso()
        status = finish(result.status, result.message or 'Submitted', external_id=result.external_id, submitted_at=stamp,
                        confirmed_at=stamp if result.status == adapters.CONFIRMED else None, detail=detail)
        with connect() as db:
            db.execute("UPDATE approvals SET status='consumed', consumed_at=? WHERE id=?", (stamp, approval['id']))
        try:
            transition(job['id'], 'applied', f'Submitted via {adapter.name}; confirmation {result.external_id}')
        except ValueError as exc:
            log.warning('Could not mark job %s applied: %s', job['id'], exc)
        return status
    if result.status == adapters.RETRY:
        return _retry_or_fail(app_id, app, job, result.message, cfg)
    if result.status in (adapters.SUBMITTED, adapters.CONFIRMED):  # no confirmation id: outcome uncertain
        return finish('manual_action_required', 'Submission returned no confirmation id - verify in your email/portal.', detail=detail)
    return finish(result.status if result.status in NOTIFY_STATES else 'manual_action_required', result.message, detail=detail)


def _retry_or_fail(app_id, app, job, message, cfg):
    with connect() as db:
        attempts = db.execute('SELECT attempts FROM applications WHERE id=?', (app_id,)).fetchone()[0]
        if attempts >= cfg['max_attempts']:
            _set(db, app_id, 'failed', last_error=message[:500], detail=json.dumps({'reason': f'Failed after {attempts} attempts: {message}'}),
                 notified_at=None)
            _event(db, job['id'], 'application_failed', message)
            return 'failed'
        retry_at = (datetime.now(timezone.utc) + timedelta(seconds=cfg['retry_base'] * 2 ** (attempts - 1))).isoformat()
        _set(db, app_id, 'queued', last_error=message[:500], next_attempt_at=retry_at)
        return 'queued'


class ApplicationWorker:
    def __init__(self, http_factory=None):
        self.http_factory = http_factory or (lambda: HttpClient(timeout=20, retries=1, default_interval=2.0))

    async def run_once(self):
        cfg = settings()
        processed = 0
        http = self.http_factory()
        try:
            while (app_id := claim_next()) is not None:
                try:
                    await process(app_id, http, cfg)
                except Exception as exc:
                    log.exception('Application %s crashed', app_id)
                    with connect() as db:  # outcome unknown -> never auto-retry a possibly sent submission
                        _set(db, app_id, 'manual_action_required', last_error=f'internal error: {type(exc).__name__}',
                             detail=json.dumps({'reason': 'Internal error during processing - check before applying manually.'}),
                             notified_at=None)
                processed += 1
        finally:
            await http.aclose()
        if processed:
            await send_application_updates()
        return processed

    async def run(self, poll=60):
        import asyncio
        recover_after_restart()
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Application worker error; continuing')
            await asyncio.sleep(poll)


async def send_application_updates(sender=None, limit=10):
    """Individual messages for application outcomes (with the direct application URL). Jobs that have a new
    Approve & Apply decision pending are covered by their action message instead."""
    from .. import workflow
    send = sender or send_telegram
    with connect() as db:
        rows = db.execute(f"""SELECT a.*, j.title, j.employer, j.company, j.score, j.url FROM applications a JOIN jobs j ON j.id=a.job_id
                              WHERE a.notified_at IS NULL AND a.status IN ({','.join('?' * len(NOTIFY_STATES))})
                              ORDER BY a.id LIMIT ?""", (*NOTIFY_STATES, limit)).fetchall()
    sent = 0
    for r in rows:
        state = workflow.job_state(r['job_id'])
        if state and any(a != workflow.REJECT for a in state['actions']):
            with connect() as db:  # the actionable message (with "Paused: ...") covers it
                db.execute('UPDATE applications SET notified_at=? WHERE id=?', (now_iso(), r['id']))
            continue
        detail = json.loads(r['detail'] or '{}')
        label = {'submitted': 'Application submitted', 'confirmed': 'Application confirmed', 'dry_run': 'Dry run only - not submitted',
                 'blocked': 'Application blocked', 'failed': 'Application failed',
                 'manual_action_required': 'Manual action needed'}[r['status']]
        lines = [f"{label}: {r['title']} — {r['employer'] or r['company']} ({r['score']}/100)", detail.get('reason') or '']
        if detail.get('missing'):
            lines.append('Missing: ' + '; '.join(detail['missing'])[:400])
        if r['external_id']:
            lines.append(f"Confirmation: {r['external_id']}")
        if r['status'] not in ('submitted', 'confirmed'):
            lines.append(f"Apply manually: {r['apply_url'] or r['url']}")
        lines.append(f'Dashboard: {dashboard_base()}')
        try:
            await send('\n'.join(x for x in lines if x))
        except Exception:
            log.exception('Telegram application update failed for application %s', r['id'])
            continue
        with connect() as db:
            db.execute('UPDATE applications SET notified_at=? WHERE id=?', (now_iso(), r['id']))
        sent += 1
    return sent
