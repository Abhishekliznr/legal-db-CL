"""
Search Router: Case Search, Case Details & Search-Field Definitions
------------------------------------------------------------------------
Handles:
- QUERY /api/cases                 : Structured JSON search (filters, search fields, page,
                                      limit, sort in the request body, per RFC 10008).
                                      NOT visible in Swagger UI (/docs) - swagger-ui-dist
                                      doesn't render the QUERY method. See /openapi.json
                                      under paths./api/cases.query, or curl -X QUERY.
- GET  /api/cases/{case_id}        : Full judgment metadata, provisions, and citations
- GET/POST/PATCH/DELETE /api/cases/searches[/{field_id}] : CRUD for the advanced
                                      boolean search-builder field definitions.

Rewritten fresh against the new schema (spec §6): the list endpoint queries
`case_search_view` (spec's own "convenience view for the API layer") instead
of hand-joining `cases`/`parties`/`case_judges`/... every time; free-text
search now uses the maintained `documents.search_vector` tsvector
(`@@ plainto_tsquery`) for prose fields instead of an ILIKE chain across 12
parameters, kept alongside ILIKE for structured identifiers (case number,
CNR, neutral citation) where tsvector ranking doesn't help. Case detail
drops to raw joins across documents/cases/parties/case_counsels/
document_sections/citations for the fuller payload the view doesn't carry.
"""

import logging
from typing import Optional, List, Dict, Any, Union
from uuid import UUID

import psycopg2
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from db.connection import get_pooled_connection

logger = logging.getLogger("api_backend_v2.search")

router = APIRouter(tags=["Case Search & Details"])


# ============================================================
# REQUEST MODELS
# ============================================================

class SearchQueryModel(BaseModel):
    text: Optional[str] = None
    all: Optional[List[str]] = None
    any: Optional[List[str]] = None
    exact: Optional[str] = None
    none: Optional[List[str]] = None


class SearchDateRangeModel(BaseModel):
    from_date: Optional[str] = Field(None, alias="from")
    to_date: Optional[str] = Field(None, alias="to")


class SearchSortModel(BaseModel):
    field: Optional[str] = "date"
    direction: Optional[str] = "desc"


class SearchRequestModel(BaseModel):
    query: Optional[Union[SearchQueryModel, str]] = None
    filters: Optional[Dict[str, Any]] = Field(default_factory=dict)
    date: Optional[SearchDateRangeModel] = None
    sort: Optional[SearchSortModel] = Field(default_factory=SearchSortModel)
    page: int = Field(1, ge=1)
    limit: int = Field(20, ge=1, le=100)


# ============================================================
# RESPONSE MODELS
# ============================================================

class PartySummary(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None


class JudgeSummary(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None


class AdvocateSummary(BaseModel):
    name: Optional[str] = None
    representing: Optional[str] = None


class ProvisionSummary(BaseModel):
    act_name: Optional[str] = None
    section: Optional[str] = None


class CitationSummary(BaseModel):
    citation: Optional[str] = None
    treatment: Optional[str] = None


class CitationMade(BaseModel):
    citation: Optional[str] = None
    treatment: Optional[str] = None
    cited_document_id: Optional[str] = None


class CitedByItem(BaseModel):
    case_number: Optional[str] = None
    citation: Optional[str] = None
    treatment: Optional[str] = None
    document_id: Optional[str] = None


class CaseListItem(BaseModel):
    id: str
    case_number: Optional[str] = None
    cnr_number: Optional[str] = None
    neutral_citation: Optional[str] = None
    judgment_date: Optional[str] = None
    case_category: Optional[str] = None
    treatment_status: str = "GOOD_LAW"
    overruled_keyword_present: Optional[bool] = None
    case_note_ai: Optional[str] = None
    pdf_url: Optional[str] = None
    court_name: str
    document_id: str
    parties: List[PartySummary] = []
    judges: List[JudgeSummary] = []
    acts: List[str] = []


class CaseSearchResponse(BaseModel):
    total: int
    page: int
    limit: int
    total_pages: int
    results: List[CaseListItem]


class CaseDetail(BaseModel):
    id: str
    case_number: Optional[str] = None
    cnr_number: Optional[str] = None
    neutral_citation: Optional[str] = None
    judgment_date: Optional[str] = None
    disposition_category: Optional[str] = None
    disposition_raw: Optional[str] = None
    treatment_status: str = "GOOD_LAW"
    overruled_keyword_present: Optional[bool] = None
    case_note_ai: Optional[str] = None
    pdf_url: Optional[str] = None
    court_name: str
    court_id: Optional[str] = None
    document_id: str
    parties: List[PartySummary] = []
    judges: List[JudgeSummary] = []
    advocates: List[AdvocateSummary] = []
    provisions: List[ProvisionSummary] = []
    citations_made: List[CitationMade] = []
    cited_by: List[CitedByItem] = []


# ============================================================
# SEARCH
# ============================================================

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
    limit: int = 20,
) -> Dict[str, Any]:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            where_clauses: List[str] = []
            params: List[Any] = []

            # 1. Free text — tsvector for prose fields, ILIKE for structured identifiers.
            if text_query and text_query.strip():
                tsquery_str = text_query.strip()
                ilike_str = f"%{tsquery_str}%"
                where_clauses.append("""(
                    v.search_vector @@ plainto_tsquery('english', %s)
                    OR v.case_number ILIKE %s
                    OR v.cnr_number ILIKE %s
                    OR v.neutral_citation ILIKE %s
                )""")
                params.extend([tsquery_str, ilike_str, ilike_str, ilike_str])

            if exact_phrase and exact_phrase.strip():
                where_clauses.append("v.search_vector @@ phraseto_tsquery('english', %s)")
                params.append(exact_phrase.strip())

            if all_terms:
                terms = [t.strip() for t in all_terms if t and t.strip()]
                if terms:
                    where_clauses.append("v.search_vector @@ plainto_tsquery('english', %s)")
                    params.append(" ".join(terms))

            if any_terms:
                any_clauses = []
                for term in any_terms:
                    if term and term.strip():
                        any_clauses.append("v.search_vector @@ plainto_tsquery('english', %s)")
                        params.append(term.strip())
                if any_clauses:
                    where_clauses.append("(" + " OR ".join(any_clauses) + ")")

            if none_terms:
                for term in none_terms:
                    if term and term.strip():
                        where_clauses.append("NOT (v.search_vector @@ plainto_tsquery('english', %s))")
                        params.append(term.strip())

            # 2. Filters
            if filters_dict:
                for f_key, f_val in filters_dict.items():
                    if f_val is None or f_val == "" or (isinstance(f_val, list) and len(f_val) == 0):
                        continue
                    val_list = f_val if isinstance(f_val, list) else [f_val]

                    if f_key in ("court", "court_id"):
                        int_ids = [int(v) for v in val_list if str(v).isdigit()]
                        if int_ids:
                            where_clauses.append("v.court_id = ANY(%s)")
                            params.append(int_ids)

                    elif f_key in ("treatment_status", "status"):
                        where_clauses.append("v.treatment_status = ANY(%s)")
                        params.append(val_list)

                    elif f_key in ("judgment_year", "year"):
                        int_years = [int(y) for y in val_list if str(y).isdigit()]
                        if int_years:
                            where_clauses.append("EXTRACT(YEAR FROM v.judgment_date)::INT = ANY(%s)")
                            params.append(int_years)

                    elif f_key in ("judge", "judges"):
                        judge_likes = [f"%{str(j).strip()}%" for j in val_list if str(j).strip()]
                        if judge_likes:
                            where_clauses.append("EXISTS (SELECT 1 FROM unnest(COALESCE(v.coram, ARRAY[]::text[])) cj WHERE cj ILIKE ANY(%s))")
                            params.append(judge_likes)

                    elif f_key in ("act", "acts"):
                        act_likes = [f"%{str(a).strip()}%" for a in val_list if str(a).strip()]
                        if act_likes:
                            where_clauses.append("EXISTS (SELECT 1 FROM unnest(COALESCE(v.acts, ARRAY[]::text[])) ac WHERE ac ILIKE ANY(%s))")
                            params.append(act_likes)

            # 3. Date range
            if from_date:
                where_clauses.append("v.judgment_date >= %s")
                params.append(from_date)
            if to_date:
                where_clauses.append("v.judgment_date <= %s")
                params.append(to_date)

            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            cur.execute(f"SELECT COUNT(*) FROM case_search_view v {where_sql};", params)
            total_records = cur.fetchone()[0]

            offset = (page - 1) * limit
            total_pages = (total_records + limit - 1) // limit if total_records > 0 else 1
            direction = "ASC" if str(sort_direction).lower() == "asc" else "DESC"
            order_sql = f"ORDER BY v.judgment_date {direction} NULLS LAST"

            cur.execute(f"""
                SELECT v.case_id, v.case_number, v.cnr_number, v.neutral_citation, v.judgment_date,
                       v.case_category, v.treatment_status, v.overruled_keyword_present,
                       v.case_note_ai, v.pdf_url, v.court_name, v.document_id,
                       v.petitioners, v.respondents, v.coram, v.acts
                FROM case_search_view v
                {where_sql}
                {order_sql}
                LIMIT %s OFFSET %s;
            """, params + [limit, offset])
            rows = cur.fetchall()

            results = []
            for r in rows:
                (case_id, case_number, cnr_number, neutral_citation, judgment_date, case_category,
                 treatment_status, overruled_flag, case_note_ai, pdf_url, court_name, document_id,
                 petitioners, respondents, coram, acts) = r

                parties = [{"name": p, "role": "PETITIONER"} for p in (petitioners or [])] + \
                          [{"name": p, "role": "RESPONDENT"} for p in (respondents or [])]
                judges = [{"name": j, "role": None} for j in (coram or [])]

                results.append({
                    "id": str(case_id),
                    "case_number": case_number,
                    "cnr_number": cnr_number,
                    "neutral_citation": neutral_citation,
                    "judgment_date": judgment_date.strftime("%Y-%m-%d") if judgment_date else None,
                    "case_category": case_category,
                    "treatment_status": treatment_status or "GOOD_LAW",
                    "overruled_keyword_present": overruled_flag,
                    "case_note_ai": case_note_ai,
                    "pdf_url": pdf_url,
                    "court_name": court_name,
                    "document_id": str(document_id),
                    "parties": parties,
                    "judges": judges,
                    "acts": acts or [],
                })

            return {"total": total_records, "page": page, "limit": limit, "total_pages": total_pages, "results": results}


@router.api_route(
    "/api/cases", methods=["QUERY"], response_model=CaseSearchResponse,
    summary="Search & list cases (HTTP QUERY method, RFC 10008)",
    description="Not renderable in Swagger UI — see /openapi.json under paths./api/cases.query, or `curl -X QUERY`.",
)
def query_cases(req: SearchRequestModel):
    text_q = all_terms = any_terms = exact_phrase = none_terms = None
    if isinstance(req.query, str):
        text_q = req.query
    elif isinstance(req.query, SearchQueryModel):
        text_q, all_terms, any_terms, exact_phrase, none_terms = req.query.text, req.query.all, req.query.any, req.query.exact, req.query.none

    from_d = req.date.from_date if req.date else None
    to_d = req.date.to_date if req.date else None
    sort_f = req.sort.field if req.sort else "date"
    sort_d = req.sort.direction if req.sort else "desc"

    try:
        return execute_case_search(
            text_query=text_q, all_terms=all_terms, any_terms=any_terms, exact_phrase=exact_phrase,
            none_terms=none_terms, filters_dict=req.filters, from_date=from_d, to_date=to_d,
            sort_field=sort_f, sort_direction=sort_d, page=req.page, limit=req.limit,
        )
    except psycopg2.OperationalError:
        logger.exception("Database connection failed during case search")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error during case search")
        raise HTTPException(status_code=500, detail="Case search failed.")


# ============================================================
# SEARCH-FIELD DEFINITIONS (unchanged shape from the old service — pure presentation config)
# ============================================================

class SearchFieldDefinition(BaseModel):
    id: str
    key: str
    label: str
    placeholder: str
    combinator: str
    isActive: bool
    displayOrder: int


class SearchFieldsResponse(BaseModel):
    fields: List[SearchFieldDefinition]


class SearchFieldCreate(BaseModel):
    key: str
    label: str
    placeholder: str = "Search items..."
    combinator: str
    isActive: bool = True
    displayOrder: int = 0


class SearchFieldUpdate(BaseModel):
    key: Optional[str] = None
    label: Optional[str] = None
    placeholder: Optional[str] = None
    combinator: Optional[str] = None
    isActive: Optional[bool] = None
    displayOrder: Optional[int] = None


def _row_to_search_field(row) -> SearchFieldDefinition:
    f_id, key, label, placeholder, combinator, is_active, display_order = row
    return SearchFieldDefinition(id=str(f_id), key=key, label=label, placeholder=placeholder, combinator=combinator, isActive=is_active, displayOrder=display_order)


@router.get("/api/cases/searches", response_model=SearchFieldsResponse)
def get_configuration_driven_search_fields(include_inactive: bool = Query(False)):
    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                where = "" if include_inactive else "WHERE is_active"
                cur.execute(f"""
                    SELECT id, key, label, placeholder, combinator, is_active, display_order
                    FROM search_field_definitions {where} ORDER BY display_order;
                """)
                return SearchFieldsResponse(fields=[_row_to_search_field(r) for r in cur.fetchall()])
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while loading search fields")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while loading search fields")
        raise HTTPException(status_code=500, detail="Failed to load search field metadata.")


@router.post("/api/cases/searches", response_model=SearchFieldDefinition, status_code=201)
def create_search_field(payload: SearchFieldCreate):
    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO search_field_definitions (key, label, placeholder, combinator, is_active, display_order)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, key, label, placeholder, combinator, is_active, display_order;
                """, (payload.key, payload.label, payload.placeholder, payload.combinator, payload.isActive, payload.displayOrder))
                row = cur.fetchone()
                conn.commit()
                return _row_to_search_field(row)
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail=f"A search field with key '{payload.key}' already exists.")
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while creating search field")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while creating search field")
        raise HTTPException(status_code=500, detail="Failed to create search field.")


@router.get("/api/cases/searches/{field_id}", response_model=SearchFieldDefinition)
def get_search_field(field_id: UUID):
    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, key, label, placeholder, combinator, is_active, display_order FROM search_field_definitions WHERE id = %s;", (str(field_id),))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Search field not found.")
                return _row_to_search_field(row)
    except HTTPException:
        raise
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while loading search field %s", field_id)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while loading search field %s", field_id)
        raise HTTPException(status_code=500, detail="Failed to load search field.")


@router.patch("/api/cases/searches/{field_id}", response_model=SearchFieldDefinition)
def update_search_field(field_id: UUID, payload: SearchFieldUpdate):
    updates = payload.model_dump(exclude_unset=True)
    column_map = {"key": "key", "label": "label", "placeholder": "placeholder", "combinator": "combinator", "isActive": "is_active", "displayOrder": "display_order"}
    set_clauses, params = [], []
    for field_name, column in column_map.items():
        if field_name in updates:
            set_clauses.append(f"{column} = %s")
            params.append(updates[field_name])

    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM search_field_definitions WHERE id = %s;", (str(field_id),))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Search field not found.")
                if set_clauses:
                    set_clauses.append("updated_at = CURRENT_TIMESTAMP")
                    params.append(str(field_id))
                    cur.execute(f"UPDATE search_field_definitions SET {', '.join(set_clauses)} WHERE id = %s;", params)
                conn.commit()
                cur.execute("SELECT id, key, label, placeholder, combinator, is_active, display_order FROM search_field_definitions WHERE id = %s;", (str(field_id),))
                return _row_to_search_field(cur.fetchone())
    except HTTPException:
        raise
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="A search field with that key already exists.")
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while updating search field %s", field_id)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while updating search field %s", field_id)
        raise HTTPException(status_code=500, detail="Failed to update search field.")


@router.delete("/api/cases/searches/{field_id}", status_code=204)
def delete_search_field(field_id: UUID):
    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM search_field_definitions WHERE id = %s RETURNING id;", (str(field_id),))
                deleted = cur.fetchone()
                conn.commit()
                if not deleted:
                    raise HTTPException(status_code=404, detail="Search field not found.")
                return None
    except HTTPException:
        raise
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while deleting search field %s", field_id)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while deleting search field %s", field_id)
        raise HTTPException(status_code=500, detail="Failed to delete search field.")


# ============================================================
# CASE DETAIL — drops below the view for the fuller payload
# ============================================================

def _to_valid_int(case_id: str):
    try:
        return int(case_id)
    except (ValueError, TypeError):
        return None


@router.get("/api/cases/{case_id:path}", response_model=CaseDetail)
def get_case_detail(case_id: str):
    valid_int_id = _to_valid_int(case_id)
    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                id_clause = "c.case_id = %s OR " if valid_int_id is not None else ""
                id_params = [valid_int_id] if valid_int_id is not None else []
                cur.execute(f"""
                    SELECT c.case_id, c.case_number, c.cnr_number, d.neutral_citation, d.judgment_date,
                           d.disposition_category, d.disposition_raw, d.overruled_keyword_present,
                           d.case_note_ai, d.pdf_url, crt.court_name, crt.court_id, d.document_id
                    FROM cases c
                    JOIN documents d ON d.document_id = c.document_id
                    JOIN courts crt ON crt.court_id = c.court_id
                    WHERE {id_clause}c.case_number = %s OR c.cnr_number = %s;
                """, (*id_params, case_id, case_id))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Case judgment not found.")

                (db_case_id, case_number, cnr_number, neutral_citation, judgment_date, disposition_category,
                 disposition_raw, overruled_flag, case_note_ai, pdf_url, court_name, court_id, document_id) = row

                cur.execute("SELECT party_name, party_side FROM parties WHERE case_id = %s ORDER BY party_order;", (db_case_id,))
                parties = [{"name": p[0], "role": p[1]} for p in cur.fetchall()]

                cur.execute("""
                    SELECT j.full_name, dc.is_author FROM document_coram dc
                    JOIN judges j ON j.judge_id = dc.judge_id
                    WHERE dc.document_id = %s ORDER BY dc.bench_order;
                """, (document_id,))
                judges = [{"name": j[0], "role": "AUTHOR" if j[1] else "BENCH_MEMBER"} for j in cur.fetchall()]

                cur.execute("""
                    SELECT a.full_name, cc.designation FROM case_counsels cc
                    JOIN advocates a ON a.advocate_id = cc.advocate_id
                    WHERE cc.case_id = %s;
                """, (db_case_id,))
                advocates = [{"name": a[0], "representing": a[1]} for a in cur.fetchall()]

                cur.execute("""
                    SELECT st.statute_name, sec.section_number FROM document_sections ds
                    JOIN sections sec ON sec.section_id = ds.section_id
                    JOIN statutes st ON st.statute_id = sec.statute_id
                    WHERE ds.document_id = %s;
                """, (document_id,))
                provisions = [{"act_name": p[0], "section": p[1]} for p in cur.fetchall()]

                cur.execute("""
                    SELECT cited_case_name, treatment, COALESCE(cited_document_id::text, '')
                    FROM citations WHERE citing_document_id = %s;
                """, (document_id,))
                citations_made = [{"citation": c[0], "treatment": c[1], "cited_document_id": c[2] or None} for c in cur.fetchall()]

                cur.execute("""
                    SELECT ca.case_number, cit.cited_reporter_citation, cit.treatment, d2.document_id::text
                    FROM citations cit
                    JOIN documents d2 ON d2.document_id = cit.citing_document_id
                    LEFT JOIN cases ca ON ca.document_id = d2.document_id
                    WHERE cit.cited_document_id = %s;
                """, (document_id,))
                cited_by = [{"case_number": c[0], "citation": c[1], "treatment": c[2], "document_id": c[3]} for c in cur.fetchall()]

                cur.execute("SELECT treatment_status FROM case_search_view WHERE case_id = %s;", (db_case_id,))
                ts_row = cur.fetchone()
                treatment_status = ts_row[0] if ts_row else "GOOD_LAW"

                return {
                    "id": str(db_case_id), "case_number": case_number, "cnr_number": cnr_number,
                    "neutral_citation": neutral_citation,
                    "judgment_date": judgment_date.strftime("%Y-%m-%d") if judgment_date else None,
                    "disposition_category": disposition_category, "disposition_raw": disposition_raw,
                    "treatment_status": treatment_status, "overruled_keyword_present": overruled_flag,
                    "case_note_ai": case_note_ai, "pdf_url": pdf_url, "court_name": court_name,
                    "court_id": str(court_id), "document_id": str(document_id),
                    "parties": parties, "judges": judges, "advocates": advocates,
                    "provisions": provisions, "citations_made": citations_made, "cited_by": cited_by,
                }
    except HTTPException:
        raise
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while loading case %s", case_id)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while loading case %s", case_id)
        raise HTTPException(status_code=500, detail="Failed to load case detail.")
