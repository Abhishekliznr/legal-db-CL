"""
FastAPI Backend Server & Legal Intelligence Search Engine
---------------------------------------------------------
Provides REST API endpoints for multi-field legal search, filters, case detail view,
citations graph exploration, treatment status badges, and local PDF streaming directly
from stored pdf_path references in PostgreSQL.
"""

import os
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Query, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    from backend import db_manager, scraper_pipeline
except ImportError:
    import db_manager
    import scraper_pipeline

class ScraperJobRequest(BaseModel):
    court_id: str = Field("SCIN", description="Court code: SCIN (Supreme Court of India)")
    from_date: Optional[str] = Field(None, description="Start date (YYYY-MM-DD or DD-MM-YYYY)")
    to_date: Optional[str] = Field(None, description="End date (YYYY-MM-DD or DD-MM-YYYY)")
    upload_azure: bool = Field(True, description="Upload PDFs and Bronze/Silver JSON to Azure Blob Storage")
    stream_cloud: bool = Field(True, description="Stream directly to Azure and delete local temporary PDFs")
    extract_metadata: bool = Field(True, description="Automatically run AI CaseNote and Statutory metadata extractor")

app = FastAPI(
    title="Legal Judgment Intelligence & Admin Scraper API",
    description="High-performance legal search and automated scraping pipeline with Azure Blob Storage and PostgreSQL ingestion",
    version="2.1.0"
)

# Allow CORS for development flexibility
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
    """Automatically creates all database tables, extensions, and seed data on server startup if not present."""
    try:
        print("🔍 Checking and verifying PostgreSQL tables & schema on startup...")
        db_manager.init_database(drop_existing=False)
        print("✅ Database schema & tables verified!")
    except Exception as e:
        print(f"⚠️ Warning: Auto-initialization on startup encountered: {e}. Check PostgreSQL connection.")


# ============================================================
# API ENDPOINTS
# ============================================================

@app.get("/api/filters")
def get_filters():
    """Returns dynamic list of available dropdown filters (Courts, Years, Treatment Statuses, Judges, Acts)."""
    conn = db_manager.get_connection()
    try:
        with conn.cursor() as cur:
            # 1. Courts
            cur.execute("SELECT court_id, name FROM courts ORDER BY name;")
            courts = [{"court_id": r[0], "name": r[1]} for r in cur.fetchall()]

            # 2. Years
            cur.execute("""
                SELECT DISTINCT COALESCE(
                    EXTRACT(YEAR FROM judgment_date)::INT,
                    EXTRACT(YEAR FROM registration_date)::INT,
                    2025
                ) AS year
                FROM cases
                ORDER BY year DESC;
            """)
            years = [r[0] for r in cur.fetchall() if r[0] is not None]
            if not years:
                years = [2025, 2024, 2023, 2022, 2021]

            # 3. Treatment Statuses
            cur.execute("SELECT DISTINCT treatment_status FROM cases WHERE treatment_status IS NOT NULL ORDER BY treatment_status;")
            treatments = [r[0] for r in cur.fetchall()]
            if not treatments:
                treatments = ["GOOD_LAW", "OVERRULED", "DOUBTED", "DISTINGUISHED"]

            # 4. Canonical Judges
            cur.execute("SELECT DISTINCT canonical_name FROM judge_master ORDER BY canonical_name LIMIT 100;")
            judges = [r[0] for r in cur.fetchall()]

            # 5. Canonical Acts
            cur.execute("SELECT DISTINCT canonical_name FROM act_master ORDER BY canonical_name LIMIT 100;")
            acts = [r[0] for r in cur.fetchall()]

            return {
                "courts": courts,
                "years": years,
                "treatments": treatments,
                "judges": judges,
                "acts": acts
            }
    finally:
        conn.close()


@app.get("/api/cases/search")
def search_cases(
    q: Optional[str] = Query(None, description="Free text query across all legal fields"),
    court_id: Optional[str] = Query(None, description="Filter by Court ID"),
    year: Optional[int] = Query(None, description="Filter by Judgment Year"),
    treatment_status: Optional[str] = Query(None, description="Filter by Treatment Status (GOOD_LAW, OVERRULED, DOUBTED, DISTINGUISHED)"),
    judge: Optional[str] = Query(None, description="Filter by Judge Name"),
    act: Optional[str] = Query(None, description="Filter by Canonical Act"),
    is_reported: Optional[bool] = Query(None, description="Filter by Reported Status"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(10, ge=1, le=100, description="Results per page")
):
    """
    Multi-field legal judgment search with filters and pagination.
    Searches across: Case Name, Case Number, CNR/Diary No, Neutral Citation,
    AI Case Notes, Parties, Judges, Advocates, Provisions, and Citations.
    """
    conn = db_manager.get_connection()
    try:
        with conn.cursor() as cur:
            where_clauses = []
            params: List[Any] = []

            # 1. Free Text Search
            if q and q.strip():
                query_str = f"%{q.strip()}%"
                text_condition = """(
                    c.case_number ILIKE %s
                    OR c.cnr ILIKE %s
                    OR c.diary_number ILIKE %s
                    OR c.neutral_citation ILIKE %s
                    OR c.case_note_ai ILIKE %s
                    OR EXISTS (SELECT 1 FROM parties p WHERE p.case_id = c.id AND p.name ILIKE %s)
                    OR EXISTS (SELECT 1 FROM case_judges cj JOIN judge_master jm ON cj.judge_id = jm.id WHERE cj.case_id = c.id AND jm.canonical_name ILIKE %s)
                    OR EXISTS (SELECT 1 FROM case_advocates ca JOIN advocates a ON ca.advocate_id = a.id WHERE ca.case_id = c.id AND a.name ILIKE %s)
                    OR EXISTS (SELECT 1 FROM provisions pr JOIN act_master am ON pr.act_id = am.id WHERE pr.case_id = c.id AND (am.canonical_name ILIKE %s OR pr.raw_act_name ILIKE %s OR pr.section ILIKE %s))
                    OR EXISTS (SELECT 1 FROM citations cit WHERE cit.citing_case_id = c.id AND cit.raw_citation_text ILIKE %s)
                )"""
                where_clauses.append(text_condition)
                params.extend([query_str] * 12)

            # 2. Filter: Court
            if court_id and court_id.strip():
                where_clauses.append("c.court_id = %s")
                params.append(court_id.strip())

            # 3. Filter: Year
            if year:
                where_clauses.append("EXTRACT(YEAR FROM c.judgment_date) = %s")
                params.append(year)

            # 4. Filter: Treatment Status
            if treatment_status and treatment_status.strip():
                where_clauses.append("c.treatment_status = %s")
                params.append(treatment_status.strip())

            # 5. Filter: Judge
            if judge and judge.strip():
                where_clauses.append("EXISTS (SELECT 1 FROM case_judges cj JOIN judge_master jm ON cj.judge_id = jm.id WHERE cj.case_id = c.id AND jm.canonical_name ILIKE %s)")
                params.append(f"%{judge.strip()}%")

            # 6. Filter: Act
            if act and act.strip():
                where_clauses.append("EXISTS (SELECT 1 FROM provisions pr JOIN act_master am ON pr.act_id = am.id WHERE pr.case_id = c.id AND am.canonical_name ILIKE %s)")
                params.append(f"%{act.strip()}%")

            # 7. Filter: Reported Status
            if is_reported is not None:
                where_clauses.append("c.is_reported = %s")
                params.append(is_reported)

            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            # Count total matching records
            count_sql = f"SELECT COUNT(DISTINCT c.id) FROM cases c {where_sql};"
            cur.execute(count_sql, params)
            total_records = cur.fetchone()[0]

            # Calculate pagination
            offset = (page - 1) * page_size
            total_pages = (total_records + page_size - 1) // page_size if total_records > 0 else 1

            # Fetch matching cases
            query_sql = f"""
                SELECT c.id, c.diary_number, c.case_number, c.cnr, c.neutral_citation,
                       c.judgment_date, c.case_category, c.document_type,
                       c.treatment_status, c.overruled, c.is_reported, c.reporting_status,
                       c.case_note_ai, c.pdf_path, c.pdf_url,
                       co.name AS court_name
                FROM cases c
                LEFT JOIN courts co ON c.court_id = co.court_id
                {where_sql}
                ORDER BY c.judgment_date DESC NULLS LAST, c.created_at DESC
                LIMIT %s OFFSET %s;
            """
            cur.execute(query_sql, params + [page_size, offset])
            rows = cur.fetchall()

            results = []
            for r in rows:
                case_id = str(r[0])

                # Fetch parties summary
                cur.execute("SELECT name, role FROM parties WHERE case_id = %s LIMIT 4;", (case_id,))
                parties = [{"name": p[0], "role": p[1]} for p in cur.fetchall()]

                # Fetch judges summary
                cur.execute("""
                    SELECT jm.canonical_name, cj.role
                    FROM case_judges cj JOIN judge_master jm ON cj.judge_id = jm.id
                    WHERE cj.case_id = %s;
                """, (case_id,))
                judges = [{"name": j[0], "role": j[1]} for j in cur.fetchall()]

                # Fetch provisions sample
                cur.execute("""
                    SELECT COALESCE(am.canonical_name, pr.raw_act_name) AS act_name, pr.section, pr.provision_full
                    FROM provisions pr 
                    LEFT JOIN act_master am ON pr.act_id = am.id 
                    WHERE pr.case_id = %s LIMIT 3;
                """, (case_id,))
                provisions = [{"act_name": p[0], "section": p[1], "full_text": p[2]} for p in cur.fetchall()]

                # Fetch citations sample
                cur.execute("""
                    SELECT raw_citation_text, treatment_type 
                    FROM citations 
                    WHERE citing_case_id = %s LIMIT 3;
                """, (case_id,))
                citations = [{"citation": cit[0], "treatment": cit[1]} for cit in cur.fetchall()]

                results.append({
                    "id": case_id,
                    "diary_number": r[1],
                    "case_number": r[2],
                    "cnr": r[3],
                    "neutral_citation": r[4],
                    "judgment_date": r[5].strftime("%Y-%m-%d") if r[5] else None,
                    "case_category": r[6],
                    "document_type": r[7],
                    "treatment_status": r[8] or "GOOD_LAW",
                    "overruled": r[9],
                    "is_reported": r[10],
                    "reporting_status": r[11],
                    "case_note_ai": r[12],
                    "pdf_path": r[13],
                    "pdf_url": r[14],
                    "court_name": r[15],
                    "parties": parties,
                    "judges": judges,
                    "provisions": provisions,
                    "citations": citations
                })

            return {
                "total": total_records,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "results": results
            }
    finally:
        conn.close()


@app.get("/api/cases/{case_id}")
def get_case_detail(case_id: str):
    """Returns complete metadata, relations, citations, and treatment status for a single case judgment."""
    conn = db_manager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.id, c.diary_number, c.case_id_code, c.cnr, c.case_number,
                       c.case_category, c.registration_date, c.judgment_date,
                       c.neutral_citation, c.disposal_nature, c.result_text,
                       c.case_age_days, c.language, c.document_type, c.confidence_score,
                       c.case_note_ai, c.treatment_status, c.overruled, c.is_reported,
                       c.reporting_status, c.reporting_source, c.pdf_path, c.pdf_url, c.source_page,
                       co.name AS court_name, co.bench_seat
                FROM cases c
                LEFT JOIN courts co ON c.court_id = co.court_id
                WHERE c.id = %s;
            """, (case_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Case record not found")

            # Parties
            cur.execute("SELECT name, role, party_type FROM parties WHERE case_id = %s;", (case_id,))
            parties = [{"name": p[0], "role": p[1], "party_type": p[2]} for p in cur.fetchall()]

            # Judges
            cur.execute("""
                SELECT jm.canonical_name, cj.role
                FROM case_judges cj JOIN judge_master jm ON cj.judge_id = jm.id
                WHERE cj.case_id = %s;
            """, (case_id,))
            judges = [{"name": j[0], "role": j[1]} for j in cur.fetchall()]

            # Advocates
            cur.execute("""
                SELECT a.name, ca.party_role
                FROM case_advocates ca JOIN advocates a ON ca.advocate_id = a.id
                WHERE ca.case_id = %s;
            """, (case_id,))
            advocates = [{"name": a[0], "party_role": a[1]} for a in cur.fetchall()]

            # Provisions
            cur.execute("""
                SELECT COALESCE(am.canonical_name, pr.raw_act_name) AS act_name, pr.section, pr.provision_full
                FROM provisions pr 
                LEFT JOIN act_master am ON pr.act_id = am.id 
                WHERE pr.case_id = %s;
            """, (case_id,))
            provisions = [{"act_name": p[0], "section": p[1], "full_text": p[2]} for p in cur.fetchall()]

            # Constitutional Articles
            cur.execute("SELECT article FROM case_articles WHERE case_id = %s;", (case_id,))
            articles = [a[0] for a in cur.fetchall()]

            # Citations (Outgoing citations in this judgment)
            cur.execute("""
                SELECT id, raw_citation_text, reporter_type, treatment_type, confidence_score, context_snippet
                FROM citations
                WHERE citing_case_id = %s;
            """, (case_id,))
            citations = [{
                "id": str(c[0]),
                "raw_citation_text": c[1],
                "reporter_type": c[2],
                "treatment_type": c[3],
                "confidence_score": float(c[4]) if c[4] else None,
                "context_snippet": c[5]
            } for c in cur.fetchall()]

            # Incoming Citations (Cases that cite this judgment)
            cur.execute("""
                SELECT c.id, c.case_number, c.judgment_date, cit.treatment_type, cit.context_snippet
                FROM citations cit
                JOIN cases c ON cit.citing_case_id = c.id
                WHERE cit.cited_case_id = %s;
            """, (case_id,))
            cited_by = [{
                "citing_case_id": str(cb[0]),
                "case_number": cb[1],
                "judgment_date": cb[2].strftime("%Y-%m-%d") if cb[2] else None,
                "treatment_type": cb[3],
                "context_snippet": cb[4]
            } for cb in cur.fetchall()]

            return {
                "id": str(row[0]),
                "diary_number": row[1],
                "case_id_code": row[2],
                "cnr": row[3],
                "case_number": row[4],
                "case_category": row[5],
                "registration_date": row[6].strftime("%Y-%m-%d") if row[6] else None,
                "judgment_date": row[7].strftime("%Y-%m-%d") if row[7] else None,
                "neutral_citation": row[8],
                "disposal_nature": row[9],
                "result_text": row[10],
                "case_age_days": row[11],
                "language": row[12],
                "document_type": row[13],
                "confidence_score": float(row[14]) if row[14] else None,
                "case_note_ai": row[15],
                "treatment_status": row[16] or "GOOD_LAW",
                "overruled": row[17],
                "is_reported": row[18],
                "reporting_status": row[19],
                "reporting_source": row[20],
                "pdf_path": row[21],
                "pdf_url": row[22],
                "source_page": row[23],
                "court_name": row[24],
                "bench_seat": row[25],
                "parties": parties,
                "judges": judges,
                "advocates": advocates,
                "provisions": provisions,
                "articles": articles,
                "citations": citations,
                "cited_by": cited_by
            }
    finally:
        conn.close()


def resolve_pdf_file_path(stored_path: Optional[str]) -> Optional[Path]:
    """
    Dynamically resolves the PDF file path across Docker (/app/backend/app/...) 
    and Windows host (C:\\D-Drive\\HC-Scraper\\backend\\app\\...) environments.
    """
    if not stored_path:
        return None

    p = Path(stored_path)
    if p.exists() and p.is_file():
        return p

    # Extract filename from stored path
    filename = Path(stored_path.replace("\\", "/")).name
    base_dir = Path(__file__).resolve().parent  # /app/backend or c:\D-Drive\HC-Scraper\backend

    candidate_paths = [
        base_dir / "app" / "SUPREME_COURT_OF_INDIA_SCRAPER" / "pdf" / filename,
        base_dir / "app" / "pdf" / filename,
        base_dir.parent / "backend" / "app" / "SUPREME_COURT_OF_INDIA_SCRAPER" / "pdf" / filename,
        Path("/app/backend/app/SUPREME_COURT_OF_INDIA_SCRAPER/pdf") / filename,
        Path("/app/backend/app/pdf") / filename,
        Path("C:/D-Drive/HC-Scraper/backend/app/SUPREME_COURT_OF_INDIA_SCRAPER/pdf") / filename,
    ]

    for cand in candidate_paths:
        if cand.exists() and cand.is_file():
            return cand

    # Recursive search across all court PDF directories
    court_app_dir = base_dir / "app"
    if court_app_dir.exists():
        for found in court_app_dir.glob(f"**/pdf/{filename}"):
            if found.is_file():
                return found

    return None


@app.get("/api/cases/{case_id}/pdf")
def serve_case_pdf(case_id: str):
    """
    Streams the physical PDF file for a case judgment directly from its stored pdf_path.
    Resolves seamlessly inside Docker containers and across operating systems.
    """
    conn = db_manager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pdf_path, pdf_url FROM cases WHERE id = %s;", (case_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="No case record found.")

            stored_pdf_path = row[0]
            pdf_url = row[1]

            resolved_path = resolve_pdf_file_path(stored_pdf_path)
            if resolved_path and resolved_path.exists():
                return FileResponse(
                    path=str(resolved_path),
                    media_type="application/pdf",
                    filename=resolved_path.name
                )

            if pdf_url:
                from fastapi.responses import RedirectResponse
                return RedirectResponse(url=pdf_url)

            raise HTTPException(
                status_code=404,
                detail=f"PDF file not found on local disk: {stored_pdf_path}"
            )
    finally:
        conn.close()


# ============================================================
# ADMIN SCRAPER & PIPELINE AUTOMATION ENDPOINTS
# ============================================================

@app.get("/api/scraper/courts")
def get_supported_courts():
    """Returns list of supported courts for scraping."""
    return {
        "courts": [
            {
                "court_id": "SCIN",
                "court_name": "Supreme Court of India",
                "status": "ACTIVE",
                "description": "Scrapes judgments from sci.gov.in by date range with automated captcha solver"
            },
            {
                "court_id": "DHC",
                "court_name": "Delhi High Court",
                "status": "READY",
                "description": "Delhi High Court Judgment Portal"
            },
            {
                "court_id": "ALHC",
                "court_name": "Allahabad High Court",
                "status": "READY",
                "description": "Allahabad High Court Judgment Portal"
            }
        ]
    }


@app.post("/api/scraper/start")
def trigger_scraper_job(req: ScraperJobRequest):
    """
    Triggers an asynchronous scraping, AI metadata extraction, and DB ingestion pipeline job.
    Returns the unique job_id for status tracking.
    """
    if not req.from_date or not req.to_date:
        raise HTTPException(status_code=400, detail="from_date and to_date are required (e.g. '2025-01-01').")

    job_id = scraper_pipeline.start_scraper_job(
        court_id=req.court_id,
        from_date=req.from_date,
        to_date=req.to_date,
        upload_azure=req.upload_azure,
        stream_cloud=req.stream_cloud,
        extract_metadata=req.extract_metadata
    )

    return {
        "status": "success",
        "job_id": job_id,
        "court_id": req.court_id,
        "from_date": req.from_date,
        "to_date": req.to_date,
        "message": f"Scraper job {job_id} launched successfully in background."
    }


@app.get("/api/scraper/status/{job_id}")
def get_scraper_job_status(job_id: str):
    """
    Returns real-time status, log stream, cases count, and Azure Blob URLs for an active or completed job.
    """
    # 1. Check in-memory active jobs first for instant log streaming
    if job_id in scraper_pipeline.ACTIVE_JOBS:
        mem_job = scraper_pipeline.ACTIVE_JOBS[job_id]
        db_job = db_manager.get_scraper_job(job_id) or {}
        return {
            "job_id": job_id,
            "status": mem_job.get("status", "RUNNING"),
            "court_id": mem_job.get("court_id"),
            "from_date": str(mem_job.get("from_date")),
            "to_date": str(mem_job.get("to_date")),
            "total_cases_found": db_job.get("total_cases_found", 0),
            "new_cases_scraped": db_job.get("new_cases_scraped", 0),
            "skipped_cases": db_job.get("skipped_cases", 0),
            "bronze_blob_url": db_job.get("bronze_blob_url"),
            "silver_blob_url": db_job.get("silver_blob_url"),
            "logs": mem_job.get("logs", []),
            "error_message": db_job.get("error_message"),
            "started_at": str(db_job.get("started_at")),
            "completed_at": str(db_job.get("completed_at"))
        }

    # 2. Check PostgreSQL for completed or past jobs
    job = db_manager.get_scraper_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"No scraper job found with ID: {job_id}")

    return {
        "job_id": str(job["job_id"]),
        "status": job["status"],
        "court_id": job["court_id"],
        "court_name": job.get("court_name", "Supreme Court of India"),
        "from_date": str(job["from_date"]) if job["from_date"] else None,
        "to_date": str(job["to_date"]) if job["to_date"] else None,
        "total_cases_found": job["total_cases_found"],
        "new_cases_scraped": job["new_cases_scraped"],
        "skipped_cases": job["skipped_cases"],
        "bronze_blob_url": job["bronze_blob_url"],
        "silver_blob_url": job["silver_blob_url"],
        "logs": job.get("logs") or [],
        "error_message": job["error_message"],
        "started_at": job["started_at"].isoformat() if job["started_at"] else None,
        "completed_at": job["completed_at"].isoformat() if job["completed_at"] else None
    }


@app.get("/api/scraper/jobs")
def list_scraper_jobs(limit: int = Query(50, ge=1, le=200)):
    """Returns history of past scraping jobs for admin dashboard."""
    jobs = db_manager.list_scraper_jobs(limit=limit)
    return {
        "total": len(jobs),
        "jobs": jobs
    }


@app.post("/api/scraper/stop/{job_id}")
def cancel_scraper_job(job_id: str):
    """Signals cancellation to an active background scraping job."""
    stopped = scraper_pipeline.stop_scraper_job(job_id)
    if stopped:
        return {"status": "success", "job_id": job_id, "message": "Scraper job cancelled."}
    return {"status": "not_active", "job_id": job_id, "message": "Job is not currently active in memory."}


# Mount static frontend UI directory
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if not FRONTEND_DIR.exists():
    FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
if not FRONTEND_DIR.exists():
    FRONTEND_DIR = Path(__file__).resolve().parent / "web"
FRONTEND_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def index_page():
    """Serves the main Legal Search Interface."""
    index_html_path = FRONTEND_DIR / "index.html"
    if index_html_path.exists():
        return HTMLResponse(content=index_html_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Legal Intelligence API running.</h1>")


if __name__ == "__main__":
    import uvicorn
    import sys
    # Add root and backend to sys.path so direct execution works from any folder
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    uvicorn.run("api:app" if Path("api.py").exists() else "backend.api:app", host="0.0.0.0", port=3000, reload=True)
