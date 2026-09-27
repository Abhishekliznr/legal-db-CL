"""
Orchestrator — owns every scrape_batches/raw_ingestions write.
See legal-db/docs/scraper-backend-revamp-spec.md §4.4.

    orchestrator/batch_runner.py — creates scrape_batches row, calls an adapter,
                                    checksum-dedups, uploads PDF to blob, inserts
                                    raw_ingestions rows, then runs the pipeline
                                    (pipeline/ocr.py -> promotion.py -> llm_enrichment.py)
                                    for each new record
    orchestrator/registry.py     — court adapter -> pipeline registry (worker.py resolves from it)
    orchestrator/live_logs.py    — batched live log writer (cr_batch_logs)
    orchestrator/shutdown.py     — SIGTERM -> stop after the current record
"""
