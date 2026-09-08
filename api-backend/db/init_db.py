"""
Schema initialization CLI for api-backend.

Four commands, for the two real deployment shapes:

- `init` applies schema.sql + supplement.sql + filters_supplement.sql +
  view_supplement.sql together, for a fully standalone database
  (api-backend's own docker-compose db, or any fresh Postgres). Run once
  against an empty database — see schema.sql's header about CREATE TYPE
  not being idempotent.
- `ensure-supplement` applies ONLY supplement.sql (case_research_search_history),
  idempotently (IF NOT EXISTS throughout).
- `ensure-filters` applies ONLY filters_supplement.sql (filter_definitions/
  filter_options/search_field_definitions), idempotently.
- `ensure-view` applies ONLY view_supplement.sql (case_search_view),
  idempotently (CREATE OR REPLACE VIEW is naturally safe to re-run).

All three `ensure-*` commands are the shared-DB shape: scraper-backend
already ran its own `init` against the real database (which creates none of
case_research_search_history/filter_definitions/filter_options/
search_field_definitions/case_search_view — see schema.sql's header), and
api-backend needs all three added afterward without touching anything
scraper-backend created. Run all three ensure-* commands once against a
shared database.

Usage:
    python -m db.init_db init                  # standalone: full schema + all three supplements
    python -m db.init_db init --drop           # ...dropping everything first
    python -m db.init_db ensure-supplement     # shared-DB: just add case_research_search_history
    python -m db.init_db ensure-filters        # shared-DB: just add filter/search metadata tables
    python -m db.init_db ensure-view           # shared-DB: just add/refresh case_search_view
"""

import argparse
from pathlib import Path

from db.connection import get_connection

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
SUPPLEMENT_PATH = Path(__file__).resolve().parent / "supplement.sql"
FILTERS_SUPPLEMENT_PATH = Path(__file__).resolve().parent / "filters_supplement.sql"
VIEW_SUPPLEMENT_PATH = Path(__file__).resolve().parent / "view_supplement.sql"

# Reverse-dependency order so FK/type drops don't fail. Includes both the
# current schema's own tables AND every table/type name from the pre-
# 2026-09-08 schema (documents/parties/case_counsels/document_coram/etc.,
# statutes, advocates, departments) -- IF EXISTS makes the old names
# harmless no-ops once a database has actually been rebuilt on the current
# schema, but covering them here means `init --drop` alone fully cleans a
# database still carrying leftovers from before the rewrite.
_DROP_TABLES_SQL = """
DROP TABLE IF EXISTS
    case_research_search_history,
    filter_options, filter_definitions, search_field_definitions,
    court_scrape_config,
    citation_sequences, cases, raw_ingestions, scrape_batches,
    orders, rules, sections, acts, industries, ministries,
    subjects, case_categories, judges, courts,
    document_paragraphs, case_timeline_events, case_appellate_history,
    document_holdings, citations, document_ministry_department,
    document_industries, document_subjects, document_sections,
    document_coram, case_counsels, parties, documents,
    statutes, departments, advocates
    CASCADE;
"""

_DROP_VIEWS_SQL = """
DROP VIEW IF EXISTS case_search_view CASCADE;
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
DROP FUNCTION IF EXISTS cases_search_vector_update() CASCADE;
DROP FUNCTION IF EXISTS documents_search_vector_update() CASCADE;
"""


def init_database(drop_existing: bool = False) -> None:
    """Standalone path: full schema.sql + all three supplements against an empty (or --drop'd) database."""
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    supplement_sql = SUPPLEMENT_PATH.read_text(encoding="utf-8")
    filters_supplement_sql = FILTERS_SUPPLEMENT_PATH.read_text(encoding="utf-8")
    view_supplement_sql = VIEW_SUPPLEMENT_PATH.read_text(encoding="utf-8")
    conn = get_connection()
    try:
        if drop_existing:
            print("Dropping existing tables/views/types/functions for a clean re-init...")
            with conn.cursor() as cur:
                cur.execute(_DROP_VIEWS_SQL)
                cur.execute(_DROP_TABLES_SQL)
                cur.execute(_DROP_TYPES_SQL)
                cur.execute(_DROP_FUNCTIONS_SQL)
            conn.commit()

        print("Applying schema.sql...")
        with conn.cursor() as cur:
            cur.execute(schema_sql)
        conn.commit()

        print("Applying supplement.sql (case_research_search_history)...")
        with conn.cursor() as cur:
            cur.execute(supplement_sql)
        conn.commit()

        print("Applying filters_supplement.sql (filter_definitions/filter_options/search_field_definitions)...")
        with conn.cursor() as cur:
            cur.execute(filters_supplement_sql)
        conn.commit()

        print("Applying view_supplement.sql (case_search_view)...")
        with conn.cursor() as cur:
            cur.execute(view_supplement_sql)
        conn.commit()

        print("Schema applied successfully.")
    finally:
        conn.close()


def _apply_one(path: Path, label: str) -> None:
    sql = path.read_text(encoding="utf-8")
    conn = get_connection()
    try:
        print(f"Applying {path.name} ({label}) — idempotent, safe to re-run...")
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print(f"{path.name} applied successfully.")
    finally:
        conn.close()


def ensure_supplement() -> None:
    """Shared-DB path: idempotently adds ONLY case_research_search_history — see this file's docstring."""
    _apply_one(SUPPLEMENT_PATH, "case_research_search_history")


def ensure_filters() -> None:
    """Shared-DB path: idempotently adds ONLY the filter/search metadata tables — see this file's docstring."""
    _apply_one(FILTERS_SUPPLEMENT_PATH, "filter_definitions/filter_options/search_field_definitions")


def ensure_view() -> None:
    """Shared-DB path: idempotently (re)creates ONLY case_search_view — see this file's docstring."""
    _apply_one(VIEW_SUPPLEMENT_PATH, "case_search_view")


def main() -> None:
    parser = argparse.ArgumentParser(description="api-backend schema manager")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Apply schema.sql + all three supplements (standalone database)")
    init_parser.add_argument("--drop", action="store_true", help="Drop existing tables/types first")

    subparsers.add_parser("ensure-supplement", help="Idempotently apply only supplement.sql (shared-DB deployment)")
    subparsers.add_parser("ensure-filters", help="Idempotently apply only filters_supplement.sql (shared-DB deployment)")
    subparsers.add_parser("ensure-view", help="Idempotently apply only view_supplement.sql (shared-DB deployment)")

    args = parser.parse_args()
    if args.command == "init":
        init_database(drop_existing=args.drop)
    elif args.command == "ensure-supplement":
        ensure_supplement()
    elif args.command == "ensure-filters":
        ensure_filters()
    elif args.command == "ensure-view":
        ensure_view()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
