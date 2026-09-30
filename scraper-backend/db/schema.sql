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
-- to regex-first extraction (adapters/supreme_court/extraction.py) for everything
-- that's pattern-shaped, with the LLM kept only for fields that need actual
-- reading comprehension (case_note, and other fields not modeled here yet)
-- — most of the old normalized structure existed to hold LLM-envelope
-- fields (citations/treatment, timeline, holdings, appellate history) this
-- iteration doesn't populate; (2) an explicit ask to trim the schema down
-- to only the fields actually needed right now. LLM-sourced columns
-- (case_note; industries, which has no reliable regex signal — see
-- adapters/supreme_court/extraction.py's module docstring) are populated by
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
-- services. Intended to run ONCE against an empty database — the enum
-- types below are wrapped in DO blocks so re-running is safe for THEM
-- (see §1), but the CREATE TABLE statements still aren't idempotent, so
-- re-running this without dropping first will still fail with
-- "relation already exists" on the tables. Use
-- `python -m db.init_db init --drop` for a clean local re-init.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 0. EXTENSIONS
-- ---------------------------------------------------------------------
-- unaccent/btree_gin/pgcrypto were dropped here (2026-09-11): none of them
-- is actually referenced anywhere in this schema or the pipeline/
-- normalization code (no unaccent() call, no composite btree+GIN index, no
-- gen_random_uuid() call — and Postgres 13+ ships gen_random_uuid() in core
-- anyway). pg_trgm is the only one genuinely load-bearing: the three
-- gin_trgm_ops indexes below need it.
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

-- Real, stable advocate identity (enrollment_no) -- a source that gives one
-- (e.g. Madhya Pradesh's case-status page, format "NAME[P-1] [3258/1996]")
-- gets a proper cr_advocates row; a source that only gives a bare name (e.g.
-- Supreme Court's results table) keeps using cr_cases.petitioner_advocate/
-- respondent_advocate (plain TEXT, added in migration 0007) instead --
-- there's nothing to normalize without an enrollment number.
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
-- matching the field name the new `cr_cases.acts` array actually points at.
CREATE TABLE IF NOT EXISTS cr_acts (
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

-- Rules/Orders (separate lookup tables + cr_cases.rules/orders arrays,
-- alongside a rules_relevant/_other + orders_relevant/_other LLM-classified
-- split) were dropped 2026-09-19 (db/migrations/0012) -- only Section-level
-- provisions are tracked now, not Rules/Orders as a distinct kind.
CREATE TABLE IF NOT EXISTS cr_sections (
    section_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    act_id          BIGINT NOT NULL REFERENCES cr_acts(act_id),
    section_number  TEXT NOT NULL,               -- '308', '482', '2(l)', '226'
    CONSTRAINT uq_cr_sections_act_number UNIQUE (act_id, section_number)
);

-- ---------------------------------------------------------------------
-- 3. PIPELINE TABLE (scrape/OCR staging)
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
    -- Set by POST /api/scraper/batches/{id}/cancel; orchestrator/batch_runner.py's per-record
    -- loop polls this between records and stops early once true (see db/migrations/0003).
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    -- NULL while RUNNING; set once by finish_batch() alongside `status`. Without this there is
    -- no way to compute how long a finished batch actually took — only requested_at exists
    -- otherwise (see db/migrations/0004).
    finished_at     TIMESTAMPTZ,
    -- Set by finish_batch() on a source-failure status (SOURCE_BLOCKED/RATE_LIMITED/
    -- SOURCE_UNAVAILABLE/STRUCTURE_CHANGED) or a top-level adapter exception (FAILED) --
    -- orchestrator/batch_runner.py's run_batch() has always tried to pass this, but
    -- finish_batch() silently had no column/param for it until db/migrations/0009
    -- (found live: a real sci.gov.in 403 crashed with an unhandled TypeError instead
    -- of cleanly recording the batch as failed). NULL for a normal COMPLETED/CANCELLED batch.
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

-- One row per PDF actually pulled off the court site, BEFORE it becomes a
-- clean `cr_cases` row. Idempotency/retry/audit layer -- kept deliberately
-- minimal (no raw_ai_extraction JSONB blob, no separate EXTRACTED status)
-- since regex-derived fields are computed directly at promotion time, not
-- staged through a separate LLM-extraction step the way the old pipeline
-- was. That LLM step comes back later as an update to specific `cr_cases`
-- columns (case_note, industries), not as a cr_raw_ingestions stage again.
CREATE TABLE IF NOT EXISTS cr_raw_ingestions (
    ingestion_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id          BIGINT REFERENCES cr_scrape_batches(batch_id),
    court_id          BIGINT REFERENCES cr_courts(court_id),
    -- No longer defaults to 'ECOURTS' -- that adapter is retired (every
    -- High Court now scrapes its own site via its own adapter) and every
    -- insert already sets this explicitly (routers/scraper_router.py's
    -- _ADAPTER_REGISTRY, threaded through orchestrator/batch_runner.py),
    -- so the default is only ever a safety net, never a real value; a
    -- prior real bug was exactly this default being silently relied upon.
    data_source       data_source_enum NOT NULL DEFAULT 'OTHER',

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
    status        TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING / DONE / NO_JUDGMENT / SKIPPED / FAILED
    reason        TEXT,
    ingestion_id  BIGINT REFERENCES cr_raw_ingestions(ingestion_id),
    case_id       BIGINT,                             -- -> cr_cases, FK added after cr_cases below
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

-- One row per scraped case (one PDF, one results-table row). Flattened
-- from the old documents+cases split: every field the current pipeline
-- populates lives directly here, with array columns (bench/sections/acts/
-- rules/orders/ministries/industries/case_category) replacing what used to
-- be separate junction tables. Trade-off accepted deliberately: Postgres
-- can't FK-constrain the contents of an array column, so referential
-- integrity into judges/acts/sections/etc. is enforced in application code
-- (adapters/supreme_court/promotion.py's get-or-create helpers), not by the database.
CREATE TABLE IF NOT EXISTS cr_cases (
    case_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    liznr_id           TEXT,                     -- our own citation, e.g. 'LIZNR/SCIN/0001/2026' -- generated
                                                  -- at promotion time (citation_sequences below); doubles as
                                                  -- the safe public identifier (case_id is never exposed externally)
    court_id           BIGINT NOT NULL REFERENCES cr_courts(court_id),

    case_number        TEXT NOT NULL,
    cnr                TEXT,                     -- pan-India eCourts case number record, when the source
                                                  -- page exposes one (e.g. Madhya Pradesh's case-status page)
    petitioner         TEXT,
    respondent         TEXT,
    petitioner_advocate TEXT,  -- regex-parsed from the SCI results table's "Petitioner/Respondent
                               -- Advocate" cell (adapters.supreme_court.extraction.parse_advocates) --
                               -- reintroduces the advocate data the 2026-09-08 flattening dropped.
                               -- Supreme-Court-only: a bare name, no enrollment number to normalize --
                               -- see cr_advocates/petitioner_advocate_ids below for a source that has one
    respondent_advocate TEXT,  -- frequently NULL even when petitioner_advocate isn't -- see
                               -- parse_advocates' own docstring on why the respondent side is so
                               -- often simply missing from the source cell, not a parsing failure

    -- Normalized advocate identity (-> cr_advocates.advocate_id), for a source that gives a real
    -- enrollment number (e.g. Madhya Pradesh's case-status page) -- independent of the flat
    -- petitioner_advocate/respondent_advocate TEXT columns above, which stay Supreme-Court-only.
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
    judgment_by        BIGINT REFERENCES cr_judges(judge_id),  -- single judge_id -- who authored/signed

    judgment_date      DATE,
    language           TEXT,
    neutral_citation   TEXT,

    sections           BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_sections.section_id
    acts               BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_acts.act_id (every act referenced by any section below)
    subject            BIGINT REFERENCES cr_subjects(subject_id),  -- coarse Civil/Criminal/... tag

    case_note          TEXT,                     -- LLM-generated headnote (pipeline/llm_enrichment.py), Manupatra-style dash-separated digest
    conclusion         TEXT,                     -- regex, low coverage (~1-3% of judgments have a literal heading) -- see adapters/supreme_court/extraction.py
    judgement          TEXT,                     -- full opinion text after the "J U D G M E N T"/"O R D E R" heading
    ocr_text           TEXT,                     -- final OCR text, source of truth for judgement/conclusion/provisions above -- NEVER overwritten by parsing/enrichment

    source_pdf_url     TEXT,
    blob_pdf_id       TEXT,

    ministries         BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_ministries.ministry_id, matched against petitioner/respondent only (see regex_extraction.find_ministry_in_party_name)
    industries         BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_industries.industry_id -- LLM-classified (pipeline/llm_enrichment.py); no reliable regex signal exists for this field (see adapters/supreme_court/extraction.py's module docstring)

    disposition        disposition_category_enum,
    favouring_party    favouring_party_enum,       -- LLM-classified (pipeline/llm_enrichment.py) -- which side the outcome favoured
    document_type      doc_type_enum NOT NULL DEFAULT 'CaseLaw',  -- constant for this adapter -- every sci.gov.in row is a court judgment/order, not extracted per-row
    case_category      BIGINT[] NOT NULL DEFAULT '{}',  -- -> cr_case_categories.category_id

    needs_review       BOOLEAN NOT NULL DEFAULT FALSE,  -- set when judgment_date couldn't be parsed or another required signal was missing

    -- AVAILABLE, or why this case has no judgment yet (db/migrations 0017 / api 0004): metadata-only
    -- cases are saved rather than skipped, and filled in by a later run that gets the PDF.
    judgment_status          TEXT NOT NULL DEFAULT 'AVAILABLE'
                             CONSTRAINT ck_cr_cases_judgment_status CHECK (judgment_status IN ('AVAILABLE', 'NOT_PUBLISHED', 'DOWNLOAD_FAILED')),
    judgment_missing_reason  TEXT,
    judgment_checked_at      TIMESTAMPTZ,

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

DO $$ BEGIN
    ALTER TABLE cr_raw_ingestions
        ADD CONSTRAINT fk_cr_raw_ingestions_case
        FOREIGN KEY (case_id) REFERENCES cr_cases(case_id);
EXCEPTION WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE cr_batch_items
        ADD CONSTRAINT fk_cr_batch_items_case
        FOREIGN KEY (case_id) REFERENCES cr_cases(case_id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN null;
END $$;
CREATE INDEX IF NOT EXISTS ix_cr_batch_items_case ON cr_batch_items(case_id) WHERE case_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_cr_cases_court_date ON cr_cases(court_id, judgment_date);
CREATE INDEX IF NOT EXISTS ix_cr_cases_missing_judgment ON cr_cases(court_id, judgment_status) WHERE judgment_status <> 'AVAILABLE';
CREATE INDEX IF NOT EXISTS ix_cr_cases_created_at ON cr_cases(created_at);
CREATE INDEX IF NOT EXISTS ix_cr_cases_disposition ON cr_cases(disposition);
CREATE INDEX IF NOT EXISTS ix_cr_cases_search ON cr_cases USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS ix_cr_cases_number_trgm ON cr_cases USING GIN (case_number gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_cr_cases_petitioner_trgm ON cr_cases USING GIN (petitioner gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_cr_cases_respondent_trgm ON cr_cases USING GIN (respondent gin_trgm_ops);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cr_cases_liznr_id ON cr_cases(liznr_id) WHERE liznr_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_cr_cases_cnr ON cr_cases(cnr) WHERE cnr IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_cr_cases_enrichment_pending ON cr_cases (case_id) WHERE enrichment_status IN ('PENDING', 'FAILED', 'TRUNCATED');

-- Atomic per-(court, year) counter backing cr_cases.liznr_id
-- ('LIZNR/<court_code>/<seq>/<year>'). Resets every year, same convention
-- as Manupatra's own MANU/XX/NNNN/YYYY scheme and the official INSC neutral
-- citation. Claimed via INSERT ... ON CONFLICT DO UPDATE ... RETURNING
-- next_seq, which Postgres serializes correctly under concurrent
-- promotions via the row lock the UPDATE takes.
CREATE TABLE IF NOT EXISTS cr_citation_sequences (
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

-- One row per court that gets scraped. Turns "one Python file per court"
-- into "one row per court" -- each court has its own adapter (no shared
-- generic eCourts adapter; see adapters/__init__.py), so `adapter` names a
-- key in routers/scraper_router.py's `_ADAPTER_REGISTRY` rather than a
-- fixed SQL enum -- that registry grows one court at a time, in code, not
-- via a migration per court.
CREATE TABLE IF NOT EXISTS cr_court_scrape_config (
    court_id        BIGINT PRIMARY KEY REFERENCES cr_courts(court_id),
    adapter         TEXT NOT NULL,              -- e.g. 'supreme_court' -- must match an _ADAPTER_REGISTRY key
    config          JSONB NOT NULL DEFAULT '{}', -- that adapter's own free-form settings (e.g. a High Court's base URL) -- no adapter-specific columns here
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    last_scraped_to DATE,                       -- watermark: resume from here on next run
    notes           TEXT
);
