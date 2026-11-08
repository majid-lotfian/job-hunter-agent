"""CAPTCHA-blocked browser attempts are handed to the manual (phone) workflow, with the blocked outcome and its
evidence kept in the audit history; other blocks (login, form problems) stay blocked; nothing is ever retried."""
import json
import os
import unittest
from unittest import mock

from tests.test_browser_worker import ANSWERED, FakeBackend  # noqa: E402  (sets up the test DB)
from tests.test_manual import FakeTelegram, ManualBase  # noqa: E402
from tests.test_supervised import employer  # noqa: E402
from app import applications, core, workflow  # noqa: E402
from app.applications import browser, manual  # noqa: E402

CAPTCHA_AFTER_CLICK = {'status': 'blocked', 'reason': 'human verification challenge (hcaptcha checkbox) after submit - not completed',
                       'submitted_clicked': True, 'filled': ['CV (PDF)'], 'missing': [], 'evidence_dir': '/evidence/app-x',
                       'evidence': {'screenshot_sha256': 'e9' * 32, 'url': 'https://acme.recruitee.com/o/ai-engineer/c/new'}}
TURNSTILE_ON_LOAD = {'status': 'blocked', 'reason': 'human verification challenge (turnstile widget)', 'submitted_clicked': False,
                     'evidence_dir': '/evidence/app-y'}
LOGIN = {'status': 'blocked', 'reason': 'login required', 'submitted_clicked': False}


class BlockedToManualTests(ManualBase):
    def setUp(self):
        super().setUp()
        self.profile_path.write_text(ANSWERED)

    def live_attempt(self, worker_result):
        job = self.add_job(score=90, source='greenhouse:acme')
        with core.connect() as db:
            db.execute("UPDATE jobs SET source_id=? WHERE id=?", (str(4000 + job), job))
        self.make_cv(job)
        workflow.decide(job, workflow.APPROVE_APPLY, channel='telegram', token=self.state(job)['token'])
        env = {'APPLY_MODE': 'live', 'APPLY_LIVE_ADAPTERS': 'browser', 'BROWSER_WORKER': 'fake', 'APPLY_LIVE_JOB_IDS': str(job)}
        backend = FakeBackend(worker_result)
        with mock.patch.dict(browser.BACKENDS, {'fake': backend}), mock.patch.dict(os.environ, env):
            outcome = self.process_applications(employer())
        return job, outcome, backend

    def test_captcha_after_click_goes_to_manual_with_evidence_kept(self):
        job, outcome, backend = self.live_attempt(CAPTCHA_AFTER_CLICK)
        self.assertEqual(outcome, ['manual_action_required'])
        self.assertEqual(len(backend.calls), 1)                              # one attempt, never retried
        row = self.apps(job)[0]
        detail = json.loads(row['detail'])
        self.assertEqual(detail['blocked']['status'], 'blocked')
        self.assertIn('hcaptcha', detail['blocked']['reason'])
        self.assertEqual(detail['blocked']['evidence_dir'], '/evidence/app-x')
        self.assertEqual(detail['blocked']['screenshot_sha256'], 'e9' * 32)
        self.assertTrue(detail['blocked']['submit_clicked'])
        self.assertIn('after the Submit click', row['last_error'])
        self.assertIsNone(row['external_id'])
        events = self.events(job)
        self.assertLess(events.index('application_blocked'), events.index('application_manual_action_required'))
        self.assertNotEqual(self.job(job)['status'], 'applied')
        self.assertIsNone(applications.claim_next())
        tg = FakeTelegram()                                                 # the phone workflow takes over
        self.assertEqual(self.updates(tg), 1)
        self.assertEqual(len(tg.docs), 1)
        self.assertTrue(tg.docs[0]['bytes'].startswith(b'%PDF'))
        self.assertIn('CAPTCHA', tg.msgs[0]['text'])
        self.assertEqual([b['text'] for row in tg.msgs[0]['markup']['inline_keyboard'] for b in row],
                         ['🌐 Open Application', '✅ I Submitted This Application', '⏳ Submit Later'])
        self.assertEqual(tg.msgs[0]['markup']['inline_keyboard'][0][0]['url'], f'https://job-boards.greenhouse.io/acme/jobs/123')

    def test_challenge_before_submit_also_goes_to_manual(self):
        job, outcome, _ = self.live_attempt(TURNSTILE_ON_LOAD)
        self.assertEqual(outcome, ['manual_action_required'])
        self.assertIn('before submitting', self.apps(job)[0]['last_error'])
        self.assertFalse(json.loads(self.apps(job)[0]['detail'])['blocked']['submit_clicked'])

    def test_login_wall_stays_blocked(self):
        job, outcome, _ = self.live_attempt(LOGIN)
        self.assertEqual(outcome, ['blocked'])
        self.assertNotIn('blocked', json.loads(self.apps(job)[0]['detail']))
        tg = FakeTelegram()
        self.updates(tg)
        self.assertEqual(tg.docs, [])                                       # no manual PDF workflow for a login wall

    def test_existing_blocked_application_can_be_routed_once(self):
        job, outcome, _ = self.live_attempt(LOGIN)
        app = self.apps(job)[0]
        with self.assertRaises(ValueError):                                 # not a CAPTCHA block
            manual.route_blocked_to_manual(app['id'], actor='test')
        with core.connect() as db:                                          # simulate an old CAPTCHA-blocked record
            db.execute("UPDATE applications SET last_error=? WHERE id=?",
                       ('cloud browser stopped: human verification challenge (hcaptcha checkbox) after submit - not completed', app['id']))
        self.assertTrue(manual.route_blocked_to_manual(app['id'], actor='test'))
        row = self.apps(job)[0]
        self.assertEqual(row['status'], 'manual_action_required')
        self.assertIsNone(row['notified_at'])
        detail = json.loads(row['detail'])
        self.assertIn('hcaptcha', detail['blocked']['reason'])
        self.assertIn('routed_to_manual', self.events(job))
        with self.assertRaises(ValueError):                                 # second routing refused
            manual.route_blocked_to_manual(app['id'], actor='test')
        self.assertIsNone(applications.claim_next())

    def test_is_human_verification(self):
        self.assertTrue(browser.is_human_verification('human verification challenge (turnstile widget)'))
        self.assertTrue(browser.is_human_verification('the form uses a CAPTCHA'))
        self.assertFalse(browser.is_human_verification('login required'))
        self.assertFalse(browser.is_human_verification('required fields could not be filled'))


if __name__ == '__main__':
    unittest.main()
