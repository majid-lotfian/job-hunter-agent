# Scoring, CV daily limit, Telegram digests, Workday, and the agent-report workflow

- **PR:** #6 - https://github.com/majid-lotfian/job-hunter-agent/pull/6 (continues the multi-source discovery PR)
- **Branch:** `feature/multi-source-discovery` · **Commits for this task:** `7d6f055`, `ee9c087`, plus the report commit
- **Date:** 2026-11-05 10:56 (Europe/Amsterdam) · **Author:** Claude Code (Opus 5.5)
- **Status:** open - not merged, not deployed, no service restarted

> Legend: **Implemented** = code in this PR · **Unit-tested** = automated tests · **Live-verified** = observed against
> real endpoints/data on the VM, always on a *copy* of the production DB · **Assumed/Proposed** = not verified.

## 1. Task objective

Part A, before deploying PR #6:
1. Score AI/ML roles on title **and** real requirements.
2. Limit automatic CVs to `CV_DAILY_LIMIT=5` per local day.
3. Consolidate Telegram notifications.
4. Evaluate and enable Workday for Philips, NVIDIA, ING, RELX and Wolters Kluwer.
5. Preserve all existing behaviour.

Part B: a permanent, GitHub-based reporting workflow (`docs/agent-reports/`, index, `AGENTS.md`).

## 2. Initial problems identified

- **Under-scored titles:** relevant titles scored only 20-40 because the score was mostly a substring match on `search.yaml` `titles_any`. Affected titles included ML Engineer, Applied Scientist, Research Engineer, Deep Learning, NLP and ML Scientist.
- **Over-scored titles:** any title match plus one keyword reached 70, even when the description was thin ("AI Engineer talent pool" adverts).
- **CV burst:** 57-78 jobs would be ≥70 on the first scan, meaning that many Gemini CVs and as many Telegram messages at once.
- **Telegram volume:** one message per CV, plus up to 10 per discovery scan.
- **Workday:** opt-in and untested per employer. ING was configured with the wrong site (`ICSNLDGEN`, about 5 postings in total).
- **Reports:** only in the terminal, so the owner had to copy them by hand.

## 3. Architecture and implementation decisions

**Scoring.** `core.score_job` keeps its signature, its `search.yaml` gates (excluded terms, `locations_any`, `max_age_days`) and the 0-100 scale. The new `core.score_breakdown()` returns the parts:

| Part | Points |
|---|---|
| Title: primary target role | 40 |
| Title: adjacent role | 30 |
| Title: keyword-only match | 10 |
| Requirements: `search.yaml` keywords | 5 each, max 20 |
| Requirements: distinct AI/ML skills in the description | 4 each |
| Requirements total | capped at 45 |
| Seniority (mid, senior, staff, lead, principal, II-IV) | +5 |
| Location in NL (Randstad, NL, remote-NL) | +5 |
| Dutch required | -10 |

- Primary roles are your 13 listed titles plus `titles_any`. Adjacent roles are MLOps, AI Platform, AI Solutions, AI architect, forward-deployed, data scientist and AI consultant.
- Skills come from the same AI_SIGNALS list as the relevance filter.
- **Effect:** a title match alone gives at most 40 + 20 = 60, below 70. Reaching 70 needs real requirements.
- Relevance tiers (primary, secondary, possible) still decide whether a job is stored at all. The score ranks jobs and drives CVs and notifications.

**Daily CV limit.** All state lives in the database:
- New `cv_jobs` columns: `quota_day`, `notified_at`, `completed_at`, added with idempotent `ALTER TABLE` in `core.init_db`.
- The first claim of an *automatic* job (reason `new` or `legacy`) stores the local date in `CV_TIMEZONE` (default Europe/Amsterdam). Today's usage is `COUNT(quota_day = today)`.
- **Claim order:** manual requests (`manual`, `regenerate`) first and never counted, then highest score, then newest posting.
- The claim runs inside `BEGIN IMMEDIATE`, so concurrent workers can't overshoot the limit.
- **One slot per job:** a job that already has a `quota_day` is never charged again, including retries on later days.
- **Refunds:** configuration or local errors (missing or invalid key, missing master CV) give the slot back if this claim consumed it.
- **Unlimited:** set `CV_DAILY_LIMIT` to a negative value.

**Telegram digests.**
- *CVs:* `process()` no longer sends anything. The worker sends `send_cv_digest()` once after each batch, listing every unannounced outcome (ready, needs review, failed). Rows are marked `notified_at` only after a successful send, or when Telegram isn't configured. A failed send is retried, and a restart never repeats a digest.
- *Discovery:* one digest per scan, built from the persistent `jobs.notified_at` (store migration v3; jobs existing at migration time count as already announced). Each line shows score, employer, location, CV status, the "Dutch required" flag and the posting link. Bounded to 15 lines and 3,900 characters.
- *Critical alert:* "CV generation paused: …" is sent once per distinct reason, for example an invalid API key.

**Workday.** Each site was live-tested twice. Enabled only where both runs were reliable, using the endpoint the public career site itself uses (no login). `robots.txt` on every enabled tenant explicitly `Allow`s the site. A per-entry `id` allows several sites per tenant.

**Quality fixes found during live verification:**
- A title naming a non-NL region ("… - US", "(Canada)") is rejected unless it also names EU/NL.
- Near-identical postings (shingle similarity ≥ 0.9) on the *same aggregator* merge as reposts. Employer ATS boards keep the strict "different requisition" rule.

**Reporting.** `docs/agent-reports/` with an index, a template and `AGENTS.md`. `CLAUDE.md` imports `AGENTS.md`, because Claude Code loads `CLAUDE.md` automatically.

## 4. Files added or modified

| File | Change |
|---|---|
| `app/core.py` | `score_breakdown`/`score_job` v2, `PRIMARY_TITLES`, `SECONDARY_TITLES`, `SCORE_WEIGHTS`; `cv_jobs` columns `quota_day`, `notified_at`, `completed_at` (+ allowed fields) |
| `app/cv_pipeline.py` | `CV_DAILY_LIMIT`/`CV_TIMEZONE` settings, `local_day`, `quota_status`, quota-aware `claim_next`, slot refund, `send_cv_digest`, one-time pause alert; per-CV Telegram removed |
| `app/discovery/engine.py` | passes region/language to scoring; persistent per-scan discovery digest; removed per-job messages and baseline flag |
| `app/discovery/store.py` | migration v3 (`jobs.notified_at`); passes `aggregator` to dedupe |
| `app/discovery/dedupe.py` | aggregator repost merge (`REPOST_THRESHOLD = 0.9`) |
| `app/discovery/relevance.py` | `title_region_conflict` |
| `app/discovery/config.py` | per-entry `id`; notification settings reduced to `min_score` |
| `app/main.py` | `/api/cv-worker` returns quota; dashboard shows "automatic CVs today: x/5, n queued" |
| `discovery.yaml` | Workday: 5 sites enabled, RELX generic disabled, ING → `ICSGBLCOR`; notifications section simplified |
| `.env.example`, `README.md` | `CV_DAILY_LIMIT`, `CV_TIMEZONE`, digest behaviour |
| `docs/discovery/SOURCES.md` | Workday status and counts |
| `tests/test_scoring.py` (new), `tests/test_cv_pipeline.py`, `tests/test_discovery.py` | new and updated tests (§7) |
| `AGENTS.md`, `CLAUDE.md`, `docs/agent-reports/README.md`, `TEMPLATE.md`, this report | reporting workflow |

## 5. Features implemented

| Feature | Implemented | Unit-tested | Live-verified |
|---|---|---|---|
| Evidence-based scoring | yes | yes (5 tests, 13 target titles × strong/weak descriptions) | yes: re-scored 190 + 198 real jobs (§8) |
| CV daily limit (persistent, timezone, priority, carry-over, manual bypass, single slot, refund) | yes | yes (10 tests) | partly: dry run on real data, claims only, **no Gemini calls** |
| CV digest + one-time pause alert | yes | yes | **no**: Telegram was disabled in all live runs (message text captured instead) |
| Discovery digest (persistent, retried, bounded) | yes | yes (4 tests) | yes: text captured, not sent |
| Workday ×5 | yes | config test | yes: 2 runs per site |
| Title-region rejection, aggregator repost merge | yes | yes | evaluated on live-run data, not with a full re-run (§8) |
| Reporting workflow | yes | n/a | n/a |

## 6. Configuration changes

New `.env` options (both optional; defaults apply):
- `CV_DAILY_LIMIT=5`
- `CV_TIMEZONE=Europe/Amsterdam`

`CV_MIN_SCORE` stays 70.

`discovery.yaml` changes:
- 5 Workday sites enabled.
- The `notifications` section now has only `min_score` (`max_individual_per_run` and `digest` were removed; one digest per scan is the default). Unknown old keys are ignored.

`search.yaml` is **unchanged**, so there's no conflict with the VM-local edit.

## 7. Tests executed and results

`/home/majid_lotfian_work/job-hunter-agent/.venv/bin/python -m unittest discover -s tests` → **143 tests, OK** (up from 122).

New or updated tests:
- **Scoring:** the 13 target titles with strong requirements are ≥70 and with weak descriptions are <70. Irrelevant roles stay low. Gates are unchanged. Seniority, location and the Dutch penalty work. The breakdown adds up and stays bounded.
- **Quota:**
  - per-day limit, priority order and carry-over to the next day,
  - newest posting wins on equal score,
  - manual requests bypass the quota and don't consume it,
  - retries across days use only one slot, and config errors refund it,
  - the limit persists across restarts; `-1` means unlimited; completed CVs aren't re-queued,
  - unknown timezones fall back to UTC.
- **CV digest:** one message per batch, no repeat, retried after a failed send. The pause alert is sent only once. Existing CV outcomes aren't re-announced after the column migration.
- **Discovery digest:** one per scan, no repeats, retried on failure, bounded for large batches, shows CV status.
- **Quality fixes:** title-region conflict and aggregator repost merge.
- **Updated tests:** the CV pipeline tests now assert the digest instead of per-CV messages, and the Workday configuration test reflects the enabled sites.

## 8. Live verification results

All runs used **copies of the production database** in a scratch directory, with Telegram disabled. Nothing was written to production.

**Scoring calibration** (re-scoring the 190 jobs from the earlier live run):

| Score band | Old | New |
|---|---|---|
| 0-29 | 74 | 8 |
| 30-49 | 58 | 35 |
| 50-69 | 1 | 73 |
| ≥70 | 57 | 74 |

- Previously under-scored titles now score 72-95 with real requirements, for example ML Engineer (old 20-35), Applied Scientist (old 20-30) and Research Engineer.
- Title-only matches now fall below 70, for example "AI Engineer talent pool" (old 70 → 45) and KPN "AI Engineer" with Dutch required and thin requirements (old 75 → 49).

**Workday** (2 runs per site, default queries, 3 pages each):

| Site | Run 1 | Run 2 | Relevant NL jobs | Decision |
|---|---|---|---|---|
| Philips `jobs-and-careers` | ok, 122 fetched, 101 s | ok, 26 s | 3 (Amsterdam, Eindhoven) | **enabled** |
| NVIDIA `NVIDIAExternalCareerSite` | ok, 216 fetched, 316 s (94 detail fetches) | ok, 34 s | 2 (remote NL) | **enabled** |
| ING `ICSNLDGEN` | ok but empty (site has ~5 postings) | same | 0 | replaced |
| ING `ICSGBLCOR` | ok, 78 fetched, 52 s | ok, 22 s | 0 currently | **enabled** (reliable, about 8 requests per steady scan) |
| RELX `relx` | ok, 134 fetched, 160 s | ok, 34 s | 0 | disabled (covered by Elsevier) |
| Elsevier (RELX `ElsevierJobs`) | ok, 89 fetched, 109 s | ok, 28 s | 4 (Amsterdam) | **enabled** |
| Wolters Kluwer `External` | ok, 97 fetched, 116 s | ok, 28 s | 4 (Alphen a/d Rijn, Hoevelaken) | **enabled** |

**Final integrated discovery run** (fresh production DB copy, final scoring, 74 enabled sources, `force`):
- **Source health:** 74 `ok`, 0 failed, 1 disabled, 2 not configured (Adzuna, Jooble).
- **Jobs:** 10,374 postings checked → **198 new logical jobs from 108 employers**. 8 cross-source duplicates were merged (Databricks, Nebius ×4, NVIDIA, a Celonis role on The Muse, Working Nomads ↔ Remotive), and all 8 merge reasons were checked in the log.
- **Telegram:** **1 discovery digest** for 179 relevant jobs (15 lines plus "… and N more"), instead of up to 179 messages.
- **Legacy jobs:** the 5 legacy jobs kept their statuses (approved, new, needs_revision) and were marked as already announced.
- **Resources:** 8.3 min (a first scan, with 529 detail fetches and 672 requests), peak RSS 76 MB.
- **Jobs per source family:**

| Family | Jobs | Family | Jobs |
|---|---|---|---|
| Himalayas | 93 | Lever | 6 |
| Greenhouse | 39 | SmartRecruiters | 6 |
| Ashby | 14 | The Muse | 5 |
| Arbeitnow | 13 | Working Nomads | 4 |
| Workday | 13 | Remotive | 1 |
| Recruitee | 11 | Remote OK | 1 |

  37 source instances produced at least one relevant job.
- **Scoring distribution (198 jobs):**

| Band | Jobs | Band | Jobs |
|---|---|---|---|
| 10-19 | 1 | 60-69 | 26 |
| 20-29 | 7 | 70-79 | 21 |
| 30-39 | 11 | 80-89 | 31 |
| 40-49 | 24 | 90-99 | 26 |
| 50-59 | 51 | | |

  Tiers: primary 139, secondary 44, possible 15. **78 jobs are eligible for automatic CVs** (score ≥70).
- **Regions:** Randstad 81, remote open to NL 95, remote Europe 7, remote worldwide 9, other 6.
- **Language:** English 171, Dutch-language posting 15, Dutch required 8, Dutch preferred 4.

**CV quota dry run** (same copy, claims only, no Gemini calls): with `CV_DAILY_LIMIT=5`, exactly 5 jobs were selected for "today" (all scored 95) and **73 stayed queued**. An earlier flawed script, which released each claim back to `pending`, re-claimed the same job 11 times, and quota usage stayed at **1**. That incidentally confirms one slot per job.

**Fixes evaluated on the final-run data without a full re-run:**
- `title_region_conflict` rejects exactly 1 of the 198 jobs ("Staff AI Engineer - US"). Its EU twin is kept.
- The 2 identical Reddit postings on Himalayas have similarity 1.00, so they will merge.

**Not live-verified:**
- real Gemini CV generation under the quota (unchanged generation code; the quota is unit-tested and dry-run only),
- actual Telegram delivery (disabled in live runs on purpose),
- behaviour across a real Europe/Amsterdam midnight (unit-tested with a mocked date).

## 9. Performance and resource considerations

- **Scans:** the first scan after deploy takes about 8-13 min (Workday adds about 15 min of detail fetches on its first scan). Steady-state scans take about 1.5 min plus 2-3 min for Workday, which has a 6-hour interval. Peak memory stays under 80 MB.
- **CV worker:** at most 5 Gemini generations per day, paced at least 15 s apart, so roughly 5-15 requests per day including correction rounds. That's well inside free-tier limits.
- **Telegram:** at most 1 discovery digest per scan, plus 1 CV digest per batch, plus 1 alert per distinct pause reason.

## 10. Security and privacy checks

- `git diff origin/main` on this branch was scanned for API key, token, email and phone patterns. The only hit is the public repository URL in the HTTP User-Agent.
- No `.env`, `data/`, database, master CV or generated CV is tracked (`git ls-files` checked).
- Reports and docs contain only aggregate numbers and public job titles and employers. No CV content or contact details appear anywhere.
- Live runs used DB copies in a scratch directory. Production `data/jobs.sqlite3` is untouched (still schema version 0, 5 jobs).
- Dashboard authentication is unchanged; the new quota data is behind `/api/cv-worker`, which requires login.

## 11. Known limitations

- **Scoring:** weights are heuristic and were calibrated on about 200 real jobs. The AI-skill count rewards long, keyword-rich descriptions, and requirements often hit the 45-point cap, so many strong jobs tie at 95. Ties are broken by newest posting.
- **Himalayas** is about half of the relevant jobs. Its "open to NL" data is trusted as provided, apart from the new title-region check.
- **Telegram:** the discovery digest lists 15 jobs and links the rest to the dashboard. Per-job approve/reject buttons are gone from Telegram; use the dashboard.
- **Pause alert:** it's in memory, so after a service restart the alert for the same unresolved problem is sent once more.
- **Daily limit:** a slot is consumed when an automatic job is first claimed, so a job that later fails permanently still used that day's slot (by design, to avoid retry storms).
- **NVIDIA's first Workday scan** fetches about 94 details (about 5 min) because its list shows "3 Locations" instead of cities.

## 12. Outstanding issues / decisions needed

1. **Review the new scoring weights** (§3). With them, 78 of 198 jobs are CV-eligible; at 5 per day that's about 16 days of backlog. Raising `CV_MIN_SCORE` would shorten it, but it was kept at 70 as instructed.
2. **Confirm that losing the Telegram approve/reject buttons is acceptable.** The digest links to the dashboard instead.
3. Adzuna and Jooble remain unconfigured until you add free API keys.

## 13. Deployment instructions (after review and approval)

1. Back up the database: `cp data/jobs.sqlite3 ~/job-hunter-backups/jobs-pre-pr6.sqlite3`. The app also backs up automatically before migrating, to `data/backups/`.
2. Merge PR #6, then update the VM checkout. The PR also changes `app/main.py`, which has the VM-local edit, so a plain fast-forward is refused. Use:
   `git stash push -- app/main.py deploy/job-hunter.service search.yaml && git merge --ff-only origin/main && git stash pop`
   Verified: the VM's `app/main.py` patch applies cleanly to the PR version (`git apply --check`), and the PR doesn't touch `search.yaml` or `deploy/job-hunter.service`.
3. Run the tests: `.venv/bin/python -m unittest discover -s tests`.
4. Optionally add `CV_DAILY_LIMIT=5` and `CV_TIMEZONE=Europe/Amsterdam` to `.env` (these are the defaults).
5. Restart **only** `job-hunter.service`. On start:
   - The discovery schema migrates v0 → v3, additively, with a backup.
   - `cv_jobs` gets 3 new columns; existing CV outcomes are marked as announced.
   - The first scan runs in the background (about 10-25 min including Workday) and sends **one** discovery digest.
   - The CV worker starts 5 automatic CVs and sends **one** CV digest.
6. Verify the dashboard: the *Sources* panel (74 `ok`) and the note "automatic CVs today: x/5". Also check `journalctl -u job-hunter.service | grep -E "Discovery finished|CV for job"`.

## 14. Rollback instructions

1. Stop the service, `git checkout <previous main commit>`, then restart. All schema changes are additive (new columns and tables), so the previous code runs on the migrated database.
2. While rolled back, the old code sends per-CV and per-job Telegram messages again and has no daily limit.
3. Rolling forward later is safe, because jobs inserted meanwhile are linked on start.
4. For a byte-exact rollback, restore the backup from step 1 or `data/backups/`.

## 15. Recommended next steps

1. Deploy, then watch the first two days of digests and CVs. Tune `SCORE_WEIGHTS` if needed; they live in one dictionary in `app/core.py`.
2. Add Adzuna and Jooble keys for broader NL coverage.
3. Consider a "skip" or "not interested" action in the dashboard, so unwanted queued jobs don't use daily CV slots.
4. Consider persisting the pause-alert state if repeated alerts after restarts become annoying.

## 16. PR URL and commits

- PR: https://github.com/majid-lotfian/job-hunter-agent/pull/6
- This task: `7d6f055` (scoring, CV daily limit, Telegram digests), `ee9c087` (Workday, quality fixes, AGENTS.md and template), plus the report and index commit.
- Earlier PR #6 commits: `f7c9f6e`, `8a1f29f`, `b81f027`, `e81e358` (see `docs/discovery/DEVELOPMENT_REPORT.md`).
