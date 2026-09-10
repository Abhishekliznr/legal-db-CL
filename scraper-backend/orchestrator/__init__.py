"""
Orchestrator — owns every scrape_batches/raw_ingestions write.
See legal-db/docs/scraper-backend-revamp-spec.md §4.4.

    orchestrator/batch_runner.py — creates scrape_batches row, calls an adapter,
                                    checksum-dedups, uploads PDF to blob, inserts
                                    raw_ingestions rows, then runs the pipeline
                                    (pipeline/ocr.py -> promotion.py -> llm_enrichment.py)
                                    for each new record
    orchestrator/job_registry.py — status queries over raw_ingestions (no
                                    in-memory job-state dicts)
"""
