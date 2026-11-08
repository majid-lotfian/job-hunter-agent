"""Mobile manual-application workflow: Telegram notification with the CV PDF, "I Submitted" confirmation,
cancel / later, authorization, stale and repeated callbacks, and separation from autonomous submission."""
import asyncio
import json
import os
import unittest
from unittest import mock

from tests.test_supervised import GH_JOB, SupervisedBase, employer  # noqa: E402  (sets up the test DB)
from app import applications, core, telegram_callbacks, workflow  # noqa: E402
from app.applications import browser, manual  # noqa: E402

CHAT, USER = '4242', '777'


class FakeTelegram:
    """Records documents/messages the app sends (sendDocument / sendMessage)."""
    def __init__(self, fail=False):
        self.docs, self.msgs, self.fail = [], [], fail

    async def doc(self, path, caption='', filename=None):
        if self.fail:
            raise RuntimeError('telegram down')
        self.docs.append({'path': str(path), 'caption': caption, 'filename': filename, 'bytes': open(path, 'rb').read()})
        return True

    async def msg(self, text, markup=None):
        if self.fail:
            raise RuntimeError('telegram down')
        self.msgs.append({'text': text, 'markup': markup})
        return {'message_id': 500 + len(self.msgs)}


class FakeClient:
    """Stands in for the httpx client of the polling loop: records Bot API calls."""
    def __init__(self):
        self.calls = []

    async def post(self, url, json=None):
        self.calls.append((url.rsplit('/', 1)[-1], json))


class ManualBase(SupervisedBase):
    def manual_app(self, gh=False):
        if gh:
            job = self.add_job(score=88, source='greenhouse:acme')
            with core.connect() as db:
                db.execute("UPDATE jobs SET source_id='3001' WHERE id=?", (job,))
            job, app = self.prepared(job=job)
        else:
            job, app = self.prepared()        # Recruitee form with CAPTCHA -> manual submission
        return job, self.apps(job)[0]

    def updates(self, tg):
        with mock.patch.object(manual, 'send_telegram_document', tg.doc), mock.patch.object(manual, 'send_telegram', tg.msg):
            return asyncio.run(applications.send_application_updates())

    def press(self, data, chat=CHAT, user=USER, allowed_users=None, message_id=900):
        client = FakeClient()
        cb = {'id': 'cb1', 'data': data, 'from': {'id': user}, 'message': {'message_id': message_id, 'chat': {'id': chat}}}
        result = asyncio.run(telegram_callbacks.handle(client, 'TOKEN', cb, allowed_chat=CHAT, allowed_users=allowed_users or set()))
        return result, client.calls

    def buttons(self, tg):
        return [b for row in tg.msgs[-1]['markup']['inline_keyboard'] for b in row]


class NotificationTests(ManualBase):
    def test_pdf_document_details_and_buttons(self):
        job, app = self.manual_app()
        tg = FakeTelegram()
        self.assertEqual(self.updates(tg), 1)
        self.assertEqual(len(tg.docs), 1)
        pdf = applications._cv_bundle(job)['pdf']
        self.assertEqual(tg.docs[0]['path'], str(pdf))                       # the current, validated PDF itself
        self.assertTrue(tg.docs[0]['bytes'].startswith(b'%PDF'))
        text = tg.msgs[0]['text']
        for part in ('Acme', 'AI Engineer', 'Match score: 85/100', 'Why not automatic:', 'CAPTCHA',
                     f"application #{app['id']}", 'https://acme.recruitee.com/o/ai-engineer/c/new'):
            self.assertIn(part, text)
        labels = [b['text'] for b in self.buttons(tg)]
        self.assertEqual(labels, ['🌐 Open Application', '✅ I Submitted This Application', '⏳ Submit Later'])
        self.assertEqual(self.buttons(tg)[0]['url'], 'https://acme.recruitee.com/o/ai-engineer/c/new')
        self.assertTrue(self.buttons(tg)[1]['callback_data'].startswith(f"ms:{app['id']}:"))
        notice = json.loads(self.apps(job)[0]['detail'])['manual_notice']
        self.assertEqual(notice['message_id'], 501)
        self.assertEqual(self.updates(tg), 0)                                 # sent once, never repeated
        self.assertEqual(len(tg.docs), 1)

    def test_greenhouse_application_url_and_missing_questions_are_listed(self):
        job, app = self.manual_app(gh=True)
        tg = FakeTelegram()
        self.updates(tg)
        self.assertIn('https://job-boards.greenhouse.io/acme/jobs/123', tg.msgs[0]['text'])
        self.assertEqual(self.buttons(tg)[0]['url'], 'https://job-boards.greenhouse.io/acme/jobs/123')
        self.assertIn('Have you worked for Acme before?', tg.msgs[0]['text'])   # unanswered screening question

    def test_failed_delivery_marks_nothing_and_is_retried(self):
        job, app = self.manual_app()
        self.assertEqual(self.updates(FakeTelegram(fail=True)), 0)
        row = self.apps(job)[0]
        self.assertIsNone(row['notified_at'])
        self.assertNotIn('manual_notice', json.loads(row['detail']))
        self.assertEqual(row['status'], 'manual_action_required')
        self.assertEqual(self.updates(FakeTelegram()), 1)                    # delivered on the next round

    def test_stale_cv_is_never_attached(self):
        job, app = self.manual_app()
        path = applications._cv_bundle(job)['html']
        path.write_text(path.read_text() + '\n<!-- edited -->')              # CV changed after preparation
        tg = FakeTelegram()
        sent, why = asyncio.run(manual.send_manual_notification(app['id'], send_doc=tg.doc, send_msg=tg.msg))
        self.assertFalse(sent)
        self.assertIn('CV changed', why)
        self.assertEqual(tg.docs, [])


class ConfirmationTests(ManualBase):
    def notified(self, gh=False):
        job, app = self.manual_app(gh=gh)
        tg = FakeTelegram()
        self.updates(tg)
        cb = {b['text']: b.get('callback_data') for b in self.buttons(tg)}
        return job, app, cb

    def test_confirm_records_user_reported_manual_submission(self):
        job, app, cb = self.notified()
        result, calls = self.press(cb['✅ I Submitted This Application'])
        self.assertEqual(self.apps(job)[0]['status'], 'manual_action_required')   # nothing recorded yet
        text, markup = result['followup']
        self.assertTrue(text.startswith("Did you successfully submit this application on the employer's website?"))
        confirm, cancel = markup['inline_keyboard'][0]
        self.assertEqual((confirm['text'], cancel['text']), ('Confirm', 'Cancel'))
        self.assertIn('sendMessage', [c[0] for c in calls])
        result, calls = self.press(confirm['callback_data'])
        row = self.apps(job)[0]
        detail = json.loads(row['detail'])
        self.assertEqual(row['status'], 'submitted')
        self.assertIsNotNone(row['submitted_at'])
        self.assertIsNone(row['external_id'])                               # no invented employer confirmation
        self.assertIsNone(row['confirmed_at'])
        self.assertTrue(detail['submitted_manually'])
        self.assertFalse(detail['employer_verified'])
        self.assertEqual(detail['confirmation_source'], manual.SOURCE)
        self.assertIn('Manually submitted', detail['reason'])                # shown on the dashboard
        self.assertEqual(self.job(job)['status'], 'applied')
        self.assertIn('application_submitted_manually', self.events(job))
        edits = [c for c in calls if c[0] == 'editMessageText']
        self.assertEqual(edits[0][1]['message_id'], 501)                    # original notification updated
        self.assertIn('✅ Manually submitted', edits[0][1]['text'])
        result, _ = self.press(confirm['callback_data'])                    # repeated press
        self.assertIn('Already recorded', result['answer'])
        self.assertEqual(self.events(job).count('application_submitted_manually'), 1)

    def test_cancel_records_nothing(self):
        job, app, cb = self.notified()
        result, _ = self.press(cb['✅ I Submitted This Application'])
        cancel = result['followup'][1]['inline_keyboard'][0][1]
        result, calls = self.press(cancel['callback_data'])
        self.assertIn('nothing was recorded', result['answer'])
        self.assertEqual(self.apps(job)[0]['status'], 'manual_action_required')
        self.assertIn('editMessageReplyMarkup', [c[0] for c in calls])      # the confirm buttons disappear

    def test_unauthorized_chat_or_user_is_rejected(self):
        job, app, cb = self.notified()
        token = cb['✅ I Submitted This Application'].rsplit(':', 1)[1]
        for kwargs in ({'chat': '999'}, {'user': '13', 'allowed_users': {USER}}):
            result, calls = self.press(f"mc:{app['id']}:{token}", **kwargs)
            self.assertEqual(result['answer'], 'Unauthorized')
        self.assertEqual(self.apps(job)[0]['status'], 'manual_action_required')

    def test_stale_or_forged_tokens_are_rejected(self):
        job, app, cb = self.notified()
        token = cb['✅ I Submitted This Application'].rsplit(':', 1)[1]
        result, _ = self.press(f"mc:{app['id']}:{'0' * 10}")
        self.assertIn('Outdated button', result['answer'])
        path = applications._cv_bundle(job)['html']
        path.write_text(path.read_text() + '\n<!-- new version -->')         # CV regenerated after the message
        result, _ = self.press(f"mc:{app['id']}:{token}")
        self.assertIn('Outdated button', result['answer'])
        self.assertEqual(self.apps(job)[0]['status'], 'manual_action_required')

    def test_submit_later_keeps_it_open_without_reminders(self):
        job, app, cb = self.notified()
        result, calls = self.press(cb['⏳ Submit Later'])
        self.assertFalse(result['remove'])
        self.assertIn('Kept for later', result['answer'])
        self.assertIsNotNone(json.loads(self.apps(job)[0]['detail'])['manual_notice']['later_at'])
        tg = FakeTelegram()
        self.assertEqual(self.updates(tg), 0)
        self.assertEqual(tg.msgs, [])
        result, _ = self.press(cb['✅ I Submitted This Application'])      # still possible afterwards
        self.assertIn('followup', result)


class SeparationTests(ManualBase):
    def test_manual_confirmation_never_triggers_autonomous_submission(self):
        job, app = self.manual_app(gh=True)
        tg = FakeTelegram()
        self.updates(tg)
        token = [b for row in tg.msgs[0]['markup']['inline_keyboard'] for b in row][1]['callback_data'].rsplit(':', 1)[1]
        backend = mock.Mock()
        with mock.patch.dict(browser.BACKENDS, {'local': backend, 'cloud_run': backend}), \
                mock.patch.dict(os.environ, {'APPLY_MODE': 'off'}):
            self.press(f"mc:{app['id']}:{token}")
            self.assertEqual(os.environ['APPLY_MODE'], 'off')
        backend.assert_not_called()
        self.assertIsNone(applications.claim_next())                        # nothing queued
        self.assertEqual(self.state(job)['actions'], [])                     # applied: no further approvals
        result = workflow.decide(job, workflow.APPROVE_APPLY, channel='telegram', token=self.state(job)['token'])
        self.assertNotEqual(result['result'], 'approved')

    def test_approval_buttons_still_go_to_the_approval_path_only(self):
        job = self.recruitee_job()
        self.make_cv(job)
        token = self.state(job)['token']
        result, _ = self.press(f"wf:{workflow.CODE_FOR[workflow.APPROVE_APPLY]}:{job}:{token}")
        self.assertIn('Approved', result['answer'])
        row = self.apps(job)[0]
        self.assertEqual(row['status'], 'queued')                            # an approval, not a manual record
        self.assertNotIn('submitted_manually', json.loads(row['detail'] or '{}'))

    def test_manual_button_on_non_manual_application_does_nothing(self):
        job = self.recruitee_job()
        self.make_cv(job)
        workflow.decide(job, workflow.APPROVE_APPLY, channel='dashboard', token=self.state(job)['token'])
        app = self.apps(job)[0]                                              # queued, not manual
        result, _ = self.press(f"mc:{app['id']}:{'a' * 10}")
        self.assertIn('no manual action recorded', result['answer'])
        self.assertEqual(self.apps(job)[0]['status'], 'queued')


if __name__ == '__main__':
    unittest.main()
