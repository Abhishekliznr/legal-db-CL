-- 0004_add_batch_finished_at.sql
--
-- Adds cr_scrape_batches.finished_at (TIMESTAMPTZ, nullable), set by
-- db/scrape_jobs.py::finish_batch() alongside `status`. Without this there
-- was no way to compute a finished batch's duration at all — only
-- requested_at existed, and nothing marked when a RUNNING batch actually
-- stopped. The admin UI's "Duration"/"Elapsed Time" field needs this to be
-- real data instead of a placeholder.
--
-- Idempotent: IF NOT EXISTS guard makes this safe to run more than once,
-- and a no-op against a database built from a fresh db/schema.sql that
-- already includes the column.
--
-- Apply with:
--   psql "$YOUR_CONN_STRING" -f db/migrations/0004_add_batch_finished_at.sql
--
-- NOT run against any real database as part of writing this file.

ALTER TABLE cr_scrape_batches
    ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
