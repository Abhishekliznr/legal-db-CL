-- =====================================================================
-- API-BACKEND-V2-ONLY SUPPLEMENT: admin-configurable filter/search metadata
--
-- Split into its own file (moved out of schema.sql on 2026-09-08, when
-- scraper-backend's own schema rewrite dropped these three tables
-- entirely — see schema.sql's header) so it can be applied on its own via
-- `python -m db.init_db ensure-filters`, the real deployment shape in a
-- shared-DB setup: scraper-backend owns `init` (schema.sql, CREATE TYPE
-- and all) but never creates these, since it has no code path that reads
-- or writes any of them — api-backend owns them unconditionally,
-- regardless of whether it's running standalone or against a database
-- scraper-backend already initialized. Everything here uses
-- IF NOT EXISTS / CREATE OR REPLACE specifically so it's safe to re-run.
--
-- Serves GET /api/cases/filters and GET /api/cases/searches
-- (routers/filter_router.py, routers/search_router.py) — presentation
-- config, not case data, seeded by db/seed_filters.py.
-- =====================================================================

CREATE TABLE IF NOT EXISTS cr_filter_definitions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key             TEXT UNIQUE NOT NULL,
    label           TEXT NOT NULL,
    type            TEXT NOT NULL DEFAULT 'select',
    selection_mode  TEXT NOT NULL DEFAULT 'multi',
    query_key       TEXT,
    data_source     TEXT NOT NULL DEFAULT 'database',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    is_searchable   BOOLEAN NOT NULL DEFAULT FALSE,
    display_order   INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cr_filter_options (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filter_id       UUID NOT NULL REFERENCES cr_filter_definitions(id) ON DELETE CASCADE,
    value           TEXT NOT NULL,
    label           TEXT NOT NULL,
    display_order   INT NOT NULL DEFAULT 0,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cr_search_field_definitions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key             TEXT UNIQUE NOT NULL,
    label           TEXT NOT NULL,
    placeholder     TEXT NOT NULL DEFAULT 'Search items...',
    combinator      TEXT NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    display_order   INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_cr_filter_options_filter_id ON cr_filter_options(filter_id);

-- touch_updated_at() already exists either way (api-backend's own
-- schema.sql defines it for cr_cases/cr_raw_ingestions, and so does
-- scraper-backend-v2's in the shared-DB shape) -- redefined here too via
-- CREATE OR REPLACE purely so this file has zero assumptions about which
-- `init` ran first, not because it's actually missing in the shared-DB case.
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_cr_filter_definitions_touch BEFORE UPDATE ON cr_filter_definitions
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE OR REPLACE TRIGGER trg_cr_search_field_definitions_touch BEFORE UPDATE ON cr_search_field_definitions
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
