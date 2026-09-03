"""
PostgreSQL Connection, Schema & Scraper-Job Tracking
-------------------------------------------------------------
api-backend's own copy of the DB layer (kept in sync by hand with
scraper-backend's copy — the two services no longer share code).
Defines the production schema (Global UUIDs, Master Judge & Act tables with
alias mapping, the Citator & Citation Graph) and the scraper_jobs tracking
CRUD that api-backend's /api/scraper/* proxying reads/writes.

Case-ingestion logic (writing cases/parties/citations from scraped JSON)
lives in scraper-backend/ingestion.py, not here — only the scraper writes
case data, so that logic doesn't need to ship inside api-backend's image.
"""

import os
import sys
import argparse
import json
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, Any, List, Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import psycopg2
    import psycopg2.pool
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None

import normalizer


# ============================================================
# CONFIGURATION & CONNECTION
# ============================================================

def get_db_url() -> str:
    """
    Constructs Database connection string prioritizing remote Azure DB credentials
    when available, or local Docker / .env credentials.
    """
    host = os.environ.get("LEGAL_CORPUS_DB_HOST")
    db_name = os.environ.get("LEGAL_CORPUS_DB_NAME", "legal_db")
    user = os.environ.get("LEGAL_CORPUS_DB_USER", "postgres")
    password = os.environ.get("LEGAL_CORPUS_DB_PASSWORD", "1234")
    port = os.environ.get("LEGAL_CORPUS_DB_PORT", "5432")

    # 1. If pointing to remote Azure PostgreSQL Flexible Server
    if host and ("azure.com" in host or "postgres.database" in host) and db_name and user and password:
        return f"postgresql://{user}:{password}@{host}:{port}/{db_name}"

    # 2. Check full connection strings from docker-compose / environment
    url = os.environ.get("DATABASE_URL") or os.environ.get("DB_CONNECTION")
    if url:
        if "//@db:" in url and not os.path.exists("/.dockerenv"):
            url = url.replace("//@db:", "//@localhost:")
        return url

    # 3. Fallback defaults (Local / Docker)
    fallback_host = "localhost" if not os.path.exists("/.dockerenv") else (host or "db")
    return f"postgresql://{user}:{password}@{fallback_host}:{port}/{db_name}"


def get_connection(db_url: Optional[str] = None):
    """Establishes a fresh, unpooled connection to PostgreSQL. Used by scraper-backend,
    whose ingestion transactions are long-held and shouldn't share a pool sized for
    api-backend's short reads."""
    if psycopg2 is None:
        raise ImportError("psycopg2 module is missing. Run: pip install psycopg2-binary")
    target_url = db_url or get_db_url()
    return psycopg2.connect(target_url)


# ============================================================
# CONNECTION POOL (api-backend only)
# ============================================================
# scraper-backend must keep using get_connection() above — its ingestion writes
# hold a connection for the duration of a whole scrape/import run, which would
# starve a pool sized for api-backend's fast, frequent reads.

_pool: "Optional[psycopg2.pool.ThreadedConnectionPool]" = None


def init_connection_pool(minconn: int = 2, maxconn: int = 20, db_url: Optional[str] = None) -> None:
    """Creates the process-wide read connection pool. Call once, at api-backend startup."""
    global _pool
    if psycopg2 is None:
        raise ImportError("psycopg2 module is missing. Run: pip install psycopg2-binary")
    if _pool is not None:
        return
    _pool = psycopg2.pool.ThreadedConnectionPool(minconn, maxconn, db_url or get_db_url())


def close_connection_pool() -> None:
    """Closes all pooled connections. Call once, at api-backend shutdown."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


@contextmanager
def get_pooled_connection():
    """
    Context manager yielding a connection borrowed from the pool, returning it
    automatically afterward. Rolls back any open transaction before returning
    the connection so a failed request never leaves the pooled connection dirty
    for the next borrower.

        with db_manager.get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
    """
    if _pool is None:
        raise RuntimeError("Connection pool not initialized — call init_connection_pool() at startup first.")
    conn = _pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def parse_date(date_str: Optional[str]) -> Optional[str]:
    """Parses various date formats into YYYY-MM-DD."""
    if not date_str:
        return None
    date_str = date_str.strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%d %B %Y"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


# ============================================================
# PRODUCTION SCHEMA DDL (Global UUIDs & Master Tables)
# ============================================================

CREATE_TABLES_SQL = """
-- 0. EXTENSIONS
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- 1. COURTS MASTER
CREATE TABLE IF NOT EXISTS courts (
    court_id VARCHAR(50) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    type VARCHAR(50),
    state_code VARCHAR(50),
    state_name VARCHAR(100),
    bench_seat VARCHAR(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. JUDGE MASTER & ALIASES
CREATE TABLE IF NOT EXISTS judge_master (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name TEXT UNIQUE NOT NULL,
    court_type VARCHAR(50) DEFAULT 'SUPREME_COURT',
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS judge_aliases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    judge_id UUID NOT NULL REFERENCES judge_master(id) ON DELETE CASCADE,
    alias_name TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. ACT MASTER & ALIASES
CREATE TABLE IF NOT EXISTS act_master (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name TEXT UNIQUE NOT NULL,
    short_code VARCHAR(50),
    year INTEGER,
    jurisdiction VARCHAR(50) DEFAULT 'CENTRAL',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS act_aliases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    act_id UUID NOT NULL REFERENCES act_master(id) ON DELETE CASCADE,
    alias_pattern TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 4. CASES TABLE (System of Record)
CREATE TABLE IF NOT EXISTS cases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    court_id VARCHAR(50) REFERENCES courts(court_id) ON DELETE SET NULL,
    diary_number VARCHAR(100),
    case_id_code VARCHAR(100),
    cnr VARCHAR(100),
    case_number TEXT,
    case_category VARCHAR(100),
    registration_date DATE,
    judgment_date DATE,
    neutral_citation VARCHAR(150),
    disposal_nature VARCHAR(100),
    result_text TEXT,
    case_age_days INTEGER,
    language VARCHAR(20) DEFAULT 'en',
    document_type VARCHAR(100),
    confidence_score NUMERIC(5,2),
    case_note_ai TEXT,              -- AI Summary generated from PDF text
    treatment_status VARCHAR(30) DEFAULT 'GOOD_LAW', -- GOOD_LAW, DOUBTED, OVERRULED, DISTINGUISHED
    overruled BOOLEAN DEFAULT FALSE,
    is_reported BOOLEAN DEFAULT FALSE,
    reporting_status VARCHAR(50),
    reporting_source VARCHAR(100),
    pdf_path TEXT,                 -- Local filesystem or Blob path
    pdf_url TEXT,                  -- Direct download / online URL
    source_page TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 5. CASE_JUDGES JUNCTION
CREATE TABLE IF NOT EXISTS case_judges (
    case_id UUID NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    judge_id UUID NOT NULL REFERENCES judge_master(id) ON DELETE CASCADE,
    role VARCHAR(50) DEFAULT 'BENCH_MEMBER', -- PRESIDING, COMPANION, BENCH_MEMBER
    PRIMARY KEY (case_id, judge_id)
);

-- 6. PARTIES TABLE
CREATE TABLE IF NOT EXISTS parties (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    role VARCHAR(50),            -- PETITIONER, RESPONDENT, COMPLAINANT, ACCUSED
    party_type VARCHAR(50),      -- INDIVIDUAL, STATE, CORPORATION
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 7. ADVOCATES MASTER & JUNCTION
CREATE TABLE IF NOT EXISTS advocates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT UNIQUE NOT NULL,
    designation VARCHAR(100)
);

CREATE TABLE IF NOT EXISTS case_advocates (
    case_id UUID NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    advocate_id UUID NOT NULL REFERENCES advocates(id) ON DELETE CASCADE,
    party_role VARCHAR(50),      -- PETITIONER, RESPONDENT, ADVOCATE
    PRIMARY KEY (case_id, advocate_id)
);

-- 8. PROVISIONS TABLE
CREATE TABLE IF NOT EXISTS provisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    act_id UUID REFERENCES act_master(id) ON DELETE SET NULL,
    raw_act_name TEXT NOT NULL,
    section VARCHAR(100),
    provision_full TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 9. CASE_ARTICLES TABLE
CREATE TABLE IF NOT EXISTS case_articles (
    case_id UUID NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    article VARCHAR(100) NOT NULL,
    PRIMARY KEY (case_id, article)
);

-- 10. CITATIONS TABLE (The Citator Graph)
CREATE TABLE IF NOT EXISTS citations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    citing_case_id UUID NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    cited_case_id UUID REFERENCES cases(id) ON DELETE SET NULL,
    raw_citation_text TEXT NOT NULL,
    reporter_type VARCHAR(50),
    treatment_type VARCHAR(30) DEFAULT 'REFERRED', -- OVERRULED, DOUBTED, DISTINGUISHED, FOLLOWED, REFERRED
    confidence_score NUMERIC(5,2) DEFAULT 50.0,
    context_snippet TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 11. SCRAPER_JOBS TABLE (Deduplication, Checkpointing & Run Logs)
CREATE TABLE IF NOT EXISTS scraper_jobs (
    job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    court_id VARCHAR(50) NOT NULL REFERENCES courts(court_id) ON DELETE CASCADE,
    from_date DATE,
    to_date DATE,
    year INT,
    status VARCHAR(50) DEFAULT 'RUNNING',
    total_cases_found INT DEFAULT 0,
    new_cases_scraped INT DEFAULT 0,
    skipped_cases INT DEFAULT 0,
    bronze_blob_url TEXT,
    silver_blob_url TEXT,
    error_message TEXT,
    logs JSONB DEFAULT '[]'::jsonb,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE,
    notes TEXT
);

-- 12. FILTER_DEFINITIONS & FILTER_OPTIONS (Admin-managed /api/cases/filters)
CREATE TABLE IF NOT EXISTS filter_definitions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'select',
    selection_mode TEXT NOT NULL DEFAULT 'multi',
    query_key TEXT,                                 -- only set when it differs from `key` (e.g. court -> court_id)
    data_source TEXT NOT NULL DEFAULT 'database',    -- 'database' (fixed live-count SQL) | 'static' (admin-supplied options)
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    is_searchable BOOLEAN NOT NULL DEFAULT FALSE,    -- frontend hint: render a search box inside the option list (large lists e.g. judges, acts)
    display_order INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS filter_options (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filter_id UUID NOT NULL REFERENCES filter_definitions(id) ON DELETE CASCADE,
    value TEXT NOT NULL,
    label TEXT NOT NULL,
    display_order INT NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 13. SEARCH_FIELD_DEFINITIONS (Admin-managed /api/cases/searches)
CREATE TABLE IF NOT EXISTS search_field_definitions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    placeholder TEXT NOT NULL DEFAULT 'Search items...',
    combinator TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    display_order INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 14. INDEXES FOR LIGHTNING FAST RETRIEVAL & DEDUPLICATION
CREATE INDEX IF NOT EXISTS idx_cases_court_id ON cases(court_id);
CREATE INDEX IF NOT EXISTS idx_cases_diary_num ON cases(diary_number);
CREATE INDEX IF NOT EXISTS idx_cases_judgment_date ON cases(judgment_date);
CREATE INDEX IF NOT EXISTS idx_cases_treatment ON cases(treatment_status);
CREATE INDEX IF NOT EXISTS idx_cases_overruled ON cases(overruled);
CREATE INDEX IF NOT EXISTS idx_cases_is_reported ON cases(is_reported);

CREATE INDEX IF NOT EXISTS idx_citations_citing ON citations(citing_case_id);
CREATE INDEX IF NOT EXISTS idx_citations_cited ON citations(cited_case_id);
CREATE INDEX IF NOT EXISTS idx_citations_treatment ON citations(treatment_type);

CREATE INDEX IF NOT EXISTS idx_provisions_act ON provisions(act_id);
CREATE INDEX IF NOT EXISTS idx_provisions_section ON provisions(section);
CREATE INDEX IF NOT EXISTS idx_parties_case ON parties(case_id);
CREATE INDEX IF NOT EXISTS idx_case_judges_case ON case_judges(case_id);
CREATE INDEX IF NOT EXISTS idx_case_judges_judge ON case_judges(judge_id);
CREATE INDEX IF NOT EXISTS idx_scraper_jobs_court ON scraper_jobs(court_id, from_date, to_date);
CREATE INDEX IF NOT EXISTS idx_filter_options_filter_id ON filter_options(filter_id);

-- 15. TRIGRAM INDEXES FOR ILIKE '%term%' FREE-TEXT SEARCH (api-backend search)
CREATE INDEX IF NOT EXISTS idx_trgm_cases_note_ai ON cases USING gin (case_note_ai gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_trgm_cases_case_number ON cases USING gin (case_number gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_trgm_cases_cnr ON cases USING gin (cnr gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_trgm_cases_neutral_citation ON cases USING gin (neutral_citation gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_trgm_cases_diary_number ON cases USING gin (diary_number gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_trgm_parties_name ON parties USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_trgm_judge_master_name ON judge_master USING gin (canonical_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_trgm_advocates_name ON advocates USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_trgm_act_master_name ON act_master USING gin (canonical_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_trgm_provisions_raw_act ON provisions USING gin (raw_act_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_trgm_provisions_section ON provisions USING gin (section gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_trgm_citations_raw_text ON citations USING gin (raw_citation_text gin_trgm_ops);
"""


# ============================================================
# SEED MASTER ACTS & SEED COURTS
# ============================================================

INITIAL_COURTS = [
    ("SUPREME_COURT_OF_INDIA", "Supreme Court of India", "APEX", "DL", "Delhi", "New Delhi"),
    ("ALLAHABAD_HIGH_COURT", "Allahabad High Court", "HIGH_COURT", "UP", "Uttar Pradesh", "Allahabad"),
    ("BOMBAY_HIGH_COURT", "Bombay High Court", "HIGH_COURT", "MH", "Maharashtra", "Mumbai"),
    ("CALCUTTA_HIGH_COURT", "Calcutta High Court", "HIGH_COURT", "WB", "West Bengal", "Kolkata"),
    ("DELHI_HIGH_COURT", "Delhi High Court", "HIGH_COURT", "DL", "Delhi", "New Delhi"),
    ("GUJARAT_HIGH_COURT", "Gujarat High Court", "HIGH_COURT", "GJ", "Gujarat", "Ahmedabad"),
    ("MADRAS_HIGH_COURT", "Madras High Court", "HIGH_COURT", "TN", "Tamil Nadu", "Chennai"),
    ("MADHYA_PRADESH_HIGH_COURT", "Madhya Pradesh High Court", "HIGH_COURT", "MP", "Madhya Pradesh", "Jabalpur"),
    ("KARNATAKA_HIGH_COURT", "Karnataka High Court", "HIGH_COURT", "KA", "Karnataka", "Bengaluru"),
    ("KERALA_HIGH_COURT", "Kerala High Court", "HIGH_COURT", "KL", "Kerala", "Ernakulam"),
    ("PUNJAB_AND_HARYANA_HIGH_COURT", "Punjab and Haryana High Court", "HIGH_COURT", "PB", "Punjab & Haryana", "Chandigarh"),
    ("RAJASTHAN_HIGH_COURT", "Rajasthan High Court", "HIGH_COURT", "RJ", "Rajasthan", "Jodhpur"),
]


def seed_canonical_acts(conn):
    """Populates act_master and act_aliases from the normalizer registry."""
    with conn.cursor() as cur:
        for act in normalizer.CANONICAL_ACTS_SEED:
            cur.execute("""
                INSERT INTO act_master (canonical_name, short_code, year, jurisdiction)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (canonical_name) DO UPDATE
                SET short_code = EXCLUDED.short_code,
                    year = EXCLUDED.year
                RETURNING id;
            """, (act["canonical_name"], act["short_code"], act["year"], act["jurisdiction"]))
            act_id = cur.fetchone()[0]

            for alias in act["aliases"]:
                cur.execute("""
                    INSERT INTO act_aliases (act_id, alias_pattern)
                    VALUES (%s, %s)
                    ON CONFLICT (alias_pattern) DO NOTHING;
                """, (act_id, alias.lower().strip()))

        conn.commit()


INITIAL_FILTER_DEFINITIONS = [
    # (key, label, type, selection_mode, query_key, data_source, is_searchable, display_order)
    ("court", "Court", "select", "multi", "court_id", "database", False, 0),
    ("treatment_status", "Treatment Status", "select", "multi", None, "database", False, 1),
    ("judge", "Judge / Bench", "select", "multi", None, "database", True, 2),
    ("act", "Act / Law", "select", "multi", None, "database", True, 3),
    ("judgment_year", "Judgment Year", "select", "multi", None, "database", False, 4),
]

INITIAL_SEARCH_FIELD_DEFINITIONS = [
    # (key, label, placeholder, combinator, display_order)
    ("all", "All of these words", "Search items...", "AND", 0),
    ("any", "Any of these words", "Search items...", "OR", 1),
    ("exact", "Exactly this phrase", "Search items...", "PHRASE", 2),
    ("none", "None of these words", "Search items...", "NOT", 3),
    ("text", "These words", "Search items...", "AND", 4),
]


def seed_filter_and_search_definitions(conn):
    """
    Seeds the 5 filter definitions and 5 search-field definitions that reproduce
    today's hardcoded behavior. ON CONFLICT (key) DO NOTHING so this is a no-op
    against an already-seeded DB and never clobbers admin edits.
    """
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO filter_definitions (key, label, type, selection_mode, query_key, data_source, is_searchable, display_order)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (key) DO NOTHING;
        """, INITIAL_FILTER_DEFINITIONS)

        cur.executemany("""
            INSERT INTO search_field_definitions (key, label, placeholder, combinator, display_order)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (key) DO NOTHING;
        """, INITIAL_SEARCH_FIELD_DEFINITIONS)

        conn.commit()


def init_database(db_url: Optional[str] = None, drop_existing: bool = False):
    """Initializes the production database schema and seeds master data if not present."""
    conn = get_connection(db_url)
    try:
        with conn.cursor() as cur:
            if drop_existing:
                print("🧹 Resetting old schema tables for clean migration...")
                cur.execute("""
                    DROP TABLE IF EXISTS citations, case_articles, provisions, case_advocates,
                    advocates, case_judges, parties, case_subjects, cases, act_aliases,
                    act_master, judge_aliases, judge_master, scraper_jobs,
                    filter_options, filter_definitions, search_field_definitions CASCADE;
                """)
                conn.commit()

            print("🚀 Verifying/creating enterprise PostgreSQL schema with UUIDs & Citations...")
            cur.execute(CREATE_TABLES_SQL)

            # Incremental column additions for tables that may already exist from an
            # earlier version of the schema (CREATE TABLE IF NOT EXISTS above won't
            # retrofit new columns onto an already-created table).
            cur.execute("ALTER TABLE filter_definitions ADD COLUMN IF NOT EXISTS is_searchable BOOLEAN NOT NULL DEFAULT FALSE;")
            conn.commit()

            # Seed courts
            cur.executemany("""
                INSERT INTO courts (court_id, name, type, state_code, state_name, bench_seat)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (court_id) DO NOTHING;
            """, INITIAL_COURTS)
            conn.commit()

        # Seed acts, filters, and search fields
        seed_canonical_acts(conn)
        seed_filter_and_search_definitions(conn)
        print("✅ Database verified and initialized successfully with master Acts, Aliases, Courts, Filters, and Search Fields!")
    finally:
        conn.close()


# ============================================================
# SCRAPER JOB TRACKING (written by scraper-backend, read by both)
# ============================================================

def log_scraper_job_start(court_id: str, from_date: str = None, to_date: str = None, notes: str = None, db_url: Optional[str] = None) -> Optional[str]:
    """Records the beginning of a scraper job in PostgreSQL and returns job_id UUID."""
    try:
        from_d = parse_date(from_date) if from_date else None
        to_d = parse_date(to_date) if to_date else None
        initial_log = [f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🚀 Scraper job started for court {court_id} ({from_date} to {to_date})"]

        conn = get_connection(db_url)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scraper_jobs (court_id, from_date, to_date, status, logs, notes)
                VALUES (%s, %s, %s, 'RUNNING', %s::jsonb, %s)
                RETURNING job_id;
            """, (court_id, from_d, to_d, json.dumps(initial_log), notes))
            job_id = str(cur.fetchone()[0])
            conn.commit()
        conn.close()
        return job_id
    except Exception as e:
        print(f"⚠️ Could not log scraper job start: {e}")
        return None


def append_scraper_job_log(job_id: str, message: str, db_url: Optional[str] = None):
    """Appends a timestamped log line to the scraper job in PostgreSQL."""
    if not job_id:
        return
    try:
        ts = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{ts}] {message}"
        conn = get_connection(db_url)
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE scraper_jobs
                SET logs = COALESCE(logs, '[]'::jsonb) || %s::jsonb
                WHERE job_id = %s::uuid;
            """, (json.dumps([log_entry]), job_id))
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Could not append log to job {job_id}: {e}")


def update_scraper_job_progress(job_id: str, total_found: int = 0, new_scraped: int = 0, skipped: int = 0, db_url: Optional[str] = None):
    """Updates real-time progress counters in scraper_jobs."""
    if not job_id:
        return
    try:
        conn = get_connection(db_url)
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE scraper_jobs
                SET total_cases_found = %s,
                    new_cases_scraped = %s,
                    skipped_cases = %s
                WHERE job_id = %s::uuid;
            """, (total_found, new_scraped, skipped, job_id))
            conn.commit()
        conn.close()
    except Exception as e:
        pass


def log_scraper_job_finish(
    job_id: str,
    status: str = "COMPLETED",
    total_found: int = 0,
    new_scraped: int = 0,
    skipped: int = 0,
    bronze_blob_url: Optional[str] = None,
    silver_blob_url: Optional[str] = None,
    error_message: Optional[str] = None,
    notes: Optional[str] = None,
    db_url: Optional[str] = None
):
    """Updates scraper_jobs with completion status, counts, blob URLs, and optional error."""
    if not job_id:
        return
    try:
        status_log = f"[{datetime.now().strftime('%H:%M:%S')}] 🏁 Job {status}: {new_scraped} new scraped, {skipped} skipped duplicates."
        if error_message:
            status_log += f" Error: {error_message}"

        conn = get_connection(db_url)
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE scraper_jobs
                SET status = %s,
                    total_cases_found = %s,
                    new_cases_scraped = %s,
                    skipped_cases = %s,
                    bronze_blob_url = COALESCE(%s, bronze_blob_url),
                    silver_blob_url = COALESCE(%s, silver_blob_url),
                    error_message = %s,
                    completed_at = CURRENT_TIMESTAMP,
                    logs = COALESCE(logs, '[]'::jsonb) || %s::jsonb,
                    notes = COALESCE(%s, notes)
                WHERE job_id = %s::uuid;
            """, (status, total_found, new_scraped, skipped, bronze_blob_url, silver_blob_url, error_message, json.dumps([status_log]), notes, job_id))
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Could not log scraper job finish: {e}")


def get_scraper_job(job_id: str, db_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieves a single scraper job by UUID."""
    if not job_id:
        return None
    try:
        conn = get_connection(db_url)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    j.job_id, j.court_id, c.name AS court_name,
                    j.from_date, j.to_date, j.status,
                    j.total_cases_found, j.new_cases_scraped, j.skipped_cases,
                    j.bronze_blob_url, j.silver_blob_url, j.error_message,
                    j.logs, j.started_at, j.completed_at, j.notes
                FROM scraper_jobs j
                LEFT JOIN courts c ON j.court_id = c.court_id
                WHERE j.job_id = %s::uuid;
            """, (job_id,))
            row = cur.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"⚠️ Could not get scraper job {job_id}: {e}")
        return None


def list_scraper_jobs(limit: int = 50, db_url: Optional[str] = None) -> List[Dict[str, Any]]:
    """Lists recent scraper jobs for admin dashboard history."""
    try:
        conn = get_connection(db_url)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    j.job_id, j.court_id, c.name AS court_name,
                    j.from_date, j.to_date, j.status,
                    j.total_cases_found, j.new_cases_scraped, j.skipped_cases,
                    j.bronze_blob_url, j.silver_blob_url, j.error_message,
                    j.started_at, j.completed_at
                FROM scraper_jobs j
                LEFT JOIN courts c ON j.court_id = c.court_id
                ORDER BY j.started_at DESC
                LIMIT %s;
            """, (limit,))
            rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"⚠️ Could not list scraper jobs: {e}")
        return []


# ============================================================
# CLI INTERFACE (schema-only — ingestion CLI lives in scraper-backend/ingestion.py)
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Legal Database Schema Manager")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    init_parser = subparsers.add_parser("init", help="Initialize schema and seed master tables")
    init_parser.add_argument("--drop", action="store_true", help="Drop existing tables before recreating")

    args = parser.parse_args()

    if args.command == "init":
        init_database(drop_existing=args.drop)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
