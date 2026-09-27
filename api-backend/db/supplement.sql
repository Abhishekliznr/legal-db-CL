-- =====================================================================
-- API-BACKEND-V2-ONLY SUPPLEMENT
--
-- Split into its own file (rather than living inline at the end of
-- schema.sql, where it started) so it can be applied on its own via
-- `python -m db.init_db ensure-supplement` — the real deployment shape in
-- a shared-DB setup, where scraper-backend owns `init` (schema.sql,
-- CREATE TYPE and all) and api-backend only needs to add this one extra
-- table afterward, idempotently, without re-running anything scraper-
-- backend-v2 already created. Unlike schema.sql, everything here uses
-- IF NOT EXISTS specifically so it's safe to run more than once.
--
-- Deliberately NOT in scraper-backend's copy of schema.sql — per-user
-- search history for legal-ui's case-research feature; the scraper never
-- reads or writes this.
-- =====================================================================

CREATE TABLE IF NOT EXISTS cr_search_history (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT NOT NULL,      -- opaque caller-supplied id (legal-ui's NextAuth session), no users table here
    query       TEXT NOT NULL,      -- display label only
    -- The results page's URL query string (e.g. "all=bail&prov=..."), replayed as-is by
    -- "search again". NULL on rows recorded before this column existed -- those replay `query`
    -- as a plain text search instead.
    params      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Tables created before `params` existed -- CREATE TABLE IF NOT EXISTS above won't add it.
ALTER TABLE cr_search_history ADD COLUMN IF NOT EXISTS params TEXT;

CREATE INDEX IF NOT EXISTS ix_cr_search_history_user ON cr_search_history(user_id, created_at DESC);
