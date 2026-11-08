# Job Hunter Agent — self-hosted AI job discovery, CV tailoring and application automation

An open-source, low-resource job discovery and review assistant. It monitors **~70 employer career boards** (Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee, Workable, Personio, Teamtailor; Workday opt-in) and **free job-board APIs** (Arbeitnow, The Muse, Himalayas, Jobicy, Remotive, Remote OK, Working Nomads; Adzuna/Jooble with optional keys) for AI/ML roles in the Netherlands, merges cross-source duplicates, scores matches, sends Telegram notifications, and exposes a lightweight FastAPI dashboard. For every new matching job (score ≥ `CV_MIN_SCORE`, default 70) a tailored HTML CV is **generated automatically** in the background from your private master CV with the Google Gemini API, validated, saved, and announced on Telegram for your review. An interactive Claude Code workflow remains available as an optional manual fallback.

**Current capabilities:** The agent discovers and ranks jobs, extracts applicant information from a private CV into a reviewable profile, tailors CVs to each vacancy without inventing experience, generates HTML/PDF documents, and offers Telegram/dashboard approval. For supported forms, an approved application can be completed and submitted by a Playwright browser worker, with confirmation checks and duplicate protection. **This is not a universal auto-apply tool:** platform support varies, CAPTCHA or login may block submission, and a successful click is not proof of employer receipt. Live submissions require explicit approval, a verified profile and opt-in configuration; `APPLY_MODE=off` is the default. See [Application modes and limitations](#application-modes-and-limitations). No commercial auto-apply subscription is needed, but GCP compute, APIs and storage may incur charges.


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

## Deploy as a separate service on a VM

On the VM, use a separate directory, venv, and systemd unit. Do not modify the existing price-checker cron/service. A sample unit is provided in `deploy/job-hunter.service`; replace `YOUR_LINUX_USER` with your VM Linux username, copy it to `/etc/systemd/system/`, then enable/start. The local browser backend does not require GitHub Actions or new GCP IAM permissions.

For a local browser, create a tunnel from your machine using your existing SSH access, e.g. `gcloud compute ssh price-checker --zone=us-central1-a --project=vacation-price-checker --tunnel-through-iap -- -L 8088:127.0.0.1:8088` and open `http://127.0.0.1:8088`.

## Automatic CV generation

When discovery stores a new job with score ≥ `CV_MIN_SCORE` (default 70), it is queued. A single background worker inside the app (so discovery and the dashboard stay responsive) then:

1. **Redacts** personal data from `data/master_cv.html` before anything leaves the VM: the whole `<header>`/`.contact` block (name, location, e-mail, phone, profile links), your name, e-mail addresses, phone numbers and **every URL/href** are replaced by placeholders such as `[[PII_3]]`. If anything that looks like an e-mail, phone number, `mailto:`/`tel:` or URL is still present, nothing is sent.
2. Sends only the redacted `<body>` plus the job posting (marked as untrusted data) to Gemini, with strict rules: the master CV is the only source of facts; never invent skills, employers, titles, dates, achievements or metrics; keep all sections, employer/role/date lines and HTML structure. The CSS is not sent at all.
3. **Restores** the original values locally and re-uses the master's `<head>`/`<style>` byte-for-byte, so styling, contact details and links are exactly as in your master CV.
4. **Validates** the result. Gemini is asked for structured JSON output (`responseJsonSchema`, falling back to plain JSON mode if a model rejects the schema). Malformed, empty or truncated replies are never used: the model gets one correction round, then the job is retried with the normal bounded backoff. A CV is rejected (never saved) if it adds links, scripts, event handlers, forms or other active elements, changes the CSS or name, drops the e-mail link, mangles placeholders, drops or renames a section, changes or drops an employer/role/date line, is less than half the master's length, or tailored nothing (one correction round, then `failed`). It is saved but marked **needs review** if numbers or technical terms from the job posting are not found in the master CV, or the length changed a lot.
5. Saves `data/cvs/job-<id>.html` (any previous file is backed up to `data/tailoring/job-<id>/`). After each batch, **one Telegram CV digest** lists every new outcome (ready / needs review / failed) with score, employer, review link and the job requirements your CV does not support. Approving/rejecting stays manual; submission is possible only through the separately gated application workflow.

## Approvals and applications

| Score | What happens |
|---|---|
| ≥ `CV_MIN_SCORE` (70) | CV generated automatically and validated → Telegram + dashboard: **Approve & Apply** / **Reject** |
| `APPROVAL_MIN_SCORE` (40) – 69 | no automatic CV → **Approve CV & Apply** / **Reject**; one approval covers CV generation, validation and the application (validation warnings or failures pause and ask again) |
| < 40 | dashboard only |

Telegram buttons and dashboard buttons call the same backend (`app/workflow.py`). Every decision is stored (`approvals`) and audited (`events`); an approval is bound to a version token of the job text and the CV file, so a changed job or CV never reuses an old approval. Callbacks are checked against `TELEGRAM_CHAT_ID` (and optionally `TELEGRAM_ALLOWED_USER_IDS`); repeated or stale presses do nothing. Individual actionable messages are capped per batch (`TELEGRAM_ACTIONS_PER_BATCH`); discovery stays a digest.

**Application submission** (`app/applications/`) runs only after an explicit approval. States: `awaiting_cv`, `queued`, `submitting`, `submitted`, `confirmed`, `dry_run`, `manual_action_required`, `blocked`, `failed`, `cancelled`. One live application per job and per canonical apply URL; an interrupted submission is never retried automatically; a job becomes `applied` only with an external confirmation id. Personal data comes only from your private, verified `data/applicant_profile.yaml` (template: `applicant_profile.example.yaml`). **`APPLY_MODE=off` by default**: the agent checks readiness and offers the supervised application flow. Most ATS submission APIs require employer credentials; the project also implements a browser-based Playwright worker for supported public application forms and a guarded Recruitee API adapter. See the detailed modes below. No CAPTCHA, login or anti-bot measure is ever bypassed.

**Daily limit (optional):** `CV_DAILY_LIMIT` (default -1 = no fixed limit) automatic CVs per calendar day in `CV_TIMEZONE` (default Europe/Amsterdam), enforced in the database across restarts. Eligible jobs stay queued for the next days, highest score first, then newest posting. Each job uses at most one slot, even if Gemini errors force retries. Manual *Generate CV* / CLI requests are processed first and are not counted. A critical failure (e.g. invalid API key) sends one alert.

**Statuses** (dashboard *CV* column, `python -m app.cv_pipeline --status`): `pending` → `generating` → `ready` | `needs_review` | `failed`.

- Temporary Gemini errors (HTTP 429 rate limit/quota, 5xx overload, timeouts, network) are retried with exponential backoff and jitter (`CV_RETRY_BASE_SECONDS` × 2ⁿ, capped by `CV_RETRY_MAX_SECONDS`, honouring `Retry-After`/`RetryInfo`). After `CV_MAX_ATTEMPTS` of them the job is **not** failed: it moves to `waiting_retry` with a persistent `next_attempt_at` (~10 min, doubling per cycle up to `CV_WAIT_RETRY_MAX_SECONDS`) and is retried automatically, also after a restart. It fails only after `CV_RETRY_GIVE_UP_HOURS` (default 72) of continuous temporary errors. A 429/5xx also sets a shared, persistent cooldown for all CV workers. Malformed/truncated replies stay bounded (`failed` after `CV_MAX_ATTEMPTS`); 401/403/404 halt the worker until restart or a dashboard Retry.
- **PDF copies:** right after a tailored HTML CV is saved, the worker renders `data/cvs/job-<id>.pdf` from it (A4, the CV's own print CSS, WeasyPrint in a short-lived child process with a timeout and memory cap; no network access). The HTML stays the source of truth; Gemini is never called for a PDF. Failures are recorded (`cv_jobs.pdf_status/pdf_error`, `events`) and retried on their own (`PDF_MAX_ATTEMPTS`, backoff), or on request: dashboard **Retry PDF**, or `.venv/bin/python -m app.pdf --job-id 7` / `--missing`. A valid PDF of the current HTML is never re-rendered. The dashboard offers **PDF ⤓** (same login). Applications attach only a PDF whose recorded HTML hash matches the current CV; otherwise nothing is submitted.
- **Supervised applications (default while `APPLY_MODE` is not `live`):** **Prepare Application** (dashboard or Telegram) is the version-bound approval. The worker then checks the vacancy (Recruitee/Greenhouse APIs), makes sure a current CV PDF exists (generating the CV first if needed), reads the employer's form fields and screening questions (Recruitee, Greenhouse; generic list elsewhere), fills what your verified profile (`data/applicant_profile.yaml`) covers and lists what is missing. Screening questions are answered only from your explicit `answers`. Telegram says once when it is ready; the **Review Application** page (`/applications/<id>`) shows the CV (HTML + PDF), employer, position, application page, every field with the value to enter, missing information and the status, with **Open Application Page**, **Re-check**, **Mark as Submitted** (needs your confirmation text + checkbox) and **Cancel**. You submit in your own browser; CAPTCHA/login are never bypassed and nothing is recorded as submitted without your confirmation. Profile: `.venv/bin/python -m app.applications.profile --init-from-cv | --check | --show`.
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


## What the system does

```text
Private master CV + job preferences
        |
        +--> Applicant profile draft --> owner verifies personal/application answers
        |
Job discovery (employer ATS + permitted job APIs)
        |
Normalize, deduplicate, filter, score and rank
        |
Gemini: job-specific HTML CV (factual tailoring, PII redaction)
        |
Validation --> A4 PDF (WeasyPrint)
        |
Telegram notification / authenticated dashboard
        |
HUMAN GATE: inspect CV, answer screening questions, approve or reject
        |
Application orchestrator + SQLite audit/duplicate guards
        |
Supported ATS --> Playwright browser worker (local VM or optional Cloud Run Job)
        |
Fill + verify fields --> upload current PDF --> submit at most once
        |
Employer confirmation? --> record confirmed / blocked / uncertain; notify owner
```

The applicant profile is a **separate, owner-verified file**: CV extraction creates a draft, not authorization to invent missing details or silently answer screening questions. The approval gate prevents unsolicited submissions and gives the applicant an opportunity to catch CV mistakes. Telegram lets you review and approve from a phone, but a review link to the dashboard may require secure access to your VM.

### Technology stack

| Layer | Implementation |
| --- | --- |
| Backend and dashboard | Python, FastAPI, Uvicorn, Jinja2 |
| Scheduling | APScheduler, periodic source scans |
| Job sources | Greenhouse, Lever, Ashby, Recruitee and other configured ATS boards; permitted job APIs |
| Job matching | Deterministic relevance rules in `search.yaml` (not an ATS score) |
| LLM-based CV tailoring | Google Gemini REST API via `httpx`; factual safeguards and local PII restoration |
| CV documents | HTML, WeasyPrint PDF rendering |
| Application automation | Playwright/Chromium browser worker; limited Recruitee API adapter |
| Workflow and audit | SQLite application state, approvals, events and duplicate prevention |
| Notifications and approvals | Telegram Bot API and authenticated dashboard |
| Hosting | Google Compute Engine VM with systemd; optional Cloud Run Jobs + private Cloud Storage for browser execution |

## New user guide: Google Cloud VM to working agent

This is a **self-hosted** application, not a hosted service. The steps below are a general deployment recipe; review the repository configuration and cloud pricing before enabling billable resources.

### 1. Create a Google Cloud project and VM

1. Sign in at [Google Cloud Console](https://console.cloud.google.com/), create or select a project, and enable billing if required.
2. Enable the **Compute Engine API**. Open **Compute Engine → VM instances → Create instance**.
3. Choose a region/zone convenient for you, a supported Linux image (Ubuntu or Debian), and a machine type. **e2-medium (4 GB RAM)** is a practical starting point if you intend to run Chromium and the app on the same VM; smaller machines may run out of memory. This machine type is **not guaranteed to be free-tier eligible**.
4. Use an adequately sized persistent boot disk, enable SSH access, and **do not expose the dashboard port 8088 to the public internet**. Do not grant broad IAM roles or add unrelated firewall openings.
5. Create the VM and connect using the Console **SSH** button. Keep track of the instance name, project ID, zone and Linux username.

The project and VM names in examples elsewhere in this repository are from one deployment; replace them with **your own**. Stopping a VM interrupts scheduled discovery and Telegram polling; persistent disk normally survives a normal stop.

### 2. Install prerequisites and clone the repository

On your **Linux VM**:

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip libpango-1.0-0
git clone https://github.com/majid-lotfian/job-hunter-agent.git
cd job-hunter-agent
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
mkdir -p data
```

The PDF renderer needs compatible system libraries. If installation fails, consult the [WeasyPrint installation guide](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#installation). Browser automation also needs **Playwright and Chromium**, which are not installed by the base `requirements.txt`; see [Browser worker setup](#browser-worker-setup). Use a supported Python version and verify the test suite before deployment.

### 3. Set up Gemini and Telegram

**Gemini:** Create an API key in [Google AI Studio](https://aistudio.google.com/apikey). Put it in `.env` as `GEMINI_API_KEY`; keep `LLM_PROVIDER=gemini`. Confirm the selected `LLM_MODEL` is available to your key using `.venv/bin/python -m app.cv_pipeline --check`. API free-tier eligibility, quotas, model names and data-use terms can change.

**Telegram:** Open [@BotFather](https://t.me/BotFather), create a bot with `/newbot`, and copy its token to `TELEGRAM_BOT_TOKEN`. Start a chat with your new bot; determine the numeric chat ID using Telegram's Bot API `getUpdates` method after sending the bot a message, then set `TELEGRAM_CHAT_ID`. Optionally restrict button presses with `TELEGRAM_ALLOWED_USER_IDS`. **Keep bot tokens, chat IDs and API keys private**; never commit `.env`. Only one running app instance should poll a given bot.

**Dashboard:** Generate a strong random `DASHBOARD_TOKEN` and place it in `.env`. The dashboard listens on localhost by default. Set `APP_BASE_URL` only to an address you can access securely; a localhost-only URL will not open directly on a remote phone.

### 4. Supply your CV, job preferences and applicant profile

1. Copy your factual, current master **HTML** CV to `data/master_cv.html`. This is the tailoring source; the app does not directly accept an arbitrary PDF or DOCX as the master CV without conversion to the supported HTML format.
2. Edit `search.yaml` for role titles, skills, target locations and scoring; review `discovery.yaml` to enable/disable sources and adjust scanning intervals.
3. Create a **draft** applicant profile from your CV:

```bash
.venv/bin/python -m app.applications.profile --init-from-cv
.venv/bin/python -m app.applications.profile --check
```

4. Open `data/applicant_profile.yaml` privately. Verify your name, email, phone, location, work authorization, links, languages and any other extracted values. Add answers to screening questions only when you know they are accurate and you agree to provide them. See `applicant_profile.example.yaml` for the schema. **Only after checking everything**, set `verified: true` and rerun `--check`.
5. Do not commit `data/`, personal CVs, the SQLite database, tokens, or employer-specific applicant answers.

### 5. Start locally on the VM and check health

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m app.cv_pipeline --check
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8088
```

In another SSH session, verify `curl http://127.0.0.1:8088/health`. Access the dashboard through an **SSH port-forward** from your own computer, using your own VM/project/zone:

```bash
gcloud compute ssh YOUR_VM --project=YOUR_PROJECT --zone=YOUR_ZONE -- -L 8088:127.0.0.1:8088
```

If your setup requires Identity-Aware Proxy tunneling, add `--tunnel-through-iap` before `--` and configure IAP permissions. Visit `http://127.0.0.1:8088` locally. **Do not open port 8088 to everyone.**

### 6. Keep it running with systemd

Edit `deploy/job-hunter.service`: replace **every** `YOUR_LINUX_USER` placeholder with your VM user and check the working directory. Then:

```bash
sudo cp deploy/job-hunter.service /etc/systemd/system/job-hunter.service
sudo systemctl daemon-reload
sudo systemctl enable --now job-hunter.service
sudo systemctl status job-hunter.service
sudo journalctl -u job-hunter.service -n 100 --no-pager
```

Keep other services and their units unchanged. The service restarts on failure and starts on boot; it does not require an open SSH session.

### 7. Use Telegram approvals

With discovery enabled, the agent sends matches and tailored CV notifications. Review the job description and generated CV before selecting **Approve & Apply** or **Reject**. Approval is tied to the specific job/CV version; changed content requires fresh approval. For missing screening answers, inspect the authenticated application review page and complete your profile. You can keep `APPLY_MODE=off` indefinitely and submit manually.

### 8. Apply manually from your phone (Telegram)

Whenever the agent cannot submit a job itself (CAPTCHA, unsupported form, missing answers, `APPLY_MODE=off`, or an unclear browser outcome), the prepared application is sent to Telegram **once**. This includes a live attempt that a CAPTCHA / human-verification challenge stopped: the agent never solves it, keeps the blocked outcome and its evidence (screenshot hash, evidence folder, whether Submit had been clicked) in the audit history, and hands the application to this workflow automatically. Older CAPTCHA-blocked applications can be handed over with `python -m app.applications.manual --route-blocked <application id>`. The message contains:

- the current, validated tailored CV as a real **PDF document** (download or share it straight into the employer's upload field);
- company, job title, match score, the direct application URL, why it is not automatic, and any missing screening questions;
- buttons: **🌐 Open Application** (opens the employer's form), **✅ I Submitted This Application**, **⏳ Submit Later**.

To record your application, tap **I Submitted This Application** after you have really submitted on the employer's site. The bot asks *"Did you successfully submit this application on the employer's website?"*; tap **Confirm** (or **Cancel**). The application is then marked **Manually submitted**:
- status `submitted`, with `submitted_at`;
- the source "user-reported via Telegram";
- `employer_verified=false`. No employer reference is invented.

The original message shows "✅ Manually submitted", the dashboard shows the same reason, and the job becomes `applied`. Future approvals, automatic submissions and repeated confirmations are refused.

**Submit Later** keeps the application open without reminders; the buttons in that message keep working. Buttons are bound to the application's CV and job version. After a CV or posting change they are rejected as outdated, and only `TELEGRAM_CHAT_ID` / `TELEGRAM_ALLOWED_USER_IDS` can use them.

These buttons are separate from **Approve & Submit** (`wf:` callbacks, autonomous submission): a manual confirmation never queues anything, never starts the browser worker and never changes `APPLY_MODE`. To list or (re)send these messages:

```bash
.venv/bin/python -m app.applications.manual --list
.venv/bin/python -m app.applications.manual --notify <application id>
```

## Application modes and limitations

| Mode | Behavior | Appropriate use |
| --- | --- | --- |
| `APPLY_MODE=off` | No automated live submission; readiness/review and manual application | Default and safest onboarding |
| `APPLY_MODE=dry_run` | Prepare/test the application without clicking final Submit | Verify form support, PDF and fields |
| `APPLY_MODE=live` | Permits actual submission **only** for approved jobs through explicitly enabled live adapters | Opt-in, after profile and CV review |

A live browser application additionally requires `APPLY_LIVE_ADAPTERS=browser` and a configured `BROWSER_WORKER`. Never enable live mode just to test installation. The browser worker reads employer form requirements, fills known fields, checks values and file attachments, and clicks Submit **at most once** when permitted. Only an employer confirmation counts as success; blocked and uncertain outcomes are recorded without blindly retrying. No CAPTCHA solving, stealth bypass or unauthorized login automation is included.

**Platform coverage is conditional:** discovery supports more sources than submission. The browser integration has been developed primarily for Greenhouse-style hosted forms and selected Recruitee forms; Ashby, Lever and other ATS may require additional form readers. Some employers add custom widgets, interactive CAPTCHA, login, consent forms, geographic restrictions or mandatory screening questions. A page loading successfully is not evidence that its submit-time bot checks will accept an application. Respect each employer's terms and do not send unwanted or duplicate applications.

### Browser worker setup

**Option A — run on the same VM (simplest with sufficient RAM):**

The local worker requires a Python environment with the Playwright package and its Chromium browser. Install it **on the VM**, not on your personal laptop:

```bash
.venv/bin/python -m pip install playwright==1.63.0
.venv/bin/python -m playwright install --with-deps chromium
```

Check the actual worker configuration in `app/applications/browser.py` and set `BROWSER_WORKER=local` in `.env`. If the worker uses a different interpreter, configure `BROWSER_WORKER_PYTHON` accordingly. Restart the service after changing `.env`. Test the browser worker against mock forms before any live run. The local option shares RAM/CPU with discovery and PDF generation.

**Option B — isolated Cloud Run Job (advanced, potentially billable):**

The repository includes `worker/Dockerfile`, `worker/setup_cloud_run.sh` and a cloud dispatcher. Review the script **before** running it from a checked-out repository in Google Cloud Shell. It provisions Cloud Run, Artifact Registry, Cloud Build, a private short-lived Cloud Storage bucket, service accounts and a **service-account key**. This creates cloud resources and may incur charges. Its example defaults are deployment-specific: override `PROJECT`, `REGION`, `BUCKET`, `JOB` as needed. Check organization policy and prefer keyless credentials where possible; if using the script's key, restrict permissions, store it outside Git and rotate/delete it when no longer needed. The cloud path was implemented but should be tested in your project before production use.

After setup, configure `BROWSER_WORKER=cloud_run`, `BROWSER_WORKER_BUCKET`, `BROWSER_WORKER_JOB`, `BROWSER_WORKER_REGION`, and the isolated gcloud credential configuration on the VM. Cloud Run should run **one task with zero retries** so an ambiguous submission is never automatically duplicated.

### Practical checklist before any real submission

- Confirm the job is open and you genuinely want to apply.
- Review the tailored HTML/PDF against your real work history; correct any unsupported claims.
- Verify the private applicant profile and explicitly answer all required screening questions.
- Confirm the form is supported and a dry run passes.
- Approve the **specific** job/CV version through Telegram or the dashboard.
- Enable the required live mode/adapter deliberately; monitor the result and disable live mode again if you only intended one application.
- Treat `blocked` or `uncertain` as **not confirmed**, and check with the employer before trying again.

## Cost, privacy and operating notes

- **No subscription to an auto-apply SaaS is required.** Telegram bots can be created without a paid subscription, and some Gemini/Google Cloud services offer free quotas. However, a Compute Engine VM (especially e2-medium), persistent disk, egress, Cloud Run, Artifact Registry and paid API usage may cost money. Set budget alerts in Google Cloud Billing and check current pricing.
- CV tailoring redacts personal identifiers before Gemini requests and restores them locally. An approved live application necessarily sends the required personal details and PDF to the employer. Keep `.env`, `data/` and credentials private, restrict VM access and back up the SQLite database securely.
- The agent is not an ATS score predictor, does not guarantee interviews, and does not guarantee every employer accepts headless browser submissions.
- For troubleshooting, inspect `sudo journalctl -u job-hunter.service -n 100 --no-pager`, `/health`, source-health dashboard, CV status CLI and application state; avoid sharing unredacted tokens, CVs or application screenshots publicly.

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
- `app/applications/`: supervised application preparation, verified applicant profiles, guarded submission adapters, browser dispatch and state machine.
- `worker/`: Playwright browser worker, CAPTCHA probe, mock-form tests and optional Cloud Run packaging.
- `app/pdf.py`: A4 PDF generation and validation of the current tailored HTML CV.
- `app/main.py`: dashboard, API (serves CVs, queues generation; never generates during a request), hourly APScheduler scan, CV worker, Telegram long polling.
- `data/master_cv.html`: private source HTML CV, required for CV generation (never commit).
- `profile.yaml`: legacy template data; no longer used by the HTML CV generator.
- `discovery.yaml`: discovery sources (verified employer boards, job APIs), freshness, lifecycle and notification settings.
- `search.yaml`: scoring/matching rules (plus legacy board lists, still honoured).
- `.env`: private settings (never commit).
- `data/`: local SQLite database, tailored HTML CVs (`data/cvs/`) and tailoring workspaces (`data/tailoring/`) — never commit.

Job boards can change or rate-limit their public endpoints. A failing source is recorded in source health and never stops the others. Source URLs are not a promise of live availability. Keep monitored board counts reasonable and comply with provider terms.
