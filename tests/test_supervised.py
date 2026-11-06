"""Supervised (browser-assisted) applications: profile, preparation package, review page, manual submission record.

No network: employer systems are an httpx.MockTransport, Telegram is a recorder. APPLY_MODE stays off.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from tests.test_workflow import PROFILE_YAML, Base, LiveMockAdapter, run  # noqa: E402  (sets up the test DB first)
from app import applications, auth, core, main, workflow  # noqa: E402
from app.applications import adapters, prepare, profile as profile_mod  # noqa: E402
from app.discovery.http import HttpClient  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FULL_PROFILE = """verified: true
full_name: Test Candidate
first_name: Test
last_name: Candidate
email: candidate@example.org
phone: "+31 6 1234 5678"
location: Utrecht, Netherlands
work_authorization: EU citizen
linkedin: https://www.linkedin.com/in/test
languages: {English: Fluent, Dutch: Intermediate}
answers:
  - match: "work permit|authori[sz]ed to work"
    answer: true
"""

OFFER = {'offer': {'options_cv': 'required', 'options_phone': 'required', 'options_cover_letter': 'optional',
                   'options_photo': 'off', 'careers_apply_url': 'https://acme.recruitee.com/o/ai-engineer/c/new',
                   'locations_question_required': True, 'location_question_visible': False,
                   'description': '<p>We need Python, RAG and LLM agents.</p>', 'requirements': '',
                   'open_questions': [
                       {'id': 1, 'required': True, 'kind': 'boolean', 'body': 'Do you hold a valid work permit?',
                        'open_question_options': [{'body': 'Yes'}, {'body': 'No'}]},
                       {'id': 2, 'required': True, 'kind': 'text', 'body': 'Why do you want to join us?'}]}}
# as served by Recruitee: the config JSON is HTML-escaped inside an attribute
CAPTCHA_PAGE = ('<html><div data-props="{&quot;appConfig&quot;:{&quot;captcha&quot;:{&quot;apiHost&quot;:&quot;x&quot;},'
                '&quot;featureFlags&quot;:{&quot;hcaptcha&quot;:true}},&quot;captcha&quot;:&quot;invisible&quot;}"></div></html>')
NO_CAPTCHA_PAGE = '<html><div data-props="{&quot;captcha&quot;:&quot;none&quot;}"></div></html>'

GH_JOB = {'absolute_url': 'https://job-boards.greenhouse.io/acme/jobs/123', 'content': '&lt;p&gt;Python and RAG&lt;/p&gt;',
          'questions': [
              {'label': 'First Name', 'required': True, 'fields': [{'name': 'first_name', 'type': 'input_text'}]},
              {'label': 'Last Name', 'required': True, 'fields': [{'name': 'last_name', 'type': 'input_text'}]},
              {'label': 'Email', 'required': True, 'fields': [{'name': 'email', 'type': 'input_text'}]},
              {'label': 'Resume/CV', 'required': True, 'fields': [{'name': 'resume', 'type': 'input_file'},
                                                                  {'name': 'resume_text', 'type': 'textarea'}]},
              {'label': 'Which languages do you speak?', 'required': True, 'fields': [
                  {'name': 'question_9', 'type': 'multi_value_multi_select',
                   'values': [{'label': 'English', 'value': 1}, {'label': 'Dutch', 'value': 2}, {'label': 'German', 'value': 3}]}]},
              {'label': 'Are you currently authorized to work in the country where this position is located?', 'required': True,
               'fields': [{'name': 'question_10', 'type': 'multi_value_single_select',
                           'values': [{'label': 'Yes', 'value': 1}, {'label': 'No', 'value': 0}]}]},
              {'label': 'Have you worked for Acme before?', 'required': True, 'fields': [
                  {'name': 'question_11', 'type': 'multi_value_single_select', 'values': [{'label': 'Yes'}, {'label': 'No'}]}]},
              {'label': 'Hidden', 'required': False, 'fields': [{'name': 'x', 'type': 'input_hidden'}]}]}


def employer(offer=OFFER, page=CAPTCHA_PAGE, gh=GH_JOB, calls=None):
    def handler(request):
        assert request.method == 'GET', 'preparation must never POST'
        if calls is not None:
            calls.append(str(request.url))
        url = str(request.url)
        if '/api/offers/' in url:
            return httpx.Response(404) if offer is None else httpx.Response(200, json=offer)
        if 'boards-api.greenhouse.io' in url:
            return httpx.Response(404) if gh is None else httpx.Response(200, json=gh)
        if '/c/new' in url:
            return httpx.Response(200, text=page)
        return httpx.Response(404)
    return HttpClient(transport=httpx.MockTransport(handler), default_interval=0, retries=0)


class SupervisedBase(Base):
    def setUp(self):
        super().setUp()
        self.profile_path.write_text(FULL_PROFILE)

    def recruitee_job(self, score=85):
        return self.add_job(score=score, source='recruitee:acme', url='https://acme.recruitee.com/o/ai-engineer')

    def prepared(self, job=None, http=None):
        job = job or self.recruitee_job()
        self.make_cv(job)
        result = workflow.decide(job, workflow.APPROVE_APPLY, channel='dashboard', token=self.state(job)['token'])
        self.assertEqual(result['result'], 'approved')
        self.assertEqual(self.process_applications(http or employer()), ['manual_action_required'])
        return job, self.apps(job)[0]

    def package(self, app):
        return json.loads(app['detail'])['package']


class ProfileTests(unittest.TestCase):
    def test_validation_reports_problems_without_values(self):
        p = profile_mod.Profile({'verified': False, 'email': 'not-an-email', 'phone': 'call me', 'linkedin': 'linkedin.com/x',
                                 'work_arrangements': ['office'], 'answers': [{'match': '(', 'answer': True}], 'shoe_size': 44})
        errors, warnings = p.validate()
        text = ' | '.join(errors)
        for expected in ('name missing', 'location missing', 'work_authorization missing', 'email is not a valid address',
                         'phone must be', 'linkedin must be a full https:// URL', 'work_arrangements may only contain',
                         'not a valid regular expression', 'not marked `verified: true`'):
            self.assertIn(expected, text)
        self.assertNotIn('not-an-email', text)                                  # no personal values echoed
        self.assertTrue(any('shoe_size' in w for w in warnings))

    def test_complete_profile_is_valid(self):
        import yaml
        errors, warnings = profile_mod.Profile(yaml.safe_load(FULL_PROFILE)).validate()
        self.assertEqual(errors, [])
        self.assertTrue(any('salary_expectation not set' in w for w in warnings))

    def test_draft_from_cv_uses_only_literal_cv_values(self):
        master = ('<html><body><header><h1>Ann Van Dijk</h1><div class="contact">Almere, Netherlands · Dutch citizen · '
                  '<a href="mailto:ann@example.org">ann@example.org</a> · +31 6 0000 0000 · '
                  '<a href="https://www.linkedin.com/in/ann">LinkedIn</a> · <a href="https://github.com/ann">GitHub</a> · '
                  '<a href="https://ann.example">Portfolio</a></div></header><h2>Languages</h2>'
                  '<p><b>English:</b> Fluent <b>Dutch:</b> Intermediate</p></body></html>')
        data, notes = profile_mod.draft_from_cv(master)
        self.assertIs(data['verified'], False)
        self.assertEqual((data['full_name'], data['first_name'], data['last_name']), ('Ann Van Dijk', 'Ann', 'Van Dijk'))
        self.assertEqual((data['email'], data['phone'], data['location'], data['nationality']),
                         ('ann@example.org', '+31 6 0000 0000', 'Almere, Netherlands', 'Dutch'))
        self.assertEqual((data['linkedin'], data['github'], data['portfolio']),
                         ('https://www.linkedin.com/in/ann', 'https://github.com/ann', 'https://ann.example'))
        self.assertEqual(data['languages'], {'English': 'Fluent', 'Dutch': 'Intermediate'})
        self.assertIsNone(data['salary_expectation'])                          # never invented
        self.assertEqual(data['preferred_locations'], [])
        self.assertTrue(any('derived' in n for n in notes))

    def test_init_never_overwrites(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'data').mkdir()
        (tmp / 'data' / 'master_cv.html').write_text('<html><body><h1>A B</h1></body></html>')
        with mock.patch.object(profile_mod, 'ROOT', tmp), mock.patch.object(profile_mod, 'PROFILE_PATH', tmp / 'data' / 'p.yaml'):
            self.assertEqual(profile_mod.main(['--init-from-cv']), 2)           # created, but not usable yet (unverified)
            self.assertEqual(oct((tmp / 'data' / 'p.yaml').stat().st_mode & 0o777), '0o600')
            (tmp / 'data' / 'p.yaml').write_text('verified: true\n')
            self.assertEqual(profile_mod.main(['--init-from-cv']), 1)
            self.assertEqual((tmp / 'data' / 'p.yaml').read_text(), 'verified: true\n')


class FieldMatchingTests(unittest.TestCase):
    def setUp(self):
        import yaml
        self.profile = profile_mod.Profile(yaml.safe_load(FULL_PROFILE))

    def match(self, field, cv=None):
        return prepare.match_field(field, self.profile, cv if cv is not None else {'pdf': Path('x.pdf')})

    def test_profile_and_cv_fields(self):
        F = adapters.FormField
        self.assertEqual(self.match(F('email', 'Email', True, 'email'))['value'], 'candidate@example.org')
        self.assertEqual(self.match(F('first_name', 'First Name', True))['value'], 'Test')
        self.assertEqual(self.match(F('name', 'Full name', True))['value'], 'Test Candidate')
        self.assertEqual(self.match(F('resume', 'Resume/CV', True, 'file'))['source'], 'cv_pdf')
        self.assertEqual(self.match(F('resume', 'Resume/CV', True, 'file'), cv={'pdf': None})['status'], 'missing')
        self.assertEqual(self.match(F('cover_letter', 'Cover letter', True, 'textarea'))['status'], 'missing')   # never generated
        self.assertEqual(self.match(F('cover_letter', 'Cover letter', False, 'textarea'))['status'], 'optional')
        self.assertEqual(self.match(F('preferred_name', 'Preferred First Name', False))['value'], None)
        self.assertEqual(self.match(F('salary', 'Salary expectation', True))['status'], 'missing')            # not in profile

    def test_screening_questions_only_from_explicit_answers(self):
        F = adapters.FormField
        permit = self.match(F('q1', 'Do you hold a valid work permit?', True, 'single_choice', ['Yes', 'No'], True))
        self.assertEqual((permit['value'], permit['source'], permit['status']), ('Yes', 'answer', 'ok'))
        why = self.match(F('q2', 'Why do you want to join us?', True, 'textarea', [], True))
        self.assertEqual((why['value'], why['status']), (None, 'needs_answer'))
        before = self.match(F('q3', 'Have you worked for Acme before?', True, 'single_choice', ['Yes', 'No'], True))
        self.assertEqual(before['status'], 'needs_answer')
        mismatch = self.match(F('q4', 'Work permit status', True, 'single_choice', ['EU', 'Non-EU'], True))
        self.assertEqual(mismatch['status'], 'needs_answer')
        self.assertIn('options', mismatch['note'])
        langs = self.match(F('q5', 'Which languages do you speak?', True, 'multi_choice', ['English', 'Dutch', 'German'], True))
        self.assertEqual((langs['value'], langs['source']), (['English', 'Dutch'], 'profile:languages'))


class PreparationTests(SupervisedBase):
    def test_recruitee_package_with_captcha_requires_browser_submission(self):
        calls = []
        job, app = self.prepared(http=employer(calls=calls))
        detail = json.loads(app['detail'])
        pkg = detail['package']
        self.assertTrue(detail['prepared'])
        self.assertIs(pkg['captcha'], True)
        self.assertEqual(pkg['availability'], {'open': True, 'checked_via': 'Recruitee offer API'})
        self.assertEqual(pkg['apply_page'], 'https://acme.recruitee.com/o/ai-engineer/c/new')
        self.assertTrue(pkg['cv']['pdf_current'])
        by_key = {f['key']: f for f in pkg['fields']}
        self.assertEqual(by_key['email']['value'], 'candidate@example.org')
        self.assertEqual(by_key['cv']['source'], 'cv_pdf')
        self.assertIs(by_key['question_1']['value'], True)                      # boolean question, from the explicit answer
        self.assertNotIn('location_choice', by_key)                              # hidden single-location question
        self.assertEqual(pkg['questions'], ['Why do you want to join us?'])
        self.assertEqual(pkg['missing'], [])
        self.assertFalse(pkg['ready'])
        self.assertIn('CAPTCHA', app['last_error'])
        self.assertTrue(all('/api/offers/' in c or '/c/new' in c for c in calls))   # read-only GETs only
        self.assertNotEqual(self.job(job)['status'], 'applied')

    def test_missing_profile_values_are_flagged_not_invented(self):
        self.profile_path.write_text(FULL_PROFILE.replace('phone: "+31 6 1234 5678"\n', ''))
        job, app = self.prepared()
        pkg = self.package(app)
        self.assertIn('Phone', pkg['missing'])
        self.assertTrue(any('Verified applicant profile' in m and 'phone missing' in m for m in pkg['missing']))
        self.assertIsNone({f['key']: f for f in pkg['fields']}['phone']['value'])

    def test_form_without_captcha_is_detected(self):
        job, app = self.prepared(http=employer(page=NO_CAPTCHA_PAGE))
        self.assertIs(self.package(app)['captcha'], False)

    def test_missing_pdf_is_listed_once(self):
        rows = [prepare.match_field(adapters.FormField('cv', 'CV (PDF)', True, 'file'), None, {'pdf': None})]
        self.assertEqual(rows[0]['status'], 'missing')

    def test_closed_vacancy_is_blocked(self):
        job = self.recruitee_job()
        self.make_cv(job)
        workflow.decide(job, workflow.APPROVE_APPLY, channel='dashboard', token=self.state(job)['token'])
        self.assertEqual(self.process_applications(employer(offer=None)), ['blocked'])
        self.assertIn('no longer open', self.apps(job)[0]['last_error'])

    def test_greenhouse_form_questions(self):
        job = self.add_job(score=85, source='greenhouse:acme')
        with core.connect() as db:
            db.execute("UPDATE jobs SET source_id='123' WHERE id=?", (job,))
        job, app = self.prepared(job=job)
        pkg = self.package(app)
        self.assertEqual(pkg['adapter'], 'greenhouse')
        self.assertEqual(pkg['apply_page'], 'https://job-boards.greenhouse.io/acme/jobs/123')
        by_label = {f['label']: f for f in pkg['fields']}
        self.assertNotIn('Hidden', by_label)
        self.assertEqual(by_label['Resume/CV']['kind'], 'file')
        self.assertEqual(by_label['Which languages do you speak?']['value'], ['English', 'Dutch'])
        self.assertEqual(by_label['Are you currently authorized to work in the country where this position is located?']['value'], 'Yes')
        self.assertEqual(pkg['questions'], ['Have you worked for Acme before?'])
        self.assertIsNone(pkg['captcha'])

    def test_unknown_system_gets_generic_fields_without_network(self):
        job = self.add_job(score=85)                                            # greenhouse:acme with a non-numeric id
        job, app = self.prepared(job=job, http=employer(calls=None))
        pkg = self.package(app)
        self.assertEqual(pkg['adapter'], 'manual')
        self.assertIsNone(pkg['availability']['open'])
        self.assertTrue(any('usual ones' in n for n in pkg['notes']))

    def test_cv_is_generated_first_when_missing(self):
        job = self.recruitee_job(score=55)                                      # 40-69 tier: no automatic CV
        result = workflow.decide(job, workflow.APPROVE_CV_APPLY, channel='dashboard', token=self.state(job)['token'])
        self.assertEqual(result['message'], 'Approved - generating the CV, then preparing the application')
        self.assertEqual(self.apps(job)[0]['status'], 'awaiting_cv')
        from app import cv_pipeline
        from tests.test_cv_pipeline import FakeProvider, good_reply
        self.assertEqual(cv_pipeline.claim_next(), job)
        run(cv_pipeline.process(job, FakeProvider(good_reply)))                # also renders the PDF
        self.assertEqual(self.process_applications(employer()), ['manual_action_required'])
        self.assertTrue(self.package(self.apps(job)[0])['cv']['pdf_current'])

    def test_captcha_prevents_live_submission_even_in_live_mode(self):
        class CaptchaLive(LiveMockAdapter):
            async def form(self, http, job):
                return adapters.FormSpec(adapters.generic_fields(), available=True, captcha=True, apply_page='https://x.example/apply')
        adapter = CaptchaLive()
        job = self.add_job(score=85)
        self.make_cv(job)
        workflow.decide(job, workflow.APPROVE_APPLY, channel='dashboard', token=self.state(job)['token'])
        with mock.patch.object(adapters, 'ADAPTERS', [adapter]), \
                mock.patch.dict(os.environ, {'APPLY_MODE': 'live', 'APPLY_LIVE_ADAPTERS': 'mocklive'}):
            self.assertEqual(self.process_applications(), ['manual_action_required'])
        self.assertEqual(adapter.submits, 0)

    def test_ready_notification_once_with_review_link(self):
        job, app = self.prepared()
        run(applications.send_application_updates(sender=self.send))
        self.assertEqual(len(self.sent), 1)
        self.assertIn('Application ready for review', self.sent[0])
        self.assertIn(f"/applications/{app['id']}", self.sent[0])
        self.assertIn('CAPTCHA', self.sent[0])
        self.assertNotIn('candidate@example.org', self.sent[0])               # no personal values in Telegram
        run(applications.refresh_package(app['id'], http=employer()))
        run(applications.send_application_updates(sender=self.send))
        self.assertEqual(len(self.sent), 1)                                     # no duplicate after a re-check

    def test_refresh_picks_up_profile_changes(self):
        self.profile_path.write_text(FULL_PROFILE.replace('phone: "+31 6 1234 5678"\n', ''))
        job, app = self.prepared()
        self.assertIn('Phone', self.package(app)['missing'])
        self.profile_path.write_text(FULL_PROFILE + '  - question: "Why do you want to join us?"\n    answer: "Your RAG work."\n')
        pkg = run(applications.refresh_package(app['id'], http=employer()))
        self.assertEqual((pkg['missing'], pkg['questions']), ([], []))
        self.assertIs(pkg['ready'], True)                                       # everything known; still submitted in the browser
        self.assertEqual(self.apps(job)[0]['status'], 'manual_action_required')


class SubmissionRecordTests(SupervisedBase):
    def test_mark_submitted_requires_explicit_confirmation(self):
        job, app = self.prepared()
        with self.assertRaisesRegex(ValueError, 'Confirm'):
            applications.mark_submitted(app['id'], 'REF-1', confirmed=False)
        with self.assertRaisesRegex(ValueError, 'confirmation'):
            applications.mark_submitted(app['id'], '  ', confirmed=True)
        self.assertEqual(self.apps(job)[0]['status'], 'manual_action_required')
        self.assertEqual(applications.mark_submitted(app['id'], 'Email "Bedankt voor je sollicitatie" 12:03', confirmed=True), 'submitted')
        row = self.apps(job)[0]
        self.assertEqual((row['status'], row['external_id'][:7]), ('submitted', 'manual:'))
        self.assertIsNotNone(row['submitted_at'])
        self.assertEqual(self.job(job)['status'], 'applied')
        self.assertIn('application_submitted_manually', self.events(job))
        with self.assertRaisesRegex(ValueError, 'Already recorded'):
            applications.mark_submitted(app['id'], 'again', confirmed=True)
        self.assertEqual(self.state(job)['actions'], [])

    def test_unprepared_application_cannot_be_marked_submitted(self):
        job = self.recruitee_job()
        self.make_cv(job)
        workflow.decide(job, workflow.APPROVE_APPLY, channel='dashboard', token=self.state(job)['token'])
        (self.cvs / f'job-{job}.pdf').unlink()                                  # stale/missing PDF -> not prepared
        self.assertEqual(self.process_applications(employer()), ['manual_action_required'])
        app = self.apps(job)[0]
        self.assertFalse(json.loads(app['detail']).get('prepared'))
        with self.assertRaisesRegex(ValueError, 'prepared'):
            applications.mark_submitted(app['id'], 'REF-1', confirmed=True)

    def test_duplicate_vacancy_cannot_be_recorded_twice(self):
        job, app = self.prepared()
        applications.mark_submitted(app['id'], 'REF-1', confirmed=True)
        twin = self.add_job(score=84, source='recruitee:acme', url='https://acme.recruitee.com/o/ai-engineer')
        self.make_cv(twin)
        workflow.decide(twin, workflow.APPROVE_APPLY, channel='dashboard', token=self.state(twin)['token'])
        self.assertEqual(self.process_applications(employer()), ['blocked'])     # duplicate guard before preparation
        self.assertIn('duplicate prevented', self.apps(twin)[0]['last_error'])

    def test_cancel_allows_preparing_again(self):
        job, app = self.prepared()
        self.assertEqual(applications.cancel(app['id']), 'cancelled')
        self.assertEqual(self.apps(job)[0]['status'], 'cancelled')
        self.assertEqual(self.job(job)['status'], 'new')
        with core.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM approvals WHERE job_id=?', (job,)).fetchone()[0], 'consumed')
        self.assertIn(workflow.APPROVE_APPLY, self.state(job)['actions'])
        with self.assertRaises(ValueError):
            applications.cancel(app['id'])
        job2, app2 = self.prepared(job=self.recruitee_job())
        applications.mark_submitted(app2['id'], 'REF-2', confirmed=True)
        with self.assertRaisesRegex(ValueError, 'Cannot cancel'):
            applications.cancel(app2['id'])


class ReviewPageTests(SupervisedBase):
    def client(self, login=True):
        c = TestClient(main.app, base_url='https://testserver')
        if login:
            c.cookies.set(auth.COOKIE_NAME, auth.new_session())
        return c

    def test_review_page_requires_login_and_escapes_values(self):
        job = self.recruitee_job()
        with core.connect() as db:
            db.execute("UPDATE jobs SET title='<script>alert(1)</script> AI Engineer' WHERE id=?", (job,))
        job, app = self.prepared(job=job)
        anon = self.client(login=False)
        self.assertIn(anon.get(f"/applications/{app['id']}", follow_redirects=False).status_code, (302, 303))
        self.assertIn(anon.get(f"/applications/{app['id']}/open", follow_redirects=False).status_code, (302, 303))
        self.assertEqual(anon.post(f"/api/applications/{app['id']}/submitted", json={}).status_code, 401)
        page = self.client().get(f"/applications/{app['id']}")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('<script>alert(1)</script>', page.text)
        self.assertIn('&lt;script&gt;', page.text)
        for text in ('Open Application Page', 'Mark as Submitted', 'Why do you want to join us?', 'uses a CAPTCHA',
                     f'/api/jobs/{job}/cv.pdf', 'candidate@example.org'):
            self.assertIn(text, page.text)
        self.assertEqual(self.client().get('/applications/99999').status_code, 404)

    def test_open_page_redirects_and_logs(self):
        job, app = self.prepared()
        r = self.client().get(f"/applications/{app['id']}/open", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers['location']), (303, 'https://acme.recruitee.com/o/ai-engineer/c/new'))
        self.assertIn('application_page_opened', self.events(job))

    def test_dashboard_api_flow(self):
        job, app = self.prepared()
        c = self.client()
        listed = next(j for j in c.get('/api/jobs').json() if j['id'] == job)
        self.assertEqual(listed['application_id'], app['id'])
        self.assertNotIn('package', listed['application_detail'])              # personal values only on the review page
        self.assertEqual(listed['application_detail']['summary']['questions'], 1)
        with mock.patch.object(applications, 'HttpClient', lambda **kw: employer()):
            self.assertEqual(c.post(f"/api/applications/{app['id']}/refresh").json()['questions'], 1)
        self.assertEqual(c.post(f"/api/applications/{app['id']}/submitted", json={'confirmation': 'REF-9'}).status_code, 400)
        r = c.post(f"/api/applications/{app['id']}/submitted", json={'confirmation': 'REF-9', 'confirmed': True})
        self.assertEqual((r.status_code, r.json()), (200, {'status': 'submitted'}))
        self.assertEqual(c.post(f"/api/applications/{app['id']}/cancel").status_code, 400)
        self.assertIn('Submitted (confirmation recorded)', c.get(f"/applications/{app['id']}").text)


if __name__ == '__main__':
    unittest.main()
