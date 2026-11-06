"""Discovery orchestration.

For every configured source (bounded concurrency, per-source interval, isolated failures):
  fetch -> skip unchanged known postings -> cheap pre-filter -> (detail fetch) -> normalize ->
  relevance + location classification -> score_job (title + requirements) -> dedupe/upsert -> lifecycle -> health.
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


class SourceState:
    """Per-source, per-run state: one DB connection, known ids loaded once, batched 'seen' updates.
    Keeps the per-record cost in memory for the ~98% of postings that are unchanged or irrelevant."""

    def __init__(self, source_key):
        self.db = core.connect()
        self.known = {r['source_job_id']: r for r in self.db.execute(
            'SELECT id, job_id, source_job_id, updated_marker FROM job_sources WHERE source_key=?', (source_key,))}
        self.rejected = dict(self.db.execute(
            'SELECT source_job_id, updated_marker FROM rejected_postings WHERE source_key=?', (source_key,)).fetchall())
        self.seen_links, self.seen_jobs, self.seen_rejected = set(), set(), set()
        self.source_key = source_key

    def touch(self, link):
        if link is not None:
            self.seen_links.add(link['id'])
            self.seen_jobs.add(link['job_id'])

    def flush(self, now):
        with self.db:
            for ids, sql in ((self.seen_links, 'UPDATE job_sources SET last_seen_at=?, missing_count=0, active=1 WHERE id IN ({})'),
                             (self.seen_jobs, "UPDATE jobs SET last_seen_at=?, availability='active', closed_at=NULL WHERE id IN ({})")):
                ids = list(ids)
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    self.db.execute(sql.format(','.join('?' * len(chunk))), (now, *chunk))
            rej = list(self.seen_rejected)
            for i in range(0, len(rej), 500):
                chunk = rej[i:i + 500]
                self.db.execute(f"UPDATE rejected_postings SET last_seen_at=? WHERE source_key=? AND source_job_id IN "
                                f"({','.join('?' * len(chunk))})", (now, self.source_key, *chunk))
        self.seen_links.clear(), self.seen_jobs.clear(), self.seen_rejected.clear()

    def close(self):
        self.db.close()


async def _process(run, rec, http, ctx, cfg, now_dt, now, state):
    src, stats, settings = run.source, run.stats, cfg['settings']
    known = state.known.get(rec.source_job_id)
    if known and rec.updated_marker and (known['updated_marker'] or '') == rec.updated_marker:
        state.touch(known)
        stats['unchanged'] += 1
        return
    fetched_detail = False
    if rec.needs_detail:
        if rec.updated_marker and state.rejected.get(rec.source_job_id) == rec.updated_marker:
            state.seen_rejected.add(rec.source_job_id)
            stats['rejected'] += 1
            stats['rejected_cached'] += 1
            return
        pre_loc = parse_location(rec.all_location_text(), rec.country, rec.remote_hint)
        if not worth_detail_fetch(rec.title, pre_loc, settings):
            stats['rejected'] += 1
            state.touch(known)
            return
        try:
            stats['detail_fetches'] += 1
            rec = await src.detail(http, rec)
            fetched_detail = True
        except SourceError as exc:
            ctx.detail_errors += 1
            stats['errors'] += 1
            run.error = run.error or f'detail: {exc}'
            state.touch(known)
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
        if fetched_detail:  # remember costly rejections so unchanged postings are not re-fetched
            with state.db:
                store.remember_rejection(state.db, rec.source_key, rec.source_job_id, rec.updated_marker, '; '.join(cls.reasons))
        state.touch(known)
        return
    if cls.location_ok and loc.region in ('unknown', 'remote_unspecified'):
        loc.label = 'Netherlands (per description)' if loc.region == 'unknown' else 'Remote (Europe per description)'
    # Existing scoring, unchanged. A complete employer listing proves the job is open, so the age
    # window (search.yaml max_age_days) is applied to aggregator postings only.
    language_req = language_requirement(rec.title, rec.description)
    score, parts = core.score_breakdown({'title': rec.title, 'description': rec.description,
                                         'location': loc.label or rec.location or '', 'location_region': loc.region,
                                         'language_req': language_req,
                                         'published_at': rec.published_at if rec.kind == AGGREGATOR else None}, cfg['matching'])
    sen = seniority(rec.title, rec.seniority_hint)
    keys = [k for k in dict.fromkeys([ats_key(rec.url), ats_key(rec.apply_url)]) if k]
    n = {'location': loc, 'score': max(score, 0), 'tier': cls.tier, 'reason': '; '.join(cls.reasons),
         'employer_key': employer_key(rec.employer), 'title_key': title_key(rec.title), 'seniority_sig': seniority_sig(sen),
         'seniority_label': seniority_label(sen), 'employment_type': employment_type(rec.employment_type),
         'language_req': language_req, 'canonical_url': canonical_url(rec.url),
         'canonical_apply_url': canonical_url(rec.apply_url), 'ats_keys': keys,
         'priority': store.PRIORITY.get(rec.kind, 1), 'listing': 'complete' if src.complete_listing else 'search',
         'ai_keywords': ','.join(parts.get('ai_keywords') or {}) or None}
    with state.db:
        outcome, job_id, how = store.upsert(state.db, rec, n, now)
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
    from . import sources as _s
    ctx = _s.FetchContext()
    started = time.monotonic()
    attempted = core.now_iso()
    state = SourceState(source.key)
    try:
        async for rec in source.fetch(http, ctx):
            run.stats['fetched'] += 1
            try:
                await _process(run, rec, http, ctx, cfg, now_dt, attempted, state)
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
    try:
        state.flush(attempted)  # postings seen this run (needed before closure detection)
    finally:
        state.close()
    closed = 0
    if source.complete_listing and run.status == OK and not ctx.truncated:
        with core.connect() as db:
            closed = store.mark_missing(db, source.key, attempted, cfg['settings']['close_after_missed_scans'])
    run.stats['closed'] = closed
    with core.connect() as db:
        store.record_health(db, source.key, source.family, run.status, enabled=True, attempted=attempted,
                            stats=run.stats, error=run.error, duration_ms=int((time.monotonic() - started) * 1000))
    log.info('Discovery %s: %s fetched=%d accepted=%d new=%d dup=%d unchanged=%d rejected=%d errors=%d%s',
             source.key, run.status, run.stats['fetched'], run.stats['accepted'], run.stats['new'], run.stats['duplicates'],
             run.stats['unchanged'], run.stats['rejected'], run.stats['errors'], f' ({run.error})' if run.error else '')
    return run


DIGEST_MAX_LINES = 15


async def _notify(cfg, notifier):
    """One Telegram digest of every relevant job not yet announced (persistent `jobs.notified_at`).
    Jobs are marked only after a successful send, so restarts never repeat a job and a failed send is
    retried at the next scan. Irrelevant/low-score jobs are marked silently. Returns 1 if a digest was sent."""
    ncfg = cfg['notifications']
    with core.connect() as db:
        pending = [dict(r) for r in db.execute(
            """SELECT j.id, j.title, j.employer, j.company, j.score, j.location_label, j.location, j.url, j.relevance_tier,
                      j.language_req, c.status AS cv_status FROM jobs j LEFT JOIN cv_jobs c ON c.job_id=j.id
               WHERE j.notified_at IS NULL ORDER BY j.score DESC, j.id""")]
    if not pending:
        return 0
    relevant = [j for j in pending if j['relevance_tier'] in ('primary', 'secondary') and j['score'] >= ncfg['min_score']
                and (j['location_label'] or j['location'])]
    sent = 0
    if relevant:
        lines = [f'Job discovery: {len(relevant)} new relevant job(s)']
        for j in relevant[:DIGEST_MAX_LINES]:
            cv = {'pending': 'CV queued', 'generating': 'CV generating', 'ready': 'CV ready', 'needs_review': 'CV needs review',
                  'waiting_retry': 'CV retrying later',
                  'failed': 'CV failed'}.get(j['cv_status'], 'no automatic CV')
            dutch = ' · Dutch required' if j['language_req'] == 'dutch_required' else ''
            lines.append(f"• {j['score']}/100 {j['title']} — {j['employer'] or j['company']} "
                         f"({j['location_label'] or j['location']}) · {cv}{dutch}\n  {j['url']}")
        if len(relevant) > DIGEST_MAX_LINES:
            lines.append(f'… and {len(relevant) - DIGEST_MAX_LINES} more in the dashboard')
        lines.append(f'Dashboard: {core.dashboard_base()}')
        send_text = notifier.get('text') if notifier else core.send_telegram
        try:
            await send_text('\n'.join(lines)[:3900])
            sent = 1
        except Exception:
            log.exception('Telegram discovery digest failed; will retry next scan')
            return 0
    with core.connect() as db:
        db.executemany('UPDATE jobs SET notified_at=? WHERE id=?', [(core.now_iso(), j['id']) for j in pending])
    return sent


async def run_discovery(on_new_job=None, *, sources=None, cfg=None, http=None, notifier=None, force=False):
    cfg = cfg or dconfig.load()
    core.init_db()
    config_errors = []
    config_built = sources is None
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
        store.prune_rejections(db)
        if config_built:  # sources removed from discovery.yaml: keep their history, mark them as removed
            keys = [s.key for s in sources]
            db.execute(f"UPDATE source_health SET status='removed', enabled=0 WHERE source_key NOT IN ({','.join('?' * len(keys))})", keys)
    totals = Counter()
    for r in runs:
        totals.update({k: r.stats[k] for k in ('fetched', 'accepted', 'new', 'duplicates', 'rejected', 'errors', 'closed',
                                               'unchanged', 'detail_fetches', 'rejected_cached')})
    new_jobs = [j for r in runs for j in r.new_jobs]
    for job in sorted(new_jobs, key=lambda j: (TIER_RANK.get(j['tier'], 9), -j['score'])):
        if on_new_job and job['tier'] in ('primary', 'secondary'):
            try:
                on_new_job(job['job_id'], job['score'])  # CV queue (applies CV_MIN_SCORE itself)
            except Exception:
                log.exception('CV queue hook failed for job %s', job['job_id'])
    notified = await _notify(cfg, notifier)
    statuses = Counter(r.status for r in runs)
    summary = {'checked': totals['fetched'], 'new': totals['new'], 'duplicates': totals['duplicates'],
               'accepted': totals['accepted'], 'rejected': totals['rejected'], 'unchanged': totals['unchanged'],
               'closed': totals['closed'], 'stale': stale, 'errors': totals['errors'], 'notifications': notified,
               'detail_fetches': totals['detail_fetches'], 'rejected_cached': totals['rejected_cached'],
               'http_requests': http.requests,
               'sources': dict(statuses), 'config_errors': config_errors}
    with core.connect() as db:
        db.execute('INSERT INTO discovery_runs (started_at, finished_at, sources, fetched, new_unique, duplicates, errors, '
                   'closed, summary) VALUES (?,?,?,?,?,?,?,?,?)',
                   (run_started, core.now_iso(), len(runs), totals['fetched'], totals['new'], totals['duplicates'],
                    totals['errors'], totals['closed'], json.dumps(summary)))
    log.info('Discovery finished: %s', json.dumps(summary))
    return summary
