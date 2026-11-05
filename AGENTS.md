# Instructions for AI coding agents

These conventions apply to every AI coding agent (Claude Code, Codex, Copilot, ...) working in this repository.

## Development workflow

- Work on a feature branch (or git worktree) from the latest `main`; never commit to `main` directly.
- Run the full test suite before committing: `.venv/bin/python -m unittest discover -s tests`
- Stage files explicitly (never `git add -A` in the production checkout). Review the diff and scan it for secrets.
- Open a pull request with `gh pr create`. Never merge a PR, deploy, or restart services without the owner's
  explicit approval.
- The production VM keeps uncommitted, VM-specific edits in `app/main.py`, `deploy/job-hunter.service` and
  `search.yaml`. Preserve them; avoid editing those files' changed hunks in PRs (prefer new config files).

## Agent reports (required for substantial tasks)

Every substantial development task (new feature, redesign, multi-file fix, data migration, anything that needs a
deployment decision) must produce a Markdown report **committed in the same PR as the code**. The owner reviews
work by reading these reports directly from GitHub, so a task is not complete until its report is in the PR
branch. Local-only reports do not count.

1. **Location and name:** `docs/agent-reports/YYYY-MM-DD_HHMM_PR-<number>_<short-topic>.md`, time in the
   **Europe/Amsterdam** timezone (`TZ=Europe/Amsterdam date +%Y-%m-%d_%H%M`), topic in lowercase kebab-case.
   If the PR number is unknown when you start, use `PR-new` and rename the file (and fix links) after
   `gh pr create`, then push again.
2. **Structure:** follow `docs/agent-reports/TEMPLATE.md` (16 required sections, plus discovery- or CV-specific
   sections when relevant).
3. **Index:** add a row at the top of `docs/agent-reports/README.md` (newest first): date/time, title, PR, status,
   link, one-sentence summary. Update the status when it changes (e.g. `open` → `merged` → `deployed`).
4. **Commit** the report and index together with the implementation; push; create or update the PR; put the
   report path in the PR description.
5. **Precision:** distinguish what was implemented, what was tested (unit vs. live), and what is assumed or only
   proposed. Never claim a test or verification that did not happen. Give exact commands, numbers and commits.

## Sensitive information (never in reports, commits, PR text or logs you paste)

- API keys, tokens, `.env` contents, authentication cookies
- the private master CV (`data/master_cv.html`) or generated personal CVs (`data/cvs/`)
- personal contact details (email, phone, address)
- production database contents (`data/*.sqlite3`) or raw private logs

Use aggregate statistics and sanitized examples instead (e.g. "190 jobs from 107 employers", a job title and
employer from a public posting).
