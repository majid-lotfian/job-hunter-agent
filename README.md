# Job Hunter Agent — self-hosted MVP

An open-source, low-resource job discovery and review assistant. It monitors configured **public Greenhouse and Lever job boards**, scores matches, sends Telegram notifications, and exposes a lightweight FastAPI dashboard. Job-specific HTML CVs are tailored **on demand, interactively with Claude Code** (your Claude subscription) — no paid AI API is used and nothing is generated automatically.

**Important limits:** This is a working MVP, not a universal job search engine. It does **not** yet search LinkedIn, Indeed, Glassdoor, Ashby, Workday, SmartRecruiters, or Adzuna. It does **not** automatically submit applications, generate LLM-written cover letters, or calculate genuine ATS scores. The displayed score is a transparent rule-based relevance score. Telegram approval approves a *draft*, not an application submission. `applied` status must be recorded manually after confirmed external submission. No credentials are embedded in the code.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

1. Place your private, factual master HTML CV at `data/master_cv.html` on the VM. This path is gitignored: **never commit your personal CV**. It is the only factual source for tailored CVs and is never modified (see *Tailoring a CV with Claude Code* below).
2. Edit `search.yaml`: add Greenhouse board tokens and/or Lever company slugs to the source lists. Empty lists intentionally discover nothing.
3. Set a strong `DASHBOARD_TOKEN` in `.env`. Optional: configure `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to receive notifications and approve/reject via Telegram. **Only run one app instance with Telegram polling.**
4. Run `uvicorn app.main:app --host 127.0.0.1 --port 8088`. Visit `http://localhost:8088` via SSH tunnel and log in with your `DASHBOARD_TOKEN`.

**Do not expose port 8088 publicly.** See *Dashboard security* below.

## Safe VM deployment, separate from price checker

On the VM, use a separate directory, venv, and systemd unit. Do not modify the existing price-checker cron/service. A sample unit is provided in `deploy/job-hunter.service`; replace `YOUR_LINUX_USER` with your VM Linux username, copy it to `/etc/systemd/system/`, then enable/start. This does not require GitHub Actions or new GCP IAM permissions.

For a local browser, create a tunnel from your machine using your existing SSH access, e.g. `gcloud compute ssh price-checker --zone=us-central1-a --project=vacation-price-checker --tunnel-through-iap -- -L 8088:127.0.0.1:8088` and open `http://127.0.0.1:8088`.

## Tailoring a CV with Claude Code

CVs are **not** generated during discovery or when you click *HTML CV* in the dashboard. You tailor one per job, when you decide to apply, in an interactive Claude Code session that you start yourself. No AI API calls are made by the app, and Claude Code is never run unattended.

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

CV files created by older versions of this app (master copies / templates) show as `legacy (untailored)` in `--list`; replace them with `--force` when you want a tailored version. `OPENAI_API_KEY`/`OPENAI_MODEL` are no longer used and can be removed from `.env`.

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

Tests use a temporary database and files; they never touch `data/` or call external services.

## Architecture

- `app/core.py`: sources, matching, SQLite, notifications, status changes, CV path helpers.
- `app/tailor_cv.py`: CLI for the interactive Claude Code CV tailoring workflow (prepare / finalize / list).
- `app/main.py`: dashboard, API (serves existing tailored CVs only), hourly APScheduler scan, Telegram long polling.
- `data/master_cv.html`: private source HTML CV, required for CV generation (never commit).
- `profile.yaml`: legacy template data; no longer used by the HTML CV generator.
- `search.yaml`: monitored company boards and filtering.
- `.env`: private settings (never commit).
- `data/`: local SQLite database, tailored HTML CVs (`data/cvs/`) and tailoring workspaces (`data/tailoring/`) — never commit.

Job boards can change or rate-limit their public endpoints. Discovery errors are logged and do not crash the scheduler. Source URLs are not a promise of live availability. Keep monitored board counts reasonable and comply with provider terms.
