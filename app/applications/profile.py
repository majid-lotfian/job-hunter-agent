"""Verified applicant profile (private, never committed: lives in data/, which is gitignored).

Only information you wrote and marked `verified: true` is ever used in an application. Screening questions are
answered only from your explicit `answers`; anything not covered stops the application for manual action.
See applicant_profile.example.yaml in the repository root for the format.
"""
import re
import unicodedata

import yaml

from ..core import ROOT

PROFILE_PATH = ROOT / 'data' / 'applicant_profile.yaml'


class Profile:
    def __init__(self, data):
        self.data = data or {}
        self.verified = self.data.get('verified') is True
        self.answers = list(self.data.get('answers') or [])

    def get(self, key):
        value = self.data.get(key)
        return value.strip() if isinstance(value, str) else value

    @property
    def full_name(self):
        return self.get('full_name') or ' '.join(x for x in (self.get('first_name'), self.get('last_name')) if x) or None

    def answer_for(self, *question_texts):
        """Your explicit answer for a screening question (exact text or your regex), else None. Never guessed."""
        norms = [_norm(q) for q in question_texts if q]
        for entry in self.answers:
            if not isinstance(entry, dict) or 'answer' not in entry:
                continue
            if entry.get('question') and _norm(entry['question']) in norms:
                return entry['answer']
            pattern = entry.get('match')
            if pattern and any(re.search(pattern, q, re.I) for q in question_texts if q):
                return entry['answer']
        return None


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
