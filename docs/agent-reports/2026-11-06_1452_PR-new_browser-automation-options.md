# Browser automation options for applicant-facing forms (+ PR #10/#11 deployment record)

- **PR:** #new (renamed after creation)
- **Branch:** `docs/browser-automation-options` · **Commits:** this report, the index update and one test-fixture fix
- **Date:** 2026-11-06 14:52 (Europe/Amsterdam) · **Author:** Claude Code (Opus 5.5)
- **Status:** open. Investigation only; no automation infrastructure was implemented, as you asked.

> Legend: **Implemented** = code in this PR · **Unit-tested** = covered by automated tests ·
> **Live-verified** = observed against real systems (state where) · **Assumed/Proposed** = not verified.

## Owner decisions summary (read first)

1. **PRs #10 and #11 are merged and deployed** (`d425b4e`). 273/273 tests pass in production. `APPLY_MODE=off`; hotel-bot was untouched.
2. **Your applicant profile exists and is unverified** (`data/applicant_profile.yaml`, mode 600, gitignored). Review it, then set `verified: true` (§17).
3. **Recommended shortest path to one real application:** a small **local runner on your own computer**, Option 1 below.
   - It runs Playwright in a visible browser and pulls the approved application package from the dashboard.
   - It fills the form and uploads the PDF, then **you press Submit the first time**. Later it can press Submit itself after your per-job approval.
   - It **pauses on any CAPTCHA challenge or login**; it never solves one.
   - VM impact: two small authenticated API endpoints, nothing heavy.
4. **Rejected:** a headless browser on the VM (memory, reliability, and the CAPTCHA problem).
5. **To proceed:** approve Option 1 (or 2), pick the first job (73 or Nebius 24), and confirm you can run a Python script on your laptop.

## 1. Task objective

- Merge PR #10, retarget and merge PR #11, and deploy both safely.
- Initialize the profile, unverified.
- Investigate a lightweight, authorized browser-automation approach for applicant-facing forms, and report options, resources and the shortest viable path **before** building more infrastructure.

## 2. Initial problems identified

- **Every applicant-facing form the agent can reach has bot protection:**
  - Recruitee careers forms: invisible hCaptcha (6 of 7 checked; see the PR #11 report).
  - Greenhouse's embedded application form: reCAPTCHA.
  - Ashby application pages: reCAPTCHA.
  - Lever job pages: reCAPTCHA.

  All live-checked on 2026-11-06 by looking for CAPTCHA markers in the public pages.
- Applicant-side APIs exist only on Recruitee, and only on CAPTCHA-free forms (PR #11). None of today's vacancies qualify.
- **No vacancy explicitly accepts applications by email.** The descriptions mention recruiter contact addresses, and "send us your CV" phrases point to forms. Scan of 15 jobs with email hints (score ≥ 60).
- **Deployment finding:** one test failed once in the production checkout. The cause was a test fixture that gives test jobs a random hex `source_id`. About 2 % of the time it is all digits, and a `greenhouse:acme` test job then looks like a real Greenhouse job; the mocked 404 correctly marks it "closed". Reproduced deterministically. The product code is correct. Fixed in this PR (`'x' + hex`).

## 3. Options (architecture and implementation decisions)

The key constraint is the same everywhere: invisible CAPTCHAs score the browsing environment. A datacenter IP running a headless browser is likely to be challenged, and solving a challenge automatically would be a bypass. Only a **real browser in your own environment** can pass invisible checks legitimately, with **you** handling any visible challenge.

| # | Option | Where it runs | Resources | Autonomy | CAPTCHA handling | Effort | Verdict |
|---|---|---|---|---|---|---|---|
| **1** | **Local runner**: Python + Playwright, a visible browser with a persistent dedicated profile; pulls approved packages from the dashboard API | Your laptop | Laptop: about 300 MB disk (Chromium) and 300–500 MB RAM while running. VM: 2 small endpoints | Fills and uploads automatically. Submit is your click at first; later the runner clicks after your per-job approval | Pauses and pings you on Telegram; never solves | About 1 day for Recruitee + Greenhouse fillers, tested on saved form fixtures | **Recommended** |
| 2 | **Userscript or mini extension** (e.g. Tampermonkey) in your normal browser, auto-filling when you open the dashboard's "Open Application Page" link | Your browser | Negligible | Fill only; you click Submit | Your normal browser: invisible checks usually pass, and you see any challenge | About half a day | Good fallback; least autonomous |
| 3 | **Claude in Chrome / desktop computer use** driving your Chrome | Your computer, with Claude Code or the desktop app running locally | None on the VM | Interactive per application, not unattended | You handle challenges | None to build | Possible ad hoc; this VM session cannot reach your Chrome |
| 4 | Headless Playwright/Chrome on the VM | e2-micro | Measured today: about 1 GB combined RSS and 50–65 s per page; frequent "no output" failures | Would be full | Likely challenged (datacenter IP + headless); would need solving = bypass | – | **Rejected** |
| 5 | Separate small cloud browser VM (e.g. 2 GB e2-small, about EUR 13/month) | GCP | Fine | Full | Same datacenter/automation problem | – | **Rejected** |
| 6 | Email applications (SMTP / Gmail API from your mailbox) | VM | Negligible | Full, authorized | None | About half a day | Only when a posting explicitly asks for email; none today |
| 7 | Recruitee API on CAPTCHA-free forms (PR #11) | VM | Negligible | Full | Not applicable | Done | Active, but no eligible vacancy today |

**Terms of use:** the hosted ATS candidate terms (Greenhouse, Ashby, Lever, Recruitee) were **not reviewed in this task**. Filling a form in your own browser while you press Submit is ordinary use. Having software press Submit automatically should be checked against each site's terms before Option 1's autonomous mode is enabled. Proposed: confirm per platform before switching it on.

### Shortest viable path (Option 1 MVP)

1. **VM, small additions:**
   - `GET /api/applications/{id}/package.json`, authenticated with a separate `RUNNER_TOKEN` from `.env`. It returns the prepared fields and values plus the PDF URL, only for prepared applications with an active approval.
   - `POST /api/applications/{id}/runner-result`, to report "filled", "paused (CAPTCHA/login)" or "submitted + confirmation text/URL".

   "Submitted" is recorded only with captured confirmation evidence (thank-you page text or URL) and only once (the existing duplicate guards apply).
2. **Laptop:** `runner.py` (Playwright, visible browser, persistent profile in a dedicated folder):
   - opens the apply page;
   - fills fields using per-ATS selectors for the Recruitee and Greenhouse forms;
   - uploads the PDF;
   - answers only the questions in the package;
   - **stops before Submit** (supervised mode).

   After a few supervised runs, autonomous mode would click Submit only when the page shows no challenge, and wait for the confirmation.
3. **Tests:** saved HTML fixtures of each form, with no live submission.
4. **First job:**
   - **job 73** (Conclusion, Recruitee): the simplest form, 4 fields and no questions, but a Dutch-language posting.
   - or **job 24** (Nebius, Greenhouse): English, 4 screening questions to answer in your profile (languages, work authorization, residence, previous employment).

## 4. Files added or modified

| File | Change |
|---|---|
| `docs/agent-reports/2026-11-06_1452_PR-new_browser-automation-options.md` | This report |
| `docs/agent-reports/README.md` | Index row; PRs #10 and #11 marked deployed |
| `tests/test_workflow.py` | Test job `source_id` is never all-digit (flake fix) |

## 5. Features implemented

None. Investigation only, as requested.

## 6. Configuration changes

None. Production `.env` still has `APPLY_MODE=off` and `CV_AUTO_GENERATE=false`.

## 7. Tests executed and results

- **Production checkout after the fast-forward to `d425b4e`, network blocked:**
  - first run: 272/273 (the flake above);
  - deterministic reproduction of the flake, confirmed;
  - rerun: **273/273 OK**.
- **This branch with the flake fix:** full suite, see the PR description for the result.

## 8. Live verification results (tested vs. assumed)

**Deployment (live-verified on the VM):**
- **Merges:** PR #10 merged as `b01b1c6`. PR #11 was retargeted to `main` (clean diff, 11 files) and merged as `d425b4e`.
- **Backup:** DB backup via the SQLite backup API in `data/deploy-backup-pr10-11-20261106/` (integrity ok; jobs 198, cv_jobs 3, events 6, applications 0, approvals 0, llm_usage 1, all matching). `.env`, `search.yaml`, the service file and `app/main.py` were copied there too.
- **Update:** stash, fast-forward, pop: clean (3 VM files, same 8/5-line diff). No new dependencies.
- **Restart:** only `job-hunter.service`. `hotel-bot.service` start time unchanged (11:47:17).
- **After the restart:**
  - `/health` returns 200; `/applications/1` without login returns 303; `POST /api/applications/1/submitted` without login returns 401;
  - the dashboard lists 196 active jobs; buttons read "Prepare Application" / "Prepare Application + CV";
  - the job 7 PDF downloads (200, application/pdf); an unknown application returns 404;
  - 0 applications, 0 approvals, Gemini requests still 1 today;
  - 0 errors or token leaks in the log since the restart.

**Profile (live, on the VM):**
- `--init-from-cv` created the draft. Filled: full_name, first_name, last_name, email, phone, location, nationality, work_authorization, linkedin, github, portfolio, languages, plus 2 derived answers.
- Empty (not in the CV): salary_expectation, notice_period, availability, preferred_locations, work_arrangements.
- `--check`: the only error is "not marked `verified: true`".

**CAPTCHA markers (public GETs):** Greenhouse embed form (reCAPTCHA), Ashby application page (reCAPTCHA), Lever job page (reCAPTCHA).

**Assumed / not verified:**
- that invisible CAPTCHAs pass in a visible Playwright browser on a residential connection. Likely often, never guaranteed, and the runner would pause if not;
- the ATS terms of use.

## 9. Performance and resource considerations

The recommended option puts the browser on your laptop. On the VM it adds two small JSON endpoints and serves a ~21 KB PDF per application, so the 1 GB VM is not affected.

## 10. Security and privacy checks

- **Runner access:** the runner would authenticate with a dedicated revocable token, separate from the dashboard password. It would receive personal values only for applications you approved.
- **Browser profile:** a dedicated folder, not your everyday browser profile.
- **What it never does:** no CAPTCHA solving, no login automation, no use of employer keys.
- **This report:** contains no personal values.

## 11. Known limitations

- Even the recommended runner cannot guarantee unattended submission: a visible CAPTCHA challenge or a login wall pauses it for you.
- Per-ATS form fillers need maintenance when the employer form changes.

## 12. Outstanding issues / decisions needed

1. Review and verify the profile (§17).
2. Approve Option 1 (recommended) or 2, or choose another.
3. Choose the first job (73 or 24) and, for job 24, add your answers to its 4 screening questions in the profile.
4. Decide whether the runner may press Submit itself after approval (only after a terms check per platform), or only fill.

## 13. Deployment instructions

This PR touches only docs and a test fixture, so no deployment or restart is needed. Merging it is safe.

## 14. Rollback instructions

Revert the PR. Production rollback for #10/#11: `git reset --hard 529f3d9`, re-apply the VM edits, restart `job-hunter` (no schema changes).

## 15. Recommended next steps

1. Verify the profile.
2. Approve Option 1 and the first job.
3. I implement the two VM endpoints and `runner.py` with fixture tests in one PR.
4. You install the runner on your laptop (`pip install playwright`, `playwright install chromium`).
5. First supervised run: you press Submit, and the confirmation is recorded.

## 16. PR URL and commits

See the PR (the URL is added after creation).

## 17. Profile review checklist (`nano data/applicant_profile.yaml` on the VM)

- **first_name / last_name:** split from your full name. Check how employers should see it.
- **work_authorization** and the 2 answers (authorized to work: yes; sponsorship: no): derived from "Dutch citizen". Confirm.
- **Optional fields** that many forms ask for: salary_expectation, notice_period, availability, preferred_locations, work_arrangements (onsite / hybrid / remote).
- **Screening questions:** add explicit answers for the ones you'll meet, e.g. Nebius: "Have you ever been employed by Nebius before?", "Do you currently reside in the location specified for this role?"
- **Finally:** set `verified: true`, then run `.venv/bin/python -m app.applications.profile --check`. It should say "Profile OK".
