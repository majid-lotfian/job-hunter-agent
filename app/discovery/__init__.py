"""Multi-source job discovery.

Layout:
    models.py     normalized JobRecord, source outcome types and errors
    http.py       shared HTTP client: timeouts, retries with jitter, Retry-After, per-host pacing
    normalize.py  location / remote / employer / title / seniority / language / URL normalization
    relevance.py  deterministic role + location relevance classification (no LLM calls)
    sources/      one adapter per source family (ATS boards, job APIs)
    dedupe.py     cross-source duplicate detection (pure functions)
    store.py      schema migrations, idempotent upserts with provenance, lifecycle, source health
    config.py     discovery.yaml (+ legacy search.yaml sources) -> source instances
    engine.py     orchestration: bounded concurrency, health, notifications, CV queue hook
"""
