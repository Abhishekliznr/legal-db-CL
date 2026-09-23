"""
Search Router: Case Search, Case Details & Search-Field Definitions
------------------------------------------------------------------------
Handles:
- QUERY /api/cases                 : Structured JSON search (filters, search fields, page,
                                      limit, sort in the request body, per RFC 10008).
                                      NOT visible in Swagger UI (/docs) - swagger-ui-dist
                                      doesn't render the QUERY method. See /openapi.json
                                      under paths./api/cases.query, or curl -X QUERY.
- GET  /api/cases/{case_id}        : Full case metadata + provisions.
- GET  /api/cases/searches          : Advanced boolean search-builder field
                                      definitions (read-only).

Rewritten again 2026-09-08 for the flattened `cr_cases` schema (db/schema.sql's
rewrite note) — both endpoints now read `cr_case_search_view` (this service's
own convenience view resolving cases' id-arrays to display names; NOT the
same view the pre-rewrite schema had, and scraper-backend has no
equivalent). Free-text search uses `cr_cases.search_vector` (re-added to the
schema specifically because this router needs it — the flattened schema's
first draft omitted it).

Permanently dropped from every response below, not just renamed — no
backing data exists for any of these in the current pipeline (see
db/schema.sql's rewrite note): advocates/counsel, citations made/cited-by
and the treatment_status field they computed, holdings, prior appellate
history, and procedural timeline. A case number can also no longer own
several distinct case numbers under one document (the old documents/cases
split) — one row IS one case now, so `document_id` as a separate concept is
gone too; `case_id` is the only identifier.

Updated 2026-09-10 for scraper-backend's llm_enrichment.py rewrite, which
now actually populates fields this router previously had no data for:
`favouring_party` (list + detail), `industries` (detail), and a
relevant/other split of the provisions the case cites — CaseDetail's
`relevant_provisions`/`other_provisions`, resolved the same way as the
existing `provisions` field but from `cr_cases.sections_relevant/_other`
(and the matching rules_/orders_ columns) instead of the unified
sections/rules/orders arrays. See db/schema.sql's cr_cases comment for why
these aren't guaranteed a strict partition of `provisions`.

Updated 2026-09-11: both endpoints now project every field
`cr_case_search_view` exposes except `judgement`/`ocr_text` on the list
endpoint (full opinion text and raw OCR text are detail-only — too large
per row on a paginated list). Also reverses the 2026-09-08 "URL
construction is a frontend concern" decision for the PDF blob: this
service now also returns `blob_pdf_url`, `blob_pdf_id` resolved against
BLOB_BASE_URL/BLOB_CONTAINER env vars server-side (null if either is
unset) — `blob_pdf_id` itself is kept as-is for callers that already
build their own link.

Updated 2026-09-12: `get_case_detail`'s own SELECT had silently never
actually fetched `judgement`/`ocr_text` (both were declared on `CaseDetail`
and unconditionally serialized as null; `tests/manual_phase4_e2e.py`'s
`detail["ocr_text"]` assertion was accordingly broken) — fixed.

Updated 2026-09-13: `cr_case_search_view` gained a `provisions` column
(act+section/rule/order pairs, resolved the same way `get_case_detail`'s
unified `provisions` is — not the _relevant/_other LLM subsets) since the
existing `act_names` here is act-level only, with no section attached.
`CaseListItem.provisions` projects it so the frontend's case-research quick
view can show per-act sections without a second request to the detail
endpoint.

Also `get_case_detail`'s `provisions`/`relevant_provisions`/`other_provisions`
(the UNION ALL of sections/rules/orders, unified and LLM-classified) are
replaced with `sections`/`rules`/`orders`, each already split by kind and
carrying only the unified (non-_relevant/_other) set -- the detail page
now renders one row per kind and dropped the relevant/other split, and nothing
else read those fields.

Updated 2026-09-19: Rules/Orders as a distinct provision kind, and the
sections_relevant/_other + rules_/orders_ LLM-classified split, were
dropped from `cr_cases` entirely (scraper-backend/db/migrations/0012) --
only Section-level provisions are tracked now. `CaseDetail.rules`/`.orders`
are gone; `CaseDetail.sections`/`CaseListItem.provisions` are unaffected
(both already resolved from `cr_cases.sections` only). Both endpoints also
gained `cnr` (Madhya Pradesh's case-status page exposes one; most other
sources won't populate it, so expect null on those rows).
"""

import logging
import os
from typing import Optional, List, Dict, Any, Union

import psycopg2
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from db.connection import get_pooled_connection

logger = logging.getLogger("api_backend_v2.search")

router = APIRouter(tags=["Case Search & Details"])

_BLOB_BASE_URL = os.environ.get("BLOB_BASE_URL", "").strip().rstrip("/")
_BLOB_CONTAINER = os.environ.get("BLOB_CONTAINER", "").strip().strip("/")


def _build_blob_pdf_url(blob_pdf_id: Optional[str]) -> Optional[str]:
    if not blob_pdf_id or not _BLOB_BASE_URL or not _BLOB_CONTAINER:
        return None
    return f"{_BLOB_BASE_URL}/{_BLOB_CONTAINER}/{blob_pdf_id}"


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
    advocate: Optional[str] = None


class JudgeSummary(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None


class ProvisionSummary(BaseModel):
    act_name: Optional[str] = None
    section: Optional[str] = None


class CaseListItem(BaseModel):
    id: str
    case_number: Optional[str] = None
    cnr: Optional[str] = None
    liznr_id: Optional[str] = None
    neutral_citation: Optional[str] = None
    judgment_date: Optional[str] = None
    language: Optional[str] = None
    disposition: Optional[str] = None
    document_type: Optional[str] = None
    subject: Optional[str] = None
    case_category: List[str] = []
    case_note: Optional[str] = None
    conclusion: Optional[str] = None
    favouring_party: Optional[str] = None
    # blob_pdf_id is the bare path within the blob container (e.g.
    # "SCIN/<checksum>.pdf"); blob_pdf_url is that path resolved against
    # BLOB_BASE_URL/BLOB_CONTAINER server-side, null if either env var is
    # unset. source_pdf_url is already a complete URL (the court's own
    # site) and can be used as-is.
    blob_pdf_id: Optional[str] = None
    blob_pdf_url: Optional[str] = None
    source_pdf_url: Optional[str] = None
    court_name: str
    court_id: Optional[str] = None
    judgment_by: Optional[str] = None
    parties: List[PartySummary] = []
    judges: List[JudgeSummary] = []
    acts: List[str] = []
    provisions: List[ProvisionSummary] = []
    ministries: List[str] = []
    industries: List[str] = []
    needs_review: bool = False
    # Filing year only, parsed from case_number -- see db/schema.sql's
    # cr_cases.filing_year comment for why this isn't a full filing date.
    filing_year: Optional[int] = None


class CaseSearchResponse(BaseModel):
    total: int
    page: int
    limit: int
    total_pages: int
    results: List[CaseListItem]


class CaseDetail(BaseModel):
    id: str
    case_number: Optional[str] = None
    cnr: Optional[str] = None
    liznr_id: Optional[str] = None
    neutral_citation: Optional[str] = None
    judgment_date: Optional[str] = None
    language: Optional[str] = None
    disposition: Optional[str] = None
    document_type: Optional[str] = None
    case_note: Optional[str] = None
    conclusion: Optional[str] = None
    judgement: Optional[str] = None
    ocr_text: Optional[str] = None
    blob_pdf_id: Optional[str] = None
    blob_pdf_url: Optional[str] = None
    source_pdf_url: Optional[str] = None
    court_name: str
    court_id: Optional[str] = None
    judgment_by: Optional[str] = None
    parties: List[PartySummary] = []
    judges: List[JudgeSummary] = []
    # Unified sections array (rules/orders as a distinct kind, and the
    # LLM-classified _relevant/_other subsets, were both dropped 2026-09-19 --
    # scraper-backend/db/migrations/0012).
    sections: List[ProvisionSummary] = []
    subject: Optional[str] = None
    case_category: List[str] = []
    ministries: List[str] = []
    industries: List[str] = []
    favouring_party: Optional[str] = None
    needs_review: bool = False
    filing_year: Optional[int] = None


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
                    OR v.neutral_citation ILIKE %s
                )""")
                params.extend([tsquery_str, ilike_str, ilike_str])

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

                    elif f_key in ("judgment_year", "year"):
                        int_years = [int(y) for y in val_list if str(y).isdigit()]
                        if int_years:
                            where_clauses.append("EXTRACT(YEAR FROM v.judgment_date)::INT = ANY(%s)")
                            params.append(int_years)

                    elif f_key in ("judge", "judges"):
                        judge_likes = [f"%{str(j).strip()}%" for j in val_list if str(j).strip()]
                        if judge_likes:
                            where_clauses.append("EXISTS (SELECT 1 FROM unnest(COALESCE(v.bench_names, ARRAY[]::text[])) bn WHERE bn ILIKE ANY(%s))")
                            params.append(judge_likes)

                    elif f_key in ("act", "acts"):
                        act_likes = [f"%{str(a).strip()}%" for a in val_list if str(a).strip()]
                        if act_likes:
                            where_clauses.append("EXISTS (SELECT 1 FROM unnest(COALESCE(v.act_names, ARRAY[]::text[])) an WHERE an ILIKE ANY(%s))")
                            params.append(act_likes)

                    elif f_key == "disposition":
                        # v.disposition is disposition_category_enum, not
                        # text -- ANY() against a plain text[] param fails
                        # with "operator does not exist" without this cast
                        # (found via a real run, not just inspection).
                        where_clauses.append("v.disposition::text = ANY(%s)")
                        params.append(val_list)

                    elif f_key == "favouring_party":
                        # Same enum-cast reasoning as disposition above.
                        where_clauses.append("v.favouring_party::text = ANY(%s)")
                        params.append(val_list)

                    elif f_key in ("industry", "industries"):
                        industry_likes = [f"%{str(i).strip()}%" for i in val_list if str(i).strip()]
                        if industry_likes:
                            where_clauses.append("EXISTS (SELECT 1 FROM unnest(COALESCE(v.industry_names, ARRAY[]::text[])) ind WHERE ind ILIKE ANY(%s))")
                            params.append(industry_likes)

                    elif f_key in ("ministry", "ministries"):
                        ministry_likes = [f"%{str(m).strip()}%" for m in val_list if str(m).strip()]
                        if ministry_likes:
                            where_clauses.append("EXISTS (SELECT 1 FROM unnest(COALESCE(v.ministry_names, ARRAY[]::text[])) mn WHERE mn ILIKE ANY(%s))")
                            params.append(ministry_likes)

            # 3. Date range
            if from_date:
                where_clauses.append("v.judgment_date >= %s")
                params.append(from_date)
            if to_date:
                where_clauses.append("v.judgment_date <= %s")
                params.append(to_date)

            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            cur.execute(f"SELECT COUNT(*) FROM cr_case_search_view v {where_sql};", params)
            total_records = cur.fetchone()[0]

            offset = (page - 1) * limit
            total_pages = (total_records + limit - 1) // limit if total_records > 0 else 1
            direction = "ASC" if str(sort_direction).lower() == "asc" else "DESC"
            order_sql = f"ORDER BY v.judgment_date {direction} NULLS LAST"

            cur.execute(f"""
                SELECT v.case_id, v.case_number, v.cnr, v.liznr_id, v.neutral_citation, v.judgment_date,
                       v.language, v.disposition, v.document_type, v.subject_name, v.category_names,
                       v.case_note, v.conclusion, v.blob_pdf_id, v.source_pdf_url, v.court_name,
                       v.court_id, v.judgment_by_name, v.petitioner, v.respondent,
                       v.petitioner_advocate, v.respondent_advocate, v.filing_year, v.bench_names,
                       v.act_names, v.ministry_names, v.industry_names, v.favouring_party, v.needs_review,
                       v.provisions
                FROM cr_case_search_view v
                {where_sql}
                {order_sql}
                LIMIT %s OFFSET %s;
            """, params + [limit, offset])
            rows = cur.fetchall()

            results = []
            for r in rows:
                (case_id, case_number, cnr, liznr_id, neutral_citation, judgment_date,
                 language, disposition, document_type, subject_name, category_names,
                 case_note, conclusion, blob_pdf_id, source_pdf_url, court_name,
                 court_id, judgment_by_name, petitioner, respondent,
                 petitioner_advocate, respondent_advocate, filing_year, bench_names,
                 act_names, ministry_names, industry_names, favouring_party, needs_review,
                 provisions) = r

                parties = ([{"name": petitioner, "role": "PETITIONER", "advocate": petitioner_advocate}] if petitioner else []) + \
                          ([{"name": respondent, "role": "RESPONDENT", "advocate": respondent_advocate}] if respondent else [])
                judges = [{"name": j, "role": None} for j in (bench_names or [])]

                results.append({
                    "id": str(case_id),
                    "case_number": case_number,
                    "cnr": cnr,
                    "liznr_id": liznr_id,
                    "neutral_citation": neutral_citation,
                    "judgment_date": judgment_date.strftime("%Y-%m-%d") if judgment_date else None,
                    "language": language,
                    "disposition": disposition,
                    "document_type": document_type,
                    "subject": subject_name,
                    "case_category": category_names or [],
                    "case_note": case_note,
                    "conclusion": conclusion,
                    "favouring_party": favouring_party,
                    "blob_pdf_id": blob_pdf_id,
                    "blob_pdf_url": _build_blob_pdf_url(blob_pdf_id),
                    "source_pdf_url": source_pdf_url,
                    "court_name": court_name,
                    "court_id": str(court_id) if court_id else None,
                    "judgment_by": judgment_by_name,
                    "parties": parties,
                    "judges": judges,
                    "acts": act_names or [],
                    "provisions": provisions or [],
                    "ministries": ministry_names or [],
                    "industries": industry_names or [],
                    "needs_review": needs_review,
                    "filing_year": filing_year,
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
                    FROM cr_search_field_definitions {where} ORDER BY display_order;
                """)
                return SearchFieldsResponse(fields=[_row_to_search_field(r) for r in cur.fetchall()])
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while loading search fields")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while loading search fields")
        raise HTTPException(status_code=500, detail="Failed to load search field metadata.")


# ============================================================
# CASE DETAIL
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
                id_clause = "v.case_id = %s OR " if valid_int_id is not None else ""
                id_params = [valid_int_id] if valid_int_id is not None else []
                cur.execute(f"""
                    SELECT v.case_id, v.case_number, v.cnr, v.liznr_id, v.neutral_citation, v.judgment_date,
                           v.language, v.disposition, v.document_type, v.case_note, v.conclusion,
                           v.judgement, v.ocr_text,
                           v.blob_pdf_id, v.source_pdf_url, v.court_name, v.court_id,
                           v.judgment_by_name, v.petitioner, v.respondent,
                           v.petitioner_advocate, v.respondent_advocate, v.filing_year,
                           v.bench_names, v.subject_name,
                           v.category_names, v.ministry_names, v.industry_names, v.favouring_party, v.needs_review
                    FROM cr_case_search_view v
                    WHERE {id_clause}v.case_number = %s OR v.liznr_id = %s;
                """, (*id_params, case_id, case_id))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Case not found.")

                (db_case_id, case_number, cnr, liznr_id, neutral_citation, judgment_date,
                 language, disposition, document_type, case_note, conclusion,
                 judgement, ocr_text,
                 blob_pdf_id, source_pdf_url, court_name, court_id,
                 judgment_by_name, petitioner, respondent,
                 petitioner_advocate, respondent_advocate, filing_year,
                 bench_names, subject_name,
                 category_names, ministry_names, industry_names, favouring_party, needs_review) = row

                parties = ([{"name": petitioner, "role": "PETITIONER", "advocate": petitioner_advocate}] if petitioner else []) + \
                          ([{"name": respondent, "role": "RESPONDENT", "advocate": respondent_advocate}] if respondent else [])
                judges = [{"name": j, "role": None} for j in (bench_names or [])]

                # Sections reconstructed from the raw id array (not carried
                # by cr_case_search_view), resolved back to `cr_acts` for the
                # act name that goes with each number. (Used to also do this
                # for rules/orders -- dropped 2026-09-19 along with those
                # columns, scraper-backend/db/migrations/0012.)
                cur.execute("""
                    SELECT a.act_name, s.section_number
                    FROM cr_cases c
                    JOIN cr_sections s ON s.section_id = ANY(c.sections)
                    JOIN cr_acts a ON a.act_id = s.act_id
                    WHERE c.case_id = %s;
                """, (db_case_id,))
                sections = [{"act_name": p[0], "section": p[1]} for p in cur.fetchall()]

                return {
                    "id": str(db_case_id), "case_number": case_number, "cnr": cnr, "liznr_id": liznr_id,
                    "neutral_citation": neutral_citation,
                    "judgment_date": judgment_date.strftime("%Y-%m-%d") if judgment_date else None,
                    "language": language, "disposition": disposition, "document_type": document_type,
                    "case_note": case_note, "conclusion": conclusion,
                    "judgement": judgement, "ocr_text": ocr_text,
                    "blob_pdf_id": blob_pdf_id, "blob_pdf_url": _build_blob_pdf_url(blob_pdf_id),
                    "source_pdf_url": source_pdf_url,
                    "court_name": court_name, "court_id": str(court_id) if court_id else None,
                    "judgment_by": judgment_by_name,
                    "parties": parties, "judges": judges,
                    "sections": sections,
                    "subject": subject_name, "case_category": category_names or [],
                    "ministries": ministry_names or [], "industries": industry_names or [],
                    "favouring_party": favouring_party, "needs_review": needs_review,
                    "filing_year": filing_year,
                }
    except HTTPException:
        raise
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while loading case %s", case_id)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while loading case %s", case_id)
        raise HTTPException(status_code=500, detail="Failed to load case detail.")
