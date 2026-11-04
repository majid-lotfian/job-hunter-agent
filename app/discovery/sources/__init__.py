"""Source adapters. Each adapter yields normalized JobRecord objects."""
import os
from dataclasses import dataclass

from ..models import DIRECT


@dataclass
class FetchContext:
    """Per-run state an adapter reports back to the engine."""
    truncated: bool = False     # page/item limits were hit: the listing is not complete
    pages: int = 0
    detail_errors: int = 0


class Source:
    family = 'base'
    kind = DIRECT
    complete_listing = True        # the adapter returns ALL open jobs (absence = evidence of closure)
    default_interval_minutes = 60
    attribution = None             # e.g. 'Remotive' - shown with the job link
    required_env = ()

    def __init__(self, key, options=None):
        self.key = key
        self.options = dict(options or {})
        self.enabled = bool(self.options.get('enabled', True))
        self.interval_minutes = int(self.options.get('interval_minutes', self.default_interval_minutes))
        self.max_pages = int(self.options.get('max_pages', 20))
        self.label = self.options.get('name') or key

    def missing_config(self):
        missing = [name for name in self.required_env if not os.getenv(name, '').strip()]
        return f'set {", ".join(missing)} in .env' if missing else None

    async def fetch(self, http, ctx):  # pragma: no cover - interface
        raise NotImplementedError
        yield

    async def detail(self, http, record):
        return record
