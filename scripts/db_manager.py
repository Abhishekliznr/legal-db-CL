"""
PostgreSQL Database Manager and Data Ingestion Pipeline for Legal Scraper
-------------------------------------------------------------------------
This module initializes the relational PostgreSQL schema and ingests scraped
case metadata, AI summaries, legal provisions, acts, articles, reporting info,
and categorization metadata without storing binary PDFs into PostgreSQL.
Only pdf_path and pdf_url are stored as references in the database.
"""

import os
import re
import json
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    psycopg2 = None


# ============================================================
# CONFIGURATION & CONNECTION
# ============================================================

def get_db_url() -> str:
    """Get Database connection string from environment variable DB_CONNECTION."""
    url = os.environ.get("DB_CONNECTION")
    if not url:
        url = "postgresql://postgres:1234@localhost:5432/legal_db"
    return url


def get_connection(db_url: Optional[str] = None):
    """Establishes connection to PostgreSQL database."""
    if psycopg2 is None:
        raise ImportError(
            "psycopg2 module is missing. Please run: pip install psycopg2-binary"
        )
    target_url = db_url or get_db_url()
    return psycopg2.connect(target_url)


# ============================================================
# SCHEMA DDL STATEMENTS
# ============================================================

CREATE_TABLES_SQL = """
-- 1. COURTS TABLE
CREATE TABLE IF NOT EXISTS courts (
    court_id VARCHAR(50) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    type VARCHAR(50),
    state_code VARCHAR(50),
    state_name VARCHAR(100),
    bench_seat VARCHAR(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. CASES TABLE
CREATE TABLE IF NOT EXISTS cases (
    id BIGSERIAL PRIMARY KEY,
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
    overruled BOOLEAN DEFAULT FALSE,
    is_reported BOOLEAN DEFAULT FALSE,
    reporting_status VARCHAR(50),
    reporting_source VARCHAR(100),
    pdf_path TEXT,                 -- Local filesystem path (e.g., C:\\...\\pdf\\*.pdf)
    pdf_url TEXT,                  -- Direct download/online URL
    source_page TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. PARTIES TABLE
CREATE TABLE IF NOT EXISTS parties (
    id BIGSERIAL PRIMARY KEY,
    case_id BIGINT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    role VARCHAR(50),            -- PETITIONER, RESPONDENT, COMPLAINANT, ACCUSED, etc.
    party_type VARCHAR(50),      -- INDIVIDUAL, STATE, CORPORATION, etc.
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 4. JUDGES TABLE
CREATE TABLE IF NOT EXISTS judges (
    id BIGSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

-- 5. CASE_JUDGES JUNCTION TABLE
CREATE TABLE IF NOT EXISTS case_judges (
    case_id BIGINT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    judge_id BIGINT NOT NULL REFERENCES judges(id) ON DELETE CASCADE,
    role VARCHAR(50) DEFAULT 'BENCH_MEMBER', -- PRESIDING, COMPANION, BENCH_MEMBER
    PRIMARY KEY (case_id, judge_id)
);

-- 6. ADVOCATES TABLE
CREATE TABLE IF NOT EXISTS advocates (
    id BIGSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    designation VARCHAR(100)
);

-- 7. CASE_ADVOCATES JUNCTION TABLE
CREATE TABLE IF NOT EXISTS case_advocates (
    case_id BIGINT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    advocate_id BIGINT NOT NULL REFERENCES advocates(id) ON DELETE CASCADE,
    party_role VARCHAR(50),      -- PETITIONER, RESPONDENT, ADVOCATE, etc.
    PRIMARY KEY (case_id, advocate_id)
);

-- 8. ACTS TABLE
CREATE TABLE IF NOT EXISTS acts (
    id BIGSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    short_name VARCHAR(100)
);

-- 9. PROVISIONS TABLE
CREATE TABLE IF NOT EXISTS provisions (
    id BIGSERIAL PRIMARY KEY,
    case_id BIGINT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    act_id BIGINT REFERENCES acts(id) ON DELETE SET NULL,
    act_name TEXT NOT NULL,
    section VARCHAR(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 10. CASE_ARTICLES TABLE
CREATE TABLE IF NOT EXISTS case_articles (
    case_id BIGINT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    article VARCHAR(100) NOT NULL,
    PRIMARY KEY (case_id, article)
);

-- 11. REPORTER_CITATIONS TABLE
CREATE TABLE IF NOT EXISTS reporter_citations (
    id BIGSERIAL PRIMARY KEY,
    case_id BIGINT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    reporter VARCHAR(100),
    citation VARCHAR(200),
    year INTEGER,
    volume VARCHAR(50),
    page VARCHAR(50),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 12. CASE_SUBJECTS TABLE
CREATE TABLE IF NOT EXISTS case_subjects (
    case_id BIGINT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    subject VARCHAR(150) NOT NULL,
    PRIMARY KEY (case_id, subject)
);

-- 13. CASE_INDUSTRIES TABLE
CREATE TABLE IF NOT EXISTS case_industries (
    case_id BIGINT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    industry VARCHAR(150) NOT NULL,
    PRIMARY KEY (case_id, industry)
);

-- 14. CASE_MINISTRIES TABLE
CREATE TABLE IF NOT EXISTS case_ministries (
    case_id BIGINT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    ministry VARCHAR(150) NOT NULL,
    PRIMARY KEY (case_id, ministry)
);

-- 15. CASE_DEPARTMENTS TABLE
CREATE TABLE IF NOT EXISTS case_departments (
    case_id BIGINT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    department VARCHAR(150) NOT NULL,
    PRIMARY KEY (case_id, department)
);

-- MIGRATION CHECKS FOR EXISTING TABLES
ALTER TABLE cases ADD COLUMN IF NOT EXISTS overruled BOOLEAN DEFAULT FALSE;
ALTER TABLE cases ADD COLUMN IF NOT EXISTS is_reported BOOLEAN DEFAULT FALSE;
ALTER TABLE cases ADD COLUMN IF NOT EXISTS reporting_status VARCHAR(50);
ALTER TABLE cases ADD COLUMN IF NOT EXISTS reporting_source VARCHAR(100);
ALTER TABLE provisions ADD COLUMN IF NOT EXISTS act_id BIGINT REFERENCES acts(id) ON DELETE SET NULL;

-- INDEXES FOR PERFORMANCE
CREATE INDEX IF NOT EXISTS idx_cases_court_id ON cases(court_id);
CREATE INDEX IF NOT EXISTS idx_cases_judgment_date ON cases(judgment_date);
CREATE INDEX IF NOT EXISTS idx_cases_cnr ON cases(cnr);
CREATE INDEX IF NOT EXISTS idx_cases_neutral_citation ON cases(neutral_citation);
CREATE INDEX IF NOT EXISTS idx_cases_diary_number ON cases(diary_number);
CREATE INDEX IF NOT EXISTS idx_cases_is_reported ON cases(is_reported);
CREATE INDEX IF NOT EXISTS idx_parties_case_id ON parties(case_id);
CREATE INDEX IF NOT EXISTS idx_provisions_case_id ON provisions(case_id);
CREATE INDEX IF NOT EXISTS idx_provisions_act_id ON provisions(act_id);
CREATE INDEX IF NOT EXISTS idx_case_articles_case_id ON case_articles(case_id);
"""

DROP_TABLES_SQL = """
DROP TABLE IF EXISTS case_departments, case_ministries, case_industries, case_subjects, reporter_citations, case_articles, provisions, case_advocates, advocates, case_judges, judges, parties, cases, courts, acts CASCADE;
"""


def init_db(db_url: Optional[str] = None, reset: bool = False):
    """Creates all database tables and indexes if they do not exist."""
    target_url = db_url or get_db_url()
    print("=" * 80)
    print("INITIALIZING POSTGRESQL SCHEMA")
    print(f"Target DB: {target_url}")
    if reset:
        print("Mode     : RESET (Dropping existing tables)")
    print("=" * 80)

    conn = get_connection(target_url)
    try:
        with conn.cursor() as cur:
            if reset:
                cur.execute(DROP_TABLES_SQL)
                print("[INFO] Dropped existing tables.")
            cur.execute(CREATE_TABLES_SQL)
        conn.commit()
        print("[SUCCESS] All tables and indexes created successfully.")
    except Exception as e:
        conn.rollback()
        print(f"[ERROR] Failed to initialize database: {e}")
        raise
    finally:
        conn.close()


# ============================================================
# HELPER PARSERS & STRING UTILS
# ============================================================

def parse_date(date_str: Optional[str]) -> Optional[str]:
    """Converts DD-MM-YYYY or YYYY-MM-DD strings to YYYY-MM-DD for PostgreSQL DATE column."""
    if not date_str or not isinstance(date_str, str):
        return None
    date_str = date_str.strip()
    if not date_str:
        return None

    formats = ["%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y"]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_bench_string(bench_str: Optional[str]) -> List[Dict[str, str]]:
    """Parses raw Supreme Court bench strings into judge objects with roles."""
    if not bench_str or not isinstance(bench_str, str):
        return []
    
    pattern = r"HON'BLE\s+(?:MR\.|MS\.|MRS\.|DR\.)?\s*JUSTICE\s+([A-Z\.\s]+?)(?=HON'BLE|$)"
    matches = re.findall(pattern, bench_str, re.IGNORECASE)
    results = []
    if matches:
        for idx, m in enumerate(matches):
            name = m.strip()
            if name:
                role = "PRESIDING" if idx == 0 else "COMPANION"
                results.append({"name": f"HON'BLE MR. JUSTICE {name}", "role": role})
    else:
        clean = re.sub(r"^HON'BLE\s*", "", bench_str.strip(), flags=re.IGNORECASE).strip()
        if clean:
            results.append({"name": clean, "role": "BENCH_MEMBER"})
    return results


def get_or_create_judge(cur, name: str) -> int:
    """Retrieves judge ID or inserts a new judge record."""
    clean_name = name.strip()
    cur.execute("SELECT id FROM judges WHERE name = %s;", (clean_name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO judges (name) VALUES (%s) RETURNING id;", (clean_name,))
    return cur.fetchone()[0]


def get_or_create_advocate(cur, name: str, designation: Optional[str] = None) -> int:
    """Retrieves advocate ID or inserts a new advocate record."""
    clean_name = name.strip()
    cur.execute("SELECT id FROM advocates WHERE name = %s;", (clean_name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO advocates (name, designation) VALUES (%s, %s) RETURNING id;",
        (clean_name, designation),
    )
    return cur.fetchone()[0]


def get_or_create_act(cur, name: str, short_name: Optional[str] = None) -> int:
    """Retrieves act ID or inserts a new act record."""
    clean_name = name.strip()
    cur.execute("SELECT id FROM acts WHERE name = %s;", (clean_name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO acts (name, short_name) VALUES (%s, %s) RETURNING id;",
        (clean_name, short_name),
    )
    return cur.fetchone()[0]


# ============================================================
# RECORD IMPORT LOGIC
# ============================================================

def process_record(
    cur,
    record: Dict[str, Any],
    default_court_id: str = "SCIN",
    raw_record_map: Optional[Dict[str, Any]] = None
) -> int:
    """
    Parses a single JSON case record (handling both standard enriched metadata format
    and raw scraper format) and inserts into PostgreSQL tables.
    Uses raw scraper fallbacks if enriched lists (judges/advocates) are empty.
    """

    is_enriched = "case" in record and "court" in record

    if is_enriched:
        court_info = record.get("court", {})
        case_info = record.get("case", {})
        doc_info = record.get("document", {})
        source_info = record.get("source", {})
        legal_info = record.get("legal_information", {})
        quality_info = record.get("quality", {})
        outcome_info = case_info.get("decision", {}) or record.get("outcome", {})
        overruled_info = record.get("overruled", {})
        reporting_info = record.get("reporting", {})

        court_id = court_info.get("court_id") or default_court_id
        court_name = court_info.get("name") or "Supreme Court of India"
        court_type = court_info.get("type") or "SUPREME_COURT"
        state_code = court_info.get("state_code") or "IN"
        state_name = court_info.get("state_name") or "India"
        bench_seat = court_info.get("bench_seat") or "New Delhi"

        diary_number = case_info.get("cnr") or case_info.get("case_id")
        case_id_code = case_info.get("case_id")
        cnr = case_info.get("cnr")
        case_num_obj = case_info.get("case_number", {})
        case_number = case_num_obj.get("display") if isinstance(case_num_obj, dict) else str(case_num_obj or "")
        case_category = case_info.get("case_category")
        registration_date = parse_date(case_info.get("registration_date"))
        judgment_date = parse_date(case_info.get("date_of_judgment") or outcome_info.get("date_of_judgment"))
        neutral_citation = case_info.get("neutral_citation") or record.get("citations", {}).get("neutral_citation")
        disposal_nature = outcome_info.get("disposal_nature")
        result_text = outcome_info.get("result_text")
        
        case_age_obj = case_info.get("case_age", {})
        case_age_days = case_age_obj.get("age_days") if isinstance(case_age_obj, dict) else None
        
        language = doc_info.get("language") or "en"
        document_type = doc_info.get("document_type") or doc_info.get("doc_type")
        confidence_score = quality_info.get("confidence_score")
        case_note_ai = doc_info.get("case_note_ai")

        overruled = bool(overruled_info.get("present", False)) if isinstance(overruled_info, dict) else False
        is_reported = bool(reporting_info.get("is_reported", False)) if isinstance(reporting_info, dict) else False
        reporting_status = reporting_info.get("status") if isinstance(reporting_info, dict) else None
        reporting_source = reporting_info.get("source") if isinstance(reporting_info, dict) else None

        pdf_obj = source_info.get("pdf", {})
        pdf_path = pdf_obj.get("pdf_path")
        pdf_url = pdf_obj.get("pdf_url") or source_info.get("scraper", {}).get("pdf_source")
        source_page = source_info.get("scraper", {}).get("source_page")

        parties_list = case_info.get("parties", [])
        judges_list = case_info.get("bench", {}).get("judges", []) or case_info.get("coram", {}).get("judges", [])
        advocates_list = case_info.get("advocates", []) or case_info.get("counsel", [])
        acts_list = legal_info.get("acts", [])
        provisions_list = legal_info.get("provisions", [])
        articles_list = legal_info.get("constitutional_articles", [])
        reporter_list = reporting_info.get("reporter_citations", [])

        subjects_list = legal_info.get("subject_matter") or legal_info.get("subject", [])
        industries_list = legal_info.get("industry", [])
        ministries_list = legal_info.get("ministry", [])
        departments_list = legal_info.get("department", [])

        # Fallback to Raw Scraper Record if judges or advocates are empty
        raw_rec = (raw_record_map or {}).get(diary_number) or (raw_record_map or {}).get(case_id_code)
        if not judges_list and raw_rec and raw_rec.get("bench"):
            judges_list = parse_bench_string(raw_rec.get("bench"))
        
        if not advocates_list and raw_rec and raw_rec.get("advocate"):
            advocates_list = [{"name": raw_rec.get("advocate").strip(), "for_party_role": "ADVOCATE"}]

    else:
        # Raw Scraper JSON format
        court_name = record.get("court") or "Supreme Court of India"
        court_id = default_court_id
        court_type = "SUPREME_COURT"
        state_code = "IN"
        state_name = "India"
        bench_seat = "New Delhi"

        diary_number = record.get("diary_number")
        case_id_code = diary_number
        cnr = diary_number
        case_number = record.get("case_number")
        case_category = None
        registration_date = None
        judgment_date = parse_date(record.get("decision_date"))
        neutral_citation = record.get("neutral_citation")
        disposal_nature = None
        result_text = None
        case_age_days = None
        language = "en"
        document_type = "Caselaws"
        confidence_score = None
        case_note_ai = None

        overruled = False
        is_reported = False
        reporting_status = None
        reporting_source = None

        pdf_path = record.get("pdf_path")
        pdf_url = record.get("pdf_url")
        source_page = record.get("source_page")

        # Build parties
        parties_list = []
        if record.get("petitioner"):
            parties_list.append({"name": record.get("petitioner"), "role": "PETITIONER", "party_type": "INDIVIDUAL"})
        if record.get("respondent"):
            parties_list.append({"name": record.get("respondent"), "role": "RESPONDENT", "party_type": "INDIVIDUAL"})

        # Build judges from bench string
        judges_list = parse_bench_string(record.get("bench") or record.get("judge"))

        # Build advocates
        advocates_list = []
        if record.get("advocate"):
            advocates_list = [{"name": record.get("advocate").strip(), "for_party_role": "ADVOCATE"}]

        acts_list = []
        provisions_list = []
        articles_list = []
        reporter_list = []
        subjects_list = []
        industries_list = []
        ministries_list = []
        departments_list = []

    # 1. Ensure Court exists
    cur.execute("""
        INSERT INTO courts (court_id, name, type, state_code, state_name, bench_seat)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (court_id) DO UPDATE SET
            name = EXCLUDED.name,
            type = EXCLUDED.type;
    """, (court_id, court_name, court_type, state_code, state_name, bench_seat))

    # 2. Check if Case already exists (by cnr or diary_number or neutral_citation)
    existing_id = None
    if cnr:
        cur.execute("SELECT id FROM cases WHERE cnr = %s;", (cnr,))
        row = cur.fetchone()
        if row:
            existing_id = row[0]

    if not existing_id and neutral_citation:
        cur.execute("SELECT id FROM cases WHERE neutral_citation = %s;", (neutral_citation,))
        row = cur.fetchone()
        if row:
            existing_id = row[0]

    if not existing_id and diary_number:
        cur.execute("SELECT id FROM cases WHERE diary_number = %s;", (diary_number,))
        row = cur.fetchone()
        if row:
            existing_id = row[0]

    if existing_id:
        # Update existing case record
        cur.execute("""
            UPDATE cases SET
                court_id = %s,
                diary_number = %s,
                case_id_code = %s,
                case_number = %s,
                case_category = %s,
                registration_date = %s,
                judgment_date = %s,
                neutral_citation = %s,
                disposal_nature = %s,
                result_text = %s,
                case_age_days = %s,
                language = %s,
                document_type = %s,
                confidence_score = %s,
                case_note_ai = COALESCE(%s, case_note_ai),
                overruled = %s,
                is_reported = %s,
                reporting_status = %s,
                reporting_source = %s,
                pdf_path = %s,
                pdf_url = %s,
                source_page = %s
            WHERE id = %s;
        """, (
            court_id, diary_number, case_id_code, case_number, case_category,
            registration_date, judgment_date, neutral_citation, disposal_nature,
            result_text, case_age_days, language, document_type, confidence_score,
            case_note_ai, overruled, is_reported, reporting_status, reporting_source,
            pdf_path, pdf_url, source_page, existing_id
        ))
        case_db_id = existing_id
    else:
        # Insert new case record
        cur.execute("""
            INSERT INTO cases (
                court_id, diary_number, case_id_code, cnr, case_number, case_category,
                registration_date, judgment_date, neutral_citation, disposal_nature,
                result_text, case_age_days, language, document_type, confidence_score,
                case_note_ai, overruled, is_reported, reporting_status, reporting_source,
                pdf_path, pdf_url, source_page
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s
            ) RETURNING id;
        """, (
            court_id, diary_number, case_id_code, cnr, case_number, case_category,
            registration_date, judgment_date, neutral_citation, disposal_nature,
            result_text, case_age_days, language, document_type, confidence_score,
            case_note_ai, overruled, is_reported, reporting_status, reporting_source,
            pdf_path, pdf_url, source_page
        ))
        case_db_id = cur.fetchone()[0]

    # 3. Insert Parties
    if parties_list:
        cur.execute("DELETE FROM parties WHERE case_id = %s;", (case_db_id,))
        for party in parties_list:
            if isinstance(party, dict):
                p_name = party.get("name")
                p_role = party.get("role", "PARTY")
                p_type = party.get("party_type", "INDIVIDUAL")
            else:
                p_name = str(party)
                p_role = "PARTY"
                p_type = "INDIVIDUAL"
            if p_name and p_name.strip():
                cur.execute("""
                    INSERT INTO parties (case_id, name, role, party_type)
                    VALUES (%s, %s, %s, %s);
                """, (case_db_id, p_name.strip(), p_role, p_type))

    # 4. Insert Judges & Case_Judges
    if judges_list:
        for judge_entry in judges_list:
            if isinstance(judge_entry, dict):
                j_name = judge_entry.get("name")
                j_role = judge_entry.get("role", "BENCH_MEMBER")
            else:
                j_name = str(judge_entry)
                j_role = "BENCH_MEMBER"

            if j_name and j_name.strip():
                judge_id = get_or_create_judge(cur, j_name)
                cur.execute("""
                    INSERT INTO case_judges (case_id, judge_id, role)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (case_id, judge_id) DO UPDATE SET role = EXCLUDED.role;
                """, (case_db_id, judge_id, j_role))

    # 5. Insert Advocates & Case_Advocates
    if advocates_list:
        for adv_entry in advocates_list:
            if isinstance(adv_entry, dict):
                a_name = adv_entry.get("name")
                a_designation = adv_entry.get("designation")
                a_role = adv_entry.get("for_party_role") or adv_entry.get("role", "COUNSEL")
            else:
                a_name = str(adv_entry)
                a_designation = None
                a_role = "COUNSEL"

            if a_name and a_name.strip():
                adv_id = get_or_create_advocate(cur, a_name, a_designation)
                cur.execute("""
                    INSERT INTO case_advocates (case_id, advocate_id, party_role)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (case_id, advocate_id) DO UPDATE SET party_role = EXCLUDED.party_role;
                """, (case_db_id, adv_id, a_role))

    # 6. Insert Acts & Provisions
    if acts_list:
        for act_entry in acts_list:
            if isinstance(act_entry, dict):
                act_n = act_entry.get("act_name")
                act_s = act_entry.get("short_name")
            else:
                act_n = str(act_entry)
                act_s = None
            if act_n and act_n.strip():
                get_or_create_act(cur, act_n, act_s)

    if provisions_list:
        cur.execute("DELETE FROM provisions WHERE case_id = %s;", (case_db_id,))
        for prov in provisions_list:
            act_name = prov.get("act_name") if isinstance(prov, dict) else None
            section = prov.get("section") if isinstance(prov, dict) else None
            if act_name and act_name.strip():
                act_id = get_or_create_act(cur, act_name)
                cur.execute("""
                    INSERT INTO provisions (case_id, act_id, act_name, section)
                    VALUES (%s, %s, %s, %s);
                """, (case_db_id, act_id, act_name.strip(), section.strip() if section else None))

    # 7. Insert Constitutional Articles
    if articles_list:
        cur.execute("DELETE FROM case_articles WHERE case_id = %s;", (case_db_id,))
        for art in articles_list:
            if art and str(art).strip():
                cur.execute("""
                    INSERT INTO case_articles (case_id, article)
                    VALUES (%s, %s)
                    ON CONFLICT (case_id, article) DO NOTHING;
                """, (case_db_id, str(art).strip()))

    # 8. Insert Reporter Citations
    if reporter_list:
        cur.execute("DELETE FROM reporter_citations WHERE case_id = %s;", (case_db_id,))
        for rep in reporter_list:
            if isinstance(rep, dict):
                cur.execute("""
                    INSERT INTO reporter_citations (case_id, reporter, citation, year, volume, page)
                    VALUES (%s, %s, %s, %s, %s, %s);
                """, (
                    case_db_id, rep.get("reporter"), rep.get("citation"),
                    rep.get("year"), rep.get("volume"), rep.get("page")
                ))

    # 9. Insert Multi-valued Metadata Categories
    if subjects_list:
        cur.execute("DELETE FROM case_subjects WHERE case_id = %s;", (case_db_id,))
        for subj in subjects_list:
            if subj and str(subj).strip():
                cur.execute("""
                    INSERT INTO case_subjects (case_id, subject)
                    VALUES (%s, %s)
                    ON CONFLICT (case_id, subject) DO NOTHING;
                """, (case_db_id, str(subj).strip()))

    if industries_list:
        cur.execute("DELETE FROM case_industries WHERE case_id = %s;", (case_db_id,))
        for ind in industries_list:
            if ind and str(ind).strip():
                cur.execute("""
                    INSERT INTO case_industries (case_id, industry)
                    VALUES (%s, %s)
                    ON CONFLICT (case_id, industry) DO NOTHING;
                """, (case_db_id, str(ind).strip()))

    if ministries_list:
        cur.execute("DELETE FROM case_ministries WHERE case_id = %s;", (case_db_id,))
        for min_item in ministries_list:
            if min_item and str(min_item).strip():
                cur.execute("""
                    INSERT INTO case_ministries (case_id, ministry)
                    VALUES (%s, %s)
                    ON CONFLICT (case_id, ministry) DO NOTHING;
                """, (case_db_id, str(min_item).strip()))

    if departments_list:
        cur.execute("DELETE FROM case_departments WHERE case_id = %s;", (case_db_id,))
        for dept in departments_list:
            if dept and str(dept).strip():
                cur.execute("""
                    INSERT INTO case_departments (case_id, department)
                    VALUES (%s, %s)
                    ON CONFLICT (case_id, department) DO NOTHING;
                """, (case_db_id, str(dept).strip()))

    return case_db_id


def import_metadata_json(file_path: Path, db_url: Optional[str] = None, record_limit: Optional[int] = None):
    """
    Loads a JSON metadata file and ingests records into PostgreSQL.
    Merges raw scraper records (supreme_court_judgments.json) for 100% judge and advocate coverage.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")

    # Look for corresponding raw scraper file supreme_court_judgments.json in same folder
    raw_record_map = {}
    raw_json_path = path.parent / "supreme_court_judgments.json"
    if raw_json_path.exists():
        try:
            with open(raw_json_path, "r", encoding="utf-8") as rf:
                raw_data = json.load(rf)
                if isinstance(raw_data, dict):
                    for c_name, recs in raw_data.items():
                        if isinstance(recs, list):
                            for r in recs:
                                d_num = r.get("diary_number")
                                if d_num:
                                    raw_record_map[d_num] = r
            print(f"[INFO] Loaded {len(raw_record_map)} raw scraper records for judge/advocate fallback.")
        except Exception as raw_err:
            print(f"[WARN] Failed to load raw scraper records: {raw_err}")

    target_url = db_url or get_db_url()
    print("=" * 80)
    print("IMPORTING LEGAL METADATA TO POSTGRESQL")
    print(f"Source JSON : {path}")
    print(f"Target DB   : {target_url}")
    if record_limit:
        print(f"Limit       : {record_limit} record(s)")
    print("=" * 80)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    records_to_process = []
    if isinstance(data, dict):
        for court_name, records in data.items():
            if isinstance(records, list):
                records_to_process.extend(records)
    elif isinstance(data, list):
        records_to_process = data

    total = len(records_to_process)
    print(f"Total case records found in JSON: {total}")

    conn = get_connection(target_url)
    imported = 0
    errors = 0

    try:
        with conn.cursor() as cur:
            for idx, rec in enumerate(records_to_process, start=1):
                if record_limit and imported >= record_limit:
                    break

                try:
                    cur.execute("SAVEPOINT case_sp;")
                    case_id = process_record(cur, rec, "SCIN", raw_record_map)
                    cur.execute("RELEASE SAVEPOINT case_sp;")
                    conn.commit()
                    imported += 1
                    print(f"  [OK] [{idx}/{total}] Imported Case ID: {case_id}")
                except Exception as rec_err:
                    cur.execute("ROLLBACK TO SAVEPOINT case_sp;")
                    conn.commit()
                    errors += 1
                    print(f"  [WARN] [{idx}/{total}] Failed to import record: {rec_err}")

        print("\n" + "=" * 80)
        print("IMPORT COMPLETE")
        print(f"Successfully processed : {imported}")
        print(f"Errors encountered     : {errors}")
        print("=" * 80)
    finally:
        conn.close()


def query_sample_cases(limit: int = 5, db_url: Optional[str] = None):
    """Prints a detailed summary sample of stored cases from PostgreSQL."""
    target_url = db_url or get_db_url()
    conn = get_connection(target_url)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.id, c.diary_number, c.case_number, c.judgment_date,
                       c.overruled, c.is_reported, c.reporting_status,
                       c.pdf_path, c.pdf_url,
                       SUBSTRING(c.case_note_ai FROM 1 FOR 120) AS note_preview
                FROM cases c
                ORDER BY c.id DESC
                LIMIT %s;
            """, (limit,))
            rows = cur.fetchall()

            print("\n" + "=" * 80)
            print(f"SAMPLE POSTGRESQL RECORDS (Limit: {limit})")
            print("=" * 80)
            for r in rows:
                case_id = r[0]
                print(f"ID: {case_id} | Diary: {r[1]} | Case No: {r[2]}")
                print(f"  Judgment Date   : {r[3]}")
                print(f"  Overruled       : {r[4]} | Reported: {r[5]} ({r[6]})")
                print(f"  PDF Path        : {r[7]}")
                print(f"  PDF URL         : {r[8]}")
                print(f"  AI Case Note    : {r[9]}...")

                # Fetch parties
                cur.execute("SELECT name, role FROM parties WHERE case_id = %s;", (case_id,))
                parties = cur.fetchall()
                print(f"  Parties ({len(parties)})    : {', '.join([f'{p[0]} ({p[1]})' for p in parties[:3]])}")

                # Fetch judges
                cur.execute("""
                    SELECT j.name, cj.role
                    FROM case_judges cj JOIN judges j ON cj.judge_id = j.id
                    WHERE cj.case_id = %s;
                """, (case_id,))
                judges = cur.fetchall()
                print(f"  Judges ({len(judges)})     : {', '.join([f'{j[0]} [{j[1]}]' for j in judges[:3]])}")

                # Fetch advocates
                cur.execute("""
                    SELECT a.name, ca.party_role
                    FROM case_advocates ca JOIN advocates a ON ca.advocate_id = a.id
                    WHERE ca.case_id = %s;
                """, (case_id,))
                advs = cur.fetchall()
                print(f"  Advocates ({len(advs)})  : {', '.join([f'{a[0]} ({a[1]})' for a in advs[:3]])}")

                # Fetch provisions
                cur.execute("SELECT act_name, section FROM provisions WHERE case_id = %s;", (case_id,))
                provs = cur.fetchall()
                print(f"  Provisions ({len(provs)}): {', '.join([f'{p[0]} s.{p[1]}' for p in provs[:3]])}")

                # Fetch articles
                cur.execute("SELECT article FROM case_articles WHERE case_id = %s;", (case_id,))
                arts = [a[0] for a in cur.fetchall()]
                if arts:
                    print(f"  Articles        : {', '.join(arts)}")

                print("-" * 60)
    finally:
        conn.close()


# ============================================================
# CLI ENTRY POINT
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="PostgreSQL Database Manager & Ingestion Tool for Scraped Legal Metadata."
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # init command
    parser_init = subparsers.add_parser("init", help="Initialize PostgreSQL schema and tables")
    parser_init.add_argument("--db-url", help="Database connection URL", default=None)
    parser_init.add_argument("--reset", action="store_true", help="Drop existing tables and reset schema")

    # import command
    parser_import = subparsers.add_parser("import", help="Import metadata JSON file into PostgreSQL")
    parser_import.add_argument("file", help="Path to JSON file (e.g. supreme_court_metadata.json)")
    parser_import.add_argument("--db-url", help="Database connection URL", default=None)
    parser_import.add_argument("--limit", type=int, default=None, help="Limit number of records to import")

    # query command
    parser_query = subparsers.add_parser("query", help="Show sample case records from PostgreSQL")
    parser_query.add_argument("--limit", type=int, default=5, help="Number of records to show")
    parser_query.add_argument("--db-url", help="Database connection URL", default=None)

    args = parser.parse_args()

    if args.command == "init":
        init_db(args.db_url, args.reset)
    elif args.command == "import":
        init_db(args.db_url, False)
        import_metadata_json(Path(args.file), args.db_url, args.limit)
    elif args.command == "query":
        query_sample_cases(args.limit, args.db_url)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
