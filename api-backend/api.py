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
api-backend-only table (case_research_search_history).

Startup does NOT auto-apply the schema — run `python -m db.init_db init`
explicitly once. In a shared-DB deployment, only ONE of api-backend /
scraper-backend should actually run that (see db/schema.sql's header);
this service still verifies the schema is *present* at startup without
trying to (re)create it.
"""

import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
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
