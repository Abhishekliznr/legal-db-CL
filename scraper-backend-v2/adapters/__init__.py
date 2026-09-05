"""
Scraper adapters — one per data source, behind a common interface.

Phase 1 (Supreme Court, sci.gov.in) and Phase 2 (generic eCourts adapter for
all High Courts) fill this package in. See
legal-db/docs/scraper-backend-revamp-spec.md §4.

Not built yet:
    adapters/base.py                 — ScraperAdapter protocol + RawJudgmentRecord (§4.1)
    adapters/supreme_court/adapter.py — sci.gov.in (§4.2)
    adapters/ecourts/adapter.py       — generic, config-driven, all High Courts (§4.3)
"""
