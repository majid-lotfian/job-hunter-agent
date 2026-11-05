"""Approval workflows (Telegram + dashboard), application submission state machine, CV worker throughput.

No network: Gemini is a fake provider, employer endpoints are an httpx.MockTransport, Telegram is a recorder.
"""
import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault('DB_PATH', str(Path(_TMP.name) / 'workflow-test.sqlite3'))
os.environ.setdefault('DASHBOARD_TOKEN', 'test-token-for-unit-tests')
os.environ['TELEGRAM_BOT_TOKEN'] = ''

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import applications, auth, core, cv_pipeline, main, workflow  # noqa: E402
from app.applications import adapters, profile as profile_mod  # noqa: E402
from app.discovery.http import HttpClient  # noqa: E402
from tests.test_cv_pipeline import MASTER, FakeProvider, good_reply  # noqa: E402

PROFILE_YAML = """verified: true
first_name: Test
last_name: Candidate
email: candidate@example.org
phone: "+00 000 000"
answers:
  - question: "Do you live in the Netherlands?"
    answer: true
  - match: "work permit"
    answer: true
"""


def run(coro):
    return asyncio.run(coro)


def no_network():
    async def no_sleep(_):
        return None
    return HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)), sleep=no_sleep, default_interval=0, retries=0)


class LiveMockAdapter(adapters.Adapter):
    """A live-capable test adapter: lets us exercise the live path without any real endpoint."""
    name = 'mocklive'
    live_capable = True

    def __init__(self, result=None, plan=None):
        self.result = result or adapters.Result(adapters.SUBMITTED, 'ok', external_id='EXT-1')
        self.plan = plan or adapters.Result(adapters.READY, 'ready', fields=['name', 'email', 'cv (file)'])
        self.submits = 0

    def supports(self, job):
        return True

    async def prepare(self, http, job, profile, cv):
        return self.plan

    async def submit(self, http, job, profile, cv, plan):
        self.submits += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Base(unittest.TestCase):
    def setUp(self):
        core.init_db()
        self.tmp = Path(tempfile.mkdtemp(dir=_TMP.name))
        (self.tmp / 'master.html').write_text(MASTER, encoding='utf-8')
        self.cvs = self.tmp / 'cvs'
        self.profile_path = self.tmp / 'applicant_profile.yaml'
        self.sent, self.markups = [], []

        async def send(text, markup=None):
            self.sent.append(text)
            self.markups.append(markup)
            return True
        self.send = send
        path = lambda i: self.cvs / f'job-{i}.html'
        for p in (mock.patch.object(cv_pipeline, 'MASTER', self.tmp / 'master.html'),
                  mock.patch.object(cv_pipeline, 'WORKSPACES', self.tmp / 'tailoring'),
                  mock.patch.object(cv_pipeline, 'cv_path', path), mock.patch.object(workflow, 'cv_path', path),
                  mock.patch.object(applications, 'cv_path', path), mock.patch.object(main, 'cv_path', path),
                  mock.patch.object(cv_pipeline, 'send_telegram', send), mock.patch.object(workflow, 'send_telegram', send),
                  mock.patch.object(applications, 'send_telegram', send),
                  mock.patch.object(profile_mod, 'PROFILE_PATH', self.profile_path),
                  mock.patch.dict(os.environ, {'CV_MIN_SCORE': '70', 'APPROVAL_MIN_SCORE': '40', 'CV_DAILY_LIMIT': '-1',
                                               'LLM_MIN_INTERVAL_SECONDS': '0', 'APPLY_MODE': 'off', 'APPLY_LIVE_ADAPTERS': '',
                                               'LLM_MAX_REQUESTS_PER_DAY': '250', 'TELEGRAM_ACTIONS_PER_BATCH': '8'})):
            p.start()
            self.addCleanup(p.stop)
        cv_pipeline._cooldown['until'] = 0
        workflow._last_summary['count'] = None
        with core.connect() as db:
            for t in ('applications', 'approvals', 'cv_jobs', 'events', 'job_sources', 'llm_usage', 'jobs'):
                db.execute(f'DELETE FROM {t}')

    def add_job(self, score, title='AI Engineer', source='greenhouse:acme', url=None):
        with core.connect() as db:
            return db.execute(
                "INSERT INTO jobs (source,source_id,company,employer,title,location,description,url,apply_url,discovered_at,score,"
                "availability,relevance_tier) VALUES (?,?,?,?,?,'Amsterdam','We need Python, RAG and LLM agents.',?,?,'now',?,'active','primary')",
                (source, os.urandom(4).hex(), 'acme', 'Acme', title, url or f'https://jobs.example/{os.urandom(3).hex()}',
                 url, score)).lastrowid

    def make_cv(self, job_id):
        cv_pipeline.enqueue(job_id, 'new')
        claimed = cv_pipeline.claim_next()
        self.assertEqual(claimed, job_id)
        return run(cv_pipeline.process(job_id, FakeProvider(good_reply)))

    def state(self, job_id):
        return workflow.job_state(job_id)

    def apps(self, job_id):
        with core.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM applications WHERE job_id=? ORDER BY id', (job_id,))]

    def events(self, job_id):
        with core.connect() as db:
            return [r['action'] for r in db.execute('SELECT action FROM events WHERE job_id=? ORDER BY id', (job_id,))]

    def job(self, job_id):
        with core.connect() as db:
            return dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def process_applications(self, http=None):
        processed = []
        while (app_id := applications.claim_next()) is not None:
            processed.append(run(applications.process(app_id, http or no_network())))
        return processed


class TierTests(Base):
    def test_tiers(self):
        self.assertEqual([workflow.workflow_for(s) for s in (100, 70, 69, 40, 39, 0)],
                         ['auto_cv', 'auto_cv', 'approval', 'approval', 'reference', 'reference'])

    def test_below_40_is_reference_only(self):
        low = self.add_job(score=30)
        cv_pipeline.CVWorker().on_new_job(low, 30)
        self.assertIsNone(core.get_cv_job(low))
        run(workflow.send_pending_actions(sender=self.send))
        self.assertEqual(self.sent, [])


class AutoCvWorkflowTests(Base):
    def test_cv_ready_then_approve_and_apply(self):
        job = self.add_job(score=85)
        run(workflow.send_pending_actions(sender=self.send))
        self.assertEqual(self.sent, [])                                     # no message before the automatic CV exists
        self.assertEqual(self.make_cv(job), 'ready')
        run(workflow.send_pending_actions(sender=self.send))
        self.assertEqual(len(self.sent), 1)
        self.assertIn('85/100', self.sent[0])
        self.assertIn('AI keywords:', self.sent[0])
        self.assertIn('Apply URL:', self.sent[0])
        buttons = self.markups[0]['inline_keyboard'][0]
        self.assertEqual([b['text'] for b in buttons], ['Approve & Apply', 'Reject'])
        data = buttons[0]['callback_data']
        answer, remove = workflow.handle_telegram_callback(data, chat_id='42', user_id='7', allowed_chat='42')
        self.assertEqual((answer, remove), ('Approved - application queued', True))
        app = self.apps(job)[0]
        self.assertEqual((app['status'], app['cv_version']), ('queued', workflow.cv_version(job)))
        self.assertEqual(self.job(job)['status'], 'approved')
        self.assertIn('approved', self.events(job))
        # repeated press (Telegram) and the dashboard now see the same state
        self.assertEqual(workflow.handle_telegram_callback(data, chat_id='42', user_id='7', allowed_chat='42')[0][:16], 'Already approved')
        self.assertEqual(self.state(job)['actions'], [workflow.REJECT])      # only cancelling is possible now

    def test_mode_off_ends_in_manual_action_with_url_and_never_applied(self):
        job = self.add_job(score=85)
        self.make_cv(job)
        st = self.state(job)
        workflow.decide(job, workflow.APPROVE_APPLY, channel='dashboard', token=st['token'])
        self.assertEqual(self.process_applications(), ['manual_action_required'])
        self.assertNotEqual(self.job(job)['status'], 'applied')
        self.assertEqual(self.state(job)['actions'], [])                    # no second approval offered
        run(applications.send_application_updates(sender=self.send))
        self.assertIn('Apply manually: ', self.sent[-1])
        self.sent.clear()
        run(applications.send_application_updates(sender=self.send))
        self.assertEqual(self.sent, [])                                     # not repeated

    def test_stale_tokens_are_refused(self):
        job = self.add_job(score=85)
        self.make_cv(job)
        old = self.state(job)['token']
        self.cvs.joinpath(f'job-{job}.html').write_text(self.cvs.joinpath(f'job-{job}.html').read_text() + '<!-- edited -->')
        result = workflow.decide(job, workflow.APPROVE_APPLY, channel='telegram', token=old)
        self.assertEqual(result['result'], 'stale')
        self.assertEqual(self.apps(job), [])
        with core.connect() as db:
            db.execute("UPDATE jobs SET description='Requirements changed' WHERE id=?", (job,))
        self.assertEqual(workflow.decide(job, workflow.APPROVE_APPLY, channel='telegram', token=self.state(job)['token'])['result'],
                         'approved')

    def test_job_or_cv_change_after_approval_stops_submission(self):
        job = self.add_job(score=85)
        self.make_cv(job)
        workflow.decide(job, workflow.APPROVE_APPLY, channel='dashboard', token=self.state(job)['token'])
        with core.connect() as db:
            db.execute("UPDATE jobs SET description='new requirements' WHERE id=?", (job,))
        with mock.patch.object(adapters, 'ADAPTERS', [LiveMockAdapter()]), \
                mock.patch.dict(os.environ, {'APPLY_MODE': 'live', 'APPLY_LIVE_ADAPTERS': 'mocklive'}):
            self.assertEqual(self.process_applications(), ['manual_action_required'])
        self.assertIn('changed after your approval', self.apps(job)[0]['last_error'])
        self.assertEqual(self.state(job)['actions'], [workflow.APPROVE_APPLY, workflow.REJECT])   # re-approval offered


class ApprovalWorkflowTests(Base):
    def test_mid_score_single_approval_generates_validates_and_applies(self):
        job, auto = self.add_job(score=55), self.add_job(score=95)
        cv_pipeline.enqueue(auto, 'new')
        run(workflow.send_pending_actions(sender=self.send))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual([b['text'] for b in self.markups[0]['inline_keyboard'][0]], ['Approve CV & Apply', 'Reject'])
        self.assertIsNone(core.get_cv_job(job))                             # no automatic CV for 40-69
        result = workflow.decide(job, workflow.APPROVE_CV_APPLY, channel='telegram', token=self.state(job)['token'])
        self.assertEqual(result['result'], 'cv_queued')
        self.assertEqual(self.apps(job)[0]['status'], 'awaiting_cv')
        self.assertEqual(cv_pipeline.claim_next(), job)                     # approved jobs go before higher-scored auto jobs
        run(cv_pipeline.process(job, FakeProvider(good_reply)))
        self.assertEqual(self.apps(job)[0]['status'], 'queued')             # no second approval needed
        with mock.patch.object(adapters, 'ADAPTERS', [LiveMockAdapter()]), \
                mock.patch.dict(os.environ, {'APPLY_MODE': 'live', 'APPLY_LIVE_ADAPTERS': 'mocklive'}):
            self.assertEqual(self.process_applications(), ['submitted'])
        app = self.apps(job)[0]
        self.assertEqual((app['external_id'], self.job(job)['status']), ('EXT-1', 'applied'))
        self.assertIn('cv_validated', self.events(job))
        self.assertIn('application_submitted', self.events(job))

    def test_validation_warnings_pause_and_ask_again(self):
        job = self.add_job(score=55)
        workflow.decide(job, workflow.APPROVE_CV_APPLY, channel='dashboard', token=self.state(job)['token'])
        cv_pipeline.claim_next()
        invent = lambda p: {'body_html': good_reply(p)['body_html'].replace('Maintained dashboards.', 'Maintained 40 dashboards.')}
        self.assertEqual(run(cv_pipeline.process(job, FakeProvider(invent))), 'needs_review')
        app = self.apps(job)[0]
        self.assertEqual(app['status'], 'manual_action_required')
        self.assertIn('warnings', app['last_error'])
        st = self.state(job)
        self.assertEqual(st['actions'], [workflow.APPROVE_APPLY, workflow.REJECT])   # explicit Approve & Apply for this CV
        run(workflow.send_pending_actions(sender=self.send))
        self.assertIn('CV warnings:', self.sent[0])
        self.assertIn('Paused:', self.sent[0])
        self.assertEqual(len(self.sent), 1)                                 # application update is folded into it

    def test_cv_failure_pauses_without_submitting(self):
        job = self.add_job(score=55)
        workflow.decide(job, workflow.APPROVE_CV_APPLY, channel='dashboard', token=self.state(job)['token'])
        cv_pipeline.claim_next()
        bad = lambda p: {'body_html': '<p>x</p>'}
        self.assertEqual(run(cv_pipeline.process(job, FakeProvider(bad, bad))), 'failed')
        self.assertEqual(self.apps(job)[0]['status'], 'manual_action_required')
        self.assertEqual(self.process_applications(), [])

    def test_reject_cancels_everything(self):
        job = self.add_job(score=55)
        workflow.decide(job, workflow.APPROVE_CV_APPLY, channel='dashboard', token=self.state(job)['token'])
        result = workflow.decide(job, workflow.REJECT, channel='telegram', token=self.state(job)['token'])
        self.assertEqual(result['result'], 'rejected')
        self.assertEqual(core.get_cv_job(job)['status'], 'cancelled')
        self.assertEqual(self.apps(job)[0]['status'], 'cancelled')
        self.assertEqual(self.job(job)['status'], 'rejected')
        self.assertIsNone(cv_pipeline.claim_next())
        self.assertEqual(workflow.decide(job, workflow.APPROVE_CV_APPLY, channel='dashboard', token=self.state(job)['token'])['result'],
                         'already')

    def test_message_cap_and_summary(self):
        for i in range(11):
            self.add_job(score=50 + i)
        with mock.patch.dict(os.environ, {'TELEGRAM_ACTIONS_PER_BATCH': '3'}):
            run(workflow.send_pending_actions(sender=self.send))
            self.assertEqual(sum(1 for m in self.markups if m), 3)
            self.assertIn('8 more job(s) await your decision', self.sent[-1])
            self.sent.clear(), self.markups.clear()
            run(workflow.send_pending_actions(sender=self.send))
            self.assertEqual(sum(1 for m in self.markups if m), 3)          # next batch continues, highest score first


class TelegramSecurityTests(Base):
    def test_authorization_and_malformed_data(self):
        job = self.add_job(score=55)
        token = self.state(job)['token']
        data = f'wf:ac:{job}:{token}'
        self.assertEqual(workflow.handle_telegram_callback(data, chat_id='99', user_id='7', allowed_chat='42'), ('Unauthorized', False))
        self.assertEqual(workflow.handle_telegram_callback(data, chat_id='42', user_id='8', allowed_chat='42', allowed_users={'7'})[0],
                         'Unauthorized')
        self.assertEqual(workflow.handle_telegram_callback('wf:zz:1:abc', chat_id='42', user_id='7', allowed_chat='42')[0], 'Unknown action')
        self.assertEqual(self.apps(job), [])
        self.assertIn('older message', workflow.handle_telegram_callback(f'approve:{job}', chat_id='42', user_id='7', allowed_chat='42')[0])
        self.assertEqual(self.apps(job), [])                                # legacy "Approve draft" never approves an application
        self.assertEqual(workflow.handle_telegram_callback(f'reject:{job}', chat_id='42', user_id='7', allowed_chat='42')[0], 'Rejected')


class DashboardDecisionTests(Base):
    def test_dashboard_uses_same_backend(self):
        job = self.add_job(score=55)
        c = TestClient(main.app, base_url='https://testserver')
        self.assertEqual(c.post(f'/api/jobs/{job}/decision', json={'action': 'reject', 'token': 'x'}).status_code, 401)
        c.cookies.set(auth.COOKIE_NAME, auth.new_session())
        listed = next(j for j in c.get('/api/jobs').json() if j['id'] == job)
        self.assertEqual((listed['workflow'], listed['actions']), ('approval', ['approve_cv_apply', 'reject']))
        self.assertEqual(c.post(f'/api/jobs/{job}/decision', json={'action': 'approve_cv_apply', 'token': 'deadbeef00'}).status_code, 409)
        r = c.post(f'/api/jobs/{job}/decision', json={'action': 'approve_cv_apply', 'token': listed['token']})
        self.assertEqual((r.status_code, r.json()['result']), (200, 'cv_queued'))
        with core.connect() as db:
            self.assertEqual(db.execute('SELECT channel FROM approvals WHERE job_id=?', (job,)).fetchone()[0], 'dashboard')


class ApplicationStateTests(Base):
    def approved_job(self, score=85, **kw):
        job = self.add_job(score=score, **kw)
        self.make_cv(job)
        workflow.decide(job, workflow.APPROVE_APPLY, channel='dashboard', token=self.state(job)['token'])
        return job

    def test_live_requires_listed_live_capable_adapter(self):
        job = self.approved_job()
        adapter = LiveMockAdapter()
        with mock.patch.object(adapters, 'ADAPTERS', [adapter]), mock.patch.dict(os.environ, {'APPLY_MODE': 'live'}):
            self.assertEqual(self.process_applications(), ['dry_run'])      # not listed in APPLY_LIVE_ADAPTERS
        self.assertEqual(adapter.submits, 0)
        self.assertNotEqual(self.job(job)['status'], 'applied')

    def test_submission_without_confirmation_id_is_not_applied(self):
        job = self.approved_job()
        adapter = LiveMockAdapter(result=adapters.Result(adapters.SUBMITTED, 'sent'))
        with mock.patch.object(adapters, 'ADAPTERS', [adapter]), \
                mock.patch.dict(os.environ, {'APPLY_MODE': 'live', 'APPLY_LIVE_ADAPTERS': 'mocklive'}):
            self.assertEqual(self.process_applications(), ['manual_action_required'])
        self.assertNotEqual(self.job(job)['status'], 'applied')

    def test_retries_then_failure_and_blocked_states(self):
        job = self.approved_job()
        adapter = LiveMockAdapter(result=adapters.Result(adapters.RETRY, 'HTTP 503'))
        with mock.patch.object(adapters, 'ADAPTERS', [adapter]), \
                mock.patch.dict(os.environ, {'APPLY_MODE': 'live', 'APPLY_LIVE_ADAPTERS': 'mocklive', 'APPLY_MAX_ATTEMPTS': '2'}):
            self.assertEqual(self.process_applications(), ['queued'])
            self.assertIsNotNone(self.apps(job)[0]['next_attempt_at'])
            with core.connect() as db:
                db.execute('UPDATE applications SET next_attempt_at=NULL')
            self.assertEqual(self.process_applications(), ['failed'])
        job2 = self.approved_job()
        blocked = LiveMockAdapter(plan=adapters.Result(adapters.BLOCKED, 'CAPTCHA required'))
        with mock.patch.object(adapters, 'ADAPTERS', [blocked]), mock.patch.dict(os.environ, {'APPLY_MODE': 'live', 'APPLY_LIVE_ADAPTERS': 'mocklive'}):
            self.assertEqual(self.process_applications(), ['blocked'])
        self.assertEqual(blocked.submits, 0)
        self.assertEqual(self.apps(job2)[0]['last_error'], 'CAPTCHA required')

    def test_duplicates_prevented(self):
        job = self.approved_job(url='https://acme.example/jobs/1?utm_source=x')
        with self.assertRaises(sqlite3.IntegrityError):                     # one live application per job
            with core.connect() as db:
                db.execute("INSERT INTO applications (job_id, status, created_at, updated_at) VALUES (?, 'queued', 'n', 'n')", (job,))
        with mock.patch.object(adapters, 'ADAPTERS', [LiveMockAdapter()]), \
                mock.patch.dict(os.environ, {'APPLY_MODE': 'live', 'APPLY_LIVE_ADAPTERS': 'mocklive'}):
            self.assertEqual(self.process_applications(), ['submitted'])
            twin = self.approved_job(url='https://acme.example/jobs/1?utm_source=other')   # same vacancy from another source
            self.assertEqual(self.process_applications(), ['blocked'])
        self.assertIn('duplicate prevented', self.apps(twin)[0]['last_error'])

    def test_restart_and_crash_never_resubmit(self):
        job = self.approved_job()
        applications.claim_next()                                           # claimed, then the process "dies"
        self.assertEqual(applications.recover_after_restart(), 1)
        self.assertEqual(self.apps(job)[0]['status'], 'manual_action_required')
        self.assertIsNone(applications.claim_next())
        job2 = self.approved_job()
        crashing = LiveMockAdapter(result=RuntimeError('boom'))
        with mock.patch.object(adapters, 'ADAPTERS', [crashing]), \
                mock.patch.dict(os.environ, {'APPLY_MODE': 'live', 'APPLY_LIVE_ADAPTERS': 'mocklive'}):
            run(applications.ApplicationWorker(http_factory=no_network).run_once())
        self.assertEqual(self.apps(job2)[0]['status'], 'manual_action_required')
        self.assertEqual(crashing.submits, 1)


class RecruiteeAdapterTests(Base):
    OFFER = {'offer': {'options_cv': 'required', 'options_phone': 'required', 'options_cover_letter': 'optional',
                       'open_questions': [
                           {'id': 1, 'required': True, 'kind': 'boolean', 'body': 'Ben je woonachtig in Nederland?',
                            'translations': {'en': {'body': 'Do you live in the Netherlands?'}}},
                           {'id': 2, 'required': True, 'kind': 'boolean', 'body': 'Do you hold a valid work permit?'},
                           {'id': 3, 'required': False, 'kind': 'string', 'body': 'Anything else?'}]}}

    def http(self, offer=None):
        def handler(request):
            assert request.method == 'GET', 'the adapter must never POST'
            return httpx.Response(200, json=offer or self.OFFER)
        return HttpClient(transport=httpx.MockTransport(handler), default_interval=0, retries=0)

    def job(self, **over):
        row = {'source': 'recruitee:acme', 'url': 'https://acme.recruitee.com/o/ai-engineer', 'apply_url': None, 'id': 1}
        row.update(over)
        return row

    def test_supports_and_reads_public_requirements(self):
        a = adapters.RecruiteeAdapter()
        self.assertTrue(a.supports(self.job()))
        self.assertFalse(a.supports(self.job(source='greenhouse:acme')))
        plan = run(a.prepare(self.http(), self.job(), None, None))
        self.assertEqual(plan.status, adapters.MANUAL)
        self.assertIn('applicant profile (data/applicant_profile.yaml)', plan.missing)

    def test_profile_answers_and_pdf(self):
        self.profile_path.write_text(PROFILE_YAML)
        prof = profile_mod.load_profile()
        html = self.tmp / 'cv.html'
        html.write_text('x')
        plan = run(adapters.RecruiteeAdapter().prepare(self.http(), self.job(), prof, {'html': html, 'pdf': None}))
        self.assertEqual(plan.missing, ['CV as PDF (only the HTML CV exists; no PDF renderer is configured)'])
        plan = run(adapters.RecruiteeAdapter().prepare(self.http(), self.job(), prof, {'html': html, 'pdf': html}))
        self.assertEqual(plan.status, adapters.READY)
        self.assertIn('question 1', plan.fields)
        self.assertNotIn('candidate@example.org', json.dumps(plan.fields))   # field names only, never values

    def test_unverified_profile_and_unanswered_questions(self):
        self.profile_path.write_text(PROFILE_YAML.replace('verified: true', 'verified: false').replace('match: "work permit"', 'match: "visa"'))
        plan = run(adapters.RecruiteeAdapter().prepare(self.http(), self.job(), profile_mod.load_profile(),
                                                       {'html': self.tmp, 'pdf': self.tmp}))
        self.assertIn('profile not marked verified: true', plan.missing)
        self.assertTrue(any('work permit' in m for m in plan.missing))
        self.assertFalse(adapters.RecruiteeAdapter.live_capable)


class WorkerThroughputTests(Base):
    def test_no_fixed_daily_limit_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CV_DAILY_LIMIT', None)
            self.assertEqual(cv_pipeline.settings()['daily_limit'], -1)
            jobs = [self.add_job(score=80 + i) for i in range(7)]
            for j in jobs:
                cv_pipeline.enqueue(j, 'new')
            claimed = 0
            while (j := cv_pipeline.claim_next()) is not None:
                core.set_cv_job(j, 'ready')
                claimed += 1
            self.assertEqual(claimed, 7)

    def test_llm_budget_protects_against_runaway_costs(self):
        job = self.add_job(score=85)
        cv_pipeline.enqueue(job, 'new')
        with mock.patch.dict(os.environ, {'LLM_MAX_REQUESTS_PER_DAY': '1'}):
            cv_pipeline.record_llm_request()
            self.assertIsNone(cv_pipeline.claim_next())                     # budget used: nothing claimed, job waits
            self.assertEqual(core.get_cv_job(job)['status'], 'pending')

    def test_rate_limit_pauses_all_workers(self):
        job = self.add_job(score=85)
        cv_pipeline.enqueue(job, 'new')
        cv_pipeline.claim_next()
        from app import llm
        run(cv_pipeline.process(job, FakeProvider(llm.TemporaryLLMError('HTTP 429 RESOURCE_EXHAUSTED', retry_after=30))))
        other = self.add_job(score=99)
        cv_pipeline.enqueue(other, 'new')
        self.assertIsNone(cv_pipeline.claim_next())
        self.assertGreater(cv_pipeline.quota_status()['rate_limited_for_seconds'], 0)
        cv_pipeline._cooldown['until'] = 0
        self.assertEqual(cv_pipeline.claim_next(), other)

    def test_concurrency_processes_queue_without_double_claims(self):
        jobs = [self.add_job(score=80 + i) for i in range(4)]
        for j in jobs:
            cv_pipeline.enqueue(j, 'new')
        provider = FakeProvider(*[good_reply] * 4)
        with mock.patch.dict(os.environ, {'CV_WORKER_CONCURRENCY': '2'}):
            worker = cv_pipeline.CVWorker(lambda: provider)
            run(worker.run_batch())
        self.assertEqual(sorted(core.get_cv_job(j)['status'] for j in jobs), ['ready'] * 4)
        self.assertEqual(len(provider.prompts), 4)                          # each CV generated exactly once
        self.assertEqual(cv_pipeline.llm_requests_today(), 4)


class DiscoveryIntegrationTests(Base):
    def test_engine_stores_matched_ai_keywords(self):
        from app.discovery import engine
        from tests.test_discovery import FakeSource, make_http, rec, test_cfg
        src = FakeSource('greenhouse:acme', [rec(job_id='kw1', title='Software Engineer',
                                                 description='We build RAG pipelines and LLM agents in Python with PyTorch, '
                                                             'embeddings, transformers, vector search, MLOps and evaluation. ' * 3)])
        run(engine.run_discovery(sources=[src], cfg=test_cfg(), http=make_http(lambda r: httpx.Response(500)),
                                 notifier={'text': mock.AsyncMock()}, force=True))
        with core.connect() as db:
            row = db.execute("SELECT score, ai_keywords FROM jobs WHERE source_id='kw1'").fetchone()
        self.assertEqual(set(row['ai_keywords'].split(',')), {'RAG', 'LLM', 'agent'})
        self.assertGreaterEqual(row['score'], 70)

    def test_v4_migration_marks_existing_jobs_announced(self):
        from app.discovery import store
        path = Path(_TMP.name) / 'v3.sqlite3'
        db = sqlite3.connect(path)
        db.executescript("""CREATE TABLE jobs (id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL, company TEXT NOT NULL,
            title TEXT NOT NULL, location TEXT, description TEXT, url TEXT NOT NULL, apply_url TEXT, published_at TEXT,
            discovered_at TEXT NOT NULL, score INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'new', cv_path TEXT, UNIQUE(source,source_id));
            CREATE TABLE events (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, action TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL);
            INSERT INTO jobs (source,source_id,company,title,url,discovered_at,score,status) VALUES ('lever','a','x','AI Engineer','u','2026-11-04',55,'approved');""")
        db.commit(), db.close()
        with mock.patch.object(core, 'DB', path), mock.patch.object(store, 'DB', path):
            core.init_db()
            db = sqlite3.connect(path)
            row = db.execute('SELECT status, action_notified_at FROM jobs').fetchone()
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            db.close()
        self.assertEqual(row[0], 'approved')                                # statuses preserved
        self.assertIsNotNone(row[1])                                        # no approval-message flood for old jobs
        self.assertTrue({'approvals', 'applications', 'llm_usage'} <= tables)


if __name__ == '__main__':
    unittest.main()
