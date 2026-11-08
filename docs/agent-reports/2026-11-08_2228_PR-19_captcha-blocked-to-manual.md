# CAPTCHA-blocked attempts go to the manual Telegram workflow; Conclusion #74 blocked attempt; Greenhouse search

- **PR:** [#19](https://github.com/majid-lotfian/job-hunter-agent/pull/19)
- **Branch:** `feature/blocked-to-manual` (from `main` f8c5f74)
- **Date:** 2026-11-08 22:28 (Europe/Amsterdam) · **Author:** Claude Code (Opus 5.5)
- **Status:** open: not merged, not deployed. `APPLY_MODE=off`. No submission in this task. `hotel-bot` untouched.

> Legend: **Implemented** = code in this PR · **Unit-tested** = automated tests · **Live-verified** = observed on
> real systems · **Assumed/Proposed** = not verified.

## Owner decisions summary (read first)

1. **Conclusion #74, the first real autonomous attempt (2026-11-08 21:18–21:20):**
   - you pressed "Approve & Submit"; the agent filled and verified all 4 fields and clicked "Versturen" **once**;
   - Recruitee then showed an **hCaptcha image puzzle**; the agent stopped without interacting: **not submitted, no employer confirmation**;
   - `APPLY_MODE` was restored to `off` automatically (21:19:53).
2. **New behaviour (this PR):** a browser attempt stopped by a CAPTCHA or human-verification challenge **automatically** becomes a manual application:
   - the CV PDF, the direct link and the Open / I Submitted / Submit Later buttons go to Telegram;
   - the blocked outcome and its evidence stay in the audit history;
   - login walls and form problems stay `blocked`;
   - nothing is retried or solved.
3. **#74 is already handed over:** application 4 was routed to the manual workflow and its Telegram message (98) was sent. See §8.
4. **Search:** no Greenhouse job qualifies for autonomous submission today.
   - Every non-Nebius Greenhouse form has unanswered required questions.
   - The four Nebius forms with zero questions (#34, #27, #35, #37) show an interactive Cloudflare Turnstile.
   - The dry run on the best one (**Nebius #34, score 94**) stopped at page load and filled nothing.
5. **Decisions for you:**
   - merge and deploy this PR;
   - apply manually to #74 (and optionally Nebius #34) from your phone;
   - for future autonomous attempts, the Adyen answers or other standing answers would make Greenhouse forms complete.

## 1. Task objective

1. Route CAPTCHA-blocked applications to the manual workflow while keeping the evidence.
2. Find a high-scoring job (preferably Greenhouse) that needs only verified profile data.
3. No invented answers.
4. A real-browser dry run of the best candidate, without submitting.
5. A PR with the fix, tests and this report, including the #74 attempt.

## 2. Initial problems identified

- **Blocked attempts had no phone path:** a `blocked` application was outside the PR #17 workflow (`manual_candidate` covers only `manual_action_required` and `dry_run`). After #74 you received only a text "Application blocked".
- **Lost evidence:** the worker's evidence (folder, screenshot hash, whether Submit was clicked) was logged but not stored with the application.

## 3. Architecture and implementation decisions

- **Evidence on the result:** `browser.submit` attaches the worker's evidence to the result (`Result.evidence`).
- **CAPTCHA route in `process()`:** if a browser attempt ends `blocked` and `browser.is_human_verification(reason)` (human verification, CAPTCHA, Turnstile), `_blocked_to_manual` does three things:
  1. writes an `application_blocked` event with the evidence folder and whether Submit was clicked;
  2. stores `detail['blocked']`: reason, time, evidence folder, screenshot hash, `submit_clicked`;
  3. finishes as `manual_action_required` with the package, saying whether the challenge came before or after the click.

  The existing update loop then sends the PR #17 notification. The approval is consumed; nothing is queued again.
- **Other blocks unchanged:** a login wall or form problem stays `blocked`.
- **`manual.route_blocked_to_manual`** (CLI `--route-blocked ID`): for older CAPTCHA-blocked records. It accepts only `blocked` applications with a human-verification reason and a package, keeps the original reason, writes a `routed_to_manual` event, and refuses to run a second time.

## 4. Files added or modified

| File | Change |
|---|---|
| `app/applications/__init__.py` | `_blocked_to_manual`; worker evidence stored in `detail['browser_result']` |
| `app/applications/browser.py` | Evidence returned with the result; `is_human_verification()` |
| `app/applications/adapters.py` | `Result.evidence` (optional) |
| `app/applications/manual.py` | `route_blocked_to_manual`, `--route-blocked` |
| `tests/test_blocked_manual.py` (new) | 5 tests |
| `tests/test_browser_worker.py` | The challenge test now expects manual (login still blocked) |
| `README.md` | Manual-workflow section mentions CAPTCHA hand-over and `--route-blocked` |
| `docs/agent-reports/…`, `README.md` (index) | This report; #18 marked deployed |

## 5. Features implemented

All rows of §4 are implemented and unit-tested. The routing of the existing #74 record is live-verified (§8).

## 6. Configuration changes

None. `APPLY_MODE=off`. During the #74 attempt the production settings were temporarily `APPLY_MODE=live`, `APPLY_LIVE_ADAPTERS=browser` and `APPLY_LIVE_JOB_IDS=74`, from 21:18 to 21:19:53. They were then removed automatically.

## 7. Tests executed and results

- **Full suite:** `.venv/bin/python -m unittest discover -s tests` gives **333 OK (21 skipped)**, twice.
- **Real-browser tests:** `RUN_BROWSER_TESTS=1 … tests.test_browser_worker` gives **35 OK**.
- **New tests:**
  - CAPTCHA after the click: manual, evidence kept, `application_blocked` written before `application_manual_action_required`, one attempt, nothing queued; then the PDF and 3 buttons are sent;
  - a challenge before submit also goes to manual ("before submitting");
  - a login wall stays blocked, with no PDF message;
  - routing an old record works once (a non-CAPTCHA block is refused, and so is a second routing);
  - `is_human_verification`.

## 8. Live verification results (tested vs. assumed)

**PR #18 deployment** (2026-11-08 21:16):
- prod `f8c5f74`; backup `data/deploy-backup-pr18-20261108/`;
- 328 tests and 35 browser tests OK on production;
- only `job-hunter` restarted.

**Conclusion #74, application 4:**
- **Before:** the vacancy was open, there was no prior submission in the DB, prepared application 3 was cancelled for a fresh approval, the review said READY, and the final deployed-code dry run filled and verified 4/4.
- **Approval:** live mode was enabled for job 74 only; the Telegram review was sent (CV `4d7f08390a58`); you approved via Telegram at 21:18:44.
- **Worker:** filled and verified CV, name, email and phone, then clicked Versturen once. **An hCaptcha image challenge was shown** ("Klik op de TWEE pictogrammen die anders zijn dan de andere"), and the worker stopped.
- **Recorded:** result `blocked`, `submitted_clicked: true`, no confirmation; the URL stayed on the form.
- **Evidence:** `data/applications/app-4-job-74-20261108-211944/` (screenshot sha256 `e9cb05be…`, checked visually).
- **DB:** application 4 `blocked`, then (this task) routed to `manual_action_required` with `detail.blocked` kept. Events in order: approved → application_blocked → routed_to_manual → manual_notice_sent. The Telegram PDF and buttons went out as message 98.
- **Not submitted:** `submitted_at` NULL; the job is not `applied`.
- **Routing method:** it used this branch's `route_blocked_to_manual` against production data. The deployed PR #17 code handles the resulting buttons.

**Search** (live form readers against the verified profile; no new supported-form jobs since 11-07):

| Jobs | Result |
|---|---|
| Nebius #34 (94), #27, #35, #37 (75) | 0 questions, but an **interactive Turnstile** on the form (probe: challenge on load, no token) |
| Other Nebius (#17, #23, #24, #25, #33, #18, #10, #28, #36, #14, #19, #199) | 1–5 questions + Turnstile |
| Adyen #15/#13/#16 (2 questions), #9 (6), #12 (8) | manual (#15 already application 2) |
| Databricks #11/#8/#44/#39 | 3–4 questions + required "Preferred First Name" |
| Thoughtworks #43 (6), GitLab #41 (3), Celonis #42 (4), IMC #26 (4), DEPT #32 (4) | manual |
| Conclusion #74 / #73 (Recruitee) | 0 questions; hCaptcha challenges at submit (#74 live) → manual |
| Eraneos (Recruitee) | Dutch-fluency question → manual |

**Dry run, best Greenhouse candidate:** Nebius #34, "Staff Machine Learning Engineer, LLM Inference Optimization", score 94.
- **CV:** generated 22:27, version `e5e1d6199d00`; review checks found nothing; I checked it against the master CV and found no unsupported claims.
- **Package:** complete (0 missing, 0 questions).
- **Worker (`submit=false`):** `blocked` at page load, "human verification challenge (turnstile widget)", **nothing filled**.
- **Evidence:** `data/applications/app-0-job-34-20261108-222839/`.

## 9. Performance and resource considerations

Negligible. Gemini was overloaded again for job 34 (4×503, then success after about 50 minutes of retries).

## 10. Security and privacy checks

- **CAPTCHA:** no solving or bypass; the worker stops when a challenge is visible.
- **Approval scope:** the live window was limited to job 74 and closed automatically.
- **Evidence stays on the VM** (`data/`, gitignored).
- **The report contains no personal values.**

## 11. Known limitations

- **Autonomous submission is currently possible on none of the open jobs:** Recruitee with hCaptcha challenges at submit, Nebius shows Turnstile, and the other Greenhouse forms need answers.
- **Untested Greenhouse check:** reCAPTCHA Enterprise behaviour (Adyen, Databricks, …) at submit is still untested, because no such form is complete with your current profile.

## 12. Outstanding issues / decisions needed

1. Approve merging and deploying this PR.
2. Apply manually to Conclusion #74 (Telegram message 98), then tap "I Submitted".
3. Optional: provide standing answers (for example Adyen's two questions) so that a Greenhouse/reCAPTCHA form can be attempted.

## 13. Deployment instructions (require approval)

1. Back up the DB.
2. `git stash push app/main.py deploy/job-hunter.service search.yaml`, `git merge --ff-only origin/main`, `git stash pop`.
3. Run the full and real-browser tests.
4. `sudo systemctl restart job-hunter`.

No configuration change is needed.

## 14. Rollback instructions

Revert the merge commit. Applications already routed to manual remain valid manual applications.

## 15. Recommended next steps

1. Deploy.
2. Manual application for #74.
3. Decide whether to give standing answers for Adyen, to test a reCAPTCHA Enterprise form.

## 16. PR URL and commits

https://github.com/majid-lotfian/job-hunter-agent/pull/19 · commits: faa045c (code, tests, README), report commit, rename commit.
