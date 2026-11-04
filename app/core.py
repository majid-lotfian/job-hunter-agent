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
        -- status: pending | generating | ready | failed | needs_review
        CREATE TABLE IF NOT EXISTS cv_jobs (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id), status TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT 'new', attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT, last_error TEXT, warnings TEXT, notes TEXT,
            model TEXT, source TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS cv_jobs_due ON cv_jobs(status, next_attempt_at);
        ''')
    from .discovery import store  # lazy: store imports core
    store.migrate()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_cv_job(job_id):
    with connect() as db:
        return db.execute('SELECT * FROM cv_jobs WHERE job_id=?', (job_id,)).fetchone()


def set_cv_job(job_id, status, **fields):
    """Insert or update the cv_jobs row for a job. Unspecified columns keep their value."""
    allowed = {'reason', 'attempts', 'next_attempt_at', 'last_error', 'warnings', 'notes', 'model', 'source'}
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


def score_job(job, settings):
    m = settings.get('matching', {})
    title = job['title'].lower()
    body = (job['title'] + ' ' + job.get('description', '')).lower()
    loc = (job.get('location') or '').lower()
    if any(term.lower() in title for term in m.get('excluded_terms', [])):
        return -1
    title_match = any(term.lower() in title for term in m.get('titles_any', []))
    keyword_match = any(term.lower() in body for term in m.get('keywords_any', []))
    if not (title_match or keyword_match):
        return -1
    places = m.get('locations_any', [])
    if places and not any(p.lower() in loc for p in places):
        return -1
    published = parse_date(job.get('published_at'))
    if published and published < datetime.now(timezone.utc) - timedelta(days=int(m.get('max_age_days', 30))):
        return -1
    score = (50 if title_match else 0) + (15 if keyword_match else 0)
    score += min(30, 5 * sum(k.lower() in body for k in m.get('keywords_any', [])))
    return min(score, 100)


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
