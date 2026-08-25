"""
FastAPI Backend Server & Search Engine for Legal Scraper Metadata
-------------------------------------------------------------------
Provides REST API endpoints for multi-field legal search, filters, case detail view,
and local PDF streaming directly from stored pdf_path references in PostgreSQL.
"""

import os
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import db_manager

app = FastAPI(
    title="Legal Judgment Search API",
    description="High-performance legal search API querying PostgreSQL legal_db",
    version="1.0.0"
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
# API ENDPOINTS
# ============================================================

@app.get("/api/filters")
def get_filters():
    """Returns dynamic list of available dropdown filters (Courts, Years, Subjects, Judges)."""
    conn = db_manager.get_connection()
    try:
        with conn.cursor() as cur:
            # 1. Courts
            cur.execute("SELECT court_id, name FROM courts ORDER BY name;")
            courts = [{"court_id": r[0], "name": r[1]} for r in cur.fetchall()]

            # 2. Years
            cur.execute("""
                SELECT DISTINCT EXTRACT(YEAR FROM judgment_date)::INT AS year
                FROM cases
                WHERE judgment_date IS NOT NULL
                ORDER BY year DESC;
            """)
            years = [r[0] for r in cur.fetchall()]

            # 3. Subjects
            cur.execute("SELECT DISTINCT subject FROM case_subjects ORDER BY subject;")
            subjects = [r[0] for r in cur.fetchall()]

            # 4. Judges
            cur.execute("SELECT DISTINCT name FROM judges ORDER BY name;")
            judges = [r[0] for r in cur.fetchall()]

            return {
                "courts": courts,
                "years": years,
                "subjects": subjects,
                "judges": judges
            }
    finally:
        conn.close()


@app.get("/api/cases/search")
def search_cases(
    q: Optional[str] = Query(None, description="Free text query across all legal fields"),
    court_id: Optional[str] = Query(None, description="Filter by Court ID"),
    year: Optional[int] = Query(None, description="Filter by Judgment Year"),
    subject: Optional[str] = Query(None, description="Filter by Legal Subject"),
    judge: Optional[str] = Query(None, description="Filter by Judge Name"),
    is_reported: Optional[bool] = Query(None, description="Filter by Reported Status"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(10, ge=1, le=100, description="Results per page")
):
    """
    Multi-field legal judgment search with filters and pagination.
    Searches across: Case Name, Case Number, CNR/Diary No, Neutral Citation,
    AI Case Notes, Parties, Judges, Advocates, Provisions, Articles, and Subjects.
    """
    conn = db_manager.get_connection()
    try:
        with conn.cursor() as cur:
            where_clauses = []
            params: List[Any] = []

            # 1. Text Search across multiple legal fields
            if q and q.strip():
                query_str = f"%{q.strip()}%"
                text_condition = """(
                    c.case_number ILIKE %s
                    OR c.cnr ILIKE %s
                    OR c.diary_number ILIKE %s
                    OR c.neutral_citation ILIKE %s
                    OR c.case_note_ai ILIKE %s
                    OR EXISTS (SELECT 1 FROM parties p WHERE p.case_id = c.id AND p.name ILIKE %s)
                    OR EXISTS (SELECT 1 FROM case_judges cj JOIN judges j ON cj.judge_id = j.id WHERE cj.case_id = c.id AND j.name ILIKE %s)
                    OR EXISTS (SELECT 1 FROM case_advocates ca JOIN advocates a ON ca.advocate_id = a.id WHERE ca.case_id = c.id AND a.name ILIKE %s)
                    OR EXISTS (SELECT 1 FROM provisions pr WHERE pr.case_id = c.id AND (pr.act_name ILIKE %s OR pr.section ILIKE %s))
                    OR EXISTS (SELECT 1 FROM case_articles ca_art WHERE ca_art.case_id = c.id AND ca_art.article ILIKE %s)
                    OR EXISTS (SELECT 1 FROM case_subjects cs WHERE cs.case_id = c.id AND cs.subject ILIKE %s)
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

            # 4. Filter: Subject
            if subject and subject.strip():
                where_clauses.append("EXISTS (SELECT 1 FROM case_subjects cs WHERE cs.case_id = c.id AND cs.subject = %s)")
                params.append(subject.strip())

            # 5. Filter: Judge
            if judge and judge.strip():
                where_clauses.append("EXISTS (SELECT 1 FROM case_judges cj JOIN judges j ON cj.judge_id = j.id WHERE cj.case_id = c.id AND j.name = %s)")
                params.append(judge.strip())

            # 6. Filter: Reported Status
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
                       c.overruled, c.is_reported, c.reporting_status,
                       c.case_note_ai, c.pdf_path, c.pdf_url,
                       co.name AS court_name
                FROM cases c
                LEFT JOIN courts co ON c.court_id = co.court_id
                {where_sql}
                ORDER BY c.judgment_date DESC NULLS LAST, c.id DESC
                LIMIT %s OFFSET %s;
            """
            cur.execute(query_sql, params + [page_size, offset])
            rows = cur.fetchall()

            results = []
            for r in rows:
                case_id = r[0]

                # Fetch parties summary
                cur.execute("SELECT name, role FROM parties WHERE case_id = %s LIMIT 4;", (case_id,))
                parties = [{"name": p[0], "role": p[1]} for p in cur.fetchall()]

                # Fetch judges summary
                cur.execute("""
                    SELECT j.name, cj.role
                    FROM case_judges cj JOIN judges j ON cj.judge_id = j.id
                    WHERE cj.case_id = %s;
                """, (case_id,))
                judges = [{"name": j[0], "role": j[1]} for j in cur.fetchall()]

                # Fetch subjects
                cur.execute("SELECT subject FROM case_subjects WHERE case_id = %s;", (case_id,))
                subjects = [s[0] for s in cur.fetchall()]

                # Fetch provisions sample
                cur.execute("SELECT act_name, section FROM provisions WHERE case_id = %s LIMIT 3;", (case_id,))
                provisions = [{"act_name": p[0], "section": p[1]} for p in cur.fetchall()]

                results.append({
                    "id": case_id,
                    "diary_number": r[1],
                    "case_number": r[2],
                    "cnr": r[3],
                    "neutral_citation": r[4],
                    "judgment_date": r[5].strftime("%Y-%m-%d") if r[5] else None,
                    "case_category": r[6],
                    "document_type": r[7],
                    "overruled": r[8],
                    "is_reported": r[9],
                    "reporting_status": r[10],
                    "case_note_ai": r[11],
                    "pdf_path": r[12],
                    "pdf_url": r[13],
                    "court_name": r[14],
                    "parties": parties,
                    "judges": judges,
                    "subjects": subjects,
                    "provisions": provisions
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
def get_case_detail(case_id: int):
    """Returns complete metadata and relations for a single case judgment."""
    conn = db_manager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.id, c.diary_number, c.case_id_code, c.cnr, c.case_number,
                       c.case_category, c.registration_date, c.judgment_date,
                       c.neutral_citation, c.disposal_nature, c.result_text,
                       c.case_age_days, c.language, c.document_type, c.confidence_score,
                       c.case_note_ai, c.overruled, c.is_reported, c.reporting_status,
                       c.reporting_source, c.pdf_path, c.pdf_url, c.source_page,
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
                SELECT j.name, cj.role
                FROM case_judges cj JOIN judges j ON cj.judge_id = j.id
                WHERE cj.case_id = %s;
            """, (case_id,))
            judges = [{"name": j[0], "role": j[1]} for j in cur.fetchall()]

            # Advocates
            cur.execute("""
                SELECT a.name, a.designation, ca.party_role
                FROM case_advocates ca JOIN advocates a ON ca.advocate_id = a.id
                WHERE ca.case_id = %s;
            """, (case_id,))
            advocates = [{"name": a[0], "designation": a[1], "party_role": a[2]} for a in cur.fetchall()]

            # Provisions
            cur.execute("SELECT act_name, section FROM provisions WHERE case_id = %s;", (case_id,))
            provisions = [{"act_name": p[0], "section": p[1]} for p in cur.fetchall()]

            # Articles
            cur.execute("SELECT article FROM case_articles WHERE case_id = %s;", (case_id,))
            articles = [a[0] for a in cur.fetchall()]

            # Reporter Citations
            cur.execute("SELECT reporter, citation, year, volume, page FROM reporter_citations WHERE case_id = %s;", (case_id,))
            citations = [{"reporter": r[0], "citation": r[1], "year": r[2], "volume": r[3], "page": r[4]} for r in cur.fetchall()]

            # Subjects
            cur.execute("SELECT subject FROM case_subjects WHERE case_id = %s;", (case_id,))
            subjects = [s[0] for s in cur.fetchall()]

            # Industries
            cur.execute("SELECT industry FROM case_industries WHERE case_id = %s;", (case_id,))
            industries = [i[0] for i in cur.fetchall()]

            # Ministries
            cur.execute("SELECT ministry FROM case_ministries WHERE case_id = %s;", (case_id,))
            ministries = [m[0] for m in cur.fetchall()]

            # Departments
            cur.execute("SELECT department FROM case_departments WHERE case_id = %s;", (case_id,))
            departments = [d[0] for d in cur.fetchall()]

            pdf_file_exists = False
            if row[20]:
                pdf_file_exists = Path(row[20]).exists()

            return {
                "id": row[0],
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
                "overruled": row[16],
                "is_reported": row[17],
                "reporting_status": row[18],
                "reporting_source": row[19],
                "pdf_path": row[20],
                "pdf_url": row[21],
                "pdf_file_exists": pdf_file_exists,
                "source_page": row[22],
                "court_name": row[23],
                "bench_seat": row[24],
                "parties": parties,
                "judges": judges,
                "advocates": advocates,
                "provisions": provisions,
                "articles": articles,
                "reporter_citations": citations,
                "subjects": subjects,
                "industries": industries,
                "ministries": ministries,
                "departments": departments
            }
    finally:
        conn.close()


@app.get("/api/cases/{case_id}/pdf")
def serve_case_pdf(case_id: int):
    """
    Streams the physical PDF file for a case judgment directly from its stored pdf_path.
    This allows the browser to display the PDF inline in an iframe or modal viewer.
    """
    conn = db_manager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pdf_path FROM cases WHERE id = %s;", (case_id,))
            row = cur.fetchone()
            if not row or not row[0]:
                raise HTTPException(status_code=404, detail="No PDF path registered for this case.")

            pdf_path = Path(row[0])
            if not pdf_path.exists():
                raise HTTPException(
                    status_code=404,
                    detail=f"PDF file not found on local disk: {pdf_path}"
                )

            return FileResponse(
                path=str(pdf_path),
                media_type="application/pdf",
                filename=pdf_path.name
            )
    finally:
        conn.close()


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
    return HTMLResponse(content="<h1>Legal Search API running. Web UI loading...</h1>")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=3000, reload=True)
