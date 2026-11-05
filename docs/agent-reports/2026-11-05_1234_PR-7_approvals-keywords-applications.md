# Approval workflows, AI keyword scoring, higher-throughput CV worker, application submission framework

- **PR:** #7 - https://github.com/majid-lotfian/job-hunter-agent/pull/7
- **Base:** `feature/multi-source-discovery` (**stacked on PR #6**, which must be merged first; then retarget #7 to `main`)
- **Branch:** `feature/approvals-applications` · **Commits:** `c82a44c`, `a83f447`, plus the report commit
- **Date:** 2026-11-05 12:34 (Europe/Amsterdam) · **Author:** Claude Code (Opus 5.5)
- **Status:** open - not merged, not deployed, no service restarted, **no real application submitted**

## Decisions needed from the owner (summary)

1. **Volume of automatic CVs.** With your rule (one strong AI keyword lifts a suitable AI role to ≥70), **151 of 198** real jobs from the last live run are in the auto-CV tier, up from 78. On the real data copy, a dry-run rescore of the existing jobs moves auto-CV from 80 to 155. This means roughly 150-300 Gemini calls for the first batch; the default daily budget (`LLM_MAX_REQUESTS_PER_DAY=250`) spreads them over 1-2 days. Keep this, or tighten it (for example, require the keyword in the title, or a primary AI role title)? Examples of lifted roles are in §8.
2. **No automatic submission is possible yet.**
   - Every ATS application API found requires the **employer's** API key: Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Personio and Teamtailor.
   - Recruitee's public careers-site apply endpoint is **not verified**: its terms, a possible CAPTCHA, and the accepted file formats are unknown.
   - So `APPLY_MODE=off` and no live adapter exists. After approval, the agent checks readiness and sends you the direct application link.
   - Should I investigate Recruitee's apply endpoint with you (one supervised test application to a job you choose), or keep everything manual?
3. **A PDF CV is needed for any real upload.** Employer forms expect a PDF or Word file; the agent only has the HTML CV. Approve installing a PDF renderer on the VM (for example, WeasyPrint with its system libraries, or headless Chromium; both are heavy for an e2-micro), or export PDFs yourself?
4. **Applicant profile.** Create `data/applicant_profile.yaml` from `applicant_profile.example.yaml` (private, never committed) and set `verified: true`. Without it, every application stops for manual action.
5. **Jobs that require Dutch get no keyword floor** (they keep the -10 penalty). Is that the intended handling of "otherwise suitable"?
6. **Merge order:** PR #6 first, then this PR. Then approve deployment (§13).

> Legend: **Implemented** = code in this PR · **Unit-tested** = automated tests (mocks only) · **Live-verified** =
> observed against real data/endpoints on the VM using *copies* of the production DB, with Telegram disabled and no
> Gemini calls · **Assumed/Proposed** = not verified.

## 1. Task objective

1. Two approval workflows, synchronized between Telegram and the dashboard, with durable, versioned, auditable state:
   - ≥70: automatic CV, then Approve & Apply.
   - 40-69: Approve CV & Apply.
2. A strong-AI-keyword priority in scoring (RAG, agent, agentic, LLM, fine-tune and variants) that lifts suitable roles to ≥70.
3. Remove the default limit of 5 CVs per day. Add a configurable, rate-limit-aware, cost-protected worker.
4. Restore interactive Telegram buttons, protected against unauthorized, stale and repeated presses.
5. A reliable application-submission subsystem that only submits after approval and never fabricates data, tested with mocks and dry runs, with **no real submissions**.
6. Tests.
7. This report.

## 2. Initial problems identified

- **Buttons:** PR #6's digests removed the approve/reject buttons. The old callbacks only set `status='approved'`, which wasn't tied to any CV or job version.
- **No submission code existed.** Only manual "applied" tracking with a confirmation note.
- **CV throughput:** `CV_DAILY_LIMIT=5` throttled automatic CVs (78 eligible jobs meant about 16 days of backlog).
- **Scoring:** jobs explicitly about LLMs, RAG or agents could still score below 70 when the title didn't match the target list.

## 3. Architecture and implementation decisions

**Workflow (`app/workflow.py`).** One backend for both channels:
- **Tiers:** `workflow_for(score)` gives `auto_cv` (≥ `CV_MIN_SCORE`=70), `approval` (≥ `APPROVAL_MIN_SCORE`=40) or `reference`.
- **Version token:** `action_token = sha256(job_version + cv_version)[:10]`, where `job_version` hashes the title, employer, description and URLs, and `cv_version` hashes the CV file content. Every button and dashboard action carries the token. `decide()` recomputes it inside a `BEGIN IMMEDIATE` transaction, so a changed job or CV gives `stale` (no state change).
- **`decide(job, action, channel, token, actor)`:**
  - writes an `approvals` row (action, channel, versions, actor) and an `events` row,
  - sets the job status (`approved` or `rejected`),
  - creates the `applications` row: `queued` for Approve & Apply, `awaiting_cv` for Approve CV & Apply,
  - Reject revokes approvals, cancels a pending CV and cancels unstarted applications.
  Repeated actions return `already`. Actions are computed by one function, `available_actions()`, used by both channels.
- **Approve CV & Apply:** queues the CV with reason `approved`, which takes top priority and never counts against any automatic limit. `on_cv_finished()`:
  - **ready:** the application becomes `queued`, bound to the new CV version, with no second approval.
  - **needs review** (validation warnings): pause (`manual_action_required`, `reapprove`). The approval is superseded, and you get **Approve & Apply** for that exact CV version.
  - **failed:** pause.
  - **job text changed since approval:** pause.
- **After an approved application ends in "apply manually"** (mode off, unsupported system, missing information), the agent doesn't ask for a second approval. It sends the outcome with the apply URL. Re-approval is only offered for `reapprove` pauses.

**Telegram:**
- Discovery stays a digest. **Individual actionable messages** go out for jobs awaiting a decision, capped by `TELEGRAM_ACTIONS_PER_BATCH`=8 per batch plus one "N more" line. They're persistent: `jobs.action_notified_at` and `action_version`, so a changed version is announced again and an unchanged one never repeats.
- Messages show: score, title, employer, location, matched AI keywords, CV status (plus warnings), application status (plus pause reason), CV review link and apply URL.
- **Callbacks** (`wf:<aa|ac|rj>:<job_id>:<token>`): the chat must equal `TELEGRAM_CHAT_ID`, plus an optional `TELEGRAM_ALLOWED_USER_IDS`. Malformed data is refused. Legacy "Approve draft" buttons never approve an application; a legacy "Reject" still works. Buttons are removed after a decision or a stale press, so it can't be pressed twice.

**Scoring (`app/core.py`).**
- **Keyword families:** RAG, agent, agentic, LLM, fine-tuning, matched case-insensitively on word boundaries, including compounds and variants (Retrieval-Augmented Generation, AI agents, agent-based, multi-agent, autonomous agents, LLM-based, large language models, fine-tune/finetune/fine-tuned/fine tuning).
- **False-positive contexts removed:** travel/real-estate/customer-service/… agents, RAG status/rating (red-amber-green), LL.M./master of laws.
- **Floor:** 70 for 1 family, plus 5 per extra family, capped at 90. Applied only when:
  - the role is AI-relevant (tier `primary` or `secondary`; `possible` only with the keyword in the title),
  - all `search.yaml` gates pass (exclusions, location, age),
  - the job doesn't require Dutch.
- The breakdown exposes `ai_keywords`, `keyword_floor`, `base` and `role_tier`. The discovery engine stores the families in `jobs.ai_keywords`, and the dashboard and Telegram show them.

**CV worker (`app/cv_pipeline.py`):**
- **Daily limit:** default **-1 (no fixed limit)**. `CV_DAILY_LIMIT` remains an optional safety control.
- **Concurrency:** `CV_WORKER_CONCURRENCY` (default 1) parallel slots sharing one pacing lock, so `LLM_MIN_INTERVAL_SECONDS` still holds.
- **Cost protection:** a persistent daily request budget (`llm_usage`, `LLM_MAX_REQUESTS_PER_DAY`=250). At the budget, jobs wait without consuming attempts.
- **Rate limits:** a 429 / `RESOURCE_EXHAUSTED` pauses **all** workers (`LLM_RATE_LIMIT_COOLDOWN_SECONDS`=120, honouring `Retry-After`). Bounded retries with exponential backoff are unchanged.
- **Priority:** approved first, then manual, then score, then freshness.
- **Restart safety and idempotency:** stale `generating` jobs are reset on start; completed CVs are never regenerated.

**Applications (`app/applications/`):**
- **State machine:** `awaiting_cv`, `queued`, `submitting`, `submitted`, `confirmed`, `dry_run`, `manual_action_required`, `blocked`, `failed`, `cancelled`.
- **Before each attempt:**
  1. the approval must be active and match the *current* job and CV versions;
  2. the duplicate guard runs, by canonical apply URL, across sources and jobs;
  3. a read-only readiness check (`adapter.prepare`) runs;
  4. the mode gate applies.
- **Modes:** `APPLY_MODE=off` (default) gives `manual_action_required` with the apply link. `dry_run` builds the request and never sends. `live` only applies to live-capable adapters listed in `APPLY_LIVE_ADAPTERS`, and none exist.
- **Never sent twice:**
  - A unique partial index allows only one live application per job.
  - `submitting` at restart becomes `manual_action_required` ("outcome unknown"), never retried.
  - A crash during processing also becomes manual, never retried.
- **"Applied":** set only for `submitted`/`confirmed` **with an external confirmation id**. A submission without an id becomes `manual_action_required`.
- **Adapters:**
  - `ManualAdapter` is the catch-all.
  - `RecruiteeAdapter` reads the job's public form requirements (`GET /api/offers/{slug}`: CV/phone/cover-letter/photo options, required screening questions and their types) and checks that every required field can be filled from the verified profile: no guessing, exact or regex question match, boolean and choice validation. It is `live_capable=False`, and its `submit()` is never reached. Field *names*, never values, are recorded.
- **Profile:** `data/applicant_profile.yaml` (gitignored, private), used only if `verified: true`.
- **Rescore:** `python -m app.rescore [--dry-run]` recomputes `score` and `ai_keywords` for stored active jobs, without touching statuses, approvals, applications or CVs.

**Schema v4** (additive; `app/discovery/store.py`): tables `approvals`, `applications` (with the unique live index) and `llm_usage`; job columns `ai_keywords`, `action_notified_at`, `action_version`. Existing jobs are marked as already announced, so there's no flood of approval messages.

## 4. Files added or modified

| File | Change |
|---|---|
| `app/workflow.py` (new) | tiers, version tokens, `decide`, `on_cv_finished`, actionable messages, Telegram callback handler |
| `app/applications/__init__.py` (new) | state machine, `ApplicationWorker`, restart recovery, duplicate guard, outcome notifications |
| `app/applications/adapters.py` (new) | adapter interface, `ManualAdapter`, `RecruiteeAdapter` (read-only readiness) |
| `app/applications/profile.py` (new) | verified profile loading, explicit answer matching |
| `app/rescore.py` (new) | safe rescore CLI |
| `app/core.py` | `STRONG_AI_KEYWORDS`, false-positive filter, `strong_ai_keywords()`, keyword floor in `score_breakdown` |
| `app/cv_pipeline.py` | unlimited default, concurrency, LLM budget, rate-limit cooldown, approved-first priority, workflow hook, failure-only digest |
| `app/main.py` | Telegram callbacks → workflow, decision endpoint, workflow fields in `/api/jobs`, dashboard buttons, application worker, actionable messages after scans |
| `app/discovery/store.py`, `app/discovery/engine.py` | schema v4; store `ai_keywords` |
| `applicant_profile.example.yaml` (new), `.env.example`, `README.md` | configuration and documentation |
| `tests/test_keywords.py`, `tests/test_workflow.py` (new); `tests/test_cv_pipeline.py`, `tests/test_scoring.py` | tests (§7) |

## 5. Features implemented

| Feature | Implemented | Unit-tested | Live-verified |
|---|---|---|---|
| Both approval workflows + reject, shared backend | yes | yes | yes: dashboard API on a real data copy (§8) |
| Version-bound approvals, stale/repeat/duplicate protection, audit events | yes | yes | yes: stale gives 409, repeat gives "already" |
| Telegram actionable messages + secure callbacks | yes | yes (recorder) | **no**: Telegram disabled in all live checks |
| Strong AI keyword scoring + exposure | yes | yes (8 tests) | yes: 198 real jobs re-scored |
| Unlimited default, concurrency, LLM budget, rate-limit cooldown | yes | yes | partly: defaults observed in the running app; **no Gemini calls made** |
| Application state machine, worker, restart/crash safety | yes | yes (mock adapters) | **no real submissions** (by instruction) |
| Recruitee readiness check | yes | yes (mock HTTP) | yes: read-only GET of a real public job form |
| Live submission adapter | **no** (none permitted/verified) | live path tested with a mock adapter | no |
| Rescore CLI | yes | yes | yes: dry run on the real data copy |

## 6. Configuration changes (`.env`; all optional with safe defaults)

| Setting | Default | Purpose |
|---|---|---|
| `CV_DAILY_LIMIT` | `-1` (was 5) | optional cap on automatic CVs per day |
| `CV_WORKER_CONCURRENCY` | 1 | parallel CV generations |
| `LLM_MAX_REQUESTS_PER_DAY` | 250 | daily request budget |
| `LLM_RATE_LIMIT_COOLDOWN_SECONDS` | 120 | pause after a Gemini 429 |
| `APPROVAL_MIN_SCORE` | 40 | lower bound of the approval tier |
| `TELEGRAM_ACTIONS_PER_BATCH` | 8 | individual actionable messages per batch |
| `TELEGRAM_ALLOWED_USER_IDS` | empty | optional extra callback check |
| `APPLY_MODE` | `off` | `off` / `dry_run` / `live` |
| `APPLY_LIVE_ADAPTERS` | empty | adapters allowed to submit live |
| `APPLY_MAX_ATTEMPTS` | 3 | attempts per application |

- `CV_MIN_SCORE` is unchanged (70).
- New private file `data/applicant_profile.yaml` (template `applicant_profile.example.yaml`).
- `search.yaml` and `discovery.yaml` are unchanged.

## 7. Tests executed and results

`.venv/bin/python -m unittest discover -s tests` → **179 tests, OK** (PR #6 had 143; 36 new or extended).

- **`test_keywords.py` (8):** case-insensitivity and all listed variants and compounds; word boundaries (drag, storage, reagents, agentur, fragment, Agenda, …); non-AI meanings (travel agent, RAG status, LL.M.); breakdown exposure; a single keyword gives ≥70 for each of RAG/agent/agentic/LLM/fine-tune/finetune; multiple keywords raise priority within bounds; unusual titles need the keyword in the title; no floor for non-AI roles, sales, internships, wrong geography or Dutch-required jobs.
- **`test_workflow.py` (28):**
  - tiers and reference-only below 40;
  - ≥70 path: no message before the CV, then an actionable message with Approve & Apply/Reject, approval through the Telegram callback, repeated press says "already", and only cancel remains;
  - mode off ends in manual action with the URL, never applied, not re-notified;
  - stale tokens after CV or job changes; job change after approval stops the submission and offers re-approval;
  - 40-69 path: Approve CV & Apply, prioritized above higher-scored automatic jobs, CV generated and validated, queued with no second approval, submitted through a mock live adapter, job applied with confirmation id and events;
  - validation warnings pause (one message, with "Paused:" and "CV warnings:"); CV failure pauses without submitting; reject cancels the CV, application and approvals;
  - message cap plus summary, with the next batch continuing;
  - Telegram authorization (chat, user list), malformed data, legacy buttons;
  - dashboard decision endpoint (401, 409 stale, 200, `channel=dashboard`);
  - live only with a listed live-capable adapter; a submission without an id isn't applied; retries with backoff, then failed; blocked (CAPTCHA) never submits; duplicates (unique index; same vacancy from another source gives blocked); restart and crash never resubmit;
  - Recruitee: public requirements, missing PDF/profile/answers, unverified profile, `live_capable=False`, GET only, field names only;
  - worker: no fixed daily limit, LLM budget, rate-limit pause for all, concurrency 2 with each CV generated exactly once;
  - engine stores keywords; v4 migration keeps statuses and doesn't flood; rescore changes only the score and keywords.
- **Updated:** the CV pipeline tests now expect actionable messages (instead of the ready-CV digest) and reset the module-level cooldown; the scoring test exempts titles that contain a strong keyword, since the new rule supersedes the old one.

## 8. Live verification results

All live checks used copies of the production DB in a scratch directory. Telegram was not configured, no Gemini call was made, `APPLY_MODE=off`, and **no application or form submission was made anywhere**.

**Migration:** the copy migrated from v3 to v4; legacy statuses were preserved.

**Re-scoring the 198 real jobs** from the previous live run (production `search.yaml`):

| Tier | Before keyword priority | After |
|---|---|---|
| auto-CV (≥70) | 78 | **151** |
| approval (40-69) | 101 | **38** |
| reference (<40) | 19 | **9** |

- **Keyword families matched (jobs):** LLM 120, agent 98, agentic 79, RAG 47, fine-tuning 35.
- **Lifted to ≥70 by the floor:** 73 jobs (56 primary, 17 secondary).
- **Examples of lifted roles:** "Senior Architect, Enterprise Agentic Automation", "Software Engineer - AI Tooling Infrastructure", "Product Engineer, Voice AI", "Fullstack Engineer (AI & Data Platform)", "Senior Backend Engineer, AI-native", "Senior Python Developer (Fullstack, BE-Heavy)", "Community Engineer, Toqan (Prosus AI Platform)", "Staff SRE — AI Platform".
- **Score bands after:**

| Band | Jobs | Band | Jobs |
|---|---|---|---|
| 10s | 1 | 60s | 7 |
| 20s | 6 | 70s | 67 |
| 30s | 2 | 80s | 57 |
| 40s | 5 | 90s | 27 |
| 50s | 26 | | |

- The first version also lifted `possible`-tier generic roles (165 auto-CV). That's why the floor now requires the keyword in the title for that tier.

**Rescore dry run** on the copy (203 active jobs): 166 would change. Auto-CV goes from 80 to 155, approval from 101 to 38, reference from 22 to 10.

**Recruitee readiness check** (one read-only GET of a real public Conclusion job form):
- Required fields: name, email, phone, CV file, location choice.
- Missing (correctly reported): the profile, a PDF CV, and an answer to "Welke werklocatie heeft je voorkeur?".
- The adapter is `live_capable=False`. A Greenhouse job resolves to the manual adapter.

**App on a spare port** (copy, no `.env`):
- `/api/jobs` returns 203 jobs with `workflow`, `actions`, `token`, `ai_keywords`, `application_status` and `apply_url`.
- On a real 67-point job:
  - stale token gives **409**;
  - Approve CV & Apply gives **200 `cv_queued`**;
  - a repeat gives **`already`**;
  - the state is job `approved`, application `awaiting_cv`, CV `pending`, actions `[reject]`;
  - Reject gives **`rejected`**.
- `/api/cv-worker` shows a daily limit of -1, a budget of 250 and concurrency 1, and is paused with "GEMINI_API_KEY is not set" (expected: no key in this environment).

**Not live-verified:**
- Telegram delivery and button presses (unit-tested with a recorder);
- Gemini generation under the new worker settings;
- any real application submission (forbidden, and no live adapter exists);
- concurrency greater than 1 against Gemini.

## 9. Performance and resource considerations

- **Worker:** concurrency 1 by default. Each CV takes 1-2 Gemini calls, at least 15 s apart. The daily budget of 250 caps cost and quota use. A 429 pauses all slots instead of hammering the API.
- **First batch:** about 151 auto-tier jobs means roughly 2-3 hours of generation at 15-second pacing, spread over 1-2 days by the budget.
- **Dashboard:** `/api/jobs` now computes the workflow state per job (a few queries plus one hash of the CV file). For about 200 jobs that's fine; for thousands, consider caching the CV hash.
- **Application worker:** polls every 60 s, with no network calls unless an application is queued. The Recruitee readiness check is one GET.

## 10. Security and privacy checks

- **Never committed:** personal profile data (the profile lives in gitignored `data/`; only a placeholder template is committed); no secrets in code, `.env.example` has empty values; adapters record field names, never values.
- **Approval checks:** stale, unauthorized and repeated callbacks are refused (tested). Approvals are bound to job and CV hashes. Legacy approve buttons can't trigger applications.
- **No bypassing:** no CAPTCHA, login or anti-bot bypass anywhere. Blocked or unsupported systems become manual action.
- **Branch diff scan:** clean (see §16 commit list; the same pattern scan as PR #6 was run before pushing).
- **Production untouched:** schema v0, 5 jobs, service not restarted, hotel-bot untouched.

## 11. Known limitations

- No live submission adapter (see Decisions 2 and 3). The framework, guards and states are ready and tested with mocks.
- Recruitee question matching relies on your exact question text or regex. Screening questions in other languages need entries in your profile.
- The keyword floor makes most AI-adjacent roles auto-CV (76% of the live sample).
- Actionable Telegram messages are capped at 8 per batch, so a large backlog takes several batches (scans happen hourly, CV batches continuously).
- The pause alert and the summary-line deduplication are in memory, so they may repeat once after a restart.

## 12. Outstanding issues / decisions needed

See **Decisions needed** at the top: auto-CV volume, a verified submission path, a PDF renderer, the applicant profile, Dutch-required handling, and merge order plus deployment approval.

## 13. Deployment instructions (after PR #6 is merged and deployed, and this PR is approved)

1. Back up the database: `cp data/jobs.sqlite3 ~/job-hunter-backups/jobs-pre-pr7.sqlite3`. The app also backs up automatically before migrating.
2. Retarget PR #7 to `main` and merge. On the VM:
   `git stash push -- app/main.py deploy/job-hunter.service search.yaml && git merge --ff-only origin/main && git stash pop`
3. Run the tests: `.venv/bin/python -m unittest discover -s tests`.
4. Optionally create `data/applicant_profile.yaml` from the template. **Keep `APPLY_MODE=off`.**
5. Optionally run `.venv/bin/python -m app.rescore --dry-run`, then `python -m app.rescore` to apply the keyword priority to existing jobs.
6. Restart **only** `job-hunter.service`. On start: the schema migrates to v4 (additive). Existing jobs are marked announced. The CV worker processes the auto tier under the daily LLM budget. Actionable Telegram messages are sent at most 8 per batch.
7. Verify: the dashboard shows workflow, AI keywords and buttons; press Reject on one irrelevant job in Telegram to confirm the callbacks work.

## 14. Rollback instructions

- Stop the service, check out the PR #6 commit, and restart. The v4 tables and columns are additive, so the older code ignores them.
- Applications can't have been submitted (mode off, no live adapter).
- For a byte-exact rollback, restore the backup from step 1.

## 15. Recommended next steps

1. Decide on the auto-CV volume (Decision 1). If needed, I can add `KEYWORD_FLOOR_MODE=title_or_primary`.
2. Decide on the PDF renderer. With a PDF, the Recruitee readiness check can pass, and a supervised, owner-approved test of the apply endpoint can follow.
3. Fill in `data/applicant_profile.yaml`, including answers for common screening questions.
4. Consider an email-based "apply manually" helper (prefilled mailto/links) for systems that can't be automated.

## 16. PR URL and commits

- PR: https://github.com/majid-lotfian/job-hunter-agent/pull/7 (stacked on PR #6)
- `c82a44c`: approval workflows, keyword scoring, application framework
- `a83f447`: keyword floor needs a real AI role; rescore command
- Report and index: the commit that adds this file.

**Research sources (application APIs):**
- [Lever Postings API](https://github.com/lever/postings-api) (apply requires the employer's API key)
- [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html) (submission requires the employer's Basic-Auth key)
- [Recruitee API docs](https://apidocs.recruitee.com/) (the ATS API covers employer endpoints; the careers-site apply endpoint is not verified)
