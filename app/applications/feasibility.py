"""Which open vacancies could be submitted autonomously, ranked. Read-only (public GETs only, nothing is submitted).

    .venv/bin/python -m app.applications.feasibility            # top matches, feasibility per vacancy
    .venv/bin/python -m app.applications.feasibility --all      # include non-English / lower-scored jobs

A vacancy is autonomously submittable only if its application system offers an applicant-permitted submission API
(today: Recruitee's public Careers Site API) AND the employer's form has its CAPTCHA disabled, asks for no consent
agreement and every required field/question can be filled from your verified profile.
"""
import argparse
import asyncio
import re
import sys

from bs4 import BeautifulSoup

from ..core import ROOT, connect, init_db
from ..discovery.http import HttpClient
from . import adapters
from .prepare import match_field
from .profile import load_profile

ROLE = re.compile(r'\b(ai|ml|llm|rag|genai|machine learning|applied ai|agentic|nlp|data scien)', re.I)
TECH = ['python', 'rag', 'retrieval', 'langchain', 'langgraph', 'agent', 'llm', 'fine-tun', 'pytorch', 'hugging face',
        'docker', 'kubernetes', 'aws', 'azure', 'gcp', 'typescript', 'java', 'golang', 'rust', 'c++', 'spark', 'mlops',
        'inference', 'cuda', 'vllm', 'evaluation', 'ranking', 'deepspeed', 'scala', 'react']
WHY_NOT = {'greenhouse': 'Greenhouse apply API needs the employer\'s Job Board API key',
           'manual': 'no applicant-permitted submission API for this system (Lever/Ashby/Workday/... need employer keys or a login)'}


def _master_terms():
    path = ROOT / 'data' / 'master_cv.html'
    text = BeautifulSoup(path.read_text(encoding='utf-8'), 'html.parser').get_text(' ').lower() if path.is_file() else ''
    return text


def _has(term, text):
    return re.search(r'(?<![a-z0-9])' + re.escape(term) + (r'' if term.endswith('-tun') else r'(?![a-z0-9])'), text) is not None


def gaps(description, cv_text):
    """Technologies the posting names that the master CV does not (whole words: 'rust' is not 'trust')."""
    d = (description or '').lower()
    return [t for t in TECH if _has(t, d) and not _has(t, cv_text)]


async def assess(job, http, profile, cv_text):
    adapter = adapters.select_adapter(job)
    row = {'id': job['id'], 'score': job['score'], 'title': job['title'], 'employer': job['employer'] or job['company'],
           'ats': (job['source'] or '').split(':')[0], 'language': job['language_req'] or '?',
           'gaps': gaps(adapters._text(job['description']), cv_text), 'url': job['apply_url'] or job['url']}
    if not adapter.live_capable:
        row.update(autonomous=False, reason=WHY_NOT.get(adapter.name, WHY_NOT['manual']))
        return row
    spec = await adapter.form(http, job)
    blockers = []
    if spec.available is not True:
        blockers.append('vacancy closed' if spec.available is False else 'vacancy not confirmed')
    if spec.captcha is not False:
        blockers.append('CAPTCHA on the form' if spec.captcha else 'CAPTCHA setting unknown')
    if spec.consent_required is not False:
        blockers.append('consent agreement required' if spec.consent_required else 'consent setting unknown')
    open_items = [f.label for f in spec.fields
                  if match_field(f, profile, {'pdf': True})['status'] in ('missing', 'needs_answer')]
    if open_items:
        blockers.append('needs your input: ' + '; '.join(open_items)[:160])
    row.update(autonomous=not blockers, reason='; '.join(blockers) or 'eligible: no CAPTCHA, no consent box, all fields known')
    return row


async def run(include_all=False, limit=25):
    init_db()
    with connect() as db:
        jobs = db.execute("""SELECT * FROM jobs WHERE COALESCE(availability,'active')='active'
                             AND status NOT IN ('rejected','applied','interview','closed') ORDER BY score DESC, id""").fetchall()
    jobs = [j for j in jobs if ROLE.search(j['title'] or '')]
    if not include_all:
        jobs = [j for j in jobs if j['score'] >= 70 and not (j['language_req'] or '').startswith('dutch')]
    jobs = jobs[:limit] if not include_all else jobs
    profile, cv_text = load_profile(), _master_terms()
    http = HttpClient(timeout=20, retries=1, default_interval=1.5)
    try:
        rows = [await assess(j, http, profile, cv_text) for j in jobs]
    finally:
        await http.aclose()
    rows.sort(key=lambda r: (not r['autonomous'], -r['score'], len(r['gaps'])))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m app.applications.feasibility', description=__doc__.split('\n')[0])
    parser.add_argument('--all', action='store_true', help='include Dutch-language and lower-scored jobs')
    parser.add_argument('--limit', type=int, default=25)
    args = parser.parse_args(argv)
    rows = asyncio.run(run(args.all, args.limit))
    print('| # | Score | Job | Employer | ATS | Lang | Autonomous | Reason | Not in your CV |')
    print('|---|---|---|---|---|---|---|---|---|')
    for r in rows:
        print(f"| {r['id']} | {r['score']} | {r['title']} | {r['employer']} | {r['ats']} | {r['language']} | "
              f"{'YES' if r['autonomous'] else 'no'} | {r['reason']} | {', '.join(r['gaps']) or '-'} |")
    print(f"\n{sum(r['autonomous'] for r in rows)} of {len(rows)} vacancies can be submitted autonomously.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
