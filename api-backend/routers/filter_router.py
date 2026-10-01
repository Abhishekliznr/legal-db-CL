"""
Filter Router: Configuration-Driven Filter Metadata API (read-only)
-------------------------------------------------------------------
Provides:
- QUERY  /api/cases/filters             : Active filter definitions with live/static options,
                                           "database" options' counts scoped to the request body's
                                           search (same shape as QUERY /api/cases's query/filters/
                                           date -- see search_router.py). ?include_inactive=true
                                           also returns inactive ones (admin use) with their
                                           options embedded.

Two kinds of filters, distinguished by `dataSource`:
- "database" (built-in filters: court, judge, judgment_year,
  disposition, favouring_party, industry, ministry — see
  db/seed_filters.py's `_FILTER_DEFINITIONS`) — options are always computed
  live via the fixed, whitelisted SQL in `_compute_database_options()`,
  dispatched by `key`. This dispatch is NOT admin-configurable; that's what
  keeps it free of SQL-injection risk.
- "static" (any filter defined directly in the database) — options are real
  rows in the `cr_filter_options` table.

Rewritten again 2026-09-08 for the flattened `cr_cases` schema (array
columns instead of junction tables — see db/schema.sql's rewrite note).
Only `_compute_database_options()` changes: `judge`/`act` now unnest
`cr_cases.bench`/`cr_cases.acts` instead of joining document_coram/
document_sections; `judgment_year` reads `cr_cases.judgment_date` directly
(no more separate `documents` table). `treatment_status` is REMOVED
entirely, not just querying different tables — it read a column computed
from the `citations` table (which citation, if any, treated the document
as Overruled/Doubted/Distinguished), and citation/treatment tracking isn't
modeled by this pipeline iteration at all. db/seed_filters.py no longer
seeds that cr_filter_definitions row, and self-heals a database that already
has one from before this change.

Extended 2026-09-10 for scraper-backend's llm_enrichment.py rewrite, which
now actually populates `cr_cases.disposition` (LLM fallback, on top of the
existing regex classifier)/`favouring_party`/`industries` at meaningful
volume — four new dispatch keys added to `_compute_database_options()`
(disposition, favouring_party, industry, ministry; `ministry` existed as
data before but had no filter facet). `industry`/`favouring_party` option
lists can be sparse until enrichment has caught up on the whole corpus,
since both are LLM-only with no regex fallback.

Changed GET -> QUERY (RFC 10008, same as QUERY /api/cases -- see
search_router.py's module docstring) 2026-09-23: `_compute_database_options()`
now scopes every count to the same search (text query + filters + date
range) the results list is running, instead of a static whole-database
count, and drops any option -- or the whole filter, if every one of its
options drops -- that has zero matches under that search. A facet's own
count still ignores its OWN selected values while applying every other
active filter (db/case_query.py's `exclude_filter_keys`), so picking one
court doesn't make every other court vanish from the court filter -- only
narrows sibling facets. "static" filters (admin-defined, no live count) are
unaffected -- there's no case data to scope them by.

2026-09-26: the "act" facet was removed -- superseded by the Act/Section
picker (routers/provision_router.py + the request body's `provisions`),
which matches acts exactly instead of by ILIKE substring. db/seed_filters.py
self-heals the old cr_filter_definitions row away. build_case_where() still
accepts an `act`/`acts` key in `filters` for existing API callers.
"""

import logging
from typing import Any, Dict, List, Literal, Optional, Union

import psycopg2
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from db.case_query import build_case_where, parse_search_request
from db.connection import get_pooled_connection
from routers.search_router import ProvisionFilterModel, SearchDateRangeModel, SearchQueryModel

logger = logging.getLogger("api_backend_v2.filters")

router = APIRouter(tags=["Case Filters & Search Metadata"])


# ============================================================
# MODELS
# ============================================================

class FilterOption(BaseModel):
    value: str
    label: str
    count: Optional[int] = Field(None, description="Only present for 'database'-sourced filters; 'static' options have no live count.")


class FilterDefinition(BaseModel):
    id: str
    key: str
    label: str
    type: str
    selectionMode: str
    dataSource: str
    queryKey: Optional[str] = Field(None, description="Only present when it differs from `key`.")
    isActive: bool
    isSearchable: bool = False
    displayOrder: int
    options: List[FilterOption] = []


class DateRangeFilter(BaseModel):
    label: str = "Judgment Date Range"
    type: str = "date_range"
    location: str = Field("top-level", description="Sent as the separate top-level `date: {from, to}` field on the search request, not inside `filters`.")


class FiltersResponse(BaseModel):
    filters: List[FilterDefinition]
    dateRange: DateRangeFilter


class FiltersRequestModel(BaseModel):
    """Same query/filters/date shape as QUERY /api/cases's SearchRequestModel (no
    sort/page/limit -- this endpoint doesn't paginate) -- one search, sent to both
    endpoints, so the sidebar's counts never drift from what the results list itself
    matches."""
    query: Optional[Union[SearchQueryModel, str]] = None
    filters: Optional[Dict[str, Any]] = Field(default_factory=dict)
    date: Optional[SearchDateRangeModel] = None
    provisions: List[ProvisionFilterModel] = Field(default_factory=list)
    provisions_match: Literal["any", "all"] = "any"


# ============================================================
# OPTION COMPUTATION — fixed, whitelisted per-key SQL against the new schema
# ============================================================

# Every filters_dict alias execute_case_search()/build_case_where() dispatch on for a given
# facet key -- e.g. "court" is stored/matched as either "court" or "court_id" (its queryKey).
# Used to exclude a facet's OWN selected values from the WHERE clause that computes ITS OWN
# option counts, so selecting one court doesn't make every other court disappear from the court
# filter (it should still narrow judge/act/year/etc counts -- just not its own facet).
_FILTER_KEY_ALIASES = {
    "court": ("court", "court_id"),
    "judge": ("judge", "judges"),
    "judgment_year": ("judgment_year", "year"),
    "disposition": ("disposition",),
    "favouring_party": ("favouring_party",),
    "industry": ("industry", "industries"),
    "ministry": ("ministry", "ministries"),
    "judgment": ("judgment",),
}


def _compute_database_options(cur, key: str, search: Dict[str, Any]) -> List[dict]:
    where_sql, params = build_case_where(
        **search, exclude_filter_keys=set(_FILTER_KEY_ALIASES.get(key, (key,))),
    )
    matched_cte = f"SELECT v.case_id FROM cr_case_search_view v {where_sql}"

    if key == "court":
        cur.execute(f"""
            WITH matched AS ({matched_cte})
            SELECT c.court_id::text, c.court_name, COUNT(*)
            FROM cr_courts c
            JOIN cr_cases ca ON ca.court_id = c.court_id
            JOIN matched m ON m.case_id = ca.case_id
            GROUP BY c.court_id, c.court_name
            ORDER BY COUNT(*) DESC;
        """, params)
        return [{"value": r[0], "label": r[1], "count": r[2]} for r in cur.fetchall()]

    if key == "judge":
        cur.execute(f"""
            WITH matched AS ({matched_cte})
            SELECT j.full_name, COUNT(DISTINCT m.case_id)
            FROM cr_judges j
            JOIN cr_cases c ON j.judge_id = ANY(c.bench)
            JOIN matched m ON m.case_id = c.case_id
            GROUP BY j.full_name
            ORDER BY COUNT(DISTINCT m.case_id) DESC
            LIMIT 30;
        """, params)
        return [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

    if key == "judgment_year":
        cur.execute(f"""
            WITH matched AS ({matched_cte})
            SELECT EXTRACT(YEAR FROM c.judgment_date)::INT AS yr, COUNT(*)
            FROM cr_cases c
            JOIN matched m ON m.case_id = c.case_id
            WHERE c.judgment_date IS NOT NULL
            GROUP BY yr
            ORDER BY yr DESC;
        """, params)
        return [{"value": str(r[0]), "label": str(r[0]), "count": r[1]} for r in cur.fetchall()]

    if key == "disposition":
        cur.execute(f"""
            WITH matched AS ({matched_cte})
            SELECT c.disposition::text, COUNT(*)
            FROM cr_cases c
            JOIN matched m ON m.case_id = c.case_id
            WHERE c.disposition IS NOT NULL
            GROUP BY c.disposition
            ORDER BY COUNT(*) DESC;
        """, params)
        return [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

    if key == "favouring_party":
        cur.execute(f"""
            WITH matched AS ({matched_cte})
            SELECT c.favouring_party::text, COUNT(*)
            FROM cr_cases c
            JOIN matched m ON m.case_id = c.case_id
            WHERE c.favouring_party IS NOT NULL
            GROUP BY c.favouring_party
            ORDER BY COUNT(*) DESC;
        """, params)
        return [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

    if key == "industry":
        # LLM-classified (scraper-backend/pipeline/llm_enrichment.py) — no
        # reliable regex signal exists for this field, so counts here can be
        # sparse until enrichment has run over most of the corpus.
        cur.execute(f"""
            WITH matched AS ({matched_cte})
            SELECT i.industry_name, COUNT(DISTINCT m.case_id)
            FROM cr_industries i
            JOIN cr_cases c ON i.industry_id = ANY(c.industries)
            JOIN matched m ON m.case_id = c.case_id
            GROUP BY i.industry_name
            ORDER BY COUNT(DISTINCT m.case_id) DESC
            LIMIT 30;
        """, params)
        return [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

    if key == "ministry":
        cur.execute(f"""
            WITH matched AS ({matched_cte})
            SELECT m.ministry_name, COUNT(DISTINCT mm.case_id)
            FROM cr_ministries m
            JOIN cr_cases c ON m.ministry_id = ANY(c.ministries)
            JOIN matched mm ON mm.case_id = c.case_id
            GROUP BY m.ministry_name
            ORDER BY COUNT(DISTINCT mm.case_id) DESC
            LIMIT 30;
        """, params)
        return [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

    if key == "judgment":
        cur.execute(f"""
            WITH matched AS ({matched_cte})
            SELECT (c.judgment_status = 'AVAILABLE') AS available, COUNT(*)
            FROM cr_cases c
            JOIN matched m ON m.case_id = c.case_id
            GROUP BY available
            ORDER BY available DESC;
        """, params)
        return [
            {"value": "available", "label": "Judgment available", "count": r[1]} if r[0]
            else {"value": "missing", "label": "Judgment not available yet", "count": r[1]}
            for r in cur.fetchall()
        ]

    return []


def _fetch_static_options(cur, filter_id, include_inactive: bool = False) -> List[dict]:
    if include_inactive:
        cur.execute("SELECT value, label FROM cr_filter_options WHERE filter_id = %s ORDER BY display_order;", (str(filter_id),))
    else:
        cur.execute("SELECT value, label FROM cr_filter_options WHERE filter_id = %s AND is_active ORDER BY display_order;", (str(filter_id),))
    return [{"value": r[0], "label": r[1]} for r in cur.fetchall()]


# ============================================================
# ENDPOINTS
# ============================================================

@router.api_route(
    "/api/cases/filters", methods=["QUERY"], response_model=FiltersResponse, response_model_exclude_none=True,
    summary="Filter definitions + search-scoped option counts (HTTP QUERY method, RFC 10008)",
    description="Not renderable in Swagger UI — see /openapi.json under paths./api/cases/filters.query, or `curl -X QUERY`.",
)
def get_configuration_driven_filters(req: FiltersRequestModel, include_inactive: bool = Query(False)):
    search = parse_search_request(req)
    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                where = "" if include_inactive else "WHERE is_active"
                cur.execute(f"""
                    SELECT id, key, label, type, selection_mode, query_key, data_source, is_active, is_searchable, display_order
                    FROM cr_filter_definitions {where} ORDER BY display_order;
                """)
                rows = cur.fetchall()

                filters = []
                for f_id, key, label, ftype, selection_mode, query_key, data_source, is_active, is_searchable, display_order in rows:
                    if data_source == "database":
                        options = _compute_database_options(cur, key, search)
                        if not options:
                            # Nothing matches the current search under this facet -- an
                            # empty filter is dead UI, so drop the whole definition
                            # rather than render a header with no options under it.
                            continue
                    else:
                        # "static" options are admin-defined rows with no live count
                        # (FilterOption.count stays None for these) -- not scoped by
                        # search, always shown as configured.
                        options = _fetch_static_options(cur, f_id, include_inactive=include_inactive)

                    filters.append(FilterDefinition(
                        id=str(f_id), key=key, label=label, type=ftype, selectionMode=selection_mode,
                        dataSource=data_source, queryKey=query_key, isActive=is_active, isSearchable=is_searchable,
                        displayOrder=display_order, options=options,
                    ))

                return FiltersResponse(filters=filters, dateRange=DateRangeFilter())
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while loading filters")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while loading filters")
        raise HTTPException(status_code=500, detail="Failed to load filter metadata.")
