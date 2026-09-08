-- =====================================================================
-- CASE LAW DATABASE SCHEMA — api-backend
-- Target: PostgreSQL 15+
--
-- This is api-backend's OWN copy of the schema — scraper-backend owns
-- an identical copy at scraper-backend/db/schema.sql (no shared package
-- between services, each keeps a full standalone copy, kept in sync by
-- hand). The two files are expected to be IDENTICAL through §5 (extensions
-- through the cases/citation_sequences core + its triggers) — any change to
-- those sections belongs in both copies. §6 (case_search_view) and §7
-- (court_scrape_config) below are api-backend-only additions for its own
-- convenience/standalone-completeness; scraper-backend has no equivalent
-- and doesn't need one. filter_definitions/filter_options/
-- search_field_definitions live in db/filters_supplement.sql, not inline
-- here — scraper-backend has no code path that reads or writes any of
-- the three, so api-backend owns creating them itself unconditionally
-- (see db/init_db.py's `ensure-filters` command) regardless of deployment
-- shape. case_research_search_history lives in db/supplement.sql, same
-- split, same reasoning.
--
-- REWRITE (2026-09-08, mirroring scraper-backend's same-day rewrite):
-- replaces the old documents/cases split (with parties/case_counsels/
-- document_coram/document_sections/document_subjects/document_industries/
-- document_ministry_department/citations/case_appellate_history/
-- case_timeline_events/document_holdings/document_paragraphs as separate
-- junction/editorial tables, plus `statutes`/`advocates`/`departments`) with
-- ONE flat `cases` table carrying native Postgres array columns (bench,
-- sections, acts, rules, orders, ministries, industries, case_category).
-- `case_search_view` below is NEW here (scraper-backend has no
-- equivalent — it never reads its own writes) and exists purely so
-- routers/*.py don't hand-roll the same array-to-names resolution in every
-- query; it is NOT part of the "identical through §7" contract above.
--
-- Real, permanent feature loss from this rewrite, not yet reintroduced by
-- anything: citation/treatment tracking (`citations` table is gone —
-- `treatment_status`/`cited_by`/`citations_made` no longer exist anywhere),
-- advocate/counsel data (`case_counsels`/`advocates` gone, no advocate
-- column on `cases` at all), and holdings/timeline/prior-appellate-history
-- (all LLM-envelope fields the old schema modeled that this iteration
-- doesn't store). routers/*.py were rewritten to drop these from API
-- responses entirely rather than return always-empty placeholders.
--
-- Only ONE of the two services should actually run `python -m db.init_db
-- init` against a given Postgres instance in a shared-DB deployment
-- (CREATE TYPE has no IF NOT EXISTS, so running it from both would fail on
-- the second run) — scraper-backend is the natural owner since it's the
-- write side and existed first. api-backend then runs `ensure-supplement`
-- (case_research_search_history) and `ensure-filters` (the three filter/
-- search tables) against that same database. Each service's own
-- docker-compose.yml still spins up its own separate Postgres for local dev
-- by default, where api-backend's own `init` applies everything at once.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 0. EXTENSIONS
-- ---------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- fuzzy name / party search
CREATE EXTENSION IF NOT EXISTS unaccent;     -- normalize names for search
CREATE EXTENSION IF NOT EXISTS btree_gin;    -- composite GIN indexes
CREATE EXTENSION IF NOT EXISTS pgcrypto;     -- gen_random_uuid() used by operational tables below

-- ---------------------------------------------------------------------
-- 1. ENUMS  (only for genuinely closed, stable vocabularies;
--            everything else is a lookup table so you can add values
--            without a migration)
-- ---------------------------------------------------------------------
CREATE TYPE doc_type_enum        AS ENUM ('CaseLaw', 'BusinessPolicy');
CREATE TYPE ingestion_status_enum AS ENUM (
    'QUEUED', 'DOWNLOADED', 'DOWNLOAD_FAILED',
    'OCR_DONE', 'OCR_FAILED',
    'PROMOTED', 'PROMOTION_FAILED', 'NEEDS_REVIEW'
);
CREATE TYPE disposition_category_enum AS ENUM (
    'Allowed', 'Dismissed', 'Partly Allowed', 'Disposed',
    'Remanded', 'Withdrawn', 'Quashed', 'Set Aside', 'Other'
);
CREATE TYPE data_source_enum AS ENUM ('ECOURTS', 'MANUPATRA', 'INDIAN_KANOON', 'SCI_WEBSITE', 'OTHER');

-- ---------------------------------------------------------------------
-- 2. MASTER / LOOKUP TABLES
-- ---------------------------------------------------------------------

CREATE TABLE courts (
    court_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    court_name      TEXT NOT NULL,               -- 'High Court of Delhi at New Delhi'
    court_type      TEXT NOT NULL,               -- 'Supreme Court','High Court','Tribunal','District Court'
    state           TEXT,                        -- 'Delhi','Uttarakhand'
    ecourts_code    TEXT,                        -- eCourts internal court/establishment code
    court_code      TEXT,                        -- short code for our own liznr_id scheme below,
                                                  -- e.g. 'SCIN', 'DHC'
    CONSTRAINT uq_courts_name UNIQUE (court_name),
    CONSTRAINT uq_courts_code UNIQUE (court_code)
);

CREATE TABLE judges (
    judge_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name         TEXT NOT NULL,             -- 'Anil Kshetarpal'
    normalized_name   TEXT NOT NULL,             -- upper, honorifics/punctuation stripped
    CONSTRAINT uq_judges_normalized UNIQUE (normalized_name)
);

CREATE TABLE case_categories (
    category_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category_code   TEXT NOT NULL,   -- 'W.P.(C)', 'CRL.M.A.', 'CRP', 'CS(OS)'
    category_name   TEXT NOT NULL,   -- 'Writ Petition (Civil)', 'Criminal Miscellaneous Application'
    CONSTRAINT uq_case_categories_code UNIQUE (category_code)
);

CREATE TABLE subjects (
    subject_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_name      TEXT NOT NULL,             -- 'Civil', 'Criminal' (coarse, regex-derived for now)
    CONSTRAINT uq_subjects_name UNIQUE (subject_name)
);

CREATE TABLE ministries (
    ministry_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ministry_name   TEXT NOT NULL,               -- 'Ministry of Railways', 'Cabinet Division'
    CONSTRAINT uq_ministries_name UNIQUE (ministry_name)
);

CREATE TABLE industries (
    industry_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    industry_name   TEXT NOT NULL,
    CONSTRAINT uq_industries_name UNIQUE (industry_name)
);

-- Renamed from the old `statutes` -- same concept (a named Act/Code), just
-- matching the field name `cases.acts` actually points at.
CREATE TABLE acts (
    act_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    act_name        TEXT NOT NULL,               -- 'Indian Penal Code, 1860'
    act_year        INT,
    short_code      TEXT,                        -- 'IPC', 'CrPC'
    CONSTRAINT uq_acts_name_year UNIQUE (act_name, act_year)
);

-- Sections/Rules/Orders are kept as three separate lookup tables (matching
-- cases.sections/rules/orders being three separate arrays) even though
-- they're structurally identical -- a "Section" (Section 302 IPC), a
-- "Rule" (Rule 5 of some Rules), and an "Order" (Order XXI of the CPC) are
-- different things a legal researcher filters by separately, not
-- interchangeable numbers under one bucket. All three resolve back to
-- `acts`.
CREATE TABLE sections (
    section_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    act_id          BIGINT NOT NULL REFERENCES acts(act_id),
    section_number  TEXT NOT NULL,               -- '308', '482', '2(l)', '226'
    CONSTRAINT uq_sections_act_number UNIQUE (act_id, section_number)
);

CREATE TABLE rules (
    rule_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    act_id          BIGINT REFERENCES acts(act_id),   -- nullable: standalone rules may not resolve to a named Act
    rule_number     TEXT NOT NULL,
    CONSTRAINT uq_rules_act_number UNIQUE (act_id, rule_number)
);

CREATE TABLE orders (
    order_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    act_id          BIGINT REFERENCES acts(act_id),   -- nullable, same reasoning as rules.act_id
    order_number    TEXT NOT NULL,                    -- 'XXI' (CPC's Order XXI)
    CONSTRAINT uq_orders_act_number UNIQUE (act_id, order_number)
);

-- ---------------------------------------------------------------------
-- 3. PIPELINE TABLE (scrape/OCR staging) — scraper-backend owns writing
--    to this; api-backend never reads it, kept here only so a standalone
--    api-backend database has a complete schema to apply.
-- ---------------------------------------------------------------------

CREATE TABLE scrape_batches (
    batch_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    court_id        BIGINT REFERENCES courts(court_id),
    date_from       DATE NOT NULL,
    date_to         DATE NOT NULL,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL DEFAULT 'RUNNING',
    total_found     INT DEFAULT 0,
    total_downloaded INT DEFAULT 0,
    total_promoted  INT DEFAULT 0
);

CREATE TABLE raw_ingestions (
    ingestion_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id          BIGINT REFERENCES scrape_batches(batch_id),
    court_id          BIGINT REFERENCES courts(court_id),
    data_source       data_source_enum NOT NULL DEFAULT 'ECOURTS',

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

    CONSTRAINT uq_raw_ingestions_checksum UNIQUE (file_checksum)
);

CREATE INDEX ix_raw_ingestions_status ON raw_ingestions(status);
CREATE INDEX ix_raw_ingestions_batch  ON raw_ingestions(batch_id);

-- ---------------------------------------------------------------------
-- 4. CORE TABLE
-- ---------------------------------------------------------------------

-- One row per scraped case (one PDF, one results-table row). Array columns
-- (bench/sections/acts/rules/orders/ministries/industries/case_category)
-- replace what used to be separate junction tables — Postgres can't
-- FK-constrain array contents, so referential integrity into judges/acts/
-- sections/etc. is enforced in scraper-backend's application code
-- (pipeline/promotion.py's get-or-create helpers), not by the database.
CREATE TABLE cases (
    case_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    liznr_id           TEXT,                     -- our own citation, e.g. 'LIZNR/SCIN/0001/2026'
    court_id           BIGINT NOT NULL REFERENCES courts(court_id),

    case_number        TEXT NOT NULL,
    petitioner         TEXT,
    respondent         TEXT,

    bench              BIGINT[] NOT NULL DEFAULT '{}',  -- -> judges.judge_id, full coram in bench order
    judgment_by        BIGINT REFERENCES judges(judge_id),

    judgment_date      DATE,
    language           TEXT,
    neutral_citation   TEXT,

    sections           BIGINT[] NOT NULL DEFAULT '{}',  -- -> sections.section_id
    acts               BIGINT[] NOT NULL DEFAULT '{}',  -- -> acts.act_id
    rules              BIGINT[] NOT NULL DEFAULT '{}',  -- -> rules.rule_id
    orders             BIGINT[] NOT NULL DEFAULT '{}',  -- -> orders.order_id
    subject            BIGINT REFERENCES subjects(subject_id),

    case_note          TEXT,                     -- LLM-generated headnote -- NULL until the LLM pass is wired back in
    conclusion         TEXT,                     -- regex, low coverage
    judgement          TEXT,                     -- full opinion text after the "J U D G M E N T"/"O R D E R" heading
    ocr_text           TEXT,

    source_pdf_url     TEXT,
    blob_pdf_id       TEXT,

    ministries         BIGINT[] NOT NULL DEFAULT '{}',  -- -> ministries.ministry_id
    industries         BIGINT[] NOT NULL DEFAULT '{}',  -- -> industries.industry_id -- always empty for now

    disposition        disposition_category_enum,
    document_type      doc_type_enum NOT NULL DEFAULT 'CaseLaw',
    case_category      BIGINT[] NOT NULL DEFAULT '{}',  -- -> case_categories.category_id

    needs_review       BOOLEAN NOT NULL DEFAULT FALSE,

    search_vector      tsvector,                  -- maintained by trg_cases_search_vector below

    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_cases_court_number UNIQUE (court_id, case_number)
);

ALTER TABLE raw_ingestions
    ADD CONSTRAINT fk_raw_ingestions_case
    FOREIGN KEY (case_id) REFERENCES cases(case_id);

CREATE INDEX ix_cases_court_date ON cases(court_id, judgment_date);
CREATE INDEX ix_cases_disposition ON cases(disposition);
CREATE INDEX ix_cases_search ON cases USING GIN (search_vector);
CREATE INDEX ix_cases_number_trgm ON cases USING GIN (case_number gin_trgm_ops);
CREATE INDEX ix_cases_petitioner_trgm ON cases USING GIN (petitioner gin_trgm_ops);
CREATE INDEX ix_cases_respondent_trgm ON cases USING GIN (respondent gin_trgm_ops);
CREATE UNIQUE INDEX ux_cases_liznr_id ON cases(liznr_id) WHERE liznr_id IS NOT NULL;

CREATE TABLE citation_sequences (
    court_id       BIGINT NOT NULL REFERENCES courts(court_id),
    citation_year  INT NOT NULL,
    next_seq       INT NOT NULL DEFAULT 1,
    PRIMARY KEY (court_id, citation_year)
);

-- ---------------------------------------------------------------------
-- 5. search_vector + updated_at TRIGGERS
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION cases_search_vector_update() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', coalesce(NEW.case_note,'')), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.case_number,'') || ' ' || coalesce(NEW.petitioner,'') || ' ' || coalesce(NEW.respondent,'')), 'B') ||
        setweight(to_tsvector('english', coalesce(NEW.judgement,'') || ' ' || coalesce(NEW.conclusion,'')), 'C') ||
        setweight(to_tsvector('english', coalesce(NEW.ocr_text,'')), 'D');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_cases_search_vector
BEFORE INSERT OR UPDATE ON cases
FOR EACH ROW EXECUTE FUNCTION cases_search_vector_update();

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_cases_touch BEFORE UPDATE ON cases
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TRIGGER trg_raw_ingestions_touch BEFORE UPDATE ON raw_ingestions
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

CREATE TABLE court_scrape_config (
    court_id        BIGINT PRIMARY KEY REFERENCES courts(court_id),
    adapter         TEXT NOT NULL,
    state_code      TEXT,
    bench_code      TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    last_scraped_to DATE,
    notes           TEXT,
    CONSTRAINT ck_court_scrape_config_adapter CHECK (adapter IN ('supreme_court', 'ecourts'))
);

-- Section 8 (case_research_search_history) lives in db/supplement.sql, and
-- the admin-configurable filter/search metadata (filter_definitions/
-- filter_options/search_field_definitions) lives in
-- db/filters_supplement.sql — both split out (rather than inline here) so
-- each can be applied on its own via `ensure-supplement`/`ensure-filters`
-- against a database scraper-backend already initialized (the shared-DB
-- deployment shape). `init` applies schema.sql + both supplements together
-- for a standalone database.
