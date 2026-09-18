"""
Filter Router: Configuration-Driven Filter Metadata API (read-only)
-------------------------------------------------------------------
Provides:
- GET    /api/cases/filters             : Active filter definitions with live/static options.
                                           ?include_inactive=true also returns inactive ones
                                           (admin use) with their options embedded.

Two kinds of filters, distinguished by `dataSource`:
- "database" (built-in filters: court, judge, act, judgment_year,
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
"""

import logging
from typing import List, Optional

import psycopg2
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from db.connection import get_pooled_connection

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


# ============================================================
# OPTION COMPUTATION — fixed, whitelisted per-key SQL against the new schema
# ============================================================

def _compute_database_options(cur, key: str) -> List[dict]:
    if key == "court":
        cur.execute("""
            SELECT c.court_id::text, c.court_name, COUNT(ca.case_id)
            FROM cr_courts c
            LEFT JOIN cr_cases ca ON ca.court_id = c.court_id
            GROUP BY c.court_id, c.court_name
            ORDER BY COUNT(ca.case_id) DESC;
        """)
        return [{"value": r[0], "label": r[1], "count": r[2]} for r in cur.fetchall()]

    if key == "judge":
        cur.execute("""
            SELECT j.full_name, COUNT(DISTINCT c.case_id)
            FROM cr_judges j
            JOIN cr_cases c ON j.judge_id = ANY(c.bench)
            GROUP BY j.full_name
            ORDER BY COUNT(DISTINCT c.case_id) DESC
            LIMIT 30;
        """)
        return [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

    if key == "act":
        cur.execute("""
            SELECT a.act_name, COUNT(DISTINCT c.case_id)
            FROM cr_acts a
            JOIN cr_cases c ON a.act_id = ANY(c.acts)
            GROUP BY a.act_name
            ORDER BY COUNT(DISTINCT c.case_id) DESC
            LIMIT 30;
        """)
        return [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

    if key == "judgment_year":
        cur.execute("""
            SELECT EXTRACT(YEAR FROM judgment_date)::INT AS yr, COUNT(*)
            FROM cr_cases
            WHERE judgment_date IS NOT NULL
            GROUP BY yr
            ORDER BY yr DESC;
        """)
        return [{"value": str(r[0]), "label": str(r[0]), "count": r[1]} for r in cur.fetchall()]

    if key == "disposition":
        cur.execute("""
            SELECT disposition::text, COUNT(*)
            FROM cr_cases
            WHERE disposition IS NOT NULL
            GROUP BY disposition
            ORDER BY COUNT(*) DESC;
        """)
        return [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

    if key == "favouring_party":
        cur.execute("""
            SELECT favouring_party::text, COUNT(*)
            FROM cr_cases
            WHERE favouring_party IS NOT NULL
            GROUP BY favouring_party
            ORDER BY COUNT(*) DESC;
        """)
        return [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

    if key == "industry":
        # LLM-classified (scraper-backend/pipeline/llm_enrichment.py) — no
        # reliable regex signal exists for this field, so counts here can be
        # sparse until enrichment has run over most of the corpus.
        cur.execute("""
            SELECT i.industry_name, COUNT(DISTINCT c.case_id)
            FROM cr_industries i
            JOIN cr_cases c ON i.industry_id = ANY(c.industries)
            GROUP BY i.industry_name
            ORDER BY COUNT(DISTINCT c.case_id) DESC
            LIMIT 30;
        """)
        return [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

    if key == "ministry":
        cur.execute("""
            SELECT m.ministry_name, COUNT(DISTINCT c.case_id)
            FROM cr_ministries m
            JOIN cr_cases c ON m.ministry_id = ANY(c.ministries)
            GROUP BY m.ministry_name
            ORDER BY COUNT(DISTINCT c.case_id) DESC
            LIMIT 30;
        """)
        return [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

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

@router.get("/api/cases/filters", response_model=FiltersResponse, response_model_exclude_none=True)
def get_configuration_driven_filters(include_inactive: bool = Query(False)):
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
                    options = (
                        _compute_database_options(cur, key) if data_source == "database"
                        else _fetch_static_options(cur, f_id, include_inactive=include_inactive)
                    )
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
