-- =====================================================================
-- CASE LAW DATABASE SCHEMA — api-backend
-- Target: PostgreSQL 15+
--
-- This is api-backend's OWN copy of the schema — scraper-backend owns
-- an identical copy at scraper-backend/db/schema.sql (no shared package
-- between services, each keeps a full standalone copy, kept in sync by
-- hand). The two files are expected to be IDENTICAL through §5 (extensions
-- through the cr_cases/cr_citation_sequences core + its triggers) — any
-- change to those sections belongs in both copies. §6 (cr_case_search_view)
-- and §7 (cr_court_scrape_config) below are api-backend-only additions for
-- its own convenience/standalone-completeness; scraper-backend has no
-- equivalent and doesn't need one. cr_filter_definitions/cr_filter_options/
-- cr_search_field_definitions live in db/filters_supplement.sql, not inline
-- here — scraper-backend has no code path that reads or writes any of
-- the three, so api-backend owns creating them itself unconditionally
-- (see db/init_db.py's `ensure-filters` command) regardless of deployment
-- shape. cr_search_history lives in db/supplement.sql, same
-- split, same reasoning.
--
-- Every table in this file carries a cr_ prefix (2026-09-09 rename) —
-- "cr" for "case research", the umbrella this whole corpus serves.
--
-- REWRITE (2026-09-08, mirroring scraper-backend's same-day rewrite):
-- replaces the old documents/cases split (with parties/case_counsels/
-- document_coram/document_sections/document_subjects/document_industries/
-- document_ministry_department/citations/case_appellate_history/
-- case_timeline_events/document_holdings/document_paragraphs as separate
-- junction/editorial tables, plus `statutes`/`advocates`/`departments`) with
-- ONE flat `cr_cases` table carrying native Postgres array columns (bench,
-- sections, acts, rules, orders, ministries, industries, case_category).
-- `cr_case_search_view` below is NEW here (scraper-backend has no
-- equivalent — it never reads its own writes) and exists purely so
-- routers/*.py don't hand-roll the same array-to-names resolution in every
-- query; it is NOT part of the "identical through §7" contract above.
--
-- Real, permanent feature loss from this rewrite, not yet reintroduced by
-- anything: citation/treatment tracking (`citations` table is gone —
-- `treatment_status`/`cited_by`/`citations_made` no longer exist anywhere),
-- and holdings/timeline/prior-appellate-history (all LLM-envelope fields
-- the old schema modeled that this iteration doesn't store). routers/*.py
-- were rewritten to drop these from API responses entirely rather than
-- return always-empty placeholders.
--
-- Advocate/counsel data (`case_counsels`/`advocates` gone) was reintroduced
-- 2026-09-13 as flat `petitioner_advocate`/`respondent_advocate` columns on
-- `cr_cases` (db/migrations/0007_add_advocates_filing_year.sql), matching
-- this schema's flat-columns-over-junction-tables shape rather than
-- restoring the old separate tables.
--
-- Only ONE of the two services should actually run `python -m db.init_db
-- init` against a given Postgres instance in a shared-DB deployment —
-- the CREATE TABLE statements below still aren't idempotent even though
-- the enum types now are (see §1) — scraper-backend is the natural owner
-- since it's the write side and existed first. api-backend then runs `ensure-supplement`
-- (cr_search_history) and `ensure-filters` (the three filter/
-- search tables) against that same database. Each service's own
-- docker-compose.yml still spins up its own separate Postgres for local dev
-- by default, where api-backend's own `init` applies everything at once.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 0. EXTENSIONS
-- ---------------------------------------------------------------------
-- unaccent/btree_gin/pgcrypto were dropped here (2026-09-11): none of them
-- is actually referenced anywhere in this schema or the routers/ code (no
-- unaccent() call, no composite btree+GIN index). gen_random_uuid() is
-- called (filters_supplement.sql, supplement.sql) but Postgres 13+ ships it
-- in core, so pgcrypto isn't needed for it either. pg_trgm is the only one
-- genuinely load-bearing: the three gin_trgm_ops indexes below need it.
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- fuzzy name / party search

-- ---------------------------------------------------------------------
-- 1. ENUMS  (only for genuinely closed, stable vocabularies;
--            everything else is a lookup table so you can add values
--            without a migration)
-- ---------------------------------------------------------------------
-- Postgres has no CREATE TYPE IF NOT EXISTS, so each is wrapped in a
-- DO block that swallows only duplicate_object — this is what actually
-- makes schema.sql safe to re-run against a database that already has the
-- types but not the tables (e.g. cr_cases got dropped/recreated separately
-- from these types at some point) instead of aborting the whole script.
DO $$ BEGIN
    CREATE TYPE doc_type_enum AS ENUM ('CaseLaw', 'BusinessPolicy');
EXCEPTION WHEN duplicate_object THEN null;
END $$;
DO $$ BEGIN
    CREATE TYPE ingestion_status_enum AS ENUM (
        'QUEUED', 'DOWNLOADED', 'DOWNLOAD_FAILED',
        'OCR_DONE', 'OCR_FAILED',
        'PROMOTED', 'PROMOTION_FAILED', 'NEEDS_REVIEW'
    );
EXCEPTION WHEN duplicate_object THEN null;
END $$;
DO $$ BEGIN
    CREATE TYPE disposition_category_enum AS ENUM (
        'Allowed', 'Dismissed', 'Partly Allowed', 'Disposed',
        'Remanded', 'Withdrawn', 'Quashed', 'Set Aside', 'Other'
    );
EXCEPTION WHEN duplicate_object THEN null;
END $$;
DO $$ BEGIN
    CREATE TYPE data_source_enum AS ENUM ('ECOURTS', 'MANUPATRA', 'INDIAN_KANOON', 'SCI_WEBSITE', 'MPHC_WEBSITE', 'OTHER');
EXCEPTION WHEN duplicate_object THEN null;
END $$;
DO $$ BEGIN
    CREATE TYPE favouring_party_enum AS ENUM ('Petitioner', 'Respondent', 'Partly', 'Neither');
EXCEPTION WHEN duplicate_object THEN null;
END $$;

-- ---------------------------------------------------------------------
-- 2. MASTER / LOOKUP TABLES
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cr_courts (
    court_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    court_name      TEXT NOT NULL,               -- 'High Court of Delhi at New Delhi'
    court_type      TEXT NOT NULL,               -- 'Supreme Court','High Court','Tribunal','District Court'
    state           TEXT,                        -- 'Delhi','Uttarakhand'
    ecourts_code    TEXT,                        -- eCourts internal court/establishment code
    court_code      TEXT,                        -- short code for our own liznr_id scheme below,
                                                  -- e.g. 'SCIN', 'DHC'
    CONSTRAINT uq_cr_courts_name UNIQUE (court_name),
    CONSTRAINT uq_cr_courts_code UNIQUE (court_code)
);

CREATE TABLE IF NOT EXISTS cr_judges (
    judge_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name         TEXT NOT NULL,             -- 'Anil Kshetarpal'
    normalized_name   TEXT NOT NULL,             -- upper, honorifics/punctuation stripped
    CONSTRAINT uq_cr_judges_normalized UNIQUE (normalized_name)
);

-- See scraper-backend/db/schema.sql's copy for why (kept in sync by hand).
CREATE TABLE IF NOT EXISTS cr_advocates (
    advocate_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    advocate_name   TEXT NOT NULL,
    enrollment_no   TEXT NOT NULL,
    enrollment_year INTEGER,
    CONSTRAINT uq_cr_advocates_enrollment_no UNIQUE (enrollment_no)
);

CREATE TABLE IF NOT EXISTS cr_case_categories (
    category_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category_code   TEXT NOT NULL,   -- 'W.P.(C)', 'CRL.M.A.', 'CRP', 'CS(OS)'
    category_name   TEXT NOT NULL,   -- 'Writ Petition (Civil)', 'Criminal Miscellaneous Application'
    CONSTRAINT uq_cr_case_categories_code UNIQUE (category_code)
);

CREATE TABLE IF NOT EXISTS cr_subjects (
    subject_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_name      TEXT NOT NULL,             -- 'Civil', 'Criminal' (coarse, regex-derived for now)
    CONSTRAINT uq_cr_subjects_name UNIQUE (subject_name)
);

CREATE TABLE IF NOT EXISTS cr_ministries (
    ministry_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ministry_name   TEXT NOT NULL,               -- 'Ministry of Railways', 'Cabinet Division'
    CONSTRAINT uq_cr_ministries_name UNIQUE (ministry_name)
);

CREATE TABLE IF NOT EXISTS cr_industries (
    industry_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    industry_name   TEXT NOT NULL,
    CONSTRAINT uq_cr_industries_name UNIQUE (industry_name)
);

-- Renamed from the old `statutes` -- same concept (a named Act/Code), just
-- matching the field name `cr_cases.acts` actually points at.
CREATE TABLE IF NOT EXISTS cr_acts (
    act_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    act_name        TEXT NOT NULL,               -- 'Indian Penal Code, 1860'
    act_year        INT,
    short_code      TEXT,                        -- 'IPC', 'CrPC'
    -- NULLS NOT DISTINCT (Postgres 15+, this schema's target) so two
    -- act_year-less rows for the same act_name (e.g. the Constitution)
    -- actually conflict on insert instead of silently duplicating -- see
    -- scraper-backend/db/migrations/0002_dedupe_acts_nulls_not_distinct.sql
    -- for the fix against an already-populated database.
    CONSTRAINT uq_cr_acts_name_year UNIQUE NULLS NOT DISTINCT (act_name, act_year)
);

-- Rules/Orders (separate lookup tables + cr_cases.rules/orders arrays,
-- alongside a rules_relevant/_other + orders_relevant/_other LLM-classified
-- split) were dropped 2026-09-19 (scraper-backend/db/migrations/0012) --
-- only Section-level provisions are tracked now, not Rules/Orders as a
-- distinct kind.
CREATE TABLE IF NOT EXISTS cr_sections (
    section_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    act_id          BIGINT NOT NULL REFERENCES cr_acts(act_id),
    section_number  TEXT NOT NULL,               -- '308', '482', '2(l)', '226'
    CONSTRAINT uq_cr_sections_act_number UNIQUE (act_id, section_number)
);

-- ---------------------------------------------------------------------
-- 3. PIPELINE TABLE (scrape/OCR staging) — scraper-backend owns writing
--    to this; api-backend never reads it, kept here only so a standalone
--    api-backend database has a complete schema to apply.
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cr_scrape_batches (
    batch_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    court_id        BIGINT REFERENCES cr_courts(court_id),
    date_from       DATE NOT NULL,
    date_to         DATE NOT NULL,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL DEFAULT 'QUEUED',    -- QUEUED / RUNNING / COMPLETED / FAILED / CANCELLED / SOURCE_*
    total_found     INT DEFAULT 0,
    total_downloaded INT DEFAULT 0,
    total_promoted  INT DEFAULT 0,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    finished_at     TIMESTAMPTZ,
    -- See scraper-backend/db/schema.sql's copy for why (kept in sync by hand).
    error_message   TEXT,
    -- Set once a resumable adapter's discovered case list is saved to cr_batch_items;
    -- a resume skips discovery when set (db/migrations/0013).
    discovered_at   TIMESTAMPTZ,
    run_count       INT NOT NULL DEFAULT 1,       -- 1 for the first run, +1 per resume
    -- On-demand worker bookkeeping (db/migrations/0015): the running pod bumps heartbeat_at
    -- every 30s; queued_at/claimed_at/job_name describe the latest run's K8s Job.
    heartbeat_at    TIMESTAMPTZ,
    job_name        TEXT,
    queued_at       TIMESTAMPTZ,
    claimed_at      TIMESTAMPTZ,
    -- The admin who started the batch, snapshotted (db/migrations batch_actors).
    requested_by_id    TEXT,
    requested_by_name  TEXT,
    requested_by_email TEXT
);

CREATE TABLE IF NOT EXISTS cr_raw_ingestions (
    ingestion_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id          BIGINT REFERENCES cr_scrape_batches(batch_id),
    court_id          BIGINT REFERENCES cr_courts(court_id),
    -- No longer defaults to 'ECOURTS' -- see scraper-backend/db/schema.sql's
    -- copy of this table for why (that adapter is retired; kept in sync by
    -- hand across both copies per this repo's standing convention).
    data_source       data_source_enum NOT NULL DEFAULT 'OTHER',

    source_pdf_url    TEXT NOT NULL,
    blob_pdf_id      TEXT,
    file_checksum     TEXT,
    downloaded_at     TIMESTAMPTZ,
    page_count        INT,

    ocr_text          TEXT,
    ocr_engine        TEXT,
    ocr_confidence    NUMERIC(5,2),
    ocr_completed_at  TIMESTAMPTZ,

    error_message     TEXT,
    status            ingestion_status_enum NOT NULL DEFAULT 'QUEUED',

    case_id           BIGINT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_cr_raw_ingestions_checksum UNIQUE (file_checksum)
);

CREATE INDEX IF NOT EXISTS ix_cr_raw_ingestions_status ON cr_raw_ingestions(status);
CREATE INDEX IF NOT EXISTS ix_cr_raw_ingestions_batch  ON cr_raw_ingestions(batch_id);

-- The case list a resumable adapter discovered for a batch, with per-case
-- progress, so a stopped batch resumes from where it stopped. A case is only
-- marked DONE after promotion commits (db/migrations/0013).
CREATE TABLE IF NOT EXISTS cr_batch_items (
    item_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id      BIGINT NOT NULL REFERENCES cr_scrape_batches(batch_id) ON DELETE CASCADE,
    position      INT NOT NULL,
    item_key      TEXT NOT NULL,
    payload       JSONB NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING / DONE / SKIPPED / FAILED
    reason        TEXT,
    ingestion_id  BIGINT REFERENCES cr_raw_ingestions(ingestion_id),
    attempts      INT NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_cr_batch_items_key UNIQUE (batch_id, item_key)
);

CREATE INDEX IF NOT EXISTS ix_cr_batch_items_batch_status ON cr_batch_items(batch_id, status);

-- History of what happened to a batch across its runs (started, discovered,
-- stop requested, resumed, interrupted, and one finish event per run named
-- after its status), for the admin timeline (db/migrations/0014).
CREATE TABLE IF NOT EXISTS cr_batch_events (
    event_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id     BIGINT NOT NULL REFERENCES cr_scrape_batches(batch_id) ON DELETE CASCADE,
    run_number   INT NOT NULL,
    event_type   TEXT NOT NULL,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    message      TEXT,
    details      JSONB NOT NULL DEFAULT '{}',
    -- Admin behind STARTED / STOP_REQUESTED / RESUMED; NULL for worker/sweep events.
    actor_id     TEXT,
    actor_name   TEXT,
    actor_email  TEXT
);

CREATE INDEX IF NOT EXISTS ix_cr_batch_events_batch ON cr_batch_events(batch_id, occurred_at);

CREATE INDEX IF NOT EXISTS ix_cr_scrape_batches_active
    ON cr_scrape_batches(court_id) WHERE status IN ('QUEUED', 'RUNNING');
CREATE INDEX IF NOT EXISTS ix_cr_scrape_batches_court_requested ON cr_scrape_batches(court_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS ix_cr_scrape_batches_requested_by ON cr_scrape_batches(requested_by_id) WHERE requested_by_id IS NOT NULL;

-- Live log lines written by the worker pod, tailed by api-backend's SSE endpoint (db/migrations/0015).
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

-- ---------------------------------------------------------------------
-- 4. CORE TABLE
-- ---------------------------------------------------------------------

-- One row per scraped case (one PDF, one results-table row). Array columns
-- (bench/sections/acts/rules/orders/ministries/industries/case_category)
-- replace what used to be separate junction tables — Postgres can't
-- FK-constrain array contents, so referential integrity into judges/acts/
-- sections/etc. is enforced in scraper-backend's application code
-- (adapters/supreme_court/promotion.py's get-or-create helpers), not by the database.
CREATE TABLE IF NOT EXISTS cr_cases (
    case_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    liznr_id           TEXT,                     -- our own citation, e.g. 'LIZNR/SCIN/0001/2026'
    court_id           BIGINT NOT NULL REFERENCES cr_courts(court_id),

    case_number        TEXT NOT NULL,
    cnr                TEXT,                     -- pan-India eCourts case number record, when the source
                                                  -- page exposes one (e.g. Madhya Pradesh's case-status page)
    petitioner         TEXT,
    respondent         TEXT,
    petitioner_advocate TEXT,  -- regex-parsed from the SCI results table's "Petitioner/Respondent
                               -- Advocate" cell (adapters.supreme_court.extraction.parse_advocates) --
                               -- reintroduces the advocate data the 2026-09-08 flattening dropped.
                               -- Supreme-Court-only: a bare name, no enrollment number -- see
                               -- cr_advocates/petitioner_advocate_ids below for a source that has one
    respondent_advocate TEXT,  -- frequently NULL even when petitioner_advocate isn't -- see
                               -- parse_advocates' own docstring on why the respondent side is so
                               -- often simply missing from the source cell, not a parsing failure

    -- See scraper-backend/db/schema.sql's copy for why (kept in sync by hand).
    petitioner_advocate_ids BIGINT[] NOT NULL DEFAULT '{}',
    respondent_advocate_ids BIGINT[] NOT NULL DEFAULT '{}',

    -- Filing year only (not a full filing DATE -- sci.gov.in's judgments-by-date
    -- search table has no such column; the real filing/registration date lives
    -- behind a separate per-case Case Status lookup this adapter doesn't call).
    -- Parsed straight out of case_number (e.g. "... of 2021") via
    -- normalization.case_numbers.extract_filing_year -- good enough for a coarse
    -- case-age-in-years figure without a second scrape per case.
    filing_year        INTEGER,

    bench              BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_judges.judge_id, full coram in bench order
    judgment_by        BIGINT REFERENCES cr_judges(judge_id),

    judgment_date      DATE,
    language           TEXT,
    neutral_citation   TEXT,

    sections           BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_sections.section_id
    acts               BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_acts.act_id
    subject            BIGINT REFERENCES cr_subjects(subject_id),

    case_note          TEXT,                     -- LLM-generated headnote (llm_enrichment.py), Manupatra-style dash-separated digest
    conclusion         TEXT,                     -- regex, low coverage (~1-3% of judgments have a literal heading), LLM fallback if regex found nothing
    judgement          TEXT,                     -- full opinion text after the "J U D G M E N T"/"O R D E R" heading
    ocr_text           TEXT,

    source_pdf_url     TEXT,
    blob_pdf_id       TEXT,

    ministries         BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_ministries.ministry_id, matched against petitioner/respondent only
    industries         BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_industries.industry_id -- LLM-classified (llm_enrichment.py); no reliable regex signal exists for this field

    disposition        disposition_category_enum,
    favouring_party    favouring_party_enum,       -- LLM-classified (llm_enrichment.py) -- which side the outcome favoured
    document_type      doc_type_enum NOT NULL DEFAULT 'CaseLaw',
    case_category      BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_case_categories.category_id

    needs_review       BOOLEAN NOT NULL DEFAULT FALSE,

    -- Enrichment status tracking (llm_enrichment.py, 2026-09-10 rewrite) --
    -- makes an LLM enrichment failure queryable/retryable instead of
    -- indistinguishable from "case genuinely has no provisions". Internal
    -- pipeline-ops data, not exposed by any router response below.
    enrichment_status    TEXT NOT NULL DEFAULT 'PENDING'
                         CHECK (enrichment_status IN ('PENDING', 'DONE', 'FAILED', 'TRUNCATED', 'SKIPPED')),
    enrichment_error     TEXT,
    enriched_at          TIMESTAMPTZ,
    enrichment_attempts  INTEGER NOT NULL DEFAULT 0,

    search_vector      tsvector,                  -- maintained by trg_cr_cases_search_vector below

    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_cr_cases_court_number UNIQUE (court_id, case_number)
);

DO $$ BEGIN
    ALTER TABLE cr_raw_ingestions
        ADD CONSTRAINT fk_cr_raw_ingestions_case
        FOREIGN KEY (case_id) REFERENCES cr_cases(case_id);
EXCEPTION WHEN duplicate_object THEN null;
END $$;

CREATE INDEX IF NOT EXISTS ix_cr_cases_court_date ON cr_cases(court_id, judgment_date);
CREATE INDEX IF NOT EXISTS ix_cr_cases_created_at ON cr_cases(created_at);
CREATE INDEX IF NOT EXISTS ix_cr_cases_disposition ON cr_cases(disposition);
CREATE INDEX IF NOT EXISTS ix_cr_cases_search ON cr_cases USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS ix_cr_cases_number_trgm ON cr_cases USING GIN (case_number gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_cr_cases_petitioner_trgm ON cr_cases USING GIN (petitioner gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_cr_cases_respondent_trgm ON cr_cases USING GIN (respondent gin_trgm_ops);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cr_cases_liznr_id ON cr_cases(liznr_id) WHERE liznr_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_cr_cases_cnr ON cr_cases(cnr) WHERE cnr IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_cr_cases_enrichment_pending ON cr_cases (case_id) WHERE enrichment_status IN ('PENDING', 'FAILED', 'TRUNCATED');

CREATE TABLE IF NOT EXISTS cr_citation_sequences (
    court_id       BIGINT NOT NULL REFERENCES cr_courts(court_id),
    citation_year  INT NOT NULL,
    next_seq       INT NOT NULL DEFAULT 1,
    PRIMARY KEY (court_id, citation_year)
);

-- ---------------------------------------------------------------------
-- 5. search_vector + updated_at TRIGGERS
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION cr_cases_search_vector_update() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', coalesce(NEW.case_note,'')), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.case_number,'') || ' ' || coalesce(NEW.petitioner,'') || ' ' || coalesce(NEW.respondent,'')), 'B') ||
        setweight(to_tsvector('english', coalesce(NEW.judgement,'') || ' ' || coalesce(NEW.conclusion,'')), 'C') ||
        setweight(to_tsvector('english', coalesce(NEW.ocr_text,'')), 'D');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_cr_cases_search_vector
BEFORE INSERT OR UPDATE ON cr_cases
FOR EACH ROW EXECUTE FUNCTION cr_cases_search_vector_update();

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_cr_cases_touch BEFORE UPDATE ON cr_cases
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE OR REPLACE TRIGGER trg_cr_raw_ingestions_touch BEFORE UPDATE ON cr_raw_ingestions
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ---------------------------------------------------------------------
-- 6. CONVENIENCE VIEW for the API layer — api-backend-only (scraper-
--    backend-v2 has no equivalent; it never reads its own writes). Lives
--    in db/view_supplement.sql, not inline here, for the same reason
--    filter_definitions/etc. moved to filters_supplement.sql: so it can be
--    applied on its own (`python -m db.init_db ensure-view`) against a
--    database scraper-backend already initialized, which never creates
--    it. `init` below applies it too, for the standalone path.
-- ---------------------------------------------------------------------

-- =====================================================================
-- 7. OPERATIONAL SUPPLEMENT (api-backend-only) — court scrape config,
--    kept here only so a standalone database has the full schema (this
--    service never writes to it).
-- =====================================================================

-- `adapter` names a key in scraper-backend's routers/scraper_router.py's
-- `_ADAPTER_REGISTRY` (each court has its own adapter — no shared generic
-- eCourts adapter) rather than a fixed SQL enum, since that registry grows
-- one court at a time, in code. `config` is that adapter's own free-form
-- settings (JSONB) — no adapter-specific columns here.
CREATE TABLE IF NOT EXISTS cr_court_scrape_config (
    court_id        BIGINT PRIMARY KEY REFERENCES cr_courts(court_id),
    adapter         TEXT NOT NULL,
    config          JSONB NOT NULL DEFAULT '{}',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    last_scraped_to DATE,
    notes           TEXT
);

-- Section 8 (cr_search_history) lives in db/supplement.sql, and
-- the admin-configurable filter/search metadata (cr_filter_definitions/
-- cr_filter_options/cr_search_field_definitions) lives in
-- db/filters_supplement.sql — both split out (rather than inline here) so
-- each can be applied on its own via `ensure-supplement`/`ensure-filters`
-- against a database scraper-backend already initialized (the shared-DB
-- deployment shape). `init` applies schema.sql + both supplements together
-- for a standalone database.
