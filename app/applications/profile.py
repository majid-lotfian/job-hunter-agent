"""Verified applicant profile (private, never committed: lives in data/, which is gitignored).

Only information you wrote and marked `verified: true` is ever used in an application. Screening questions are
answered only from your explicit `answers`; anything not covered is flagged for you, never guessed.
See applicant_profile.example.yaml in the repository root for the format.

Review / update (on the VM, project directory):
    .venv/bin/python -m app.applications.profile --init-from-cv   # first draft from data/master_cv.html (never overwrites)
    .venv/bin/python -m app.applications.profile --check          # validation report (no personal values printed)
    .venv/bin/python -m app.applications.profile --show           # print the profile
    nano data/applicant_profile.yaml                               # edit; set `verified: true` once everything is right
"""
import argparse
import re
import sys
import unicodedata

import yaml

from ..core import ROOT

PROFILE_PATH = ROOT / 'data' / 'applicant_profile.yaml'

TEXT_FIELDS = ('full_name', 'first_name', 'last_name', 'email', 'phone', 'location', 'nationality', 'work_authorization',
               'linkedin', 'github', 'portfolio', 'salary_expectation', 'notice_period', 'availability')
LIST_FIELDS = ('preferred_locations', 'work_arrangements')
KNOWN = set(TEXT_FIELDS) | set(LIST_FIELDS) | {'verified', 'languages', 'answers'}
REQUIRED = ('email', 'phone', 'location', 'work_authorization')     # plus a name (full_name or first + last)
ARRANGEMENTS = {'onsite', 'hybrid', 'remote'}
EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$')
URL = re.compile(r'^https://[^\s]+\.[^\s]+$')


class Profile:
    def __init__(self, data):
        self.data = data if isinstance(data, dict) else {}
        self.verified = self.data.get('verified') is True
        self.answers = list(self.data.get('answers') or [])

    def get(self, key):
        value = self.data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, str):
            return value.strip() or None
        return value

    @property
    def full_name(self):
        return self.get('full_name') or ' '.join(x for x in (self.get('first_name'), self.get('last_name')) if x) or None

    @property
    def languages(self):
        langs = self.data.get('languages') or {}
        return {str(k).strip(): str(v).strip() for k, v in langs.items()} if isinstance(langs, dict) else {}

    def answer_for(self, *question_texts):
        """Your explicit answer for a screening question (exact text or your regex), else None. Never guessed."""
        norms = [_norm(q) for q in question_texts if q]
        for entry in self.answers:
            if not isinstance(entry, dict) or 'answer' not in entry:
                continue
            if entry.get('question') and _norm(entry['question']) in norms:
                return entry['answer']
            pattern = entry.get('match')
            if pattern:
                try:
                    if any(re.search(pattern, q, re.I) for q in question_texts if q):
                        return entry['answer']
                except re.error:
                    continue
        return None

    def validate(self):
        """Returns (errors, warnings). Errors make the profile unusable for applications; no values are echoed."""
        errors, warnings = [], []
        if not self.full_name:
            errors.append('name missing (full_name, or first_name + last_name)')
        for key in REQUIRED:
            if not self.get(key):
                errors.append(f'{key} missing')
        email = self.get('email')
        if email and not EMAIL.match(email):
            errors.append('email is not a valid address')
        phone = self.get('phone')
        if phone and not (8 <= len(re.sub(r'\D', '', phone)) <= 15 and re.fullmatch(r'\+?[\d\s().-]+', phone)):
            errors.append('phone must be digits with an optional leading + (8-15 digits)')
        for key in ('linkedin', 'github', 'portfolio'):
            value = self.get(key)
            if value and not URL.match(value):
                errors.append(f'{key} must be a full https:// URL')
        for key in LIST_FIELDS:
            value = self.data.get(key)
            if value is not None and not (isinstance(value, list) and all(isinstance(x, str) for x in value)):
                errors.append(f'{key} must be a list of text values')
        bad = {str(x).lower() for x in (self.data.get('work_arrangements') or []) if isinstance(x, str)} - ARRANGEMENTS
        if bad:
            errors.append(f'work_arrangements may only contain {sorted(ARRANGEMENTS)}')
        if self.data.get('languages') is not None and not isinstance(self.data.get('languages'), dict):
            errors.append('languages must be a mapping, e.g. {English: Fluent, Dutch: B1}')
        for i, entry in enumerate(self.answers, 1):
            if not isinstance(entry, dict) or 'answer' not in entry or not (entry.get('question') or entry.get('match')):
                errors.append(f'answers[{i}] needs `answer` and `question` or `match`')
            elif entry.get('match'):
                try:
                    re.compile(entry['match'])
                except re.error:
                    errors.append(f'answers[{i}].match is not a valid regular expression')
        unknown = sorted(set(self.data) - KNOWN)
        if unknown:
            warnings.append(f'unknown keys ignored: {", ".join(unknown)}')
        for key in ('salary_expectation', 'notice_period', 'availability', 'preferred_locations', 'work_arrangements'):
            if self.data.get(key) in (None, '', []):
                warnings.append(f'{key} not set (asked by some employers)')
        if not self.verified:
            errors.append('not marked `verified: true` - review every value, then set it')
        return errors, warnings


def _norm(text):
    text = unicodedata.normalize('NFKC', str(text)).lower()
    return re.sub(r'[^a-z0-9]+', ' ', text).strip()


def load_profile(path=None):
    path = path or PROFILE_PATH
    if not path.is_file():
        return None
    try:
        return Profile(yaml.safe_load(path.read_text(encoding='utf-8')))
    except yaml.YAMLError:
        return None


# --------------------------------------------------------------------------- first draft from the master CV

LEVEL = r'(Native|Fluent|Proficient|Professional|Advanced|Intermediate|Basic|Elementary|Beginner|[ABC][12])'


def draft_from_cv(master_html):
    """Values that are literally present in the master CV header/languages; everything else stays empty.
    Returns (data, notes). The draft is never marked verified."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(master_html, 'html.parser')
    data, notes = {'verified': False}, []
    h1 = soup.find('h1')
    if h1:
        name = h1.get_text(' ', strip=True)
        data['full_name'] = name
        parts = name.split()
        if len(parts) >= 2:
            data['first_name'], data['last_name'] = parts[0], ' '.join(parts[1:])
            notes.append('first_name/last_name split from the full name - check')
    header = soup.find('header') or soup.find(class_='contact') or soup
    for a in header.find_all('a', href=True):
        href, label = a['href'].strip(), a.get_text(' ', strip=True).lower()
        if href.lower().startswith('mailto:'):
            data['email'] = href[7:].split('?')[0]
        elif href.lower().startswith('tel:'):
            data['phone'] = a.get_text(' ', strip=True) or href[4:]
        elif label == 'linkedin' or 'linkedin.com' in href.lower():
            data['linkedin'] = href
        elif label == 'github' or 'github.com' in href.lower():
            data['github'] = href
        elif label in ('portfolio', 'website', 'homepage'):
            data['portfolio'] = href
    text = header.get_text(' · ', strip=True)
    if 'phone' not in data:                      # phone shown as plain text instead of a tel: link
        phone = re.search(r'\+\d[\d\s().-]{6,}\d', text)
        if phone:
            data['phone'] = phone.group(0).strip()
    loc = re.search(r"([A-Z][\w'-]+(?: [A-Z][\w'-]+)*, (?:The Netherlands|Netherlands|Nederland))", text)
    if loc:
        data['location'] = loc.group(1)
    nat = re.search(r'\b([A-Z][a-z]+) (?:citizen|national)\b', text)
    if nat:
        data['nationality'] = nat.group(1)
        if nat.group(1) == 'Dutch':
            data['work_authorization'] = 'Dutch citizen (EU) - no work permit or sponsorship needed in the Netherlands/EU'
            notes.append('work_authorization derived from "Dutch citizen" in your CV - check')
    langs = dict(re.findall(r'\b([A-Z][a-z]+):\s*' + LEVEL, soup.get_text(' ')))
    if langs:
        data['languages'] = langs
    for key in LIST_FIELDS:
        data[key] = []
    for key in ('salary_expectation', 'notice_period', 'availability'):
        data[key] = None
    data['answers'] = []
    if data.get('work_authorization'):
        data['answers'] = [
            {'match': r'authori[sz]ed to work|right to work|work permit', 'answer': True},
            {'match': r'(require|need).{0,30}sponsorship|visa sponsorship', 'answer': False},
        ]
        notes.append('two screening answers (authorized to work: yes, sponsorship: no) derived from your nationality - check')
    return data, notes


HEADER = """# Applicant profile - PRIVATE (data/ is gitignored). Used only when `verified: true`.
# Drafted from data/master_cv.html; empty values were not in your CV and are never guessed.
# Review every value, fill what you want employers to see, then set verified: true.
# Check: .venv/bin/python -m app.applications.profile --check
"""


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m app.applications.profile', description='Review the applicant profile.')
    parser.add_argument('--check', action='store_true', help='validate (prints no personal values)')
    parser.add_argument('--show', action='store_true', help='print the profile')
    parser.add_argument('--init-from-cv', action='store_true', help='create a draft from the master CV (never overwrites)')
    args = parser.parse_args(argv)
    if args.init_from_cv:
        if PROFILE_PATH.exists():
            print(f'{PROFILE_PATH} already exists - not overwritten.')
            return 1
        master = ROOT / 'data' / 'master_cv.html'
        if not master.is_file():
            print('data/master_cv.html not found.')
            return 1
        data, notes = draft_from_cv(master.read_text(encoding='utf-8'))
        PROFILE_PATH.write_text(HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding='utf-8')
        PROFILE_PATH.chmod(0o600)
        print(f'Draft written to {PROFILE_PATH} (mode 600, verified: false). '
              f'Filled: {", ".join(k for k, v in data.items() if v not in (None, [], False))}')
        for note in notes:
            print(f'  check: {note}')
    profile = load_profile()
    if profile is None:
        print('No readable profile at data/applicant_profile.yaml (use --init-from-cv, or copy applicant_profile.example.yaml).')
        return 1
    if args.show:
        print(yaml.safe_dump(profile.data, sort_keys=False, allow_unicode=True))
    errors, warnings = profile.validate()
    filled = sorted(k for k in KNOWN if profile.data.get(k) not in (None, '', [], {}))
    print(f'Fields set: {", ".join(filled)}')
    for e in errors:
        print(f'ERROR: {e}')
    for w in warnings:
        print(f'note: {w}')
    print('Profile OK - usable for applications.' if not errors else 'Profile NOT usable until the errors are fixed.')
    return 0 if not errors else 2


if __name__ == '__main__':
    sys.exit(main())
