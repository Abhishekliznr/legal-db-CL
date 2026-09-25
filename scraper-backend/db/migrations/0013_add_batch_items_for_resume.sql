-- 0013_add_batch_items_for_resume.sql
--
-- Lets a stopped batch (cancelled, source failure, server restart) resume
-- from where it stopped instead of starting over:
--   - cr_batch_items: the case list a resumable adapter discovered for a
--     batch (e.g. MP's ILRS results), with per-case status. A case is only
--     marked DONE after promotion commits, so a case interrupted mid-pipeline
--     stays PENDING and is picked up on resume.
--   - cr_scrape_batches.discovered_at: set once the case list is saved; a
--     resume skips discovery when it's set.
--   - cr_scrape_batches.run_count: 1 for the first run, +1 per resume.
--
-- Idempotent (IF NOT EXISTS guards); applied automatically at startup by
-- db/init_db.py's apply_migrations().

ALTER TABLE cr_scrape_batches
    ADD COLUMN IF NOT EXISTS discovered_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS run_count INT NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS cr_batch_items (
    item_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id      BIGINT NOT NULL REFERENCES cr_scrape_batches(batch_id) ON DELETE CASCADE,
    position      INT NOT NULL,
    item_key      TEXT NOT NULL,
    payload       JSONB NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING / DONE / SKIPPED / FAILED
    reason        TEXT,
    ingestion_id  BIGINT REFERENCES cr_raw_ingestions(ingestion_id),
    attempts      INT NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_cr_batch_items_key UNIQUE (batch_id, item_key)
);

CREATE INDEX IF NOT EXISTS ix_cr_batch_items_batch_status ON cr_batch_items(batch_id, status);
