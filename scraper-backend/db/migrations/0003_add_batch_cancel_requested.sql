-- 0003_add_batch_cancel_requested.sql
--
-- Adds cr_scrape_batches.cancel_requested (BOOLEAN, default FALSE), the flag
-- POST /api/scraper/batches/{id}/cancel sets and orchestrator/batch_runner.py's
-- per-record loop polls between records to stop a running batch early. Also
-- widens the status column's documented value set to include 'CANCELLED'
-- (status itself is plain TEXT, not an enum, so no type change is needed —
-- see db/schema.sql's own comment on cr_scrape_batches.status).
--
-- Idempotent: IF NOT EXISTS guards make this safe to run more than once, and
-- a no-op against a database that already has the column (e.g. one built
-- from a fresh db/schema.sql that already includes it).
--
-- Apply with:
--   psql "$YOUR_CONN_STRING" -f db/migrations/0003_add_batch_cancel_requested.sql
--
-- NOT run against any real database as part of writing this file.

ALTER TABLE cr_scrape_batches
    ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE;
