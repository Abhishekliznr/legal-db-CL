"""
Scraper adapters — one per data source, behind a common interface. See
legal-db/docs/scraper-backend-revamp-spec.md §4.

    adapters/base.py                  — ScraperAdapter protocol + RawJudgmentRecord (§4.1)
    adapters/supreme_court/adapter.py — sci.gov.in (§4.2)
    adapters/ecourts/adapter.py       — generic, config-driven, all 25 High
                                         Courts, resolved via cr_court_scrape_config (§4.3)
"""
