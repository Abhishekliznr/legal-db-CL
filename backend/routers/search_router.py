"""
Search Router: Dedicated Endpoints for Case Search, Citator Treatment, Case Details & PDF
------------------------------------------------------------------------------------------
Handles:
- GET /api/cases/search            : Multi-field full-text search with dynamic filters & pagination
- GET /api/cases/{case_id}         : Full judgment metadata, provisions, and citations
- GET /api/cases/{case_id}/citations: Citation precedence graph (Good Law, Overruled, Distinguished)
- GET /api/cases/{case_id}/pdf     : Stream judgment PDF directly or redirect to Azure Blob
- GET /api/stats                   : Corpus statistics & citator status breakdown
"""

import os
from pathlib import Path
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

try:
    from backend import db_manager
except ImportError:
    import db_manager

router = APIRouter(tags=["Legal Search & Citator Engine"])


@router.get("/api/cases/search", response_model=Dict[str, Any])
def search_cases(
    q: Optional[str] = Query(None, description="Free text query across case name, numbers, AI notes, judges, acts"),
    court_id: Optional[str] = Query(None, description="Filter by Court ID (e.g. SCIN)"),
    year: Optional[int] = Query(None, description="Filter by Judgment Year"),
    treatment_status: Optional[str] = Query(None, description="Filter by Citator Status (GOOD_LAW, OVERRULED, DOUBTED, DISTINGUISHED)"),
    judge: Optional[str] = Query(None, description="Filter by Judge Name"),
    act: Optional[str] = Query(None, description="Filter by Canonical Act"),
    is_reported: Optional[bool] = Query(None, description="Filter by Reported Status"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(10, ge=1, le=100, description="Results per page")
):
    """
    High-performance multi-field legal search across the entire case corpus.
    Supports full-text queries, dynamic filter combinations, Citator treatment status, and pagination.
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

            # Count total records
            count_sql = f"SELECT COUNT(DISTINCT c.id) FROM cases c {where_sql};"
            cur.execute(count_sql, params)
            total_records = cur.fetchone()[0]

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
                    "court_name": r[15] or "Supreme Court of India",
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


@router.get("/api/cases/{case_id}", response_model=Dict[str, Any])
def get_case_detail(case_id: str):
    """Retrieves full case details including all parties, judges, advocates, acts, provisions, and citations."""
    conn = db_manager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.id, c.diary_number, c.case_number, c.cnr, c.neutral_citation,
                       c.judgment_date, c.registration_date, c.decision_date,
                       c.case_category, c.document_type, c.treatment_status, c.overruled,
                       c.is_reported, c.reporting_status, c.case_note_ai, c.pdf_path, c.pdf_url,
                       co.name AS court_name, co.court_id
                FROM cases c
                LEFT JOIN courts co ON c.court_id = co.court_id
                WHERE c.id = %s::uuid OR c.diary_number = %s OR c.case_number = %s;
            """, (case_id if len(case_id) == 36 and '-' in case_id else '00000000-0000-0000-0000-000000000000', case_id, case_id))
            
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Case judgment not found.")

            c_uuid = str(row[0])

            # Parties
            cur.execute("SELECT name, role FROM parties WHERE case_id = %s;", (c_uuid,))
            parties = [{"name": p[0], "role": p[1]} for p in cur.fetchall()]

            # Judges
            cur.execute("""
                SELECT jm.canonical_name, cj.role
                FROM case_judges cj JOIN judge_master jm ON cj.judge_id = jm.id
                WHERE cj.case_id = %s;
            """, (c_uuid,))
            judges = [{"name": j[0], "role": j[1]} for j in cur.fetchall()]

            # Advocates
            cur.execute("""
                SELECT a.name, ca.representing_party
                FROM case_advocates ca JOIN advocates a ON ca.advocate_id = a.id
                WHERE ca.case_id = %s;
            """, (c_uuid,))
            advocates = [{"name": a[0], "representing": a[1]} for a in cur.fetchall()]

            # Provisions
            cur.execute("""
                SELECT COALESCE(am.canonical_name, pr.raw_act_name) AS act_name, pr.section, pr.provision_full
                FROM provisions pr 
                LEFT JOIN act_master am ON pr.act_id = am.id 
                WHERE pr.case_id = %s;
            """, (c_uuid,))
            provisions = [{"act_name": p[0], "section": p[1], "full_text": p[2]} for p in cur.fetchall()]

            # Citations Made
            cur.execute("""
                SELECT raw_citation_text, treatment_type, COALESCE(cited_case_id::text, '')
                FROM citations
                WHERE citing_case_id = %s;
            """, (c_uuid,))
            citations_made = [{"citation": c[0], "treatment": c[1], "cited_case_id": c[2]} for c in cur.fetchall()]

            # Cases that cite this case
            cur.execute("""
                SELECT c.case_number, c.neutral_citation, cit.treatment_type, c.id::text
                FROM citations cit
                JOIN cases c ON cit.citing_case_id = c.id
                WHERE cit.cited_case_id = %s;
            """, (c_uuid,))
            cited_by = [{"case_number": c[0], "citation": c[1], "treatment": c[2], "case_id": c[3]} for c in cur.fetchall()]

            return {
                "id": c_uuid,
                "diary_number": row[1],
                "case_number": row[2],
                "cnr": row[3],
                "neutral_citation": row[4],
                "judgment_date": row[5].strftime("%Y-%m-%d") if row[5] else None,
                "registration_date": row[6].strftime("%Y-%m-%d") if row[6] else None,
                "decision_date": row[7].strftime("%Y-%m-%d") if row[7] else None,
                "case_category": row[8],
                "document_type": row[9],
                "treatment_status": row[10] or "GOOD_LAW",
                "overruled": row[11],
                "is_reported": row[12],
                "reporting_status": row[13],
                "case_note_ai": row[14],
                "pdf_path": row[15],
                "pdf_url": row[16],
                "court_name": row[17] or "Supreme Court of India",
                "court_id": row[18],
                "parties": parties,
                "judges": judges,
                "advocates": advocates,
                "provisions": provisions,
                "citations_made": citations_made,
                "cited_by": cited_by
            }
    finally:
        conn.close()


@router.get("/api/cases/{case_id}/citations", response_model=Dict[str, Any])
def get_case_citation_network(case_id: str):
    """Returns the graph network of citations (nodes and links) for visual citator exploration."""
    conn = db_manager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.id, c.case_number, c.neutral_citation, c.treatment_status 
                FROM cases c 
                WHERE c.id = %s::uuid OR c.diary_number = %s;
            """, (case_id if len(case_id) == 36 and '-' in case_id else '00000000-0000-0000-0000-000000000000', case_id))
            root = cur.fetchone()
            if not root:
                raise HTTPException(status_code=404, detail="Case not found.")

            root_uuid = str(root[0])
            nodes = [{
                "id": root_uuid,
                "label": root[1] or root[2] or "Root Case",
                "type": "root",
                "treatment": root[3] or "GOOD_LAW"
            }]
            links = []

            # Citations made by this case
            cur.execute("""
                SELECT cit.raw_citation_text, cit.treatment_type, cit.cited_case_id::text
                FROM citations cit
                WHERE cit.citing_case_id = %s;
            """, (root_uuid,))
            for r in cur.fetchall():
                node_id = r[2] if r[2] else f"cit_{hash(r[0])}"
                nodes.append({
                    "id": node_id,
                    "label": r[0],
                    "type": "cited",
                    "treatment": r[1] or "AFFIRMED"
                })
                links.append({
                    "source": root_uuid,
                    "target": node_id,
                    "treatment": r[1] or "CITED"
                })

            return {"root_id": root_uuid, "nodes": nodes, "links": links}
    finally:
        conn.close()


@router.get("/api/cases/{case_id}/pdf")
def get_case_pdf(case_id: str):
    """Directly streams the local judgment PDF or redirects to permanent Azure Blob URL."""
    conn = db_manager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT pdf_path, pdf_url 
                FROM cases 
                WHERE id = %s::uuid OR diary_number = %s OR case_number = %s;
            """, (case_id if len(case_id) == 36 and '-' in case_id else '00000000-0000-0000-0000-000000000000', case_id, case_id))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Case not found.")

            pdf_path, pdf_url = row[0], row[1]
            if pdf_path and Path(pdf_path).exists():
                return FileResponse(pdf_path, media_type="application/pdf", filename=Path(pdf_path).name)
            elif pdf_url:
                return RedirectResponse(url=pdf_url)
            else:
                raise HTTPException(status_code=404, detail="Physical PDF document not found on storage.")
    finally:
        conn.close()


@router.get("/api/stats", response_model=Dict[str, Any])
def get_corpus_statistics():
    """Returns summary statistics for the legal research corpus."""
    conn = db_manager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM cases;")
            total_cases = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM provisions;")
            total_provisions = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM citations;")
            total_citations = cur.fetchone()[0]

            cur.execute("""
                SELECT treatment_status, COUNT(*) 
                FROM cases 
                GROUP BY treatment_status;
            """)
            treatment_counts = {r[0] or "UNKNOWN": r[1] for r in cur.fetchall()}

            return {
                "total_cases": total_cases,
                "total_provisions": total_provisions,
                "total_citations": total_citations,
                "treatment_breakdown": treatment_counts
            }
    finally:
        conn.close()
