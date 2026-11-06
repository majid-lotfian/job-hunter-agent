"""Shared, polite HTTP client for discovery sources.

- one connection-pooled httpx.AsyncClient for all sources (bounded connections)
- per-host minimum interval between requests (rate limiting)
- retries with exponential backoff + jitter on timeouts, network errors, 429 and 5xx
- honours Retry-After (capped), raises SourceRateLimited when still throttled
- streams responses and aborts above a size cap (protects the 1 GB VM)
"""
import asyncio
import json
import random
import time
from urllib.parse import urlsplit

import httpx

from .models import InvalidResponse, SourceError, SourceRateLimited

USER_AGENT = 'JobHunterAgent/0.2 (+personal job search; low volume; github.com/majid-lotfian/job-hunter-agent)'
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 60.0


def _retry_after(response):
    value = (response.headers.get('retry-after') or '').strip()
    try:
        return min(MAX_RETRY_AFTER, max(0.0, float(value)))
    except ValueError:
        return None


class HttpClient:
    def __init__(self, *, transport=None, timeout=20.0, retries=2, backoff=1.0, default_interval=1.0,
                 host_intervals=None, max_bytes=15_000_000, max_connections=4, sleep=None, clock=None):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0), transport=transport, follow_redirects=True,
            headers={'User-Agent': USER_AGENT, 'Accept': 'application/json, application/xml;q=0.9, */*;q=0.5'},
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections))
        self.retries = retries
        self.backoff = backoff
        self.default_interval = default_interval
        self.host_intervals = dict(host_intervals or {})
        self.max_bytes = max_bytes
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or time.monotonic
        self._host_locks = {}
        self._host_last = {}
        self.requests = 0

    async def aclose(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    def _interval(self, host):
        for suffix, value in self.host_intervals.items():
            if host == suffix or host.endswith('.' + suffix):
                return value
        return self.default_interval

    async def _pace(self, host):
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._host_last.get(host, -1e9) + self._interval(host) - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._host_last[host] = self._clock()

    async def _fetch(self, method, url, max_bytes, **kwargs):
        async with self._client.stream(method, url, **kwargs) as response:
            declared = response.headers.get('content-length')
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise InvalidResponse(f'response too large ({int(declared) // 1_000_000} MB > cap)')
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise InvalidResponse(f'response too large (> {max_bytes // 1_000_000} MB cap)')
                chunks.append(chunk)
            response._content = b''.join(chunks)
            return response

    async def request(self, method, url, *, expect='json', max_bytes=None, **kwargs):
        """Return parsed JSON (expect='json'), text (expect='text') or the response ('response')."""
        host = urlsplit(url).hostname or ''
        last_error = None
        for attempt in range(self.retries + 1):
            await self._pace(host)
            self.requests += 1
            try:
                response = await self._fetch(method, url, max_bytes or self.max_bytes, **kwargs)
            except InvalidResponse:
                raise
            except httpx.TimeoutException as exc:
                last_error, retry_after = SourceError(f'timeout ({type(exc).__name__}) for {host}'), None
            except httpx.TransportError as exc:
                last_error, retry_after = SourceError(f'network error ({type(exc).__name__}) for {host}'), None
            else:
                if response.status_code in RETRY_STATUS:
                    retry_after = _retry_after(response)
                    last_error = (SourceRateLimited(f'HTTP 429 from {host}', retry_after) if response.status_code == 429
                                  else SourceError(f'HTTP {response.status_code} from {host}'))
                elif response.status_code >= 400:
                    raise SourceError(f'HTTP {response.status_code} from {host}')
                else:
                    return self._decode(response, expect, host)
            if attempt < self.retries:
                delay = self.backoff * (2 ** attempt) + random.uniform(0, self.backoff)
                await self._sleep(max(delay, retry_after or 0))
        raise last_error

    @staticmethod
    def _decode(response, expect, host):
        if expect == 'response':
            return response
        if expect == 'text':
            return response.text
        try:
            return json.loads(response.content)
        except ValueError:
            raise InvalidResponse(f'invalid JSON from {host}') from None

    async def get_json(self, url, **kwargs):
        return await self.request('GET', url, expect='json', **kwargs)

    async def get_text(self, url, **kwargs):
        return await self.request('GET', url, expect='text', **kwargs)

    async def post_json(self, url, payload, **kwargs):
        return await self.request('POST', url, expect='json', json=payload, **kwargs)
