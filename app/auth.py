"""Cookie-based dashboard authentication.

DASHBOARD_TOKEN is the login secret. It is only ever submitted in a POST body to
/login and is never placed in URLs, cookies or logs. A successful login sets a
signed, expiring session cookie (HttpOnly, SameSite=Lax, Secure by default).
The signing key is derived from DASHBOARD_TOKEN, so rotating the token
invalidates every existing session.
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

COOKIE_NAME = 'jh_session'
SESSION_SECONDS = int(os.getenv('SESSION_HOURS', '12')) * 3600
PLACEHOLDER = 'replace-with-a-long-random-secret'
MAX_FAILURES = 5
FAILURE_WINDOW = 300  # seconds
_failures = defaultdict(deque)


def dashboard_token():
    token = os.getenv('DASHBOARD_TOKEN', '')
    if not token or token == PLACEHOLDER:
        raise HTTPException(503, 'Set a secure DASHBOARD_TOKEN in .env')
    return token


def cookie_secure():
    # Browsers treat http://localhost and http://127.0.0.1 as secure contexts, so
    # Secure cookies work through the SSH tunnel. Set COOKIE_SECURE=false only for
    # a browser that refuses Secure cookies on plain-HTTP localhost.
    return os.getenv('COOKIE_SECURE', 'true').strip().lower() not in ('0', 'false', 'no')


def _signing_key(token):
    return hmac.new(token.encode(), b'job-hunter-session-v1', hashlib.sha256).digest()


def _sign(key, payload):
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def new_session(now=None):
    expires = int((now or time.time()) + SESSION_SECONDS)
    payload = f'{expires}.{secrets.token_urlsafe(16)}'
    return f'{payload}.{_sign(_signing_key(dashboard_token()), payload)}'


def valid_session(value, now=None):
    if not value or value.count('.') != 2:
        return False
    expires, nonce, signature = value.split('.')
    payload = f'{expires}.{nonce}'
    try:
        expected = _sign(_signing_key(dashboard_token()), payload)
    except HTTPException:
        return False
    if not hmac.compare_digest(signature, expected):
        return False
    return expires.isdigit() and int(expires) > (now or time.time())


def check_token(candidate):
    return bool(candidate) and hmac.compare_digest(candidate.encode(), dashboard_token().encode())


def is_authenticated(request: Request):
    return valid_session(request.cookies.get(COOKIE_NAME))


def require_api_session(request: Request):
    """Dependency for JSON API endpoints: 401 without a valid session."""
    if not is_authenticated(request):
        raise HTTPException(401, 'Not logged in')


def same_origin(request: Request):
    """Reject cross-site state-changing requests (defence in depth on top of SameSite=Lax)."""
    origin = request.headers.get('origin')
    if origin is None:
        return True
    host = request.headers.get('host', '')
    return re.sub(r'^https?://', '', origin).rstrip('/') == host


def safe_next(value):
    """Only allow local, absolute-path redirects (prevents open redirects)."""
    if not value or not value.startswith('/') or value.startswith('//') or '\\' in value:
        return '/'
    return value


def client_key(request: Request):
    return request.client.host if request.client else 'unknown'


def throttled(key, now=None):
    now = now or time.time()
    attempts = _failures[key]
    while attempts and attempts[0] < now - FAILURE_WINDOW:
        attempts.popleft()
    return len(attempts) >= MAX_FAILURES


def record_failure(key, now=None):
    _failures[key].append(now or time.time())


def reset_failures(key):
    _failures.pop(key, None)


class RedactSecretsFilter(logging.Filter):
    """Redact token-like query parameters from uvicorn access log lines."""
    PATTERN = re.compile(r'(?i)([?&](?:token|access_token|session|password|' + COOKIE_NAME + r')=)[^&\s"]*')

    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(self.PATTERN.sub(r'\1[REDACTED]', a) if isinstance(a, str) else a
                                for a in record.args)
        if isinstance(record.msg, str):
            record.msg = self.PATTERN.sub(r'\1[REDACTED]', record.msg)
        return True


def install_log_redaction():
    for name in ('uvicorn.access', 'uvicorn.error', 'uvicorn'):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactSecretsFilter) for f in logger.filters):
            logger.addFilter(RedactSecretsFilter())
