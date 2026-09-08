"""
Orchestrator — owns every scrape_batches/raw_ingestions write.

Phase 1, not yet built. See legal-db/docs/scraper-backend-revamp-spec.md §4.4.

Not built yet:
    orchestrator/batch_runner.py — creates scrape_batches row, calls an adapter,
                                    checksum-dedups, uploads PDF to blob, inserts
                                    raw_ingestions rows
    orchestrator/job_registry.py — status queries over raw_ingestions (no
                                    in-memory job-state dicts)
"""
