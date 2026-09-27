"""
One directory per High Court, mirroring adapters/supreme_court/ — each gets
its own adapter.py (scraping that court's own official site) and its own
extraction.py + promotion.py (that court's own field extraction and
cr_cases promotion), registered in orchestrator/registry.py's
ADAPTER_REGISTRY. There is no shared generic High Court adapter (the old
eCourts adapter, config-driven by state_code/bench_code, is retired) — each
court's site, results format, and judgment layout gets its own pipeline,
built one court at a time.

    adapters/high_courts/mp/   — Madhya Pradesh High Court (first, in progress)
"""
