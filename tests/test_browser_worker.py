"""Cloud browser worker: dispatch, result mapping, state machine integration (fake backends), plus optional real
browser runs against local mock forms (RUN_BROWSER_TESTS=1 and Playwright installed). Never a live employer site."""
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.test_supervised import FULL_PROFILE, GH_JOB, SupervisedBase, employer  # noqa: E402  (sets up the test DB)
from app import applications, core, workflow  # noqa: E402
from app.applications import adapters, browser  # noqa: E402

FORMS = Path(__file__).parent / 'fixtures' / 'forms'
WORKER = Path(__file__).resolve().parent.parent / 'worker' / 'apply_worker.py'
LIVE = {'APPLY_MODE': 'live', 'APPLY_LIVE_JOB_IDS': '*', 'APPLY_LIVE_ADAPTERS': 'browser', 'BROWSER_WORKER': 'fake'}
ANSWERED = FULL_PROFILE + '  - question: "Have you worked for Acme before?"\n    answer: "No"\n'
CONFIRMED = {'status': 'submitted', 'reason': 'confirmation shown', 'submitted_clicked': True, 'filled': [], 'missing': [],
             'evidence': {'url': 'https://job-boards.greenhouse.io/acme/jobs/123/confirmation',
                          'confirmation_text': 'Thank you for applying! Reference GH-4711', 'screenshot_sha256': 'ab' * 32,
                          'at': '2026-11-06T15:00:00+00:00'}}


class FakeBackend:
    def __init__(self, result=CONFIRMED):
        self.result, self.calls = result, []

    def __call__(self, pkg, cfg, workdir):
        self.calls.append(pkg)
        return self.result


class BrowserIntegrationTests(SupervisedBase):
    def setUp(self):
        super().setUp()
        self.profile_path.write_text(ANSWERED)

    def gh_job(self):
        job = self.add_job(score=90, source='greenhouse:acme')
        with core.connect() as db:   # numeric id = a real Greenhouse job; unique per test job
            db.execute("UPDATE jobs SET source_id=? WHERE id=?", (str(1000 + job), job))
        self.make_cv(job)
        self.assertEqual(workflow.decide(job, workflow.APPROVE_APPLY, channel='telegram',
                                         token=self.state(job)['token'])['result'], 'approved')
        return job

    def run_with(self, backend, env=LIVE):
        with mock.patch.dict(browser.BACKENDS, {'fake': backend}), mock.patch.dict(os.environ, env):
            return self.process_applications(employer())

    def test_complete_package_is_submitted_once_with_evidence(self):
        job, backend = self.gh_job(), FakeBackend()
        self.assertEqual(self.run_with(backend), ['submitted'])
        self.assertEqual(len(backend.calls), 1)
        pkg = backend.calls[0]
        self.assertTrue(pkg['submit'])
        self.assertEqual(pkg['apply_url'], 'https://job-boards.greenhouse.io/acme/jobs/123')
        labels = {f['label']: f for f in pkg['fields']}
        self.assertEqual(labels['Resume/CV']['value'], 'cv_pdf')
        self.assertEqual(labels['Email']['value'], 'candidate@example.org')
        self.assertEqual(labels['Have you worked for Acme before?']['value'], 'No')      # explicit answer only
        self.assertTrue(pkg['cv_pdf'].endswith('.pdf'))
        app = self.apps(job)[0]
        self.assertEqual((app['status'], app['adapter']), ('submitted', 'browser'))
        self.assertTrue(app['external_id'].startswith('browser:2026-11-06T15:00:00:abab'))
        self.assertEqual(self.job(job)['status'], 'applied')
        self.assertEqual(self.run_with(backend), [])                                      # nothing left, no second run
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(self.state(job)['actions'], [])

    def test_challenge_goes_to_manual_login_stays_blocked_never_retried(self):
        cases = (({'status': 'blocked', 'reason': 'human verification challenge after submit - not completed',
                   'submitted_clicked': True}, 'manual_action_required', 'CAPTCHA'),
                 ({'status': 'blocked', 'reason': 'login required', 'submitted_clicked': False}, 'blocked', 'cloud browser stopped'))
        for result, status, text in cases:
            job, backend = self.gh_job(), FakeBackend(result)
            self.assertEqual(self.run_with(backend), [status])
            self.assertIn(text, self.apps(job)[0]['last_error'])
            self.assertEqual(len(backend.calls), 1)
            self.assertNotEqual(self.job(job)['status'], 'applied')
            self.assertIsNone(applications.claim_next())

    def test_unclear_outcomes_go_to_manual_follow_up(self):
        for result in ({'status': 'uncertain', 'reason': 'no confirmation within the time limit', 'submitted_clicked': True},
                       {'status': 'submitted', 'submitted_clicked': True, 'evidence': {}},       # "submitted" without evidence
                       {'status': 'failed', 'reason': 'worker error', 'submitted_clicked': True}, None):
            job, backend = self.gh_job(), FakeBackend(result)
            self.assertEqual(self.run_with(backend), ['manual_action_required'])
            self.assertNotEqual(self.job(job)['status'], 'applied')
            self.assertIsNone(applications.claim_next())

    def test_form_problem_before_submit_is_blocked(self):
        job = self.gh_job()
        backend = FakeBackend({'status': 'failed', 'reason': 'required fields could not be filled', 'submitted_clicked': False,
                               'missing': ['Phone']})
        self.assertEqual(self.run_with(backend), ['blocked'])
        self.assertEqual(json.loads(self.apps(job)[0]['detail'])['missing'], ['Phone'])

    def test_never_dispatched_without_all_gates(self):
        cases = {'apply mode off': {**LIVE, 'APPLY_MODE': 'off'},
                 'browser not allowed': {**LIVE, 'APPLY_LIVE_ADAPTERS': 'recruitee'},
                 'worker not configured': {**LIVE, 'BROWSER_WORKER': ''}}
        for name, env in cases.items():
            backend = FakeBackend()
            self.gh_job()
            self.assertIn(self.run_with(backend, env)[0], ('manual_action_required', 'dry_run'), name)
            self.assertEqual(backend.calls, [], name)

    def test_incomplete_package_is_not_dispatched(self):
        self.profile_path.write_text(FULL_PROFILE)          # "worked for Acme before?" unanswered
        job, backend = self.gh_job(), FakeBackend()
        self.assertEqual(self.run_with(backend), ['manual_action_required'])
        self.assertEqual(backend.calls, [])
        detail = json.loads(self.apps(job)[0]['detail'])
        self.assertFalse(detail['browser']['eligible'])
        self.assertIn('missing information or unanswered questions', detail['browser']['reasons'])

    def test_unsupported_system_is_not_dispatched(self):
        job = self.add_job(score=90)                         # generic system (no readable form)
        self.make_cv(job)
        workflow.decide(job, workflow.APPROVE_APPLY, channel='telegram', token=self.state(job)['token'])
        backend = FakeBackend()
        self.assertEqual(self.run_with(backend), ['manual_action_required'])
        self.assertEqual(backend.calls, [])

    def test_form_url_uses_greenhouse_hosted_form_not_employer_embed(self):
        job = {'source': 'greenhouse:nebius', 'source_id': '5008613101', 'apply_url': 'https://careers.nebius.com/?gh_jid=5008613101'}
        self.assertEqual(browser.form_url(job, {'apply_page': job['apply_url']}),
                         'https://job-boards.greenhouse.io/nebius/jobs/5008613101')
        hosted = 'https://job-boards.eu.greenhouse.io/jetbrains/jobs/4937092101'
        self.assertEqual(browser.form_url({'source': 'greenhouse:jetbrains', 'source_id': '4937092101'}, {'apply_page': hosted}), hosted)
        page = 'https://acme.recruitee.com/o/ml-engineer/c/new'
        self.assertEqual(browser.form_url({'source': 'recruitee:acme', 'source_id': '1'}, {'apply_page': page}), page)

    def test_posting_digest_ignores_entities_and_whitespace(self):
        from app.applications.prepare import _digest
        self.assertEqual(_digest('Build Junie.&nbsp; Ship it'), _digest('Build Junie.  Ship it'))
        self.assertEqual(_digest('A&amp;nbsp;B'), _digest('A B'))
        self.assertNotEqual(_digest('Build Junie'), _digest('Build Junie CLI'))

    def test_phone_read_back_accepts_regrouped_spaces_only(self):
        import sys as _sys
        _sys.path.insert(0, str(WORKER.parent))
        from apply_worker import same_value
        self.assertTrue(same_value('phone', '+31 6 12345678', '+31 6 1234 5678'))
        self.assertFalse(same_value('phone', '+31 6 12345679', '+31 6 1234 5678'))   # different number
        self.assertFalse(same_value('phone', '06 12345678', '+31 6 1234 5678'))      # different form: not assumed equal
        self.assertFalse(same_value('text', 'Test  Candidate', 'Test Candidate'))   # other fields: exact

    def test_recruitee_package_carries_stable_names_and_dutch_labels(self):
        job = {'id': 5, 'source': 'recruitee:conclusion', 'source_id': 'x', 'apply_url': 'https://conclusion.recruitee.com/o/x/c/new'}
        package = {'apply_page': job['apply_url'], 'fields': [
            {'key': 'name', 'label': 'Full name', 'kind': 'text', 'value': 'A', 'required': True, 'status': 'ok', 'source': 'profile:full_name'},
            {'key': 'cv', 'label': 'CV (PDF)', 'kind': 'file', 'value': 'x', 'required': True, 'status': 'ok', 'source': 'cv_pdf'},
            {'key': 'photo', 'label': 'Photo', 'kind': 'file', 'value': None, 'required': False, 'status': 'optional', 'source': None}]}
        pkg = browser.build_worker_package({'id': 1}, job, package, '/tmp/cv.pdf', submit=False)
        fields = {f['key']: f for f in pkg['fields']}
        self.assertEqual(set(fields), {'name', 'cv'})                        # optional photo is never sent
        self.assertEqual(fields['name']['selectors'], ['[name="candidate.name"]'])
        self.assertIn('Volledige naam', fields['name']['aliases'])
        self.assertEqual(fields['cv']['selectors'], ['input[type="file"][name="candidate.cv"]'])
        gh = browser.build_worker_package({'id': 1}, {**job, 'source': 'greenhouse:acme', 'source_id': '1'}, package, '/tmp/cv.pdf', False)
        self.assertEqual(gh['fields'][0]['selectors'], [])                    # other systems: labels as before

    def test_mapping(self):
        R = adapters
        self.assertEqual(browser.map_result(CONFIRMED).status, R.SUBMITTED)
        self.assertEqual(browser.map_result({'status': 'filled'}).status, R.DRY_RUN)
        self.assertEqual(browser.map_result({'status': 'blocked', 'reason': 'x'}).status, R.BLOCKED)
        self.assertEqual(browser.map_result({'status': 'failed', 'submitted_clicked': False}).status, R.BLOCKED)
        self.assertEqual(browser.map_result({'status': 'failed', 'submitted_clicked': True}).status, R.MANUAL)
        self.assertEqual(browser.map_result(None).status, R.MANUAL)


@unittest.skipUnless(os.getenv('RUN_BROWSER_TESTS') == '1', 'real browser runs only with RUN_BROWSER_TESTS=1')
class MockFormBrowserTests(unittest.TestCase):
    """End-to-end: the real worker + headless Chromium against local mock forms (no employer site)."""

    @classmethod
    def setUpClass(cls):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FORMS))
        handler.log_message = lambda *a: None
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'
        cls.tmp = Path(tempfile.mkdtemp())
        (cls.tmp / 'cv.pdf').write_bytes(b'%PDF-1.4\n% test\n%%EOF\n')

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_worker(self, page, fields, submit=True):
        pkg = {'application_id': 1, 'job_id': 1, 'apply_url': f'{self.base}/{page}', 'cv_pdf': str(self.tmp / 'cv.pdf'),
               'fields': fields, 'submit': submit, 'confirmation': {}, 'timeouts': {'page': 120, 'action': 60, 'confirm': 30}}
        (self.tmp / 'p.json').write_text(json.dumps(pkg))
        subprocess.run([os.getenv('BROWSER_WORKER_PYTHON', sys.executable), str(WORKER), '--package', str(self.tmp / 'p.json'),
                        '--result', str(self.tmp / 'r.json'), '--evidence-dir', str(self.tmp / 'ev')], timeout=600, check=True)
        return json.loads((self.tmp / 'r.json').read_text())

    GH = [{'label': 'First Name', 'kind': 'text', 'value': 'Test', 'required': True},
          {'label': 'Last Name', 'kind': 'text', 'value': 'Candidate', 'required': True},
          {'label': 'Email', 'kind': 'email', 'value': 'candidate@example.org', 'required': True},
          {'label': 'Phone', 'kind': 'phone', 'value': '+31 6 1234 5678', 'required': True},
          {'label': 'Resume/CV', 'kind': 'file', 'value': 'cv_pdf', 'required': True},
          {'label': 'Are you currently authorized to work in the country where this position is located?',
           'kind': 'single_choice', 'value': 'Yes', 'required': True},
          {'label': 'Which languages do you speak?', 'kind': 'multi_choice', 'value': ['English', 'Dutch'], 'required': True}]

    def test_greenhouse_like_form_is_submitted_with_confirmation(self):
        r = self.run_worker('greenhouse_like.html', self.GH)
        self.assertEqual(r['status'], 'submitted', r)
        self.assertEqual(r['missing'], [])
        self.assertIn('Thank you for applying', r['evidence']['confirmation_text'])
        self.assertTrue((self.tmp / 'ev' / r['evidence']['screenshot']).is_file())

    def test_dry_run_fills_without_submitting(self):
        r = self.run_worker('greenhouse_like.html', self.GH, submit=False)
        self.assertEqual((r['status'], r['submitted_clicked']), ('filled', False))

    def test_visible_challenge_is_blocked(self):
        r = self.run_worker('greenhouse_like.html?mode=challenge', self.GH)
        self.assertEqual(r['status'], 'blocked', r)
        self.assertIn('human verification', r['reason'])

    def test_login_wall_is_blocked_before_filling(self):
        r = self.run_worker('login.html', self.GH)
        self.assertEqual((r['status'], r['reason'], r['filled']), ('blocked', 'login required', []))

    def test_validation_error_and_silence(self):
        self.assertEqual(self.run_worker('greenhouse_like.html?mode=invalid', self.GH)['status'], 'failed')
        self.assertEqual(self.run_worker('greenhouse_like.html?mode=silent', self.GH)['status'], 'uncertain')

    def test_recruitee_like_form_with_radio_question(self):
        r = self.run_worker('recruitee_like.html', [
            {'label': 'Full name', 'kind': 'text', 'value': 'Test Candidate', 'required': True},
            {'label': 'Email', 'kind': 'email', 'value': 'candidate@example.org', 'required': True},
            {'label': 'Phone', 'kind': 'phone', 'value': '+31612345678', 'required': True},
            {'label': 'CV', 'kind': 'file', 'value': 'cv_pdf', 'required': True},
            {'label': 'Do you hold a valid work permit?', 'kind': 'boolean', 'value': True, 'required': True}])
        self.assertEqual(r['status'], 'submitted', r)
        self.assertIn('Bedankt voor je sollicitatie', r['evidence']['confirmation_text'])


    GH_REACT = [{'label': 'First Name', 'kind': 'text', 'value': 'Test', 'required': True},
                {'label': 'Last Name', 'kind': 'text', 'value': 'Candidate', 'required': True},
                {'label': 'Email', 'kind': 'email', 'value': 'candidate@example.org', 'required': True},
                {'label': 'Resume/CV', 'kind': 'file', 'value': 'cv_pdf', 'required': True},
                {'label': 'How did you hear about this role?', 'kind': 'single_choice', 'value': "JetBrains' website", 'required': True},
                {'label': 'Which is your preferred location for this role?', 'kind': 'multi_choice',
                 'value': ['Amsterdam, Netherlands'], 'required': True}]

    def test_real_greenhouse_widgets_combobox_and_checkbox_list(self):
        r = self.run_worker('greenhouse_react.html', self.GH_REACT)
        self.assertEqual((r['status'], r['missing']), ('submitted', []), r)

    @staticmethod
    def recruitee_fields(hints=True, aliases=True):
        from app.applications.browser import field_hints
        rows = [('name', 'Full name', 'text', 'Test Candidate'), ('email', 'Email', 'email', 'candidate@example.org'),
                ('phone', 'Phone', 'phone', '+31 6 1234 5678'), ('cv', 'CV (PDF)', 'file', 'cv_pdf')]
        out = []
        for key, label, kind, value in rows:
            selectors, alias = field_hints('recruitee', key)
            out.append({'key': key, 'label': label, 'kind': kind, 'value': value, 'required': True,
                        'selectors': selectors if hints else [], 'aliases': alias if aliases else []})
        return out

    def test_dutch_recruitee_form_by_stable_names(self):
        r = self.run_worker('recruitee_nl.html', self.recruitee_fields())
        self.assertEqual((r['status'], r['missing']), ('submitted', []), r)
        self.assertTrue(r['verified_before_submit'])
        self.assertIn('name=Test Candidate email=candidate@example.org phone=+31 6 12345678 cv=1 photo=0 cover=0 submits=1',
                      r['evidence']['confirmation_text'])                     # CV in the CV field, never the photo

    def test_dutch_recruitee_form_by_dutch_labels_only(self):
        r = self.run_worker('recruitee_nl.html', self.recruitee_fields(hints=False))
        self.assertEqual(r['status'], 'submitted', r)
        self.assertIn('cv=1 photo=0 cover=0', r['evidence']['confirmation_text'])

    def test_english_recruitee_form_by_english_labels(self):
        fields = self.recruitee_fields(hints=False, aliases=False)
        fields[2]['label'], fields[3]['label'] = 'Phone number', 'CV'           # labels exactly as the English page shows
        r = self.run_worker('recruitee_nl.html?lang=en', fields)
        self.assertEqual(r['status'], 'submitted', r)
        self.assertIn('cv=1 photo=0 cover=0', r['evidence']['confirmation_text'])

    def test_unknown_labels_never_guess_a_file_input(self):
        r = self.run_worker('recruitee_nl.html', self.recruitee_fields(hints=False, aliases=False))
        self.assertEqual((r['status'], r['submitted_clicked']), ('failed', False), r)
        self.assertEqual(set(r['missing']), {'Full name', 'Email', 'Phone', 'CV (PDF)'})   # not attached to "Foto"

    def test_dry_run_on_dutch_form_reads_every_value_back(self):
        r = self.run_worker('recruitee_nl.html', self.recruitee_fields(), submit=False)
        self.assertEqual((r['status'], r['submitted_clicked'], r['missing'], r['cleared']), ('filled', False, [], []), r)
        self.assertTrue(r['verified_before_submit'])

    def test_resume_autofill_overwrite_is_undone_before_submit(self):
        r = self.run_worker('greenhouse_react.html?autofill=fast', self.GH_REACT)
        self.assertEqual(r['status'], 'submitted', r)
        self.assertIn('Received first=Test last=Candidate preferred= submits=1', r['evidence']['confirmation_text'])
        self.assertIn('Preferred First Name', r['cleared'])          # autofilled extra field emptied again

    def test_file_chosen_before_the_page_is_ready_is_attached_again(self):
        r = self.run_worker('greenhouse_react.html?hydrate=3500&autofill=fast', self.GH_REACT)
        self.assertEqual(r['status'], 'submitted', r)
        self.assertIn('Received first=Test last=Candidate', r['evidence']['confirmation_text'])

    def test_late_autofill_is_caught_by_the_final_check(self):
        r = self.run_worker('greenhouse_react.html?autofill=late', self.GH_REACT)
        self.assertEqual(r['status'], 'submitted', r)
        self.assertIn('First Name', r['refilled'])
        self.assertIn('Received first=Test last=Candidate preferred= submits=1', r['evidence']['confirmation_text'])

    def test_field_the_page_keeps_changing_stops_before_submit(self):
        r = self.run_worker('greenhouse_react.html?autofill=always', self.GH_REACT)
        self.assertEqual((r['status'], r['submitted_clicked']), ('failed', False), r)
        self.assertIn('First Name', r['missing'])

    def test_greenhouse_required_message_is_reported_as_form_error(self):
        r = self.run_worker('greenhouse_react.html?mode=gh_required', self.GH_REACT)
        self.assertEqual((r['status'], r['submitted_clicked']), ('failed', True), r)
        self.assertIn('First Name is required', r['reason'])

    def test_rejected_upload_stops_before_submit(self):
        r = self.run_worker('greenhouse_react.html?upload=reject', self.GH_REACT)
        self.assertEqual((r['status'], r['submitted_clicked']), ('failed', False), r)
        self.assertEqual(r['missing'], ['Resume/CV'])

    def test_combobox_never_picks_an_unlisted_answer(self):
        fields = [dict(f) for f in self.GH_REACT]
        fields[4]['value'] = 'A recruiter called me'          # not one of the employer's options
        r = self.run_worker('greenhouse_react.html', fields)
        self.assertEqual((r['status'], r['submitted_clicked']), ('failed', False), r)
        self.assertIn('How did you hear about this role?', r['missing'])

    def test_turnstile_waiting_for_a_click_blocks_before_submit(self):
        r = self.run_worker('greenhouse_react.html?turnstile=block', self.GH_REACT)
        self.assertEqual((r['status'], r['submitted_clicked']), ('blocked', False), r)
        self.assertIn('turnstile', r['reason'])

    def test_turnstile_that_passed_silently_is_not_a_blocker(self):
        r = self.run_worker('greenhouse_react.html?turnstile=pass', self.GH_REACT)
        self.assertEqual(r['status'], 'submitted', r)


class CaptchaClassificationTests(unittest.TestCase):
    """Pure functions of the worker/probe: which CAPTCHA frame or request means what (no browser needed)."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(WORKER.parent))
        import apply_worker
        import captcha_probe
        cls.w, cls.p = apply_worker, captcha_probe

    def test_frames(self):
        c = self.w.classify_frame
        self.assertEqual(c('https://www.google.com/recaptcha/enterprise/anchor?ar=1&k=K&size=invisible'), ('recaptcha', 'invisible'))
        self.assertEqual(c('https://www.google.com/recaptcha/api2/anchor?ar=1&k=K&size=normal'), ('recaptcha', 'checkbox'))
        self.assertEqual(c('https://www.google.com/recaptcha/enterprise/bframe?hl=en&k=K'), ('recaptcha', 'challenge'))
        self.assertEqual(c('https://newassets.hcaptcha.com/captcha/v1/x/static/hcaptcha.html#frame=challenge&id=1'), ('hcaptcha', 'challenge'))
        self.assertEqual(c('https://captcha-assets.recruiteecdn.com/captcha/v1/x/static/hcaptcha-enclave.html#frame=enclave',
                           'Widget containing checkbox for hCaptcha security challenge'), ('hcaptcha', 'checkbox'))
        self.assertEqual(c('about:srcdoc', 'Main content of the hCaptcha challenge'), ('hcaptcha', 'challenge'))
        self.assertEqual(c('https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/f/av0/rch/x'), ('turnstile', 'widget'))
        self.assertEqual(c('https://www.youtube.com/embed/x', 'video'), (None, None))

    def test_request_modes_and_verdicts(self):
        modes = self.p.provider_modes([
            'https://www.google.com/recaptcha/enterprise.js?render=6LcKEY',
            'https://www.google.com/recaptcha/enterprise/anchor?ar=1&k=6LcKEY&size=invisible',
            'https://captcha-base.recruiteecdn.com/checksiteconfig?v=1&host=x'])
        self.assertEqual(modes, {'recaptcha': ['invisible', 'score-based (enterprise)'], 'hcaptcha': ['site config checked']})
        base = {'access': 'ok', 'challenge_on_load': None, 'challenge_after_focus': None}
        v = self.p.verdict
        self.assertEqual(v({**base, 'scripts': [], 'modes': {}}), 'no CAPTCHA observed')
        self.assertTrue(v({**base, 'scripts': ['recaptcha'], 'modes': {'recaptcha': ['score-based (enterprise)']}}).startswith('score-based'))
        self.assertTrue(v({**base, 'scripts': ['hcaptcha'], 'modes': modes}).startswith('invisible'))
        self.assertTrue(v({**base, 'scripts': ['turnstile'], 'modes': {}, 'challenge_on_load': 'x'}).startswith('interactive'))
        self.assertTrue(v({**base, 'access': 'refused (403)', 'scripts': [], 'modes': {}}).startswith('blocked'))

if __name__ == '__main__':
    unittest.main()
