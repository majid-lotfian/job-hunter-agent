# Automatic A4 PDF for every tailored CV (and PR #8 deployment record)

- **PR:** #9 - https://github.com/majid-lotfian/job-hunter-agent/pull/9
- **Branch:** `feature/cv-pdf` · **Commits:** `c9ac81e` (code + tests), report commit on top
- **Date:** 2026-11-06 12:50 (Europe/Amsterdam) · **Author:** Claude Code (Opus 5.5)
- **Status:** open: not merged and not deployed. Production runs `main` at `656f01d` (PR #8).

> Legend: **Implemented** = code in this PR · **Unit-tested** = covered by automated tests ·
> **Live-verified** = observed against real systems (state where) · **Assumed/Proposed** = not verified.

## Owner decisions summary (read first)

1. **Approve PR #9.** Deploying it adds one Python dependency, `weasyprint` (~100 MB installed, no service). Steps are in §13. After the restart, job 7 (and jobs 1 and 2) get their PDFs automatically within one worker batch.
2. **Accept WeasyPrint instead of headless Chrome.** Chrome was tested first on this VM and is not suitable: 50–65 s and about 1 GB of combined process memory per render, and it often exited without writing a file (§3).
3. **First real application (from the earlier task):** the best match uses hCaptcha on its form, so it cannot be submitted automatically under your rules (§17). A browser-assisted, manual-submit route is the realistic next step.

## 1. Task objective

Create a PDF for every tailored HTML CV automatically:
- A4, keeping the CV's own styling, with no browser headers or footers;
- no Gemini rerun; failures recorded and retried independently;
- backfill for existing CVs, starting with job 7;
- applications attach only a valid PDF;
- the PDF can be downloaded from the authenticated dashboard;
- strict resource limits on a VM with 1 GB RAM and 2 GB swap.

## 2. Initial problems identified

- No PDF existed anywhere. `RecruiteeAdapter.prepare()` reported "no PDF renderer is configured", and `applications._cv_bundle()` would have accepted any file named `job-<id>.pdf`, even if stale or broken.
- **Headless Chrome on this VM** (Chrome 154, run as `--headless=new` plus the usual offline flags):
  - **Reliability:** without `--virtual-time-budget` it exited with code 0 but no PDF in 3 of 3 runs. With it, it worked 3 of 3 times while the VM was quiet, then hung or wrote nothing again once memory pressure rose (load average 7.6; `/proc/pressure/memory` avg60 above 50 %).
  - **Successful renders:** 53–64 s, with a summed RSS across Chrome's processes of about **1.06 GB** (the sum double-counts shared pages, but it still pushed the VM into swap).
- **Test isolation bug:** `tests/test_discovery.py::test_cv_backfill_skips_closed_jobs` read the real `data/cvs/`, so it failed only in the production checkout (job-1/job-2 CVs exist there). This happened during the PR #8 deployment and is fixed here.

## 3. Architecture and implementation decisions

- **Renderer: WeasyPrint 70** (HTML/CSS to PDF on the system Pango library, which was already installed: `libpango-1.0-0` 1.50).
  - **Measured on the job-7 CV:** 2 A4 pages, about 21 KB.
  - **In-process render:** 4.0 s, 54 MB peak.
  - **Via the child process (as deployed):** 10–17 s, 55 MB peak.
  - **Fidelity:** it honours the CV's `@page` and `@media print` rules and its flexbox date alignment. The output was checked visually (§8).
  - **No header or footer:** WeasyPrint adds none (they are a Chrome feature).
- **Child process:** every render runs as `python -m app.pdf --render IN OUT`.
  - The service never keeps WeasyPrint's memory, and a hang can be killed.
  - Limits: `PDF_TIMEOUT_SECONDS` (default 120) and an address-space cap `PDF_MAX_MEMORY_MB` (default 1024).
- **One renderer at a time:** a `flock` on `data/.pdf-render.lock`, shared by the service and the CLI.
- **Offline rendering:** WeasyPrint's `URLFetcher(allowed_protocols={'data'})` blocks http(s) and local `file:` fetches. The CV is self-contained (inline `<style>`).
- **Validation before a PDF is accepted:**
  - `%PDF-` header and `%%EOF` trailer;
  - size between 1 KB and 10 MB;
  - 1 to `PDF_MAX_PAGES` (4) pages;
  - A4 page size (±5 mm);
  - at least 200 characters of laid-out text (not blank);
  - the HTML did not change during rendering.
- **Atomic and traceable:**
  - The PDF is written to a temporary file in the same folder, then moved into place with `os.replace`, so a failure never touches an existing PDF.
  - A sidecar `job-<id>.pdf.sha256` stores the hash of the source HTML.
  - `pdf_is_current()` is the single test of whether a PDF exists, is valid and matches the current HTML byte-for-byte.
- **Pipeline:**
  - Right after a successful HTML CV, `process()` renders the PDF in a worker thread, **before** `_cv_finished` hands an approved application over. This avoids a race found by the tests: with only a later batch step, an "Approve CV & Apply" application could reach the application worker before its PDF existed.
  - `CVWorker.process_pdfs()` (once per batch) handles retries and backfill.
  - PDF state is stored in additive `cv_jobs` columns: `pdf_status`, `pdf_error`, `pdf_attempts`, `pdf_next_attempt_at`, `pdf_info`.
  - Outcomes are logged to `events` as `cv_pdf_ready` / `cv_pdf_failed`.
  - PDF updates touch only the `pdf_*` columns, never the CV status, so a regeneration requested meanwhile is not lost.
  - Gemini is never called for a PDF.
- **Retries:** after a failure the next attempt waits `PDF_RETRY_BASE_SECONDS·2ⁿ⁻¹` (300 s, then 600 s). Automatic attempts stop after `PDF_MAX_ATTEMPTS` (3). After that a retry needs the dashboard **Retry PDF** button or `python -m app.pdf --job-id N` / `--missing`.
- **No unnecessary overwrite:** a valid PDF of the current HTML is kept (`make_pdf` returns "already current (kept)"). `--force` re-renders.
- **Applications:**
  - `_cv_bundle()` returns the PDF only when `pdf_is_current()` is true.
  - `process()` stops with `manual_action_required` ("CV PDF is missing, invalid or outdated - nothing was submitted"), records `missing: ['CV as PDF']`, asks for re-approval, and requests a new PDF.
  - The HTML CV stays available for review.

## 4. Files added or modified

| File | Change |
|---|---|
| `app/pdf.py` (new) | Renderer child, validation, lock, atomic write, sidecar, `pdf_is_current`, CLI |
| `app/cv_pipeline.py` | Inline PDF after a successful CV, `process_pdfs()` batch step, `make_pdf`, `request_pdf`, `pdf_candidates`, `_set_pdf`, PDF fields in `cv_state` |
| `app/core.py` | Additive `cv_jobs.pdf_*` columns and allowed fields |
| `app/applications/__init__.py` | Attachment selection (current PDF only) and a missing/invalid-PDF gate before any submission |
| `app/applications/adapters.py` | Message text for a missing PDF |
| `app/main.py` | `GET /api/jobs/{id}/cv.pdf` (same login as the HTML CV), `POST /api/jobs/{id}/cv/pdf` (retry), dashboard **PDF ⤓** / **Make PDF** / **Retry PDF**. The VM-local hunk (lines 1–3) is untouched. |
| `requirements.txt` | `weasyprint>=70,<71` |
| `.env.example`, `README.md` | `PDF_*` settings and documentation |
| `tests/test_pdf.py` (new), `tests/test_workflow.py`, `tests/test_cv_pipeline.py`, `tests/test_discovery.py` | New tests, shared fake renderer, isolation fix |

## 5. Features implemented

All **implemented and unit-tested**:
- automatic PDF after each CV;
- independent retries with backoff, and a manual retry;
- backfill of existing CVs;
- no re-render of a current PDF;
- stale-PDF detection;
- PDF-only attachment and the submission gate;
- authenticated download and retry endpoints;
- the CLI;
- renderer limits (timeout, memory cap, lock, offline).

## 6. Configuration changes

All optional (defaults shown). None are required in `.env`.

| Variable | Default | Meaning |
|---|---|---|
| `PDF_AUTO_GENERATE` | true | Render PDFs automatically (manual CLI still works when false) |
| `PDF_MAX_ATTEMPTS` | 3 | Automatic attempts per HTML version |
| `PDF_RETRY_BASE_SECONDS` | 300 | Backoff base (×2 per attempt) |
| `PDF_TIMEOUT_SECONDS` | 120 | Renderer child killed after this |
| `PDF_MAX_MEMORY_MB` | 1024 | Address-space cap for the renderer child |
| `PDF_MAX_PAGES` | 4 | Upper page limit for validation |

## 7. Tests executed and results

The full suite was run in two environments, with outbound network blocked by `HTTPS_PROXY=http://127.0.0.1:9`:

| Environment | Result |
|---|---|
| Test venv with WeasyPrint 70 (scratch venv; project requirements + weasyprint) | **237 tests, OK** |
| Production `.venv` (no WeasyPrint yet) | **237 tests, OK (2 skipped**: the real-render tests) |

New tests (13 in `tests/test_pdf.py`, 3 in `tests/test_workflow.py`):

| Requirement | Tests |
|---|---|
| Successful conversion (real WeasyPrint: A4, 1 page, text, sidecar, HTML unchanged) | `test_real_render_is_a_valid_a4_pdf_with_text` |
| Offline rendering (http/https/file blocked, data: allowed) | `test_external_resources_are_not_fetched` |
| Missing HTML | `test_missing_html`; `make_pdf` → `('failed', 'HTML CV missing')` |
| Conversion failure (crash, garbage, truncated, blank, too long, US Letter, no output); old PDF never replaced; no temp files left | `test_failures_never_replace_an_existing_pdf` |
| Timeout | `test_timeout_is_reported` |
| Timeout and memory cap passed to the child | `test_child_gets_timeout_and_memory_cap` |
| Automatic PDF after a CV | `test_successful_cv_gets_a_pdf_automatically` |
| Retry behaviour (failure recorded in `cv_jobs` + events, backoff, attempt limit, manual retry, Gemini called once, HTML untouched) | `test_pdf_failure_is_recorded_and_retried_without_gemini` |
| No unnecessary overwrite | `test_valid_pdf_is_not_rendered_again` |
| Existing CVs without Gemini (job-7 case) | `test_existing_html_cvs_get_pdfs_without_gemini` |
| New HTML makes the old PDF stale and gets a new PDF | `test_regenerated_html_makes_the_old_pdf_stale` |
| PDF step never changes the CV status | `test_pdf_step_never_changes_the_cv_status` |
| Switch-off setting | `test_automatic_pdfs_can_be_switched_off` |
| Attachment selection (current PDF used, HTML kept) | `test_attachment_is_the_current_pdf_and_html_stays_available` |
| Missing / invalid / stale PDF blocks submission (adapter never called) | `test_missing_or_invalid_pdf_blocks_submission` |
| Dashboard download with the existing auth (redirect/401 without login, 200 PDF attachment with login, 404 when missing, retry 409/pending) | `test_dashboard_pdf_download_uses_existing_auth` |

## 8. Live verification results (tested vs. assumed)

**Live-verified on the VM, without deploying.** The branch code rendered the real production `data/cvs/job-7.html` into a temporary directory three times. Production `data/cvs/` and the DB were not touched; there is still no PDF there.

| Run | Pages | Size | Text chars | Page size | Time | Renderer peak RSS | MemAvailable before → after |
|---|---|---|---|---|---|---|---|
| 1 | 2 | 21,129 B | 4,428 | 210 × 297 mm | 10.4 s | 55 MB | 332 → 335 MB |
| 2 | 2 | 21,129 B | 4,428 | 210 × 297 mm | 17.2 s | 55 MB | 330 → 324 MB |
| 3 | 2 | 21,128 B | 4,428 | 210 × 297 mm | 14.0 s | 55 MB | 333 → 307 MB |

- `pdf_is_current()` was true after each run.
- No renderer processes or temporary files were left behind.
- A visual check of the PDF matched the HTML layout: header block, section rules, right-aligned dates (flexbox), bullet lists and bold metrics, A4 margins from the CV's `@page`.
- The text layer is extractable, which matters for applicant tracking systems (ATS).

**Not yet verified (needs the deployment):**
- `data/cvs/job-7.pdf` in production;
- the DB `pdf_*` state;
- the dashboard download in the live service.

These happen automatically in the first CV worker batch after the restart (§13).

## 9. Performance and resource considerations

- **Per render:** about 10–17 s wall time on this VM (mostly child-process start-up and imports) and about 55 MB peak, released when the child exits. Compare Chrome: 53–64 s and about 1 GB combined, with frequent failures.
- **Cost:** one render per new CV version, serialized. With bulk auto-CV off, that is a handful per day.
- **Event loop:** never blocked; the render runs in `asyncio.to_thread`.
- **Disk:** WeasyPrint plus dependencies add about 97 MB to the venv (Pillow, fontTools, pydyf, tinycss2, cssselect2, Pyphen, tinyhtml5, cffi). Each PDF is about 21 KB.

## 10. Security and privacy checks

- Rendering is offline (no http/https/file fetches) and runs in a child process with memory and time limits.
- The PDF download requires the existing dashboard session (unauthenticated requests are redirected to the login page, or get 401 for API calls). It is sent with `Cache-Control: no-store` (the existing `NO_STORE` headers).
- The diff was scanned for keys, tokens and personal data; none were found.
- The report contains no CV content, contact data or DB rows (aggregates only).
- The scratch copies of the job-7 PDF are in the session's temporary directory only.

## 11. Known limitations

- **Real-render tests** are skipped where WeasyPrint is not installed. After deployment the production venv runs them.
- **WeasyPrint is not a browser.** CSS features this CV does not use (complex grid layouts, JavaScript) could render differently. Validation catches blank or oversized output, but not subtle layout differences. Have a quick look at the first PDF.
- **Page count** is reported by the renderer. The parent checks the file structure but does not parse page objects (WeasyPrint compresses them).
- **The memory cap** is an address-space limit (`RLIMIT_AS`), not a cgroup limit.

## 12. Outstanding issues / decisions needed

1. **Approve PR #9** and its deployment (§13).
2. **CV volume:** bulk auto-CV is still off (`CV_AUTO_GENERATE=false`). Job 7's single Gemini call took about 20 s and succeeded first time. Recommendation: `CV_DAILY_LIMIT=10` with `CV_AUTO_GENERATE=true` once you are happy with the first PDFs. That is roughly 10–20 Gemini requests and 10 PDF renders a day, well within the free tier and the VM.
3. **Telegram token rotation** (from PR #8): still pending, at your choice of time.
4. **First application:** see §17.

## 13. Deployment instructions (not executed, require approval)

Production checkout: `/home/majid_lotfian_work/job-hunter-agent`. Only `job-hunter.service` is affected; **`hotel-bot.service` is not touched.**

1. Merge PR #9. Then `git fetch` and check that `origin/main` contains `c9ac81e`.
2. **Back up the DB** (same method as PR #8):
   ```
   .venv/bin/python - <<'PY'
   import sqlite3
   src, dst = sqlite3.connect('data/jobs.sqlite3'), sqlite3.connect('data/deploy-backup-pr9.sqlite3')
   src.backup(dst)
   print(dst.execute('PRAGMA integrity_check').fetchone())
   PY
   ```
   Also copy `.env`, `search.yaml`, `deploy/job-hunter.service` and `app/main.py`.
3. Update the code:
   - `git stash push app/main.py deploy/job-hunter.service search.yaml`
   - `git merge --ff-only origin/main`
   - `git stash pop`

   Expected to be clean, because PR #9 does not touch the first lines of `app/main.py`.
4. `.venv/bin/pip install -r requirements.txt`. This installs WeasyPrint 70 and its pure-Python dependencies. Check with `.venv/bin/python -c "import weasyprint"`.
5. `.venv/bin/python -m unittest discover -s tests`. Expect 237 OK with 0 skipped.
6. Check `.env`: keep `APPLY_MODE=off` and `CV_AUTO_GENERATE=false` (no bulk CVs). No PDF setting is needed.
7. `sudo systemctl restart job-hunter` (only this unit).
8. **Verify:**
   - the log shows `PDF for job 7: 2 page(s), ~21000 bytes` (and the same for jobs 1 and 2) within one worker batch;
   - `data/cvs/job-7.pdf` and `job-7.pdf.sha256` exist;
   - `cv_jobs.pdf_status='ready'`;
   - `events` has `cv_pdf_ready`;
   - the dashboard shows **PDF ⤓** and the download works;
   - no Gemini request was made (`llm_usage` unchanged).

   Manual alternative: `.venv/bin/python -m app.pdf --job-id 7`.

## 14. Rollback instructions

1. `sudo systemctl stop job-hunter`
2. `git reset --hard 656f01d`, then re-apply the stashed VM edits.
3. `sudo systemctl start job-hunter`

The new `cv_jobs.pdf_*` columns are additive and ignored by older code. Generated PDFs can stay (older code ignores them) or be deleted. WeasyPrint can remain installed or be removed with `pip uninstall weasyprint`.

## 15. Recommended next steps

1. Deploy PR #9 and look at `job-7.pdf` in the dashboard.
2. Then turn auto-CV back on with a daily cap (§12).
3. Then prepare the browser-assisted first application (§17).

## 16. PR URL and commits

- PR: https://github.com/majid-lotfian/job-hunter-agent/pull/9
- `c9ac81e`: Generate an A4 PDF for every tailored CV and attach only current PDFs
- Report commit: this file and the index row

## 17. Context: PR #8 deployment and first-application findings (earlier task, report not committed before)

**PR #8 merge and deployment (2026-11-06, approved by the owner):**
- **Merge:** PR #8 merged as `656f01d`; `main` contains PRs #6, #7 and #8. Tests: 193 OK in a clean worktree.
- **Backup:** DB backup via the SQLite backup API (integrity ok; jobs 7/7, cv_jobs 3/3, events 3/3). `.env`, `search.yaml`, the service file and `app/main.py` were copied to a backup folder. The VM-local edits were preserved through stash, fast-forward and pop with no conflict.
- **Settings and tests:** `.env` got `CV_AUTO_GENERATE=false` and `APPLY_MODE=off`. Tests in the production checkout: 192 OK and 1 failure, caused by the isolation bug fixed in this PR (not by the code). The real CV files were not modified.
- **Restart:** only `job-hunter.service` was restarted. The migration ran to schema 4, with an automatic backup in `data/backups/`. `hotel-bot.service` was untouched.
- **Afterwards:**
  - discovery: 197 jobs, 75 sources `ok`;
  - dashboard login works (no session: 303/401; bad token: 401; good token: 303);
  - the token does not appear in new logs (0 matches);
  - 0 approvals and 0 applications.
- **Job 7:** migrated from `failed` to `waiting_retry`, then picked up immediately and generated on the first Gemini call. Result: `ready`, `gemini-3.6-flash`, 0 errors, 0 warnings, 0 numbers not in the master CV, length 99 % of the master, contact header restored exactly, `<head>` identical except the two marker metas. Telegram sent the Approve & Apply message; nothing was pressed.

**First application candidate:**
- **Job:** AI Solutions Engineer, Conclusion (job 73, score 80, Amsterdam; Recruitee posting `conclusion.recruitee.com/o/ai-solutions-engineer`).
- **Match:** Python, GenAI/RAG/agentic AI, LangChain/LangGraph, prompt engineering. Not in the master CV: Azure (OpenAI/AI Foundry/AI Search) and TypeScript. The posting is written in Dutch (`dutch_likely`).
- **Form** (public offer JSON): name, email, phone (required), CV file (required), optional cover letter and photo. No screening questions. The location question is required but hidden (single location).
- **API:** Recruitee's Careers Site API (`POST /api/offers/{slug}/candidates`) is documented as public. A success returns 201 with a candidate id, which would count as a genuine confirmation.
- **Blocker:** the careers page enables **invisible hCaptcha** on this form (`"captcha":"invisible"`, `hcaptcha: true` in the page config). Posting directly to the API would get around the bot protection the employer turned on, so under the owner's rules (no CAPTCHA or anti-bot bypass) automated submission is **not permitted** for this employer.
- **Feasible route:** browser-assisted. The agent prepares the PDF (this PR), the field values from a verified `data/applicant_profile.yaml` (does not exist yet) and the apply link. You submit in your own browser and solve the hCaptcha if it appears. The confirmation is then recorded with the existing "applied" action, which requires a confirmation reference.
- Not implemented yet; it needs your decisions on the profile file and the target job.
