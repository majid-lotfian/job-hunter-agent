# Mobile manual-application workflow: Telegram CV PDF, Open / I Submitted / Submit Later, user-reported records

- **PR:** #new (renamed after creation), **stacked on PR #16**
- **Branch:** `feature/mobile-manual-apply` (from `feature/live-review-allowlist` 5468aa9)
- **Date:** 2026-11-08 12:54 (Europe/Amsterdam) · **Author:** Claude Code (Opus 5.5)
- **Status:** open: not merged, not deployed. One demo Telegram message was sent; **nothing was submitted or marked as submitted.**

> Legend: **Implemented** = code in this PR · **Unit-tested** = automated tests · **Live-verified** = observed on
> real systems · **Assumed/Proposed** = not verified.

## Owner decisions summary (read first)

1. **What it does:** when the agent cannot submit, you receive in Telegram, once per application:
   - the current, validated tailored CV as a real PDF document;
   - company, title, score, the direct application URL, the reason, and any missing questions;
   - buttons: **🌐 Open Application**, **✅ I Submitted This Application** (then Confirm/Cancel), **⏳ Submit Later**.
2. **What a confirmation records:** a **user-reported** manual submission:
   - status `submitted` with `submitted_at`;
   - `submitted_manually`, `employer_verified=false` and the source "user-reported via Telegram";
   - no `external_id` and no `confirmed_at` (both reserved for employer-verified confirmations);
   - the job becomes `applied`;
   - the dashboard shows "Manually submitted …", and the original Telegram message is edited to "✅ Manually submitted".
3. **Separate from autonomous submission:** the new buttons (`ms`/`mc`/`mx`/`ml`, keyed by application ID) never call `workflow.decide`, never queue anything, never start the browser worker and never touch `APPLY_MODE`. "Approve & Submit" (`wf:`) is unchanged.
4. **Merge order:** **merge PR #16 first, then this PR.** This PR is built on #16 and reuses its `send_telegram_document`, its PDF validation and its per-job labels and allowlist. With #16 merged, this PR's diff against `main` is only the manual workflow.
5. **Demo (live):** one message was sent for the only application flagged for manual submission (application 1, JetBrains #38), at Telegram message 80. **Its buttons are inactive until this PR is deployed** (production answers "Unknown action"). Application 1 is unchanged.
   - **Caution:** before applying manually to #38, check your inbox for a Nov 6 JetBrains confirmation, so you don't apply twice.

## 1. Task objective

A phone-friendly manual route: notify with everything needed (including the CV PDF), let the owner record "I submitted it" from Telegram with a confirmation step, and keep:
- the records distinct from employer-verified submissions;
- authorization and freshness checks;
- the audit history;
- duplicate prevention;
- strict separation from PR #16's autonomous approval flow.

## 2. Initial problems identified

- **Manual notices were text only:** `send_application_updates` sent a link to the VM dashboard and no CV.
- **`mark_submitted()` needs a typed confirmation** (employer reference), and only accepts "prepared" applications.
- **Limited Telegram dispatch:** the callback handler in `app/main.py` could only answer and remove buttons. It could not ask a follow-up question or edit the original message.
- **`send_telegram` returned only `True`,** so the message ID needed to update the notice later was lost.

## 3. Architecture and implementation decisions

- **`app/applications/manual.py`:**
  - **Candidates:** applications in `manual_action_required` or `dry_run` with a prepared package (supervised preparations and unclear browser outcomes).
  - **Token:** `manual_token = hash(application id, CV version, job version)`; a changed CV or job makes every button stale.
  - **`check_current`:** the CV version must equal the current one, the job version must be unchanged, and `_cv_bundle` must report the PDF as valid for exactly this HTML (PR #9/#16 validation).
  - **`send_manual_notification`:** sends the PDF first (`send_telegram_document` from PR #16), then the message with buttons. **Only after both succeed** does it store `detail.manual_notice` (token, CV version, message ID, text) and `notified_at`. On failure nothing is marked, and it is retried on the next update round.
  - **`record_manual_submission`:** applies the same duplicate guards as `mark_submitted` (same job, same canonical URL), works in a single `BEGIN IMMEDIATE` transaction, and records audit events `application_submitted_manually` and `applied`.
  - **`handle_callback`:** `s` asks the question with Confirm/Cancel; `c` records; `x` records nothing; `l` sets `later_at` and an event with no reminders. Repeated, stale or non-manual presses are rejected.
- **`app/telegram_callbacks.py`:** one entry point for button presses.
  - It authorizes every press (`workflow.telegram_authorized`: `TELEGRAM_CHAT_ID` plus `TELEGRAM_ALLOWED_USER_IDS`), then routes `m?:` to the manual flow and everything else to the unchanged approval handler.
  - It performs `answerCallbackQuery`, button removal, the follow-up question and editing of the original notice.
- **`app/main.py`:** the polling loop calls the dispatcher. The VM-local edit at the top of the file is untouched.
- **`send_application_updates`:** manual candidates get the new PDF message; stale CVs fall back to the old text update, without buttons. `workflow.send_pending_actions` passes its sender through, so `None` means real Telegram.
- **`core.send_telegram`:** returns the sent message (with `message_id`), and stays truthy as before.

## 4. Files added or modified

| File | Change |
|---|---|
| `app/applications/manual.py` (new) | Notification, freshness, callbacks, user-reported record, `--list`/`--notify` CLI |
| `app/telegram_callbacks.py` (new) | Authorized dispatch for approval and manual buttons |
| `app/main.py` | Polling loop delegates to the dispatcher (2 hunks, outside the VM-local edit) |
| `app/applications/__init__.py` | Manual PDF notifications in `send_application_updates` |
| `app/workflow.py` | `telegram_authorized()`; sender passed through |
| `app/core.py` | `send_telegram` returns the message |
| `tests/test_manual.py` (new) | 12 tests |
| `README.md` | "8. Apply manually from your phone (Telegram)" |

## 5. Features implemented

All rows of §4 are implemented and unit-tested. The Telegram send is live-verified (demo, §8). The button presses are **not** live-tested, because they need the PR deployed.

## 6. Configuration changes

None. It uses the existing `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and `TELEGRAM_ALLOWED_USER_IDS`.

**Recommended:** set `TELEGRAM_ALLOWED_USER_IDS` to your user ID, so a group chat member cannot press buttons.

## 7. Tests executed and results

- **Full suite:** `.venv/bin/python -m unittest discover -s tests` gives **321 OK (16 skipped)**, 3 runs.
- **`tests/test_manual.py`, 12 tests:**
  - PDF document is the current validated file, with all details and the 3 buttons; Open points to the right URL; sent only once;
  - Greenhouse application URL and missing screening question listed;
  - failed Telegram delivery: nothing marked, retried later;
  - a stale CV is never attached;
  - Confirm records a user-reported submission: no `external_id` or `confirmed_at`, job applied, events written, original message edited; a repeated Confirm is refused;
  - Cancel records nothing;
  - unauthorized chat or user is rejected;
  - forged tokens and a CV changed after the message are rejected;
  - Submit Later: no reminders, still confirmable later;
  - a manual confirmation never calls the browser worker, never changes `APPLY_MODE` and never queues anything; afterwards no approval is possible;
  - `wf:` approval buttons still go only to the approval path;
  - a manual button on a non-manual application does nothing.

## 8. Live verification results (tested vs. assumed)

**Demo (2026-11-08 12:54):** the branch code was run against production data (`python …/demo_manual.py 1 --send`). Application 1 (JetBrains #38, `manual_action_required`) is the only application flagged for manual submission.

- **First try, before loading production's `.env`:** "Telegram is not configured", and nothing was marked. This live-confirms the failure path.
- **Second try:** **sent**. The PDF is `data/cvs/job-38.pdf` (current, CV `c8f2374f42ae`), followed by message 80, which contains:
  - JetBrains · (Senior) Machine Learning Developer (Junie) · 85/100;
  - `https://job-boards.eu.greenhouse.io/jetbrains/jobs/4937092101`;
  - reason: the evidence-based "NOT SUBMITTED …" note;
  - "Nothing missing";
  - the 3 buttons.
- **Afterwards:** application 1 is still `manual_action_required`, `submitted_at` NULL, `external_id` NULL, job 38 still `approved`. The only new rows are `manual_notice_sent` and `detail.manual_notice`.

**Not verified live:** the button presses, which need deployment. Until then, production answers "Unknown action" to the new buttons and records nothing.

## 9. Performance and resource considerations

Two Telegram API calls per manual application, sent once. No new processes or dependencies.

## 10. Security and privacy checks

- **Authorization:** every press is authorized against the chat and the allowed user IDs.
- **Freshness:** tokens are bound to the application, CV and job; stale or forged presses are rejected.
- **No double recording:** confirmation runs in a transaction with duplicate guards; repeated presses are refused.
- **What is sent:** only your own CV and job data, to your configured chat. The message holds no profile values beyond what the CV PDF contains.
- **The report is sanitized.**

## 11. Known limitations

- A manual record is user-reported. The agent cannot verify it with the employer; `employer_verified=false` makes that explicit.
- Telegram 429 responses on polling (seen in the logs) delay button handling but do not lose presses: updates stay queued at Telegram.
- Messages sent before deployment (the demo) only become functional after deployment, and only while the CV and job are unchanged.

## 12. Outstanding issues / decisions needed

1. Merge PR #16, then this PR, then deploy (with your approval).
2. Optionally set `TELEGRAM_ALLOWED_USER_IDS`.
3. **#38:** confirm the inbox check before using "I Submitted" or applying manually.

## 13. Deployment instructions (require approval)

1. **Order:** PR #16, then this PR.
2. **Deploy:**
   - back up the DB;
   - `git stash push app/main.py deploy/job-hunter.service search.yaml`, `git merge --ff-only origin/main`, `git stash pop`. The `main.py` stash should pop cleanly: different hunks.
   - run the full tests;
   - `sudo systemctl restart job-hunter`.
3. **Check:** `python -m app.applications.manual --list`.

## 14. Rollback instructions

Revert the merge commit. Manual records already made remain valid rows (status `submitted`, `submitted_manually`).

## 15. Recommended next steps

1. Deploy.
2. Use the demo message for #38, or re-send it with `--notify 1`, after the inbox check.
3. Dutch-label support for Recruitee forms, which moves more jobs from manual to autonomous.

## 16. PR URL and commits

See the PR (URL added after creation).
