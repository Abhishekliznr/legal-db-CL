-- 0015_on_demand_worker.sql
--
-- scraper-backend now runs as a one-batch-per-pod Job (python -m worker),
-- launched by api-backend through the trigger service; api-backend serves
-- every batch endpoint. The database is the only channel between the two:
--
-- - status QUEUED: api-backend created the batch and launched a Job, no
--   worker has claimed it yet (worker flips QUEUED -> RUNNING atomically).
-- - heartbeat_at: bumped every 30s by the running worker. Replaces the old
--   startup-time fail_orphaned_batches(), which would now fail every other
--   pod's RUNNING batch; api-backend fails batches whose heartbeat went stale.
-- - queued_at / claimed_at / job_name: when the latest run was queued, when a
--   worker picked it up, and which K8s Job that was. A QUEUED run nobody
--   claims within 15 min is failed by api-backend (the pod never started).
-- - cr_batch_logs: the live log lines the worker used to keep in memory,
--   tailed by api-backend's SSE endpoint. Pruned after 7 days by api-backend.
--
-- Mirrored by api-backend's db/migrations/0002_on_demand_worker.sql
-- (whichever service starts first applies it). Idempotent.

ALTER TABLE cr_scrape_batches ALTER COLUMN status SET DEFAULT 'QUEUED';
ALTER TABLE cr_scrape_batches ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
ALTER TABLE cr_scrape_batches ADD COLUMN IF NOT EXISTS job_name TEXT;
ALTER TABLE cr_scrape_batches ADD COLUMN IF NOT EXISTS queued_at TIMESTAMPTZ;
ALTER TABLE cr_scrape_batches ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_cr_scrape_batches_active
    ON cr_scrape_batches(court_id) WHERE status IN ('QUEUED', 'RUNNING');

CREATE TABLE IF NOT EXISTS cr_batch_logs (
    log_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id     BIGINT NOT NULL REFERENCES cr_scrape_batches(batch_id) ON DELETE CASCADE,
    run_number   INT NOT NULL,
    logged_at    TIMESTAMPTZ NOT NULL,
    level        TEXT NOT NULL,
    stage        TEXT,
    case_ref     TEXT,
    item_index   INT,
    item_total   INT,
    message      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_cr_batch_logs_batch ON cr_batch_logs(batch_id, log_id);
CREATE INDEX IF NOT EXISTS ix_cr_batch_logs_logged_at ON cr_batch_logs(logged_at);
