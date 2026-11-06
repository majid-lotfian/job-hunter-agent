# Supervised application workflow (prepare → review → submit in your browser → record confirmation)

- **PR:** #10 - https://github.com/majid-lotfian/job-hunter-agent/pull/10
- **Branch:** `feature/supervised-apply` · **Commits:** feature commit, then this report commit on top
- **Date:** 2026-11-06 13:41 (Europe/Amsterdam) · **Author:** Claude Code (Opus 5.5)
- **Status:** open: not merged and not deployed. Production runs `529f3d9` (PRs #6–#9).

> Legend: **Implemented** = code in this PR · **Unit-tested** = covered by automated tests ·
> **Live-verified** = observed against real systems (state where) · **Assumed/Proposed** = not verified.

## Owner decisions summary (read first)

1. **Approve PR #10 and its deployment** (§13). No new dependencies and no schema changes.
2. **After deployment, create and check your profile.** `--init-from-cv` drafts it from the master CV, filling 14 of 19 profile fields. You check the derived values (name split, work authorization, two screening answers), optionally add salary, notice period, availability, preferred locations and work arrangements, then set `verified: true`.
3. **First candidate: job 73**, AI Solutions Engineer at Conclusion (§17). It is the best functional match, but the posting is in Dutch and the form uses hCaptcha, so you submit it yourself. Confirm that you want to apply. If your intermediate Dutch is a concern, see the alternatives in §17.
4. **The first real submission happens only in your browser**, after you click Prepare Application. Nothing is submitted automatically, and `APPLY_MODE` stays `off`.

## 1. Task objective

A reusable application workflow:
- a verified applicant profile;
- automatic preparation of each approved application (vacancy check, CV and PDF, form fields and screening questions, profile matching, missing information);
- browser-assisted submission for CAPTCHA, login or manual flows;
- dashboard and Telegram integration (Prepare, Review, Open page, Mark as Submitted with confirmation, Cancel);
- an assessment of job 73.

## 2. Initial problems identified

- The application framework (PR #7) ended every approved job in `manual_action_required` with a bare "apply manually". There was no package of form fields, values or questions, and no way to record a manual submission against the application record. The only option was the job-level "applied" button, which bypasses the application, approval and duplicate checks.
- The applicant profile had only the original handful of keys, no validation and no review tooling. `data/applicant_profile.yaml` did not exist.
- **Employer forms are readable without authentication** in two systems (both live-verified on 2026-11-06):
  - **Recruitee:** `/api/offers/{slug}` returns fields, required flags and open questions.
  - **Greenhouse:** `boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}?questions=true` returns every question with its type and options.

  Together they cover 49 of the 197 jobs (11 Recruitee, 38 Greenhouse).
- The Recruitee careers page embeds its config HTML-escaped (`&quot;captcha&quot;:&quot;invisible&quot;`). A first version missed the CAPTCHA; the live preview (§8) caught it, and it is fixed and tested.
- A closed Recruitee vacancy (HTTP 404) was treated as a temporary error and retried. It now becomes `blocked` ("vacancy is closed").

## 3. Architecture and implementation decisions

- **No new state machine.** The existing approval → application flow is reused:
  - **Prepare Application** is the existing version-bound approval decision.
  - While `APPLY_MODE` is not `live`, the buttons read *Prepare Application* / *Prepare Application + CV*.
  - The worker runs every existing gate, in this order: approval version, cross-source duplicate, current CV PDF, then readiness.
  - A new package step follows, after which the application stops in `manual_action_required` with `detail.prepared = true`.
  - It stops this way whenever automatic submission is off, not permitted (no live adapter), or **the employer's form uses a CAPTCHA**. The CAPTCHA case also blocks live mode.
- **Package** (`app/applications/prepare.py`, stored in `applications.detail.package`):
  - **Vacancy:** open, closed or not confirmed, and how it was checked.
  - **Employer page:** the apply URL.
  - **CV:** the HTML link, plus the PDF link only when `pdf_is_current`.
  - **Form fields:** each field with its required flag, the value to enter and where the value comes from.
  - **Needs your input:** missing information, and screening questions that need your answer.
  - **Warnings:** CAPTCHA, Dutch language, posting changed since the CV was tailored, vacancy not confirmed.
  - **Profile:** validation errors.
- **Form readers** (`adapters.Adapter.form()`, GET only):
  - Recruitee: offer API plus a CAPTCHA check of the careers page.
  - Greenhouse: board API questions; EEOC/compliance and hidden fields are skipped.
  - Everything else: a generic field list with a "check the page" note and no network call.
- **Field matching:**
  - Contact fields come from the profile; the CV field uses the current PDF; a cover letter is never generated.
  - **Screening questions use only explicit `answers`.** A true/false answer is mapped onto exact Yes/No options only.
  - Language multi-selects are matched literally against your profile languages.
  - Anything else becomes "your answer needed".
- **Mark as Submitted** (`applications.mark_submitted`):
  - It requires a prepared application, `confirmed: true` (checkbox) and a confirmation text of at least 3 characters.
  - It runs in one transaction and refuses a second submission for the job and for the same canonical vacancy URL.
  - It sets `submitted`, `external_id = manual:<confirmation>`, `submitted_at`, and the job status `applied`, with events.
  - It is never called automatically.
- **Cancel:**
  - Allowed only before submission. It cancels the application, revokes an active approval, cancels a still-pending approved CV request (no wasted Gemini call), and resets the job to `new` so it can be prepared again.
  - Submitted applications cannot be cancelled.
- **Privacy:**
  - The full package (with your values) is shown only on the authenticated review page.
  - `/api/jobs` carries only counts (`summary`).
  - Telegram messages contain no personal values.
- **Resources:** no browser automation. Preparation makes 1–2 small HTTP GETs per application. The pages are server-rendered with escaped values and a few lines of JavaScript.

## 4. Files added or modified

| File | Change |
|---|---|
| `app/applications/prepare.py` (new) | Package builder and field matching |
| `app/applications/page.py` (new) | Review page (escaped HTML) and allowed actions |
| `app/applications/profile.py` | Full profile schema, `validate()`, `draft_from_cv()`, CLI `--init-from-cv/--check/--show` (file mode 600, never overwrites) |
| `app/applications/adapters.py` | `FormField`/`FormSpec`, `form()` for Recruitee (with CAPTCHA detection) and the new `GreenhouseAdapter`, generic fallback, Recruitee 404 → blocked |
| `app/applications/__init__.py` | Package step and CAPTCHA gate in `process()`; `refresh_package`, `mark_submitted`, `cancel`, `is_prepared`; Telegram "ready for review" message |
| `app/workflow.py` | *Prepare Application* labels and messages while not live; `application_id`, `action_labels` and a package summary in `job_state` |
| `app/main.py` | `/applications/{id}` (review), `/applications/{id}/open`, `POST /api/applications/{id}/submitted`, `/cancel`, `/refresh`; dashboard Review/Open links and summary. Imports are added below line 15; the VM-local hunk at the top is untouched. |
| `applicant_profile.example.yaml`, `README.md` | New schema and documentation |
| `tests/test_supervised.py` (new), `tests/test_workflow.py`, `tests/test_cv_pipeline.py` | 24 new tests; label expectations updated |

## 5. Features implemented

All implemented and unit-tested:
- profile validation, CV draft and CLI;
- the preparation package, form readers, field matching and CAPTCHA gate;
- closed-vacancy blocking;
- the review page, Open, Re-check, Mark as Submitted and Cancel;
- dashboard links and the Telegram ready notice with de-duplication;
- the duplicate guard for manual submissions.

## 6. Configuration changes

None. The profile lives at `data/applicant_profile.yaml` (gitignored via `data/`, checked with `git check-ignore`). `APPLY_MODE` stays `off`.

## 7. Tests executed and results

The full suite ran in the production venv with outbound network blocked (`HTTPS_PROXY=http://127.0.0.1:9`): **261 tests, OK** (237 before this PR, plus 24 new).

| Requirement | Tests (`tests/test_supervised.py`) |
|---|---|
| Profile validation (no values echoed), complete profile valid | `test_validation_reports_problems_without_values`, `test_complete_profile_is_valid` |
| Draft uses only literal CV values, nothing invented; init never overwrites, mode 600 | `test_draft_from_cv_uses_only_literal_cv_values`, `test_init_never_overwrites` |
| Field matching; screening questions only from explicit answers; option mismatch; languages | `test_profile_and_cv_fields`, `test_screening_questions_only_from_explicit_answers` |
| Preparation: Recruitee form, escaped-HTML CAPTCHA, hidden location question, GET-only | `test_recruitee_package_with_captcha_requires_browser_submission`, `test_form_without_captcha_is_detected` |
| Missing fields flagged, not invented | `test_missing_profile_values_are_flagged_not_invented`, `test_missing_pdf_is_listed_once` |
| Closed vacancy | `test_closed_vacancy_is_blocked` |
| Greenhouse questions | `test_greenhouse_form_questions` |
| Generic system, no network | `test_unknown_system_gets_generic_fields_without_network` |
| CV generated first when missing | `test_cv_is_generated_first_when_missing` |
| CAPTCHA blocks live mode | `test_captcha_prevents_live_submission_even_in_live_mode` |
| Telegram once, review link, no personal values; no duplicate after re-check | `test_ready_notification_once_with_review_link` |
| Re-check picks up profile changes | `test_refresh_picks_up_profile_changes` |
| Submission confirmation required; job applied; no second record | `test_mark_submitted_requires_explicit_confirmation` |
| Stale/missing PDF → not prepared → cannot be marked submitted | `test_unprepared_application_cannot_be_marked_submitted` |
| Duplicate vacancy prevention | `test_duplicate_vacancy_cannot_be_recorded_twice` |
| Cancel and prepare again; submitted cannot be cancelled | `test_cancel_allows_preparing_again` |
| Review page auth, HTML escaping, contents; open redirect logged; dashboard API flow | `test_review_page_requires_login_and_escapes_values`, `test_open_page_redirects_and_logs`, `test_dashboard_api_flow` |

Existing tests also cover the stale-CV and changed-job cases (approval version checks).

## 8. Live verification results (tested vs. assumed)

**Live-verified, read-only, nothing written.** The branch code ran against the live Recruitee API for job 73, with an in-memory profile drafted from the master CV and treated as verified only for this preview. Results:
- **Draft profile:** 14 of 19 profile fields filled. Not in the CV, so left empty: salary, notice period, availability, preferred locations, work arrangements. Validation errors once verified: none.
- **Vacancy:** open (Recruitee offer API). **CAPTCHA: yes** (after the HTML-escape fix).
- **Fields:** Full name, Email, Phone and CV (PDF) are required and all filled. Cover letter and photo are optional. No screening questions. The hidden single-location question is not shown.
- **Warnings:** CAPTCHA, and the posting is in Dutch.
- **Earlier live checks the same day:** the Greenhouse questions API (Nebius, 12 questions including languages, work authorization, residence and "employed before"), and Recruitee for Conclusion and Eraneos.

**Not done (needs deployment and your actions):** creating the real profile file on the VM, a real Prepare Application run, and the review page in production.

## 9. Performance and resource considerations

- Per prepared application: 1–2 HTTP GETs. The Recruitee careers page is about 3 MB; the Greenhouse JSON is small.
- No background service or browser is added, and no extra Gemini calls are made. A CV is generated only when you click *Prepare Application + CV* for a job without one.

## 10. Security and privacy checks

- The profile file is gitignored and created with mode 600. The CLI `--check` prints field names, not values.
- The review page requires the dashboard session. State-changing endpoints are POST behind the existing same-origin middleware and the session dependency. Every value is HTML-escaped (tested with a `<script>` job title).
- `/applications/{id}/open` redirects only to http(s) URLs.
- Telegram and `/api/jobs` contain no personal values.
- No CAPTCHA, login or anti-bot bypass: preparation only reads public JSON and the public careers page.
- The diff was scanned for keys, tokens and personal data; none were found.

## 11. Known limitations

- Form readers exist for Recruitee and Greenhouse only. Ashby, Lever, Workday, SmartRecruiters and aggregators get the generic list ("check the page").
- CAPTCHA detection is Recruitee-specific. Greenhouse forms may use an invisible CAPTCHA; the package notes this, and submission is in the browser anyway.
- "Posting changed" compares the live text with the stored description, so a whitespace or markup change may warn without a real content change.
- The job-level "applied" status button from PR #7 still exists. Prefer **Mark as Submitted**, which updates the application, approval and duplicate records.

## 12. Outstanding issues / decisions needed

1. Approve PR #10 (§13).
2. Create and verify the profile (Owner decisions summary, item 2).
3. Confirm job 73, or choose an alternative (§17).
4. Still open from earlier: CV daily volume (bulk auto-CV is off), Telegram token rotation, and updating the PR #9 row in the report index (done in this PR).

## 13. Deployment instructions (not executed, require approval)

1. Merge PR #10. Then `git fetch`.
2. Back up the DB with the SQLite backup API (as in the PR #9 report), and copy the VM-local files.
3. Update the code:
   - `git stash push app/main.py deploy/job-hunter.service search.yaml`
   - `git merge --ff-only origin/main`
   - `git stash pop`

   Expected to be clean, because the new imports are below line 15.
4. `.venv/bin/python -m unittest discover -s tests`. Expect 261 OK.
5. `sudo systemctl restart job-hunter` (only this unit; **`hotel-bot.service` is not touched**). `APPLY_MODE=off` and `CV_AUTO_GENERATE=false` stay.
6. **Profile:**
   - `.venv/bin/python -m app.applications.profile --init-from-cv`
   - review and edit with `nano data/applicant_profile.yaml`, then set `verified: true`
   - `--check` should report "Profile OK".
7. **First run:**
   - In the dashboard, job 73 shows *Prepare Application + CV*, since it has no CV yet. That is 1 Gemini request plus a PDF.
   - Within a few minutes, Telegram sends "Application ready for review".
   - Open **Review Application**, download the PDF, then **Open Application Page**.
   - Submit yourself, then **Mark as Submitted** with the confirmation.

## 14. Rollback instructions

`git reset --hard 529f3d9`, re-apply the VM-local edits, then `sudo systemctl restart job-hunter`. There are no schema changes. Packages stored in `applications.detail` are ignored by older code.

## 15. Recommended next steps

1. Deploy PR #10 and create the profile.
2. Prepare job 73 (or the chosen alternative), then submit it in your browser and record the confirmation.
3. Then add form readers for Ashby and Lever (their public posting APIs) to cover more employers.

## 16. PR URL and commits

- PR: https://github.com/majid-lotfian/job-hunter-agent/pull/10
- Feature commit: supervised application workflow (see the PR)
- Report commit: this file and the index rows

## 17. First application candidate: job 73 assessment

**AI Solutions Engineer, Conclusion (Recruitee, job 73, score 80, Amsterdam/Utrecht hybrid, permanent, EUR 5,000–6,500/month).**
- **Still open:** yes. The offer API was live (HTTP 200, published 2026-09-25).
- **Relevance:** strong for an applied-AI role.
  - **Requirements matched by the master CV:** Python, API integration, Generative AI, prompt engineering, RAG, agentic AI, LangChain/LangGraph, fast prototyping, HBO/WO level (the CV has a PhD).
  - **Not in the master CV (gaps; do not claim them):** Azure OpenAI / Azure AI Foundry / AI Search (the CV shows Google Cloud/Vertex AI instead), TypeScript, Semantic Kernel/AutoGen.
- **Dutch:**
  - The posting is written entirely in Dutch; the English translation is an unfilled template.
  - No explicit language requirement is stated, but it is a client-facing consultancy role ("business en IT met elkaar te verbinden").
  - Your CV lists Dutch: Intermediate. Expect Dutch to matter in interviews and client work.
- **Form:** name, email, phone and CV (PDF) required; cover letter and photo optional; no screening questions; invisible hCaptcha, so you submit in your browser.
- **Recommendation:** suitable as the first supervised application if you are comfortable with Dutch-speaking client work. Otherwise, the strongest English-language alternatives in the DB:
  - **Prosus, Machine Learning Engineer – LLMs** (job 56, score 90, Amsterdam). Requires multi-node distributed training (DeepSpeed/FSDP, data at 100B+ token scale), which is only partly shown in your CV. A stretch.
  - **Nebius, Senior ML Engineer (Agentic Search)** (job 24, score 95, Amsterdam/remote, Greenhouse). Strong on agentic search and RAG, but it mentions Kubernetes and Go/Rust/C++, which are not in your CV. Its Greenhouse form has 4 screening questions (languages, work authorization, residence, previous employment).

  Neither is a better fit than job 73 on the core skills. Nebius is the best English-language option if the Dutch requirement worries you.
