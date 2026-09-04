"""
Search Router: Case Search, Case Details & Search-Field Definitions
------------------------------------------------------------------------
Handles:
- QUERY /api/cases                 : Structured JSON search (filters, search fields, page,
                                      limit, sort in the request body, per RFC 10008).
                                      NOT visible in Swagger UI (/docs) - swagger-ui-dist
                                      doesn't render the QUERY method yet. It IS present in
                                      /openapi.json under paths./api/cases.query. See the
                                      handler's docstring below for a curl example.
- GET  /api/cases/{case_id}        : Full judgment metadata, provisions, and citations
- GET/POST/PATCH/DELETE /api/cases/searches[/{field_id}] : CRUD for the advanced
                                      boolean search-builder field definitions
                                      (all/any/exact/none/text) backing GET's public
                                      contract.
"""

import logging
import uuid
from collections import defaultdict
from typing import Optional, List, Dict, Any, Union
from uuid import UUID

import psycopg2
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import db_manager

logger = logging.getLogger("api_backend.search")

router = APIRouter(tags=["Case Search & Details"])


# ============================================================
# REQUEST MODELS
# ============================================================

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
    full_text: Optional[str] = None


class CitationSummary(BaseModel):
    citation: Optional[str] = None
    treatment: Optional[str] = None


class CitationMade(BaseModel):
    citation: Optional[str] = None
    treatment: Optional[str] = None
    cited_case_id: Optional[str] = None


class CitedByItem(BaseModel):
    case_number: Optional[str] = None
    citation: Optional[str] = None
    treatment: Optional[str] = None
    case_id: Optional[str] = None


class CaseListItem(BaseModel):
    id: str
    diary_number: Optional[str] = None
    case_number: Optional[str] = None
    cnr: Optional[str] = None
    neutral_citation: Optional[str] = None
    judgment_date: Optional[str] = None
    case_category: Optional[str] = None
    document_type: Optional[str] = None
    treatment_status: str = "GOOD_LAW"
    overruled: Optional[bool] = None
    is_reported: Optional[bool] = None
    reporting_status: Optional[str] = None
    case_note_ai: Optional[str] = None
    pdf_path: Optional[str] = None
    pdf_url: Optional[str] = None
    court_name: str
    parties: List[PartySummary] = []
    judges: List[JudgeSummary] = []
    provisions: List[ProvisionSummary] = []
    citations: List[CitationSummary] = []


class CaseSearchResponse(BaseModel):
    total: int
    page: int
    limit: int
    total_pages: int
    results: List[CaseListItem]


class CaseDetail(BaseModel):
    id: str
    diary_number: Optional[str] = None
    case_number: Optional[str] = None
    cnr: Optional[str] = None
    neutral_citation: Optional[str] = None
    judgment_date: Optional[str] = None
    registration_date: Optional[str] = None
    case_category: Optional[str] = None
    document_type: Optional[str] = None
    treatment_status: str = "GOOD_LAW"
    overruled: Optional[bool] = None
    is_reported: Optional[bool] = None
    reporting_status: Optional[str] = None
    case_note_ai: Optional[str] = None
    pdf_path: Optional[str] = None
    pdf_url: Optional[str] = None
    court_name: str
    court_id: Optional[str] = None
    parties: List[PartySummary] = []
    judges: List[JudgeSummary] = []
    advocates: List[AdvocateSummary] = []
    provisions: List[ProvisionSummary] = []
    citations_made: List[CitationMade] = []
    cited_by: List[CitedByItem] = []


# ============================================================
# HELPERS
# ============================================================

def _to_valid_uuid(case_id: str) -> Optional[str]:
    """Returns a normalized UUID string if case_id is a real UUID, else None."""
    try:
        return str(uuid.UUID(case_id))
    except (ValueError, AttributeError, TypeError):
        return None


def _fetch_related_for_cases(cur, case_ids: List[str]) -> Dict[str, Dict[str, list]]:
    """
    Batch-fetches parties/judges/provisions/citations for a whole page of case_ids in
    4 queries total (instead of 4 queries PER case — the N+1 pattern this replaces).
    Per-case truncation (parties<=4, provisions<=3, citations<=3, matching the previous
    per-row LIMITs) is applied in Python after grouping.
    """
    related: Dict[str, Dict[str, list]] = defaultdict(lambda: {"parties": [], "judges": [], "provisions": [], "citations": []})
    if not case_ids:
        return related

    cur.execute("SELECT case_id, name, role FROM parties WHERE case_id = ANY(%s::uuid[]);", (case_ids,))
    for case_id, name, role in cur.fetchall():
        if len(related[str(case_id)]["parties"]) < 4:
            related[str(case_id)]["parties"].append({"name": name, "role": role})

    cur.execute("""
        SELECT cj.case_id, jm.canonical_name, cj.role
        FROM case_judges cj JOIN judge_master jm ON cj.judge_id = jm.id
        WHERE cj.case_id = ANY(%s::uuid[]);
    """, (case_ids,))
    for case_id, name, role in cur.fetchall():
        related[str(case_id)]["judges"].append({"name": name, "role": role})

    cur.execute("""
        SELECT pr.case_id, COALESCE(am.canonical_name, pr.raw_act_name) AS act_name, pr.section, pr.provision_full
        FROM provisions pr
        LEFT JOIN act_master am ON pr.act_id = am.id
        WHERE pr.case_id = ANY(%s::uuid[]);
    """, (case_ids,))
    for case_id, act_name, section, full_text in cur.fetchall():
        if len(related[str(case_id)]["provisions"]) < 3:
            related[str(case_id)]["provisions"].append({"act_name": act_name, "section": section, "full_text": full_text})

    cur.execute("""
        SELECT citing_case_id, raw_citation_text, treatment_type
        FROM citations
        WHERE citing_case_id = ANY(%s::uuid[]);
    """, (case_ids,))
    for case_id, citation, treatment in cur.fetchall():
        if len(related[str(case_id)]["citations"]) < 3:
            related[str(case_id)]["citations"].append({"citation": citation, "treatment": treatment})

    return related


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
    """Core parameterized SQL builder & executor."""
    with db_manager.get_pooled_connection() as conn:
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

                    elif f_key == "is_reported":
                        is_reported_val = val_list[0] if isinstance(val_list, list) else val_list
                        where_clauses.append("c.is_reported = %s")
                        params.append(bool(is_reported_val))

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

            case_ids = [str(r[0]) for r in rows]
            related = _fetch_related_for_cases(cur, case_ids)

            results = []
            for r in rows:
                case_id = str(r[0])
                rel = related[case_id]
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
                    "parties": rel["parties"],
                    "judges": rel["judges"],
                    "provisions": rel["provisions"],
                    "citations": rel["citations"]
                })

            return {
                "total": total_records,
                "page": page,
                "limit": limit,
                "total_pages": total_pages,
                "results": results
            }


@router.api_route(
    "/api/cases",
    methods=["QUERY"],
    response_model=CaseSearchResponse,
    summary="Search & list cases (HTTP QUERY method, RFC 10008)",
    description=(
        "NOTE: Swagger UI (/docs) cannot render this operation yet - swagger-ui-dist "
        "has no renderer for the QUERY method as of the RFC 10008 (June 2026) "
        "standardization (tracked in fastapi/fastapi#15839 and upstream swagger-ui "
        "issues). The route itself works correctly over real HTTP; inspect its full "
        "request/response schema directly in /openapi.json under paths./api/cases.query, "
        "or call it with e.g. `curl -X QUERY http://<host>/api/cases -H 'Content-Type: "
        "application/json' -d '{...}'`."
    ),
)
def query_cases(req: SearchRequestModel):
    """
    Configuration-Driven JSON Search API (RFC 10008 QUERY method):
    Accepts structured queries, filter maps, date ranges, and sorting in the
    request body. Returns matching cases in a single round-trip. Filter/facet
    metadata lives separately in GET /api/cases/filters.

    Not visible in Swagger UI (/docs) - see the `description` above / /openapi.json.
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

    try:
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
    except psycopg2.OperationalError:
        logger.exception("Database connection failed during case search")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error during case search")
        raise HTTPException(status_code=500, detail="Case search failed.")


# ============================================================
# SEARCH-FIELD DEFINITIONS (CRUD backing GET /api/cases/searches)
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
    return SearchFieldDefinition(
        id=str(f_id), key=key, label=label, placeholder=placeholder,
        combinator=combinator, isActive=is_active, displayOrder=display_order
    )


@router.get("/api/cases/searches", response_model=SearchFieldsResponse)
def get_configuration_driven_search_fields(
    include_inactive: bool = Query(False, description="Admin use: also return inactive search-field definitions.")
):
    """
    Configuration-Driven Search Builder Metadata API:
    Describes the advanced boolean search fields (All/Any/Exact/None-of-these-words)
    so the frontend can render the search builder form with zero hardcoding.
    """
    try:
        with db_manager.get_pooled_connection() as conn:
            with conn.cursor() as cur:
                if include_inactive:
                    cur.execute("""
                        SELECT id, key, label, placeholder, combinator, is_active, display_order
                        FROM search_field_definitions ORDER BY display_order;
                    """)
                else:
                    cur.execute("""
                        SELECT id, key, label, placeholder, combinator, is_active, display_order
                        FROM search_field_definitions WHERE is_active ORDER BY display_order;
                    """)
                fields = [_row_to_search_field(row) for row in cur.fetchall()]
                return SearchFieldsResponse(fields=fields)
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while loading search fields")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while loading search fields")
        raise HTTPException(status_code=500, detail="Failed to load search field metadata.")


@router.post("/api/cases/searches", response_model=SearchFieldDefinition, status_code=201)
def create_search_field(payload: SearchFieldCreate):
    try:
        with db_manager.get_pooled_connection() as conn:
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
        with db_manager.get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, key, label, placeholder, combinator, is_active, display_order
                    FROM search_field_definitions WHERE id = %s;
                """, (str(field_id),))
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
    """Partial update — only fields present in the request body are changed."""
    updates = payload.model_dump(exclude_unset=True)
    column_map = {
        "key": "key", "label": "label", "placeholder": "placeholder",
        "combinator": "combinator", "isActive": "is_active", "displayOrder": "display_order"
    }
    set_clauses = []
    params: List = []
    for field_name, column in column_map.items():
        if field_name in updates:
            set_clauses.append(f"{column} = %s")
            params.append(updates[field_name])

    try:
        with db_manager.get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM search_field_definitions WHERE id = %s;", (str(field_id),))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Search field not found.")

                if set_clauses:
                    set_clauses.append("updated_at = CURRENT_TIMESTAMP")
                    params.append(str(field_id))
                    cur.execute(f"UPDATE search_field_definitions SET {', '.join(set_clauses)} WHERE id = %s;", params)

                conn.commit()
                cur.execute("""
                    SELECT id, key, label, placeholder, combinator, is_active, display_order
                    FROM search_field_definitions WHERE id = %s;
                """, (str(field_id),))
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
        with db_manager.get_pooled_connection() as conn:
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


@router.get("/api/cases/{case_id:path}", response_model=CaseDetail)
def get_case_detail(case_id: str):
    """Retrieves full case details including parties, judges, advocates, acts, provisions, and citations."""
    valid_uuid = _to_valid_uuid(case_id)
    try:
        with db_manager.get_pooled_connection() as conn:
            with conn.cursor() as cur:
                id_clause = "c.id = %s::uuid OR " if valid_uuid else ""
                id_params = [valid_uuid] if valid_uuid else []
                cur.execute(f"""
                    SELECT c.id, c.diary_number, c.case_number, c.cnr, c.neutral_citation,
                           c.judgment_date, c.registration_date,
                           c.case_category, c.document_type, c.treatment_status, c.overruled,
                           c.is_reported, c.reporting_status, c.case_note_ai, c.pdf_path, c.pdf_url,
                           co.name AS court_name, co.court_id
                    FROM cases c
                    LEFT JOIN courts co ON c.court_id = co.court_id
                    WHERE {id_clause}c.diary_number = %s OR c.case_number = %s;
                """, (*id_params, case_id, case_id))

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
                    SELECT a.name, ca.party_role
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
                    "case_category": row[7],
                    "document_type": row[8],
                    "treatment_status": row[9] or "GOOD_LAW",
                    "overruled": row[10],
                    "is_reported": row[11],
                    "reporting_status": row[12],
                    "case_note_ai": row[13],
                    "pdf_path": row[14],
                    "pdf_url": row[15],
                    "court_name": row[16] or "Supreme Court of India",
                    "court_id": row[17],
                    "parties": parties,
                    "judges": judges,
                    "advocates": advocates,
                    "provisions": provisions,
                    "citations_made": citations_made,
                    "cited_by": cited_by
                }
    except HTTPException:
        raise
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while loading case %s", case_id)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while loading case %s", case_id)
        raise HTTPException(status_code=500, detail="Failed to load case detail.")


