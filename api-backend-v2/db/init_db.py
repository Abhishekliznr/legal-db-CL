"""
Schema initialization CLI for api-backend-v2.

Two commands, for the two real deployment shapes:

- `init` applies schema.sql + supplement.sql together, for a fully
  standalone database (api-backend-v2's own docker-compose db, or any fresh
  Postgres). Run once against an empty database — see schema.sql's header
  about CREATE TYPE not being idempotent.
- `ensure-supplement` applies ONLY supplement.sql (case_research_search_history),
  idempotently (IF NOT EXISTS throughout). This is the shared-DB shape:
  scraper-backend-v2 already ran its own `init` against the real database,
  and api-backend-v2 just needs its one extra table added without touching
  anything scraper-backend-v2 created.

Usage:
    python -m db.init_db init                  # standalone: full schema + supplement
    python -m db.init_db init --drop           # ...dropping everything first
    python -m db.init_db ensure-supplement     # shared-DB: just add case_research_search_history
"""

import argparse
from pathlib import Path

from db.connection import get_connection

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
SUPPLEMENT_PATH = Path(__file__).resolve().parent / "supplement.sql"

# Reverse-dependency order so FK/type drops don't fail.
_DROP_TABLES_SQL = """
DROP TABLE IF EXISTS
    case_research_search_history,
    filter_options, filter_definitions, search_field_definitions,
    court_scrape_config,
    citations, document_ministry_department, document_industries,
    document_subjects, document_sections, document_coram, case_counsels,
    parties, cases, documents, raw_ingestions, scrape_batches,
    sections, statutes, industries, departments, ministries,
    subjects, case_categories, advocates, judges, courts
    CASCADE;
"""

_DROP_TYPES_SQL = """
DROP TYPE IF EXISTS
    doc_type_enum, decision_type_enum, party_side_enum,
    ingestion_status_enum, citation_treatment_enum,
    disposition_category_enum, data_source_enum
    CASCADE;
"""

_DROP_FUNCTIONS_SQL = """
DROP FUNCTION IF EXISTS documents_search_vector_update() CASCADE;
DROP FUNCTION IF EXISTS touch_updated_at() CASCADE;
"""


def init_database(drop_existing: bool = False) -> None:
    """Standalone path: full schema.sql + supplement.sql against an empty (or --drop'd) database."""
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    supplement_sql = SUPPLEMENT_PATH.read_text(encoding="utf-8")
    conn = get_connection()
    try:
        if drop_existing:
            print("Dropping existing tables/types/functions for a clean re-init...")
            with conn.cursor() as cur:
                cur.execute(_DROP_TABLES_SQL)
                cur.execute(_DROP_TYPES_SQL)
                cur.execute(_DROP_FUNCTIONS_SQL)
            conn.commit()

        print("Applying schema.sql (caselaw_schema.sql + operational supplement)...")
        with conn.cursor() as cur:
            cur.execute(schema_sql)
        conn.commit()

        print("Applying supplement.sql (case_research_search_history)...")
        with conn.cursor() as cur:
            cur.execute(supplement_sql)
        conn.commit()

        print("Schema applied successfully.")
    finally:
        conn.close()


def ensure_supplement() -> None:
    """Shared-DB path: idempotently adds ONLY case_research_search_history — see this file's docstring."""
    supplement_sql = SUPPLEMENT_PATH.read_text(encoding="utf-8")
    conn = get_connection()
    try:
        print("Applying supplement.sql (case_research_search_history) — idempotent, safe to re-run...")
        with conn.cursor() as cur:
            cur.execute(supplement_sql)
        conn.commit()
        print("Supplement applied successfully.")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="api-backend-v2 schema manager")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Apply schema.sql + supplement.sql (standalone database)")
    init_parser.add_argument("--drop", action="store_true", help="Drop existing tables/types first")

    subparsers.add_parser("ensure-supplement", help="Idempotently apply only supplement.sql (shared-DB deployment)")

    args = parser.parse_args()
    if args.command == "init":
        init_database(drop_existing=args.drop)
    elif args.command == "ensure-supplement":
        ensure_supplement()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
