# Job Hunter Agent — self-hosted MVP

An open-source, low-resource job discovery and review assistant. It monitors **~70 employer career boards** (Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee, Workable, Personio, Teamtailor; Workday opt-in) and **free job-board APIs** (Arbeitnow, The Muse, Himalayas, Jobicy, Remotive, Remote OK, Working Nomads; Adzuna/Jooble with optional keys) for AI/ML roles in the Netherlands, merges cross-source duplicates, scores matches, sends Telegram notifications, and exposes a lightweight FastAPI dashboard. For every new matching job (score ≥ `CV_MIN_SCORE`, default 70) a tailored HTML CV is **generated automatically** in the background from your private master CV with the Google Gemini API, validated, saved, and announced on Telegram for your review. An interactive Claude Code workflow remains available as an optional manual fallback.

**Important limits:** This is a working MVP, not a universal job search engine. It does **not** search LinkedIn, Indeed, Glassdoor or other platforms that offer no permitted API (no scraping, no browser automation; see `docs/discovery/SOURCES.md`). It **never** submits applications (you review and approve every CV and apply yourself), does not generate cover letters, or calculate genuine ATS scores. The displayed score is a transparent rule-based relevance score. Telegram approval approves a *draft*, not an application submission. `applied` status must be recorded manually after confirmed external submission. No credentials are embedded in the code.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

1. Place your private, factual master HTML CV at `data/master_cv.html` on the VM. This path is gitignored: **never commit your personal CV**. It is the only factual source for tailored CVs and is never modified (see *Automatic CV generation* below).
2. Review `discovery.yaml` (sources, verified employer boards, freshness and notification settings) and `search.yaml` (scoring: titles, keywords, `min_score`). Boards listed under `search.yaml` `sources:` are still scanned too.
3. Set a strong `DASHBOARD_TOKEN` in `.env`. For automatic CVs, set `GEMINI_API_KEY` (and optionally `LLM_MODEL`, `CV_MIN_SCORE`) and run `.venv/bin/python -m app.cv_pipeline --check`. Optional: configure `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to receive notifications and approve/reject via Telegram. **Only run one app instance with Telegram polling.**
4. Run `uvicorn app.main:app --host 127.0.0.1 --port 8088`. Visit `http://localhost:8088` via SSH tunnel and log in with your `DASHBOARD_TOKEN`.

**Do not expose port 8088 publicly.** See *Dashboard security* below.

## Safe VM deployment, separate from price checker

On the VM, use a separate directory, venv, and systemd unit. Do not modify the existing price-checker cron/service. A sample unit is provided in `deploy/job-hunter.service`; replace `YOUR_LINUX_USER` with your VM Linux username, copy it to `/etc/systemd/system/`, then enable/start. This does not require GitHub Actions or new GCP IAM permissions.

For a local browser, create a tunnel from your machine using your existing SSH access, e.g. `gcloud compute ssh price-checker --zone=us-central1-a --project=vacation-price-checker --tunnel-through-iap -- -L 8088:127.0.0.1:8088` and open `http://127.0.0.1:8088`.

## Automatic CV generation

When discovery stores a new job with score ≥ `CV_MIN_SCORE` (default 70), it is queued. A single background worker inside the app (so discovery and the dashboard stay responsive) then:

1. **Redacts** personal data from `data/master_cv.html` before anything leaves the VM: the whole `<header>`/`.contact` block (name, location, e-mail, phone, profile links), your name, e-mail addresses, phone numbers and **every URL/href** are replaced by placeholders such as `[[PII_3]]`. If anything that looks like an e-mail, phone number, `mailto:`/`tel:` or URL is still present, nothing is sent.
2. Sends only the redacted `<body>` plus the job posting (marked as untrusted data) to Gemini, with strict rules: the master CV is the only source of facts; never invent skills, employers, titles, dates, achievements or metrics; keep all sections, employer/role/date lines and HTML structure. The CSS is not sent at all.
3. **Restores** the original values locally and re-uses the master's `<head>`/`<style>` byte-for-byte, so styling, contact details and links are exactly as in your master CV.
4. **Validates** the result. Gemini is asked for structured JSON output (`responseJsonSchema`, falling back to plain JSON mode if a model rejects the schema). Malformed, empty or truncated replies are never used: the model gets one correction round, then the job is retried with the normal bounded backoff. A CV is rejected (never saved) if it adds links, scripts, event handlers, forms or other active elements, changes the CSS or name, drops the e-mail link, mangles placeholders, drops or renames a section, changes or drops an employer/role/date line, is less than half the master's length, or tailored nothing (one correction round, then `failed`). It is saved but marked **needs review** if numbers or technical terms from the job posting are not found in the master CV, or the length changed a lot.
5. Saves `data/cvs/job-<id>.html` (any previous file is backed up to `data/tailoring/job-<id>/`), and sends a Telegram message with the review link and the job requirements your CV does not support. Approving/rejecting stays manual, and nothing is ever submitted.

**Statuses** (dashboard *CV* column, `python -m app.cv_pipeline --status`): `pending` → `generating` → `ready` | `needs_review` | `failed`.

- Temporary API errors (HTTP 429 quota, 5xx, timeouts, network, malformed/truncated JSON replies) are retried with bounded exponential backoff (`CV_RETRY_BASE_SECONDS` × 4ⁿ, capped by `CV_RETRY_MAX_SECONDS`, honouring `Retry-After`), at most `CV_MAX_ATTEMPTS` times, then `failed`.
- Configuration problems (missing/invalid `GEMINI_API_KEY`, unknown model, missing master CV) pause the worker without using up attempts. The dashboard shows the reason.
- `ready` and `needs_review` CVs are **never regenerated automatically**. A tailored CV created with Claude Code is never overwritten.
- **Legacy placeholder CVs** (files from older versions without the tailoring marker) are detected: for jobs above the threshold they are queued and replaced (the old file is backed up). Below the threshold they show as `legacy` with a *Generate CV* button.
- The dashboard's *Generate CV* / *Retry CV* button queues a job of any score (not available for completed CVs). To deliberately regenerate a completed CV, run `.venv/bin/python -m app.cv_pipeline --job-id ID --force`.

```bash
.venv/bin/python -m app.cv_pipeline --check            # API key valid? model available? lists Flash models for your key
.venv/bin/python -m app.cv_pipeline --status           # CV status per job (errors / review warnings)
.venv/bin/python -m app.cv_pipeline --job-id 7         # queue a job
.venv/bin/python -m app.cv_pipeline --run-once         # process one due job in the foreground
```

### Gemini API: model, free tier and terms

- **Model:** `LLM_MODEL` (default `gemini-3.6-flash`, a stable Flash model). Other options are `gemini-3.5-flash-lite` (cheaper/faster) or `gemini-3.8-flash` (newest, not yet marked stable). Avoid `gemini-2.5-flash`/`gemini-2.5-pro`, which are deprecated. `--check` confirms what your key can use.
- **Free tier:** Flash and Flash-Lite models have a free tier in Google AI Studio with no billing. The per-project limits are shown in AI Studio (https://aistudio.google.com/rate-limit). One request per CV, paced by `LLM_MIN_INTERVAL_SECONDS`, is well within them. Quota errors (429) are retried as described above.
- **Data use:** for unpaid use, Google's Gemini API terms allow using prompts to improve its products, including human review, but for users in the **EEA, Switzerland and the UK** the paid-service data terms apply to unpaid quota as well. Personal contact data is redacted in any case. Check the current terms yourself: https://ai.google.dev/gemini-api/terms.

## Optional fallback: tailoring a CV with Claude Code

Use this to tailor or revise a CV by hand, e.g. when an automatic CV failed or needs review, or for a job below the threshold. Finalizing records the CV as `ready` (or `needs_review` if there were warnings), and the automatic pipeline will not overwrite it.

You tailor one per job in an interactive Claude Code session that you start yourself. Claude Code is never run unattended.

On the VM, from the project directory (requires the `claude` CLI logged in with your subscription):

```bash
cd ~/job-hunter-agent
.venv/bin/python -m app.tailor_cv --list             # job IDs and CV status (none / legacy / tailored)
.venv/bin/python -m app.tailor_cv --job-id 1         # 1. prepare data/tailoring/job-1/ and print the prompt
claude "Read data/tailoring/job-1/INSTRUCTIONS.md and follow it exactly."
                                                     # 2. review/approve Claude's edits; it writes draft.html
.venv/bin/python -m app.tailor_cv --job-id 1 --finalize   # 3. validate, save, update SQLite
```

Use `--job-id 1 --launch` to have step 1 open the interactive `claude` session for you.

- **Step 1** writes `JOB.md` (job details; the scraped description is marked as untrusted data, not instructions) and `INSTRUCTIONS.md` (strict tailoring rules: rewrite the profile for the role, reorder skills/projects/bullets by relevance, use only facts from the master CV, keep CSS, structure, contact details and links).
- **Step 2** — Claude Code writes `data/tailoring/job-1/draft.html` and summarises its changes plus any job requirements your CV does not support. Ask for revisions in the same session. Open the draft in a browser to review it.
- **Step 3** rejects the draft if the `<style>` CSS changed, the name changed, scripts/external resources or links not in the master were added, the email link is missing, or nothing was tailored. It warns about master links that were dropped, missing sections, and **numbers not present in the master CV** (possible invented metrics). On success it saves `data/cvs/job-1.html`, sets the job's `cv_path`, and the dashboard *HTML CV* button serves it.

**No accidental overwrites:** if `data/cvs/job-ID.html` already exists, both steps refuse unless you pass `--force`; the previous file is then backed up to `data/tailoring/job-ID/previous-*.html`. The master CV is never written; step 3 warns if it changed since step 1. Always read the final CV before sending it.

CV files created by older versions of this app (master copies / templates) show as `legacy (untailored)` in `--list`; replace them with `--force` when you want a tailored version. `OPENAI_API_KEY`/`OPENAI_MODEL` are no longer used and should be removed from `.env`.

## Dashboard security

- **Login:** unauthenticated requests to `/` and to CV URLs redirect to `/login`. JSON API calls without a session return `401`. `/health` is the only public endpoint.
- **Session cookie:** the `DASHBOARD_TOKEN` is submitted only in the login form's POST body. It never appears in URLs, and the old `?token=` links and `X-Dashboard-Token` header are no longer accepted. Successful login sets `jh_session`, a signed, expiring cookie (`HttpOnly; Secure; SameSite=Lax; Path=/`, default 12 h via `SESSION_HOURS`). The token itself is never stored in the cookie.
- **Rotation:** sessions are signed with a key derived from `DASHBOARD_TOKEN`, so changing the token in `.env` and restarting the service logs everyone out. *Log out* clears the cookie in your browser.
- **Other protections:**
  - POST requests from another site are rejected.
  - After 5 failed logins in 5 minutes, further attempts are refused for a while.
  - Login redirects only go to paths on this site.
  - Protected pages are sent with `Cache-Control: no-store`.
  - Submitted tokens are never logged, and any `token=` query value is redacted from uvicorn access logs.
- **SSH tunnel / HTTP:** browsers (Chrome, Firefox, Edge) treat `http://localhost` and `http://127.0.0.1` as secure contexts, so the `Secure` cookie works through the tunnel at `http://localhost:8088`. If your browser refuses to keep the session over plain-HTTP localhost (some Safari versions), set `COOKIE_SECURE=false` in `.env` and restart. Traffic between your browser and the tunnel is plain HTTP on your own machine, and the SSH tunnel encrypts it in transit to the VM. Do not put the dashboard behind a public reverse proxy without HTTPS.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests use a temporary database and files; they never touch `data/` or call external services (the Gemini API is replaced by fakes / a mock transport).

## Job discovery

Every `DISCOVERY_INTERVAL_MINUTES` (default 60) the app scans all sources in `discovery.yaml` (each source also has its own minimum interval, e.g. Remotive at most 4×/day per its terms). Overlapping scans are prevented. Per source:

1. **Fetch** through one shared HTTP client: pooled connections, per-host pacing, timeouts, retries with exponential backoff and jitter, `Retry-After`, and a 15 MB response cap. Lever boards are paginated (and jobgether is filtered server-side to the Netherlands: 47 MB → ~1.5 MB per scan).
2. **Skip unchanged** postings already known (by source id + change marker), and skip re-fetching details of postings that were rejected before and have not changed.
3. **Classify deterministically** (no LLM): target-role tiers *primary* (AI/ML/LLM engineer, applied/AI scientist, NLP, ...), *secondary* (MLOps, AI platform, data scientist with ML, ...), *possible* (unusual title but strongly AI/ML description; kept, low priority); excludes internships, junior, sales, PM, etc. Location must be the Randstad (or elsewhere in NL, configurable), or remote/hybrid open to the Netherlands. Dutch language requirements are detected (*dutch required / preferred / likely*), not assumed away.
4. **Score** with the existing `search.yaml` rules (unchanged).
5. **Deduplicate** across sources (see below) and store with provenance (`job_sources`).
6. **Lifecycle:** `first seen` / `last seen`; a job on a complete employer listing is `closed` only after 2 successful scans without it (failed, empty or truncated scans never close jobs); jobs known only from search/aggregator feeds become `stale` after 21 days unseen; reappearing jobs reopen.
7. **Source health** per source: status (`ok`, `empty`, `partial`, `failed`, `rate_limited`, `skipped`, `disabled`, `not_configured`), counts (fetched/accepted/rejected/new/duplicates/errors), last error, duration — in the dashboard *Sources* panel and `/api/discovery/health`.

New logical jobs go to the existing CV queue (`CV_MIN_SCORE`, default 70) once, and to Telegram once (score ≥ `notifications.min_score`; more than 10 per scan, or the first scan of a newly added source, become one digest message).

**Deduplication** (conservative; provenance kept, the employer's own posting/apply link preferred): (1) same source + id; (2) same canonical URL (tracking parameters removed); (3) same ATS job id found in any URL (e.g. an aggregator copy linking to a Greenhouse job); (4) same normalized employer (aliases, B.V./N.V. etc.) **and** title **and** seniority **and** compatible location, never two different ids from the same board, and similar descriptions when both are available. Different seniority, city or requisition stays separate.

Sources, coverage, research notes and limitations: `docs/discovery/SOURCES.md`. Audit, design, test and live-test results: `docs/discovery/DEVELOPMENT_REPORT.md`.

## Architecture

- `app/core.py`: SQLite, scoring (`score_job`), notifications, status changes, CV path helpers; `discover()` runs the discovery engine.
- `app/discovery/`: multi-source discovery: `sources/` adapters, `http.py` (polite client), `normalize.py`, `relevance.py`, `dedupe.py`, `store.py` (migrations, provenance, lifecycle, health), `engine.py`.
- `app/cv_pipeline.py`: automatic CV generation: queue, background worker, redaction, validation, Telegram review notifications, CLI.
- `app/llm.py`: configurable LLM provider (Gemini REST API via `httpx`; key from `.env`), error classification for retries.
- `app/tailor_cv.py`: optional manual fallback: interactive Claude Code tailoring (prepare / finalize / list), shared CV validation.
- `app/main.py`: dashboard, API (serves CVs, queues generation; never generates during a request), hourly APScheduler scan, CV worker, Telegram long polling.
- `data/master_cv.html`: private source HTML CV, required for CV generation (never commit).
- `profile.yaml`: legacy template data; no longer used by the HTML CV generator.
- `discovery.yaml`: discovery sources (verified employer boards, job APIs), freshness, lifecycle and notification settings.
- `search.yaml`: scoring/matching rules (plus legacy board lists, still honoured).
- `.env`: private settings (never commit).
- `data/`: local SQLite database, tailored HTML CVs (`data/cvs/`) and tailoring workspaces (`data/tailoring/`) — never commit.

Job boards can change or rate-limit their public endpoints. A failing source is recorded in source health and never stops the others. Source URLs are not a promise of live availability. Keep monitored board counts reasonable and comply with provider terms.
