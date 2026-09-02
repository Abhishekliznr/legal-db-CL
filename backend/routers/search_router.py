"""
Search Router: Case Search, Facet Extraction, Citator Treatment, Case Details & PDF
------------------------------------------------------------------------------------
Handles:
- POST /api/case-research/search   : Structured JSON search with multi-filter mapping & dynamic facets
- POST /api/cases/search           : Alias for structured search
- GET  /api/cases/search           : Query-param search for quick GET lookups & backward compatibility
- GET  /api/cases/{case_id}        : Full judgment metadata, provisions, and citations
- GET  /api/cases/{case_id}/citations: Citation precedence graph (Good Law, Overruled, Distinguished)
- GET  /api/cases/{case_id}/pdf    : Stream judgment PDF directly or redirect to Azure Blob
- GET  /api/stats                  : Corpus statistics & citator status breakdown
"""

import os
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
from fastapi import APIRouter, Query, HTTPException, Body
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

try:
    from backend import db_manager
except ImportError:
    import db_manager

router = APIRouter(tags=["Legal Search & Citator Engine"])


class SearchQueryModel(BaseModel):
    text: Optional[str] = Field(None, description="General free text query")
    all: Optional[List[str]] = Field(None, description="All of these words must appear (AND)")
    any: Optional[List[str]] = Field(None, description="Any of these words may appear (OR)")
    exact: Optional[str] = Field(None, description="Exact phrase matching")
    none: Optional[List[str]] = Field(None, description="None of these words may appear (NOT)")


class SearchDateRangeModel(BaseModel):
    from_date: Optional[str] = Field(None, alias="from", description="Start date (YYYY-MM-DD)")
    to_date: Optional[str] = Field(None, alias="to", description="End date (YYYY-MM-DD)")


class SearchSortModel(BaseModel):
    field: Optional[str] = Field("date", description="Sort field: relevance, date, court")
    direction: Optional[str] = Field("desc", description="Sort direction: asc or desc")


class SearchRequestModel(BaseModel):
    query: Optional[Union[SearchQueryModel, str]] = Field(None, description="Text query or structured search")
    filters: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Filter keys mapped to list of selected values")
    date: Optional[SearchDateRangeModel] = Field(None, description="Decision date range")
    sort: Optional[SearchSortModel] = Field(default_factory=SearchSortModel, description="Sorting options")
    page: int = Field(1, ge=1, description="Page number")
    limit: int = Field(20, ge=1, le=100, description="Results per page")


# Safe Filter Mapping for Parameterized Queries (Zero Raw SQL Injection)
FILTER_FIELD_MAP = {
    "court": "c.court_id",
    "court_id": "c.court_id",
    "treatment_status": "c.treatment_status",
    "judgment_year": "EXTRACT(YEAR FROM c.judgment_date)",
    "year": "EXTRACT(YEAR FROM c.judgment_date)",
    "is_reported": "c.is_reported"
}


def execute_case_search(
    text_query: Optional[str] = None,
    all_terms: Optional[List[str]] = None,
    any_terms: Optional[List[str]] = None,
    exact_phrase: Optional[str] = None,
    none_terms: Optional[List[str]] = None,
    filters_dict: Optional[Dict[str, Any]] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    sort_field: str = "date",
    sort_direction: str = "desc",
    page: int = 1,
    limit: int = 20
) -> Dict[str, Any]:
    """Core parameterized SQL builder & executor with live facet generation."""
    conn = db_manager.get_connection()
    try:
        with conn.cursor() as cur:
            where_clauses = []
            params: List[Any] = []

            # 1. Free Text / General Query
            if text_query and text_query.strip():
                query_str = f"%{text_query.strip()}%"
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

            # 2. Exact Phrase
            if exact_phrase and exact_phrase.strip():
                p_str = f"%{exact_phrase.strip()}%"
                where_clauses.append("(c.case_note_ai ILIKE %s OR c.case_number ILIKE %s)")
                params.extend([p_str, p_str])

            # 3. All Terms (AND)
            if all_terms:
                for term in all_terms:
                    if term and str(term).strip():
                        t_str = f"%{str(term).strip()}%"
                        where_clauses.append("(c.case_note_ai ILIKE %s OR c.case_number ILIKE %s)")
                        params.extend([t_str, t_str])

            # 4. Any Terms (OR)
            if any_terms:
                any_clauses = []
                for term in any_terms:
                    if term and str(term).strip():
                        t_str = f"%{str(term).strip()}%"
                        any_clauses.append("(c.case_note_ai ILIKE %s)")
                        params.append(t_str)
                if any_clauses:
                    where_clauses.append("(" + " OR ".join(any_clauses) + ")")

            # 5. None Terms (NOT)
            if none_terms:
                for term in none_terms:
                    if term and str(term).strip():
                        t_str = f"%{str(term).strip()}%"
                        where_clauses.append("(c.case_note_ai NOT ILIKE %s OR c.case_note_ai IS NULL)")
                        params.append(t_str)

            # 6. Apply Filter Dictionary (Safe Parameterized Mapping)
            if filters_dict:
                for f_key, f_val in filters_dict.items():
                    if f_val is None or f_val == "" or (isinstance(f_val, list) and len(f_val) == 0):
                        continue

                    val_list = f_val if isinstance(f_val, list) else [f_val]

                    if f_key in ["court", "court_id"]:
                        expanded = set(val_list)
                        if "SCIN" in val_list or "sc" in [str(x).lower() for x in val_list]:
                            expanded.add("SUPREME_COURT_OF_INDIA")
                            expanded.add("SCIN")
                        if "SUPREME_COURT_OF_INDIA" in val_list:
                            expanded.add("SCIN")
                        where_clauses.append("(c.court_id = ANY(%s) OR UPPER(c.court_id) = ANY(%s))")
                        params.extend([list(expanded), [str(x).upper() for x in expanded]])

                    elif f_key in ["treatment_status", "status"]:
                        where_clauses.append("c.treatment_status = ANY(%s)")
                        params.append(val_list)

                    elif f_key in ["judgment_year", "year"]:
                        int_years = [int(y) for y in val_list if str(y).isdigit()]
                        if int_years:
                            where_clauses.append("EXTRACT(YEAR FROM c.judgment_date)::INT = ANY(%s)")
                            params.append(int_years)

                    elif f_key in ["judge", "judges"]:
                        judge_likes = [f"%{str(j).strip()}%" for j in val_list if str(j).strip()]
                        if judge_likes:
                            where_clauses.append("""EXISTS (
                                SELECT 1 FROM case_judges cj 
                                JOIN judge_master jm ON cj.judge_id = jm.id 
                                WHERE cj.case_id = c.id AND (jm.canonical_name ILIKE ANY(%s))
                            )""")
                            params.append(judge_likes)

                    elif f_key in ["act", "acts"]:
                        act_likes = [f"%{str(a).strip()}%" for a in val_list if str(a).strip()]
                        if act_likes:
                            where_clauses.append("""EXISTS (
                                SELECT 1 FROM provisions pr 
                                LEFT JOIN act_master am ON pr.act_id = am.id 
                                WHERE pr.case_id = c.id AND (
                                    am.canonical_name ILIKE ANY(%s) 
                                    OR pr.raw_act_name ILIKE ANY(%s)
                                )
                            )""")
                            params.extend([act_likes, act_likes])

            # 7. Date Range
            if from_date:
                where_clauses.append("c.judgment_date >= %s")
                params.append(from_date)
            if to_date:
                where_clauses.append("c.judgment_date <= %s")
                params.append(to_date)

            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            # Count total matching records
            count_sql = f"SELECT COUNT(DISTINCT c.id) FROM cases c {where_sql};"
            cur.execute(count_sql, params)
            total_records = cur.fetchone()[0]

            offset = (page - 1) * limit
            total_pages = (total_records + limit - 1) // limit if total_records > 0 else 1

            # Order by clause
            direction = "ASC" if str(sort_direction).lower() == "asc" else "DESC"
            if sort_field == "date":
                order_sql = f"ORDER BY c.judgment_date {direction} NULLS LAST, c.created_at DESC"
            else:
                order_sql = f"ORDER BY c.judgment_date DESC NULLS LAST, c.created_at DESC"

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
                {order_sql}
                LIMIT %s OFFSET %s;
            """
            cur.execute(query_sql, params + [limit, offset])
            rows = cur.fetchall()

            results = []
            for r in rows:
                case_id = str(r[0])

                # Parties summary
                cur.execute("SELECT name, role FROM parties WHERE case_id = %s LIMIT 4;", (case_id,))
                parties = [{"name": p[0], "role": p[1]} for p in cur.fetchall()]

                # Judges summary
                cur.execute("""
                    SELECT jm.canonical_name, cj.role
                    FROM case_judges cj JOIN judge_master jm ON cj.judge_id = jm.id
                    WHERE cj.case_id = %s;
                """, (case_id,))
                judges = [{"name": j[0], "role": j[1]} for j in cur.fetchall()]

                # Provisions sample
                cur.execute("""
                    SELECT COALESCE(am.canonical_name, pr.raw_act_name) AS act_name, pr.section, pr.provision_full
                    FROM provisions pr 
                    LEFT JOIN act_master am ON pr.act_id = am.id 
                    WHERE pr.case_id = %s LIMIT 3;
                """, (case_id,))
                provisions = [{"act_name": p[0], "section": p[1], "full_text": p[2]} for p in cur.fetchall()]

                # Citations sample
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

            # 8. Dynamic Facet Counts
            cur.execute("""
                SELECT COALESCE(c.court_id, 'SCIN'), COALESCE(ct.name, 'Supreme Court of India'), COUNT(c.id)
                FROM cases c
                LEFT JOIN courts ct ON c.court_id = ct.court_id
                GROUP BY c.court_id, ct.name
                ORDER BY COUNT(c.id) DESC;
            """)
            facet_courts = [{"value": r[0], "label": r[1], "count": r[2]} for r in cur.fetchall()]

            cur.execute("""
                SELECT treatment_status, COUNT(id)
                FROM cases
                WHERE treatment_status IS NOT NULL
                GROUP BY treatment_status
                ORDER BY COUNT(id) DESC;
            """)
            facet_treatments = [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

            cur.execute("""
                SELECT j.canonical_name, COUNT(DISTINCT cj.case_id)
                FROM case_judges cj
                JOIN judge_master j ON cj.judge_id = j.id
                GROUP BY j.canonical_name
                ORDER BY COUNT(DISTINCT cj.case_id) DESC
                LIMIT 20;
            """)
            facet_judges = [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

            cur.execute("""
                SELECT COALESCE(a.canonical_name, p.raw_act_name), COUNT(DISTINCT p.case_id)
                FROM provisions p
                LEFT JOIN act_master a ON p.act_id = a.id
                WHERE COALESCE(a.canonical_name, p.raw_act_name) IS NOT NULL
                GROUP BY COALESCE(a.canonical_name, p.raw_act_name)
                ORDER BY COUNT(DISTINCT p.case_id) DESC
                LIMIT 20;
            """)
            facet_acts = [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

            facets = {
                "court": facet_courts,
                "treatment_status": facet_treatments,
                "judge": facet_judges,
                "act": facet_acts
            }

            return {
                "total": total_records,
                "page": page,
                "limit": limit,
                "total_pages": total_pages,
                "results": results,
                "facets": facets
            }
    finally:
        conn.close()


@router.post("/api/cases/search", response_model=Dict[str, Any])
def post_search_cases(req: SearchRequestModel):
    """
    Configuration-Driven JSON Search API (Recommended by Frontend Team):
    Accepts structured queries, filter maps, date ranges, and sorting.
    Returns matching cases and dynamic facet counts in a single round-trip.
    """
    text_q = None
    all_terms = None
    any_terms = None
    exact_phrase = None
    none_terms = None

    if isinstance(req.query, str):
        text_q = req.query
    elif isinstance(req.query, SearchQueryModel):
        text_q = req.query.text
        all_terms = req.query.all
        any_terms = req.query.any
        exact_phrase = req.query.exact
        none_terms = req.query.none

    from_d = req.date.from_date if req.date else None
    to_d = req.date.to_date if req.date else None
    sort_f = req.sort.field if req.sort else "date"
    sort_d = req.sort.direction if req.sort else "desc"

    return execute_case_search(
        text_query=text_q,
        all_terms=all_terms,
        any_terms=any_terms,
        exact_phrase=exact_phrase,
        none_terms=none_terms,
        filters_dict=req.filters,
        from_date=from_d,
        to_date=to_d,
        sort_field=sort_f,
        sort_direction=sort_d,
        page=req.page,
        limit=req.limit
    )


@router.get("/api/cases/search", response_model=Dict[str, Any])
def get_search_cases(
    q: Optional[str] = Query(None, description="Free text query across all legal fields"),
    court_id: Optional[str] = Query(None, description="Filter by Court ID"),
    year: Optional[int] = Query(None, description="Filter by Judgment Year"),
    treatment_status: Optional[str] = Query(None, description="Filter by Treatment Status"),
    judge: Optional[str] = Query(None, description="Filter by Judge Name"),
    act: Optional[str] = Query(None, description="Filter by Canonical Act"),
    is_reported: Optional[bool] = Query(None, description="Filter by Reported Status"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(10, ge=1, le=100, description="Results per page")
):
    """GET Search Endpoint with Query Parameters for browser URL bookmarking & backward compatibility."""
    filters = {}
    if court_id:
        filters["court_id"] = court_id
    if year:
        filters["year"] = year
    if treatment_status:
        filters["treatment_status"] = treatment_status
    if judge:
        filters["judge"] = judge
    if act:
        filters["act"] = act

    return execute_case_search(
        text_query=q,
        filters_dict=filters,
        page=page,
        limit=page_size
    )


@router.get("/api/cases/{case_id}", response_model=Dict[str, Any])
def get_case_detail(case_id: str):
    """Retrieves full case details including parties, judges, advocates, acts, provisions, and citations."""
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

            cur.execute("SELECT name, role FROM parties WHERE case_id = %s;", (c_uuid,))
            parties = [{"name": p[0], "role": p[1]} for p in cur.fetchall()]

            cur.execute("""
                SELECT jm.canonical_name, cj.role
                FROM case_judges cj JOIN judge_master jm ON cj.judge_id = jm.id
                WHERE cj.case_id = %s;
            """, (c_uuid,))
            judges = [{"name": j[0], "role": j[1]} for j in cur.fetchall()]

            cur.execute("""
                SELECT a.name, ca.representing_party
                FROM case_advocates ca JOIN advocates a ON ca.advocate_id = a.id
                WHERE ca.case_id = %s;
            """, (c_uuid,))
            advocates = [{"name": a[0], "representing": a[1]} for a in cur.fetchall()]

            cur.execute("""
                SELECT COALESCE(am.canonical_name, pr.raw_act_name) AS act_name, pr.section, pr.provision_full
                FROM provisions pr 
                LEFT JOIN act_master am ON pr.act_id = am.id 
                WHERE pr.case_id = %s;
            """, (c_uuid,))
            provisions = [{"act_name": p[0], "section": p[1], "full_text": p[2]} for p in cur.fetchall()]

            cur.execute("""
                SELECT raw_citation_text, treatment_type, COALESCE(cited_case_id::text, '')
                FROM citations
                WHERE citing_case_id = %s;
            """, (c_uuid,))
            citations_made = [{"citation": c[0], "treatment": c[1], "cited_case_id": c[2]} for c in cur.fetchall()]

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
