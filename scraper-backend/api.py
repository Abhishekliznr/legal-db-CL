"""
scraper-backend: Court Judgment Scraper & Ingestion Engine (rebuild)
-------------------------------------------------------------------------
Standalone FastAPI app — no shared code with api-backend or the old
scraper-backend (docs/scraper-backend-revamp-spec.md §3.2). See that spec
for the full architecture; this file wires up what exists so far:

- Phase 0 (done): schema (db/schema.sql) + connection layer (db/connection.py)
- Phase 1 (done): Supreme Court adapter, orchestrator, OCR + promotion
  pipeline, scraper_router.
- Phase 2 (done): generic eCourts adapter for all 25 High Courts, driven by
  cr_court_scrape_config (auto-seeded at startup if empty — see
  db/seed_courts.py's ensure_seeded() below — or manually via
  `python -m db.seed_courts`). scraper_router now resolves the adapter +
  state/bench code from that config automatically.
- Phase 3 (done): real OCR fallback (Tesseract, for scanned PDFs with no
  text layer), the normalization/ package (act/judge/party cleaning).
  Rewritten 2026-09-08: LLM-based extraction (pipeline/extraction.py) was
  replaced by regex-first extraction straight into promotion
  (pipeline/regex_extraction.py) plus a separate post-promotion enrichment
  pass (pipeline/llm_enrichment.py) for case_note/industries/provisions.
- Phase 4 (done, separate service): api-backend/ reads what this service
  writes — see legal-db/api-backend/.
- Phase 5 (done): cutover — this is the only scraper-backend now, no
  separate old service or -v2 directory.

Startup auto-applies the schema via db/init_db.py's `ensure_schema()`: it
checks for the core `cases` table and only runs schema.sql the first time
it's missing (a fresh database), so it's safe to call on every container
restart without ever re-running schema.sql's non-idempotent CREATE
TYPE/CREATE TABLE statements against a database that already has them.

Startup is fail-fast: if Postgres can't be reached, or the schema check/init
itself fails, on_startup() raises and FastAPI/uvicorn refuses to come up —
see on_startup()'s docstring for why a "start anyway and report degraded"
shape is wrong for this specific failure class.
"""

import logging
import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

# Without this, Python's root logger defaults to WARNING — every
# logger.info() call across this service (batch progress in
# orchestrator/batch_runner.py, diagnostic table-header dumps in
# adapters/supreme_court/adapter.py, etc.) would be silently swallowed.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)-40s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# The azure-storage-blob/azure-core SDK's HttpLoggingPolicy logs every
# request/response — method, full URL, and every header — at INFO level by
# default. Once the line above raises the root logger to INFO, that policy
# starts firing on every blob upload, drowning out the pipeline's own
# stage-by-stage logs in raw HTTP traffic (and, since Azure's auth headers
# ride along in that same dump, it's a mild credential-hygiene problem too,
# not just noise). storage/azure_blob.py already logs the actual reason
# for any real upload failure at ERROR level, so nothing is lost by
# quieting the SDK's own request/response tracing down to WARNING+.
for _noisy_logger_name in (
    "azure",
    "azure.core.pipeline.policies.http_logging_policy",
    "urllib3",
    "urllib3.connectionpool",
):
    logging.getLogger(_noisy_logger_name).setLevel(logging.WARNING)

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from db import connection
from routers import court_config_router, scraper_router

app = FastAPI(
    title="Legal Court Scraper & Ingestion Engine (v2)",
    description="Rebuild in progress — see legal-db/docs/scraper-backend-revamp-spec.md",
    version="2.0.0",
)

logger = logging.getLogger("scraper_backend_v2.startup")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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
    being discovered later as mysterious request failures.
    """
    try:
        connection.init_connection_pool()
        with connection.get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
    except Exception as exc:
        logger.exception("Could not connect to the database at startup")
        raise RuntimeError(
            "scraper-backend startup aborted: could not connect to Postgres. "
            "Check DATABASE_TYPE/DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD "
            f"and that the database is reachable. Underlying error: {exc}"
        ) from exc

    # ensure_schema() only creates anything the first time it sees a
    # database without the core `cases` table; every later restart (and
    # a shared-DB deployment where this already ran once) is a cheap
    # no-op. A failure here means the schema is missing AND couldn't be
    # auto-applied (e.g. a permissions issue or a malformed schema.sql) —
    # that's not a state the app should ever serve traffic in.
    try:
        from db.init_db import ensure_schema
        ensure_schema()
    except Exception as exc:
        logger.exception("Could not ensure DB schema at startup")
        raise RuntimeError(
            "scraper-backend startup aborted: database schema check/init "
            f"failed. Underlying error: {exc}"
        ) from exc

    # Seeds the Supreme Court + 25 High Courts only the first time cr_courts
    # is empty, so a fresh database is immediately usable (POST
    # /api/scraper/start needs a real court_id to dispatch against) without
    # a separate manual `python -m db.seed_courts` step. Scraper-backend
    # only — it's the actual write-owner of cr_courts/cr_court_scrape_config;
    # api-backend stays read-only and has no seed script of its own.
    #
    # Unlike the DB connection and schema checks above, this stays
    # non-fatal: reference data (courts) isn't a schema-integrity concern —
    # an empty cr_courts table just means POST /api/scraper/start needs a
    # manual `python -m db.seed_courts` before it's usable, not that the
    # app is in a broken state.
    try:
        from db.seed_courts import ensure_seeded
        ensure_seeded()
    except Exception:
        logger.exception("Could not ensure courts are seeded at startup")

    # Assumes a single uvicorn worker (true locally and in the Dockerfile):
    # with several, one worker restarting would close batches another
    # worker is still running.
    try:
        from db.scrape_jobs import fail_orphaned_batches
        for batch in fail_orphaned_batches():
            logger.warning(
                "Batch %s was RUNNING when the server restarted — marked %s",
                batch["batch_id"], batch["status"],
            )
    except Exception:
        logger.exception("Could not close batches orphaned by a previous run at startup")


@app.on_event("shutdown")
def on_shutdown():
    connection.close_connection_pool()


app.include_router(court_config_router.router)
app.include_router(scraper_router.router)


STATIC_DIR = Path(__file__).resolve().parent / "static"

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def serve_landing_page():
    index_file = STATIC_DIR / "index.html"

    if index_file.exists():
        return FileResponse(index_file)

    return HTMLResponse(
        "<h1>Legal Court Scraper & Ingestion Engine (v2)</h1>"
        "<p>Visit <a href='/docs'>/docs</a>.</p>"
    )


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
        "service": "scraper-backend",
        "version": "2.0.0",
        "database": db_status,
    }
