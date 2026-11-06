import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml
from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.getenv('DB_PATH', './data/jobs.sqlite3'))
if not DB.is_absolute():
    DB = ROOT / DB
DB.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)
log = logging.getLogger('jobhunter')

# Secrets that can appear in exception text (httpx puts the full request URL, including the Telegram bot token,
# into HTTPStatusError messages and tracebacks).
SECRET_PATTERNS = (
    (re.compile(r'(api\.telegram\.org/(?:file/)?bot)[^/\s\'"]+'), r'\1<redacted>'),
    (re.compile(r'\bbot\d{5,}:[A-Za-z0-9_-]{20,}'), 'bot<redacted>'),
    (re.compile(r'([?&](?:key|api_key|app_key|token)=)[^&\s\'"]+', re.I), r'\1<redacted>'),
)


def redact_secrets(text):
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formatter that scrubs tokens/keys from the whole record, tracebacks included."""

    def format(self, record):
        return redact_secrets(super().format(record))


def _install_redaction():
    for handler in logging.getLogger().handlers:
        if not isinstance(handler.formatter, RedactingFormatter):
            fmt = handler.formatter
            handler.setFormatter(RedactingFormatter(fmt._fmt if fmt else '%(asctime)s %(levelname)s %(message)s',
                                                    fmt.datefmt if fmt else None))


_install_redaction()


def config(filename):
    with (ROOT / filename).open(encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def connect():
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    return db


def init_db():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
            company TEXT NOT NULL, title TEXT NOT NULL, location TEXT,
            description TEXT, url TEXT NOT NULL, apply_url TEXT,
            published_at TEXT, discovered_at TEXT NOT NULL, score INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'new', cv_path TEXT,
            UNIQUE(source,source_id)
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, action TEXT NOT NULL,
            note TEXT, created_at TEXT NOT NULL
        );
        -- Automatic CV generation queue/state, one row per job (see app/cv_pipeline.py).
        -- status: pending | generating | waiting_retry | ready | failed | needs_review | cancelled
        CREATE TABLE IF NOT EXISTS cv_jobs (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id), status TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT 'new', attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT, last_error TEXT, warnings TEXT, notes TEXT,
            model TEXT, source TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS cv_jobs_due ON cv_jobs(status, next_attempt_at);
        -- Small persistent key/value state for the LLM client (shared overload cooldown), survives restarts.
        CREATE TABLE IF NOT EXISTS llm_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        ''')
        # Additive cv_jobs columns: daily automatic quota and persistent digest notification state.
        cv_cols = {r['name'] for r in db.execute('PRAGMA table_info(cv_jobs)')}
        for column in ('quota_day', 'notified_at', 'completed_at'):
            if column not in cv_cols:
                db.execute(f'ALTER TABLE cv_jobs ADD COLUMN {column} TEXT')
        if 'notified_at' not in cv_cols:  # outcomes that existed before digests were already announced
            db.execute("UPDATE cv_jobs SET notified_at=updated_at WHERE status IN ('ready','needs_review','failed')")
        # Transient-failure recovery state (app/cv_pipeline.py): retry cycle, start of the current transient episode.
        for column, ddl in (('retry_cycle', 'INTEGER NOT NULL DEFAULT 0'), ('transient_since', 'TEXT'),
                            ('last_error_kind', 'TEXT'),
                            # PDF copy of the HTML CV (app/pdf.py): pending | ready | failed, retried independently
                            ('pdf_status', 'TEXT'), ('pdf_error', 'TEXT'), ('pdf_attempts', 'INTEGER NOT NULL DEFAULT 0'),
                            ('pdf_next_attempt_at', 'TEXT'), ('pdf_info', 'TEXT')):
            if column not in cv_cols:
                db.execute(f'ALTER TABLE cv_jobs ADD COLUMN {column} {ddl}')
        if 'retry_cycle' not in cv_cols:
            # One-time: jobs that older code failed permanently only because Gemini was temporarily overloaded or
            # rate limited (5xx/429/timeout/network) get another chance. They wait in waiting_retry and are retried
            # automatically (already announced, so no new failure message). Validation/permanent failures stay failed.
            db.execute(f"""UPDATE cv_jobs SET status='waiting_retry', attempts=0, retry_cycle=1, next_attempt_at=?,
                               transient_since=COALESCE(transient_since, updated_at), last_error_kind='overload',
                               last_error='Recovered after transient failure: ' || last_error, updated_at=?
                           WHERE status='failed' AND last_error LIKE 'Gave up after % attempts: %'
                             AND ({' OR '.join("last_error LIKE ?" for _ in TRANSIENT_FAILURE_MARKERS)})""",
                       (now_iso(), now_iso(), *[f'%{m}%' for m in TRANSIENT_FAILURE_MARKERS]))
    from .discovery import store  # lazy: store imports core
    store.migrate()


TRANSIENT_FAILURE_MARKERS = ('HTTP 429', 'HTTP 500', 'HTTP 502', 'HTTP 503', 'HTTP 504', 'HTTP 408',
                             'RESOURCE_EXHAUSTED', 'UNAVAILABLE', 'timed out', 'network error')


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_cv_job(job_id):
    with connect() as db:
        return db.execute('SELECT * FROM cv_jobs WHERE job_id=?', (job_id,)).fetchone()


def set_cv_job(job_id, status, **fields):
    """Insert or update the cv_jobs row for a job. Unspecified columns keep their value."""
    allowed = {'reason', 'attempts', 'next_attempt_at', 'last_error', 'warnings', 'notes', 'model', 'source',
               'quota_day', 'notified_at', 'completed_at', 'retry_cycle', 'transient_since', 'last_error_kind',
               'pdf_status', 'pdf_error', 'pdf_attempts', 'pdf_next_attempt_at', 'pdf_info'}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f'Unknown cv_jobs fields: {sorted(unknown)}')
    now = now_iso()
    with connect() as db:
        db.execute('INSERT OR IGNORE INTO cv_jobs (job_id,status,created_at,updated_at) VALUES (?,?,?,?)',
                   (job_id, status, now, now))
        cols = ['status=?', 'updated_at=?'] + [f'{k}=?' for k in fields]
        db.execute(f'UPDATE cv_jobs SET {",".join(cols)} WHERE job_id=?',
                   (status, now, *fields.values(), job_id))


def clean_markup(value):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', ' ', html.unescape(value or ''))).strip()


def parse_date(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).replace(tzinfo=timezone.utc) if datetime.fromisoformat(str(value).replace('Z', '+00:00')).tzinfo is None else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None


# Target role titles (in addition to search.yaml `titles_any`). Weight = title points.
PRIMARY_TITLES = [
    r'machine learning (engineer|scientist|researcher)', r'\bml (engineer|scientist)', r'applied (ai )?scientist',
    r'research (engineer|scientist)', r'deep learning (engineer|scientist)', r'\bnlp (engineer|scientist)',
    r'natural language processing', r'foundation models? (engineer|scientist)', r'\bllm (engineer|scientist|researcher)',
    r'generative ai (engineer|scientist)', r'\bgen ?ai (engineer|scientist)', r'agentic (ai )?engineer',
    r'ai research scientist', r'\bai (engineer|scientist|researcher)', r'applied ai', r'computer vision (engineer|scientist)',
]
SECONDARY_TITLES = [
    r'\b(ai|ml|machine learning|llm) platform engineer', r'\b(ml|llm)\s*-?ops\b', r'ai solutions? (engineer|architect)',
    r'\b(ai|ml) architect', r'forward[- ]deployed', r'data scientist', r'\bai consultant',
]
SCORE_WEIGHTS = {'title_primary': 40, 'title_secondary': 30, 'title_keyword_only': 10, 'keyword': 5, 'keyword_cap': 20,
                 'skill': 4, 'requirements_cap': 45, 'seniority': 5, 'location': 5, 'dutch_required': -10}


# Strong AI expressions: one family is enough to lift an otherwise suitable AI/ML role to >= 70.
STRONG_AI_KEYWORDS = {
    'RAG': [r'\brag\b', r'\bretrieval[- ]augmented(?:[- ]generation)?\b'],
    'agent': [r'\bai[- ]agents?\b', r'\bagent[- ]based\b', r'\bmulti[- ]agents?\b', r'\bautonomous agents?\b', r'\bagents?\b'],
    'agentic': [r'\bagentic\b'],
    'LLM': [r'\bllm[- ]based\b', r'\blarge language models?\b', r'\bllms?\b'],
    'fine-tuning': [r'\bfine[- ]?tun(?:e|es|ed|ing)\b'],
}
_STRONG_RE = {fam: [re.compile(p, re.I) for p in pats] for fam, pats in STRONG_AI_KEYWORDS.items()}
# Contexts where a keyword does not mean AI (removed before matching).
_KEYWORD_FALSE_POSITIVES = re.compile(
    r'\b(?:real[- ]estate|travel|insurance|customer[- ]service|customer|support|sales|call[- ]cent(?:er|re)|booking|'
    r'leasing|letting|user|change|free|secret|shipping|customs|transfer|patent|border|ticket|service)\s+agents?\b|'
    r'\brag\s+(?:status|rating|report(?:ing)?)\b|red[/ -]amber[/ -]green|\bll\.\s?m\.?|\bllm\s+(?:degree|in\s+law)\b|'
    r'master\s+of\s+laws', re.I)
KEYWORD_FLOOR, KEYWORD_STEP, KEYWORD_FLOOR_CAP = 70, 5, 90


def strong_ai_keywords(text):
    """{family: [matched expressions]} for the strong AI keyword families, word-boundary and case-insensitive."""
    clean = _KEYWORD_FALSE_POSITIVES.sub(' ', text or '')
    found = {}
    for fam, patterns in _STRONG_RE.items():
        hits = []
        for p in patterns:
            for m in p.finditer(clean):
                expr = re.sub(r'\s+', ' ', m.group(0).lower())
                if expr not in hits:
                    hits.append(expr)
        if hits:
            found[fam] = hits[:4]
    return found


def _title_points(title, matching):
    t = title.lower()
    if any(term.lower() in t for term in matching.get('titles_any', [])) or any(re.search(p, t) for p in PRIMARY_TITLES):
        return SCORE_WEIGHTS['title_primary'], 'primary'
    if any(re.search(p, t) for p in SECONDARY_TITLES):
        return SCORE_WEIGHTS['title_secondary'], 'secondary'
    return 0, None


def score_breakdown(job, settings):
    """Transparent 0-100 relevance score. Returns (score, parts); score -1 = rejected by search.yaml gates.

    A matching title alone is not enough to reach CV_MIN_SCORE (70): the description must also show relevant
    requirements (search.yaml keywords + distinct AI/ML skills). Seniority, location and Dutch-language
    requirements adjust the score. Gates (excluded terms, locations_any, max_age_days) are unchanged.
    """
    from .discovery.normalize import language_requirement, parse_location, seniority
    from .discovery.relevance import ai_signal_count
    m = settings.get('matching', {})
    title = job['title'] or ''
    description = job.get('description') or ''
    body = (title + ' ' + description).lower()
    loc = (job.get('location') or '').lower()
    if any(term.lower() in title.lower() for term in m.get('excluded_terms', [])):
        return -1, {'gate': 'excluded term'}
    title_pts, title_kind = _title_points(title, m)
    kw_hits = sum(k.lower() in body for k in m.get('keywords_any', []))
    if not (title_pts or kw_hits):
        return -1, {'gate': 'no title or keyword match'}
    places = m.get('locations_any', [])
    if places and not any(p.lower() in loc for p in places):
        return -1, {'gate': 'location'}
    published = parse_date(job.get('published_at'))
    if published and published < datetime.now(timezone.utc) - timedelta(days=int(m.get('max_age_days', 30))):
        return -1, {'gate': 'too old'}
    w = SCORE_WEIGHTS
    if not title_pts:
        title_pts, title_kind = w['title_keyword_only'], 'keyword_only'
    skills = ai_signal_count(description)
    requirements = min(w['requirements_cap'], min(w['keyword_cap'], w['keyword'] * kw_hits) + w['skill'] * skills)
    markers = seniority(title)
    seniority_pts = w['seniority'] if markers & {'mid', 'senior', 'staff', 'lead', 'principal', 'level2', 'level3', 'level4'} else 0
    region = job.get('location_region') or parse_location(job.get('location') or '').region
    location_pts = w['location'] if region in ('randstad', 'nl_unspecified', 'remote_nl', 'nl_other') else 0
    language = job.get('language_req') or language_requirement(title, description)
    language_pts = w['dutch_required'] if language == 'dutch_required' else 0
    base = max(0, min(100, title_pts + requirements + seniority_pts + location_pts + language_pts))
    # Strong AI keyword priority: only for roles that are otherwise suitable (relevant AI/ML/LLM technical role,
    # all gates passed, no Dutch-language requirement). One family -> >= 70; each extra family +5 (max 90).
    from .discovery.relevance import role_tier
    ai_keywords = strong_ai_keywords(f'{title}\n{description[:20000]}')
    tier, _ = role_tier(title, description)
    floor = 0
    # 'possible' = unusual title with an AI-heavy description: require the keyword in the title itself, because
    # company boilerplate ("we build AI agents") would otherwise lift generic engineering roles.
    suitable = tier in ('primary', 'secondary') or (tier == 'possible' and strong_ai_keywords(title))
    if ai_keywords and suitable and language != 'dutch_required':
        floor = min(KEYWORD_FLOOR_CAP, KEYWORD_FLOOR + KEYWORD_STEP * (len(ai_keywords) - 1))
    score = max(base, floor)
    return score, {'title': title_pts, 'title_kind': title_kind, 'keywords': kw_hits, 'skills': skills,
                   'requirements': requirements, 'seniority': seniority_pts, 'location': location_pts, 'language': language_pts,
                   'base': base, 'ai_keywords': ai_keywords, 'keyword_floor': floor, 'role_tier': tier}


def score_job(job, settings):
    return score_breakdown(job, settings)[0]


def get_job(job_id):
    with connect() as db:
        return db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()


def cv_path(job_id):
    """Location of the tailored HTML CV for a job (app.cv_pipeline or app.tailor_cv)."""
    return ROOT / 'data' / 'cvs' / f'job-{int(job_id)}.html'


def dashboard_base():
    return os.getenv('APP_BASE_URL', 'http://127.0.0.1:8088').rstrip('/')


def review_keyboard(job_id):
    return {'inline_keyboard': [[{'text': 'Approve draft', 'callback_data': f'approve:{job_id}'},
                                 {'text': 'Reject', 'callback_data': f'reject:{job_id}'}]]}


async def send_telegram(text, reply_markup=None):
    """Send a Telegram message if configured. Never put dashboard secrets in the text."""
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat:
        return False
    payload = {'chat_id': chat, 'text': text, **({'reply_markup': reply_markup} if reply_markup else {})}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f'https://api.telegram.org/bot{token}/sendMessage', json=payload)
        resp.raise_for_status()
    return True


async def notify(job_id, job):
    # Do not embed dashboard secrets in Telegram URLs. Open dashboard and authenticate separately.
    msg = f"New match ({job['score']}/100)\n{job['title']} — {job['company']}\n{job['location']}\n{job['url']}\nDashboard: {dashboard_base()}"
    await send_telegram(msg, review_keyboard(job_id))


async def discover(on_new_job=None, force=False):
    """Scan all configured sources (discovery.yaml + search.yaml boards) and store new matches.
    `on_new_job(job_id, score)` is called once per new logical job (used to queue CV generation)."""
    from .discovery.engine import run_discovery  # lazy: engine imports core
    return await run_discovery(on_new_job=on_new_job, force=force)


def transition(job_id, status, note=''):
    valid = {'approved', 'rejected', 'needs_revision', 'applied', 'interview', 'closed'}
    if status not in valid:
        raise ValueError('Invalid status')
    with connect() as db:
        job = db.execute('SELECT id,status FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job:
            raise ValueError('Unknown job')
        if status == 'applied' and job['status'] != 'approved':
            raise ValueError('Approve the draft first; record applied only after actual external submission')
        if status == 'applied' and not note.strip():
            raise ValueError('Provide a real submission confirmation before marking applied')
        db.execute('UPDATE jobs SET status=? WHERE id=?', (status, job_id))
        db.execute('INSERT INTO events (job_id,action,note,created_at) VALUES (?,?,?,?)',
                   (job_id,status,note,datetime.now(timezone.utc).isoformat()))
