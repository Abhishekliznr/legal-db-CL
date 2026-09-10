"""
api-backend: Case Search & Citator FastAPI Application (rebuild)
--------------------------------------------------------------------
Standalone — no shared code with scraper-backend or the old api-backend
(docs/scraper-backend-revamp-spec.md §3.2). Read-only, DB-only service, no
Playwright/scraping dependencies — court scraping lives in the separate
scraper-backend service, which writes to the same Postgres database this
service reads from.

Phase 4 (spec §6): filter_router/search_router/stats_router rewritten
against the new schema (documents/cases split, case_search_view,
tsvector full-text search); history_router ported with its own
api-backend-only table (cr_case_research_search_history).

Startup auto-applies the schema via db/init_db.py's `ensure_schema()`.
That only actually creates anything the first time it runs against a
database missing the core `cases` table (standalone); once that table
exists — including in a shared-DB deployment where scraper-backend
already created it (see db/schema.sql's header) — it never re-runs
schema.sql's non-idempotent CREATE TYPE/CREATE TABLE again, and instead
just idempotently ensures api-backend's own supplement tables/view exist.

Startup also runs db/seed_filters.py's `seed()` right after — populates
cr_filter_definitions/cr_search_field_definitions (what /api/cases/filters
and /api/cases/searches serve) via ON CONFLICT (key) DO NOTHING, so it's a
no-op on an already-seeded DB and never clobbers an admin's edits.
"""

import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from db import connection
from routers import filter_router, history_router, search_router, stats_router

app = FastAPI(
    title="Legal Judgment Intelligence API (v2)",
    description="Rebuild in progress — see legal-db/docs/scraper-backend-revamp-spec.md",
    version="2.0.0-phase4",
)

# No credentialed (cookie/session) requests are used by this API, so
# allow_credentials is intentionally omitted — combining it with a
# wildcard origin is a common misconfiguration browsers reject anyway.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    # Non-fatal, matching scraper-backend's api.py — a DB hiccup at
    # startup shouldn't crash-loop the container when /health can just
    # report "degraded" instead.
    try:
        connection.init_connection_pool()
    except Exception as e:
        print(f"WARNING: could not initialize DB connection pool at startup: {e}")
        return

    # ensure_schema() only creates anything the first time it sees a
    # database without the core `cases` table; every later startup (and
    # a shared-DB deployment where scraper-backend already created it)
    # is a cheap no-op / idempotent supplement check. Non-fatal for the
    # same reason as the pool init above.
    try:
        from db.init_db import ensure_schema
        ensure_schema()
    except Exception as e:
        print(f"WARNING: could not ensure DB schema at startup: {e}")

    # seed() is ON CONFLICT DO NOTHING against cr_filter_definitions/
    # cr_search_field_definitions, so this is a no-op once seeded and safe
    # to run on every startup — same non-fatal pattern as ensure_schema()
    # above, since a fresh/unseeded DB shouldn't crash-loop the container.
    try:
        from db.seed_filters import seed as seed_filters
        seed_filters()
    except Exception as e:
        print(f"WARNING: could not seed filter/search-field definitions at startup: {e}")


@app.on_event("shutdown")
def on_shutdown():
    connection.close_connection_pool()


# Filters/stats/history are static sub-paths of /api/cases and must be
# mounted before search_router, or a request to e.g. /api/cases/stats would
# be swallowed by search_router's /api/cases/{case_id} wildcard route.
app.include_router(filter_router.router)
app.include_router(stats_router.router)
app.include_router(history_router.router)
app.include_router(search_router.router)


STATIC_DIR = Path(__file__).resolve().parent / "static"


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def serve_landing_page():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse("<h1>Legal Judgment Intelligence API (v2)</h1><p>Visit <a href='/docs'>/docs</a>.</p>")


@app.get("/health")
def health_check():
    try:
        with connection.get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.fetchone()
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {e}"

    return {
        "status": "healthy" if db_status == "connected" else "degraded",
        "service": "api-backend",
        "version": "2.0.0-phase4",
        "database": db_status,
    }
