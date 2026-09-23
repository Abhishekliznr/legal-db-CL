-- 0010_add_cr_advocates.sql
--
-- Madhya Pradesh High Court's case-status page gives a real, stable
-- advocate identity per case (enrollment number + year), unlike Supreme
-- Court's source data which only ever has a bare name. Adds a proper
-- lookup table for that, plus the array-column relation to cr_cases
-- matching this schema's existing convention (bench/sections/acts/etc.) --
-- the existing flat petitioner_advocate/respondent_advocate TEXT columns
-- (migration 0007) are untouched and stay Supreme-Court-only.
--
-- Also adds the 'MPHC_WEBSITE' data_source_enum value for MP's own
-- adapter (routers/scraper_router.py's _ADAPTER_REGISTRY).
--
-- NOT run against any real database as part of writing this file.

CREATE TABLE IF NOT EXISTS cr_advocates (
    advocate_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    advocate_name   TEXT NOT NULL,
    enrollment_no   TEXT NOT NULL,
    enrollment_year INTEGER,
    CONSTRAINT uq_cr_advocates_enrollment_no UNIQUE (enrollment_no)
);

ALTER TABLE cr_cases
    ADD COLUMN IF NOT EXISTS petitioner_advocate_ids BIGINT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS respondent_advocate_ids BIGINT[] NOT NULL DEFAULT '{}';

ALTER TYPE data_source_enum ADD VALUE IF NOT EXISTS 'MPHC_WEBSITE';
