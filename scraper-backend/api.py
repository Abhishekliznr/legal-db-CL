"""
Scraper Backend: Court Judgment Scraper & Ingestion FastAPI Application
-------------------------------------------------------------------------
Orchestrates:
- 🕷️ Court Scraper Router  : /api/scraper/start|status|jobs|cancel
- 🐘 Automatic Database Startup Verification (shared schema with api-backend)
- 🌐 Static Landing Page at "/"

Heavy service (Playwright, PyMuPDF, ddddocr, Azure Blob) — kept separate
from api-backend so the lightweight read/search API never needs a browser
runtime. Both services point at the same PostgreSQL database.
"""

import sys
from pathlib import Path

# Make the repo-root `shared/` package importable whether this runs via
# Docker (PYTHONPATH=/app) or directly from a local checkout.
_script_dir = Path(__file__).resolve().parent
_project_root = _script_dir.parent
for _p in [str(_project_root), str(_script_dir)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from shared import db_manager
from routers import scraper_router

app = FastAPI(
    title="Legal Court Scraper & Ingestion Engine",
    description="Background court-judgment scraping, AI metadata extraction, and Azure Blob archiving.",
    version="1.0.0"
)

# Allow CORS for development & cross-origin admin dashboard access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# STARTUP EVENT: AUTO-INITIALIZE SCHEMA & TABLES IF MISSING
# ============================================================

@app.on_event("startup")
def on_startup():
    """Automatically verifies and creates all PostgreSQL tables, extensions, and seed data on startup."""
    try:
        print("🔍 Checking and verifying PostgreSQL tables & schema on startup...")
        db_manager.init_database(drop_existing=False)
        print("✅ Database schema & tables verified!")
    except Exception as e:
        print(f"⚠️ Warning: Auto-initialization on startup encountered: {e}. Check PostgreSQL connection.")


# ============================================================
# MOUNT MODULAR ROUTERS
# ============================================================

app.include_router(scraper_router.router)


# ============================================================
# STATIC LANDING PAGE
# ============================================================

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", response_class=HTMLResponse)
def serve_landing_page():
    """Serves a minimal status page confirming the scraper backend is up and running."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse("<h1>Legal Court Scraper & Ingestion Engine</h1><p>Visit <a href='/docs'>/docs</a> for API specifications.</p>")


# Health Check Endpoint
@app.get("/health")
def health_check():
    return {"status": "healthy", "service": "scraper-backend", "version": "1.0.0"}
