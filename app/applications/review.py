"""Owner review before a live submission: send the tailored CV PDF and the exact application details to Telegram,
with a fresh "Approve & Submit" button for this one job.

    .venv/bin/python -m app.applications.review --job 38            # print the review (read-only, nothing sent)
    .venv/bin/python -m app.applications.review --job 38 --send     # send CV PDF + details + button to Telegram
    .venv/bin/python -m app.applications.review --job 38 --reapprove-note "owner verified: no application received"

- The button carries the job's current action token (job version + CV version). A changed job or CV makes it stale,
  so an earlier approval or message can never be reused for a different CV, job or application.
- Pressing it goes through the normal approval path (workflow.decide): one approval, one queued application, the
  usual duplicate guards. Whether it is submitted or only prepared depends on APPLY_MODE=live AND the job being in
  APPLY_LIVE_JOB_IDS.
- A job whose previous application ended without a clear outcome is only offered again after
  --reapprove-note records that the owner verified it was not received.
"""
import argparse
import asyncio
import hashlib
import json
import sys

from .. import workflow
from ..core import connect, log, now_iso, send_telegram, send_telegram_document
from ..discovery.http import HttpClient
from . import _cv_bundle, _package, adapters, browser, live_allowed, settings
from .profile import load_profile


def allow_reapproval(job_id, note):
    """Record the owner's verification that the previous attempt was not received and allow one new approval."""
    with connect() as db:
        last = workflow.latest_application(db, job_id)
        if last is None or last['status'] not in workflow.FINISHED_APP_STATES:
            return False
        detail = json.loads(last['detail'] or '{}')
        detail.update(reapprove=True, reapprove_note=note[:300], reapprove_at=now_iso())
        db.execute('UPDATE applications SET detail=? WHERE id=?', (json.dumps(detail), last['id']))
        db.execute("UPDATE jobs SET action_notified_at=NULL WHERE id=?", (job_id,))
        db.execute('INSERT INTO events (job_id, action, note, created_at) VALUES (?,?,?,?)',
                   (job_id, 'reapproval_allowed', f"application {last['id']}: {note}"[:500], now_iso()))
    return True


async def build_review(job_id, http=None):
    """(ok, problems, text, context) - read-only: vacancy, form, verified profile, CV PDF."""
    with connect() as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if job is None:
        return False, ['unknown job'], '', {}
    state = workflow.job_state(job_id)
    profile, cv = load_profile(), _cv_bundle(job_id)
    cv_ver = workflow.cv_version(job_id)
    package = await _package(job, adapters.select_adapter(job), http or HttpClient(timeout=20, retries=1, default_interval=2.0),
                             profile, cv, cv_ver)
    problems = []
    if workflow.APPROVE_APPLY not in state['actions']:
        problems.append(f"no fresh approval possible now (actions: {state['actions'] or 'none'}; application: "
                        f"{state['application_status'] or 'none'})")
    ok_worker, reasons = browser.eligibility(job, package)
    problems += reasons
    if package['availability'].get('open') is not True:
        problems.append('vacancy not confirmed open')
    pdf_hash = hashlib.sha256(cv['pdf'].read_bytes()).hexdigest()[:12] if cv['pdf'] else None
    lines = [f"APPLICATION REVIEW - job {job_id}",
             f"{job['title']} - {job['employer'] or job['company']}",
             f"Score {job['score']}/100 · {job['location_label'] or job['location'] or ''}",
             f"Form: {package['adapter']} · {browser.form_url(job, package)}",
             f"Vacancy open: {package['availability'].get('open')} ({package['availability'].get('checked_via')})",
             f"CV version {cv_ver} · PDF sha256 {pdf_hash} (attached above)", '', 'Will be submitted:']
    for row in package['fields']:
        if row['status'] == 'ok':
            value = 'tailored CV PDF (above)' if row['source'] == 'cv_pdf' else row['value']
            lines.append(f"• {row['label']}: {', '.join(value) if isinstance(value, list) else value}")
    empty = [row['label'] for row in package['fields'] if row['status'] != 'ok']
    if empty:
        lines += ['', 'Left empty (optional, or not answered): ' + '; '.join(e[:60] for e in empty)]
    if package['missing'] or package['questions']:
        lines += ['', 'MISSING: ' + '; '.join((package['missing'] + package['questions']))[:600]]
    lines += ['', 'Optional consent boxes are never ticked. The agent submits once; a CAPTCHA, login or form error stops it '
                  '(never retried). It reports the employer\'s confirmation.']
    live = live_allowed(job_id, settings())
    lines.append('Pressing Approve & Submit SUBMITS this application now.' if live else
                 'Live submission is NOT enabled for this job right now: approving would only prepare it.')
    return not problems, problems, '\n'.join(lines), {'job': job, 'state': state, 'cv': cv, 'live': live}


async def send_review(job_id):
    ok, problems, text, ctx = await build_review(job_id)
    if not ok:
        return False, problems
    state, cv, job = ctx['state'], ctx['cv'], ctx['job']
    await send_telegram_document(cv['pdf'], caption=f"Tailored CV for job {job_id}: {job['title']} (CV version {state['cv_version']})",
                                 filename=f"CV-job-{job_id}.pdf")
    label = 'Approve & Submit' if ctx['live'] else 'Prepare Application'
    markup = {'inline_keyboard': [[{'text': f'{label} (job {job_id})',
                                    'callback_data': f"wf:{workflow.CODE_FOR[workflow.APPROVE_APPLY]}:{job_id}:{state['token']}"},
                                   {'text': 'Reject', 'callback_data': f"wf:{workflow.CODE_FOR[workflow.REJECT]}:{job_id}:{state['token']}"}]]}
    await send_telegram(text[:4000], markup)
    with connect() as db:   # the regular action digest must not send a second button for this version
        db.execute('UPDATE jobs SET action_notified_at=?, action_version=? WHERE id=?', (now_iso(), state['token'], job_id))
        db.execute('INSERT INTO events (job_id, action, note, created_at) VALUES (?,?,?,?)',
                   (job_id, 'review_sent', f"Telegram review with CV {state['cv_version']}, token {state['token']}, "
                                           f"live={ctx['live']}", now_iso()))
    log.info('Review for job %s sent to Telegram (CV %s, live=%s)', job_id, state['cv_version'], ctx['live'])
    return True, []


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m app.applications.review', description=__doc__.split('\n')[0])
    ap.add_argument('--job', type=int, required=True)
    ap.add_argument('--send', action='store_true', help='send CV PDF + details + approval button to Telegram')
    ap.add_argument('--reapprove-note', help='owner verified the previous attempt was not received: allow one new approval')
    args = ap.parse_args(argv)
    if args.reapprove_note:
        print('re-approval allowed' if allow_reapproval(args.job, args.reapprove_note) else 'nothing to re-approve')
    if args.send:
        sent, problems = asyncio.run(send_review(args.job))
        print('sent' if sent else 'NOT sent: ' + '; '.join(problems))
        return 0 if sent else 1
    ok, problems, text, _ = asyncio.run(build_review(args.job))
    print(text)
    print('\nREADY' if ok else '\nNOT READY: ' + '; '.join(problems))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
