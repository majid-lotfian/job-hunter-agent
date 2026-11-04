"""Dashboard authentication tests. Uses a temporary DB; never touches data/."""
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_TMP = tempfile.TemporaryDirectory()
os.environ['DB_PATH'] = str(Path(_TMP.name) / 'auth-test.sqlite3')
os.environ['DASHBOARD_TOKEN'] = 'test-token-for-unit-tests'
os.environ['TELEGRAM_BOT_TOKEN'] = ''

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, core, main  # noqa: E402

TOKEN = os.environ['DASHBOARD_TOKEN']


def client():
    # https base URL: a compliant client only sends Secure cookies over HTTPS
    # (browsers additionally allow them on http://localhost, see README).
    return TestClient(main.app, base_url='https://testserver', follow_redirects=False)


def login(c, token=TOKEN, next_path='/'):
    return c.post('/login', data={'token': token, 'next': next_path})


class AuthTests(unittest.TestCase):
    def setUp(self):
        auth._failures.clear()
        core.init_db()
        with core.connect() as db:
            self.job_id = db.execute(
                "INSERT OR IGNORE INTO jobs (source,source_id,company,title,url,discovered_at,score)"
                " VALUES ('lever','auth','acme','AI Engineer','https://x','now',70)").lastrowid or \
                db.execute("SELECT id FROM jobs WHERE source_id='auth'").fetchone()[0]
        self.cv = Path(_TMP.name) / 'auth-cv.html'
        self.cv.write_text('<html><body>TAILORED</body></html>', encoding='utf-8')
        p = mock.patch.object(main, 'cv_path', lambda i: self.cv)
        p.start()
        self.addCleanup(p.stop)

    def test_unauthenticated_pages_redirect_to_login(self):
        c = client()
        r = c.get('/')
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers['location'], '/login?next=/')
        r = c.get(f'/api/jobs/{self.job_id}/cv')
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers['location'], f'/login?next=/api/jobs/{self.job_id}/cv')
        login_page = c.get('/login')
        self.assertEqual(login_page.status_code, 200)
        self.assertIn('type="password"', login_page.text)

    def test_unauthenticated_api_is_401(self):
        c = client()
        self.assertEqual(c.get('/api/jobs').status_code, 401)
        self.assertEqual(c.post('/api/scan').status_code, 401)
        self.assertEqual(c.post(f'/api/jobs/{self.job_id}/status', json={'status': 'rejected'}).status_code, 401)

    def test_token_in_url_or_header_is_not_accepted(self):
        c = client()
        r = c.get(f'/api/jobs/{self.job_id}/cv', params={'token': TOKEN})
        self.assertEqual(r.status_code, 303)
        self.assertNotIn(TOKEN, r.headers['location'])
        self.assertEqual(c.get('/api/jobs', headers={'X-Dashboard-Token': TOKEN}).status_code, 401)
        self.assertEqual(c.get('/', params={'token': TOKEN}).status_code, 303)

    def test_wrong_token_rejected_without_cookie(self):
        c = client()
        r = login(c, 'wrong')
        self.assertEqual(r.status_code, 401)
        self.assertNotIn('set-cookie', r.headers)
        self.assertNotIn('wrong', r.text)

    def test_login_sets_secure_httponly_lax_cookie_without_token(self):
        c = client()
        r = login(c, next_path=f'/api/jobs/{self.job_id}/cv')
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers['location'], f'/api/jobs/{self.job_id}/cv')
        cookie = r.headers['set-cookie']
        lowered = cookie.lower()
        for attr in ('httponly', 'secure', 'samesite=lax', 'path=/', 'max-age='):
            self.assertIn(attr, lowered)
        self.assertTrue(cookie.startswith(auth.COOKIE_NAME + '='))
        self.assertNotIn(TOKEN, cookie)
        self.assertNotIn(TOKEN, r.headers['location'])

    def test_session_grants_access_to_dashboard_api_and_cv(self):
        c = client()
        login(c)
        r = c.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('?token=', r.text)
        self.assertEqual(r.headers['cache-control'], 'no-store')
        self.assertEqual(c.get('/api/jobs').status_code, 200)
        r = c.get(f'/api/jobs/{self.job_id}/cv')
        self.assertEqual(r.status_code, 200)
        self.assertIn('TAILORED', r.text)
        self.assertTrue(r.headers['content-disposition'].startswith('inline'))
        self.assertEqual(c.get('/api/jobs/999999/cv').status_code, 404)

    def test_missing_cv_shows_instructions_not_generation(self):
        c = client()
        login(c)
        self.cv.unlink()
        r = c.get(f'/api/jobs/{self.job_id}/cv')
        self.assertEqual(r.status_code, 404)
        self.assertIn('app.tailor_cv', r.text)
        self.assertFalse(self.cv.exists())

    def test_tampered_expired_and_rotated_sessions_rejected(self):
        value = auth.new_session()
        self.assertTrue(auth.valid_session(value))
        self.assertFalse(auth.valid_session(value[:-1] + ('0' if value[-1] != '0' else '1')))
        self.assertFalse(auth.valid_session('garbage'))
        self.assertFalse(auth.valid_session(''))
        self.assertFalse(auth.valid_session(value, now=time.time() + auth.SESSION_SECONDS + 5))
        expires, nonce, sig = value.split('.')
        self.assertFalse(auth.valid_session(f'{int(expires) + 99999}.{nonce}.{sig}'))
        with mock.patch.dict(os.environ, {'DASHBOARD_TOKEN': 'rotated-new-secret-value'}):
            self.assertFalse(auth.valid_session(value))
        c = client()
        c.cookies.set(auth.COOKIE_NAME, 'forged.value.sig')
        self.assertEqual(c.get('/api/jobs').status_code, 401)

    def test_logout_clears_cookie(self):
        c = client()
        login(c)
        r = c.post('/logout')
        self.assertEqual(r.status_code, 303)
        self.assertIn(auth.COOKIE_NAME + '=""', r.headers['set-cookie'])
        self.assertEqual(c.get('/').status_code, 303)

    def test_open_redirects_blocked(self):
        for bad in ('//evil.example', 'https://evil.example', '/\\evil.example', 'evil'):
            r = login(client(), next_path=bad)
            self.assertEqual(r.headers['location'], '/', bad)

    def test_cross_site_post_rejected(self):
        c = client()
        login(c)
        r = c.post('/api/scan', headers={'Origin': 'https://evil.example'})
        self.assertEqual(r.status_code, 403)
        r = c.post('/login', data={'token': TOKEN}, headers={'Origin': 'https://evil.example'})
        self.assertEqual(r.status_code, 403)

    def test_failed_logins_are_throttled(self):
        c = client()
        for _ in range(auth.MAX_FAILURES):
            self.assertEqual(login(c, 'wrong').status_code, 401)
        r = login(c)  # even the right token is refused while throttled
        self.assertEqual(r.status_code, 429)
        self.assertNotIn('set-cookie', r.headers)

    def test_unconfigured_token_fails_closed(self):
        with mock.patch.dict(os.environ, {'DASHBOARD_TOKEN': auth.PLACEHOLDER}):
            c = client()
            self.assertEqual(c.get('/login').status_code, 503)
            self.assertEqual(login(c, auth.PLACEHOLDER).status_code, 503)
            self.assertEqual(c.get('/api/jobs').status_code, 401)

    def test_access_log_redacts_secrets(self):
        record = logging.LogRecord('uvicorn.access', logging.INFO, __file__, 1,
                                   '%s - "%s %s HTTP/%s" %d',
                                   ('127.0.0.1:1', 'GET', f'/api/jobs/1/cv?token={TOKEN}&x=1', '1.1', 200), None)
        auth.RedactSecretsFilter().filter(record)
        line = record.getMessage()
        self.assertNotIn(TOKEN, line)
        self.assertIn('token=[REDACTED]&x=1', line)
        self.assertTrue(any(isinstance(f, auth.RedactSecretsFilter)
                            for f in logging.getLogger('uvicorn.access').filters))

    def test_login_failure_log_has_no_secret(self):
        with self.assertLogs('jobhunter', level='WARNING') as logs:
            login(client(), 'super-secret-guess')
        self.assertFalse(any('super-secret-guess' in m for m in logs.output))


if __name__ == '__main__':
    unittest.main()
