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
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

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
    cr_schema_migrations,
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


def _ensure_migrations_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cr_schema_migrations (
                filename    TEXT PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
    conn.commit()


def apply_migrations() -> None:
    """
    Applies each db/migrations/*.sql file, in filename order, exactly once
    ever — recording it in cr_schema_migrations right after it runs, in the
    same transaction, and skipping any filename already recorded there.

    This used to re-run every file on every startup instead (each one
    "idempotent" in the sense of not erroring twice), which was fine for
    additive ones (ADD COLUMN IF NOT EXISTS) but wrong for
    0006_drop_structured_content.sql's `DROP VIEW IF EXISTS
    cr_case_search_view` — not erroring on a second run isn't the same as
    being harmless to run again: that view belongs to api-backend, which
    only ever recreates it at its OWN startup. Every scraper-backend
    restart that didn't happen to coincide with an api-backend restart
    silently dropped the view and left every /api/cases search failing
    with `UndefinedTable: relation "cr_case_search_view" does not exist`
    until someone noticed and manually re-ran api-backend's `ensure-view`
    (or restarted it). Run-once tracking closes that off for this and any
    future migration with a similarly one-shot statement.
    """
    conn = get_connection()
    try:
        _ensure_migrations_table(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM cr_schema_migrations;")
            already_applied = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in already_applied:
            continue
        sql = path.read_text(encoding="utf-8")
        conn = get_connection()
        try:
            print(f"Applying migration {path.name} (first time)...")
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute("INSERT INTO cr_schema_migrations (filename) VALUES (%s);", (path.name,))
            conn.commit()
        finally:
            conn.close()


# Arbitrary fixed key for the session-level advisory lock below. Any bigint
# works as long as it's unique within this database — picked by hashing
# the string "scraper_backend_v2.ensure_schema" mod 2^31 and truncating,
# no significance beyond being a stable constant.
_ENSURE_SCHEMA_LOCK_KEY = 279_460_113


def ensure_schema() -> None:
    """
    Startup-safe entry point: applies schema.sql the first time this runs
    against a fresh database, then applies any not-yet-applied
    db/migrations/*.sql files either way (see apply_migrations() — each
    file runs exactly once, tracked in cr_schema_migrations), so an existing
    table missing a column a later migration added gets brought up to date
    automatically instead of failing at query time in some unrelated router.

    Holds a session-level Postgres advisory lock for the whole check+apply
    so that two instances starting concurrently against the same empty
    database (a rolling deploy, or >1 replica cold-starting together)
    serialize instead of both racing schema.sql's non-idempotent CREATE
    TYPE/CREATE TABLE statements — that race previously left the database
    with zero cr_ tables when both transactions stepped on each other and
    aborted, while the app still came up "healthy" (see api.py's on_startup,
    which treats this as non-fatal).
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s);", (_ENSURE_SCHEMA_LOCK_KEY,))
        try:
            if _core_schema_exists(conn):
                print("Schema already present, skipping auto-init.")
            else:
                print("No schema detected — applying schema.sql for the first time...")
                init_database(drop_existing=False)
            apply_migrations()
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s);", (_ENSURE_SCHEMA_LOCK_KEY,))
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="scraper-backend schema manager")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Apply schema.sql")
    init_parser.add_argument("--drop", action="store_true", help="Drop existing tables/types first")

    subparsers.add_parser("ensure-schema", help="Apply schema.sql (if missing) + any unapplied migrations (safe to re-run)")
    subparsers.add_parser("migrate", help="Apply any unapplied db/migrations/*.sql, in order, each exactly once")

    args = parser.parse_args()
    if args.command == "init":
        init_database(drop_existing=args.drop)
    elif args.command == "ensure-schema":
        ensure_schema()
    elif args.command == "migrate":
        apply_migrations()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
