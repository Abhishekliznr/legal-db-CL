"""
Schema initialization CLI for scraper-backend.

Applies db/schema.sql (caselaw_schema.sql + the operational supplement) to
PostgreSQL. Designed to run once against an empty database — see the note
at the top of schema.sql about CREATE TYPE not being idempotent in Postgres.

`ensure_schema()` is the startup-safe variant used by api.py: it checks
whether the core `cr_cases` table already exists and only calls
`init_database()` when it doesn't, so it can be called on every app
startup without ever re-running schema.sql's non-idempotent CREATE
TYPE/CREATE TABLE statements against a database that already has them.

Usage:
    python -m db.init_db init            # apply schema (fails if already applied)
    python -m db.init_db init --drop     # drop everything first, then apply cleanly
    python -m db.init_db ensure-schema   # apply schema only if missing (safe to re-run)
"""

import argparse
from pathlib import Path

from db.connection import get_connection

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# Reverse-dependency order so FK/type drops don't fail. Includes both the
# current schema's own tables (cr_-prefixed, 2026-09-09 rename) AND every
# table/type name from before that rename plus the pre-2026-09-08 schema
# (documents/parties/case_counsels/document_coram/etc., statutes, advocates,
# departments, filter_definitions/filter_options/search_field_definitions)
# — IF EXISTS makes the old names harmless no-ops once a database has
# actually been rebuilt on the current schema, but covering them here means
# `init --drop` alone fully cleans a database still carrying leftovers from
# before either rewrite, with no separate manual cleanup step required.
_DROP_TABLES_SQL = """
DROP TABLE IF EXISTS
    cr_court_scrape_config,
    cr_citation_sequences, cr_cases, cr_raw_ingestions, cr_scrape_batches,
    cr_orders, cr_rules, cr_sections, cr_acts, cr_industries, cr_ministries,
    cr_subjects, cr_case_categories, cr_judges, cr_courts,
    court_scrape_config,
    citation_sequences, cases, raw_ingestions, scrape_batches,
    orders, rules, sections, acts, industries, ministries,
    subjects, case_categories, judges, courts,
    filter_options, filter_definitions, search_field_definitions,
    document_paragraphs, case_timeline_events, case_appellate_history,
    document_holdings, citations, document_ministry_department,
    document_industries, document_subjects, document_sections,
    document_coram, case_counsels, parties, documents,
    statutes, departments, advocates
    CASCADE;
"""

_DROP_TYPES_SQL = """
DROP TYPE IF EXISTS
    doc_type_enum, ingestion_status_enum,
    disposition_category_enum, data_source_enum,
    decision_type_enum, party_side_enum, citation_treatment_enum, appellate_outcome_enum
    CASCADE;
"""

_DROP_FUNCTIONS_SQL = """
DROP FUNCTION IF EXISTS touch_updated_at() CASCADE;
DROP FUNCTION IF EXISTS cr_cases_search_vector_update() CASCADE;
DROP FUNCTION IF EXISTS cases_search_vector_update() CASCADE;
DROP FUNCTION IF EXISTS documents_search_vector_update() CASCADE;
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


def _core_schema_exists(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.cr_cases') IS NOT NULL;")
        return cur.fetchone()[0]


def ensure_schema() -> None:
    """
    Startup-safe entry point: applies schema.sql the first time this runs
    against a fresh database, then is a no-op on every later call (including
    a shared-DB deployment where the *other* service already applied it).
    """
    conn = get_connection()
    try:
        exists = _core_schema_exists(conn)
    finally:
        conn.close()

    if exists:
        print("Schema already present, skipping auto-init.")
        return
    print("No schema detected — applying schema.sql for the first time...")
    init_database(drop_existing=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="scraper-backend schema manager")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Apply schema.sql")
    init_parser.add_argument("--drop", action="store_true", help="Drop existing tables/types first")

    subparsers.add_parser("ensure-schema", help="Apply schema.sql only if not already present (safe to re-run)")

    args = parser.parse_args()
    if args.command == "init":
        init_database(drop_existing=args.drop)
    elif args.command == "ensure-schema":
        ensure_schema()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
