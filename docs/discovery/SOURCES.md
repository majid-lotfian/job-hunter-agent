# Job discovery sources

Research and verification date: **2026-11-05**. Every endpoint marked *implemented* was called live from the
production VM (one-off probes plus five full discovery runs against a **copy** of the production database).
"Relevant" = accepted by the deterministic relevance + location filter (AI/ML roles open to a Netherlands-based
candidate). Numbers are from the first full live run; later runs found 1 more.

## Summary

| Category | Count |
|---|---|
| Sources / source types researched | 9 job APIs, 9 ATS platforms, 34 named employers, 7 major platforms (+ ~300 employer slugs probed) |
| Implemented adapters | **18** (9 ATS + 9 job-board APIs) |
| Enabled by default | **74 source instances**: 62 employer ATS boards + 5 Workday sites + 7 job-board APIs (production also keeps `lever:sambatv` from its local `search.yaml`) |
| Live-tested successfully | **72**: all 62 default boards + 7 APIs returned valid data in 5 full runs (2 transient connect timeouts, Himalayas and Jobicy, recovered next run); Workday opt-in returned real NL vacancies for Philips, NVIDIA and Wolters Kluwer (run cut off by my time limit before ING/RELX finished) |
| Returned relevant NL vacancies in the live test | 33 source instances |
| Optional / credential-dependent | 2 (Adzuna, Jooble) - implemented, `not_configured` until keys are set; not live-tested |
| Workday | **enabled** (2026-11-05, after 2 live runs each): Philips, NVIDIA, ING (global site `ICSGBLCOR`), Elsevier (RELX `ElsevierJobs`), Wolters Kluwer; RELX generic site disabled (0 relevant NL roles, covered by Elsevier site) |
| Unsupported / blocked | LinkedIn, Indeed, Glassdoor, Welcome to the Jungle, Nationale Vacaturebank, Intermediair, Magnet.me; employer sites without a public feed (see below) |

## Job-board / aggregator APIs

| Source | Method | Status | Auth | Live test | Relevant NL jobs (unique) | Notes / terms |
|---|---|---|---|---|---|---|
| Himalayas | `himalayas.app/jobs/api/search?q=&country=Netherlands` | enabled | none | OK (142 fetched) | **95** (93) | remote jobs whose location restrictions include NL; 20/page |
| Arbeitnow | `arbeitnow.com/api/job-board-api?page=` | enabled | none | OK (1,666) | 15 (13) | EU board, many NL/DE tech jobs |
| The Muse | `themuse.com/api/public/jobs?location=&category=` | enabled | none (key optional) | OK (180) | 5 (4) | location filter only works combined with a category |
| Working Nomads | `workingnomads.com/api/exposed_jobs/` | enabled | none | OK (34) | 4 (3) | recent remote jobs |
| Remote OK | `remoteok.com/api` | enabled | none | OK (99) | 1 (1) | terms: link back + name Remote OK (source URL is kept and shown) |
| Remotive | `remotive.com/api/remote-jobs?category=` | enabled, every 6 h | none | OK (33) | 1 (0) | terms: max ~4 requests/day, link back + credit, no republishing |
| Jobicy | `jobicy.com/api/v2/remote-jobs?geo=` | enabled | none | OK (150) | 0 | terms: credit + link to original job URL |
| Adzuna | `api.adzuna.com/v1/api/jobs/nl/search/{page}` | optional | `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` (registration) | not tested (no key) | - | pricing/limits not stated on the overview page; check before use |
| Jooble | `POST jooble.org/api/{key}` | optional | `JOOBLE_API_KEY` (request form) | not tested (no key) | - | pricing/limits not stated publicly |

## Applicant tracking systems (employer boards)

All are the platforms' public, unauthenticated job-board endpoints. ATS listings are *complete*, so a job that
disappears for 2 successful scans is marked closed.

| ATS | Endpoint | Boards enabled | Relevant NL jobs (boards with hits) |
|---|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{board}/jobs` (+ `/jobs/{id}` detail, only for new promising jobs) | 22 | Nebius 19, Adyen 6, Databricks 4, Celonis 2, JetBrains 2, Catawiki, DEPT, Dataiku, GitLab, IMC, Thoughtworks |
| Lever | `api.lever.co/v0/postings/{company}?mode=json&limit=100&skip=` (+ `location=`) | 3 (+ legacy `search.yaml` boards) | jobgether 4 new (NL filter), Mendix 1 |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{board}` | 17 | ClickHouse 4, Prosus 4, DeepL, ElevenLabs, LangChain, Miro, n8n, Polarsteps |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{id}/postings?country=nl` (+ detail) | 7 | KPN 4, Devoteam, ServiceNow |
| Recruitee | `{company}.recruitee.com/api/offers/` | 9 | Eraneos 8, Conclusion 2, Centric 1 |
| Workable | `apply.workable.com/api/v1/widget/accounts/{account}?details=true` | 1 (Hugging Face) | 0 (rate-limits hard: 6 s pacing) |
| Personio | `{company}.jobs.personio.de/xml` (documented XML feed) | 1 (Ohpen) | 0 |
| Teamtailor | `{subdomain}.teamtailor.com/jobs.rss` (public RSS) | 2 (Sendcloud, Lightyear) | 0 |
| Workday | `{tenant}.wdN.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` (POST search + detail) | 5 enabled (+1 disabled) | Elsevier 4, Wolters Kluwer 4, Philips 3, NVIDIA 2, ING 0 |

**Workday:** the JSON search endpoint is not documented by Workday, but it is public, needs no login, serves the
employer's own career site, and every enabled tenant's robots.txt explicitly `Allow`s that site (only
internal/talent-community and `/refreshFacet/` paths are disallowed). Enabled at the owner's request after two
reliable live runs per site (see `docs/agent-reports/2026-11-05_1056_PR-6_scoring-cv-quota-digests-workday.md`).
ING's `ICSNLDGEN` site (configured earlier) holds only ~5 postings; the global `ICSGBLCOR` site is used instead.

Boards that were found but rarely have NL AI roles (Stripe, Datadog, Elastic, Snowflake, Samsara, HelloFresh, ...)
cost one list request per scan and are kept so new NL roles are caught; remove them to save requests.

## Named employers (candidate list from the brief)

| Employer | Careers infrastructure found | Integrated |
|---|---|---|
| Adyen | Greenhouse `adyen` | yes |
| Booking.com | own site jobs.booking.com (robots allows, crawl-delay 5); no documented feed; not on probed ATS | no |
| Prosus | Ashby `prosus` | yes |
| Picnic | SmartRecruiters `Picnic` (2 jobs); own site jobs.picnic.app disallows `/api/` for crawlers | yes (SmartRecruiters only) |
| Philips | Workday `philips` | yes |
| TomTom | not on probed ATS; Workday guess returned 422 | no |
| ING | Workday `ing` / `ICSGBLCOR` | yes |
| ABN AMRO | not found on probed ATS | no |
| Rabobank | not found (Workday 404) | no |
| ASML | own site; robots.txt disallows job-search query URLs | no (not permitted) |
| Databricks | Greenhouse `databricks` | yes |
| Microsoft / Google / Amazon NL | own career sites; no documented public job APIs | no |
| Uber | own site (SmartRecruiters `uber` has 1 non-NL job) | no |
| NVIDIA | Workday `nvidia` | yes |
| Elastic | Greenhouse `elastic` | yes |
| Mollie | Ashby `mollie` | yes |
| WeTransfer, Backbase, Deloitte, Capgemini, CGI, Accenture | not found on probed ATS (Accenture Workday 422) | no |
| Catawiki | Greenhouse `catawiki` | yes |
| MessageBird / Bird | Greenhouse `bird` (31 jobs, 0 NL) | not enabled |
| DEPT | Greenhouse `dept` | yes |
| Eraneos | Recruitee `eraneos` | yes |
| TNO, SURF, Netherlands eScience Center, Amsterdam UMC, UvA, VU | own vacancy sites; no public ATS feed found | no |

**Added from research:** Nebius, JetBrains, IMC Trading, Flow Traders, bol.com, ClickHouse, Crisp, n8n, ElevenLabs,
DeepL, Miro, Weaviate, Deeploy, Cradle, LangChain, Celonis, GitLab, Grafana Labs, Thoughtworks, Dataiku, KPN,
Coolblue, ServiceNow, Devoteam, TOPdesk, Conclusion, Centric, bunq, Nmbrs, Funda, Mendix, Ohpen, Sendcloud,
Lightyear, Hugging Face, and others (see `discovery.yaml`). DPG Media (Recruitee) was dropped: its listing exceeds the 15 MB response cap.

## Major platforms (not integrated)

| Platform | Finding |
|---|---|
| LinkedIn | job search API is partner-only; scraping prohibited by its terms |
| Indeed | no open job-search API for new integrations; scraping prohibited |
| Glassdoor | no public jobs API |
| Welcome to the Jungle, Nationale Vacaturebank, Intermediair, Magnet.me | no documented public job API found |

No scraping, browser automation, CAPTCHA or login bypass is used anywhere. Many of these platforms' vacancies
also appear on the employers' own ATS boards, which are covered directly.

## Adding a source

- Employer on a supported ATS: add one line to `discovery.yaml`, e.g. `- {board: acme, name: Acme}` under `greenhouse:`.
  Find the slug in the employer's job links (`boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`,
  `jobs.ashbyhq.com/<slug>`, `<slug>.recruitee.com`, `jobs.smartrecruiters.com/<Id>`).
- New platform: add an adapter class in `app/discovery/sources/` yielding `JobRecord`s and register it in the
  family map; add mocked tests in `tests/test_discovery.py`.
