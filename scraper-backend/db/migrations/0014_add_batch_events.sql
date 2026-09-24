-- 0014_add_batch_events.sql
--
-- History of what happened to a batch across its runs, for the admin batch
-- page's timeline: STARTED, DISCOVERED, STOP_REQUESTED, RESUMED, INTERRUPTED
-- (server restart), and one finish event per run named after the status it
-- ended with (COMPLETED / CANCELLED / FAILED / SOURCE_UNAVAILABLE / ...).
-- cr_scrape_batches only keeps the latest status/error_message, which a
-- resume overwrites, so the reasons for earlier stops were lost.
--
-- Each event is written in the same transaction as the state change it
-- records (db/scrape_jobs.py). Batches from before this migration have no
-- events; get_batch() derives a minimal timeline from their columns instead.
--
-- Idempotent; applied automatically at startup by db/init_db.py.

CREATE TABLE IF NOT EXISTS cr_batch_events (
    event_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id     BIGINT NOT NULL REFERENCES cr_scrape_batches(batch_id) ON DELETE CASCADE,
    run_number   INT NOT NULL,
    event_type   TEXT NOT NULL,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    message      TEXT,
    details      JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_cr_batch_events_batch ON cr_batch_events(batch_id, occurred_at);
