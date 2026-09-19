-- 0011_add_cr_cases_cnr.sql
--
-- Madhya Pradesh High Court's case-status page exposes a CNR (the pan-India
-- eCourts case number record) directly in its own "Case No." cell --
-- adapters/high_courts/mp/case_status.py now parses it, but cr_cases had no
-- column to write it into. Nullable: only adapters whose source page
-- actually exposes a CNR will ever populate this.
--
-- NOT run against any real database as part of writing this file.

ALTER TABLE cr_cases
    ADD COLUMN IF NOT EXISTS cnr TEXT;

CREATE INDEX IF NOT EXISTS ix_cr_cases_cnr ON cr_cases(cnr) WHERE cnr IS NOT NULL;
