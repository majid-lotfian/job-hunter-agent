# Recruitee forms by stable field names (Dutch and English), no file-input guessing, phone read-back

- **PR:** #new (renamed after creation)
- **Branch:** `feature/recruitee-stable-fields` (from `main` 2aaa14c)
- **Date:** 2026-11-08 20:53 (Europe/Amsterdam) · **Author:** Claude Code (Opus 5.5)
- **Status:** open: not merged, not deployed. **No application was submitted.** `APPLY_MODE=off`; no other service changed.

> Legend: **Implemented** = code in this PR · **Unit-tested** = automated tests · **Live-verified** = observed on
> real systems · **Assumed/Proposed** = not verified.

## Owner decisions summary (read first)

1. **Result:** live dry runs (`submit=false`) on Conclusion **#74** and **#73** now fill **all 4 required fields** and re-verify them right before the (unclicked) Submit: name, email, phone, CV. Before this PR, 3 of 4 failed on the Dutch labels.
2. **Safety bug fixed:**
   - **What happened:** when a label was not found, the worker attached the CV to "the first file input on the page". On Conclusion's form that is **Foto** (photo).
   - **Now:** files go only into the located field. The single-input fallback is used only for forms with exactly one file input.
3. **Phone:** Recruitee's phone widget regroups spaces (for example "+31 6 1234 5678" → "+31 6 12345678"). Phone read-back now requires the identical number (same digits, same leading +); all other fields still need an exact match.
4. **Neither job has been submitted** (DB, §8). #74 has a prepared manual application (3) from your earlier Telegram "Prepare" press. **Please confirm you did not apply to #74 or #73 by hand.**
5. **Next, after your approval:** merge and deploy, cancel prepared application 3, then one Telegram review with "Approve & Submit" for the chosen job, with live mode for that job only.
   - **Remaining unknown:** Recruitee's invisible hCaptcha decides at submit; the worker stops if a challenge appears.

## 1. Task objective

Make the Recruitee filler work by stable internal names (`candidate.name/.email/.phone/.cv`), keep the English-label fallback, support Dutch labels, add English and Dutch form tests including CV upload verification, and dry-run #74 and #73 on the live forms. No deploy, no submission.

## 2. Initial problems identified

- **Dutch labels:** the worker located controls only by English labels from the Recruitee API ("Full name"). Dutch vacancies render "Volledige naam", "E-mailadres", "Telefoonnummer" and "Cv".
- **File-input fallback** attached the CV to the wrong field (Foto) on multi-upload forms.
- **Autofill clean-up** would have treated fields filled through a name or alias as foreign, and emptied them again.
- **Phone masks** change spacing, so exact read-back failed (found in the live dry run).

## 3. Architecture and implementation decisions

- **Worker `field_locator(page, field)`:**
  1. `selectors` (stable attributes from the package);
  2. the English label (unchanged behaviour);
  3. `aliases` (whole-label match that ignores the trailing `*` and non-breaking spaces).

  It never guesses a control outside these. It is used for text, select, combobox, single-checkbox and file fields, both when filling and in the final verification.
- **Package:** `RECRUITEE_FIELDS` / `field_hints(ats, key)` in `app/applications/browser.py` give selectors and Dutch aliases per Recruitee key (name, email, phone, cv, cover_letter, photo). Other ATS get empty hints and behave as before.
- **File handling:** the file goes into the located field. The fallback is used only when the form has a single file input. Verification is done on the same located field.
- **`same_value(kind, shown, wanted)`:** phone means the same digits and the same leading `+`; anything else must match exactly. A number shown in a different form (national vs international) is not assumed equal.
- **Autofill clean-up:** fields count as the agent's own by label, alias **or** selector `name`.

## 4. Files added or modified

| File | Change |
|---|---|
| `worker/apply_worker.py` | `field_locator`, alias matching, file helpers take the field (no multi-input guessing), `same_value`, clean-up aware of names and aliases |
| `app/applications/browser.py` | `RECRUITEE_FIELDS`, `field_hints`; package fields carry `selectors` and `aliases` |
| `tests/fixtures/forms/recruitee_nl.html` (new) | Replica of the live Dutch Recruitee form (labels with nbsp + `*`, `candidate.*` names, Foto/Cv/Motivatiebrief uploads, phone regrouping); `?lang=en` for English labels |
| `tests/test_browser_worker.py` | 5 real-browser tests + 2 unit tests |
| `docs/agent-reports/…`, `README.md` | This report and index |

## 5. Features implemented

All rows of §4 are implemented and unit-tested. The Dutch form handling is also live-verified with dry runs (§8).

## 6. Configuration changes

None.

## 7. Tests executed and results

- **Full suite:** `.venv/bin/python -m unittest discover -s tests` gives **328 OK (21 skipped)**, twice.
- **Real-browser tests:** `RUN_BROWSER_TESTS=1 … -m unittest tests.test_browser_worker` gives **35 OK**. New tests:
  - Dutch form by stable names: submitted on the mock; the confirmation echoes `cv=1 photo=0 cover=0`;
  - Dutch form by Dutch labels only;
  - English labels;
  - unknown labels with no hints: fails before submit, and **no file is attached to Foto**;
  - dry run on the Dutch form: everything read back, nothing cleared.
- **Unit tests:** the package carries the Recruitee names and aliases (Greenhouse does not); phone comparison (regrouped spaces OK, different digits or form not OK, text fields exact).

## 8. Live verification results (tested vs. assumed)

**Live form structure** (read-only, with a blank test PDF):
- Conclusion's form has the names `candidate.name`, `candidate.email`, `candidate.phone`, `candidate.photo`, `candidate.cv` and `candidate.coverLetterFile`, with Dutch labels.
- An attached PDF stays in the input (`files.length` = 1) and its name is shown. There is no upload on attach, only analytics calls.
- The phone widget regroups spaces.

**Live dry runs** (branch code against production data, `submit=false`):

| Job | Result | Evidence (VM, private) |
|---|---|---|
| **#74** Conclusion · Senior Software Developer (AI) · 85 | `filled`: CV, name, email, phone; `verified_before_submit: true`; 0 refilled, 0 cleared; Submit not clicked | `data/applications/app-0-job-74-20261108-205215/` |
| **#73** Conclusion · AI Solutions Engineer · 80 | same: 4/4, verified, Submit not clicked | `data/applications/app-0-job-73-20261108-205246/` |

The #74 screenshot was checked: name, email and phone (country Nederland) are filled; **Foto is empty**; **Cv shows the tailored PDF**; Motivatiebrief is empty; no CAPTCHA is visible.

An earlier run in this task, before the phone fix, filled 3/4 fields with the phone not verified, so the worker stopped before Submit.

**Submission check (SQLite):**
- **#74:** application 3 `manual_action_required` (prepared); `submitted_at` NULL; no submit events.
- **#73:** no application.
- **No submitting, submitted or confirmed application** for any Conclusion URL.

The DB cannot see a manual application made outside the agent.

**CVs:**
- **#74:** version `4d7f08390a58` (corrected on Nov 8).
- **#73:** generated today with the review warning "GenAI" (a term not in the master CV). I replaced it with "LLM", which gives version `69d4001cd69a`; the PDF was re-rendered and the review checks are empty. The generated file is kept as a `.generated-bak` copy.

**Not verified:** the submit-time behaviour of Recruitee's invisible hCaptcha.

## 9. Performance and resource considerations

A dry run takes about 25 s.

## 10. Security and privacy checks

- **No submission;** live mode never enabled.
- **Live-page inspections** used a blank test PDF; contact values were masked in logs.
- **Evidence stays on the VM** under `data/` (gitignored).
- **The report contains no personal values.**

## 11. Known limitations

- Recruitee screening questions (`question_*`) are still located by label only. Jobs with Dutch questions remain manual: they need answers from your profile anyway.
- The phone must be stored in international form; a widget that converts it to national form would fail the read-back (safe stop).
- The CV generator produced unsupported wording twice (#74, #73), and the automated review flagged only one of them. A stricter check is proposed separately.

## 12. Outstanding issues / decisions needed

1. Approve merging and deploying this PR.
2. Confirm you have not applied to Conclusion #74 or #73 yourself.
3. Choose the job for the first autonomous submission: **#74 (score 85, recommended)** or #73 (80). Application 3 for #74 must be cancelled first, so that a fresh approval is possible.

## 13. Deployment instructions (require approval)

1. **Deploy:**
   - back up the DB;
   - `git stash push app/main.py deploy/job-hunter.service search.yaml`, `git merge --ff-only origin/main`, `git stash pop`;
   - run the full and real-browser tests;
   - `sudo systemctl restart job-hunter`.
2. **First autonomous submission (one job N, after your go-ahead):**
   - cancel application 3 if N=74;
   - `python -m app.applications.review --job N` gives READY;
   - set `APPLY_MODE=live`, `APPLY_LIVE_ADAPTERS=browser`, `APPLY_LIVE_JOB_IDS=N`, restart;
   - `review --job N --send`;
   - you press "Approve & Submit";
   - one submission;
   - check the confirmation evidence;
   - set `APPLY_MODE=off`, restart.

## 14. Rollback instructions

Revert the merge commit.

## 15. Recommended next steps

1. Deploy.
2. Do the first autonomous submission on #74.
3. Tighten the CV review for unsupported wording.

## 16. PR URL and commits

See the PR (URL added after creation).
