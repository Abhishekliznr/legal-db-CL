-- 0007_add_advocates_filing_year.sql
--
-- Reintroduces advocate data (dropped in the 2026-09-08 flattening, see
-- api-backend/db/schema.sql's rewrite note) as two flat columns on
-- cr_cases, plus a filing_year column for a coarse case-age-in-years
-- figure -- both already had working regex parsers
-- (pipeline/regex_extraction.parse_advocates,
-- normalization.case_numbers.extract_filing_year) that promotion.py never
-- called. See db/schema.sql's cr_cases definition for the column comments.
--
-- Apply with:
--   psql "$YOUR_CONN_STRING" -f db/migrations/0007_add_advocates_filing_year.sql
--
-- Then, on api-backend's database (same instance in a shared-DB deployment,
-- so likely a no-op re-run), recreate cr_case_search_view to expose the new
-- columns:
--   python -m db.init_db ensure-view
--
-- NOT run against any real database as part of writing this file. Existing
-- rows are backfilled lazily: NULL until the source PDF is re-promoted, not
-- retroactively parsed here.

ALTER TABLE cr_cases
    ADD COLUMN IF NOT EXISTS petitioner_advocate TEXT,
    ADD COLUMN IF NOT EXISTS respondent_advocate TEXT,
    ADD COLUMN IF NOT EXISTS filing_year INTEGER;
