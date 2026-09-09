"""
scraper-backend: Court Judgment Scraper & Ingestion Engine (rebuild)
-------------------------------------------------------------------------
Standalone FastAPI app — no shared code with api-backend or the old
scraper-backend (docs/scraper-backend-revamp-spec.md §3.2). See that spec
for the full architecture; this file wires up what exists so far:

- Phase 0 (done): schema (db/schema.sql) + connection layer (db/connection.py)
- Phase 1 (done): Supreme Court adapter, orchestrator, OCR + stub extraction +
  promotion pipeline, scraper_router. The stub extraction stage gets replaced
  by a real structured-output LLM call in Phase 3 — see pipeline/extraction.py.
- Phase 2 (done): generic eCourts adapter for all 25 High Courts, driven by
  cr_court_scrape_config (auto-seeded at startup if empty — see
  db/seed_courts.py's ensure_seeded() below — or manually via
  `python -m db.seed_courts`). scraper_router now resolves the adapter +
  state/bench code from that config automatically.
- Phase 3 (done): real OCR fallback (Tesseract, for scanned PDFs with no
  text layer), real structured-output LLM extraction (pipeline/extraction.py,
  replacing the Phase 1 stub — see pipeline/extraction_stub.py), the
  normalization/ package (act/judge/party cleaning), and citation
  finding + treatment reconciliation (pipeline/citator.py).
- Phase 4 (done, separate service): api-backend/ reads what this service
  writes — see legal-db/api-backend/.
- Phase 5 (not yet started): cutover.

Startup auto-applies the schema via db/init_db.py's `ensure_schema()`: it
checks for the core `cases` table and only runs schema.sql the first time
it's missing (a fresh database), so it's safe to call on every container
restart without ever re-running schema.sql's non-idempotent CREATE
TYPE/CREATE TABLE statements against a database that already has them.
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
from fastapi.middleware.cors import CORSMiddleware

from db import connection
from routers import court_config_router, scraper_router

app = FastAPI(
    title="Legal Court Scraper & Ingestion Engine (v2)",
    description="Rebuild in progress — see legal-db/docs/scraper-backend-revamp-spec.md",
    version="2.0.0-phase3",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    # Deliberately non-fatal: if Postgres isn't reachable yet (e.g. the db
    # container is still coming up despite the compose healthcheck, or a
    # transient network blip), the app still starts and serves /health as
    # "degraded" instead of crash-looping. A hard failure here would make
    # /health's own graceful degradation unreachable.
    try:
        connection.init_connection_pool()
    except Exception as e:
        print(f"WARNING: could not initialize DB connection pool at startup: {e}")
        return

    # ensure_schema() only creates anything the first time it sees a
    # database without the core `cases` table; every later restart (and
    # a shared-DB deployment where this already ran once) is a cheap
    # no-op. Non-fatal for the same reason as the pool init above.
    try:
        from db.init_db import ensure_schema
        ensure_schema()
    except Exception as e:
        print(f"WARNING: could not ensure DB schema at startup: {e}")

    # Same startup-safe shape as ensure_schema() above: seeds the Supreme
    # Court + 25 High Courts only the first time cr_courts is empty, so a
    # fresh database is immediately usable (POST /api/scraper/start needs
    # a real court_id to dispatch against) without a separate manual
    # `python -m db.seed_courts` step. Scraper-backend only — it's the
    # actual write-owner of cr_courts/cr_court_scrape_config; api-backend
    # stays read-only and has no seed script of its own.
    try:
        from db.seed_courts import ensure_seeded
        ensure_seeded()
    except Exception as e:
        print(f"WARNING: could not ensure courts are seeded at startup: {e}")


@app.on_event("shutdown")
def on_shutdown():
    connection.close_connection_pool()


app.include_router(court_config_router.router)
app.include_router(scraper_router.router)


STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", response_class=HTMLResponse)
def serve_landing_page():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse("<h1>Legal Court Scraper & Ingestion Engine (v2)</h1><p>Visit <a href='/docs'>/docs</a>.</p>")


@app.get("/health")
def health_check():
    """
    Verifies the app can actually reach Postgres, not just that the process
    is up — a plain 200 with no DB check would hide a bad DB_HOST/DB_PORT.
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
        "version": "2.0.0-phase3",
        "database": db_status,
    }
