# Job discovery redesign - development report

Branch `feature/multi-source-discovery`, based on `main` @ `ea98faf` (PR #5). Production was not touched:
all live tests ran against copies of the production database in a scratch directory.

## 1. Audit of the previous implementation (why only 5 jobs)

| Area | Finding |
|---|---|
| Sources | Only 2 boards configured, both Lever (`search.yaml`, VM-local): `jobgether` and `sambatv`. Greenhouse list empty. No other source types existed in code. |
| jobgether | A worldwide **aggregator** board (3,868 postings), not an employer. Only **128** are in the Netherlands. The employer is hidden. |
| sambatv | 61 postings, none relevant. |
| Filtering of the 3,929 postings | 3,012 no title/keyword match, 808 rejected by location (US 192, India 78, ...), 22 excluded terms, 22 below `min_score` 25 → **4 accepted** (+1 accepted earlier). |
| Resource use | Every hourly scan downloaded **46.7 MB** of JSON (peak ~226 MB RSS in a test) on a 1 GB VM with ~320 MB free. |
| Persistence | `INSERT OR IGNORE`: jobs never updated; no last-seen; no closure; no provenance. `published_at` stored as raw epoch-ms string. |
| Dedup | Only `(source, source_id)` uniqueness; no cross-source matching. |
| Pagination | None (Lever returned everything at once). |
| Date filter | `max_age_days: 30` applied to `createdAt` (fine for aggregators). |
| Scoring | Titles list is narrow (see §7). |
| Observability | Log lines only; the `checked` counter was cumulative across boards; no per-source health. |
| Dashboard | No filters hiding jobs: the 5 jobs were genuinely all that was stored. |
| Scheduling | Hourly APScheduler job with an asyncio lock: no overlapping scans (kept). |
| Tests | No discovery tests. |

## 2. Design

`app/discovery/` (no new dependencies; httpx, PyYAML, stdlib only):

- **Adapters** (`sources/ats.py`, `sources/boards.py`) yield normalized `JobRecord`s. Two-phase sources
  (Greenhouse, SmartRecruiters, Workday) list first and fetch details only for new/changed postings that pass a
  cheap title + location pre-filter.
- **HTTP** (`http.py`): one pooled client, per-host pacing (0.5-6 s), timeouts, 2 retries with exponential
  backoff + jitter, `Retry-After` (capped 60 s), 15 MB streamed size cap, `SourceRateLimited` / `SourceError`.
- **Normalization** (`normalize.py`): NL geography (Randstad/commuting list vs. rest of NL), remote/hybrid/onsite,
  remote-region eligibility (Europe/EMEA/worldwide vs. US-only), employer key (B.V./N.V./suffixes, aliases),
  title key (gender markers, location qualifiers, ML/AI synonyms), seniority markers (incl. II/III), employment
  type, posting language and Dutch requirement, canonical URLs, ATS job ids found in URLs, robust date parsing.
  Unknown values stay `NULL`.
- **Relevance** (`relevance.py`): deterministic tiers `primary` / `secondary` / `possible`; exclusions (intern,
  junior, PhD candidate, sales, PM, designer, content, AI trainer/annotator, ...). Context-dependent titles
  (Research Engineer, AI Consultant, Data Scientist, Python Developer, ...) need AI/ML evidence in the
  description; unusual titles with strong AI/ML descriptions are kept as `possible` instead of discarded.
- **Store** (`store.py`): versioned migrations (v1 provenance/health/runs, v2 rejected-posting memory), backup
  before migrating, idempotent upsert with provenance, lifecycle, health.
- **Engine** (`engine.py`): bounded concurrency (3), per-source interval, per-source isolation, batched
  "seen" updates, CV queue hook and Telegram policy.
- **Config**: `discovery.yaml` (new, committed). `search.yaml` stays the scoring config; its `sources:` boards are
  still merged in, so the VM-local `lever: [jobgether, sambatv]` keeps working without editing it.

## 3. Deduplication

Layers (strongest first): exact `(source_key, source_job_id)` → canonical posting/apply URL → ATS job id in any
URL → fuzzy (same employer key **and** title key **and** seniority **and** compatible location, never two ids from
the same board, description shingle-Jaccard ≥ 0.35 when both descriptions ≥ 400 chars; otherwise both cities must
be known and equal). Closed jobs are never merged into (a repost is a new job). On merge, the employer's own
(direct) posting becomes the primary link; all source links are kept in `job_sources` and shown in the dashboard.

**Tests:** merge cases (cross-source copy, `Netherlands` vs `Amsterdam`, ATS id in an aggregator apply URL,
canonical URL with tracking params) and non-merge cases (different seniority, same board different requisition,
different city, remote vs city, different description, different employer, closed job, too little evidence),
repeated-scan idempotency, migrated legacy jobs re-scanned without duplication.

**Live:** 6 cross-source duplicates consolidated in the first full run (Databricks and Nebius roles seen on both
their Greenhouse boards and Arbeitnow/Himalayas; a Celonis role on The Muse; a Lemon.io role on Remotive and
Working Nomads); all 6 manually checked as correct. Runs 2-5: 0 new duplicates created.

## 4. Live verification (copies of the production DB, real endpoints)

| Run | Postings checked | New | Merged | HTTP requests | Detail fetches | Duration | Peak RSS |
|---|---|---|---|---|---|---|---|
| 1 (baseline) | 9,745 | 189 | 5 (+1 within-run) | n/a | n/a | 12.7 min | 80 MB |
| 2 | 9,605 | 0 | 0 | n/a | n/a | 12.4 min | 83 MB |
| 3 (rejection memory filling) | 9,747 | 0 | 1 | 398 | 297 | 11.5 min | 79 MB |
| 4 (memory, before CPU fixes) | 9,598 | 0 | 0 | 104 | 0 | 8.9 min | 76 MB |
| 5 (final code) | 9,748 | 1 (genuinely new posting) | 0 | **96** | **0** | **89 s** | **66 MB** |

Result of run 1: **190 new logical jobs (190 active) from 107 distinct employers**; tiers primary 136, secondary
38, possible 16; regions: Randstad 73, remote open to NL 95, remote Europe 7, remote worldwide 10, other 5;
language: English 163, Dutch required 8, Dutch preferred 4, Dutch-language posting 15.
Per-source numbers: `SOURCES.md`.

Existing data: the 5 legacy jobs, their statuses (`approved`, `needs_revision`, `new`), CV paths, `cv_jobs` rows
and events were preserved. Re-scanning jobgether matched them (no duplicates). Legacy job #3 ("Data Scientist -
AI & Experimentation") is no longer on jobgether's NL listing; after two complete scans without it, it was marked
`closed` (user status untouched; it simply drops out of the default dashboard view, toggle "show closed/stale").

Isolation was observed live: Himalayas (run 2) and Jobicy (run 4) had connect timeouts after retries; every other
source completed and nothing was closed.

## 5. Tests

`python -m unittest discover -s tests`: **122 tests OK** (72 existing + 50 new in `tests/test_discovery.py`).
New coverage: HTTP retries/`Retry-After`/cap/pacing/timeouts/4xx; every adapter with real-shaped payloads incl.
pagination, truncation and invalid responses; credentials for optional sources; normalization; relevance;
dedupe; idempotent rescans; lifecycle (close after 2 complete misses, reopen, never close on failed/empty/
truncated scans, stale for search-only jobs); source health statuses (ok/partial/failed/rate_limited/skipped/
disabled/not_configured); notification dedupe, cap and baseline digest; CV queue hook (once, relevant only);
record-level error isolation; rejected-posting memory; migration of a legacy DB (incl. backup, idempotency);
API integration (provenance, closed hidden, health endpoint auth); config incl. legacy board merge.

## 6. Performance and resources

- Hourly steady-state scan: ~1.5 min, ~100 requests, ~66 MB peak RSS in a standalone process (production service
  idles at ~77 MB RSS; discovery adds a few tens of MB, briefly).
- First scan after deploy: ~12-13 min (detail fetches for every promising NL posting); runs in the background
  under the existing scan lock; the dashboard stays responsive.
- jobgether: server-side `location=Netherlands` filter + pagination: 47 MB → ~1.5 MB per scan.
- No LLM calls during discovery. No browser. No new Python dependencies.

## 7. Scoring weaknesses (documented, NOT changed)

The existing `score_job` (search.yaml) is unchanged and `CV_MIN_SCORE` stays 70. Observed in the live data:

- `titles_any` only contains AI Engineer, Machine Learning Engineer, Applied AI, AI Scientist, LLM Engineer. Relevant
  titles such as **ML Engineer**, Deep Learning Engineer, Applied Scientist, Research Engineer, NLP Engineer,
  Machine Learning Scientist score 20-40 and therefore never get automatic CVs.
- Any title match + one keyword reaches 70, so the score does not distinguish strong from weak matches above 70.
- `max_age_days` is now applied to aggregator postings only; an employer's complete listing is evidence the job is
  open (direct postings older than 30 days previously scored -1).

Suggested (your decision): add `ML Engineer`, `Deep Learning`, `Applied Scientist`, `Research Engineer`, `NLP`,
`Generative AI`, `MLOps` to `titles_any`.

## 8. Limitations / open issues

- **CV volume on first deploy:** 57 of the 190 new jobs score ≥ 70 → the CV worker would generate ~57 Gemini CVs
  (paced at ≥15 s each) and send ~57 "CV ready" Telegram messages over the following hour(s). See deployment step 4.
- Himalayas contributes about half of the relevant jobs (remote roles open to NL residents per Himalayas' data).
- Employers without a public ATS feed (Booking, TomTom, ABN AMRO, Rabobank, ASML, Microsoft, Google, Amazon,
  universities, TNO) are not covered; Workday employers are opt-in.
- Jobs from aggregator boards that hide the employer (jobgether) can only be deduplicated by exact id/URL.
- Fuzzy dedupe is deliberately conservative: two identical titles at the same employer on the same board stay
  separate (e.g. two "Senior ML Engineer, Ads" at Reddit on Himalayas).
- Adzuna and Jooble adapters are untested against live data (no keys).
- The live-test runs used `force=True`; in production, per-source intervals apply (Remotive/Remote OK 6 h,
  boards hourly).

## 9. Deployment steps (after review; production untouched so far)

1. **Backup** (the app also backs up automatically before migrating, to `data/backups/`):
   `cp data/jobs.sqlite3 ~/job-hunter-backups/jobs-pre-discovery.sqlite3`
2. **Merge** the PR, then on the VM update the checkout keeping the VM-local edits (`app/main.py`,
   `deploy/job-hunter.service`, `search.yaml` are not touched by this PR, so a fast-forward works):
   `git fetch origin && git merge --ff-only origin/main`
3. **Tests:** `.venv/bin/python -m unittest discover -s tests`
4. **Decide the first-scan CV burst** (≈57 CVs). Options: accept it; or temporarily set `CV_MIN_SCORE=85` (or
   `CV_AUTO_GENERATE=false`) in `.env` before the restart and lower it later - completed CVs are never regenerated,
   and the worker's backfill queues the remaining ≥70 jobs whenever you lower it again.
5. **Restart** `job-hunter.service` (only that service). On start: schema migration v0→v2 (additive) + backup,
   then the first scan (~13 min). Telegram receives one digest for the new sources instead of ~130 messages.
6. **Verify:** dashboard → *Sources* panel (all `ok`), job list; `journalctl -u job-hunter.service | grep Discovery`.

**Rollback:** stop the service, `git checkout <previous commit>`, restart. The migration is additive (new columns
and tables only), so the previous code runs on the migrated database unchanged; jobs discovered by the new engine
stay visible as ordinary rows. Rolling forward again later is safe: jobs the old code inserted meanwhile are linked
on start (`link_orphans`). Caveat: while rolled back, the old code would re-insert jobgether postings that the new
engine had stored, as separate rows. For a byte-exact rollback, restore the backup from step 1 or `data/backups/`.

## 10. Recommendations for the next iteration

1. Decide on the scoring title list (§7) - the biggest lever for which relevant jobs get CVs.
2. Enable Workday for the employers you care about (Philips, NVIDIA, ING, RELX, Wolters Kluwer); add more tenants.
3. Register free Adzuna/Jooble keys for broader NL coverage, then compare yield and duplicates.
4. Add employers by slug as you notice them (one line each); prune boards that never yield NL AI roles.
5. Consider an "employer allowlist/blocklist" and a commute-time setting (per city) in `discovery.yaml`.
6. Consider showing "Dutch required" jobs separately or down-ranked in the dashboard.
