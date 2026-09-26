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

Startup is fail-fast: if Postgres can't be reached, or the schema check/init
itself fails, on_startup() raises and FastAPI/uvicorn refuses to come up —
see on_startup()'s docstring for why a "start anyway and report degraded"
shape is wrong for this specific failure class.
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
from routers import filter_router, history_router, pdf_router, provision_router, search_router, stats_router

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
    """
    Fail-fast on database/schema problems: an app that "starts anyway" with
    no DB or a broken schema doesn't serve traffic correctly, it just fails
    every request instead of failing loudly once at boot. docker-compose.yml
    already has `depends_on: db: condition: service_healthy` (so Postgres is
    up before this container even starts) and `restart: always` on this
    service, so raising here is safe — the container gets restarted by
    Docker instead of limping along "degraded", and a real config problem
    (bad DB_HOST/DB_PORT/credentials, or a schema that failed to apply)
    shows up immediately in `docker logs` / `docker compose ps` instead of
    being discovered later as mysterious request failures. Same pattern as
    scraper-backend's api.py.
    """
    try:
        connection.init_connection_pool()
        with connection.get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
    except Exception as exc:
        print(f"ERROR: could not connect to the database at startup: {exc}")
        raise RuntimeError(
            "api-backend startup aborted: could not connect to Postgres. "
            "Check DATABASE_TYPE/DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD "
            f"and that the database is reachable. Underlying error: {exc}"
        ) from exc

    # ensure_schema() only creates anything the first time it sees a
    # database without the core `cases` table; every later startup (and
    # a shared-DB deployment where scraper-backend already created it)
    # is a cheap no-op / idempotent supplement check. A failure here means
    # the schema is missing/broken AND couldn't be auto-applied (e.g. a
    # permissions issue or a malformed schema.sql) — that's not a state
    # this app should ever serve traffic in.
    try:
        from db.init_db import ensure_schema
        ensure_schema()
    except Exception as exc:
        print(f"ERROR: could not ensure DB schema at startup: {exc}")
        raise RuntimeError(
            "api-backend startup aborted: database schema check/init "
            f"failed. Underlying error: {exc}"
        ) from exc

    # Seeding filter/search-field definitions is not a schema-integrity
    # concern — an unseeded DB just means /api/cases/filters and
    # /api/cases/searches return empty lists until seed_filters() runs
    # (ON CONFLICT DO NOTHING, safe to retry), not that the app is broken.
    # Stays non-fatal.
    try:
        from db.seed_filters import seed as seed_filters
        seed_filters()
    except Exception as e:
        print(f"WARNING: could not seed filter/search-field definitions at startup: {e}")


@app.on_event("shutdown")
def on_shutdown():
    connection.close_connection_pool()


# Filters/stats/history/pdf/acts are static (or /pdf-suffixed) sub-paths of
# /api/cases and must be mounted before search_router, or a request to e.g.
# /api/cases/stats or /api/cases/123/pdf would be swallowed by
# search_router's /api/cases/{case_id:path} wildcard route.
app.include_router(filter_router.router)
app.include_router(stats_router.router)
app.include_router(history_router.router)
app.include_router(pdf_router.router)
app.include_router(provision_router.router)
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
    """
    Verifies the app can actually reach Postgres, not just that the process
    is up — a plain 200 with no DB check would hide a bad DB_HOST/DB_PORT.

    Bad config at boot no longer reaches here at all: on_startup() now fails
    the container before it ever starts serving (see its docstring). This
    endpoint's "degraded" status covers a DB that goes away *after* a
    successful startup (e.g. Postgres restarting, a transient network
    blip) — a real runtime signal, not a substitute for the startup gate.
    """
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
