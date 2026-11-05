"""Discovery persistence: schema migration, idempotent upserts with provenance, lifecycle, source health.

Migration is additive only (new columns/tables); existing jobs, statuses, approvals, CV paths, events
and cv_jobs rows are never deleted or rewritten. A backup copy of the database is made before the
first migration.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from ..core import DB, connect, log, now_iso
from . import dedupe
from .models import AGGREGATOR
from .normalize import ats_key, canonical_url, employer_key, parse_location, seniority, seniority_label, title_key
from .relevance import role_tier

SCHEMA_VERSION = 2
JOB_COLUMNS = {
    'employer': 'TEXT', 'employer_key': 'TEXT', 'title_key': 'TEXT', 'seniority': 'TEXT', 'seniority_sig': 'TEXT',
    'location_label': 'TEXT', 'location_region': 'TEXT', 'city': 'TEXT', 'country': 'TEXT', 'remote_type': 'TEXT',
    'employment_type': 'TEXT', 'language_req': 'TEXT', 'relevance_tier': 'TEXT', 'relevance_reason': 'TEXT',
    'last_seen_at': 'TEXT', 'availability': "TEXT DEFAULT 'active'", 'closed_at': 'TEXT',
    'source_count': 'INTEGER DEFAULT 1', 'primary_priority': 'INTEGER DEFAULT 1',
}
PRIORITY = {'direct': 3, AGGREGATOR: 1}


DDL = [
    '''CREATE TABLE IF NOT EXISTS job_sources (
                id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
                source_key TEXT NOT NULL, source_job_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'direct',
                listing TEXT NOT NULL DEFAULT 'complete', url TEXT, apply_url TEXT, canonical_url TEXT,
                canonical_apply_url TEXT, ats_key TEXT, updated_marker TEXT, published_at TEXT,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, missing_count INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1, metadata TEXT, UNIQUE(source_key, source_job_id)
            )''',
    '''CREATE INDEX IF NOT EXISTS job_sources_job ON job_sources(job_id)''',
    '''CREATE INDEX IF NOT EXISTS job_sources_canon ON job_sources(canonical_url)''',
    '''CREATE INDEX IF NOT EXISTS job_sources_canon_apply ON job_sources(canonical_apply_url)''',
    '''CREATE INDEX IF NOT EXISTS job_sources_ats ON job_sources(ats_key)''',
    '''CREATE INDEX IF NOT EXISTS jobs_match ON jobs(employer_key, title_key)''',
    '''CREATE TABLE IF NOT EXISTS source_health (
                source_key TEXT PRIMARY KEY, family TEXT, enabled INTEGER NOT NULL DEFAULT 1, status TEXT,
                last_attempt_at TEXT, last_success_at TEXT, fetched INTEGER DEFAULT 0, accepted INTEGER DEFAULT 0,
                rejected INTEGER DEFAULT 0, new_unique INTEGER DEFAULT 0, duplicates INTEGER DEFAULT 0,
                unchanged INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, last_error TEXT, duration_ms INTEGER,
                consecutive_failures INTEGER DEFAULT 0, total_new INTEGER DEFAULT 0
            )''',
    '''CREATE TABLE IF NOT EXISTS discovery_runs (
                id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, sources INTEGER, fetched INTEGER,
                new_unique INTEGER, duplicates INTEGER, errors INTEGER, closed INTEGER, summary TEXT
            )''',
]


def _backup(reason):
    if not DB.exists() or DB.stat().st_size == 0:
        return None
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target = DB.parent / 'backups' / f'{DB.stem}-{reason}-{stamp}{DB.suffix}'
    target.parent.mkdir(parents=True, exist_ok=True)
    src, dst = sqlite3.connect(DB), sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close(), dst.close()
    return target


# v2: remember postings rejected after a detail fetch, so unchanged ones are not re-fetched every scan.
DDL_V2 = ['''CREATE TABLE IF NOT EXISTS rejected_postings (
                source_key TEXT NOT NULL, source_job_id TEXT NOT NULL, updated_marker TEXT, reason TEXT,
                last_seen_at TEXT NOT NULL, PRIMARY KEY (source_key, source_job_id))''']


def _migrate_v1(db):
    existing = {r['name'] for r in db.execute('PRAGMA table_info(jobs)')}
    for column, ddl in JOB_COLUMNS.items():
        if column not in existing:
            db.execute(f'ALTER TABLE jobs ADD COLUMN {column} {ddl}')
    for statement in DDL:  # executescript() would COMMIT implicitly and break atomicity
        db.execute(statement)
    _backfill_links(db)


def _migrate_v2(db):
    for statement in DDL_V2:
        db.execute(statement)


MIGRATIONS = {1: _migrate_v1, 2: _migrate_v2}


def migrate():
    """Bring the database to SCHEMA_VERSION, one versioned step at a time, in a single transaction.
    Safe to call on every start (no-op when current)."""
    with connect() as db:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        has_jobs = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
    if version >= SCHEMA_VERSION:
        link_orphans()
        return False
    if has_jobs:
        with connect() as db:
            populated = db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        if populated:
            backup = _backup(f'pre-discovery-v{version + 1}')
            log.info('Backed up database to %s before discovery schema migration', backup)
    db = sqlite3.connect(DB, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN IMMEDIATE')
        version = db.execute('PRAGMA user_version').fetchone()[0]  # another process may have migrated meanwhile
        for step in range(version + 1, SCHEMA_VERSION + 1):
            MIGRATIONS[step](db)
        db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK')
        raise
    finally:
        db.close()
    log.info('Discovery schema migrated to version %d', SCHEMA_VERSION)
    return True


def link_orphans():
    """Jobs inserted without provenance (e.g. by an older app version after a rollback, or by hand) get their
    link, so the engine recognizes them instead of creating duplicates. Cheap no-op normally."""
    with connect() as db:
        if db.execute('SELECT 1 FROM jobs j LEFT JOIN job_sources s ON s.job_id=j.id WHERE s.id IS NULL LIMIT 1').fetchone():
            _backfill_links(db)


def _legacy_source_key(row):
    source = row['source'] or 'unknown'
    if ':' in source or source not in ('lever', 'greenhouse'):
        return source
    return f"{source}:{row['company']}"


def _backfill_links(db):
    """Give every pre-existing job a provenance link and normalized fields (statuses untouched)."""
    rows = db.execute('SELECT j.* FROM jobs j LEFT JOIN job_sources s ON s.job_id=j.id WHERE s.id IS NULL').fetchall()
    seeds = {}
    for r in rows:
        key = _legacy_source_key(r)
        aggregator = (r['company'] or '').lower() == 'jobgether'
        loc = parse_location(r['location'])
        sen = seniority(r['title'])
        tier, reason = role_tier(r['title'], r['description'] or '')
        seen = r['discovered_at']
        db.execute('''INSERT OR IGNORE INTO job_sources (job_id, source_key, source_job_id, kind, listing, url, apply_url,
                      canonical_url, canonical_apply_url, ats_key, published_at, first_seen_at, last_seen_at, metadata)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (r['id'], key, r['source_id'], AGGREGATOR if aggregator else 'direct', 'complete', r['url'], r['apply_url'],
                    canonical_url(r['url']), canonical_url(r['apply_url']), ats_key(r['url']) or ats_key(r['apply_url']),
                    r['published_at'], seen, seen, json.dumps({'migrated': True})))
        db.execute('''UPDATE jobs SET employer=COALESCE(employer, ?), employer_key=COALESCE(employer_key, ?),
                      title_key=COALESCE(title_key, ?), seniority=COALESCE(seniority, ?), seniority_sig=COALESCE(seniority_sig, ?),
                      location_label=COALESCE(location_label, ?), location_region=COALESCE(location_region, ?),
                      city=COALESCE(city, ?), country=COALESCE(country, ?), relevance_tier=COALESCE(relevance_tier, ?),
                      relevance_reason=COALESCE(relevance_reason, ?), last_seen_at=COALESCE(last_seen_at, discovered_at),
                      availability=COALESCE(availability, 'active'), source_count=COALESCE(source_count, 1),
                      primary_priority=COALESCE(primary_priority, ?) WHERE id=?''',
                   (None if aggregator else r['company'], None if aggregator else employer_key(r['company']),
                    title_key(r['title']), seniority_label(sen), dedupe.seniority_sig(sen), loc.label or r['location'],
                    loc.region, loc.city, loc.country, tier if tier != 'none' else 'legacy', reason,
                    PRIORITY[AGGREGATOR if aggregator else 'direct'], r['id']))
        seeds[key] = max(seeds.get(key, ''), seen or '')
    for key, seen in seeds.items():  # boards that already produced jobs are not "first scans"
        db.execute('INSERT OR IGNORE INTO source_health (source_key, family, status, last_attempt_at, last_success_at) '
                   'VALUES (?,?,?,?,?)', (key, key.split(':')[0], 'ok', seen, seen))


# --------------------------------------------------------------------------- upsert

def _candidates(db, employer_key_, title_key_):
    rows = db.execute("SELECT id, employer_key, title_key, seniority_sig, location_region, city, description, availability "
                      "FROM jobs WHERE employer_key=? AND title_key=?", (employer_key_, title_key_)).fetchall()
    out = []
    for r in rows:
        keys = frozenset(x[0] for x in db.execute('SELECT source_key FROM job_sources WHERE job_id=?', (r['id'],)))
        out.append(dedupe.Candidate(r['id'], r['employer_key'], r['title_key'], r['seniority_sig'] or '',
                                    r['location_region'], r['city'], r['description'] or '', keys, r['availability'] or 'active'))
    return out


def find_link(db, source_key, source_job_id):
    return db.execute('SELECT * FROM job_sources WHERE source_key=? AND source_job_id=?', (source_key, source_job_id)).fetchone()


def touch_link(db, link, now):
    """Mark a known posting as seen in this run; reopen its job if it had been closed."""
    db.execute('UPDATE job_sources SET last_seen_at=?, missing_count=0, active=1 WHERE id=?', (now, link['id']))
    db.execute("UPDATE jobs SET last_seen_at=?, availability='active', closed_at=NULL WHERE id=?", (now, link['job_id']))


def upsert(db, rec, n, now):
    """Store one accepted record. `n` holds normalized fields from the engine.
    Returns (outcome, job_id, detail) where outcome is 'seen' | 'updated' | 'merged' | 'new'."""
    link = find_link(db, rec.source_key, rec.source_job_id)
    if link:
        touch_link(db, link, now)
        if (link['updated_marker'] or '') == (rec.updated_marker or ''):
            return 'seen', link['job_id'], 'exact source id'
        db.execute('UPDATE job_sources SET updated_marker=?, url=?, apply_url=?, canonical_url=?, canonical_apply_url=?, '
                   'metadata=? WHERE id=?', (rec.updated_marker, rec.url, rec.apply_url, n['canonical_url'],
                                            n['canonical_apply_url'], json.dumps(rec.metadata)[:2000], link['id']))
        job = db.execute('SELECT source, source_id FROM jobs WHERE id=?', (link['job_id'],)).fetchone()
        if job and job['source'] == rec.source_key and job['source_id'] == rec.source_job_id:
            _write_primary(db, link['job_id'], rec, n)
        return 'updated', link['job_id'], 'exact source id (changed)'

    job_id, how = _match_existing(db, rec, n)
    if job_id:
        _insert_link(db, job_id, rec, n, now)
        job = db.execute('SELECT primary_priority FROM jobs WHERE id=?', (job_id,)).fetchone()
        db.execute("UPDATE jobs SET last_seen_at=?, availability='active', closed_at=NULL, "
                   "source_count=(SELECT COUNT(*) FROM job_sources WHERE job_id=?) WHERE id=?", (now, job_id, job_id))
        if n['priority'] > (job['primary_priority'] or 1):  # prefer the employer's own posting/apply link
            db.execute('UPDATE jobs SET source=?, source_id=?, url=?, apply_url=? WHERE id=?',
                       (rec.source_key, rec.source_job_id, rec.url, rec.apply_url or rec.url, job_id))
            _write_primary(db, job_id, rec, n, keep_score=True)
        return 'merged', job_id, how

    cur = db.execute('''INSERT INTO jobs (source, source_id, company, title, location, description, url, apply_url,
                        published_at, discovered_at, score, last_seen_at, availability, source_count)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'active', 1)''',
                     (rec.source_key, rec.source_job_id, rec.employer or rec.source_key.split(':')[-1], rec.title,
                      n['location'].label or rec.location, rec.description, rec.url, rec.apply_url or rec.url,
                      rec.published_at, now, n['score'], now))
    job_id = cur.lastrowid
    _write_primary(db, job_id, rec, n)
    _insert_link(db, job_id, rec, n, now)
    return 'new', job_id, 'new'


def _match_existing(db, rec, n):
    urls = [u for u in (n['canonical_url'], n['canonical_apply_url']) if u]
    for url in urls:
        row = db.execute('SELECT job_id FROM job_sources WHERE canonical_url=? OR canonical_apply_url=? LIMIT 1', (url, url)).fetchone()
        if row:
            return row['job_id'], 'canonical URL'
    for key in n['ats_keys']:
        row = db.execute('SELECT job_id FROM job_sources WHERE ats_key=? LIMIT 1', (key,)).fetchone()
        if row:
            return row['job_id'], f'ATS id {key}'
    if n['employer_key'] and n['title_key']:
        for cand in _candidates(db, n['employer_key'], n['title_key']):
            same, why = dedupe.is_same_job(cand, source_key=rec.source_key, employer_key=n['employer_key'],
                                           title_key=n['title_key'], seniority=n['seniority_sig'],
                                           region=n['location'].region, city=n['location'].city, description=rec.description)
            if same:
                return cand.job_id, why
    return None, None


def _insert_link(db, job_id, rec, n, now):
    db.execute('''INSERT INTO job_sources (job_id, source_key, source_job_id, kind, listing, url, apply_url, canonical_url,
                  canonical_apply_url, ats_key, updated_marker, published_at, first_seen_at, last_seen_at, metadata)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
               (job_id, rec.source_key, rec.source_job_id, rec.kind, n['listing'], rec.url, rec.apply_url,
                n['canonical_url'], n['canonical_apply_url'], (n['ats_keys'] or [None])[0], rec.updated_marker,
                rec.published_at, now, now, json.dumps(rec.metadata)[:2000]))


def _write_primary(db, job_id, rec, n, keep_score=False):
    loc = n['location']
    db.execute('''UPDATE jobs SET title=?, company=COALESCE(?, company), employer=COALESCE(?, employer), location=?,
                  description=CASE WHEN length(?) >= length(COALESCE(description, '')) OR ? THEN ? ELSE description END,
                  published_at=COALESCE(?, published_at), employer_key=COALESCE(?, employer_key), title_key=?, seniority=?,
                  seniority_sig=?, location_label=?, location_region=?, city=?, country=?, remote_type=?, employment_type=?,
                  language_req=?, relevance_tier=?, relevance_reason=?, primary_priority=?, score=CASE WHEN ? THEN score ELSE ? END
                  WHERE id=?''',
               (rec.title, rec.employer, rec.employer, loc.label or rec.location, rec.description or '', not keep_score,
                rec.description or '', rec.published_at, n['employer_key'], n['title_key'], n['seniority_label'],
                n['seniority_sig'], loc.label or rec.location, loc.region, loc.city, loc.country or rec.country,
                loc.remote_type, n['employment_type'], n['language_req'], n['tier'], n['reason'], n['priority'],
                keep_score, n['score'], job_id))


# --------------------------------------------------------------------------- lifecycle

def mark_missing(db, source_key, run_started, threshold=2):
    """After a COMPLETE successful listing: count links not seen this run; close after `threshold` misses."""
    db.execute('UPDATE job_sources SET missing_count=missing_count+1 WHERE source_key=? AND active=1 AND last_seen_at < ?',
               (source_key, run_started))
    closing = [r['job_id'] for r in db.execute(
        'SELECT job_id FROM job_sources WHERE source_key=? AND active=1 AND missing_count>=?', (source_key, threshold))]
    db.execute('UPDATE job_sources SET active=0 WHERE source_key=? AND active=1 AND missing_count>=?', (source_key, threshold))
    closed = 0
    for job_id in set(closing):
        if not db.execute('SELECT 1 FROM job_sources WHERE job_id=? AND active=1', (job_id,)).fetchone():
            closed += db.execute("UPDATE jobs SET availability='closed', closed_at=? WHERE id=? AND availability!='closed'",
                                 (now_iso(), job_id)).rowcount
    return closed


def mark_stale(db, stale_days=21, now=None):
    """Jobs only known from search/feed sources and not seen for `stale_days` become 'stale' (not closed)."""
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=stale_days)).isoformat()
    return db.execute("""UPDATE jobs SET availability='stale' WHERE availability='active' AND last_seen_at < ?
                         AND NOT EXISTS (SELECT 1 FROM job_sources s WHERE s.job_id=jobs.id AND s.listing='complete' AND s.active=1)
                         AND EXISTS (SELECT 1 FROM job_sources s WHERE s.job_id=jobs.id)""", (cutoff,)).rowcount


# --------------------------------------------------------------------------- rejected-posting memory

def rejected_unchanged(db, source_key, source_job_id, marker):
    if not marker:
        return False
    row = db.execute('SELECT updated_marker FROM rejected_postings WHERE source_key=? AND source_job_id=?',
                     (source_key, source_job_id)).fetchone()
    if row and row['updated_marker'] == marker:
        db.execute('UPDATE rejected_postings SET last_seen_at=? WHERE source_key=? AND source_job_id=?',
                   (now_iso(), source_key, source_job_id))
        return True
    return False


def remember_rejection(db, source_key, source_job_id, marker, reason):
    if marker:
        db.execute('INSERT OR REPLACE INTO rejected_postings VALUES (?,?,?,?,?)',
                   (source_key, source_job_id, marker, (reason or '')[:200], now_iso()))


def prune_rejections(db, days=45):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return db.execute('DELETE FROM rejected_postings WHERE last_seen_at < ?', (cutoff,)).rowcount


# --------------------------------------------------------------------------- source health

def health(db, source_key):
    return db.execute('SELECT * FROM source_health WHERE source_key=?', (source_key,)).fetchone()


def record_health(db, source_key, family, status, *, enabled=True, attempted=None, stats=None, error=None, duration_ms=None):
    stats = stats or {}
    success = status in ('ok', 'empty', 'partial')
    db.execute('INSERT OR IGNORE INTO source_health (source_key, family) VALUES (?, ?)', (source_key, family))
    if status == 'skipped':  # interval not elapsed: keep showing the last real outcome
        return
    if attempted is None:  # disabled / not configured: keep the last real counts
        db.execute('UPDATE source_health SET status=?, enabled=?, family=?, last_error=COALESCE(?, last_error) WHERE source_key=?',
                   (status, int(enabled), family, error, source_key))
        return
    db.execute('''UPDATE source_health SET family=?, enabled=?, status=?, last_attempt_at=?,
                  last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END, fetched=?, accepted=?, rejected=?,
                  new_unique=?, duplicates=?, unchanged=?, errors=?, last_error=?, duration_ms=?,
                  consecutive_failures=CASE WHEN ? THEN 0 ELSE consecutive_failures+1 END, total_new=total_new+?
                  WHERE source_key=?''',
               (family, int(enabled), status, attempted, success, attempted, stats.get('fetched', 0), stats.get('accepted', 0),
                stats.get('rejected', 0), stats.get('new', 0), stats.get('duplicates', 0), stats.get('unchanged', 0),
                stats.get('errors', 0), (error or '')[:300] or None, duration_ms, success, stats.get('new', 0), source_key))
