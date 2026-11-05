# Agent reports

Detailed reports for substantial development tasks done by AI coding agents, committed in the same PR as the code
(convention: [`AGENTS.md`](../../AGENTS.md), structure: [`TEMPLATE.md`](TEMPLATE.md)).
Newest first - **the first row is the latest report.** Times are Europe/Amsterdam.

| Date / time | Task | PR | Status | Report | Summary |
|---|---|---|---|---|---|
| 2026-11-05 12:34 | Approval workflows, AI keyword scoring, CV worker throughput, application submission framework | [#7](https://github.com/majid-lotfian/job-hunter-agent/pull/7) (stacked on #6) | open (not merged, not deployed) | [report](2026-11-05_1234_PR-7_approvals-keywords-applications.md) | Telegram + dashboard Approve & Apply / Approve CV & Apply with version-bound approvals, strong AI keyword floor (>=70), no fixed daily CV limit, and a tested submission framework (APPLY_MODE=off; no permitted live adapter yet). |
| 2026-11-05 10:56 | Scoring, CV daily limit, Telegram digests, Workday, reporting workflow | [#6](https://github.com/majid-lotfian/job-hunter-agent/pull/6) | open (not merged, not deployed) | [report](2026-11-05_1056_PR-6_scoring-cv-quota-digests-workday.md) | Evidence-based scoring, max 5 automatic CVs/day, one Telegram digest per scan/batch, 5 Workday employers enabled after live tests, and this reporting convention. |
| 2026-11-05 01:30 | Multi-source job discovery redesign | [#6](https://github.com/majid-lotfian/job-hunter-agent/pull/6) | open (not merged, not deployed) | [report](../discovery/DEVELOPMENT_REPORT.md) | Replaced 2 Lever boards with 69 verified sources, cross-source dedupe, lifecycle and source health (190 relevant jobs from 107 employers in the live test). |
