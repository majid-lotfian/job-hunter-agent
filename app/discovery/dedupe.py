"""Cross-source duplicate detection (pure functions; the database lookups live in store.py).

Layers, strongest first:
  1. same (source_key, source_job_id)                      -> same job (idempotent re-scan)
  2. same canonical posting/apply URL (tracking removed)   -> same job
  3. same ATS job identifier found in any URL              -> same job (e.g. aggregator copy of a Greenhouse job)
  4. fuzzy: same normalized employer AND title AND seniority AND compatible location, never two different ids
     from the same board, and (when both have real descriptions) similar descriptions.
When in doubt: do not merge.
"""
import re
from dataclasses import dataclass

NL_REGIONS = ('randstad', 'nl_other', 'nl_unspecified', 'remote_nl')
MIN_DESC = 400
DESC_THRESHOLD = 0.35
REPOST_THRESHOLD = 0.9   # same aggregator, near-identical text: a repost (e.g. per location), not a new requisition


@dataclass
class Candidate:
    """What we know about an already-stored logical job (from the database)."""
    job_id: int
    employer_key: str
    title_key: str
    seniority: str          # comma-joined sorted markers, '' when none
    region: str
    city: str
    description: str
    source_keys: frozenset  # boards this job is already linked from
    availability: str = 'active'


def seniority_sig(markers):
    return ','.join(sorted(markers or ()))


def location_compatible(region_a, city_a, region_b, city_b):
    if city_a and city_b:
        return city_a.lower() == city_b.lower()
    a_nl, b_nl = region_a in NL_REGIONS, region_b in NL_REGIONS
    if a_nl and b_nl:
        return True          # e.g. 'Amsterdam' vs 'Netherlands'
    if region_a == region_b and region_a not in (None, 'unknown', 'foreign'):
        return True          # e.g. both 'remote_europe'
    return False


_WORD = re.compile(r'[a-z0-9]+')


def _shingles(text, n=3, limit=4000):
    words = _WORD.findall((text or '').lower()[:limit])
    return {' '.join(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def description_similarity(a, b):
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def is_same_job(candidate, *, source_key, employer_key, title_key, seniority, region, city, description, aggregator=False):
    """Conservative fuzzy match of a new record against a stored job. Returns (bool, reason)."""
    if not employer_key or not title_key or candidate.availability == 'closed':
        return False, 'missing employer/title or job closed'
    if candidate.employer_key != employer_key or candidate.title_key != title_key:
        return False, 'employer/title differ'
    if (candidate.seniority or '') != (seniority or ''):
        return False, 'seniority differs'
    if source_key in candidate.source_keys:
        # Employer boards: a second id is a different requisition. Aggregators often list the same vacancy
        # several times; merge only near-identical postings there.
        if not (aggregator and len(description or '') >= MIN_DESC and len(candidate.description or '') >= MIN_DESC
                and location_compatible(candidate.region, candidate.city, region, city)
                and description_similarity(candidate.description, description) >= REPOST_THRESHOLD):
            return False, 'different requisition on the same board'
        return True, 'repost on the same aggregator (near-identical description)'
    if not location_compatible(candidate.region, candidate.city, region, city):
        return False, 'locations differ'
    if len(candidate.description or '') >= MIN_DESC and len(description or '') >= MIN_DESC:
        sim = description_similarity(candidate.description, description)
        if sim < DESC_THRESHOLD:
            return False, f'descriptions differ ({sim:.2f})'
        return True, f'employer+title+location, description similarity {sim:.2f}'
    if candidate.city and city:
        return True, 'employer+title+city (no full descriptions to compare)'
    return False, 'too little evidence'
