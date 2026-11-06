# First live application attempt (JetBrains job 38): not submitted (form validation), root cause, worker fixes

- **PR:** [#15](https://github.com/majid-lotfian/job-hunter-agent/pull/15)
- **Branch:** `fix/autofill-reverify` (from `main` 401d542)
- **Date:** 2026-11-06 17:48 (Europe/Amsterdam) · **Author:** Claude Code (Opus 5.5)
- **Status:** open: not merged, not deployed. `APPLY_MODE=off` (restored at 17:29).

> Legend: **Implemented** = code in this PR · **Unit-tested** = automated tests · **Live-verified** = observed on
> real systems · **Assumed/Proposed** = not verified.

## Owner decisions summary (read first)

1. **Outcome of the authorized submission of job 38: NOT submitted. The employer did not confirm receipt.**
   - The agent's worker filled and verified every field, then clicked Submit once (17:28).
   - The JetBrains/Greenhouse form refused it client-side with **"First Name is required" / "Last Name is required"**.
   - **Cause:** attaching the CV triggers Greenhouse's resume **autofill**, which overwrote the name fields after they had been filled and read back.
   - The worker reported "uncertain": it didn't recognise Greenhouse's error text. Nothing was retried.
   - The application is recorded as `manual_action_required`, with the evidence path and the explanation.
2. **This PR fixes it.** The live dry run with the fixed worker (17:45, `submit=false`) filled all 9 fields and re-verified them as still correct immediately before the (not clicked) Submit. The screenshot was checked.
3. **Correction to the PR #14 report:** attaching a file on this form **uploads it immediately** to Greenhouse's storage (an S3 POST was observed), despite `data-allow-s3="false"`. Your CV was therefore uploaded to Greenhouse when it was attached (dry runs and the live attempt), but no application was sent.
4. **Decisions for you:**
   - approve merging and deploying this PR;
   - authorize **one** new submission attempt for job 38 (a new approval; your earlier authorization was used by the refused attempt).

## 1. Task objective

Submit one real application for job 38 via the agent's own worker, with the existing approval and duplicate protection, then restore `APPLY_MODE=off`. Steps:
- deploy PR #14;
- verify the profile against the CV, and the screening answers and the CV/PDF;
- submit exactly once;
- record the result and report confirmation evidence.

## 2. Initial problems identified

| Problem | Evidence | Fixed by |
|---|---|---|
| Resume autofill rewrites fields after they were filled | Live screenshot after Submit: names empty with required-field errors. Diagnostic with a blank dummy PDF, no submit: first name cleared about 1 s after attaching. | Files attached first, then a settle wait, then the other fields; a final re-verification of **every** field right before Submit; one re-fill; otherwise stop before Submit |
| Greenhouse validation text not recognised ("First Name is required.") | Live result `uncertain` | `validation_errors` also detects visible error-like texts, so such cases become `failed` with the message |
| A file attached before the page finished loading its scripts is ignored | Diagnostic: an attach at load+1 s was ignored, at load+5 s it worked | Wait for network idle plus 3 s before filling; retry the attach once |
| After an upload Greenhouse **removes** the resume input; the check then looked at the cover letter's input | Diagnostic: the file was attached but the check returned False | `_file_attached` always checks the field located by its label |
| The local backend deleted the evidence folder after each run | Code review before the live run | Private copy kept (`keep_evidence`) |

## 3. Architecture and implementation decisions

**Worker order** (`run`):
1. load, wait for network idle and hydration;
2. check for blockers;
3. attach files, then wait for autofill to settle;
4. fill the other fields;
5. wait 2 s;
6. empty autofilled text fields that are **not** in the package and were empty at load (only package values are sent);
7. verify every filled field; re-fill any that changed once; verify again;
8. **only if all are still correct**, check blockers again and click Submit (at most once).

**Other decisions:**
- **`verify_field`** reads state without changing it: text value, attached file, select/combobox choice, checked options.
- **Local evidence** goes to `data/applications/app-<id>-job-<job>-<time>/` (mode 700/600): package, result, worker log and screenshot. The path is logged with the outcome. `BROWSER_WORKER_EVIDENCE_DIR` overrides it.
- **The temporary wrapper** `data/worker-evidence-python.sh` (VM only, not in git) did the same for the live attempt. It is no longer configured (`BROWSER_WORKER_PYTHON` was removed from `.env`), and it becomes unnecessary with this PR.

## 4. Files added or modified

| File | Change |
|---|---|
| `worker/apply_worker.py` | `_file_target`/`_file_attached`/`_attach_file` (retry, label-bound check), `verify_field`, `clear_unrequested_values`, broader `validation_errors`, new `run` order, hydration wait |
| `app/applications/browser.py` | `keep_evidence` for the local backend, worker log captured, `BROWSER_WORKER_EVIDENCE_DIR` |
| `tests/fixtures/forms/greenhouse_react.html` | Preferred-name field, cover-letter upload; `autofill=fast\|late\|always`, `hydrate=<ms>`, `mode=gh_required`; resume input removed after upload (like the live form); confirmation echoes the received values |
| `tests/test_browser_worker.py` | 5 new real-browser tests |
| `docs/agent-reports/…`, `README.md` | This report; index; #14 status set to deployed |

## 5. Features implemented

All rows of §4 are implemented and unit-tested. The fixed worker is also live-verified on job 38's form with `submit=false`.

## 6. Configuration changes

Production `.env` during the attempt (17:26–17:29):
```
APPLY_MODE=live
APPLY_LIVE_ADAPTERS=browser
BROWSER_WORKER_PYTHON=data/worker-evidence-python.sh
```
**Restored** to `APPLY_MODE=off`, with the other two lines removed. `BROWSER_WORKER=local` remains.

## 7. Tests executed and results

- **PR #14 deployment (17:22):** 296 OK (11 skipped) and 23 real-browser tests OK on production.
- **This PR:**
  - full suite: **300 OK, 15 skipped**;
  - real-browser tests: `RUN_BROWSER_TESTS=1 … -m unittest tests.test_browser_worker` gives **28 OK** (259 s).
- **New real-browser tests:**
  - autofill overwrite is undone before submit (the confirmation echoes the right names, the extra field is emptied);
  - late autofill is caught by the final check;
  - a field the page keeps changing stops the run before submit;
  - a Greenhouse "is required" message is reported as a form error;
  - a file chosen before the page is ready is attached again.

## 8. Live verification results (tested vs. assumed)

**Pre-submission checks (17:22–17:26):**
- **Profile vs. master CV:** name, email, phone, location, languages and nationality appear in the CV text; LinkedIn, GitHub and portfolio match CV links.
- **Screening questions:** 2 required, answered from your standing rules (Other; Amsterdam, Netherlands). 0 missing, 0 open questions.
- **Optional items:** the data-retention consent and the cover letter were left empty.
- **CV and PDF:** the CV was generated 17:07 for the current posting. Review checks reported nothing (no unsupported terms, complete); the PDF is current (2 pages). I read it: it is consistent with your master CV.
- **Queue:** empty before going live (0 applications, 0 approvals).

**Live attempt:**

| Time | Event |
|---|---|
| 17:26:51 | `job-hunter` restarted with live settings (`hotel-bot` untouched) |
| 17:27:09 | Approval recorded through `workflow.decide` for job 38 only (actor: your written authorization); application 1 queued |
| 17:27:55 | The agent's worker claimed it (`submitting`, attempt 1) |
| 17:28:34 | Worker result `uncertain`: Submit clicked once, no confirmation in 30 s. Mapped to `manual_action_required`, never retried. |
| 17:29 | `APPLY_MODE=off` restored, service restarted |

**Evidence review:**
- **Screenshot** (sha256 `53630202…`, private at `data/applications/live-20261106-172834/`): the form still open, both name fields empty and red, "First Name is required." / "Last Name is required."; all other fields filled; no confirmation; the URL unchanged.
- **Conclusion:** Greenhouse's client-side validation stopped the submission, and no employer confirmation exists. A server-side record is very unlikely, because the form refused to send with empty required fields. A confirmation email from JetBrains would prove otherwise; check your inbox.
- **CAPTCHA:** no challenge appeared at any point. The invisible reCAPTCHA was never exercised server-side, because the form never sent.

**Diagnostics** (fill only, no submit; the first two with a blank dummy PDF containing no personal data):
- autofill clears the first name about 1 s after attaching;
- an S3 upload POST happens on attach;
- an attach before hydration is ignored;
- the resume input is removed after upload. That last one was traced with the real CV copy, which was uploaded again.

**Fixed-worker live dry run** (17:45, `submit=false`):
- status `filled`;
- 9/9 filled;
- `verified_before_submit: true`;
- nothing re-filled, nothing cleared;
- final-state screenshot checked: names, email, phone, CV, LinkedIn, website, Other, Amsterdam; consent unticked.

## 9. Performance and resource considerations

A worker run now takes about 25 s (network idle, hydration, autofill settle, final checks), well within the 300 s timeout.

## 10. Security and privacy checks

- **Private data stays on the VM:** evidence, packages and the CV copy are under `data/` (gitignored, modes 700/600) or in the session scratchpad.
- **The report is sanitized:** no personal values, only hashes and paths.
- **The approval was used only for job 38;** no other job was approved or queued.
- **Correction (see summary):** the CV upload reaches Greenhouse's storage at attach time.

## 11. Known limitations

- The submit-time reCAPTCHA outcome is still unknown.
- Error-text detection is pattern-based (English). An unrecognised error still ends as `uncertain` (manual, never retried), which is safe.
- `clear_unrequested_values` only touches plain text fields that were empty at load and are not in the package.

## 12. Outstanding issues / decisions needed

1. Approve merging and deploying this PR.
2. Authorize one new submission of job 38. Application 1 stays recorded as not submitted, and the duplicate protection allows a new approval because the old one is not in a live state.
3. Optional: check your inbox for any JetBrains message from 17:28 (none is expected).

## 13. Deployment instructions (require approval)

1. **Deploy:**
   - back up the DB;
   - `git stash push app/main.py deploy/job-hunter.service search.yaml`, `git merge --ff-only origin/main`, `git stash pop`;
   - run the full tests and the real-browser tests;
   - `sudo systemctl restart job-hunter`.
2. **Optional:** a live dry run on job 38 with the deployed code.
3. **With your authorization:**
   - set `APPLY_MODE=live` and `APPLY_LIVE_ADAPTERS=browser`, restart;
   - check the queue is empty;
   - approve job 38 once;
   - wait for the result;
   - restore `APPLY_MODE=off`, restart;
   - review the evidence in `data/applications/app-*`.

## 14. Rollback instructions

- **Code:** revert the merge commit.
- **Worker:** an empty `BROWSER_WORKER` disables it.

## 15. Recommended next steps

1. Deploy.
2. One authorized attempt on job 38.
3. If submitted: confirm via email. If refused by reCAPTCHA: keep Greenhouse as supervised-manual and rely on CAPTCHA-free forms.

## 16. PR URL and commits

https://github.com/majid-lotfian/job-hunter-agent/pull/15 · commits: 114cfb0 (fixes, tests, report), then the rename commit.
