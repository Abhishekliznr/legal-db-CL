"""
FastAPI Backend Application Entrypoint & Modular Router Mount
-------------------------------------------------------------
Orchestrates:
- 🕷️ Court Scraper Router  : /api/scraper/*
- 🔍 Legal Search Router   : /api/cases/*, /api/stats
- 🏷️ Dynamic Filter Router : /api/filters, /api/cases/facets
- 🐘 Automatic Database Startup Verification
- 🌐 Frontend Static Asset Serving
"""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

try:
    from backend import db_manager
    from backend.routers import scraper_router, filter_router, search_router
except ImportError:
    import db_manager
    from routers import scraper_router, filter_router, search_router

app = FastAPI(
    title="Legal Judgment Intelligence & Court Scraper Engine",
    description="Enterprise API providing multi-field search, citator treatments, dynamic facets, and automated background scraping.",
    version="2.5.0"
)

# Allow CORS for development & cross-origin frontend support
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

# 1. Dedicated Court Scraper Router (/api/scraper/*)
app.include_router(scraper_router.router)

# 2. Dedicated Dynamic Filters & Facets Router (/api/filters, /api/cases/facets)
app.include_router(filter_router.router)

# 3. Dedicated Legal Search & Citator Router (/api/cases/*, /api/stats)
app.include_router(search_router.router)


# ============================================================
# STATIC FRONTEND SERVING
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"
if not FRONTEND_DIR.exists():
    FRONTEND_DIR = BASE_DIR / "frontend"

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def serve_frontend_ui():
        """Serves the standalone Citator & Scraper web dashboard."""
        index_file = FRONTEND_DIR / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return HTMLResponse("<h1>Legal Judgment Intelligence Backend API</h1><p>Visit <a href='/docs'>/docs</a> for API specifications.</p>")
else:
    @app.get("/", response_class=HTMLResponse)
    def serve_root():
        return HTMLResponse("<h1>Legal Judgment Intelligence Backend API</h1><p>Visit <a href='/docs'>/docs</a> for interactive Swagger API specifications.</p>")


# Health Check Endpoint
@app.get("/health")
def health_check():
    return {"status": "healthy", "service": "legal-db-backend", "version": "2.5.0"}
