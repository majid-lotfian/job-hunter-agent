# Gemini overload recovery (waiting_retry, shared cooldown), deployment readiness, first-application route

- **PR:** #8 - https://github.com/majid-lotfian/job-hunter-agent/pull/8
- **Branch:** `fix/gemini-transient-retry` · **Commits:** `54b1e46` (code + tests), report commit on top
- **Date:** 2026-11-06 09:51 (Europe/Amsterdam) · **Author:** Claude Code (Opus 5.5)
- **Status:** open: not merged and not deployed. **Tasks 3 and 4 (deploy, live CV verification) are on hold until you approve.**

> Legend: **Implemented** = code in this PR · **Unit-tested** = covered by automated tests ·
> **Live-verified** = observed against real systems (state where) · **Assumed/Proposed** = not verified.

## Owner decisions summary (read first)

1. **Approve deployment** of `main` (PRs #6 and #7) together with this fix (PR #8). The fix has to be merged first, because the deploy must not run unmerged code. Plan: §13.
2. **Temporary burst control for the first deploy.** Recommended: `CV_AUTO_GENERATE=false` until the one-job verification passes, then a cap (for example `CV_DAILY_LIMIT=10`) or no limit. See §12.
3. **Rotate the Telegram bot token (recommended).** It has appeared in clear text in the VM's systemd journal since at least 2026-11-05 (§10). This PR stops future leaks; old journal lines still contain it.
4. **No model change is needed.** The error was a temporary capacity problem (503) on `gemini-3.6-flash`; the key and model are valid (§8). A fallback model stays optional.
5. **First real application:** a Recruitee job, after the five items in §17.5 are approved.

## 1. Task objective

- Investigate the Telegram report "CV generation failed after 5 attempts because of high demand".
- Make temporary Gemini failures recoverable:
  - `waiting_retry` state and persistent `next_retry_at`;
  - shared cooldown;
  - no infinite fast loops;
  - restart-safe;
  - no duplicate CVs;
  - fewer Telegram messages.
- Deploy the merged PRs safely, verify one real CV, and prepare (not perform) one supervised application.

## 2. Initial problems identified (Task 1: investigation)

**Live-verified** from `journalctl -u job-hunter` and a read-only look at the production database (aggregates only):

| Item | Finding |
|---|---|
| Exact error | `HTTP 503 UNAVAILABLE: This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.` |
| HTTP status / type | **503, temporary model overload.** This is not 429 rate limiting and not quota exhaustion: no 429 or `RESOURCE_EXHAUSTED` appears anywhere in the logs. |
| Model | `gemini-3.6-flash` (`LLM_MODEL` in `.env`) |
| Affected job | **Job 7** (only one): five 503s, then `failed`. Job 1 had one 503 on 2026-11-04 23:53, retried after 60 s and succeeded. |
| Retry behaviour (old code) | Retries for job 7 came after 60 s, 240 s, 960 s and 3840 s (`60·4ⁿ`). Attempts were at 16:53:47, 16:55:00, 17:00:00, 17:16:09 and 18:20:22 CET on 2026-11-05. The job was then permanently `failed` after 87 minutes, with no further automatic retry. |
| Root cause | The old pipeline treated "5 temporary errors" as a permanent failure, even though the provider said the problem was temporary. Its per-job backoff also had no jitter and no model-wide cooldown. |
| Code running on the VM | **Old code.** The service was started on 2026-11-04 23:53 CET from checkout `ea98faf` (PR #5). `main` is at `319715d` (PRs #6 and #7). The production DB has `user_version 0`, 7 jobs and 3 `cv_jobs` rows (2 ready, 1 failed). |
| Other findings | (a) The **Telegram bot token is printed in the journal**: httpx `HTTPStatusError` tracebacks for Telegram 502s include the full `api.telegram.org/bot<token>/getUpdates` URL (2026-11-05 02:12, 2026-11-06 02:12 and 02:49). (b) `lever/jobgether` fetch errors are intermittent (unrelated to this task). |

## 3. Architecture and implementation decisions

- **Error kinds** (`app/llm.py`): `TemporaryLLMError.kind` is one of the following.
  - `overload`: 500, 502, 503 or 504.
  - `rate_limit`: 429.
  - `quota_daily`: a 429 whose `QuotaFailure.quotaId` contains `PerDay`.
  - `timeout`, `network`, `malformed` or `empty`.

  `retry_after` comes from the `Retry-After` header (seconds or HTTP date) or from Gemini's `google.rpc.RetryInfo.retryDelay`. Errors built without a kind (older callers or tests) get one inferred from the message.
- **Two-level retry** (`app/cv_pipeline.py`):
  1. Within a cycle, the delay is `CV_RETRY_BASE_SECONDS·2ⁿ⁻¹` with 50–100 % jitter, capped at `CV_RETRY_MAX_SECONDS`, or the Retry-After value if that is longer. With the defaults: about 15–30 s, 30–60 s, 60–120 s, 120–240 s.
  2. After `CV_MAX_ATTEMPTS` (5) "busy" failures (overload, rate_limit, quota_daily, timeout, network), the job goes to **`waiting_retry`** with `next_attempt_at` set **~10 min (8–12 min with ±20 % jitter)** later. The wait doubles each cycle (10 → 20 → 40 min …), capped at 6 h. When the job is due, a fresh cycle starts.
  3. A job **fails only after 72 h** (`CV_RETRY_GIVE_UP_HOURS`) of continuous temporary errors. That window is measured from `transient_since`, which persists across cycles and restarts.
- **Other error types:**
  - Malformed or empty replies keep the old bounded behaviour (`failed` after 5 attempts). They point to the request or content, not to a busy model, and retrying them forever would waste the budget.
  - Permanent errors fail immediately: invalid request (400), blocked prompt, failed validation.
  - **Configuration errors (401/403/404) halt the worker** until the service restarts or you press Retry in the dashboard. `.env` is only read at start, so retrying a bad key every 30 minutes (old behaviour) could never succeed. You get one Telegram alert.
- **Shared persistent cooldown:**
  - Any 429 or 5xx extends a model-wide cooldown, stored in the new table `llm_state` as a wall-clock time, so it survives restarts.
  - Consecutive hits double it (30 s → 60 s → … capped at 900 s, `LLM_OVERLOAD_COOLDOWN_MAX_SECONDS`) with jitter.
  - The floor is 120 s for a 429 and 1 h for a daily quota. Retry-After wins if it is longer.
  - The streak resets on the first success. `claim_next()` claims nothing while the cooldown is active, so no worker slot can hammer an overloaded model.
- **No duplicates:**
  - Claiming stays atomic (`BEGIN IMMEDIATE`, `UPDATE … WHERE status IN ('pending','waiting_retry')`).
  - `ready` and `needs_review` CVs are never regenerated automatically.
  - A tailored CV that appears while a job is waiting is kept, and no API call is made.
  - Automatic re-queueing never skips a cooldown. Only a manual "Retry now" does.
- **Telegram:** `waiting_retry` sends no message. A final failure is announced once in the existing failure digest, which now also says how many CVs are waiting to retry.
- **Daily request budget and concurrency** (`LLM_MAX_REQUESTS_PER_DAY`, `CV_WORKER_CONCURRENCY`, `LLM_MIN_INTERVAL_SECONDS`) are unchanged. Every retry still counts against the budget.
- **One-time migration:** rows that old code `failed` with `Gave up after N attempts:` plus a transient marker (HTTP 429/5xx/408, `UNAVAILABLE`, `RESOURCE_EXHAUSTED`, timeout, network) become `waiting_retry` and are due immediately. They keep their `notified_at`, so they are not re-announced. Validation and malformed failures stay `failed`.
- **Model fallback: not implemented.** The model was healthy before and after (job 1 succeeded on its first retry; `--check` passes today), so a fallback is unnecessary. The model is never switched automatically.
- **Log redaction** (`app/core.py`): a `RedactingFormatter` on the root handlers scrubs `api.telegram.org/bot<token>`, bare `bot<id>:<token>` strings and `key=` / `api_key=` / `app_key=` / `token=` query values. This covers tracebacks too.

## 4. Files added or modified

| File | Change |
|---|---|
| `app/llm.py` | Error kinds, Retry-After (HTTP-date) and RetryInfo parsing, per-day quota detection |
| `app/cv_pipeline.py` | `waiting_retry`, jittered backoff, cycle cooldown, give-up window, shared persistent cooldown, config-error halt, digest line, status/dashboard fields |
| `app/core.py` | `llm_state` table; `cv_jobs.retry_cycle`, `transient_since`, `last_error_kind` (additive); one-time transient-failure migration; log redaction |
| `app/workflow.py` | `waiting_retry` treated as "CV in progress" (Reject only; Reject cancels it; approval keeps the cooldown) |
| `app/main.py` | CV page text and "Retry now" button for `waiting_retry`. Lines 243/274 only: the VM-local hunk (lines 1–3) is untouched. |
| `app/discovery/engine.py` | Discovery digest label "CV retrying later" |
| `.env.example`, `README.md` | New settings and documentation |
| `tests/test_cv_pipeline.py`, `tests/test_workflow.py` | 14 new tests, 4 tests updated for the new semantics |

## 5. Features implemented

All **Implemented and unit-tested**:
- transient vs permanent classification;
- jittered exponential backoff with Retry-After;
- `waiting_retry` with persistent `next_attempt_at`;
- per-cycle cooldown growth;
- 72 h give-up;
- shared persistent cooldown;
- config-error halt;
- restart recovery;
- no-duplicate guarantees;
- migration of old transient failures;
- quiet Telegram while waiting;
- log redaction.

## 6. Configuration changes

New optional settings, with defaults. None are required in `.env`, and the VM's `.env` sets none of them.

| Variable | Default | Meaning |
|---|---|---|
| `CV_RETRY_BASE_SECONDS` | 30 (was 60) | Within-cycle base delay (×2ⁿ, jitter 50–100 %) |
| `CV_RETRY_MAX_SECONDS` | 900 (was 21600) | Within-cycle cap |
| `CV_WAIT_RETRY_BASE_SECONDS` | 600 | First `waiting_retry` cooldown (±20 %), doubling per cycle |
| `CV_WAIT_RETRY_MAX_SECONDS` | 21600 | Cooldown cap; also caps Retry-After |
| `CV_RETRY_GIVE_UP_HOURS` | 72 | Continuous transient errors before `failed` (0 = never) |
| `LLM_OVERLOAD_COOLDOWN_MAX_SECONDS` | 900 | Cap of the shared cooldown |

These are unchanged: `CV_MAX_ATTEMPTS=5`, `LLM_RATE_LIMIT_COOLDOWN_SECONDS=120`, `LLM_MAX_REQUESTS_PER_DAY=250`, `CV_WORKER_CONCURRENCY=1`, `LLM_MODEL`.

## 7. Tests executed and results

`/home/majid_lotfian_work/job-hunter-agent/.venv/bin/python -m unittest discover -s tests`: **193 tests, OK** (the baseline on `main` was 179 OK). The new tests cover these cases:

| Area | Test |
|---|---|
| Overload (real 503 body through `GeminiProvider` + MockTransport) | `test_real_503_is_overload_and_sets_shared_persistent_cooldown` |
| 5× overload → waiting_retry (5–15 min) → success, no duplicates, no Telegram | `test_overload_five_times_waits_then_completes_without_duplicates` |
| Rate limiting → waiting_retry instead of failed | `test_temporary_errors_back_off_then_wait_instead_of_failing` |
| Retry-After honoured | `test_retry_after_is_respected` |
| Error kinds, RetryInfo, per-day quota, HTTP-date Retry-After | `test_temporary_error_kinds_and_retry_hints` |
| Backoff bounds and jitter band | `test_backoff_is_bounded_with_jitter`, `test_cooldown_grows_per_cycle` |
| Shared cooldown doubling, cap, reset on success, quota and Retry-After floors | `test_shared_cooldown_doubles_with_streak_and_resets_on_success` |
| Recovery after restart (waiting state and crash while generating) | `test_waiting_retry_survives_restart` |
| Give-up after 72 h, announced once | `test_gives_up_after_configured_hours_and_announces_once` |
| Permanent 400 fails immediately (one attempt) | `test_permanent_errors_fail_immediately` |
| 401 halts: one API call, one alert, manual retry lifts it | `test_configuration_error_halts_without_burning_attempts_or_retrying` |
| Auto re-queue keeps cooldown; manual retry skips it | `test_auto_requeue_keeps_cooldown_but_manual_retry_skips_it` |
| CV that appeared while waiting is kept | `test_cv_that_appeared_while_waiting_is_kept` |
| Dashboard/workflow view of waiting_retry | `test_waiting_state_visible_in_dashboard_and_workflow` |
| Migration of old transient failures (idempotent; validation/malformed stay failed) | `test_old_transient_failures_become_waiting_retry_once` |
| Telegram token and key redaction (traceback included) | `test_telegram_token_and_keys_are_scrubbed_from_logs` |

The full test output contains no token-like strings (checked with grep).

## 8. Live verification results (tested vs. assumed)

**Live-verified (read-only, no generation, no production change):**
- **Service logs:** analysed as described in §2.
- **Gemini model check:** `.venv/bin/python -m app.cv_pipeline --check`, run from the production checkout (old code). It calls `models.get` and `list_models` only. Result: `models/gemini-3.6-flash` OK (version `3.6-flash-07-2026`, `generateContent` supported). The key is valid, and newer flash models (3.7, 3.8) are also listed.
- **Migration dry run on a copy of the production DB** (consistent SQLite backup into a temporary directory, never the live file), using this branch's code:
  - migration ran twice (idempotent); `integrity_check ok`; schema went from version 0 to 4;
  - all **7 jobs preserved**;
  - job 1 and job 2 still `ready`;
  - **job 7 → `waiting_retry`** (cycle 1, kind `overload`, due immediately, `notified_at` kept);
  - the automatic migration backup was created.

  The copy was deleted afterwards.
- **PDF rendering:** a synthetic sample HTML file (not your CV) printed to a 22 KB PDF with the installed `google-chrome` 154 in headless mode (§17.4).

**Not done (waiting for your approval):**
- **Task 3:** deployment, restart, and dashboard/worker/Telegram checks.
- **Task 4:** a real Gemini CV generation for job 7.

**Assumed:** the same 503 pattern will be handled by the new code. This is covered by unit tests with the exact production error body, but has not been observed live yet.

## 9. Performance and resource considerations

- A busy model now costs at most 5 requests per cycle per job. Cycles start at least about 8 minutes apart and double, so the worst case is roughly 5 × (number of cycles in 72 h ≈ 9) ≈ 45 requests per job over 3 days. In practice the shared cooldown blocks the other jobs at the same time.
- The daily cap of 250 requests still applies.
- The new `llm_state` table has 3 rows. Cooldown reads are one indexed primary-key lookup per claim.

## 10. Security and privacy checks

- **Telegram bot token exposure (pre-existing, found during the investigation):**
  - The token appeared in clear text in `journalctl -u job-hunter` (3 tracebacks).
  - The journal is local to the VM, readable by root and the `adm` / `systemd-journal` groups.
  - The fix redacts it in all future log lines.
  - **Recommended:** rotate the token with @BotFather (`/revoke`), update `TELEGRAM_BOT_TOKEN` in `.env`, and optionally vacuum old journal entries (`sudo journalctl --vacuum-time=1s` for all units, or let rotation age them out).
  - The token is not reproduced in this report or the PR.
- **Gemini key:** sent only in the `x-goog-api-key` header (unchanged), never in URLs. `--check` output was filtered for key patterns.
- **What this report contains:** no CV content, contact details or database rows, only aggregates and job IDs. The DB copy used for the migration test was deleted.
- **What the diff contains:** it was scanned for keys, tokens and e-mail addresses, and none were found.

## 11. Known limitations

- `next_attempt_at` is the column that holds "next retry at". The API and dashboard expose it as `next_retry_at`; the DB column was not renamed, to keep the migration additive.
- **Jobs that never got a quota slot:** a migrated or waiting job that never received one (old rows have `quota_day` NULL) still needs room in `CV_DAILY_LIMIT` to be claimed. With `CV_DAILY_LIMIT=0`, use `--job-id 7` (manual) to verify.
- **Halt after a configuration error:** it is per process. A restart retries once. This is intended, because a restart is how a corrected `.env` takes effect.
- **No fallback model.** If `gemini-3.6-flash` were overloaded for more than 72 h, jobs would fail. Raise `CV_RETRY_GIVE_UP_HOURS`, or decide on a fallback (§12).

## 12. Outstanding issues / decisions needed

1. **Merge PR #8 and approve the deployment in §13.**
2. **Burst control:**
   - After the deploy, the first discovery scan (74 sources) will add about 200 jobs, and with the defaults about 150 will be queued for automatic CVs (`CV_DAILY_LIMIT=-1`).
   - **Recommendation:** `CV_AUTO_GENERATE=false` during verification, then `CV_DAILY_LIMIT=10` for the first days.
   - The 40–69 tier sends at most `TELEGRAM_ACTIONS_PER_BATCH=8` individual Telegram messages per batch, plus one summary.
3. **Rotate the Telegram bot token** (§10).
4. **Optional fallback model:** for example `gemini-3.5-flash`, used only after N waiting cycles and reported in Telegram. Not recommended now, because the 503s were short-lived.
5. **First application:** the items in §17.5.
6. Still open from PR #7: Adzuna/Jooble keys, handling of jobs that require Dutch, and `lever/jobgether` intermittent fetch errors.

## 13. Deployment instructions (not executed, require approval)

Production checkout: `/home/majid_lotfian_work/job-hunter-agent`. Only `job-hunter.service` is affected; **`hotel-bot.service` is not touched.**

1. Merge PR #8. Then `git fetch` and confirm `origin/main` contains `54b1e46`.
2. **Back up the DB** (consistent online copy, then check it):
   ```
   .venv/bin/python - <<'PY'
   import sqlite3
   src, dst = sqlite3.connect('data/jobs.sqlite3'), sqlite3.connect('data/jobs.backup-20261106.sqlite3')
   src.backup(dst)
   print(dst.execute('PRAGMA integrity_check').fetchone(),
         [dst.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] == src.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
          for t in ('jobs', 'cv_jobs', 'events')])
   PY
   ```
   Expected output: `('ok',) [True, True, True]`.
3. Copy `.env`, `search.yaml`, `deploy/job-hunter.service` and `app/main.py` to a backup folder.
4. **Burst control:** set `CV_AUTO_GENERATE=false` in `.env`. `APPLY_MODE` stays unset/off (the default is `off`; verify after the restart with `grep APPLY_MODE .env`).
5. Update the code:
   - `git stash push app/main.py deploy/job-hunter.service search.yaml`
   - `git merge --ff-only origin/main`
   - `git stash pop`

   Expected to be clean: upstream changed `app/main.py`, but not its first lines, and the two other files did not change upstream. Stop if there is any conflict.
6. Run `.venv/bin/pip install -r requirements.txt` (no new dependencies expected), then `.venv/bin/python -m unittest discover -s tests`.
7. `sudo systemctl restart job-hunter` (only this unit).
8. **Verify:**
   - the migration log line and `PRAGMA user_version = 4`;
   - row counts still match the backup;
   - job 7 is in `waiting_retry`;
   - the dashboard loads;
   - the first discovery scan finishes (source health);
   - the CV worker logs;
   - the Telegram digest arrives;
   - `/api/cv-worker` shows `waiting_retry: 1`.
9. **Task 4 (one job):**
   - Job 7 is due immediately and `CV_AUTO_GENERATE=false` does not block it. Alternatively, run `.venv/bin/python -m app.cv_pipeline --job-id 7 --run-once`.
   - Check: `ready` or `needs_review`, `data/cvs/job-7.html` exists and has the marker, validation warnings, the DB row, and the Telegram Approve & Apply message.
   - Do not press Approve.

## 14. Rollback instructions

1. `sudo systemctl stop job-hunter`
2. `git reset --hard ea98faf`, then re-apply the stashed VM edits from the backup folder.
3. Restore `data/jobs.sqlite3` from the backup in step 2 of §13.
4. `sudo systemctl start job-hunter`

The schema changes are additive, so an older version of the code can also run on the migrated DB without restoring it.

## 15. Recommended next steps

1. Approve §13 and verify one CV (Task 4).
2. Rotate the bot token.
3. Then choose the CV volume.
4. Then prepare the supervised Recruitee application (§17).

## 16. PR URL and commits

- PR: https://github.com/majid-lotfian/job-hunter-agent/pull/8
- `54b1e46`: Recover from temporary Gemini overload instead of failing CVs
- Report commit: this file and the index row

## 17. CV-specific results and readiness for one supervised application (Task 5)

### 17.1 CV generation results, API failures and retries

- **Historic results (old code):** 3 jobs have CV rows.
  - Job 1 is ready (one 503, recovered).
  - Job 2 is ready.
  - Job 7 failed after 5× 503. The migration re-queues it (§8).
- **New generation in this task:** none (Task 4 is waiting for your approval).
- **Cost and quota:** free tier. Retries count against `LLM_MAX_REQUESTS_PER_DAY=250`. No 429 or quota error has ever been observed.

### 17.2 Best first candidate

**Recruitee.** It is the only system in the framework whose job form requirements are readable without the employer's key (`GET https://{company}.recruitee.com/api/offers/{slug}`). The existing `RecruiteeAdapter.prepare()` already checks every required field against verified data.

The current sources include 2 Recruitee boards (Conclusion, Centric). Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Personio and Teamtailor apply APIs all require the employer's API key (PR #7 research), so they stay manual.

### 17.3 Feasibility

- **Technical:** probably feasible. Recruitee documents a Careers Site API endpoint `POST /api/offers/{slug}/candidates` on the company's careers subdomain. One third-party write-up says it requires no authorization. One connector shows an `X-Careers-Sites-Token` header, so some sites may require a token.
- **Not verified:**
  - the request body;
  - CAPTCHA or bot protection on specific careers sites;
  - whether a submission through the API looks identical to the web form for the employer.
- **Contractual:** unclear. The Careers Site API is intended for employers building their own careers pages. Using it as an applicant is the same data the public form sends, but Recruitee's terms do not explicitly allow third-party automated submission, and an employer could treat it as unwanted automation.
- **Recommendation:**
  - Choose one specific job whose public form needs only name, e-mail, phone and CV (PDF), with no required open questions.
  - Read that site's careers page and privacy notice.
  - Submit **once, supervised**, with you watching.
  - Alternatively, submit the first one manually with the generated PDF and use the adapter only for `dry_run` until the endpoint is confirmed.

### 17.4 Applicant information and PDF

**Verified applicant data needed:** `data/applicant_profile.yaml` (does not exist yet) containing:
- `verified: true`;
- full name;
- e-mail;
- phone (if required);
- location/country answer;
- work authorisation answer (EU/NL);
- notice period / availability;
- salary expectation (only if a form requires it);
- answers to job-specific required questions;
- plus the validated tailored CV.

Nothing will be guessed. Missing fields are reported as `manual_action_required`.

**PDF from HTML on this VM (live-verified with a sample):**
```
google-chrome --headless=new --disable-gpu --no-sandbox --no-first-run --disable-extensions \
  --disable-background-networking --disable-sync --user-data-dir=<tmp> --no-pdf-header-footer \
  --print-to-pdf=<out.pdf> file://<cv.html>
```
- Chrome 154 is installed, and the fonts (Liberation, DejaVu, Noto) are present.
- Notes:
  - **Memory:** the VM has 969 MB RAM with about 390 MB available, so render one PDF at a time.
  - **Network:** without the two `--disable-*` flags, Chrome makes background network calls.
- Implementing this as `app/pdf.py` (subprocess with a timeout, temporary profile, `@page` A4 check) is a small follow-up PR. It is **not implemented** here.

### 17.5 What needs your approval before the first real submission

1. Choose the exact job (employer, posting URL) and confirm you actually want to apply.
2. Provide `data/applicant_profile.yaml` with `verified: true`. You write or confirm every value.
3. Approve the tailored CV and the generated PDF for that job.
4. **Accept the contractual uncertainty in §17.3.** Then I implement and test (against nothing live) `RecruiteeAdapter.submit()` and you approve marking it `live_capable`.
5. Approve setting `APPLY_MODE=live` and `APPLY_LIVE_ADAPTERS=recruitee` for that **single** submission, then switching back to `off` immediately afterwards.
