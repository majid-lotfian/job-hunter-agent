"""Cloud browser worker integration: hand ONE approved application to an on-demand browser and record the outcome.

The worker itself is worker/apply_worker.py (a container for Cloud Run Jobs). This module, on the e2-micro VM:
  1. builds the worker package from the prepared review package (only verified values, explicit answers),
  2. dispatches it - `local` backend (subprocess, development/tests) or `cloud_run` backend (upload the package and
     CV PDF to a private GCS bucket, start one Cloud Run Job execution, wait, read the result),
  3. maps the worker's result onto the existing application states (adapters.Result).

Enabled only with APPLY_MODE=live, `browser` in APPLY_LIVE_ADAPTERS and BROWSER_WORKER=local|cloud_run, and only
for a job whose package is complete. A challenge or login wall -> blocked (never solved); "submitted" requires the
employer's confirmation; anything unclear after the click -> manual follow-up, never resent.

Configuration (.env):
    BROWSER_WORKER=cloud_run            # or local (development), empty = disabled
    BROWSER_WORKER_BUCKET=jh-apply-xyz  # private bucket with a 1-day lifecycle rule
    BROWSER_WORKER_JOB=apply-worker     # Cloud Run Job name
    BROWSER_WORKER_REGION=europe-west4
    BROWSER_WORKER_GCLOUD_CONFIG=/home/<user>/job-hunter-agent/data/gcloud   # gcloud config dir with the worker-dispatch SA
    BROWSER_WORKER_TIMEOUT_SECONDS=300
    BROWSER_WORKER_EVIDENCE_DIR=...     # local backend: private copy of every run (default data/applications)
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

from ..core import ROOT, log
from . import adapters

# Systems whose real form fields the agent reads from the employer (app/applications/adapters.py form readers).
# The worker never runs on a guessed/generic field list: Ashby, Lever, Workday, ... stay manual until a reader exists.
SUPPORTED_FORMS = ('greenhouse', 'recruitee')
WORKER = ROOT / 'worker' / 'apply_worker.py'


def settings():
    return {'backend': os.getenv('BROWSER_WORKER', '').strip().lower(),
            'bucket': os.getenv('BROWSER_WORKER_BUCKET', '').strip(),
            'job': os.getenv('BROWSER_WORKER_JOB', 'apply-worker').strip(),
            'region': os.getenv('BROWSER_WORKER_REGION', 'europe-west4').strip(),
            'gcloud_config': os.getenv('BROWSER_WORKER_GCLOUD_CONFIG', '').strip(),
            'timeout': int(os.getenv('BROWSER_WORKER_TIMEOUT_SECONDS', '300') or 300),
            'python': os.getenv('BROWSER_WORKER_PYTHON', sys.executable),
            'channel': os.getenv('BROWSER_WORKER_CHANNEL', '').strip(),
            'evidence_root': os.getenv('BROWSER_WORKER_EVIDENCE_DIR', '').strip() or str(ROOT / 'data' / 'applications')}


def eligibility(job, package):
    """(ok, reasons) - can this prepared application be handed to the browser worker?"""
    reasons = []
    if package.get('adapter') not in SUPPORTED_FORMS:
        reasons.append(f"{package.get('adapter') or 'this system'}: the employer's form fields could not be read")
    if package.get('availability', {}).get('open') is False:
        reasons.append('vacancy closed')
    if package.get('missing') or package.get('questions'):
        reasons.append('missing information or unanswered questions')
    if not package.get('cv', {}).get('pdf_current'):
        reasons.append('no current CV PDF')
    if not package.get('profile', {}).get('verified'):
        reasons.append('profile not verified')
    if not package.get('fields'):
        reasons.append('form fields unknown (the worker never guesses fields)')
    return not reasons, reasons


def _cv_filename(package):
    names = {r['key']: r['value'] for r in package.get('fields', []) if r.get('status') == 'ok' and isinstance(r.get('value'), str)}
    who = names.get('name') or ' '.join(x for x in (names.get('first_name'), names.get('last_name')) if x) or 'CV'
    return re.sub(r'[^A-Za-z0-9]+', '-', who).strip('-')[:60] + '-CV.pdf'


def form_url(job, package):
    """The page the worker opens. Greenhouse's absolute_url is often the employer's own careers page that embeds the
    form in an iframe (e.g. careers.nebius.com/?gh_jid=...); the worker uses Greenhouse's hosted form instead."""
    job = dict(job)
    page = package.get('apply_page') or job.get('apply_url') or job.get('url') or ''
    source = job.get('source') or ''
    if source.startswith('greenhouse:') and str(job.get('source_id') or '').isdigit() \
            and not (urlparse(page).hostname or '').endswith('greenhouse.io'):
        return f"https://job-boards.greenhouse.io/{source.split(':', 1)[1]}/jobs/{job['source_id']}"
    return page


def build_worker_package(app, job, package, cv_pdf_location, submit):
    """Only fields with a verified value; the CV field points at the PDF. Nothing else leaves the VM."""
    fields = []
    for row in package['fields']:
        if row['status'] != 'ok':
            continue
        value = 'cv_pdf' if row['source'] == 'cv_pdf' else row['value']
        fields.append({'label': row['label'], 'key': row['key'], 'kind': row['kind'], 'value': value,
                       'required': bool(row['required'])})
    return {'version': 1, 'application_id': app['id'], 'job_id': job['id'], 'apply_url': form_url(job, package),
            'ats': (job['source'] or '').split(':')[0], 'cv_pdf': str(cv_pdf_location),
            'cv_filename': _cv_filename(package),
            'fields': fields, 'submit': bool(submit),
            # Greenhouse's hosted form moves to .../confirmation after a successful application
            'confirmation': {'text': [], 'url': [r'greenhouse\.io/.+/confirmation'] if (job['source'] or '').startswith('greenhouse:') else []},
            'timeouts': {'page': 45, 'action': 15, 'confirm': 30}}


# --------------------------------------------------------------------------- dispatch backends

def _run_local(pkg, cfg, workdir):
    pkg_path, result_path = workdir / 'package.json', workdir / 'result.json'
    pkg_path.write_text(json.dumps(pkg), encoding='utf-8')
    cmd = [cfg['python'], str(WORKER), '--package', str(pkg_path), '--result', str(result_path),
           '--evidence-dir', str(workdir / 'evidence')]
    if cfg['channel']:
        cmd += ['--channel', cfg['channel']]
    try:
        done = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=cfg['timeout'])
        log_text = f'exit {done.returncode}\n{done.stdout[-4000:]}\n{done.stderr[-8000:]}'
    except subprocess.TimeoutExpired as exc:
        log_text = f'timeout after {cfg["timeout"]} s\n{exc.stderr or ""}'
    result = json.loads(result_path.read_text(encoding='utf-8')) if result_path.is_file() else None
    (workdir / 'worker.log').write_text(str(log_text), encoding='utf-8')
    kept = keep_evidence(pkg, workdir, cfg)
    if result is not None:
        result['evidence_dir'] = str(kept)
    return result


def keep_evidence(pkg, workdir, cfg):
    """Keep a private copy (folder 700, files 600) of the run's package, result, worker log and screenshots."""
    from datetime import datetime
    dest = Path(cfg['evidence_root']) / f"app-{pkg['application_id']}-job-{pkg['job_id']}-{datetime.now():%Y%m%d-%H%M%S}"
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    for item in workdir.iterdir():
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
    for path in [dest, *dest.rglob('*')]:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    return dest


def _gcloud(args, cfg, timeout):
    env = {**os.environ, **({'CLOUDSDK_CONFIG': cfg['gcloud_config']} if cfg['gcloud_config'] else {})}
    return subprocess.run(['gcloud', *args, '--quiet'], env=env, capture_output=True, text=True, timeout=timeout)


def _run_cloud_run(pkg, cfg, workdir):
    """Package + PDF -> gs://BUCKET/<run>/, one job execution (waits), result <- gs://BUCKET/<run>/result.json."""
    if not cfg['bucket']:
        raise RuntimeError('BROWSER_WORKER_BUCKET is not set')
    run_id = f"app-{pkg['application_id']}-{uuid.uuid4().hex[:8]}"
    base = f"gs://{cfg['bucket']}/{run_id}"
    local_pdf = Path(pkg['cv_pdf'])
    pkg = {**pkg, 'cv_pdf': f'{base}/cv.pdf'}
    (workdir / 'package.json').write_text(json.dumps(pkg), encoding='utf-8')
    for src, dst in ((local_pdf, f'{base}/cv.pdf'), (workdir / 'package.json', f'{base}/package.json')):
        done = _gcloud(['storage', 'cp', str(src), dst], cfg, 120)
        if done.returncode != 0:
            raise RuntimeError(f'upload failed: {done.stderr.strip()[-200:]}')
    args = f'--package={base}/package.json,--result={base}/result.json,--evidence-dir={base}/evidence'
    done = _gcloud(['run', 'jobs', 'execute', cfg['job'], f"--region={cfg['region']}", '--wait', f'--args={args}'],
                   cfg, cfg['timeout'] + 120)
    fetched = _gcloud(['storage', 'cp', f'{base}/result.json', str(workdir / 'result.json')], cfg, 120)
    if fetched.returncode != 0:
        log.warning('Browser worker run %s: no result (%s)', run_id, (done.stderr or '').strip()[-200:])
        return None
    _gcloud(['storage', 'rm', f'{base}/cv.pdf', f'{base}/package.json'], cfg, 120)   # personal data: keep only result/evidence
    return json.loads((workdir / 'result.json').read_text(encoding='utf-8'))


BACKENDS = {'local': _run_local, 'cloud_run': _run_cloud_run}


def map_result(result):
    """Worker result -> adapters.Result for the existing state machine (process() applies duplicate guards etc.)."""
    if not result:
        return adapters.Result(adapters.MANUAL, 'browser worker returned no result - outcome unknown, check before retrying')
    status, reason = result.get('status'), result.get('reason') or ''
    evidence = result.get('evidence') or {}
    if status == 'submitted' and (evidence.get('confirmation_text') or evidence.get('url')):
        ref = evidence.get('screenshot_sha256', '')[:16]
        return adapters.Result(adapters.SUBMITTED, f"Submitted by the cloud browser: {evidence.get('confirmation_text', '')[:160]}",
                               external_id=f"browser:{evidence.get('at', '')[:19]}:{ref}")
    if status == 'blocked':
        return adapters.Result(adapters.BLOCKED, f'cloud browser stopped: {reason} - not submitted')
    if status == 'failed' and not result.get('submitted_clicked'):
        return adapters.Result(adapters.BLOCKED, f'cloud browser could not complete the form: {reason} - not submitted',
                               missing=result.get('missing') or [])
    if status == 'filled':
        return adapters.Result(adapters.DRY_RUN, 'cloud browser filled the form (dry run) - nothing was submitted')
    return adapters.Result(adapters.MANUAL, f'cloud browser outcome unclear ({status}): {reason} - check before retrying')


async def submit(app, job, package, cv, submit_now=True, cfg=None):
    cfg = cfg or settings()
    backend = BACKENDS.get(cfg['backend'])
    if backend is None:
        return adapters.Result(adapters.MANUAL, 'browser worker not configured (BROWSER_WORKER)')
    with tempfile.TemporaryDirectory(prefix='jh-worker-') as tmp:
        pkg = build_worker_package(app, job, package, cv['pdf'], submit_now)
        try:
            result = await asyncio.to_thread(backend, pkg, cfg, Path(tmp))
        except Exception as exc:
            log.exception('Browser worker dispatch failed for application %s', app['id'])
            return adapters.Result(adapters.MANUAL, f'browser worker error ({type(exc).__name__}) - outcome unknown, check before retrying')
    log.info('Browser worker for application %s: %s - %s (evidence: %s)', app['id'], (result or {}).get('status'),
             (result or {}).get('reason'), (result or {}).get('evidence_dir', 'none'))
    return map_result(result)
