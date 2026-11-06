# Autonomous job submission: platform feasibility, gated Recruitee adapter, vacancy shortlist

- **PR:** #11 - https://github.com/majid-lotfian/job-hunter-agent/pull/11 (**stacked on PR #10**: merge #10 first, then retarget #11 to `main`)
- **Branch:** `feature/autonomous-recruitee` · **Commits:** implementation commit, then this report commit on top
- **Date:** 2026-11-06 14:23 (Europe/Amsterdam) · **Author:** Claude Code (Opus 5.5)
- **Status:** open: not merged and not deployed. No live submission was made, during tests or otherwise.

> Legend: **Implemented** = code in this PR · **Unit-tested** = covered by automated tests ·
> **Live-verified** = observed against real systems (state where) · **Assumed/Proposed** = not verified.

## Owner decisions summary (read first)

1. **Bottom line: no vacancy in reach can be submitted autonomously right now under your rules.**
   - The live scan found **0 of 169** matching vacancies eligible.
   - Greenhouse, Lever and Ashby (and SmartRecruiters, Workable, Personio, Teamtailor) accept applications only with the **employer's** API key.
   - Recruitee's public Careers Site API is the only applicant-side mechanism. But 6 of the 7 Recruitee-hosted careers forms checked have **invisible hCaptcha switched on**, and the 7th (Centric) requires a consent agreement the API cannot record.
   - Posting to the API on a CAPTCHA-protected form would get around the employer's bot protection, which your rules forbid.
2. **What this PR provides anyway:**
   - A complete, tested and gated autonomous adapter for Recruitee. It submits by itself, after your approval, as soon as a matching employer has the CAPTCHA disabled and no consent box.
   - A feasibility scanner that finds such vacancies.
3. **Decision needed: how to get the first autonomous application.** Options:
   - **(a) Recommended:** add more Recruitee employers to discovery and let the scanner flag the first eligible English AI role. It may take time, or never happen if most employers keep the default CAPTCHA.
   - **(b)** Accept that the best-matching roles need a manual browser submit (PR #10), and use it for the first applications now.
   - **(c)** Ask an employer directly for their apply API. Unrealistic.

   Bypassing CAPTCHA, or using another person's or employer's API key, is not an option.
4. **To enable live submission later:** merge #10 and #11, deploy, verify your profile, then set `APPLY_MODE=live` and `APPLY_LIVE_ADAPTERS=recruitee`. Approval stays per job.

## 1. Task objective

Real autonomous submission after explicit approval:
- research which ATS platforms permit applicant-side submission;
- implement the permitted adapters with mocked tests;
- find and rank real vacancies that can be submitted autonomously;
- end to end: vacancy check, profile, CV/PDF, payload validation, one submission, confirmation, applied only on success, Telegram.

## 2. Initial problems identified (platform research, verified against vendor docs on 2026-11-06)

| Platform | Applicant-side submission? | Evidence |
|---|---|---|
| **Greenhouse** | **No** | Job Board API: Basic auth whose username is the employer's API key, Base64-encoded. "Any form posts should be proxied by your own servers"; a direct post "would reveal your secret key". |
| **Lever** | **No** | Postings API `POST /v0/postings/SITE/ID?key=APIKEY`: "you need an API key, which a Super Admin of your account can generate". |
| **Ashby** | **No** | `applicationForm.submit` "requires the `candidatesWrite` permission"; API keys are managed by "an Ashby Admin"; every endpoint needs a key. |
| SmartRecruiters, Workable, Personio, Teamtailor | No | Employer/partner tokens (PR #7 research). |
| Workday, aggregators (Himalayas, Arbeitnow, …) | No | Workday needs a candidate login in a JavaScript app (heavy browser automation, logins). Aggregators link out to the employer's own system. |
| **Recruitee** | **Yes, documented public** | Careers Site API `POST /api/offers/{slug}/candidates`: no authorization, "from candidate perspective"; creating a candidate triggers the same confirmation email as the form. **But** the employer's careers form can enable hCaptcha and consent agreements, and the API documents no fields for either. |

**Recruitee form settings, live-checked** (one careers page per employer):

| Employer | CAPTCHA setting | Consent agreement | Relevant jobs |
|---|---|---|---|
| Conclusion | invisible hCaptcha | none | jobs 73, 74 (Dutch) |
| Eraneos | invisible hCaptcha | yes | job 80 (Dutch; required question "Spreek je vloeiend Nederlands?") |
| Xebia (`xebiacareers`, not yet in discovery) | invisible hCaptcha | yes | AI Engineer, ML Engineer (English) |
| Attendi (not in discovery) | invisible hCaptcha | yes | ML role closed; Software Engineer |
| Nmbrs, RTL | invisible hCaptcha | none | none relevant |
| **Centric** | **disabled** | **yes** ("Ik heb de privacyverklaring … gelezen") | job 75 (Dutch B2 required, score 30) |
| bunq, Funda, Greenchoice, AIMMS | no Recruitee-hosted form found | – | – |

**Correction found during this work:** `"hcaptcha": true` is a platform feature flag that also appears when the employer disabled the CAPTCHA (Centric). PR #10's detector treated it as "CAPTCHA on". The detection now uses the per-site `"captcha":"<mode>"` setting.

## 3. Architecture and implementation decisions

- **One permitted adapter, strictly gated.** `RecruiteeAdapter.live_capable = True`. `submit()` re-reads the form, then refuses (MANUAL, nothing sent) unless all of the following hold:
  - the vacancy is open;
  - the CAPTCHA mode is explicitly `disabled` or `none` (an unknown setting counts as protected);
  - there are no consent agreements;
  - the profile is verified and a current CV PDF exists;
  - every required field and question is filled from verified data (screening answers only from your explicit `answers`);
  - no visible location choice (it has no documented API field).
- **Exactly one POST.** The new `HttpClient.send_once()` is paced and never retries. The multipart payload contains only `candidate[name]`, `[email]`, `[phone]`, `[cv]` (the PDF of the approved HTML CV) and `open_question_answers_attributes` (`flag` / `content` / `multi_content`). No cover letter or photo, and optional unanswered questions are omitted.
- **Outcome mapping:**

  | Response | Result | Effect |
  |---|---|---|
  | 201/200 with `candidate.id` | **submitted** | `external_id = recruitee:<company>:candidate:<id>`, job `applied`, Telegram "Application submitted" with the confirmation |
  | 201 without an id | **manual** | outcome uncertain; never marked applied |
  | 422 | **blocked** | field names stored; nothing created |
  | 401 / 403 | **blocked** | site requires a token: unsupported |
  | 404 / 410 | **blocked** | vacancy closed |
  | 429 | **retry later** | not created; normal application backoff |
  | 5xx / timeout / network error | **manual** | outcome unknown; **never resent automatically** |
- **The package knows whether a job can go autonomously.** `package.autonomous = {possible, blockers}`. `process()` submits live only when `APPLY_MODE=live`, the adapter is listed in `APPLY_LIVE_ADAPTERS`, and `autonomous.possible` is true. Otherwise it prepares the job for your browser (PR #10).
- **Approval identifies exactly what is sent.** The Telegram/dashboard message shows:
  - job, employer and score;
  - the CV version hash with links to the HTML and the PDF;
  - the application system;
  - whether it would be submitted automatically (with the exact data list) or prepared for you.

  Approval tokens are bound to the job version and CV version (existing behaviour), so a changed CV or posting voids the approval.
- **Unchanged:** the existing state machine, duplicate protection (one live application per job and per canonical URL), applied-only-with-confirmation, and restart-never-resubmits.

## 4. Files added or modified

| File | Change |
|---|---|
| `app/applications/adapters.py` | Platform research summary; `careers_form_settings()` (CAPTCHA mode + consent); `FormSpec.consent_required/raw`; Recruitee `submit()`; infobox questions skipped; hidden location question no longer counted as missing in `prepare()` |
| `app/applications/prepare.py` | `autonomous {possible, blockers}`; unsupported visible location choice |
| `app/applications/__init__.py` | Live path requires `autonomous.possible`; adapter-reported missing fields stored |
| `app/applications/feasibility.py` (new) | Read-only ranked shortlist CLI |
| `app/discovery/http.py` | `send_once()` (no retries) |
| `app/workflow.py` | Approval message: CV version + PDF link + submission method |
| `tests/test_autonomous.py` (new) | 12 tests with mocked Recruitee endpoints |
| `tests/test_workflow.py`, `tests/test_cv_pipeline.py` | Live test adapter declares a permitted form; complete profile in the shared fixture; shared-DB cleanup |

## 5. Features implemented

- **Implemented and unit-tested:** gated Recruitee autonomous submission, outcome mapping, no-resend guarantees, CAPTCHA/consent detection, autonomy assessment, approval details, feasibility scan.
- **Not live-verified:** a real submission. Not allowed, and no eligible vacancy exists.

## 6. Configuration changes

None by default. Live submission later needs `APPLY_MODE=live` and `APPLY_LIVE_ADAPTERS=recruitee` in `.env`, set only with your explicit approval. During development and tests `APPLY_MODE` stayed `off` on the VM.

## 7. Tests executed and results

Full suite in the production venv, network blocked (`HTTPS_PROXY=http://127.0.0.1:9`): **273 tests, OK** (261 in PR #10, plus 12 new).

| Requirement | Test (`tests/test_autonomous.py`) |
|---|---|
| Eligible form: exactly one POST, exact multipart fields, PDF attached, no cover letter, confirmation id stored, job applied, Telegram confirmation, no second POST, no second approval | `test_eligible_form_is_submitted_once_with_confirmation` |
| No POST when: CAPTCHA on, CAPTCHA unknown, consent box, unanswered required question, visible location choice | `test_no_post_when_form_is_not_permitted` |
| `APPLY_MODE=off`, or adapter not listed: no POST | `test_apply_mode_off_or_unlisted_adapter_never_posts` |
| 422: blocked, field names kept, not applied | `test_validation_error_is_blocked_without_marking_applied` |
| 429: retried later; then success | `test_rate_limit_retries_later_but_unknown_outcomes_never_resend` |
| 500 / 201 without id / timeout: never resent, not applied | `test_unknown_outcomes_are_never_resent` |
| 403: unsupported | `test_closed_or_token_protected_site` |
| Approval message names the CV version, PDF and method; live labels | `test_live_approval_message_names_cv_version_and_method` |
| Feasibility ranks an eligible Recruitee job above Greenhouse, no POST | `test_feasibility_ranks_eligible_recruitee_first` |
| CAPTCHA mode vs feature flag; consent detection | `test_captcha_mode_not_feature_flag` |
| `send_once` never retries | `test_send_once_never_retries` |
| Whole-word CV gaps ("rust" ≠ "trust") | `test_gap_terms_are_whole_words` |

## 8. Live verification results (tested vs. assumed)

**Live-verified, read-only (public GETs only, nothing submitted):**
- the vendor documentation quoted in §2;
- the Recruitee form settings for 11 employers;
- the Xebia and Attendi offers;
- the feasibility scan against a temporary copy of the production DB: **169 role-matching active vacancies, 0 autonomously submittable**.

**Not verified:**
- **whether Recruitee's API itself enforces the hCaptcha.** Finding out would require a real submission, so it was not tested. The adapter does not rely on it either way.
- a real end-to-end submission.

## 9. Performance and resource considerations

- A submission is one multipart POST of about 25 KB.
- The feasibility scan makes about 2 GETs per Recruitee vacancy and no network calls for other platforms.
- No browser automation.

## 10. Security and privacy checks

- No CAPTCHA, login, token or rate-limit circumvention. The adapter refuses CAPTCHA/consent-protected forms and unknown settings.
- **Personal data sent**, only in the single POST after your approval and only when eligible: name, email, phone, CV PDF and your explicit screening answers.
- Logs and Telegram carry no personal values. The diff was scanned for secrets.
- The temporary DB copy used for the scan was deleted.

## 11. Known limitations

- Only Recruitee has an applicant-side API, and most Recruitee employers keep the CAPTCHA on, so autonomous coverage is near zero today.
- Recruitee's API documents no consent field. Forms with consent agreements, even optional data-retention ones, are treated as not permitted. This is conservative and could be refined per agreement wording, with your approval.
- The feasibility "gaps" column is a keyword heuristic, not a full requirements analysis.

## 12. Outstanding issues / decisions needed

1. Choose option (a), (b) or (c) from the Owner decisions summary.
2. If (a): approve adding more Recruitee employers to `discovery.yaml` (e.g. `xebiacareers`, `attendi`), and keep scanning.
3. Merge order: PR #10, then PR #11 (retarget to `main`). Deploy and verify the profile as in the PR #10 report.

## 13. Deployment instructions (not executed, require approval)

1. Merge PR #10 into `main`. Retarget PR #11 to `main` and merge it.
2. Follow the PR #10 report §13: DB backup, stash, fast-forward, pop, run the tests (expect 273 OK), restart **only** `job-hunter.service`, keep `APPLY_MODE=off`.
3. Create and verify the profile (`python -m app.applications.profile --init-from-cv`, edit, `verified: true`).
4. Run `python -m app.applications.feasibility` (read-only).
5. **Only when it shows an eligible vacancy you want:** with your approval, set `APPLY_MODE=live` and `APPLY_LIVE_ADAPTERS=recruitee`, restart, then approve that job in Telegram or the dashboard.

## 14. Rollback instructions

- **Stop autonomous submission instantly:** `APPLY_MODE=off` (or an empty `APPLY_LIVE_ADAPTERS`), then restart.
- **Code:** `git reset --hard` to the previous `main` commit, then restart. There are no schema changes.

## 15. Recommended next steps

1. Decide on the route (Owner decisions summary, item 3).
2. If (a): add Recruitee employers and re-scan weekly.
3. Meanwhile, use PR #10 to make the first applications to the strongest matches in your browser.

## 16. PR URL and commits

- PR: https://github.com/majid-lotfian/job-hunter-agent/pull/11 (base: `feature/supervised-apply`)
- Implementation commit and report commit: see the PR

## 17. Shortlist: real vacancies by technical match and feasibility

Live scan on 2026-11-06, English-language, score ≥ 70, AI/ML titles. "Not in CV" = technologies the posting names that the master CV does not.

| Job | Score | Employer · role | ATS | Autonomous? | Reason | Not in CV |
|---|---|---|---|---|---|---|
| 24 | 95 | Nebius · Senior ML Engineer (Agentic Search) | Greenhouse | no | employer key required | kubernetes, c++, inference |
| 25 | 95 | Nebius · Senior ML Engineer (AI Research) | Greenhouse | no | employer key required | inference |
| 33 | 95 | Nebius · Staff Applied AI Researcher (Agentic Search) | Greenhouse | no | employer key required | inference |
| 54 | 95 | Prosus · Senior ML Engineer – LLMs | Ashby | no | employer key required | inference, vllm, deepspeed |
| 158 | 95 | INDG · Senior AI Engineer | via Himalayas | no | employer's own system | typescript |
| 142 | 95 | IXOPAY · Sr. AI Engineer | via Himalayas | no | employer's own system | aws |
| 117 | 95 | Red Sky · Lead ML Engineer (Robotics & CV) | via Himalayas | no | employer's own system | – |
| 23 | 95 | Nebius · Senior ML Engineer, LLM Inference Optimization | Greenhouse | no | employer key required | inference, cuda, vllm |
| 17 | 95 | Nebius · Senior Applied AI Solutions Engineer | Greenhouse | no | employer key required | kubernetes, mlops, inference, cuda |
| 135 | 95 | Typeform · Staff AI Engineer – EU | via Himalayas | no | employer's own system | kubernetes … |
| 73 | 80 | Conclusion · AI Solutions Engineer | Recruitee | no | hCaptcha on form (Dutch posting) | azure, typescript |
| 80 | 95 | Eraneos · (Senior) AI Engineer | Recruitee | no | hCaptcha + consent; fluent Dutch required | – |

**Closest to autonomous:** Xebia's English AI Engineer and ML Engineer roles on Recruitee (not yet in discovery) would be ideal, but their form has hCaptcha and a consent agreement. Not eligible.
