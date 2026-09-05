"""
Schema initialization CLI for scraper-backend-v2.

Applies db/schema.sql (caselaw_schema.sql + the operational supplement) to
PostgreSQL. Designed to run once against an empty database — see the note
at the top of schema.sql about CREATE TYPE not being idempotent in Postgres.

Usage:
    python -m db.init_db init            # apply schema (fails if already applied)
    python -m db.init_db init --drop     # drop everything first, then apply cleanly
"""

import argparse
from pathlib import Path

from db.connection import get_connection

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# Reverse-dependency order so FK/type drops don't fail.
_DROP_TABLES_SQL = """
DROP TABLE IF EXISTS
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
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
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
            cur.execute(sql)
        conn.commit()
        print("Schema applied successfully.")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="scraper-backend-v2 schema manager")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Apply schema.sql")
    init_parser.add_argument("--drop", action="store_true", help="Drop existing tables/types first")

    args = parser.parse_args()
    if args.command == "init":
        init_database(drop_existing=args.drop)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
