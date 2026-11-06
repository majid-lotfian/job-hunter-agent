"""Autonomous submission via Recruitee's public Careers Site API - mocked endpoints only, never a live request.

Every test counts the POSTs that reach the (mock) employer, to prove when a submission does and does not happen.
"""
import asyncio
import json
import os
import unittest
from unittest import mock

import httpx

from tests.test_supervised import FULL_PROFILE, SupervisedBase  # noqa: E402  (sets up the test DB first)
from app import applications, core, workflow  # noqa: E402
from app.applications import adapters, feasibility  # noqa: E402
from app.discovery.http import HttpClient  # noqa: E402

LIVE = {'APPLY_MODE': 'live', 'APPLY_LIVE_ADAPTERS': 'recruitee'}
OFFER = {'offer': {'options_cv': 'required', 'options_phone': 'required', 'options_cover_letter': 'optional',
                   'options_photo': 'off', 'careers_apply_url': 'https://acme.recruitee.com/o/ai-engineer/c/new',
                   'locations_question_required': True, 'location_question_visible': False,
                   'description': '<p>We need Python, RAG and LLM agents.</p>', 'requirements': '',
                   'open_questions': [
                       {'id': 11, 'required': True, 'kind': 'boolean', 'body': 'Do you hold a valid work permit?'},
                       {'id': 12, 'required': False, 'kind': 'text', 'body': 'Anything else?'},
                       {'id': 13, 'required': False, 'kind': 'infobox', 'body': 'Mobile? Email us later.'}]}}


def page(captcha='disabled', agreements='[]'):
    body = '{&quot;agreements&quot;:' + agreements.replace('"', '&quot;') + ',&quot;featureFlags&quot;:{&quot;hcaptcha&quot;:true}'
    if captcha is not None:
        body += f',&quot;captcha&quot;:&quot;{captcha}&quot;'
    return f'<html><div data-props="{body}}}"></div></html>'


class Employer:
    """Mock Recruitee tenant. post_replies: list of httpx.Response or exceptions, consumed per POST."""
    def __init__(self, offer=OFFER, form_page=None, post_replies=None):
        self.offer, self.page = offer, form_page or page()
        self.post_replies = list(post_replies or [httpx.Response(201, json={'candidate': {'id': 777, 'name': 'x'}})])
        self.posts = []

    def handler(self, request):
        url = str(request.url)
        if request.method == 'POST':
            assert url == 'https://acme.recruitee.com/api/offers/ai-engineer/candidates', url
            self.posts.append(request.content)
            reply = self.post_replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        if url.endswith('/api/offers/ai-engineer'):
            return httpx.Response(200, json=self.offer) if self.offer else httpx.Response(404)
        if url.endswith('/c/new'):
            return httpx.Response(200, text=self.page)
        return httpx.Response(404)

    def http(self):
        return HttpClient(transport=httpx.MockTransport(self.handler), default_interval=0, retries=0)


class AutonomousTests(SupervisedBase):
    def approve(self, job=None):
        job = job or self.recruitee_job()
        self.make_cv(job)
        result = workflow.decide(job, workflow.APPROVE_APPLY, channel='telegram', token=self.state(job)['token'])
        self.assertEqual(result['result'], 'approved')
        return job

    def run_apps(self, employer, env=LIVE):
        with mock.patch.dict(os.environ, env):
            return self.process_applications(employer.http())

    def test_eligible_form_is_submitted_once_with_confirmation(self):
        job, emp = self.approve(), Employer()
        self.assertEqual(self.run_apps(emp), ['submitted'])
        self.assertEqual(len(emp.posts), 1)
        body = emp.posts[0]
        for part in (b'name="candidate[name]"', b'Test Candidate', b'name="candidate[email]"', b'candidate@example.org',
                     b'name="candidate[phone]"', b'name="candidate[open_question_answers_attributes][0][open_question_id]"',
                     b'11', b'name="candidate[open_question_answers_attributes][0][flag]"', b'true',
                     b'name="candidate[cv]"; filename="Test-Candidate-CV.pdf"', b'%PDF-'):
            self.assertIn(part, body)
        self.assertNotIn(b'cover_letter', body)                                 # never generated, never sent
        self.assertNotIn(b'[12]', body)                                         # optional unanswered question omitted
        app = self.apps(job)[0]
        self.assertEqual((app['status'], app['external_id']), ('submitted', 'recruitee:acme:candidate:777'))
        self.assertEqual(self.job(job)['status'], 'applied')
        self.assertIn('application_submitted', self.events(job))
        run_updates = asyncio.run(applications.send_application_updates(sender=self.send))
        self.assertEqual(run_updates, 1)
        self.assertIn('Application submitted', self.sent[-1])
        self.assertIn('recruitee:acme:candidate:777', self.sent[-1])
        self.assertEqual(self.run_apps(emp), [])                                # nothing left to process
        self.assertEqual(len(emp.posts), 1)
        self.assertEqual(self.state(job)['actions'], [])                        # cannot be approved twice

    def test_no_post_when_form_is_not_permitted(self):
        cases = {'captcha on': Employer(form_page=page('invisible')),
                 'captcha unknown': Employer(form_page=page(None)),
                 'consent box': Employer(form_page=page('disabled', '[{"id":1,"position":1}]')),
                 'unanswered question': Employer(offer={'offer': {**OFFER['offer'], 'open_questions': [
                     {'id': 21, 'required': True, 'kind': 'text', 'body': 'Why us?'}]}}),
                 'visible location choice': Employer(offer={'offer': {**OFFER['offer'], 'location_question_visible': True,
                                                                      'locations': [{'city': 'Amsterdam'}, {'city': 'Utrecht'}]}})}
        for name, emp in cases.items():
            job = self.approve()
            self.assertEqual(self.run_apps(emp), ['manual_action_required'], name)
            self.assertEqual(emp.posts, [], name)
            pkg = json.loads(self.apps(job)[0]['detail'])['package']
            self.assertFalse(pkg['autonomous']['possible'], name)
            self.assertTrue(pkg['autonomous']['blockers'], name)
            self.assertNotEqual(self.job(job)['status'], 'applied')

    def test_apply_mode_off_or_unlisted_adapter_never_posts(self):
        job, emp = self.approve(), Employer()
        self.assertEqual(self.run_apps(emp, env={'APPLY_MODE': 'off', 'APPLY_LIVE_ADAPTERS': 'recruitee'}), ['manual_action_required'])
        self.assertTrue(json.loads(self.apps(job)[0]['detail'])['package']['autonomous']['possible'])   # eligible, but off
        job2 = self.approve()
        self.assertEqual(self.run_apps(emp, env={'APPLY_MODE': 'live', 'APPLY_LIVE_ADAPTERS': ''}), ['dry_run'])
        self.assertEqual(emp.posts, [])

    def test_validation_error_is_blocked_without_marking_applied(self):
        job = self.approve()
        emp = Employer(post_replies=[httpx.Response(422, json={'error': ['Phone is invalid'], 'error_fields': {'phone': ['invalid']}})])
        self.assertEqual(self.run_apps(emp), ['blocked'])
        app = self.apps(job)[0]
        self.assertIn('rejected the application', app['last_error'])
        self.assertEqual(json.loads(app['detail'])['missing'], ['phone'])
        self.assertNotEqual(self.job(job)['status'], 'applied')

    def test_rate_limit_retries_later_but_unknown_outcomes_never_resend(self):
        job = self.approve()
        emp = Employer(post_replies=[httpx.Response(429), httpx.Response(201, json={'candidate': {'id': 5}})])
        self.assertEqual(self.run_apps(emp), ['queued'])                        # 429: not created -> retry later
        with core.connect() as db:
            db.execute('UPDATE applications SET next_attempt_at=NULL')
        self.assertEqual(self.run_apps(emp), ['submitted'])
        self.assertEqual(len(emp.posts), 2)

    def test_unknown_outcomes_are_never_resent(self):
        for reply in (httpx.Response(500), httpx.Response(201, json={'candidate': {}}), httpx.ReadTimeout('slow')):
            job = self.approve()
            emp = Employer(post_replies=[reply])
            self.assertEqual(self.run_apps(emp), ['manual_action_required'])
            self.assertEqual(len(emp.posts), 1)
            self.assertIsNone(applications.claim_next())                        # never retried automatically
            self.assertNotEqual(self.job(job)['status'], 'applied')

    def test_closed_or_token_protected_site(self):
        job = self.approve()
        self.assertEqual(self.run_apps(Employer(post_replies=[httpx.Response(403)])), ['blocked'])
        self.assertIn('requires a token', self.apps(job)[0]['last_error'])

    def test_live_approval_message_names_cv_version_and_method(self):
        job = self.recruitee_job()
        self.make_cv(job)
        with mock.patch.dict(os.environ, LIVE):
            state = workflow.job_state(job)
            text = workflow.action_message(self.job(job) | {'location_label': None}, state, core.get_cv_job(job))
            labels = workflow.labels()
        self.assertIn(f"CV version {state['cv_version']}", text)
        self.assertIn('cv.pdf', text)
        self.assertIn('submitted automatically ONLY if', text)
        self.assertEqual(labels[workflow.APPROVE_APPLY], 'Approve & Apply')

    def test_feasibility_ranks_eligible_recruitee_first(self):
        rec = self.recruitee_job(score=80)
        gh = self.add_job(score=95)
        with core.connect() as db:
            rows = {r['id']: r for r in db.execute('SELECT * FROM jobs WHERE id IN (?,?)', (rec, gh))}
        from app.applications import profile as profile_mod
        prof = profile_mod.load_profile()
        emp = Employer()
        r1 = asyncio.run(feasibility.assess(rows[rec], emp.http(), prof, 'python rag'))
        r2 = asyncio.run(feasibility.assess(rows[gh], emp.http(), prof, 'python rag'))
        self.assertEqual((r1['autonomous'], r2['autonomous']), (True, False))
        self.assertIn('eligible', r1['reason'])
        self.assertIn('employer', r2['reason'])
        self.assertEqual(emp.posts, [])


class FormSettingsTests(unittest.TestCase):
    def test_gap_terms_are_whole_words(self):
        self.assertEqual(feasibility.gaps('We trust you to interact with Rust and React; fine-tuning LLMs', 'python llm'),
                         ['fine-tun', 'rust', 'react'])

    def test_captcha_mode_not_feature_flag(self):
        s = adapters.careers_form_settings
        self.assertEqual(s('"featureFlags":{"hcaptcha":true},"captcha":"disabled","agreements":[]'), (False, False))
        self.assertEqual(s('"captcha":{"apiHost":"x"},"captcha":"invisible","agreements":[{"id":1}]'), (True, True))
        self.assertEqual(s('"featureFlags":{"hcaptcha":true}'), (None, None))

    def test_send_once_never_retries(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503)
        http = HttpClient(transport=httpx.MockTransport(handler), default_interval=0, retries=3)
        response = asyncio.run(http.send_once('POST', 'https://acme.recruitee.com/x', data={'a': 'b'}))
        self.assertEqual((response.status_code, len(calls)), (503, 1))


if __name__ == '__main__':
    unittest.main()
