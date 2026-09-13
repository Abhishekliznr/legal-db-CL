-- 0006_drop_structured_content.sql
--
-- Removes cr_cases.structured_content (JSONB) -- the deterministic
-- StructuredJudgment representation produced by parsers/judgment_parser.py.
-- That parser (and parsers/html_renderer.py, its HTML rendering
-- counterpart) has been deleted: no frontend ever rendered this column
-- (case-detail-view.tsx always showed `judgement`/the source PDF instead),
-- so the whole structured-JSON/HTML-parsed-view feature is being removed
-- from scraper-backend, api-backend, and legal-ui together.
--
-- cr_case_search_view (api-backend's own view, api-backend/db/
-- view_supplement.sql) selects this column, so it must be dropped BEFORE
-- the ALTER TABLE below -- Postgres refuses to drop a column a view
-- depends on. CREATE OR REPLACE VIEW cannot itself remove a column (it can
-- only append at the end), which is why this is a DROP, not a replace.
--
-- Apply with:
--   psql "$YOUR_CONN_STRING" -f db/migrations/0006_drop_structured_content.sql
--
-- Then recreate cr_case_search_view without the column, from api-backend:
--   python -m db.init_db ensure-view
--
-- NOT run against any real database as part of writing this file.

DROP VIEW IF EXISTS cr_case_search_view;

ALTER TABLE cr_cases
    DROP COLUMN IF EXISTS structured_content;
