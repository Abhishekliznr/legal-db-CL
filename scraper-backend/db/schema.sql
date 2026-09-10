-- =====================================================================
-- CASE LAW DATABASE SCHEMA — scraper-backend
-- Target: PostgreSQL 15+
-- Pipeline: sci.gov.in / eCourts scrape -> blob storage -> OCR -> regex
--           field extraction -> flat `cr_cases` row (this schema).
--
-- REWRITE (2026-09-08): replaces the earlier documents/cases split (with
-- parties/case_counsels/document_coram/document_sections/document_subjects/
-- document_industries/document_ministry_department/citations/
-- case_appellate_history/case_timeline_events/document_holdings/
-- document_paragraphs as separate junction/editorial tables) with ONE flat
-- `cr_cases` table carrying native Postgres array columns (bench, sections,
-- acts, rules, orders, ministries, industries, case_category) instead of
-- per-relationship junction tables. Driven by two decisions: (1) the
-- extraction approach is moving from a single LLM structured-output call
-- to regex-first extraction (pipeline/regex_extraction.py) for everything
-- that's pattern-shaped, with the LLM kept only for fields that need actual
-- reading comprehension (case_note, and other fields not modeled here yet)
-- — most of the old normalized structure existed to hold LLM-envelope
-- fields (citations/treatment, timeline, holdings, appellate history) this
-- iteration doesn't populate; (2) an explicit ask to trim the schema down
-- to only the fields actually needed right now. LLM-sourced columns
-- (case_note; industries, which has no reliable regex signal — see
-- pipeline/regex_extraction.py's module docstring) are populated by
-- pipeline/llm_enrichment.py (2026-09-08), which runs automatically right
-- after promotion — see that module's own docstring for the token-economy
-- design (compact head+tail excerpt + regex-derived hints, not the full
-- OCR text).
--
-- Every table in this file carries a cr_ prefix (2026-09-09 rename) —
-- "cr" for "case research", the umbrella this whole corpus serves.
--
-- Standalone: this is scraper-backend's OWN copy. api-backend keeps its
-- own separate copy, kept in sync by hand — no shared package between
-- services. Intended to run ONCE against an empty database — CREATE TYPE
-- has no IF NOT EXISTS in Postgres, so re-running this without dropping
-- first will fail with "type already exists". Use
-- `python -m db.init_db init --drop` for a clean local re-init.
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
CREATE TYPE favouring_party_enum AS ENUM ('Petitioner', 'Respondent', 'Partly', 'Neither');

-- ---------------------------------------------------------------------
-- 2. MASTER / LOOKUP TABLES
-- ---------------------------------------------------------------------

CREATE TABLE cr_courts (
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

CREATE TABLE cr_judges (
    judge_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name         TEXT NOT NULL,             -- 'Anil Kshetarpal'
    normalized_name   TEXT NOT NULL,             -- upper, honorifics/punctuation stripped
    CONSTRAINT uq_cr_judges_normalized UNIQUE (normalized_name)
);

CREATE TABLE cr_case_categories (
    category_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category_code   TEXT NOT NULL,   -- 'W.P.(C)', 'CRL.M.A.', 'CRP', 'CS(OS)'
    category_name   TEXT NOT NULL,   -- 'Writ Petition (Civil)', 'Criminal Miscellaneous Application'
    CONSTRAINT uq_cr_case_categories_code UNIQUE (category_code)
);

CREATE TABLE cr_subjects (
    subject_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_name      TEXT NOT NULL,             -- 'Civil', 'Criminal' (coarse, regex-derived for now)
    CONSTRAINT uq_cr_subjects_name UNIQUE (subject_name)
);

CREATE TABLE cr_ministries (
    ministry_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ministry_name   TEXT NOT NULL,               -- 'Ministry of Railways', 'Cabinet Division'
    CONSTRAINT uq_cr_ministries_name UNIQUE (ministry_name)
);

CREATE TABLE cr_industries (
    industry_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    industry_name   TEXT NOT NULL,
    CONSTRAINT uq_cr_industries_name UNIQUE (industry_name)
);

-- Renamed from the old `statutes` -- same concept (a named Act/Code), just
-- matching the field name the new `cr_cases.acts` array actually points at.
CREATE TABLE cr_acts (
    act_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    act_name        TEXT NOT NULL,               -- 'Indian Penal Code, 1860'
    act_year        INT,                         -- NULL for undated acts, e.g. 'Constitution of India'
    short_code      TEXT,                        -- 'IPC', 'CrPC'
    -- NULLS NOT DISTINCT (Postgres 15+, this schema's target -- see file
    -- header) so two act_year-less rows for the same act_name (e.g. the
    -- Constitution) actually conflict on insert instead of silently
    -- duplicating -- see db/migrations/0002_dedupe_acts_nulls_not_distinct.sql
    -- for the fix against an already-populated database.
    CONSTRAINT uq_cr_acts_name_year UNIQUE NULLS NOT DISTINCT (act_name, act_year)
);

-- Sections/Rules/Orders are kept as three separate lookup tables (matching
-- cr_cases.sections/rules/orders being three separate arrays) even though
-- they're structurally identical -- a "Section" (Section 302 IPC), a
-- "Rule" (Rule 5 of some Rules), and an "Order" (Order XXI of the CPC) are
-- different things a legal researcher filters by separately, not
-- interchangeable numbers under one bucket. All three resolve back to
-- `cr_acts` (see pipeline/regex_extraction.py's extract_provisions(), which
-- tags each match with which of the three it is).
CREATE TABLE cr_sections (
    section_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    act_id          BIGINT NOT NULL REFERENCES cr_acts(act_id),
    section_number  TEXT NOT NULL,               -- '308', '482', '2(l)', '226'
    CONSTRAINT uq_cr_sections_act_number UNIQUE (act_id, section_number)
);

CREATE TABLE cr_rules (
    rule_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    act_id          BIGINT REFERENCES cr_acts(act_id),   -- nullable: standalone rules (e.g. a High Court's own Rules of Practice) may not resolve to a named Act
    rule_number     TEXT NOT NULL,
    CONSTRAINT uq_cr_rules_act_number UNIQUE (act_id, rule_number)
);

CREATE TABLE cr_orders (
    order_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    act_id          BIGINT REFERENCES cr_acts(act_id),   -- nullable, same reasoning as rules.act_id
    order_number    TEXT NOT NULL,                    -- 'XXI' (CPC's Order XXI)
    CONSTRAINT uq_cr_orders_act_number UNIQUE (act_id, order_number)
);

-- ---------------------------------------------------------------------
-- 3. PIPELINE TABLE (scrape/OCR staging)
-- ---------------------------------------------------------------------

CREATE TABLE cr_scrape_batches (
    batch_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    court_id        BIGINT REFERENCES cr_courts(court_id),
    date_from       DATE NOT NULL,
    date_to         DATE NOT NULL,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL DEFAULT 'RUNNING',   -- RUNNING / COMPLETED / FAILED
    total_found     INT DEFAULT 0,
    total_downloaded INT DEFAULT 0,
    total_promoted  INT DEFAULT 0
);

-- One row per PDF actually pulled off the court site, BEFORE it becomes a
-- clean `cr_cases` row. Idempotency/retry/audit layer -- kept deliberately
-- minimal (no raw_ai_extraction JSONB blob, no separate EXTRACTED status)
-- since regex-derived fields are computed directly at promotion time, not
-- staged through a separate LLM-extraction step the way the old pipeline
-- was. That LLM step comes back later as an update to specific `cr_cases`
-- columns (case_note, industries), not as a cr_raw_ingestions stage again.
CREATE TABLE cr_raw_ingestions (
    ingestion_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id          BIGINT REFERENCES cr_scrape_batches(batch_id),
    court_id          BIGINT REFERENCES cr_courts(court_id),
    data_source       data_source_enum NOT NULL DEFAULT 'ECOURTS',

    source_pdf_url    TEXT NOT NULL,              -- the court's own PDF URL
    blob_pdf_id      TEXT,                       -- our own blob-hosted copy
    file_checksum     TEXT,                       -- sha256 of the PDF bytes -> dedup key
    downloaded_at     TIMESTAMPTZ,
    page_count        INT,

    ocr_text          TEXT,                       -- raw OCR dump, pre-cleaning
    ocr_engine        TEXT,                       -- 'pymupdf','tesseract'
    ocr_confidence    NUMERIC(5,2),
    ocr_completed_at  TIMESTAMPTZ,

    error_message     TEXT,                       -- the real reason for a *_FAILED/NEEDS_REVIEW status
    status            ingestion_status_enum NOT NULL DEFAULT 'QUEUED',

    case_id           BIGINT,                      -- FK added after `cr_cases` exists; set once promoted
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_cr_raw_ingestions_checksum UNIQUE (file_checksum)
);

CREATE INDEX ix_cr_raw_ingestions_status ON cr_raw_ingestions(status);
CREATE INDEX ix_cr_raw_ingestions_batch  ON cr_raw_ingestions(batch_id);

-- ---------------------------------------------------------------------
-- 4. CORE TABLE
-- ---------------------------------------------------------------------

-- One row per scraped case (one PDF, one results-table row). Flattened
-- from the old documents+cases split: every field the current pipeline
-- populates lives directly here, with array columns (bench/sections/acts/
-- rules/orders/ministries/industries/case_category) replacing what used to
-- be separate junction tables. Trade-off accepted deliberately: Postgres
-- can't FK-constrain the contents of an array column, so referential
-- integrity into judges/acts/sections/etc. is enforced in application code
-- (pipeline/promotion.py's get-or-create helpers), not by the database.
CREATE TABLE cr_cases (
    case_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    liznr_id           TEXT,                     -- our own citation, e.g. 'LIZNR/SCIN/0001/2026' -- generated
                                                  -- at promotion time (citation_sequences below); doubles as
                                                  -- the safe public identifier (case_id is never exposed externally)
    court_id           BIGINT NOT NULL REFERENCES cr_courts(court_id),

    case_number        TEXT NOT NULL,
    petitioner         TEXT,
    respondent         TEXT,

    bench              BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_judges.judge_id, full coram in bench order
    judgment_by        BIGINT REFERENCES cr_judges(judge_id),  -- single judge_id -- who authored/signed

    judgment_date      DATE,
    language           TEXT,
    neutral_citation   TEXT,

    sections           BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_sections.section_id
    acts               BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_acts.act_id (every act referenced by any section/rule/order below)
    rules              BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_rules.rule_id
    orders             BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_orders.order_id
    subject            BIGINT REFERENCES cr_subjects(subject_id),  -- coarse Civil/Criminal/... tag

    -- LLM-classified subsets of sections/rules/orders above (pipeline/llm_enrichment.py,
    -- 2026-09-09), populated from paragraphs regex flagged as provision-bearing
    -- (pipeline/regex_extraction.find_provision_paragraphs). "relevant" = the
    -- operative provision(s) the case is actually charged/founded/appealed under;
    -- "other" = everything else discussed (precedent, background, comparative
    -- statutes). NOT guaranteed a strict partition of sections/rules/orders above —
    -- the LLM resolves acts from wider context the regex-only extractor drops, so
    -- these can contain provisions the unified columns above miss, and vice versa.
    sections_relevant  BIGINT[] NOT NULL DEFAULT '{}',
    sections_other     BIGINT[] NOT NULL DEFAULT '{}',
    rules_relevant     BIGINT[] NOT NULL DEFAULT '{}',
    rules_other        BIGINT[] NOT NULL DEFAULT '{}',
    orders_relevant    BIGINT[] NOT NULL DEFAULT '{}',
    orders_other       BIGINT[] NOT NULL DEFAULT '{}',

    case_note          TEXT,                     -- LLM-generated headnote (pipeline/llm_enrichment.py), Manupatra-style dash-separated digest
    conclusion         TEXT,                     -- regex, low coverage (~1-3% of judgments have a literal heading) -- see pipeline/regex_extraction.py
    judgement          TEXT,                     -- full opinion text after the "J U D G M E N T"/"O R D E R" heading
    ocr_text           TEXT,                     -- final OCR text, source of truth for judgement/conclusion/provisions above

    source_pdf_url     TEXT,
    blob_pdf_id       TEXT,

    ministries         BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_ministries.ministry_id, matched against petitioner/respondent only (see regex_extraction.find_ministry_in_party_name)
    industries         BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_industries.industry_id -- LLM-classified (pipeline/llm_enrichment.py); no reliable regex signal exists for this field (see pipeline/regex_extraction.py's module docstring)

    disposition        disposition_category_enum,
    favouring_party    favouring_party_enum,       -- LLM-classified (pipeline/llm_enrichment.py) -- which side the outcome favoured
    document_type      doc_type_enum NOT NULL DEFAULT 'CaseLaw',  -- constant for this adapter -- every sci.gov.in row is a court judgment/order, not extracted per-row
    case_category      BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_case_categories.category_id

    needs_review       BOOLEAN NOT NULL DEFAULT FALSE,  -- set when judgment_date couldn't be parsed or another required signal was missing

    -- Enrichment status tracking (pipeline/llm_enrichment.py, 2026-09-10
    -- rewrite; see db/migrations/0001_add_case_enrichment_status.sql for
    -- the ALTER-based version of this against an already-populated DB) --
    -- makes an LLM enrichment failure queryable/retryable instead of
    -- indistinguishable from "case genuinely has no provisions".
    enrichment_status    TEXT NOT NULL DEFAULT 'PENDING'
                         CHECK (enrichment_status IN ('PENDING', 'DONE', 'FAILED', 'TRUNCATED', 'SKIPPED')),
    enrichment_error     TEXT,
    enriched_at          TIMESTAMPTZ,
    enrichment_attempts  INTEGER NOT NULL DEFAULT 0,

    search_vector      tsvector,                  -- maintained by trg_cr_cases_search_vector below -- api-backend's
                                                    -- free-text search has nothing to query without this; the flattened
                                                    -- schema's first draft (2026-09-08) omitted it entirely, a real gap
                                                    -- caught only once api-backend's own rewrite needed it to exist

    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_cr_cases_court_number UNIQUE (court_id, case_number)
);

ALTER TABLE cr_raw_ingestions
    ADD CONSTRAINT fk_cr_raw_ingestions_case
    FOREIGN KEY (case_id) REFERENCES cr_cases(case_id);

CREATE INDEX ix_cr_cases_court_date ON cr_cases(court_id, judgment_date);
CREATE INDEX ix_cr_cases_disposition ON cr_cases(disposition);
CREATE INDEX ix_cr_cases_search ON cr_cases USING GIN (search_vector);
CREATE INDEX ix_cr_cases_number_trgm ON cr_cases USING GIN (case_number gin_trgm_ops);
CREATE INDEX ix_cr_cases_petitioner_trgm ON cr_cases USING GIN (petitioner gin_trgm_ops);
CREATE INDEX ix_cr_cases_respondent_trgm ON cr_cases USING GIN (respondent gin_trgm_ops);
CREATE UNIQUE INDEX ux_cr_cases_liznr_id ON cr_cases(liznr_id) WHERE liznr_id IS NOT NULL;
CREATE INDEX ix_cr_cases_enrichment_pending ON cr_cases (case_id) WHERE enrichment_status IN ('PENDING', 'FAILED', 'TRUNCATED');

-- Atomic per-(court, year) counter backing cr_cases.liznr_id
-- ('LIZNR/<court_code>/<seq>/<year>'). Resets every year, same convention
-- as Manupatra's own MANU/XX/NNNN/YYYY scheme and the official INSC neutral
-- citation. Claimed via INSERT ... ON CONFLICT DO UPDATE ... RETURNING
-- next_seq, which Postgres serializes correctly under concurrent
-- promotions via the row lock the UPDATE takes.
CREATE TABLE cr_citation_sequences (
    court_id       BIGINT NOT NULL REFERENCES cr_courts(court_id),
    citation_year  INT NOT NULL,
    next_seq       INT NOT NULL DEFAULT 1,
    PRIMARY KEY (court_id, citation_year)
);

-- ---------------------------------------------------------------------
-- 5. search_vector + updated_at TRIGGERS
-- ---------------------------------------------------------------------

-- Weighted like the old documents_search_vector_update() this replaces:
-- case_note (the eventual LLM headnote) ranks highest, then the identifying
-- text (case_number/petitioner/respondent) a legal researcher actually
-- types, then judgement/conclusion, ocr_text as the lowest-weighted catch-
-- all. Runs on every INSERT/UPDATE, so a later LLM pass writing case_note
-- back onto an already-promoted row keeps this in sync automatically.
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

CREATE TRIGGER trg_cr_cases_search_vector
BEFORE INSERT OR UPDATE ON cr_cases
FOR EACH ROW EXECUTE FUNCTION cr_cases_search_vector_update();

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_cr_cases_touch BEFORE UPDATE ON cr_cases
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TRIGGER trg_cr_raw_ingestions_touch BEFORE UPDATE ON cr_raw_ingestions
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- =====================================================================
-- 6. OPERATIONAL SUPPLEMENT (not part of the domain schema above) —
--    court scrape config, this service's own concern.
--
--    filter_definitions/filter_options/search_field_definitions (admin-
--    configurable filter/search metadata for api-backend's
--    /api/cases/filters) were removed here (2026-09-08) — dead weight in
--    THIS service: zero code in scraper-backend ever reads or writes
--    them, and api-backend already owns its own separate copy
--    (api-backend/db/schema.sql), actually used by its
--    filter_router.py/search_router.py/seed_filters.py. Duplicating them
--    here added nothing scraper-backend itself needs.
-- =====================================================================

-- One row per court that gets scraped. Turns "25 Python files" into
-- "25 rows".
CREATE TABLE cr_court_scrape_config (
    court_id        BIGINT PRIMARY KEY REFERENCES cr_courts(court_id),
    adapter         TEXT NOT NULL,              -- 'supreme_court' | 'ecourts'
    state_code      TEXT,                       -- eCourts state_code select value (e.g. '7~26' for Delhi)
    bench_code      TEXT,                       -- eCourts dist_code select value
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    last_scraped_to DATE,                       -- watermark: resume from here on next run
    notes           TEXT,
    CONSTRAINT ck_cr_court_scrape_config_adapter CHECK (adapter IN ('supreme_court', 'ecourts'))
);
