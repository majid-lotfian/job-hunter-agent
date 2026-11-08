# PR #15 deployment, dry runs for JetBrains #38 and Adyen #15, per-job live allowlist and Telegram review

- **PR:** #new (renamed after creation)
- **Branch:** `feature/live-review-allowlist` (from `main` 6868ae2)
- **Date:** 2026-11-08 12:07 (Europe/Amsterdam) · **Author:** Claude Code (Opus 5.5)
- **Status:** open: not merged, not deployed. **No application was submitted in this task.** `APPLY_MODE=off`.

> Legend: **Implemented** = code in this PR · **Unit-tested** = automated tests · **Live-verified** = observed on
> real systems · **Assumed/Proposed** = not verified.

## Owner decisions summary (read first)

1. **PR #15 deployed:** prod `6868ae2`, DB backup `data/deploy-backup-pr15-20261108/`, `job-hunter` restarted only. The full suite passes; one known flaky test failed once and is fixed here. 28 real-browser tests OK.
2. **JetBrains #38:**
   - the live dry run passed: 9/9 fields filled; after Greenhouse's resume autofill ran, **both name fields stayed filled**, and every field was re-verified before the (unclicked) Submit;
   - **submission is on hold:** I cannot prove that the Nov 6 attempt reached nobody (no network log of that click). **Please check your inbox for a JetBrains/Greenhouse "thank you for applying" email from 2026-11-06 around 17:28.**
3. **Second job: Adyen #15**, "Senior Machine Learning Engineer – AML Tech". Score 89, Greenhouse, English, Amsterdam, no visible CAPTCHA.
   - The tailored CV was generated today; it is factual and was checked automatically and by reading it.
   - The live dry run passed for every field your profile covers.
   - **It needs your 2 answers** (§8).
4. **This PR** adds what Phase 4 needs:
   - **`APPLY_LIVE_JOB_IDS`:** live submission only for the listed job;
   - **Telegram review:** your CV PDF plus the exact values and an "Approve & Submit (job N)" button bound to the current job and CV version;
   - **re-approval gate:** a job with an unclear previous outcome is offered again only after you confirm it was not received.
5. **Decisions for you:**
   - merge and deploy this PR;
   - the #38 email check;
   - Adyen's 2 answers.

   Then I send both Telegram reviews, and each submission happens only after your button press.

## 1. Task objective

The task had five phases:
1. deploy and verify PR #15;
2. re-test JetBrains #38 (previous attempt, vacancy, CV, a fresh dry run, names kept after autofill);
3. a second suitable job, from a different employer, with a factual CV and a full dry run;
4. two real, employer-confirmed submissions, each after a fresh Telegram approval, with live mode only for that job and `APPLY_MODE=off` afterwards;
5. a report.

## 2. Initial problems identified

- **No way to scope live mode to one job.** With `APPLY_MODE=live`, any approved job, including a press on an older Telegram button, would be submitted.
- **Telegram could not send the CV** for review (text messages only).
- **Job #38 offers no actions:** its finished application (`manual_action_required`) blocks a second approval by design, so there is no automatic retry.
- **Flaky test:** a random test job id that is all digits looks like a real Greenhouse id (about 2% of runs; failed once during the deploy).
- **Telegram polling** sometimes receives HTTP 429 (Too Many Requests) and recovers. If the bot token is also polled elsewhere, that would explain it.

## 3. Architecture and implementation decisions

- **`APPLY_LIVE_JOB_IDS`** (comma list, `*` = all):
  - `process()` downgrades live mode to preparation for any job not listed;
  - `workflow.supervised(job_id)` and `labels(job_id)` make Telegram and the dashboard say "Prepare" for those jobs;
  - an empty list means nothing is submitted.
- **Order for each real submission:**
  1. all checks pass;
  2. `APPLY_MODE=live`, `APPLY_LIVE_ADAPTERS=browser`, `APPLY_LIVE_JOB_IDS=<that job>`, restart;
  3. send the Telegram review;
  4. **nothing happens until you press "Approve & Submit"**;
  5. the agent's worker submits once;
  6. the result goes to SQLite and Telegram;
  7. `APPLY_MODE=off` again.

  While live, every other job can only be prepared.
- **`python -m app.applications.review`:**
  - read-only review: vacancy, form, verified profile, PDF, the exact values;
  - `--send` delivers the PDF (`sendDocument`) and the details with the bound button;
  - the button uses the existing `wf:` callback and token (job version + CV version): a changed job or CV makes it stale, and each press creates a new approval row;
  - `--reapprove-note` records your verification, then allows one new approval.
- **Confirmation evidence:** Greenhouse's `…/confirmation` URL is recognised in addition to confirmation texts.

## 4. Files added or modified

| File | Change |
|---|---|
| `app/applications/__init__.py` | `live_jobs` setting, `live_allowed()`, live mode scoped per job in `process()` |
| `app/workflow.py` | `supervised(job_id)`, `labels(job_id)`; call sites per job |
| `app/applications/review.py` (new) | Review / `--send` / `--reapprove-note` CLI |
| `app/core.py` | `send_telegram_document()` |
| `app/applications/browser.py` | Greenhouse confirmation URL rule |
| `tests/test_review.py` (new) | 8 tests: allowlist, labels, re-approval gate, Telegram review (PDF, values, bound button, digest not repeated) |
| `tests/test_workflow.py`, `test_autonomous.py`, `test_browser_worker.py`, `test_supervised.py` | Live tests opt in with `APPLY_LIVE_JOB_IDS='*'`; test job ids never all digits |
| `docs/agent-reports/…`, `README.md` | This report and index (#15 status updated to deployed) |

## 5. Features implemented

All rows of §4 are implemented and unit-tested. The Telegram send is **not live-tested** (it needs this PR deployed).

## 6. Configuration changes

- **New optional `.env` key:** `APPLY_LIVE_JOB_IDS` (empty by default, which means nothing is submitted live).
- **Production `.env`:** unchanged in this task (`APPLY_MODE=off`, `BROWSER_WORKER=local`).

## 7. Tests executed and results

- **Production after the PR #15 deploy:**
  - full suite: 301 tests, 1 failure (the flaky test), then OK twice (16 skipped);
  - real-browser tests: `RUN_BROWSER_TESTS=1 … tests.test_browser_worker` gives 28 OK.
- **This branch:** full suite **309 OK (16 skipped), 3 consecutive runs**; `tests.test_review` 8 OK.

## 8. Live verification results (tested vs. assumed)

**Service:**
- `job-hunter` is active; `hotel-bot` is active and untouched;
- schema version 4;
- the browser worker is `local`, with Playwright 1.63 and Chromium 1243;
- Cloud Run is not used.

**JetBrains #38** (`job-boards.eu.greenhouse.io/jetbrains/jobs/4937092101`):
- **Previous attempt:** application 1, `manual_action_required`, last_error "NOT SUBMITTED (evidence review)…". The screenshot hash matches the recorded one (`53630202…`).
- **Evidence for "not received":** after Submit, the form stayed on the same URL with client-side "required" errors and no confirmation. **Not proven:** there is no network capture of that click. The applicant confirmation email is the authoritative check.
- **Now:**
  - the vacancy is open (Greenhouse board API);
  - the job version is unchanged;
  - CV version `c8f2374f42ae`, PDF current;
  - profile verified;
  - 0 missing, 0 open questions.
- **Dry run** (11:42, through the deployed `browser.submit`, `submit=false`):
  - `filled`, 9/9;
  - `verified_before_submit: true`, nothing re-filled or cleared;
  - the screenshot shows names, email, phone, CV, LinkedIn, website, "Other" and "Amsterdam, Netherlands", with the optional consent unticked;
  - the "Autofill my application" button is highlighted (autofill ran) and the names are still present;
  - evidence: `data/applications/app-0-job-38-20261108-114222/`.

**Second-job selection** (open jobs with score ≥ 70 on a supported form; Nebius excluded because of its interactive Turnstile):

| Job | Employer · role | Score | Form | Blockers |
|---|---|---|---|---|
| **15** | **Adyen · Senior MLE – AML Tech** | 89 | Greenhouse | 2 owner answers |
| 80 | Eraneos · (Senior) AI Engineer | 95 | Recruitee (invisible hCaptcha) | Dutch posting; required "Spreek je vloeiend Nederlands?" ("Do you speak fluent Dutch?"); Dutch page labels |
| 8 | Databricks · AI Forward Deployed Eng. | 80 | Greenhouse | 3 answers (US-sanctions declarations, prior employment) + a required "Preferred First Name" |
| 41 | GitLab · Forward Deployed Eng. | 75 | Greenhouse | 3 answers (country, employment restrictions, UK) |

**Adyen #15** (`job-boards.greenhouse.io/adyen/jobs/8288674`):
- **CV:** generated 12:05 after Gemini overload retries; review checks found nothing, complete; PDF current, 2 pages. I read it: the same facts, dates and metrics as the master CV, reordered toward production ML. Nothing was added.
- **Dry run** (12:06, `submit=false`, unanswered questions left out):
  - `filled`: CV, names, email, phone, website (portfolio), LinkedIn, all re-verified before the (unclicked) Submit;
  - the optional demographic survey and accessibility text were left empty;
  - evidence: `data/applications/app-0-job-15-20261108-120640/`.
- **Answers needed from you** (exact wording):
  1. **"Point of Data Transfer"** (required, one checkbox: "Acknowledge/Confirm"). It acknowledges that Adyen holds and uses your application data, generally for one year, per its Applicant Privacy Notice. This is your decision; the agent never ticks consent on its own.
  2. **"This position is based in Amsterdam and we work 3 days per week from the office. Are you currently located in the Netherlands, or if not, are you open to relocate and work hybrid?"** Options: Yes / No.

**Not verified:**
- the Telegram document send (needs this PR deployed);
- any real submission;
- the submit-time reCAPTCHA outcome.

## 9. Performance and resource considerations

- A dry run takes about 25 s on the e2-medium.
- CV generation depends on Gemini availability: today 5×503 and one wait cycle, then success (12:05).

## 10. Security and privacy checks

- **Private data stays on the VM:** evidence folders are under `data/applications/` (gitignored, 700/600).
- **The report is sanitized.**
- **Telegram review content:** it sends your own data and CV to your own configured chat only.
- **Live scope:** submission is limited to one job id at a time, and a fresh approval is needed for each.

## 11. Known limitations

- Recruitee pages show Dutch labels, which the label-based filler cannot match yet. Eraneos and Conclusion stay manual.
- The submit-time reCAPTCHA outcome remains unknown until a real attempt.
- Telegram 429s on polling. Check whether the bot token is used by another process.

## 12. Outstanding issues / decisions needed

1. Approve merging and deploying this PR.
2. **#38:** confirm that no JetBrains/Greenhouse application confirmation email arrived for 2026-11-06 around 17:28. Then I run `--reapprove-note` with your statement.
3. **#15:** your answers to the 2 questions in §8.
4. Then the two Telegram reviews: press "Approve & Submit" for each one you want sent.

## 13. Deployment instructions (require approval)

1. **Deploy:**
   - back up the DB;
   - `git stash push app/main.py deploy/job-hunter.service search.yaml`, `git merge --ff-only origin/main`, `git stash pop`;
   - run the full and real-browser tests;
   - `sudo systemctl restart job-hunter`.
2. **For each job N, one at a time:**
   - `python -m app.applications.review --job N` gives READY;
   - set `APPLY_MODE=live`, `APPLY_LIVE_ADAPTERS=browser`, `APPLY_LIVE_JOB_IDS=N`, restart;
   - `python -m app.applications.review --job N --send`;
   - wait for your press;
   - the worker submits once;
   - check the result and evidence (`data/applications/app-<id>-job-N-*`);
   - set `APPLY_MODE=off`, empty `APPLY_LIVE_JOB_IDS`, restart.

## 14. Rollback instructions

- **Code:** revert the merge.
- **Stop live submission immediately:** set `APPLY_MODE=off` or an empty `APPLY_LIVE_JOB_IDS`.

## 15. Recommended next steps

1. Deploy.
2. Get your answers.
3. Send the two reviews.
4. Do the two approved submissions.
5. Add Dutch-label support for Recruitee forms (Eraneos, Conclusion).

## 16. PR URL and commits

See the PR (URL added after creation).
