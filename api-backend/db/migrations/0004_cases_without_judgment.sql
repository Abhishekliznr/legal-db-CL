-- 0004_cases_without_judgment.sql
--
-- A case the court lists but whose judgment PDF is missing or won't download is now
-- saved with its metadata instead of being skipped:
-- - cr_cases.judgment_status: AVAILABLE (has a judgment), NOT_PUBLISHED (no PDF listed
--   yet) or DOWNLOAD_FAILED. A later run that gets the PDF fills the same row in and
--   sets it back to AVAILABLE, keeping its case_id and liznr_id.
-- - cr_batch_items.status gains NO_JUDGMENT (saved without judgment), and case_id links
--   an item to the case it saved, so a batch can list and retry them.
--
-- Mirrored by scraper-backend's db/migrations/0017_cases_without_judgment.sql
-- (whichever service starts first applies it). Idempotent.

ALTER TABLE cr_cases ADD COLUMN IF NOT EXISTS judgment_status TEXT NOT NULL DEFAULT 'AVAILABLE';
ALTER TABLE cr_cases ADD COLUMN IF NOT EXISTS judgment_missing_reason TEXT;
ALTER TABLE cr_cases ADD COLUMN IF NOT EXISTS judgment_checked_at TIMESTAMPTZ;

DO $$ BEGIN
    ALTER TABLE cr_cases
        ADD CONSTRAINT ck_cr_cases_judgment_status
        CHECK (judgment_status IN ('AVAILABLE', 'NOT_PUBLISHED', 'DOWNLOAD_FAILED'));
EXCEPTION WHEN duplicate_object THEN null;
END $$;

CREATE INDEX IF NOT EXISTS ix_cr_cases_missing_judgment
    ON cr_cases(court_id, judgment_status) WHERE judgment_status <> 'AVAILABLE';

ALTER TABLE cr_batch_items ADD COLUMN IF NOT EXISTS case_id BIGINT;

DO $$ BEGIN
    ALTER TABLE cr_batch_items
        ADD CONSTRAINT fk_cr_batch_items_case
        FOREIGN KEY (case_id) REFERENCES cr_cases(case_id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN null;
END $$;

CREATE INDEX IF NOT EXISTS ix_cr_batch_items_case ON cr_batch_items(case_id) WHERE case_id IS NOT NULL;
