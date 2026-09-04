"""
API Backend: Case Search & Citator FastAPI Application
--------------------------------------------------------
Orchestrates:
- 🔍 Legal Search Router   : QUERY /api/cases, /api/cases/{case_id}
- 🏷️ Dynamic Filter Router : /api/cases/filters, /api/cases/searches
- 🐘 Automatic Database Startup Verification (own copy of the DB schema)
- 🏊 Pooled Read Connections (initialized on startup, closed on shutdown)
- 🌐 Static Landing Page at "/"

Read-only, DB-only service — no Playwright/scraping dependencies live here.
Court scraping is a separate, standalone service: see ../scraper-backend.
"""

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Make this directory's own modules (db_manager, routers/) importable
# regardless of the process's cwd when it was launched.
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

import db_manager
from routers import filter_router, history_router, search_router, stats_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("api_backend")


# ============================================================
# LIFESPAN: SCHEMA VERIFICATION + CONNECTION POOL LIFECYCLE
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Checking and verifying PostgreSQL tables & schema on startup...")
    try:
        db_manager.init_database(drop_existing=False)
        logger.info("Database schema & tables verified.")
    except Exception:
        logger.exception("Database schema verification failed on startup — check PostgreSQL connectivity.")

    db_manager.init_connection_pool()
    logger.info("Database connection pool initialized.")

    yield

    db_manager.close_connection_pool()
    logger.info("Database connection pool closed.")


app = FastAPI(
    title="Legal Judgment Intelligence API",
    description="Enterprise API providing multi-field case search, citator treatments, and dynamic facets.",
    version="1.0.0",
    lifespan=lifespan,
)

# Allow CORS for development & cross-origin frontend support.
# No credentialed (cookie/session) requests are used by this API, so
# allow_credentials is intentionally omitted — combining it with a
# wildcard origin is a common misconfiguration browsers reject anyway.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# GLOBAL EXCEPTION HANDLER (backstop for anything routers don't catch)
# ============================================================

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


# ============================================================
# MOUNT MODULAR ROUTERS
# ============================================================

# Dynamic Filters & Search-Builder Metadata Router
# (/api/cases/filters, /api/cases/searches) — must be mounted before
# search_router so these static paths are matched before the
# /api/cases/{case_id} wildcard route.
app.include_router(filter_router.router)

# Aggregate stats + per-user recent search history (/api/cases/stats,
# /api/cases/history) — same reason: static sub-paths of /api/cases, so these
# must also be mounted before search_router's /api/cases/{case_id} wildcard,
# or a request to e.g. /api/cases/stats would be swallowed by get_case_detail("stats").
app.include_router(stats_router.router)
app.include_router(history_router.router)

# Legal Search & Citator Router (QUERY /api/cases, /api/cases/{case_id})
app.include_router(search_router.router)


# ============================================================
# STATIC LANDING PAGE
# ============================================================

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", response_class=HTMLResponse, tags=["System"])
def serve_landing_page():
    """Serves a minimal status page confirming the API backend is up and running."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse("<h1>Legal Judgment Intelligence API</h1><p>Visit <a href='/docs'>/docs</a> for API specifications.</p>")


# Health Check Endpoint
@app.get("/health", tags=["System"])
def health_check():
    return {"status": "healthy", "service": "api-backend", "version": "1.0.0"}
