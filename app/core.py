import asyncio
import html
import json
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
        ''')


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


async def fetch_greenhouse(client, board):
    url = f'https://boards-api.greenhouse.io/v1/boards/{quote(board, safe="")}/jobs?content=true'
    response = await client.get(url)
    response.raise_for_status()
    for item in response.json().get('jobs', []):
        location = item.get('location') or {}
        yield dict(source='greenhouse', source_id=str(item['id']), company=board,
                   title=item.get('title', ''), location=location.get('name', ''),
                   description=clean_markup(item.get('content', '')),
                   url=item.get('absolute_url', ''), apply_url=item.get('absolute_url', ''),
                   published_at=item.get('updated_at'))


async def fetch_lever(client, board):
    response = await client.get(f'https://api.lever.co/v0/postings/{quote(board, safe="")}?mode=json')
    response.raise_for_status()
    for item in response.json():
        categories = item.get('categories') or {}
        yield dict(source='lever', source_id=item['id'], company=board,
                   title=item.get('text', ''), location=categories.get('location', ''),
                   description=clean_markup(item.get('descriptionPlain') or item.get('description', '')),
                   url=item.get('hostedUrl', ''), apply_url=item.get('applyUrl', ''),
                   published_at=item.get('createdAt'))


def make_cv(job_id):
    profile = config('profile.yaml')
    with connect() as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not job:
        raise ValueError('Unknown job')
    skills = profile.get('skills') or []
    description = (job['description'] or '').lower()
    relevant = sorted(skills, key=lambda s: (s.lower() not in description, s.lower()))
    esc = lambda x: html.escape(str(x or ''))
    experiences = ''.join(
        '<section><h3>' + esc(e.get('role')) + ' — ' + esc(e.get('organization')) + '</h3><small>' + esc(e.get('period')) + '</small><ul>' +
        ''.join('<li>' + esc(b) + '</li>' for b in e.get('highlights', [])) + '</ul></section>'
        for e in profile.get('experience', []))
    links = ' · '.join(f'<a href="{esc(v)}">{esc(k)}</a>' for k, v in (profile.get('links') or {}).items())
    document = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{esc(profile.get('name'))} - {esc(job['title'])}</title>
<style>body{{font:11pt/1.45 Arial,sans-serif;max-width:780px;margin:36px auto;color:#243347;padding:0 20px}}h1{{color:#173452;margin-bottom:0}}h2{{font-size:13pt;border-bottom:1px solid #cbd5e1;padding-bottom:5px;margin-top:25px}}h3{{margin-bottom:3px}}small{{color:#64748b}}a{{color:#17618b}}@media print{{body{{margin:12mm auto}}}}</style></head><body>
<h1>{esc(profile.get('name'))}</h1><strong>{esc(profile.get('headline'))}</strong><p>{esc(profile.get('email'))} · {esc(profile.get('location'))}<br>{links}</p>
<h2>Professional Summary</h2><p>{esc(profile.get('summary'))}</p><h2>Relevant Skills</h2><p>{' · '.join(map(esc, relevant))}</p>
<h2>Experience</h2>{experiences}<footer><small>Tailored for {esc(job['title'])} at {esc(job['company'])}. Review before use; all claims come from profile.yaml.</small></footer></body></html>'''
    output = ROOT / 'data' / 'cvs' / f'job-{job_id}.html'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding='utf-8')
    with connect() as db:
        db.execute('UPDATE jobs SET cv_path=? WHERE id=?', (str(output), job_id))
    return output


async def notify(job_id, job):
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat:
        return
    base = os.getenv('APP_BASE_URL', 'http://127.0.0.1:8088').rstrip('/')
    secret = os.getenv('DASHBOARD_TOKEN', '')
    # Do not embed dashboard secrets in Telegram URLs. Open dashboard and authenticate separately.
    msg = f"New match ({job['score']}/100)\n{job['title']} — {job['company']}\n{job['location']}\n{job['url']}\nDashboard: {base}"
    keyboard = {'inline_keyboard': [[{'text': 'Approve draft', 'callback_data': f'approve:{job_id}'},
                                     {'text': 'Reject', 'callback_data': f'reject:{job_id}'}]]}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f'https://api.telegram.org/bot{token}/sendMessage', json={'chat_id': chat, 'text': msg, 'reply_markup': keyboard})
        resp.raise_for_status()


async def discover():
    settings = config('search.yaml')
    source_cfg = settings.get('sources') or {}
    init_db()
    new_count = 0
    checked = 0
    async with httpx.AsyncClient(timeout=25, headers={'User-Agent': 'JobHunterAgent/0.1 (personal job monitoring)'}) as client:
        for source, boards in source_cfg.items():
            if source not in ('greenhouse', 'lever'):
                log.warning('Unsupported source: %s', source)
                continue
            for board in boards or []:
                try:
                    feed = fetch_greenhouse(client, board) if source == 'greenhouse' else fetch_lever(client, board)
                    async for job in feed:
                        checked += 1
                        score = score_job(job, settings)
                        if score < int(settings.get('matching', {}).get('min_score', 25)):
                            continue
                        job['score'] = score
                        with connect() as db:
                            cursor = db.execute('''INSERT OR IGNORE INTO jobs
                                (source,source_id,company,title,location,description,url,apply_url,published_at,discovered_at,score)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                                (job['source'],job['source_id'],job['company'],job['title'],job['location'],job['description'],job['url'],job['apply_url'],str(job['published_at'] or ''),datetime.now(timezone.utc).isoformat(),score))
                            inserted = cursor.rowcount > 0
                            job_id = cursor.lastrowid if inserted else None
                        if inserted:
                            new_count += 1
                            make_cv(job_id)
                            try:
                                await notify(job_id, job)
                            except Exception:
                                log.exception('Telegram notification failed for job %s', job_id)
                    log.info('Finished %s/%s, checked=%s, new=%s', source, board, checked, new_count)
                except Exception:
                    log.exception('Failed to fetch %s/%s', source, board)
    log.info('Discovery finished: checked=%d, new=%d', checked, new_count)
    return {'checked': checked, 'new': new_count}


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
