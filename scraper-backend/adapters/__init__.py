"""
Scraper adapters — one per court, behind a common interface. See
legal-db/docs/scraper-backend-revamp-spec.md §4.

    adapters/base.py                  — ScraperAdapter protocol + RawJudgmentRecord (§4.1)
    adapters/supreme_court/adapter.py — sci.gov.in (§4.2)
    adapters/high_courts/<code>/      — one directory per High Court, each with
                                         its own adapter + extraction/promotion
                                         pipeline (no shared generic eCourts
                                         adapter — each High Court scrapes its
                                         own official site)
"""
