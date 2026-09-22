-- 0001_add_cr_cases_cnr.sql
--
-- Companion to scraper-backend's db/migrations/0011_add_cr_cases_cnr.sql.
-- cr_cases is created/owned by scraper-backend, but on a shared (or simply
-- already-initialized) database, api-backend's own ensure_schema() never
-- re-runs schema.sql's CREATE TABLE once cr_cases exists -- so a column
-- added there later (like cnr) never reaches an already-existing table on
-- api-backend's side, even though schema.sql's own ix_cr_cases_cnr index
-- and view_supplement.sql's cr_case_search_view both assume it's there.
-- This migration applies the same idempotent ALTER directly so api-backend
-- self-heals regardless of whether scraper-backend has redeployed yet.

ALTER TABLE cr_cases
    ADD COLUMN IF NOT EXISTS cnr TEXT;

CREATE INDEX IF NOT EXISTS ix_cr_cases_cnr ON cr_cases(cnr) WHERE cnr IS NOT NULL;
