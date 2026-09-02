"""
Enterprise PostgreSQL Database Manager & Ingestion Pipeline for Legal Intelligence
----------------------------------------------------------------------------------
This module defines the production schema using Global UUIDs, Master Judge & Act
tables with alias mapping, the Citator & Citation Graph, and handles normalized
ingestion of scraper JSON data without storing binary PDFs into PostgreSQL.
"""

import os
import sys
import re
import json
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Union

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
    from psycopg2.extras import execute_values, RealDictCursor
except ImportError:
    psycopg2 = None

import normalizer
import citator
import azure_blob


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
    """Establishes connection to PostgreSQL database."""
    if psycopg2 is None:
        raise ImportError("psycopg2 module is missing. Run: pip install psycopg2-binary")
    target_url = db_url or get_db_url()
    return psycopg2.connect(target_url)


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

-- 12. INDEXES FOR LIGHTNING FAST RETRIEVAL & DEDUPLICATION
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
                    act_master, judge_aliases, judge_master, scraper_jobs CASCADE;
                """)
                conn.commit()

            print("🚀 Verifying/creating enterprise PostgreSQL schema with UUIDs & Citations...")
            cur.execute(CREATE_TABLES_SQL)
            
            # Seed courts
            cur.executemany("""
                INSERT INTO courts (court_id, name, type, state_code, state_name, bench_seat)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (court_id) DO NOTHING;
            """, INITIAL_COURTS)
            conn.commit()

        # Seed acts
        seed_canonical_acts(conn)
        print("✅ Database verified and initialized successfully with master Acts, Aliases, and Courts!")
    finally:
        conn.close()


# ============================================================
# MASTER LOOKUP HELPERS (Judges, Acts, Advocates)
# ============================================================

def get_or_create_judge(cur, raw_name: str) -> str:
    """Normalizes judge name and resolves/creates record in judge_master."""
    cleaned = normalizer.clean_judge_name(raw_name)
    if not cleaned:
        cleaned = "UNKNOWN JUDGE"

    # Check alias first
    cur.execute("""
        SELECT judge_id FROM judge_aliases WHERE alias_name = %s;
    """, (cleaned.lower(),))
    row = cur.fetchone()
    if row:
        return row[0]

    # Insert into judge_master
    cur.execute("""
        INSERT INTO judge_master (canonical_name)
        VALUES (%s)
        ON CONFLICT (canonical_name) DO UPDATE 
        SET canonical_name = EXCLUDED.canonical_name
        RETURNING id;
    """, (cleaned,))
    judge_id = cur.fetchone()[0]

    # Save alias
    cur.execute("""
        INSERT INTO judge_aliases (judge_id, alias_name)
        VALUES (%s, %s)
        ON CONFLICT (alias_name) DO NOTHING;
    """, (judge_id, cleaned.lower()))

    return judge_id


def get_or_create_act(cur, raw_act_name: str) -> Tuple[Optional[str], str]:
    """Normalizes act name and resolves/creates record in act_master."""
    canonical_act, cleaned_raw = normalizer.normalize_act_name(raw_act_name)
    
    # Check alias
    cur.execute("""
        SELECT act_id FROM act_aliases WHERE alias_pattern = %s;
    """, (cleaned_raw.lower(),))
    row = cur.fetchone()
    if row:
        return (row[0], canonical_act)

    # Check canonical directly
    cur.execute("""
        INSERT INTO act_master (canonical_name)
        VALUES (%s)
        ON CONFLICT (canonical_name) DO UPDATE 
        SET canonical_name = EXCLUDED.canonical_name
        RETURNING id;
    """, (canonical_act,))
    act_id = cur.fetchone()[0]

    cur.execute("""
        INSERT INTO act_aliases (act_id, alias_pattern)
        VALUES (%s, %s)
        ON CONFLICT (alias_pattern) DO NOTHING;
    """, (act_id, cleaned_raw.lower()))

    return (act_id, canonical_act)


def get_or_create_advocate(cur, raw_adv_name: str) -> str:
    """Normalizes advocate name and inserts into advocates table."""
    cleaned = normalizer.clean_advocate_name(raw_adv_name)
    if not cleaned:
        cleaned = "UNKNOWN ADVOCATE"

    cur.execute("""
        INSERT INTO advocates (name)
        VALUES (%s)
        ON CONFLICT (name) DO UPDATE 
        SET name = EXCLUDED.name
        RETURNING id;
    """, (cleaned,))
    return cur.fetchone()[0]


# ============================================================
# DATA INGESTION PIPELINE
# ============================================================

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


def ingest_case_metadata(cur, item: Dict[str, Any], default_court: str = "SUPREME_COURT_OF_INDIA", upload_to_blob: bool = False) -> str:
    """
    Ingests a single scraped metadata item into the enterprise PostgreSQL schema.
    Extracts data accurately from both nested enterprise JSON and flat JSON formats.
    Optionally uploads physical PDFs to Azure Blob Storage if configured.
    """
    # 1. Extract Court
    court_val = item.get("court")
    if isinstance(court_val, dict):
        court_id = court_val.get("court_id") or default_court
    elif isinstance(court_val, str) and court_val.strip():
        court_id = "SUPREME_COURT_OF_INDIA" if "supreme" in court_val.lower() else court_val
    else:
        court_id = item.get("court_id") or default_court

    if court_id == "SCIN":
        court_id = "SUPREME_COURT_OF_INDIA"

    # 2. Extract Case Info
    case_info = item.get("case") if isinstance(item.get("case"), dict) else {}
    diary_no = case_info.get("case_id") or case_info.get("diary_number") or item.get("diary_number")
    
    case_no_obj = case_info.get("case_number") or item.get("case_number")
    if isinstance(case_no_obj, dict):
        case_no = case_no_obj.get("display") or case_no_obj.get("number") or ""
    else:
        case_no = str(case_no_obj or "")

    cnr = case_info.get("cnr") or item.get("cnr")
    case_category = case_info.get("case_category") or item.get("case_category")
    reg_date = parse_date(case_info.get("registration_date") or item.get("registration_date"))
    
    decision_info = case_info.get("decision") if isinstance(case_info.get("decision"), dict) else {}
    judg_date = parse_date(
        case_info.get("date_of_judgment") or 
        decision_info.get("date_of_judgment") or 
        item.get("judgment_date") or 
        item.get("decision_date")
    )
    
    cit_dict = item.get("citations")
    neutral_citation = cit_dict.get("neutral_citation") if isinstance(cit_dict, dict) else item.get("neutral_citation")
    disposal_nature = decision_info.get("disposal_nature") or item.get("disposal_nature")
    result_text = decision_info.get("result_text") or item.get("result")

    # 3. Extract Document & Quality Info
    doc_info = item.get("document") if isinstance(item.get("document"), dict) else {}
    doc_type = doc_info.get("document_type") or item.get("document_type") or "Caselaws"
    language = doc_info.get("language") or item.get("language") or "en"
    quality_info = item.get("quality") if isinstance(item.get("quality"), dict) else {}
    confidence = quality_info.get("confidence_score") or item.get("confidence_score")
    case_note = doc_info.get("case_note_ai") or item.get("case_note_ai") or ""

    # 4. Extract Overruled & Reporting
    overruled_info = item.get("overruled")
    if isinstance(overruled_info, dict):
        overruled = overruled_info.get("present", False)
    else:
        overruled = bool(overruled_info)

    reporting_info = item.get("reporting") if isinstance(item.get("reporting"), dict) else {}
    is_reported = reporting_info.get("is_reported", False) or bool(item.get("is_reported", False))
    reporting_status = reporting_info.get("status") or item.get("reporting_status")
    reporting_source = reporting_info.get("source") or item.get("reporting_source")

    # 5. Extract Source & PDF Info
    source_info = item.get("source") if isinstance(item.get("source"), dict) else {}
    pdf_info = source_info.get("pdf") if isinstance(source_info.get("pdf"), dict) else {}
    scraper_src = source_info.get("scraper") if isinstance(source_info.get("scraper"), dict) else {}
    pdf_path = pdf_info.get("pdf_path") or item.get("pdf_path")
    pdf_url = pdf_info.get("pdf_url") or item.get("pdf_url")
    source_page = scraper_src.get("source_page") or item.get("source_page")

    # Optional: Upload PDF to Azure Blob Storage if requested and configured
    if upload_to_blob and azure_blob.is_azure_blob_configured() and pdf_path:
        local_p = Path(pdf_path)
        if not local_p.exists() or not local_p.is_file():
            base_dir = Path(__file__).resolve().parent
            fname = Path(pdf_path.replace("\\", "/")).name
            candidate_paths = [
                base_dir / "app" / "SUPREME_COURT_OF_INDIA_SCRAPER" / "pdf" / fname,
                base_dir / "app" / "pdf" / fname,
                base_dir.parent / "backend" / "app" / "SUPREME_COURT_OF_INDIA_SCRAPER" / "pdf" / fname,
            ]
            for cand in candidate_paths:
                if cand.exists() and cand.is_file():
                    local_p = cand
                    break

        if local_p.exists() and local_p.is_file():
            court_code = "SCIN" if "SUPREME" in court_id.upper() else court_id[:4].upper()
            blob_url = azure_blob.upload_pdf_to_blob(
                local_p,
                court_code=court_code,
                diary_number=diary_no,
                case_number=case_no,
                judgment_date=judg_date
            )
            if blob_url:
                pdf_url = blob_url

    # Check if case already exists by diary number and judgment date
    existing_case_id = None
    if diary_no:
        cur.execute("""
            SELECT id FROM cases 
            WHERE diary_number = %s AND (judgment_date = %s OR judgment_date IS NULL);
        """, (diary_no, judg_date))
        row = cur.fetchone()
        if row:
            existing_case_id = row[0]

    # Insert or update cases record
    if existing_case_id:
        case_id = existing_case_id
        cur.execute("""
            UPDATE cases SET
                court_id = %s,
                case_id_code = %s,
                cnr = %s,
                case_number = %s,
                case_category = %s,
                registration_date = %s,
                judgment_date = %s,
                neutral_citation = %s,
                disposal_nature = %s,
                result_text = %s,
                language = %s,
                document_type = %s,
                confidence_score = %s,
                case_note_ai = %s,
                overruled = %s,
                is_reported = %s,
                reporting_status = %s,
                reporting_source = %s,
                pdf_path = %s,
                pdf_url = %s,
                source_page = %s
            WHERE id = %s;
        """, (
            court_id,
            diary_no,
            cnr,
            case_no,
            case_category,
            reg_date,
            judg_date,
            neutral_citation,
            disposal_nature,
            result_text,
            language,
            doc_type,
            confidence,
            case_note,
            overruled,
            is_reported,
            reporting_status,
            reporting_source,
            pdf_path,
            pdf_url,
            source_page,
            case_id
        ))
    else:
        cur.execute("""
            INSERT INTO cases (
                court_id, diary_number, case_id_code, cnr, case_number, case_category,
                registration_date, judgment_date, neutral_citation, disposal_nature,
                result_text, language, document_type, confidence_score, case_note_ai,
                treatment_status, overruled, is_reported, reporting_status,
                reporting_source, pdf_path, pdf_url, source_page
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                'GOOD_LAW', %s, %s, %s,
                %s, %s, %s, %s
            ) RETURNING id;
        """, (
            court_id,
            diary_no,
            diary_no,
            cnr,
            case_no,
            case_category,
            reg_date,
            judg_date,
            neutral_citation,
            disposal_nature,
            result_text,
            language,
            doc_type,
            confidence,
            case_note,
            overruled,
            is_reported,
            reporting_status,
            reporting_source,
            pdf_path,
            pdf_url,
            source_page
        ))
        case_id = cur.fetchone()[0]

    # Clean existing relations for re-ingestion idempotence
    cur.execute("DELETE FROM parties WHERE case_id = %s;", (case_id,))
    cur.execute("DELETE FROM case_judges WHERE case_id = %s;", (case_id,))
    cur.execute("DELETE FROM case_advocates WHERE case_id = %s;", (case_id,))
    cur.execute("DELETE FROM provisions WHERE case_id = %s;", (case_id,))
    cur.execute("DELETE FROM case_articles WHERE case_id = %s;", (case_id,))
    cur.execute("DELETE FROM citations WHERE citing_case_id = %s;", (case_id,))

    # 1. PARTIES
    parties_data = case_info.get("parties") or item.get("parties")
    if isinstance(parties_data, list):
        for p in parties_data:
            if isinstance(p, dict):
                p_name = p.get("name")
                p_role = p.get("role", "PETITIONER")
                p_type = p.get("party_type", "INDIVIDUAL")
                if p_name:
                    cur.execute("INSERT INTO parties (case_id, name, role, party_type) VALUES (%s, %s, %s, %s);",
                                (case_id, normalizer.clean_party_name(p_name), p_role, p_type))
    elif isinstance(parties_data, dict):
        pet = parties_data.get("petitioner")
        resp = parties_data.get("respondent")
        if pet:
            cur.execute("INSERT INTO parties (case_id, name, role, party_type) VALUES (%s, %s, 'PETITIONER', 'INDIVIDUAL');",
                        (case_id, normalizer.clean_party_name(pet)))
        if resp:
            cur.execute("INSERT INTO parties (case_id, name, role, party_type) VALUES (%s, %s, 'RESPONDENT', 'INDIVIDUAL');",
                        (case_id, normalizer.clean_party_name(resp)))
    else:
        pet = item.get("petitioner")
        resp = item.get("respondent")
        if pet:
            cur.execute("INSERT INTO parties (case_id, name, role, party_type) VALUES (%s, %s, 'PETITIONER', 'INDIVIDUAL');",
                        (case_id, normalizer.clean_party_name(pet)))
        if resp:
            cur.execute("INSERT INTO parties (case_id, name, role, party_type) VALUES (%s, %s, 'RESPONDENT', 'INDIVIDUAL');",
                        (case_id, normalizer.clean_party_name(resp)))
        if not pet and not resp and item.get("party_name"):
            cur.execute("INSERT INTO parties (case_id, name, role, party_type) VALUES (%s, %s, 'PETITIONER', 'INDIVIDUAL');",
                        (case_id, normalizer.clean_party_name(item.get("party_name"))))

    # 2. JUDGES (Normalized & Split)
    coram = case_info.get("coram") if isinstance(case_info.get("coram"), dict) else {}
    judges_data = coram.get("judges") or (case_info.get("bench", {}).get("judges") if isinstance(case_info.get("bench"), dict) else None) or item.get("judges") or item.get("judge") or item.get("bench")

    if isinstance(judges_data, str):
        judges_data = [j.strip() for j in judges_data.split(",") if j.strip()]

    if isinstance(judges_data, list):
        for raw_j in judges_data:
            if not raw_j:
                continue
            for clean_name, role in normalizer.split_judge_bench(str(raw_j)):
                judge_id = get_or_create_judge(cur, clean_name)
                cur.execute("""
                    INSERT INTO case_judges (case_id, judge_id, role)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (case_id, judge_id) DO NOTHING;
                """, (case_id, judge_id, role))

    # 3. ADVOCATES
    advocates = case_info.get("advocates") or item.get("advocates") or item.get("advocate")
    if isinstance(advocates, str):
        advocates = [a.strip() for a in advocates.split(",") if a.strip()]

    if isinstance(advocates, list):
        for adv in advocates:
            if not adv:
                continue
            adv_name = adv.get("name") if isinstance(adv, dict) else str(adv)
            if adv_name:
                adv_id = get_or_create_advocate(cur, adv_name)
                cur.execute("""
                    INSERT INTO case_advocates (case_id, advocate_id, party_role)
                    VALUES (%s, %s, 'ADVOCATE')
                    ON CONFLICT (case_id, advocate_id) DO NOTHING;
                """, (case_id, adv_id))

    # 4. PROVISIONS & ACTS (Normalized)
    legal_info = item.get("legal_information", {})
    provisions_data = legal_info.get("provisions") or item.get("provisions", [])
    if isinstance(provisions_data, list):
        for prov in provisions_data:
            if isinstance(prov, dict):
                raw_act = prov.get("act_name", "")
                section = prov.get("section")
                norm = normalizer.normalize_provision(f"{raw_act} s.{section}" if section else raw_act)
            else:
                norm = normalizer.normalize_provision(str(prov))
            
            act_id, canonical_act = get_or_create_act(cur, norm["canonical_act"])
            cur.execute("""
                INSERT INTO provisions (case_id, act_id, raw_act_name, section, provision_full)
                VALUES (%s, %s, %s, %s, %s);
            """, (case_id, act_id, norm["raw_act"], norm["section"], norm["full_text"]))

    # 5. CONSTITUTIONAL ARTICLES
    articles_data = legal_info.get("constitutional_articles") or item.get("articles", [])
    if isinstance(articles_data, list):
        for art in articles_data:
            if not art:
                continue
            cur.execute("""
                INSERT INTO case_articles (case_id, article)
                VALUES (%s, %s)
                ON CONFLICT (case_id, article) DO NOTHING;
            """, (case_id, str(art).strip()))

    # 6. CITATION EXTRACTION (Citator Engine)
    extracted_citations = citator.extract_citations_from_text(case_note)
    for cit in extracted_citations:
        cur.execute("""
            INSERT INTO citations (
                citing_case_id, raw_citation_text, reporter_type,
                treatment_type, confidence_score, context_snippet
            ) VALUES (%s, %s, %s, %s, %s, %s);
        """, (
            case_id,
            cit["raw_citation_text"],
            cit["reporter_type"],
            cit["treatment_type"],
            cit["confidence_score"],
            cit["context_snippet"]
        ))

    return case_id



def import_json_file(
    json_path: str, 
    court_id: str = "SUPREME_COURT_OF_INDIA", 
    default_court: Optional[str] = None,
    db_url: Optional[str] = None,
    upload_to_blob: bool = False
) -> int:
    """
    Imports JSON metadata file into PostgreSQL using normalized pipeline.
    Optionally archives the JSON and syncs PDFs directly to Azure Blob Storage.
    Returns the total number of successfully ingested cases.
    """
    p = Path(json_path)
    if not p.exists():
        print(f"❌ File not found: {json_path}")
        return 0

    effective_court = default_court or court_id or "SUPREME_COURT_OF_INDIA"

    with open(p, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    if isinstance(raw_data, dict):
        if "supreme_court" in raw_data:
            data = raw_data["supreme_court"]
        elif "Supreme Court of India" in raw_data:
            data = raw_data["Supreme Court of India"]
        else:
            first_val = next(iter(raw_data.values()))
            data = first_val if isinstance(first_val, list) else list(raw_data.values())
    elif isinstance(raw_data, list):
        data = raw_data
    else:
        data = [raw_data]

    # Optional: Archive JSON to Azure Blob Storage (Bronze/Silver Layer)
    if upload_to_blob and azure_blob.is_azure_blob_configured():
        court_code = "SCIN" if "SUPREME" in effective_court.upper() else effective_court[:4].upper()
        layer = "Silver" if "metadata" in p.name.lower() else "Bronze"
        azure_blob.upload_json_to_blob(p, court_code=court_code, layer=layer, filename=p.name)

    conn = get_connection(db_url)
    success = 0
    try:
        print(f"📂 Processing {len(data)} cases from {p.name} (Azure Upload: {upload_to_blob})...")
        with conn.cursor() as cur:
            for item in data:
                try:
                    ingest_case_metadata(cur, item, default_court=effective_court, upload_to_blob=upload_to_blob)
                    success += 1
                except Exception as e:
                    print(f"⚠️ Error ingesting case {item.get('diary_number')}: {e}")
            conn.commit()

        # Run treatment recompute job
        print("🔄 Recomputing Citator Treatment Graph...")
        stats = citator.recompute_all_treatment_statuses(conn)
        print(f"✅ Ingestion complete: {success}/{len(data)} cases ingested.")
        print(f"📊 Citator Statuses: Good Law={stats['GOOD_LAW']} | Overruled={stats['OVERRULED']} | Doubted={stats['DOUBTED']} | Distinguished={stats['DISTINGUISHED']}")
        return success
    finally:
        conn.close()


def import_json_data(
    raw_data: Union[dict, list],
    default_court: str = "SUPREME_COURT_OF_INDIA",
    db_url: Optional[str] = None
) -> int:
    """
    Direct in-memory ingestion into PostgreSQL without saving any JSON file to local disk.
    """
    effective_court = default_court or "SUPREME_COURT_OF_INDIA"
    if isinstance(raw_data, dict):
        if "supreme_court" in raw_data:
            data = raw_data["supreme_court"]
        elif "Supreme Court of India" in raw_data:
            data = raw_data["Supreme Court of India"]
        else:
            first_val = next(iter(raw_data.values()))
            data = first_val if isinstance(first_val, list) else list(raw_data.values())
    elif isinstance(raw_data, list):
        data = raw_data
    else:
        data = [raw_data]

    conn = get_connection(db_url)
    success = 0
    try:
        with conn.cursor() as cur:
            for item in data:
                try:
                    ingest_case_metadata(cur, item, default_court=effective_court, upload_to_blob=False)
                    success += 1
                except Exception as e:
                    print(f"⚠️ Error ingesting case {item.get('diary_number')}: {e}")
            conn.commit()

        # Run treatment recompute job
        citator.recompute_all_treatment_statuses(conn)
        return success
    finally:
        conn.close()


def get_existing_cases_keys(court_id: str = "SUPREME_COURT_OF_INDIA", db_url: Optional[str] = None) -> set:
    """
    Returns a fast set of existing keys (diary_number, diary_date, neutral_citation)
    from PostgreSQL for O(1) duplicate skipping during scraping.
    """
    keys = set()
    try:
        conn = get_connection(db_url)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT diary_number, judgment_date, neutral_citation, case_number 
                FROM cases 
                WHERE court_id = %s OR court_id = 'SCIN';
            """, (court_id,))
            for r in cur.fetchall():
                d_no = str(r[0] or "").strip()
                j_date = str(r[1] or "").strip()
                n_cit = str(r[2] or "").strip()
                c_no = str(r[3] or "").strip()
                if d_no:
                    keys.add(d_no)
                    if j_date:
                        keys.add(f"{d_no}_{j_date}")
                    if c_no:
                        keys.add(f"{d_no}_{c_no}")
                if n_cit:
                    keys.add(n_cit)
        conn.close()
    except Exception as e:
        print(f"⚠️ Warning querying existing cases keys: {e}")
    return keys


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
# CLI INTERFACE
# ============================================================

def query_sample_records(limit: int = 3, db_url: Optional[str] = None):
    """Prints sample normalized records with UUIDs, Citations, and Treatment Status."""
    conn = get_connection(db_url)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT 
                    c.id, c.diary_number, c.case_number, c.judgment_date,
                    c.treatment_status, c.overruled, c.is_reported, c.pdf_path, c.pdf_url,
                    c.case_note_ai,
                    COALESCE((SELECT json_agg(DISTINCT jm.canonical_name) FROM case_judges cj JOIN judge_master jm ON cj.judge_id = jm.id WHERE cj.case_id = c.id), '[]'::json) as judges,
                    COALESCE((SELECT json_agg(DISTINCT am.canonical_name) FROM provisions p JOIN act_master am ON p.act_id = am.id WHERE p.case_id = c.id), '[]'::json) as acts,
                    COALESCE((SELECT json_agg(DISTINCT cit.raw_citation_text) FROM citations cit WHERE cit.citing_case_id = c.id), '[]'::json) as citations
                FROM cases c
                ORDER BY c.judgment_date DESC NULLS LAST
                LIMIT %s;
            """, (limit,))
            rows = cur.fetchall()

            print("\n" + "="*80)
            print(f"SAMPLE ENTERPRISE POSTGRESQL RECORDS (Limit: {limit})")
            print("="*80)
            for r in rows:
                print(f"UUID: {r['id']} | Diary: {r['diary_number']} | Case: {r['case_number']}")
                print(f"  Date            : {r['judgment_date']}")
                print(f"  Treatment Status: {r['treatment_status']} | Overruled: {r['overruled']} | Reported: {r['is_reported']}")
                print(f"  Judges          : {', '.join(r['judges']) if r['judges'] else 'None'}")
                print(f"  Acts (Canonical): {', '.join(r['acts']) if r['acts'] else 'None'}")
                print(f"  Citations ({len(r['citations'])}): {', '.join(r['citations']) if r['citations'] else 'None'}")
                print(f"  AI Case Note    : {r['case_note_ai'][:120]}..." if r['case_note_ai'] else "  AI Case Note: None")
                print("-" * 80)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Enterprise Legal Database Manager & Citator Pipeline")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # init
    init_parser = subparsers.add_parser("init", help="Initialize schema and seed master tables")
    init_parser.add_argument("--drop", action="store_true", help="Drop existing tables before recreating")

    # import
    import_parser = subparsers.add_parser("import", help="Import scraped JSON metadata")
    import_parser.add_argument("file", help="Path to metadata JSON file")
    import_parser.add_argument("--court", default="SUPREME_COURT_OF_INDIA", help="Court ID")
    import_parser.add_argument("--upload-azure", action="store_true", help="Upload PDFs & raw JSON to Azure Blob Storage")

    # sync-blobs
    sync_parser = subparsers.add_parser("sync-blobs", help="Sync physical PDFs to Azure Blob Storage")
    sync_parser.add_argument("dir", help="Path to PDF directory")
    sync_parser.add_argument("--court", default="SCIN", help="Court code (e.g. SCIN, DHC, ALHC)")

    # recompute
    subparsers.add_parser("recompute-treatment", help="Run Citator treatment graph recompute")

    # query
    query_parser = subparsers.add_parser("query", help="Query sample cases")
    query_parser.add_argument("--limit", type=int, default=3, help="Number of records")

    args = parser.parse_args()

    if args.command == "init":
        init_database(drop_existing=args.drop)
    elif args.command == "import":
        import_json_file(args.file, court_id=args.court, upload_to_blob=args.upload_azure)
    elif args.command == "sync-blobs":
        azure_blob.sync_court_pdfs_to_blob(args.dir, court_code=args.court)
    elif args.command == "recompute-treatment":
        conn = get_connection()
        stats = citator.recompute_all_treatment_statuses(conn)
        print(f"✅ Treatment Recompute Complete: {stats}")
        conn.close()
    elif args.command == "query":
        query_sample_records(limit=args.limit)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
