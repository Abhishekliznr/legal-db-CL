-- =====================================================================
-- CASE LAW DATABASE SCHEMA — scraper-backend-v2
-- Target: PostgreSQL 15+
-- Pipeline: eCourts / sci.gov.in scrape -> blob storage -> OCR -> AI field
--           extraction -> structured storage (this schema)
--
-- This file is the domain schema from docs/scraper-backend-revamp-spec.md's
-- attached caselaw_schema.sql, unmodified, plus the operational tables that
-- schema doesn't cover (§3.1 of the spec) appended at the end: court scrape
-- config, scraper job tracking is already covered by scrape_batches/
-- raw_ingestions below, and the admin-configurable filter/search metadata
-- tables api-backend serves from (§6).
--
-- Standalone: this is scraper-backend-v2's OWN copy. api-backend keeps its
-- own separate copy, kept in sync by hand — no shared package between
-- services (spec §3.2). Intended to run ONCE against an empty database —
-- CREATE TYPE has no IF NOT EXISTS in Postgres, so re-running this without
-- dropping first will fail with "type already exists". Use
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
CREATE TYPE decision_type_enum   AS ENUM ('Judgment', 'Order', 'Notification', 'Circular');
CREATE TYPE party_side_enum      AS ENUM ('PETITIONER_SIDE', 'RESPONDENT_SIDE');
CREATE TYPE ingestion_status_enum AS ENUM (
    'QUEUED', 'DOWNLOADED', 'DOWNLOAD_FAILED',
    'OCR_DONE', 'OCR_FAILED',
    'EXTRACTED', 'EXTRACTION_FAILED',
    'PROMOTED', 'NEEDS_REVIEW'
);
CREATE TYPE citation_treatment_enum AS ENUM (
    'Overruled', 'Affirmed', 'Distinguished', 'Followed',
    'Referred', 'Relied Upon', 'Explained', 'Doubted',
    'Discussed', 'Mentioned'
);
CREATE TYPE disposition_category_enum AS ENUM (
    'Allowed', 'Dismissed', 'Partly Allowed', 'Disposed',
    'Remanded', 'Withdrawn', 'Quashed', 'Set Aside', 'Other'
);
CREATE TYPE data_source_enum AS ENUM ('ECOURTS', 'MANUPATRA', 'INDIAN_KANOON', 'SCI_WEBSITE', 'OTHER');
-- Outcome of THIS document on appeal/revision over the specific lower-forum
-- order it reviews (case_appellate_history below) -- distinct from a
-- citation's treatment, which is about persuasive precedent, not the order
-- actually being appealed.
CREATE TYPE appellate_outcome_enum AS ENUM (
    'Affirmed', 'Reversed', 'Partly Reversed', 'Set Aside', 'Remanded', 'Modified'
);

-- ---------------------------------------------------------------------
-- 2. MASTER / LOOKUP TABLES
-- ---------------------------------------------------------------------

CREATE TABLE courts (
    court_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    court_name      TEXT NOT NULL,               -- 'High Court of Delhi at New Delhi'
    court_type      TEXT NOT NULL,               -- 'Supreme Court','High Court','Tribunal','District Court'
    state           TEXT,                        -- 'Delhi','Uttarakhand'
    ecourts_code    TEXT,                        -- eCourts internal court/establishment code
    court_code      TEXT,                        -- short code for our own internal_citation scheme
                                                  -- below, e.g. 'SCIN', 'DHC' -- same convention already
                                                  -- used ad hoc as a scraper request param (court_code
                                                  -- on /api/scraper/start), now persisted properly
    CONSTRAINT uq_courts_name UNIQUE (court_name),
    CONSTRAINT uq_courts_code UNIQUE (court_code)
);

CREATE TABLE judges (
    judge_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name         TEXT NOT NULL,             -- 'Anil Kshetarpal'
    normalized_name   TEXT NOT NULL,             -- lower, unaccented, punctuation-stripped
    CONSTRAINT uq_judges_normalized UNIQUE (normalized_name)
);

CREATE TABLE advocates (
    advocate_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name         TEXT NOT NULL,             -- 'Rukhmini Bobde'
    normalized_name   TEXT NOT NULL,
    CONSTRAINT uq_advocates_normalized UNIQUE (normalized_name)
);

CREATE TABLE case_categories (
    category_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category_code   TEXT NOT NULL,   -- 'W.P.(C)', 'CRL.M.A.', 'CRP', 'CS(OS)'
    category_name   TEXT NOT NULL,   -- 'Writ Petition (Civil)', 'Criminal Miscellaneous Application'
    CONSTRAINT uq_case_categories_code UNIQUE (category_code)
);

CREATE TABLE subjects (
    subject_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_name      TEXT NOT NULL,             -- 'Sales Tax', 'Land Acquisition', 'Service Matters'
    parent_subject_id BIGINT REFERENCES subjects(subject_id),
    CONSTRAINT uq_subjects_name UNIQUE (subject_name)
);

CREATE TABLE ministries (
    ministry_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ministry_name   TEXT NOT NULL,               -- 'Ministry of Railways', 'Cabinet Division'
    CONSTRAINT uq_ministries_name UNIQUE (ministry_name)
);

CREATE TABLE departments (
    department_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    department_name TEXT NOT NULL,               -- 'CBEC Excise', 'CBDT'
    ministry_id     BIGINT REFERENCES ministries(ministry_id),
    CONSTRAINT uq_departments_name UNIQUE (department_name)
);

CREATE TABLE industries (
    industry_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    industry_name   TEXT NOT NULL,
    CONSTRAINT uq_industries_name UNIQUE (industry_name)
);

CREATE TABLE statutes (
    statute_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    statute_name    TEXT NOT NULL,               -- 'Indian Penal Code', 'Delhi Sales Tax Act'
    statute_year    INT,
    short_code      TEXT,                        -- 'IPC', 'CrPC', 'DST Act', 'CST Act'
    CONSTRAINT uq_statutes_name_year UNIQUE (statute_name, statute_year)
);

CREATE TABLE sections (
    section_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    statute_id      BIGINT NOT NULL REFERENCES statutes(statute_id),
    section_number  TEXT NOT NULL,               -- '308', '482', '2(l)', '226'
    CONSTRAINT uq_sections_statute_number UNIQUE (statute_id, section_number)
);

-- ---------------------------------------------------------------------
-- 3. PIPELINE TABLES (scrape orchestration + raw OCR staging)
-- ---------------------------------------------------------------------

CREATE TABLE scrape_batches (
    batch_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    court_id        BIGINT REFERENCES courts(court_id),
    date_from       DATE NOT NULL,
    date_to         DATE NOT NULL,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL DEFAULT 'RUNNING',   -- RUNNING / COMPLETED / FAILED
    total_found     INT DEFAULT 0,
    total_downloaded INT DEFAULT 0,
    total_promoted  INT DEFAULT 0
);

-- One row per PDF actually pulled off eCourts, BEFORE it becomes a
-- clean `documents` row. This is your idempotency / retry / audit layer.
CREATE TABLE raw_ingestions (
    ingestion_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id          BIGINT REFERENCES scrape_batches(batch_id),
    court_id          BIGINT REFERENCES courts(court_id),
    data_source       data_source_enum NOT NULL DEFAULT 'ECOURTS',
    source_url        TEXT NOT NULL,              -- eCourts detail/download page
    blob_path         TEXT,                       -- s3://bucket/.../file.pdf
    file_checksum     TEXT,                       -- sha256 of the PDF bytes -> dedup key
    downloaded_at     TIMESTAMPTZ,
    page_count        INT,

    ocr_text          TEXT,                       -- raw OCR dump, pre-cleaning
    ocr_engine        TEXT,                       -- 'textract','tesseract','gpt-ocr' etc
    ocr_confidence    NUMERIC(5,2),
    ocr_completed_at  TIMESTAMPTZ,

    raw_ai_extraction JSONB,                       -- full LLM structured-output payload,
                                                    -- kept verbatim so you can re-parse
                                                    -- without re-running OCR/LLM
    extraction_error  TEXT,
    status            ingestion_status_enum NOT NULL DEFAULT 'QUEUED',

    document_id       BIGINT,                      -- FK added after `documents` exists
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_raw_ingestions_checksum UNIQUE (file_checksum)
);

CREATE INDEX ix_raw_ingestions_status ON raw_ingestions(status);
CREATE INDEX ix_raw_ingestions_batch  ON raw_ingestions(batch_id);

-- ---------------------------------------------------------------------
-- 4. CORE TABLES
-- ---------------------------------------------------------------------

-- One row per JUDGMENT/ORDER PDF. Shared coram, judgment date, OCR text,
-- disposition all live here -- even when it disposes several case numbers.
CREATE TABLE documents (
    document_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ingestion_id        BIGINT UNIQUE REFERENCES raw_ingestions(ingestion_id),
    court_id            BIGINT NOT NULL REFERENCES courts(court_id),
    data_source         data_source_enum NOT NULL DEFAULT 'ECOURTS',

    doc_type            doc_type_enum NOT NULL DEFAULT 'CaseLaw',
    decision_type       decision_type_enum NOT NULL DEFAULT 'Judgment',

    neutral_citation    TEXT,               -- '2026:DHC:7372-DB' (nullable -- not every source has one)
    internal_citation   TEXT,               -- our OWN citation, e.g. 'LIZNR/SCIN/0001/2026' -- generated
                                             -- at promotion time (citation_sequences below), independent
                                             -- of whether the court ever assigned a neutral_citation;
                                             -- doubles as the safe public identifier for this document
                                             -- (sequential document_id is never exposed externally)

    reserved_date       DATE,
    judgment_date        DATE NOT NULL,     -- "Date of Judgement"
    uploaded_date        DATE,

    language            TEXT NOT NULL DEFAULT 'English',

    case_note_ai         TEXT,              -- AI-generated headnote/summary
    ratio_decidendi       TEXT,             -- AI-extracted one-line binding principle -- distinct
                                             -- from case_note_ai (a summary) and from the itemised
                                             -- points in document_holdings below
    disposition_raw       TEXT,             -- verbatim: "the petition is dismissed"
    disposition_category  disposition_category_enum,
    favoring_party_side    party_side_enum, -- who effectively won, if determinable

    overruled_keyword_present BOOLEAN NOT NULL DEFAULT FALSE,  -- keyword-hit flag
                                                                 -- (the real overrule graph is in `citations`)

    pdf_url              TEXT NOT NULL,     -- public/blob URL for the source PDF
    ocr_text             TEXT NOT NULL,     -- final cleaned OCR text
    search_vector         tsvector,         -- maintained by trigger below

    extraction_model      TEXT,             -- which LLM/version produced case_note_ai etc.
    extraction_confidence NUMERIC(5,2),
    needs_review          BOOLEAN NOT NULL DEFAULT FALSE,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE raw_ingestions
    ADD CONSTRAINT fk_raw_ingestions_document
    FOREIGN KEY (document_id) REFERENCES documents(document_id);

CREATE INDEX ix_documents_court_date ON documents(court_id, judgment_date);
CREATE INDEX ix_documents_search ON documents USING GIN (search_vector);
CREATE INDEX ix_documents_disposition ON documents(disposition_category);
CREATE UNIQUE INDEX ux_documents_internal_citation ON documents(internal_citation) WHERE internal_citation IS NOT NULL;

-- One row per CASE NUMBER / CNR (a document can own many of these --
-- see the 10 connected W.P.(C) matters in the Railways judgment).
CREATE TABLE cases (
    case_id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id          BIGINT REFERENCES documents(document_id),  -- disposing judgment
    court_id             BIGINT NOT NULL REFERENCES courts(court_id),

    cnr_number           TEXT,                -- 'DLHC010684312006'
    case_number          TEXT NOT NULL,       -- 'W.P.(C) 13676/2006'
    category_id          BIGINT REFERENCES case_categories(category_id),  -- derived from case_number
    filing_year           INT,

    proceedings_start_date DATE,              -- best-known true start (FIR/complaint/filing date)
    case_filed_date        DATE,              -- formal filing date of THIS instrument, if different
    age_years_ai            NUMERIC(6,2),      -- AI-derived: judgment_date - proceedings_start_date
    age_basis_ai             TEXT,             -- e.g. 'FIR date used as start of proceedings'

    is_lead_matter          BOOLEAN NOT NULL DEFAULT FALSE,
    case_status              TEXT NOT NULL DEFAULT 'Disposed',  -- Disposed/Pending/Reserved

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_cases_cnr UNIQUE (cnr_number),
    CONSTRAINT uq_cases_court_number UNIQUE (court_id, case_number)
);

CREATE INDEX ix_cases_document ON cases(document_id);
CREATE INDEX ix_cases_number_trgm ON cases USING GIN (case_number gin_trgm_ops);

-- ---------------------------------------------------------------------
-- 5. RELATIONSHIP / JUNCTION TABLES + EDITORIAL EXTRACTION LAYER
-- ---------------------------------------------------------------------

CREATE TABLE parties (
    party_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id           BIGINT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    party_name        TEXT NOT NULL,          -- 'Ministry of Railways', 'Babu Khan'
    party_side        party_side_enum NOT NULL,
    party_designation TEXT,                   -- 'Petitioner', 'Respondent No. 2', '& Ors.'
    party_order       SMALLINT NOT NULL DEFAULT 1,
    is_government_entity BOOLEAN NOT NULL DEFAULT FALSE,
    ministry_id       BIGINT REFERENCES ministries(ministry_id),
    department_id     BIGINT REFERENCES departments(department_id)
);

CREATE INDEX ix_parties_case ON parties(case_id);
CREATE INDEX ix_parties_name_trgm ON parties USING GIN (party_name gin_trgm_ops);

CREATE TABLE case_counsels (
    case_counsel_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id             BIGINT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    advocate_id         BIGINT NOT NULL REFERENCES advocates(advocate_id),
    side                party_side_enum NOT NULL,
    designation         TEXT,               -- 'Sr. Adv.', 'ASG', 'CGSC', 'Adv.'
    represents_party_id BIGINT REFERENCES parties(party_id),  -- optional disambiguation, e.g. "for GNCTD"
    CONSTRAINT uq_case_counsel UNIQUE (case_id, advocate_id, side)
);

CREATE INDEX ix_case_counsels_advocate ON case_counsels(advocate_id);

CREATE TABLE document_coram (
    document_id  BIGINT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    judge_id     BIGINT NOT NULL REFERENCES judges(judge_id),
    is_author    BOOLEAN NOT NULL DEFAULT FALSE,   -- who signed/authored ("X, J.")
    bench_order  SMALLINT NOT NULL DEFAULT 1,
    PRIMARY KEY (document_id, judge_id)
);

CREATE TABLE document_sections (
    document_id  BIGINT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    section_id   BIGINT NOT NULL REFERENCES sections(section_id),
    context      TEXT,                 -- 'invoked for bail', 'definition of dealer'
    is_primary   BOOLEAN NOT NULL DEFAULT FALSE,  -- editorially-chosen "lead" section, e.g.
                                                   -- Manupatra's single "Relevant Section" line
    PRIMARY KEY (document_id, section_id)
);

-- At most one primary section per document.
CREATE UNIQUE INDEX ux_document_sections_one_primary ON document_sections(document_id) WHERE is_primary;

CREATE TABLE document_subjects (
    document_id BIGINT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    subject_id  BIGINT NOT NULL REFERENCES subjects(subject_id),
    PRIMARY KEY (document_id, subject_id)
);

CREATE TABLE document_industries (
    document_id BIGINT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    industry_id BIGINT NOT NULL REFERENCES industries(industry_id),
    PRIMARY KEY (document_id, industry_id)
);

CREATE TABLE document_ministry_department (
    document_id   BIGINT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    ministry_id   BIGINT REFERENCES ministries(ministry_id),
    department_id BIGINT REFERENCES departments(department_id),
    relation_type TEXT NOT NULL DEFAULT 'Party'   -- 'Party' | 'SubjectMatter'
);

-- Atomic per-(court, year) counter backing documents.internal_citation
-- ('LIZNR/<court_code>/<seq>/<year>'). Resets every year, same convention
-- as Manupatra's own MANU/XX/NNNN/YYYY scheme and the official INSC neutral
-- citation -- NOT a lifetime running count (a decades-old court would have
-- implausibly large numbers by now if it never reset). Claimed via
-- INSERT ... ON CONFLICT DO UPDATE ... RETURNING next_seq, which Postgres
-- serializes correctly under concurrent promotions via the row lock the
-- UPDATE takes.
CREATE TABLE citation_sequences (
    court_id       BIGINT NOT NULL REFERENCES courts(court_id),
    citation_year  INT NOT NULL,
    next_seq       INT NOT NULL DEFAULT 1,
    PRIMARY KEY (court_id, citation_year)
);

-- Held points: numbered holdings extracted from a judgment, each pinned to
-- a paragraph in document_paragraphs below. Distinct from the single
-- case_note_ai blob -- Manupatra shows these as a separate numbered "Held"
-- list, each with its own pinpoint paragraph reference.
CREATE TABLE document_holdings (
    holding_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id   BIGINT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    ordinal       SMALLINT NOT NULL,
    holding_text  TEXT NOT NULL,
    paragraph_ref TEXT,                -- e.g. '9' or '4, 8' -- free text like citations.paragraph_ref,
                                        -- not a hard FK, since one holding can span several paragraphs
    CONSTRAINT uq_document_holdings_ordinal UNIQUE (document_id, ordinal)
);

CREATE INDEX ix_document_holdings_document ON document_holdings(document_id);

-- Appellate lineage: the specific lower-court/lower-forum order THIS
-- document is reviewing, and what happened to it on appeal. Deliberately
-- separate from `citations` -- a citation is "precedent this judgment
-- discusses", this is "the order this judgment is literally sitting in
-- appeal/revision over". Covers both Manupatra's "Prior History" line and
-- its "Cases Affirmed/Reversed on Appeal" section -- same underlying fact,
-- read two ways.
CREATE TABLE case_appellate_history (
    appellate_history_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id           BIGINT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    prior_court_id         BIGINT REFERENCES courts(court_id),        -- set when the lower forum is itself a modeled court
    prior_court_name       TEXT,                                      -- free-text fallback, e.g. 'Judicial Magistrate, Roorkee'
    prior_case_number      TEXT,                                      -- e.g. 'CRRFC No. 1/2015 and CRLA No. 39/2015'
    prior_order_date       DATE,
    prior_document_id      BIGINT REFERENCES documents(document_id),  -- resolved link, if the lower court judgment is itself in the database
    outcome                appellate_outcome_enum,
    notes                  TEXT
);

CREATE INDEX ix_case_appellate_history_document ON case_appellate_history(document_id);
CREATE INDEX ix_case_appellate_history_prior_doc ON case_appellate_history(prior_document_id);

-- Reconstructed procedural timeline (FIR -> bail -> chargesheet -> ... ->
-- judgment). Case-level, not document-level, since the procedural history
-- belongs to the case number, not to whichever document eventually
-- disposes it. `ordinal` carries the real ordering because some events
-- (e.g. "post-investigation, exact date not stated in the judgment") have
-- no usable date to sort by.
CREATE TABLE case_timeline_events (
    event_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id         BIGINT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    ordinal         SMALLINT NOT NULL,
    event_date      DATE,               -- nullable: not every event has a stated date
    event_date_text TEXT,               -- fallback label, e.g. 'Post-investigation'
    description     TEXT NOT NULL,
    CONSTRAINT uq_case_timeline_events_ordinal UNIQUE (case_id, ordinal)
);

CREATE INDEX ix_case_timeline_events_case ON case_timeline_events(case_id);

-- Paragraph-addressable judgment text. `documents.ocr_text` stays the
-- source of truth (full raw text, always populated); this table is the
-- structured overlay that makes `citations.paragraph_ref` and
-- `document_holdings.paragraph_ref` actually anchorable in the UI, instead
-- of the frontend regex-splitting `ocr_text` at render time.
CREATE TABLE document_paragraphs (
    document_id  BIGINT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    para_number  INT NOT NULL,
    para_text    TEXT NOT NULL,
    PRIMARY KEY (document_id, para_number)
);

-- The precedent / citation graph. This is where real "Overruled" status
-- lives (as opposed to the coarse keyword flag on `documents`).
CREATE TABLE citations (
    citation_id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    citing_document_id      BIGINT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    cited_case_name         TEXT NOT NULL,     -- 'Prahlad Singh Bhati v. NCT, Delhi'
    cited_reporter_citation TEXT,              -- '(2001) 4 SCC 280', 'AIR 2007 SCW 3123'
    cited_document_id       BIGINT REFERENCES documents(document_id),  -- resolved match, if any
    treatment                citation_treatment_enum,
    paragraph_ref             TEXT
);

CREATE INDEX ix_citations_citing ON citations(citing_document_id);
CREATE INDEX ix_citations_cited_doc ON citations(cited_document_id);
CREATE INDEX ix_citations_name_trgm ON citations USING GIN (cited_case_name gin_trgm_ops);

-- ---------------------------------------------------------------------
-- 6. SEARCH VECTOR MAINTENANCE + updated_at TRIGGERS
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION documents_search_vector_update() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', coalesce(NEW.case_note_ai,'')), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.disposition_raw,'')), 'B') ||
        setweight(to_tsvector('english', coalesce(NEW.ocr_text,'')), 'D');
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_documents_search_vector
BEFORE INSERT OR UPDATE ON documents
FOR EACH ROW EXECUTE FUNCTION documents_search_vector_update();

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_cases_touch BEFORE UPDATE ON cases
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TRIGGER trg_raw_ingestions_touch BEFORE UPDATE ON raw_ingestions
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ---------------------------------------------------------------------
-- 7. CONVENIENCE VIEW for API / search layer
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW case_search_view AS
SELECT
    c.case_id,
    c.cnr_number,
    c.case_number,
    c.court_id,
    cc.category_name AS case_category,
    c.age_years_ai,
    d.document_id,
    crt.court_name,
    d.judgment_date,
    d.decision_type,
    d.disposition_category,
    d.disposition_raw,
    d.case_note_ai,
    d.ratio_decidendi,
    d.overruled_keyword_present,
    d.neutral_citation,
    d.internal_citation,
    d.pdf_url,
    d.search_vector,
    -- Derived treatment status (spec §5.3): the old schema stored this as a
    -- column kept in sync by a nightly recompute job; the new schema has no
    -- such column by design — treatment lives per-citation-edge in the
    -- `citations` graph, computed here on read instead. Priority order
    -- matches the old recompute job's: Overruled > Doubted > Distinguished > GOOD_LAW.
    (CASE
        WHEN EXISTS (SELECT 1 FROM citations ct WHERE ct.cited_document_id = d.document_id AND ct.treatment = 'Overruled') THEN 'OVERRULED'
        WHEN EXISTS (SELECT 1 FROM citations ct WHERE ct.cited_document_id = d.document_id AND ct.treatment = 'Doubted') THEN 'DOUBTED'
        WHEN EXISTS (SELECT 1 FROM citations ct WHERE ct.cited_document_id = d.document_id AND ct.treatment = 'Distinguished') THEN 'DISTINGUISHED'
        ELSE 'GOOD_LAW'
    END) AS treatment_status,
    (SELECT array_agg(j.full_name ORDER BY dc.bench_order)
       FROM document_coram dc JOIN judges j ON j.judge_id = dc.judge_id
      WHERE dc.document_id = d.document_id) AS coram,
    (SELECT array_agg(DISTINCT p.party_name)
       FROM parties p WHERE p.case_id = c.case_id AND p.party_side = 'PETITIONER_SIDE') AS petitioners,
    (SELECT array_agg(DISTINCT p.party_name)
       FROM parties p WHERE p.case_id = c.case_id AND p.party_side = 'RESPONDENT_SIDE') AS respondents,
    (SELECT array_agg(DISTINCT s.subject_name)
       FROM document_subjects ds JOIN subjects s ON s.subject_id = ds.subject_id
      WHERE ds.document_id = d.document_id) AS subjects,
    (SELECT array_agg(DISTINCT st.statute_name)
       FROM document_sections dsec JOIN sections sec ON sec.section_id = dsec.section_id
       JOIN statutes st ON st.statute_id = sec.statute_id
      WHERE dsec.document_id = d.document_id) AS acts
FROM cases c
JOIN documents d ON d.document_id = c.document_id
JOIN courts crt ON crt.court_id = c.court_id
LEFT JOIN case_categories cc ON cc.category_id = c.category_id;

-- =====================================================================
-- 8. OPERATIONAL SUPPLEMENT (not part of the domain schema above) —
--    scraper-backend-v2 revamp spec §3.1 (court scrape config) and §6
--    (admin-configurable filter/search metadata that api-backend serves).
-- =====================================================================

-- One row per court that gets scraped. Turns "25 Python files" into
-- "25 rows" — see docs/scraper-backend-revamp-spec.md §4.3.
CREATE TABLE court_scrape_config (
    court_id        BIGINT PRIMARY KEY REFERENCES courts(court_id),
    adapter         TEXT NOT NULL,              -- 'supreme_court' | 'ecourts'
    state_code      TEXT,                       -- eCourts state_code select value (e.g. '7~26' for Delhi)
    bench_code      TEXT,                       -- eCourts dist_code select value
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    last_scraped_to DATE,                       -- watermark: resume from here on next run
    notes           TEXT,
    CONSTRAINT ck_court_scrape_config_adapter CHECK (adapter IN ('supreme_court', 'ecourts'))
);

-- Admin-managed filter metadata for api-backend's /api/cases/filters
-- (spec §6) — carried over unchanged from the old flat schema, since it's
-- presentation config, not case data, and isn't part of caselaw_schema.sql.
CREATE TABLE filter_definitions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key             TEXT UNIQUE NOT NULL,
    label           TEXT NOT NULL,
    type            TEXT NOT NULL DEFAULT 'select',
    selection_mode  TEXT NOT NULL DEFAULT 'multi',
    query_key       TEXT,                                 -- only set when it differs from `key`
    data_source     TEXT NOT NULL DEFAULT 'database',      -- 'database' (fixed live-count SQL) | 'static'
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    is_searchable   BOOLEAN NOT NULL DEFAULT FALSE,
    display_order   INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE filter_options (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filter_id       UUID NOT NULL REFERENCES filter_definitions(id) ON DELETE CASCADE,
    value           TEXT NOT NULL,
    label           TEXT NOT NULL,
    display_order   INT NOT NULL DEFAULT 0,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE search_field_definitions (
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

CREATE INDEX ix_filter_options_filter_id ON filter_options(filter_id);

CREATE TRIGGER trg_filter_definitions_touch BEFORE UPDATE ON filter_definitions
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TRIGGER trg_search_field_definitions_touch BEFORE UPDATE ON search_field_definitions
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
