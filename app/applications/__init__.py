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
- The CV attachment is the PDF rendered from the current HTML CV (app.pdf). If it is missing, invalid or older than
  the HTML, nothing is submitted: the application stops for manual action and a new PDF is requested.
- An application left in 'submitting' by a crash/restart is never retried automatically (outcome unknown).

Supervised (browser-assisted) applications: every processed application gets a review package
(app/applications/prepare.py: vacancy check, CV PDF, form fields, profile matches, missing info, screening questions).
When automatic submission is off, not permitted, or the employer's form uses a CAPTCHA, the application stops in
manual_action_required with detail.prepared=True: you open the employer's page, submit yourself, and record it with
mark_submitted() - which requires your confirmation text. Nothing here ever claims a submission on its own.
"""
import json
import os
from datetime import datetime, timedelta, timezone

from ..core import connect, cv_path, dashboard_base, log, now_iso, send_telegram, transition
from ..discovery.http import HttpClient
from ..discovery.normalize import canonical_url
from ..pdf import pdf_is_current
from . import adapters
from .prepare import build_package
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
    """Attachment selection: the HTML CV (for review) and its PDF - the PDF only if it is valid and was rendered
    from exactly the current HTML (a stale or broken PDF is never attached)."""
    html = cv_path(job_id)
    pdf = html.with_suffix('.pdf')
    return {'html': html if html.is_file() else None, 'pdf': pdf if pdf_is_current(html, pdf) else None}


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
    # 3. the CV attachment must be a valid PDF of the approved HTML CV
    profile, cv = load_profile(), _cv_bundle(job['id'])
    if cv['pdf'] is None:
        from .. import cv_pipeline
        cv_pipeline.request_pdf(job['id'])          # the CV worker renders it in its next batch (no Gemini)
        return finish('manual_action_required', 'CV PDF is missing, invalid or outdated - nothing was submitted. '
                      'A new PDF was requested; approve again once it is ready.',
                      detail={'missing': ['CV as PDF'], 'reapprove': True})
    # 4. readiness (read-only) and the review package for supervised submission
    adapter = adapters.select_adapter(job)
    plan = await adapter.prepare(http, job, profile, cv)
    detail = {'adapter': adapter.name, 'missing': plan.missing, 'fields': plan.fields}
    with connect() as db:
        db.execute('UPDATE applications SET adapter=?, mode=? WHERE id=?', (adapter.name, cfg['mode'], app_id))
    if plan.status == adapters.RETRY:
        return _retry_or_fail(app_id, app, job, plan.message, cfg)
    package = await _package(job, adapter, http, profile, cv, current_cv)
    detail['package'] = package
    if package['availability']['open'] is False:
        return finish('blocked', 'The vacancy is no longer open - nothing was submitted.', detail=detail)
    if plan.status == adapters.BLOCKED:
        return finish(plan.status, plan.message, detail=detail)
    # 4b. on-demand cloud browser worker: only when the owner enabled it, the API path is not available and the
    #     package is complete (app/applications/browser.py). It stops at any login or human-verification challenge.
    from . import browser
    via_browser = False
    if cfg['mode'] == 'live' and 'browser' in cfg['live_adapters'] and browser.settings()['backend'] \
            and not package.get('autonomous', {}).get('possible') and plan.status != adapters.BLOCKED:
        via_browser, reasons = browser.eligibility(job, package)
        detail['browser'] = {'eligible': via_browser, 'reasons': reasons}
    # 5. mode gates - nothing leaves the VM unless live mode + live-capable adapter + explicitly allowed + a form that
    #    permits autonomous submission (no CAPTCHA, no consent agreement, every required field known)
    if not via_browser and (plan.status == adapters.MANUAL or cfg['mode'] == 'off' or package['captcha']
                            or not package.get('autonomous', {}).get('possible')):
        detail.update(prepared=True, missing=package['missing'] + package['questions'])
        return finish('manual_action_required', prepared_message(package, plan.message if plan.status == adapters.MANUAL else None),
                      detail=detail)
    if not via_browser and (cfg['mode'] == 'dry_run' or not adapter.live_capable or adapter.name not in cfg['live_adapters']):
        detail['prepared'] = True
        return finish('dry_run', f'Dry run OK via {adapter.name} - nothing was submitted. Apply manually.', detail=detail)
    # 6. live submission (exactly one attempt; the application is in 'submitting', so a crash is never resent)
    if via_browser:
        method = 'browser'
        with connect() as db:
            db.execute('UPDATE applications SET adapter=? WHERE id=?', ('browser', app_id))
        result = await browser.submit(app, job, package, cv)
    else:
        method = adapter.name
        result = await adapter.submit(http, job, profile, cv, plan)
    if result.status in (adapters.SUBMITTED, adapters.CONFIRMED) and result.external_id:
        stamp = now_iso()
        status = finish(result.status, result.message or 'Submitted', external_id=result.external_id, submitted_at=stamp,
                        confirmed_at=stamp if result.status == adapters.CONFIRMED else None, detail=detail)
        with connect() as db:
            db.execute("UPDATE approvals SET status='consumed', consumed_at=? WHERE id=?", (stamp, approval['id']))
        try:
            transition(job['id'], 'applied', f'Submitted via {method}; confirmation {result.external_id}')
        except ValueError as exc:
            log.warning('Could not mark job %s applied: %s', job['id'], exc)
        return status
    if result.status == adapters.RETRY:
        return _retry_or_fail(app_id, app, job, result.message, cfg)
    if result.status in (adapters.SUBMITTED, adapters.CONFIRMED):  # no confirmation id: outcome uncertain
        return finish('manual_action_required', 'Submission returned no confirmation id - verify in your email/portal.', detail=detail)
    if result.missing:
        detail['missing'] = result.missing
    return finish(result.status if result.status in NOTIFY_STATES else 'manual_action_required', result.message, detail=detail)


async def _package(job, adapter, http, profile, cv, cv_version):
    try:
        return await build_package(job, adapter, http, profile, cv, cv_version)
    except Exception as exc:   # preparation problems never break the state machine
        log.exception('Preparing the application package for job %s failed', job['id'])
        return {'prepared_at': now_iso(), 'job': {'id': job['id'], 'title': job['title'], 'employer': job['employer'] or job['company']},
                'apply_page': job['apply_url'] or job['url'], 'adapter': adapter.name, 'availability': {'open': None, 'checked_via': ''},
                'captcha': None, 'submission': 'browser', 'posting_changed': False,
                'cv': {'version': cv_version, 'html': f"/api/jobs/{job['id']}/cv", 'pdf': f"/api/jobs/{job['id']}/cv.pdf",
                       'pdf_current': bool(cv.get('pdf'))},
                'fields': [], 'missing': [], 'questions': [], 'profile': {'verified': bool(profile and profile.verified), 'errors': [], 'notes': []},
                'warnings': [f'Could not read the employer\'s form ({type(exc).__name__}) - check the page yourself.'], 'notes': [], 'ready': False}


def prepared_message(package, reason=None):
    parts = ['Prepared for your review - submit it yourself in your browser, then record the confirmation.']
    if package.get('captcha'):
        parts.append('The employer\'s form uses a CAPTCHA.')
    if package['missing'] or package['questions']:
        parts.append(f"Needs your input: {len(package['missing'])} missing item(s), {len(package['questions'])} question(s).")
    if reason and 'missing information' not in reason:
        parts.append(reason)
    return ' '.join(parts)


def _app_row(db, app_id):
    return db.execute('SELECT a.*, j.title, j.employer, j.company, j.status AS job_status FROM applications a '
                      'JOIN jobs j ON j.id=a.job_id WHERE a.id=?', (app_id,)).fetchone()


def is_prepared(app):
    return app is not None and app['status'] in ('manual_action_required', 'dry_run') \
        and bool(json.loads(app['detail'] or '{}').get('prepared'))


async def refresh_package(app_id, http=None):
    """Re-check a prepared application (vacancy, form, profile, CV PDF) - read-only, never submits."""
    from .. import workflow
    with connect() as db:
        app = _app_row(db, app_id)
        job = db.execute('SELECT * FROM jobs WHERE id=?', (app['job_id'],)).fetchone() if app else None
    if not is_prepared(app):
        raise ValueError('Only prepared applications can be refreshed')
    own = http is None
    http = http or HttpClient(timeout=20, retries=1, default_interval=2.0)
    try:
        package = await _package(job, adapters.select_adapter(job), http, load_profile(), _cv_bundle(job['id']),
                                 workflow.cv_version(job['id']))
    finally:
        if own:
            await http.aclose()
    with connect() as db:
        detail = json.loads(app['detail'] or '{}')
        detail.update(package=package, missing=package['missing'] + package['questions'], reason=prepared_message(package))
        db.execute('UPDATE applications SET detail=?, updated_at=? WHERE id=?', (json.dumps(detail), now_iso(), app_id))
        _event(db, app['job_id'], 'application_refreshed', 'Application package re-checked')
    return package


def mark_submitted(app_id, confirmation, *, confirmed, actor='dashboard'):
    """Record YOUR manual submission. Requires a prepared application, your explicit confirmation flag and a
    confirmation text (employer reference, email subject or similar). Never called automatically."""
    confirmation = (confirmation or '').strip()
    if confirmed is not True:
        raise ValueError('Confirm that you submitted the application yourself')
    if len(confirmation) < 3:
        raise ValueError('Enter the confirmation you received (reference number, email subject, ...)')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        app = _app_row(db, app_id)
        if app is None:
            raise LookupError('Unknown application')
        if app['status'] in ('submitted', 'confirmed'):
            raise ValueError('Already recorded as submitted')
        if not is_prepared(app):
            raise ValueError('Only a prepared application can be marked as submitted')
        canon = app['canonical_apply_url'] or canonical_url(app['apply_url'])
        dup = db.execute("SELECT job_id FROM applications WHERE canonical_apply_url=? AND id!=? AND status IN ('submitting','submitted','confirmed')",
                         (canon, app_id)).fetchone() if canon else None
        if dup:
            raise ValueError(f"Already applied to this vacancy (job {dup['job_id']}) - duplicate prevented")
        if db.execute("SELECT 1 FROM applications WHERE job_id=? AND id!=? AND status IN ('submitted','confirmed')",
                      (app['job_id'], app_id)).fetchone():
            raise ValueError('This job already has a recorded submission')
        stamp = now_iso()
        detail = json.loads(app['detail'] or '{}')
        detail.update(submitted_manually=True, confirmation=confirmation[:300], recorded_by=actor,
                      reason='Submitted by you in your browser (confirmation recorded).')
        _set(db, app_id, 'submitted', external_id=f'manual:{confirmation[:200]}', submitted_at=stamp, last_error=None,
             detail=json.dumps(detail), notified_at=stamp, canonical_apply_url=canon)
        _event(db, app['job_id'], 'application_submitted_manually', f'{actor}: {confirmation[:300]}')
        if app['job_status'] != 'applied':
            db.execute("UPDATE jobs SET status='applied' WHERE id=?", (app['job_id'],))
            _event(db, app['job_id'], 'applied', f'Manual submission confirmed: {confirmation[:300]}')
    return 'submitted'


CANCELLABLE = ('awaiting_cv', 'queued', 'manual_action_required', 'dry_run', 'blocked', 'failed')


def cancel(app_id, actor='dashboard'):
    """Withdraw a not-yet-submitted application; the job can be prepared again later."""
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        app = _app_row(db, app_id)
        if app is None:
            raise LookupError('Unknown application')
        if app['status'] not in CANCELLABLE:
            raise ValueError(f"Cannot cancel an application that is {app['status'].replace('_', ' ')}")
        _set(db, app_id, 'cancelled', notified_at=now_iso())
        if app['approval_id']:
            db.execute("UPDATE approvals SET status='revoked' WHERE id=? AND status='active'", (app['approval_id'],))
        db.execute("UPDATE cv_jobs SET status='cancelled', updated_at=? WHERE job_id=? AND status IN ('pending','waiting_retry') "
                   "AND reason='approved'", (now_iso(), app['job_id']))
        if app['job_status'] == 'approved':
            db.execute("UPDATE jobs SET status='new', action_notified_at=NULL WHERE id=?", (app['job_id'],))
        _event(db, app['job_id'], 'application_cancelled', f'{actor}: cancelled before submission')
    return 'cancelled'


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
        if detail.get('prepared') and r['status'] in ('manual_action_required', 'dry_run'):
            pkg = detail.get('package') or {}
            lines = [f"Application ready for review: {r['title']} — {r['employer'] or r['company']} ({r['score']}/100)",
                     'Submit it yourself in your browser' + (' (the form uses a CAPTCHA)' if pkg.get('captcha') else '') + '.',
                     f"Needs your input: {len(pkg.get('missing') or [])} missing item(s), {len(pkg.get('questions') or [])} question(s)."
                     if (pkg.get('missing') or pkg.get('questions')) else 'All known fields are filled from your verified profile.',
                     f"Review: {dashboard_base()}/applications/{r['id']}",
                     f"Apply manually: {r['apply_url'] or r['url']}",
                     'Nothing has been submitted.']
            try:
                await send('\n'.join(lines))
            except Exception:
                log.exception('Telegram application update failed for application %s', r['id'])
                continue
            with connect() as db:
                db.execute('UPDATE applications SET notified_at=? WHERE id=?', (now_iso(), r['id']))
            sent += 1
            continue
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
