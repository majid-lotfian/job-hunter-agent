"""Mobile manual-application workflow: when the agent cannot submit, send everything needed to apply from a phone,
and let the owner record "I submitted it" from Telegram.

Message (one per application, sent once): the current tailored CV as a real Telegram document (PDF) plus company,
title, score, the direct application URL, why it is not automatic and what is missing, with buttons
  🌐 Open Application              (URL button: the employer's form)
  ✅ I Submitted This Application  -> asks "Did you successfully submit ...?"  [Confirm] [Cancel]
  ⏳ Submit Later                   -> keeps it open, no reminders

Callback data `m<op>:<application id>:<token>` (op s=submitted, c=confirm, x=cancel, l=later). The token binds the
application id to its CV version and job version: a changed CV or job makes every button stale.

This is completely separate from the autonomous "Approve & Submit" path (`wf:` callbacks, workflow.decide):
recording a manual submission never queues anything, never starts the browser worker and never touches APPLY_MODE.
A manual record is USER-REPORTED: status 'submitted' with detail.submitted_manually / employer_verified=false,
no external_id and no confirmed_at (those stay reserved for employer-verified confirmations).

    .venv/bin/python -m app.applications.manual --list                 # applications waiting for manual submission
    .venv/bin/python -m app.applications.manual --notify 12            # (re)send the message for application 12
    .venv/bin/python -m app.applications.manual --route-blocked 12     # CAPTCHA-blocked application -> manual workflow
"""
import argparse
import asyncio
import hashlib
import json
import re
import sys

from .. import workflow
from ..core import connect, log, now_iso, send_telegram, send_telegram_document
from . import _app_row, _cv_bundle, _event, _set, canonical_url

MANUAL_STATES = ('manual_action_required', 'dry_run')
CALLBACK = re.compile(r'm([sclx]):(\d+):([0-9a-f]{10})')
QUESTION = "Did you successfully submit this application on the employer's website?"
SOURCE = "user-reported via Telegram (no employer reference)"


def is_manual_callback(data):
    return bool(CALLBACK.fullmatch(data or ''))


def manual_token(app):
    return workflow._h(f"manual:{app['id']}:{app['cv_version'] or '-'}:{app['job_version'] or '-'}", 10)


def _job(db, job_id):
    return db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()


def check_current(app, job):
    """(ok, reason): the application still refers to the CURRENT CV version (valid PDF) and job version."""
    if not app['cv_version'] or app['cv_version'] != workflow.cv_version(job['id']):
        return False, 'the tailored CV changed since this application was prepared'
    if app['job_version'] and app['job_version'] != workflow.job_version(job):
        return False, 'the job posting changed since this application was prepared'
    if _cv_bundle(job['id'])['pdf'] is None:
        return False, 'the CV PDF is missing, invalid or outdated'
    return True, ''


def manual_candidate(app):
    """Applications the owner should submit manually: finished in a manual state with a prepared package."""
    detail = json.loads(app['detail'] or '{}')
    return app['status'] in MANUAL_STATES and bool(detail.get('package'))


def application_url(job, detail):
    from . import browser
    return browser.form_url(job, detail.get('package') or {}) or job['apply_url'] or job['url']


def compose(app, job, detail):
    pkg = detail.get('package') or {}
    missing = list(pkg.get('missing') or []) + list(pkg.get('questions') or [])
    reason = (app['last_error'] or detail.get('reason') or 'automatic submission is not available for this form').strip()
    cv = _cv_bundle(job['id'])
    digest = hashlib.sha256(cv['pdf'].read_bytes()).hexdigest()[:12] if cv['pdf'] else '-'
    lines = [f"📝 Manual application needed (application #{app['id']}, job {job['id']})",
             f"{job['title']} — {job['employer'] or job['company']}",
             f"Match score: {job['score']}/100",
             f"Why not automatic: {reason[:400]}",
             ('Missing / needs your answer: ' + '; '.join(m[:120] for m in missing)[:700]) if missing
             else 'Nothing missing: every known field is covered by your verified profile.',
             f"Apply here: {application_url(job, detail)}",
             f"Tailored CV: attached above (version {app['cv_version']}, PDF sha256 {digest}).",
             'After you have applied, tap "I Submitted This Application". Nothing is recorded until you confirm.']
    return '\n'.join(lines)


def keyboard(app, job, detail, token):
    return {'inline_keyboard': [[{'text': '🌐 Open Application', 'url': application_url(job, detail)}],
                                [{'text': '✅ I Submitted This Application', 'callback_data': f"ms:{app['id']}:{token}"}],
                                [{'text': '⏳ Submit Later', 'callback_data': f"ml:{app['id']}:{token}"}]]}


async def send_manual_notification(app_id, *, force=False, send_doc=None, send_msg=None):
    """Send the CV PDF + details + buttons. Returns (sent, reason). Nothing is marked when delivery fails."""
    send_doc, send_msg = send_doc or send_telegram_document, send_msg or send_telegram
    with connect() as db:
        app = _app_row(db, app_id)
        job = _job(db, app['job_id']) if app else None
    if app is None:
        return False, 'unknown application'
    if not manual_candidate(app):
        return False, f"application is {app['status'].replace('_', ' ')}, not waiting for manual submission"
    detail = json.loads(app['detail'] or '{}')
    if detail.get('manual_notice') and not force:
        return False, 'already notified'
    ok, reason = check_current(app, job)
    if not ok:
        return False, reason
    token = manual_token(app)
    text = compose(app, job, detail)
    pdf = _cv_bundle(job['id'])['pdf']
    try:
        if not await send_doc(pdf, caption=f"Tailored CV for {job['title']} — {job['employer'] or job['company']}",
                              filename=f"CV-job-{job['id']}.pdf"):
            return False, 'Telegram is not configured'
        result = await send_msg(text, keyboard(app, job, detail, token))
        if not result:
            return False, 'Telegram is not configured'
    except Exception as exc:
        log.exception('Manual-application notification failed for application %s', app_id)
        return False, f'Telegram delivery failed ({type(exc).__name__})'
    message_id = result.get('message_id') if isinstance(result, dict) else None
    with connect() as db:
        fresh = json.loads(_app_row(db, app_id)['detail'] or '{}')
        fresh['manual_notice'] = {'token': token, 'cv_version': app['cv_version'], 'message_id': message_id,
                                  'text': text, 'sent_at': now_iso(), 'later_at': None}
        db.execute('UPDATE applications SET detail=?, notified_at=? WHERE id=?', (json.dumps(fresh), now_iso(), app_id))
        _event(db, job['id'], 'manual_notice_sent', f"application {app_id}: CV {app['cv_version']} PDF + buttons sent to Telegram")
    return True, ''


def record_manual_submission(app_id, *, actor):
    """The owner's own statement that they submitted on the employer's site. User-reported, never employer-verified."""
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        app = _app_row(db, app_id)
        if app is None:
            raise LookupError('Unknown application')
        if app['status'] in ('submitted', 'confirmed'):
            raise ValueError('Already recorded as submitted')
        if not manual_candidate(app):
            raise ValueError('This application is not waiting for manual submission')
        canon = app['canonical_apply_url'] or canonical_url(app['apply_url'])
        dup = db.execute("SELECT job_id FROM applications WHERE canonical_apply_url=? AND id!=? AND status IN ('submitting','submitted','confirmed')",
                         (canon, app_id)).fetchone() if canon else None
        if dup:
            raise ValueError(f"Already applied to this vacancy (job {dup['job_id']}) - duplicate prevented")
        if db.execute("SELECT 1 FROM applications WHERE job_id=? AND id!=? AND status IN ('submitting','submitted','confirmed')",
                      (app['job_id'], app_id)).fetchone():
            raise ValueError('This job already has a recorded submission')
        stamp = now_iso()
        detail = json.loads(app['detail'] or '{}')
        detail.update(submitted_manually=True, submission_method='manual (owner, employer website)',
                      confirmation_source=SOURCE, employer_verified=False, recorded_by=actor,
                      reason='Manually submitted - reported by you via Telegram (not verified by the employer).')
        _set(db, app_id, 'submitted', submitted_at=stamp, last_error=None, detail=json.dumps(detail),
             notified_at=stamp, canonical_apply_url=canon)
        if app['approval_id']:
            db.execute("UPDATE approvals SET status='consumed', consumed_at=? WHERE id=? AND status='active'", (stamp, app['approval_id']))
        _event(db, app['job_id'], 'application_submitted_manually', f'{actor}: {SOURCE}')
        if app['job_status'] != 'applied':
            db.execute("UPDATE jobs SET status='applied' WHERE id=?", (app['job_id'],))
            _event(db, app['job_id'], 'applied', f'Manual submission reported by you (application {app_id})')
    return detail


def handle_callback(data, *, user_id):
    """Already-authorized press on a manual-workflow button. Returns a dict for the Telegram layer:
    answer (popup text), remove (drop this message's buttons), followup (text, markup) to send,
    edit (message_id, text, markup) to rewrite the original notification."""
    m = CALLBACK.fullmatch(data or '')
    if not m:
        return {'answer': 'Unknown action', 'remove': False}
    op, app_id, token = m.group(1), int(m.group(2)), m.group(3)
    with connect() as db:
        app = _app_row(db, app_id)
        job = _job(db, app['job_id']) if app else None
    if app is None:
        return {'answer': 'Unknown application', 'remove': True}
    if app['status'] in ('submitted', 'confirmed'):
        return {'answer': 'Already recorded as submitted - nothing changed.', 'remove': True}
    if not manual_candidate(app):
        return {'answer': f"This application is {app['status'].replace('_', ' ')} - no manual action recorded.", 'remove': True}
    current, why = check_current(app, job)
    if token != manual_token(app) or not current:
        return {'answer': f"Outdated button ({why or 'application changed'}). Open the dashboard for the current version.",
                'remove': True}
    detail = json.loads(app['detail'] or '{}')
    notice = detail.get('manual_notice') or {}
    if op == 's':
        return {'answer': 'Please confirm.', 'remove': False,
                'followup': (f"{QUESTION}\n{job['title']} — {job['employer'] or job['company']} (application #{app_id})",
                             {'inline_keyboard': [[{'text': 'Confirm', 'callback_data': f'mc:{app_id}:{token}'},
                                                   {'text': 'Cancel', 'callback_data': f'mx:{app_id}:{token}'}]]})}
    if op == 'x':
        return {'answer': 'Cancelled - nothing was recorded.', 'remove': True}
    if op == 'l':
        with connect() as db:
            fresh = json.loads(_app_row(db, app_id)['detail'] or '{}')
            fresh.setdefault('manual_notice', {})['later_at'] = now_iso()
            db.execute('UPDATE applications SET detail=? WHERE id=?', (json.dumps(fresh), app_id))
            _event(db, job['id'], 'manual_submit_later', f'application {app_id}: kept for later (telegram:{user_id})')
        return {'answer': 'Kept for later. No reminders will be sent; use this message when you have applied.', 'remove': False}
    try:                                   # op == 'c'
        record_manual_submission(app_id, actor=f'telegram:{user_id}')
    except (LookupError, ValueError) as exc:
        return {'answer': str(exc), 'remove': True}
    out = {'answer': 'Recorded: manually submitted (reported by you, not employer-verified).', 'remove': True}
    if notice.get('message_id') and notice.get('text'):
        out['edit'] = (notice['message_id'], notice['text'] + f"\n\n✅ Manually submitted - reported by you on {now_iso()[:16].replace('T', ' ')} UTC "
                                                              '(not verified by the employer).',
                       {'inline_keyboard': [[{'text': '🌐 Open Application', 'url': application_url(job, detail)}]]})
    return out


def route_blocked_to_manual(app_id, *, actor):
    """Hand an application that a CAPTCHA/human-verification challenge blocked (before this workflow existed) to the
    manual workflow. The blocked outcome stays in the audit history and in detail['blocked']; nothing is resent."""
    from .browser import is_human_verification
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        app = _app_row(db, app_id)
        if app is None:
            raise LookupError('Unknown application')
        if app['status'] != 'blocked':
            raise ValueError(f"application is {app['status'].replace('_', ' ')}, not blocked")
        if not is_human_verification(app['last_error']):
            raise ValueError('not blocked by a CAPTCHA / human-verification challenge - left as it is')
        detail = json.loads(app['detail'] or '{}')
        if not detail.get('package'):
            raise ValueError('no prepared package for this application')
        message = ('A CAPTCHA / human-verification challenge stopped the automatic attempt - nothing was submitted. '
                   'Apply manually with the CV PDF and link sent to Telegram.')
        detail.update(prepared=True, blocked={'status': 'blocked', 'reason': app['last_error'], 'at': app['updated_at']},
                      reason=message, missing=list(detail['package'].get('missing') or []) + list(detail['package'].get('questions') or []))
        _set(db, app_id, 'manual_action_required', last_error=message, detail=json.dumps(detail), notified_at=None)
        _event(db, app['job_id'], 'routed_to_manual', f"application {app_id} ({actor}): blocked by CAPTCHA -> manual workflow; "
                                                      f"original outcome kept: {(app['last_error'] or '')[:200]}")
    return True


def pending_manual(limit=50):
    with connect() as db:
        rows = db.execute("SELECT a.*, j.title, j.employer, j.company, j.status AS job_status FROM applications a "
                          f"JOIN jobs j ON j.id=a.job_id WHERE a.status IN ({','.join('?' * len(MANUAL_STATES))}) ORDER BY a.id LIMIT ?",
                          (*MANUAL_STATES, limit)).fetchall()
    return [r for r in rows if manual_candidate(r)]


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m app.applications.manual', description=__doc__.split('\n')[0])
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--notify', type=int, metavar='APPLICATION_ID', help='(re)send the manual-application message')
    ap.add_argument('--route-blocked', type=int, metavar='APPLICATION_ID',
                    help='move a CAPTCHA-blocked application to the manual workflow and send its message')
    args = ap.parse_args(argv)
    if args.route_blocked:
        try:
            route_blocked_to_manual(args.route_blocked, actor='cli')
        except (LookupError, ValueError) as exc:
            print(f'NOT routed: {exc}')
            return 1
        sent, reason = asyncio.run(send_manual_notification(args.route_blocked))
        print('routed to manual; message ' + ('sent' if sent else f'NOT sent: {reason}'))
        return 0 if sent else 1
    if args.notify:
        sent, reason = asyncio.run(send_manual_notification(args.notify, force=True))
        print('sent' if sent else f'NOT sent: {reason}')
        return 0 if sent else 1
    for r in pending_manual():
        notice = json.loads(r['detail'] or '{}').get('manual_notice') or {}
        print(f"application {r['id']} · job {r['job_id']} · {r['title']} — {r['employer'] or r['company']} · {r['status']} · "
              f"notified {notice.get('sent_at') or 'no'}{' · later' if notice.get('later_at') else ''}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
