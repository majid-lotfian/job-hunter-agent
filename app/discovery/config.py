"""Load discovery.yaml (+ legacy search.yaml `sources`) and build source adapters."""
from ..core import config as load_yaml
from .sources.ats import ATS_SOURCES
from .sources.boards import BOARD_SOURCES

SLUG_FIELD = {'greenhouse': 'board', 'lever': 'company', 'ashby': 'board', 'smartrecruiters': 'company',
              'workable': 'account', 'recruitee': 'company', 'personio': 'company', 'teamtailor': 'subdomain',
              'workday': 'tenant'}
DEFAULT_SETTINGS = {
    'concurrency': 3, 'request_timeout_seconds': 20, 'max_response_mb': 15, 'retries': 2,
    'close_after_missed_scans': 2, 'stale_after_days': 21, 'aggregator_max_age_days': 30,
    'accept_nl_outside_randstad': True, 'allow_relocation': False, 'default_host_interval_seconds': 1.0,
    'host_intervals': {'apply.workable.com': 6, 'myworkdayjobs.com': 3, 'remotive.com': 5, 'remoteok.com': 5,
                       'boards-api.greenhouse.io': 0.5, 'api.lever.co': 0.5, 'api.ashbyhq.com': 0.5,
                       'api.smartrecruiters.com': 0.5, 'jobs.booking.com': 5},
}
DEFAULT_NOTIFICATIONS = {'min_score': None}


def _read(filename):
    try:
        return load_yaml(filename) or {}
    except FileNotFoundError:
        return {}


def load():
    cfg = _read('discovery.yaml')
    legacy = _read('search.yaml')
    settings = {**DEFAULT_SETTINGS, **(cfg.get('settings') or {})}
    settings['host_intervals'] = {**DEFAULT_SETTINGS['host_intervals'], **((cfg.get('settings') or {}).get('host_intervals') or {})}
    notifications = {**DEFAULT_NOTIFICATIONS, **(cfg.get('notifications') or {})}
    if notifications['min_score'] is None:
        notifications['min_score'] = int((legacy.get('matching') or {}).get('min_score', 25))
    sources = dict(cfg.get('sources') or {})
    # Legacy search.yaml boards (e.g. VM-local `lever: [jobgether, sambatv]`) keep working.
    for family, slugs in (legacy.get('sources') or {}).items():
        if family not in ('greenhouse', 'lever'):
            continue
        entries = list(sources.get(family) or [])
        known = {e.get(SLUG_FIELD[family]) for e in entries if isinstance(e, dict)}
        entries += [{SLUG_FIELD[family]: s} for s in slugs or [] if s not in known]
        sources[family] = entries
    return {'settings': settings, 'notifications': notifications, 'sources': sources, 'matching': legacy}


def build_sources(cfg):
    built, errors = [], []
    for family, spec in (cfg.get('sources') or {}).items():
        if family in ATS_SOURCES:
            cls = ATS_SOURCES[family]
            for entry in spec or []:
                entry = entry if isinstance(entry, dict) else {SLUG_FIELD[family]: entry}
                slug = entry.get(SLUG_FIELD[family])
                if not slug:
                    errors.append(f'{family}: entry without "{SLUG_FIELD[family]}"')
                    continue
                built.append(cls(f"{family}:{entry.get('id') or slug}", entry))  # optional id: several sites per tenant
        elif family in BOARD_SOURCES:
            options = spec if isinstance(spec, dict) else {'enabled': bool(spec)}
            built.append(BOARD_SOURCES[family](family, options))
        else:
            errors.append(f'unknown source family: {family}')
    return built, errors
