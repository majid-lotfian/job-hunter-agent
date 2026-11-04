# Job Hunter Agent — self-hosted MVP

An open-source, low-resource job discovery and review assistant. It monitors configured **public Greenhouse and Lever job boards**, scores matches, creates **grounded HTML CV drafts**, sends Telegram notifications, and exposes a lightweight FastAPI dashboard. An OpenAI API key is optional for AI tailoring; without one, the master HTML is copied as-is.

**Important limits:** This is a working MVP, not a universal job search engine. It does **not** yet search LinkedIn, Indeed, Glassdoor, Ashby, Workday, SmartRecruiters, or Adzuna. It does **not** automatically submit applications, generate LLM-written cover letters, or calculate genuine ATS scores. The displayed score is a transparent rule-based relevance score. Telegram approval approves a *draft*, not an application submission. `applied` status must be recorded manually after confirmed external submission. No credentials are embedded in the code.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

1. Place your private, factual master HTML CV at `data/master_cv.html` on the VM. This path is gitignored: **never commit your personal CV**. The existing HTML layout is preserved; when OpenAI is configured, the agent rewrites the short profile summary and reorders existing project and skills blocks for relevance. Review generated summaries before use.
2. Edit `search.yaml`: add Greenhouse board tokens and/or Lever company slugs to the source lists. Empty lists intentionally discover nothing.
3. Set a strong `DASHBOARD_TOKEN` in `.env`. Optional: configure `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to receive notifications and approve/reject via Telegram. **Only run one app instance with Telegram polling.**
4. Run `uvicorn app.main:app --host 127.0.0.1 --port 8088`. Visit `http://127.0.0.1:8088` via SSH tunnel or a private network, enter dashboard token, and load jobs.

**Do not expose port 8088 publicly.** The dashboard token is sent as an HTTP header for API calls and as a query parameter when viewing CV files; use a trusted local SSH tunnel, and do not share CV URLs. Do not run behind a public reverse proxy without HTTPS and stronger authentication.

## Safe VM deployment, separate from price checker

On the VM, use a separate directory, venv, and systemd unit. Do not modify the existing price-checker cron/service. A sample unit is provided in `deploy/job-hunter.service`; replace `YOUR_LINUX_USER` with your VM Linux username, copy it to `/etc/systemd/system/`, then enable/start. This does not require GitHub Actions or new GCP IAM permissions.

For a local browser, create a tunnel from your machine using your existing SSH access, e.g. `gcloud compute ssh price-checker --zone=us-central1-a --project=vacation-price-checker --tunnel-through-iap -- -L 8088:127.0.0.1:8088` and open `http://127.0.0.1:8088`.

## OpenAI CV tailoring

Set `OPENAI_API_KEY` and optionally `OPENAI_MODEL` in the VM's `.env` (OpenAI API billing is separate from ChatGPT). The model uses the master CV as the sole factual source and selects the order of existing project/skill sections while tailoring the short profile paragraph. The rest of the original HTML, including styles, experience and links, remains intact. Generated CVs are cached at `data/cvs/job-ID.html`; remove a generated file only when you deliberately want it regenerated. No automatic applications are submitted. API errors fall back to the original master HTML. Avoid committing personal information or secrets to this public repository.

## Architecture

- `app/core.py`: sources, matching, SQLite, HTML CV, notifications, status changes.
- `app/main.py`: dashboard, API, hourly APScheduler scan, Telegram long polling.
- `data/master_cv.html`: private source HTML CV, required for CV generation (never commit).
- `profile.yaml`: legacy template data; no longer used by the HTML CV generator.
- `search.yaml`: monitored company boards and filtering.
- `.env`: private settings (never commit).
- `data/`: local SQLite database and generated HTML CVs (never commit).

Job boards can change or rate-limit their public endpoints. Discovery errors are logged and do not crash the scheduler. Source URLs are not a promise of live availability. Keep monitored board counts reasonable and comply with provider terms.
