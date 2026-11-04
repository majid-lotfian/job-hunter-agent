"""Discovery orchestration.

For every configured source (bounded concurrency, per-source interval, isolated failures):
  fetch -> skip unchanged known postings -> cheap pre-filter -> (detail fetch) -> normalize ->
  relevance + location classification -> existing score_job -> dedupe/upsert -> lifecycle -> health.
Then: CV queue hook for new logical jobs, Telegram (capped, digest for first scans of new sources).
No LLM calls happen during discovery.
"""
import asyncio
import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from .. import core
from . import config as dconfig
from . import store
from .dedupe import seniority_sig
from .http import HttpClient
from .models import (AGGREGATOR, DISABLED, EMPTY, FAILED, NOT_CONFIGURED, OK, PARTIAL, RATE_LIMITED, SKIPPED,
                     SourceError, SourceRateLimited)
from .normalize import (ats_key, canonical_url, employer_key, employment_type, language_requirement, parse_location,
                        seniority, seniority_label, title_key)
from .relevance import classify, worth_detail_fetch

log = core.log
TIER_RANK = {'primary': 0, 'secondary': 1, 'possible': 2}


def _age_days(iso, now):
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds() / 86400
    except (TypeError, ValueError):
        return None


class SourceRun:
    def __init__(self, source):
        self.source = source
        self.stats = Counter()
        self.new_jobs = []
        self.status = None
        self.error = None


async def _process(run, rec, http, ctx, cfg, now_dt, now):
    src, stats, settings = run.source, run.stats, cfg['settings']
    with core.connect() as db:
        known = store.find_link(db, rec.source_key, rec.source_job_id)
        if known and rec.updated_marker and (known['updated_marker'] or '') == rec.updated_marker:
            store.touch_link(db, known, now)
            stats['unchanged'] += 1
            return
    if rec.needs_detail:
        pre_loc = parse_location(rec.all_location_text(), rec.country, rec.remote_hint)
        if not worth_detail_fetch(rec.title, pre_loc, settings):
            stats['rejected'] += 1
            if known:
                with core.connect() as db:
                    store.touch_link(db, known, now)
            return
        try:
            rec = await src.detail(http, rec)
        except SourceError as exc:
            ctx.detail_errors += 1
            stats['errors'] += 1
            run.error = run.error or f'detail: {exc}'
            if known:
                with core.connect() as db:
                    store.touch_link(db, known, now)
            return
    loc = parse_location(rec.all_location_text(), rec.country, rec.remote_hint)
    age = _age_days(rec.published_at, now_dt)
    if rec.kind == AGGREGATOR and age is not None and age > settings['aggregator_max_age_days']:
        stats['rejected'] += 1
        stats['too_old'] += 1
        return
    cls = classify(rec.title, rec.description, loc, settings)
    if not cls.accepted:
        stats['rejected'] += 1
        if known:
            with core.connect() as db:
                store.touch_link(db, known, now)
        return
    if cls.location_ok and loc.region in ('unknown', 'remote_unspecified'):
        loc.label = 'Netherlands (per description)' if loc.region == 'unknown' else 'Remote (Europe per description)'
    # Existing scoring, unchanged. A complete employer listing proves the job is open, so the age
    # window (search.yaml max_age_days) is applied to aggregator postings only.
    score = core.score_job({'title': rec.title, 'description': rec.description, 'location': loc.label or rec.location or '',
                            'published_at': rec.published_at if rec.kind == AGGREGATOR else None}, cfg['matching'])
    sen = seniority(rec.title, rec.seniority_hint)
    keys = [k for k in dict.fromkeys([ats_key(rec.url), ats_key(rec.apply_url)]) if k]
    n = {'location': loc, 'score': max(score, 0), 'tier': cls.tier, 'reason': '; '.join(cls.reasons),
         'employer_key': employer_key(rec.employer), 'title_key': title_key(rec.title), 'seniority_sig': seniority_sig(sen),
         'seniority_label': seniority_label(sen), 'employment_type': employment_type(rec.employment_type),
         'language_req': language_requirement(rec.title, rec.description), 'canonical_url': canonical_url(rec.url),
         'canonical_apply_url': canonical_url(rec.apply_url), 'ats_keys': keys,
         'priority': store.PRIORITY.get(rec.kind, 1), 'listing': 'complete' if src.complete_listing else 'search'}
    with core.connect() as db:
        outcome, job_id, how = store.upsert(db, rec, n, now)
    stats['accepted'] += 1
    if outcome == 'new':
        stats['new'] += 1
        run.new_jobs.append({'job_id': job_id, 'score': n['score'], 'tier': cls.tier, 'title': rec.title,
                             'company': rec.employer or rec.source_key, 'location': loc.label or rec.location or '',
                             'url': rec.url, 'source_key': rec.source_key})
    elif outcome == 'merged':
        stats['duplicates'] += 1
        log.info('Dedup: %s/%s merged into job %s (%s)', rec.source_key, rec.source_job_id, job_id, how)
    else:
        stats[outcome] += 1


def _due(health, source, now_dt, force):
    if force or not health or not health['last_attempt_at']:
        return True
    try:
        last = datetime.fromisoformat(health['last_attempt_at'])
    except ValueError:
        return True
    return now_dt - last >= timedelta(minutes=source.interval_minutes) - timedelta(minutes=2)


async def _run_source(source, http, cfg, run_started, force):
    run = SourceRun(source)
    now_dt = datetime.now(timezone.utc)
    with core.connect() as db:
        health = store.health(db, source.key)
    if not source.enabled:
        run.status = DISABLED
    elif source.missing_config():
        run.status, run.error = NOT_CONFIGURED, source.missing_config()
    elif not _due(health, source, now_dt, force):
        run.status = SKIPPED
    if run.status:
        with core.connect() as db:
            store.record_health(db, source.key, source.family, run.status, enabled=source.enabled, error=run.error)
        return run
    baseline = not health or not health['last_success_at']
    from . import sources as _s
    ctx = _s.FetchContext()
    started = time.monotonic()
    attempted = core.now_iso()
    try:
        async for rec in source.fetch(http, ctx):
            run.stats['fetched'] += 1
            try:
                await _process(run, rec, http, ctx, cfg, now_dt, attempted)
            except SourceRateLimited:
                raise
            except Exception as exc:  # one bad record never stops the source
                run.stats['errors'] += 1
                run.error = run.error or f'record {rec.source_job_id}: {type(exc).__name__}: {exc}'
                log.exception('Discovery: failed to process %s/%s', rec.source_key, rec.source_job_id)
        run.status = PARTIAL if ctx.detail_errors or run.stats['errors'] else (OK if run.stats['fetched'] else EMPTY)
    except SourceRateLimited as exc:
        run.status, run.error = RATE_LIMITED, str(exc)
    except SourceError as exc:
        run.status, run.error = FAILED, str(exc)
    except Exception as exc:
        run.status, run.error = FAILED, f'{type(exc).__name__}: {exc}'
        log.exception('Discovery: source %s crashed', source.key)
    closed = 0
    if source.complete_listing and run.status == OK and not ctx.truncated:
        with core.connect() as db:
            closed = store.mark_missing(db, source.key, attempted, cfg['settings']['close_after_missed_scans'])
    run.stats['closed'] = closed
    for job in run.new_jobs:
        job['baseline'] = baseline
    with core.connect() as db:
        store.record_health(db, source.key, source.family, run.status, enabled=True, attempted=attempted,
                            stats=run.stats, error=run.error, duration_ms=int((time.monotonic() - started) * 1000))
    log.info('Discovery %s: %s fetched=%d accepted=%d new=%d dup=%d unchanged=%d rejected=%d errors=%d%s',
             source.key, run.status, run.stats['fetched'], run.stats['accepted'], run.stats['new'], run.stats['duplicates'],
             run.stats['unchanged'], run.stats['rejected'], run.stats['errors'], f' ({run.error})' if run.error else '')
    return run


async def _notify(new_jobs, cfg, notifier):
    ncfg = cfg['notifications']
    relevant = sorted((j for j in new_jobs if j['tier'] in ('primary', 'secondary') and j['score'] >= ncfg['min_score']),
                      key=lambda j: (-j['score'], TIER_RANK[j['tier']]))
    individual = [j for j in relevant if not (ncfg['digest'] and j['baseline'])]
    digest = [j for j in relevant if ncfg['digest'] and j['baseline']]
    if len(individual) > ncfg['max_individual_per_run']:
        digest, individual = relevant, []
    send_job = notifier.get('job') if notifier else core.notify
    send_text = notifier.get('text') if notifier else core.send_telegram
    sent = 0
    for j in individual:
        try:
            await send_job(j['job_id'], j)
            sent += 1
        except Exception:
            log.exception('Telegram notification failed for job %s', j['job_id'])
    if digest:
        lines = [f"Job discovery: {len(digest)} new relevant job(s)"]
        lines += [f"• {j['score']}/100 {j['title']} — {j['company']} ({j['location']})" for j in digest[:12]]
        if len(digest) > 12:
            lines.append(f'… and {len(digest) - 12} more')
        lines.append(f'Dashboard: {core.dashboard_base()}')
        try:
            await send_text('\n'.join(lines))
            sent += 1
        except Exception:
            log.exception('Telegram digest failed')
    return sent


async def run_discovery(on_new_job=None, *, sources=None, cfg=None, http=None, notifier=None, force=False):
    cfg = cfg or dconfig.load()
    core.init_db()
    config_errors = []
    if sources is None:
        sources, config_errors = dconfig.build_sources(cfg)
        for err in config_errors:
            log.warning('discovery.yaml: %s', err)
    settings = cfg['settings']
    own_http = http is None
    if own_http:
        http = HttpClient(timeout=settings['request_timeout_seconds'], retries=settings['retries'],
                          default_interval=settings['default_host_interval_seconds'],
                          host_intervals=settings['host_intervals'], max_bytes=int(settings['max_response_mb'] * 1_000_000))
    run_started = core.now_iso()
    sem = asyncio.Semaphore(max(1, int(settings['concurrency'])))

    async def guarded(source):
        async with sem:
            return await _run_source(source, http, cfg, run_started, force)
    try:
        runs = await asyncio.gather(*(guarded(s) for s in sources))
    finally:
        if own_http:
            await http.aclose()
    with core.connect() as db:
        stale = store.mark_stale(db, settings['stale_after_days'])
    totals = Counter()
    for r in runs:
        totals.update({k: r.stats[k] for k in ('fetched', 'accepted', 'new', 'duplicates', 'rejected', 'errors', 'closed', 'unchanged')})
    new_jobs = [j for r in runs for j in r.new_jobs]
    for job in sorted(new_jobs, key=lambda j: (TIER_RANK.get(j['tier'], 9), -j['score'])):
        if on_new_job and job['tier'] in ('primary', 'secondary'):
            try:
                on_new_job(job['job_id'], job['score'])  # CV queue (applies CV_MIN_SCORE itself)
            except Exception:
                log.exception('CV queue hook failed for job %s', job['job_id'])
    notified = await _notify(new_jobs, cfg, notifier)
    statuses = Counter(r.status for r in runs)
    summary = {'checked': totals['fetched'], 'new': totals['new'], 'duplicates': totals['duplicates'],
               'accepted': totals['accepted'], 'rejected': totals['rejected'], 'unchanged': totals['unchanged'],
               'closed': totals['closed'], 'stale': stale, 'errors': totals['errors'], 'notifications': notified,
               'sources': dict(statuses), 'config_errors': config_errors}
    with core.connect() as db:
        db.execute('INSERT INTO discovery_runs (started_at, finished_at, sources, fetched, new_unique, duplicates, errors, '
                   'closed, summary) VALUES (?,?,?,?,?,?,?,?,?)',
                   (run_started, core.now_iso(), len(runs), totals['fetched'], totals['new'], totals['duplicates'],
                    totals['errors'], totals['closed'], json.dumps(summary)))
    log.info('Discovery finished: %s', json.dumps(summary))
    return summary
