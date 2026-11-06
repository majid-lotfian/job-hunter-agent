"""CV PDF generation (app/pdf.py + its integration in the CV worker and the application workflow).

Real WeasyPrint renders are used where it is installed (skipped otherwise); integration tests use a fake renderer.
No network.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault('DB_PATH', str(Path(_TMP.name) / 'pdf-test.sqlite3'))
os.environ.setdefault('DASHBOARD_TOKEN', 'test-token-for-unit-tests')
os.environ['TELEGRAM_BOT_TOKEN'] = ''

from app import core, cv_pipeline, pdf  # noqa: E402
from tests.test_cv_pipeline import FAKE_PDF, MASTER, REPORT, FakeProvider, PipelineTests, body_of, fake_runner, good_reply, run, tailored  # noqa: E402

try:
    import weasyprint  # noqa: F401
    HAVE_WEASYPRINT = True
except (ImportError, OSError):
    HAVE_WEASYPRINT = False

class RenderTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=_TMP.name))
        self.html = self.dir / 'job-1.html'
        self.html.write_text(MASTER, encoding='utf-8')
        self.out = self.dir / 'job-1.pdf'

    def leftovers(self):
        return [p.name for p in self.dir.iterdir() if p.name.endswith('.tmp')]

    @unittest.skipUnless(HAVE_WEASYPRINT, 'WeasyPrint not installed')
    def test_real_render_is_a_valid_a4_pdf_with_text(self):
        info = pdf.render_pdf(self.html, self.out)
        self.assertEqual(info['pages'], 1)
        self.assertEqual(info['page_mm'], [210.0, 297.0])
        self.assertGreater(info['text_chars'], 200)
        self.assertGreater(info['bytes'], 1000)
        self.assertTrue(self.out.read_bytes().startswith(b'%PDF-'))
        self.assertEqual(pdf.sidecar(self.out).read_text().strip(), pdf.sha256_file(self.html))
        self.assertTrue(pdf.pdf_is_current(self.html, self.out))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.html.read_text(encoding='utf-8'), MASTER)      # the HTML CV is never modified

    @unittest.skipUnless(HAVE_WEASYPRINT, 'WeasyPrint not installed')
    def test_external_resources_are_not_fetched(self):
        self.html.write_text(MASTER.replace('<h2>Profile</h2>', '<img src="http://example.invalid/t.png">'
                                            '<link rel="stylesheet" href="file:///etc/passwd"><h2>Profile</h2>'), encoding='utf-8')
        info = pdf.render_pdf(self.html, self.out)            # missing resources do not break the render
        self.assertEqual(info['pages'], 1)
        fetcher = pdf.offline_fetcher()
        for url in ('http://example.invalid/t.png', 'https://example.invalid/x.css', 'file:///etc/passwd'):
            with self.assertRaises(Exception):
                fetcher.fetch(url)
        self.assertIsNotNone(fetcher.fetch('data:text/plain,ok'))

    def test_missing_html(self):
        with self.assertRaises(pdf.PdfError):
            pdf.render_pdf(self.dir / 'nope.html', self.out, runner=fake_runner())
        self.assertFalse(self.out.exists())

    def test_failures_never_replace_an_existing_pdf(self):
        pdf.render_pdf(self.html, self.out, runner=fake_runner())
        good = self.out.read_bytes()
        cases = [fake_runner(returncode=1, stderr='Traceback...\nMemoryError'),     # renderer crashed
                 fake_runner(data=b'<html>not a pdf</html>' * 100),                  # garbage output
                 fake_runner(data=b'%PDF-1.7\n' + b'x' * 2000),                       # truncated (no %%EOF)
                 fake_runner(report={**REPORT, 'text_chars': 0}),                    # blank
                 fake_runner(report={**REPORT, 'pages': 9}),                         # far too long
                 fake_runner(report={**REPORT, 'page_mm': [215.9, 279.4]}),          # US Letter, not A4
                 fake_runner(data=None)]                                              # no output at all
        for runner in cases:
            with self.assertRaises(pdf.PdfError):
                pdf.render_pdf(self.html, self.out, runner=runner)
            self.assertEqual(self.out.read_bytes(), good)
        self.assertEqual(self.leftovers(), [])

    def test_timeout_is_reported(self):
        def hanging(html, out, cfg):
            raise subprocess.TimeoutExpired('renderer', cfg['timeout'])
        with self.assertRaisesRegex(pdf.PdfError, 'timed out'):
            pdf.render_pdf(self.html, self.out, runner=hanging)
        self.assertEqual(self.leftovers(), [])

    def test_pdf_is_stale_after_html_changes(self):
        pdf.render_pdf(self.html, self.out, runner=fake_runner())
        self.assertTrue(pdf.pdf_is_current(self.html, self.out))
        self.html.write_text(MASTER + '<!-- edited -->', encoding='utf-8')
        self.assertFalse(pdf.pdf_is_current(self.html, self.out))
        self.out.write_bytes(b'broken')
        self.assertFalse(pdf.pdf_is_current(self.html, self.out))

    def test_child_gets_timeout_and_memory_cap(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen.update(kwargs, cmd=cmd)
            Path(cmd[-1]).write_bytes(FAKE_PDF)
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(REPORT), stderr='')
        with mock.patch.object(pdf.subprocess, 'run', fake_run), mock.patch.dict(os.environ, {'PDF_TIMEOUT_SECONDS': '45'}):
            pdf.render_pdf(self.html, self.out)
        self.assertEqual(seen['timeout'], 45)
        self.assertIsNotNone(seen['preexec_fn'])
        self.assertEqual(seen['cmd'][1:4], ['-m', 'app.pdf', '--render'])


class PipelinePdfTests(unittest.TestCase):
    """Automatic PDF after a successful CV, independent retries, no Gemini rerun."""
    add_job = PipelineTests.add_job
    claim_and_process = PipelineTests.claim_and_process

    def setUp(self):
        PipelineTests.setUp(self)
        self.calls = []
        self.runner = fake_runner(calls=self.calls)
        patcher = mock.patch.object(pdf, '_run_child', lambda h, o, c: self.runner(h, o, c))
        patcher.start()
        self.addCleanup(patcher.stop)
        with core.connect() as db:
            db.execute('DELETE FROM events')

    def events(self, job_id):
        with core.connect() as db:
            return [r['action'] for r in db.execute('SELECT action FROM events WHERE job_id=? ORDER BY id', (job_id,))]

    def pdf_file(self, job_id=None):
        return self.cvs / f'job-{job_id or self.job}.pdf'

    def test_successful_cv_gets_a_pdf_automatically(self):
        cv_pipeline.enqueue(self.job)
        provider = FakeProvider(good_reply)
        worker = cv_pipeline.CVWorker(lambda: provider)
        run(worker.run_batch())
        row = core.get_cv_job(self.job)
        self.assertEqual((row['status'], row['pdf_status']), ('ready', 'ready'))
        self.assertTrue(self.pdf_file().is_file())
        self.assertTrue(cv_pipeline.pdf_current(self.job))
        self.assertEqual(json.loads(row['pdf_info'])['pages'], 1)
        self.assertIn('cv_pdf_ready', self.events(self.job))
        self.assertEqual(len(provider.prompts), 1)
        self.assertTrue(cv_pipeline.cv_state(self.job)['cv_has_pdf'])

    def test_pdf_failure_is_recorded_and_retried_without_gemini(self):
        cv_pipeline.enqueue(self.job)
        provider = FakeProvider(good_reply)
        self.runner = fake_runner(returncode=1, stderr='boom')
        with mock.patch.dict(os.environ, {'PDF_MAX_ATTEMPTS': '2'}):
            _, status = self.claim_and_process(provider)                      # inline PDF attempt fails
            self.assertEqual(status, 'ready')                                  # the HTML CV is still ready
            html_before = (self.cvs / f'job-{self.job}.html').read_text(encoding='utf-8')
            worker = cv_pipeline.CVWorker(lambda: provider)
            row = core.get_cv_job(self.job)
            self.assertEqual((row['status'], row['pdf_status'], row['pdf_attempts']), ('ready', 'failed', 1))
            self.assertIn('exit 1', row['pdf_error'])
            self.assertIsNotNone(row['pdf_next_attempt_at'])
            self.assertIn('cv_pdf_failed', self.events(self.job))
            self.assertEqual(cv_pipeline.pdf_candidates(), [])                  # waits for its backoff
            self.assertEqual(run(worker.process_pdfs()), 0)
            with core.connect() as db:
                db.execute('UPDATE cv_jobs SET pdf_next_attempt_at=NULL')
            run(worker.process_pdfs())                                         # second failure: no automatic retry left
            self.assertEqual(core.get_cv_job(self.job)['pdf_attempts'], 2)
            self.assertIsNone(core.get_cv_job(self.job)['pdf_next_attempt_at'])
            self.assertEqual(cv_pipeline.pdf_candidates(), [])
            self.assertEqual(cv_pipeline.pdf_candidates(include_failed=True), [self.job])   # CLI --missing still can
            self.runner = fake_runner()
            self.assertEqual(cv_pipeline.request_pdf(self.job), 'pending')     # dashboard "Retry PDF"
            run(worker.process_pdfs())
        row = core.get_cv_job(self.job)
        self.assertEqual((row['status'], row['pdf_status'], row['pdf_error']), ('ready', 'ready', None))
        self.assertEqual(len(provider.prompts), 1)                             # Gemini was called exactly once
        self.assertEqual((self.cvs / f'job-{self.job}.html').read_text(encoding='utf-8'), html_before)

    def test_valid_pdf_is_not_rendered_again(self):
        cv_pipeline.enqueue(self.job)
        self.claim_and_process(FakeProvider(good_reply))                      # renders the PDF once, inline
        self.assertEqual(core.get_cv_job(self.job)['pdf_status'], 'ready')
        mtime = self.pdf_file().stat().st_mtime_ns
        self.assertEqual(cv_pipeline.make_pdf(self.job), ('ready', 'already current (kept)'))
        self.assertEqual(cv_pipeline.pdf_candidates(), [])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.pdf_file().stat().st_mtime_ns, mtime)
        self.assertEqual(cv_pipeline.make_pdf(self.job, force=True)[0], 'ready')   # --force re-renders
        self.assertEqual(len(self.calls), 2)

    def test_existing_html_cvs_get_pdfs_without_gemini(self):
        """Backfill for CVs made before this feature (no pdf_status yet), e.g. job 7 in production."""
        self.cvs.mkdir(parents=True, exist_ok=True)
        (self.cvs / f'job-{self.job}.html').write_text(
            MASTER.replace('</head>', f'<meta name="{cv_pipeline.MARKER}" content="job-{self.job}"></head>'), encoding='utf-8')
        core.set_cv_job(self.job, 'ready', source='gemini')
        no_html = self.add_job()
        core.set_cv_job(no_html, 'ready')                                     # row without a file: skipped
        self.assertEqual(cv_pipeline.pdf_candidates(), [self.job])
        worker = cv_pipeline.CVWorker(lambda: FakeProvider())                 # a provider that must not be used
        run(worker.process_pdfs())
        self.assertTrue(self.pdf_file().is_file())
        self.assertEqual(cv_pipeline.make_pdf(no_html), ('failed', 'HTML CV missing'))

    def test_regenerated_html_makes_the_old_pdf_stale(self):
        cv_pipeline.enqueue(self.job)
        self.claim_and_process(FakeProvider(good_reply))
        first = self.pdf_file().read_bytes()
        cv_pipeline.enqueue(self.job, 'manual', force=True)
        self.claim_and_process(FakeProvider(lambda p: {**good_reply(p), 'body_html': tailored(
            body_of(p), 'Python engineer with 5 years of RAG systems work.')}))
        self.assertEqual(len(self.calls), 2)                                  # a new HTML gets a new PDF
        self.assertTrue(cv_pipeline.pdf_current(self.job))
        self.assertEqual(self.pdf_file().read_bytes(), first)                 # (same fake bytes, new sidecar)
        html = self.cvs / f'job-{self.job}.html'
        html.write_text(html.read_text(encoding='utf-8') + '<!-- new version -->', encoding='utf-8')
        self.assertFalse(cv_pipeline.pdf_current(self.job))
        self.assertEqual(cv_pipeline.pdf_candidates(), [self.job])

    def test_pdf_step_never_changes_the_cv_status(self):
        cv_pipeline.enqueue(self.job)
        self.claim_and_process(FakeProvider(good_reply))
        cv_pipeline.enqueue(self.job, 'manual', force=True)                   # regeneration requested meanwhile
        self.assertEqual(cv_pipeline.make_pdf(self.job, force=True)[0], 'ready')
        self.assertEqual(core.get_cv_job(self.job)['status'], 'pending')      # still queued, not reset to ready

    def test_automatic_pdfs_can_be_switched_off(self):
        cv_pipeline.enqueue(self.job)
        with mock.patch.dict(os.environ, {'PDF_AUTO_GENERATE': 'false'}):
            self.claim_and_process(FakeProvider(good_reply))
            self.assertEqual(run(cv_pipeline.CVWorker(lambda: FakeProvider()).process_pdfs()), 0)
        self.assertFalse(self.pdf_file().exists())
        self.assertEqual(cv_pipeline.make_pdf(self.job)[0], 'ready')          # manual conversion still works


if __name__ == '__main__':
    unittest.main()
