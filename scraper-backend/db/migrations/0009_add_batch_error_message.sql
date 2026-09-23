-- 0009_add_batch_error_message.sql
--
-- orchestrator/batch_runner.py's run_batch() has always called
-- db.scrape_jobs.finish_batch(..., error_message=str(exc)) on a source
-- failure (SOURCE_BLOCKED/RATE_LIMITED/SOURCE_UNAVAILABLE/STRUCTURE_CHANGED)
-- or a top-level adapter exception (FAILED) -- but finish_batch() had no
-- error_message column/parameter to actually persist it, so that code path
-- silently raised an unhandled TypeError instead of cleanly recording the
-- failure. Found live during this migration's own verification: a real
-- sci.gov.in HTTP 403 crashed with exactly this TypeError.
--
-- NOT run against any real database as part of writing this file (found
-- and fixed against a throwaway scratch database, not a real one).

ALTER TABLE cr_scrape_batches
    ADD COLUMN IF NOT EXISTS error_message TEXT;
