-- 0005_add_structured_content.sql
--
-- Adds cr_cases.structured_content (JSONB, nullable) -- the deterministic
-- StructuredJudgment representation produced by parsers/judgment_parser.py
-- (parsers/schema.py) at promotion time: numbered paragraphs (each
-- addressable as "para-N"), headings, document extracts (FIR/recovery
-- memo/disclosure statement/demarcation memo/etc.), citations, statutory
-- references, final order, and removed OCR artifacts. Computed once from
-- cr_cases.ocr_text alone, no LLM involved, and never touched by
-- pipeline/llm_enrichment.py's separate enrichment pass.
--
-- Idempotent: IF NOT EXISTS guard makes this safe to run more than once,
-- and a no-op against a database built from a fresh db/schema.sql that
-- already includes the column.
--
-- This migration only adds the column -- it does NOT backfill existing
-- promoted rows (structured_content stays NULL for them). Backfilling is a
-- separate, explicit operation: for each cr_cases row with
-- structured_content IS NULL, call parsers.judgment_parser.parse_judgment()
-- against its own ocr_text/bench/judgment_date/case_number/neutral_citation
-- and UPDATE just that column -- ocr_text itself is never touched by a
-- backfill.
--
-- Apply with:
--   psql "$YOUR_CONN_STRING" -f db/migrations/0005_add_structured_content.sql
--
-- NOT run against any real database as part of writing this file.

ALTER TABLE cr_cases
    ADD COLUMN IF NOT EXISTS structured_content JSONB;
