-- 0003_batch_actors.sql
--
-- Who did what to a batch, for the admin panel:
-- - cr_scrape_batches.requested_by_*: the admin who started it.
-- - cr_batch_events.actor_*: the admin behind STARTED / STOP_REQUESTED / RESUMED.
--   NULL on events the worker or the stale-batch sweep write themselves.
-- Name and email are snapshots taken at the time, so they stay correct after an
-- admin is renamed or removed. Batches from before this migration have NULLs.
--
-- Also indexes the admin panel's new reads: batch history filtered by court and
-- ordered by request time, and cases added per day (cr_cases.created_at).
--
-- Mirrored by scraper-backend's db/migrations/0016_batch_actors.sql
-- (whichever service starts first applies it). Idempotent.

ALTER TABLE cr_scrape_batches ADD COLUMN IF NOT EXISTS requested_by_id TEXT;
ALTER TABLE cr_scrape_batches ADD COLUMN IF NOT EXISTS requested_by_name TEXT;
ALTER TABLE cr_scrape_batches ADD COLUMN IF NOT EXISTS requested_by_email TEXT;

ALTER TABLE cr_batch_events ADD COLUMN IF NOT EXISTS actor_id TEXT;
ALTER TABLE cr_batch_events ADD COLUMN IF NOT EXISTS actor_name TEXT;
ALTER TABLE cr_batch_events ADD COLUMN IF NOT EXISTS actor_email TEXT;

CREATE INDEX IF NOT EXISTS ix_cr_scrape_batches_court_requested ON cr_scrape_batches(court_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS ix_cr_scrape_batches_requested_by ON cr_scrape_batches(requested_by_id) WHERE requested_by_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_cr_cases_created_at ON cr_cases(created_at);
