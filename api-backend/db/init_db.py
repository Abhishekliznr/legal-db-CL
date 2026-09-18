"""
Schema initialization CLI for api-backend.

Four commands, for the two real deployment shapes:

- `init` applies schema.sql + supplement.sql + filters_supplement.sql +
  view_supplement.sql together, for a fully standalone database
  (api-backend's own docker-compose db, or any fresh Postgres). Run once
  against an empty database — see schema.sql's header about CREATE TYPE
  not being idempotent.
- `ensure-supplement` applies ONLY supplement.sql (cr_search_history),
  idempotently (IF NOT EXISTS throughout).
- `ensure-filters` applies ONLY filters_supplement.sql (cr_filter_definitions/
  cr_filter_options/cr_search_field_definitions), idempotently.
- `ensure-view` applies ONLY view_supplement.sql (cr_case_search_view),
  idempotently (CREATE OR REPLACE VIEW is naturally safe to re-run).

All three `ensure-*` commands are the shared-DB shape: scraper-backend
already ran its own `init` against the real database (which creates none of
cr_search_history/cr_filter_definitions/cr_filter_options/
cr_search_field_definitions/cr_case_search_view — see schema.sql's header),
and api-backend needs all three added afterward without touching anything
scraper-backend created. Run all three ensure-* commands once against a
shared database.

`ensure_schema()` is the startup-safe entry point used by api.py: it checks
whether the core `cr_cases` table already exists. If not (standalone, fresh
database) it runs the full `init` path. If it does (shared-DB deployment,
scraper-backend already applied schema.sql's core section) it skips that
non-idempotent part and just runs the three ensure-* commands, which are
each safe to re-run. Either way it can be called on every app startup.

Usage:
    python -m db.init_db init                  # standalone: full schema + all three supplements
    python -m db.init_db init --drop           # ...dropping everything first
    python -m db.init_db ensure-supplement     # shared-DB: just add cr_search_history
    python -m db.init_db ensure-filters        # shared-DB: just add filter/search metadata tables
    python -m db.init_db ensure-view           # shared-DB: just add/refresh cr_case_search_view
    python -m db.init_db ensure-schema         # startup-safe: pick the right path automatically
"""

import argparse
from pathlib import Path

from db.connection import get_connection

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
SUPPLEMENT_PATH = Path(__file__).resolve().parent / "supplement.sql"
FILTERS_SUPPLEMENT_PATH = Path(__file__).resolve().parent / "filters_supplement.sql"
VIEW_SUPPLEMENT_PATH = Path(__file__).resolve().parent / "view_supplement.sql"

# Reverse-dependency order so FK/type drops don't fail. Includes both the
# current schema's own tables (cr_-prefixed, 2026-09-09 rename) AND every
# table/type name from before that rename plus the pre-2026-09-08 schema
# (documents/parties/case_counsels/document_coram/etc., statutes, advocates,
# departments) -- IF EXISTS makes the old names harmless no-ops once a
# database has actually been rebuilt on the current schema, but covering
# them here means `init --drop` alone fully cleans a database still
# carrying leftovers from before either rewrite.
_DROP_TABLES_SQL = """
DROP TABLE IF EXISTS
    cr_search_history,
    cr_case_research_search_history,
    case_research_search_history,
    cr_filter_options, cr_filter_definitions, cr_search_field_definitions,
    filter_options, filter_definitions, search_field_definitions,
    cr_court_scrape_config,
    cr_citation_sequences, cr_cases, cr_raw_ingestions, cr_scrape_batches,
    cr_orders, cr_rules, cr_sections, cr_acts, cr_industries, cr_ministries,
    cr_subjects, cr_case_categories, cr_judges, cr_courts,
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
DROP VIEW IF EXISTS cr_case_search_view CASCADE;
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
DROP FUNCTION IF EXISTS cr_cases_search_vector_update() CASCADE;
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

        print("Applying supplement.sql (cr_search_history)...")
        with conn.cursor() as cur:
            cur.execute(supplement_sql)
        conn.commit()

        print("Applying filters_supplement.sql (cr_filter_definitions/cr_filter_options/cr_search_field_definitions)...")
        with conn.cursor() as cur:
            cur.execute(filters_supplement_sql)
        conn.commit()

        print("Applying view_supplement.sql (cr_case_search_view)...")
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
    """Shared-DB path: idempotently adds ONLY cr_search_history — see this file's docstring."""
    _apply_one(SUPPLEMENT_PATH, "cr_search_history")


def ensure_filters() -> None:
    """Shared-DB path: idempotently adds ONLY the filter/search metadata tables — see this file's docstring."""
    _apply_one(FILTERS_SUPPLEMENT_PATH, "cr_filter_definitions/cr_filter_options/cr_search_field_definitions")


def ensure_view() -> None:
    """Shared-DB path: idempotently (re)creates ONLY cr_case_search_view — see this file's docstring."""
    _apply_one(VIEW_SUPPLEMENT_PATH, "cr_case_search_view")


def _core_schema_exists(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.cr_cases') IS NOT NULL;")
        return cur.fetchone()[0]


# Same fixed key as scraper-backend's db/init_db.py — deliberately NOT a
# separate per-service key. Both services share one Postgres instance in
# production (see connection.py's pool-sizing note), and both run this same
# check-then-apply-non-idempotent-DDL dance at startup, so the lock has to
# be held across services, not just across replicas of one service, or two
# instances (api-backend vs scraper-backend, or two replicas of the same
# service during a rolling deploy) can still both see `cr_cases` missing at
# the same time and race each other's CREATE TYPE — which is exactly the
# "type already exists" crash this lock exists to prevent. Keep this
# literal value identical to scraper-backend's _ENSURE_SCHEMA_LOCK_KEY.
_ENSURE_SCHEMA_LOCK_KEY = 279_460_113


def ensure_schema() -> None:
    """
    Startup-safe entry point: picks standalone vs shared-DB automatically.

    - Core `cr_cases` table missing (standalone, fresh database): runs the
      full `init` path (schema.sql + all three supplements).
    - Core `cr_cases` table present (shared-DB, scraper-backend already ran
      its own `init`): skips schema.sql — its CREATE TYPE/CREATE TABLE
      aren't idempotent — and just runs the three ensure-* commands, which
      are each already safe to re-run, so this also self-heals a
      shared-DB deployment where api-backend's own tables/view aren't
      there yet.

    Holds a session-level Postgres advisory lock for the whole check+apply
    so that two instances starting concurrently against the same database
    (two replicas of this service cold-starting together, a rolling deploy
    overlap, or a race against scraper-backend's own ensure_schema in the
    shared-DB deployment) serialize instead of both racing schema.sql's
    non-idempotent CREATE TYPE/CREATE TABLE statements.

    Safe to call on every app startup either way.
    """
    lock_conn = get_connection()
    try:
        with lock_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s);", (_ENSURE_SCHEMA_LOCK_KEY,))
        try:
            conn = get_connection()
            try:
                core_exists = _core_schema_exists(conn)
            finally:
                conn.close()

            if not core_exists:
                print("No schema detected — applying schema.sql + all supplements for the first time...")
                init_database(drop_existing=False)
            else:
                print("Core schema already present — ensuring api-backend's own supplement tables/view exist...")
                ensure_supplement()
                ensure_filters()
                ensure_view()
        finally:
            with lock_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s);", (_ENSURE_SCHEMA_LOCK_KEY,))
    finally:
        lock_conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="api-backend schema manager")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Apply schema.sql + all three supplements (standalone database)")
    init_parser.add_argument("--drop", action="store_true", help="Drop existing tables/types first")

    subparsers.add_parser("ensure-supplement", help="Idempotently apply only supplement.sql (shared-DB deployment)")
    subparsers.add_parser("ensure-filters", help="Idempotently apply only filters_supplement.sql (shared-DB deployment)")
    subparsers.add_parser("ensure-view", help="Idempotently apply only view_supplement.sql (shared-DB deployment)")
    subparsers.add_parser("ensure-schema", help="Startup-safe: pick standalone vs shared-DB path automatically")

    args = parser.parse_args()
    if args.command == "init":
        init_database(drop_existing=args.drop)
    elif args.command == "ensure-supplement":
        ensure_supplement()
    elif args.command == "ensure-filters":
        ensure_filters()
    elif args.command == "ensure-view":
        ensure_view()
    elif args.command == "ensure-schema":
        ensure_schema()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
