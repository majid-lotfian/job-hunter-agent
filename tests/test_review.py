"""Live mode scoped to APPLY_LIVE_JOB_IDS, job-aware labels, owner re-approval gate and the Telegram review."""
import asyncio
import os
import unittest
from unittest import mock

from tests.test_browser_worker import ANSWERED, CONFIRMED, FakeBackend  # noqa: E402  (sets up the test DB)
from tests.test_supervised import SupervisedBase, employer  # noqa: E402
from app import core, workflow  # noqa: E402
from app.applications import browser, review  # noqa: E402

LIVE = {'APPLY_MODE': 'live', 'APPLY_LIVE_ADAPTERS': 'browser', 'BROWSER_WORKER': 'fake'}


class ReviewBase(SupervisedBase):
    def setUp(self):
        super().setUp()
        self.profile_path.write_text(ANSWERED)

    def gh_job(self, approve=True):
        job = self.add_job(score=90, source='greenhouse:acme')
        with core.connect() as db:
            db.execute("UPDATE jobs SET source_id=? WHERE id=?", (str(2000 + job), job))
        self.make_cv(job)
        if approve:
            self.assertEqual(workflow.decide(job, workflow.APPROVE_APPLY, channel='telegram',
                                             token=self.state(job)['token'])['result'], 'approved')
        return job

    def run_live(self, backend, env):
        with mock.patch.dict(browser.BACKENDS, {'fake': backend}), mock.patch.dict(os.environ, env):
            return self.process_applications(employer())


class LiveAllowlistTests(ReviewBase):
    def test_only_listed_job_is_submitted(self):
        other = self.gh_job()
        backend = FakeBackend(CONFIRMED)
        self.assertEqual(self.run_live(backend, {**LIVE, 'APPLY_LIVE_JOB_IDS': str(other + 1000)}), ['manual_action_required'])
        self.assertEqual(backend.calls, [])                                    # not in the allowlist: only prepared
        listed = self.gh_job()
        self.assertEqual(self.run_live(backend, {**LIVE, 'APPLY_LIVE_JOB_IDS': f' {listed} '}), ['submitted'])
        self.assertEqual([c['job_id'] for c in backend.calls], [listed])

    def test_empty_allowlist_never_submits(self):
        self.gh_job()
        backend = FakeBackend(CONFIRMED)
        self.assertEqual(self.run_live(backend, {**LIVE, 'APPLY_LIVE_JOB_IDS': ''}), ['manual_action_required'])
        self.assertEqual(backend.calls, [])

    def test_labels_follow_the_allowlist(self):
        with mock.patch.dict(os.environ, {'APPLY_MODE': 'live', 'APPLY_LIVE_JOB_IDS': '7'}):
            self.assertFalse(workflow.supervised(7))
            self.assertTrue(workflow.supervised(8))
            self.assertEqual(workflow.labels(8)[workflow.APPROVE_APPLY], 'Prepare Application')
            self.assertEqual(workflow.labels(7)[workflow.APPROVE_APPLY], 'Approve & Apply')
        with mock.patch.dict(os.environ, {'APPLY_MODE': 'off', 'APPLY_LIVE_JOB_IDS': '7'}):
            self.assertTrue(workflow.supervised(7))


class ReapprovalTests(ReviewBase):
    def test_unclear_outcome_needs_owner_verification_before_a_new_approval(self):
        job = self.gh_job()
        self.run_live(FakeBackend({'status': 'uncertain', 'reason': 'no confirmation', 'submitted_clicked': True}),
                      {**LIVE, 'APPLY_LIVE_JOB_IDS': str(job)})
        self.assertEqual(self.apps(job)[0]['status'], 'manual_action_required')
        self.assertNotIn(workflow.APPROVE_APPLY, self.state(job)['actions'])   # no blind second approval
        self.assertTrue(review.allow_reapproval(job, 'owner verified: no confirmation email, not received'))
        self.assertIn(workflow.APPROVE_APPLY, self.state(job)['actions'])
        with core.connect() as db:
            notes = [r['action'] for r in db.execute('SELECT action FROM events WHERE job_id=?', (job,))]
        self.assertIn('reapproval_allowed', notes)
        # the new approval is a new row: the consumed one is never reused
        self.assertEqual(workflow.decide(job, workflow.APPROVE_APPLY, channel='telegram',
                                         token=self.state(job)['token'])['result'], 'approved')
        with core.connect() as db:
            rows = db.execute('SELECT status FROM approvals WHERE job_id=? ORDER BY id', (job,)).fetchall()
        self.assertEqual([r['status'] for r in rows], ['consumed', 'active'])

    def test_nothing_to_reapprove_without_a_finished_application(self):
        job = self.gh_job(approve=False)
        self.assertFalse(review.allow_reapproval(job, 'x'))


class TelegramReviewTests(ReviewBase):
    def test_review_lists_exact_values_and_sends_pdf_and_bound_button(self):
        job = self.gh_job(approve=False)
        with mock.patch.object(review, 'HttpClient', lambda **kw: employer()), \
                mock.patch.dict(os.environ, {'APPLY_MODE': 'live', 'APPLY_LIVE_JOB_IDS': str(job)}):
            ok, problems, text, ctx = asyncio.run(review.build_review(job))
            self.assertTrue(ok, problems)
            self.assertIn('• Email: candidate@example.org', text)
            self.assertIn('• Have you worked for Acme before?: No', text)
            self.assertIn('• Resume/CV: tailored CV PDF (above)', text)
            self.assertIn('SUBMITS this application now', text)
            sent = {}
            async def doc(path, caption='', filename=None):
                sent['doc'] = (str(path), caption)
                return True
            async def msg(text, markup=None):
                sent['msg'] = (text, markup)
                return True
            with mock.patch.object(review, 'send_telegram_document', doc), mock.patch.object(review, 'send_telegram', msg):
                self.assertEqual(asyncio.run(review.send_review(job)), (True, []))
        self.assertTrue(sent['doc'][0].endswith('.pdf'))
        button = sent['msg'][1]['inline_keyboard'][0][0]
        self.assertEqual(button['text'], f'Approve & Submit (job {job})')
        token = self.state(job)['token']
        self.assertEqual(button['callback_data'], f"wf:{workflow.CODE_FOR[workflow.APPROVE_APPLY]}:{job}:{token}")
        self.assertEqual(self.job(job)['action_version'], token)               # the digest will not repeat it

    def test_review_is_refused_when_not_ready(self):
        job = self.add_job(score=90, source='greenhouse:acme')               # no CV, no PDF
        with mock.patch.object(review, 'HttpClient', lambda **kw: employer()):
            ok, problems, _, _ = asyncio.run(review.build_review(job))
        self.assertFalse(ok)
        self.assertTrue(any('PDF' in p for p in problems), problems)

    def test_prepare_label_when_live_is_not_enabled_for_the_job(self):
        job = self.gh_job(approve=False)
        with mock.patch.object(review, 'HttpClient', lambda **kw: employer()), mock.patch.dict(os.environ, {'APPLY_MODE': 'off'}):
            ok, _, text, ctx = asyncio.run(review.build_review(job))
        self.assertTrue(ok)
        self.assertFalse(ctx['live'])
        self.assertIn('NOT enabled for this job', text)


if __name__ == '__main__':
    unittest.main()
