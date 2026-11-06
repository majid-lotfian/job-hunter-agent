"""Normalized job records and source outcome types."""
from dataclasses import dataclass, field
from typing import Any, Optional

# Source families whose listings are employer-owned (direct) vs. third-party aggregators.
DIRECT, AGGREGATOR = 'direct', 'aggregator'


@dataclass
class JobRecord:
    """One vacancy as returned by one source. Unknown values stay None - never guessed."""
    source_key: str                 # e.g. 'greenhouse:adyen', 'arbeitnow'
    source_job_id: str              # id unique within source_key
    title: str
    url: str                        # original posting URL on the source
    employer: Optional[str] = None
    location: Optional[str] = None  # raw location text as given by the source
    locations: list = field(default_factory=list)  # all raw location strings
    country: Optional[str] = None   # ISO-2 when the source states it
    remote_hint: Optional[str] = None   # source-provided remote/hybrid/onsite flag
    employment_type: Optional[str] = None
    seniority_hint: Optional[str] = None
    description: str = ''           # plain text
    apply_url: Optional[str] = None
    published_at: Optional[str] = None  # ISO 8601 when the source provides a date
    updated_marker: Optional[str] = None  # changes when the posting changes (skip unchanged)
    kind: str = DIRECT
    metadata: dict = field(default_factory=dict)  # small, JSON-serializable source extras
    needs_detail: bool = False      # adapter must fetch the full posting before storing
    detail_ref: Any = None

    def all_location_text(self):
        parts = [self.location or ''] + [x for x in self.locations if x]
        return ' | '.join(dict.fromkeys(p.strip() for p in parts if p and p.strip()))


class SourceError(Exception):
    """Base class for source failures (recorded in source health)."""
    status = 'failed'


class SourceRateLimited(SourceError):
    status = 'rate_limited'

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class SourceNotConfigured(SourceError):
    status = 'not_configured'


class InvalidResponse(SourceError):
    status = 'failed'


# Outcome statuses written to source_health.status
OK, EMPTY, FAILED, RATE_LIMITED, SKIPPED, NOT_CONFIGURED, DISABLED, PARTIAL = (
    'ok', 'empty', 'failed', 'rate_limited', 'skipped', 'not_configured', 'disabled', 'partial')
