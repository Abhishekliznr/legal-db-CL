"""
Filter Router: Configuration-Driven Filter Metadata API + CRUD
-------------------------------------------------------------------
Provides:
- GET    /api/cases/filters             : Active filter definitions with live/static options.
                                           ?include_inactive=true also returns inactive ones
                                           (admin use) with their options embedded.
- POST   /api/cases/filters              : Create a filter definition.
- GET    /api/cases/filters/{filter_id}  : Get one filter definition with its options.
- PATCH  /api/cases/filters/{filter_id}  : Partially update a filter definition.
- DELETE /api/cases/filters/{filter_id}  : Delete a filter definition (cascades its options).

Two kinds of filters, distinguished by `dataSource`:
- "database" (the 5 built-in filters: court, treatment_status, judge, act,
  judgment_year) — options are always computed live via the fixed,
  whitelisted SQL in `_compute_database_options()`, dispatched by `key`.
  This dispatch is NOT admin-configurable; that's what keeps it free of
  SQL-injection risk. CRUD on these rows only controls presentation
  (label/order/active/queryKey).
- "static" (any new filter an admin creates) — options are real rows in the
  `filter_options` table.

Rewritten again 2026-09-08 for the flattened `cases` schema (array columns
instead of junction tables — see db/schema.sql's rewrite note). Only
`_compute_database_options()` changes: `judge`/`act` now unnest
`cases.bench`/`cases.acts` instead of joining document_coram/
document_sections; `judgment_year` reads `cases.judgment_date` directly
(no more separate `documents` table). `treatment_status` is REMOVED
entirely, not just querying different tables — it read a column computed
from the `citations` table (which citation, if any, treated the document
as Overruled/Doubted/Distinguished), and citation/treatment tracking isn't
modeled by this pipeline iteration at all. db/seed_filters.py no longer
seeds that filter_definitions row, and self-heals a database that already
has one from before this change.
"""

import logging
from typing import List, Literal, Optional
from uuid import UUID

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


class FilterOptionInput(BaseModel):
    label: str
    value: str


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


class FilterCreate(BaseModel):
    key: str
    label: str
    type: str = "select"
    selectionMode: str = "multi"
    dataSource: Literal["database", "static"] = "database"
    queryKey: Optional[str] = None
    isActive: bool = True
    isSearchable: bool = False
    displayOrder: int = 0
    options: Optional[List[FilterOptionInput]] = None


class FilterUpdate(BaseModel):
    key: Optional[str] = None
    label: Optional[str] = None
    type: Optional[str] = None
    selectionMode: Optional[str] = None
    dataSource: Optional[Literal["database", "static"]] = None
    queryKey: Optional[str] = None
    isActive: Optional[bool] = None
    isSearchable: Optional[bool] = None
    displayOrder: Optional[int] = None
    options: Optional[List[FilterOptionInput]] = None


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
            FROM courts c
            LEFT JOIN cases ca ON ca.court_id = c.court_id
            GROUP BY c.court_id, c.court_name
            ORDER BY COUNT(ca.case_id) DESC;
        """)
        return [{"value": r[0], "label": r[1], "count": r[2]} for r in cur.fetchall()]

    if key == "judge":
        cur.execute("""
            SELECT j.full_name, COUNT(DISTINCT c.case_id)
            FROM judges j
            JOIN cases c ON j.judge_id = ANY(c.bench)
            GROUP BY j.full_name
            ORDER BY COUNT(DISTINCT c.case_id) DESC
            LIMIT 30;
        """)
        return [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

    if key == "act":
        cur.execute("""
            SELECT a.act_name, COUNT(DISTINCT c.case_id)
            FROM acts a
            JOIN cases c ON a.act_id = ANY(c.acts)
            GROUP BY a.act_name
            ORDER BY COUNT(DISTINCT c.case_id) DESC
            LIMIT 30;
        """)
        return [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

    if key == "judgment_year":
        cur.execute("""
            SELECT EXTRACT(YEAR FROM judgment_date)::INT AS yr, COUNT(*)
            FROM cases
            WHERE judgment_date IS NOT NULL
            GROUP BY yr
            ORDER BY yr DESC;
        """)
        return [{"value": str(r[0]), "label": str(r[0]), "count": r[1]} for r in cur.fetchall()]

    return []


def _fetch_static_options(cur, filter_id, include_inactive: bool = False) -> List[dict]:
    if include_inactive:
        cur.execute("SELECT value, label FROM filter_options WHERE filter_id = %s ORDER BY display_order;", (str(filter_id),))
    else:
        cur.execute("SELECT value, label FROM filter_options WHERE filter_id = %s AND is_active ORDER BY display_order;", (str(filter_id),))
    return [{"value": r[0], "label": r[1]} for r in cur.fetchall()]


def _replace_filter_options(cur, filter_id, options: List[FilterOptionInput]):
    cur.execute("DELETE FROM filter_options WHERE filter_id = %s;", (str(filter_id),))
    if options:
        cur.executemany(
            "INSERT INTO filter_options (filter_id, value, label, display_order) VALUES (%s, %s, %s, %s);",
            [(str(filter_id), opt.value, opt.label, idx) for idx, opt in enumerate(options)],
        )


def _build_filter_definition(cur, filter_id, include_inactive_options: bool = False) -> Optional[FilterDefinition]:
    cur.execute("""
        SELECT id, key, label, type, selection_mode, query_key, data_source, is_active, is_searchable, display_order
        FROM filter_definitions WHERE id = %s;
    """, (str(filter_id),))
    row = cur.fetchone()
    if not row:
        return None
    f_id, key, label, ftype, selection_mode, query_key, data_source, is_active, is_searchable, display_order = row
    options = (
        _compute_database_options(cur, key) if data_source == "database"
        else _fetch_static_options(cur, f_id, include_inactive=include_inactive_options)
    )
    return FilterDefinition(
        id=str(f_id), key=key, label=label, type=ftype, selectionMode=selection_mode,
        dataSource=data_source, queryKey=query_key, isActive=is_active, isSearchable=is_searchable,
        displayOrder=display_order, options=options,
    )


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
                    FROM filter_definitions {where} ORDER BY display_order;
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


@router.post("/api/cases/filters", response_model=FilterDefinition, response_model_exclude_none=True, status_code=201)
def create_filter(payload: FilterCreate):
    if payload.dataSource == "database" and payload.options:
        raise HTTPException(status_code=400, detail="Cannot set `options` on a 'database'-sourced filter; its options are always computed live.")
    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO filter_definitions (key, label, type, selection_mode, query_key, data_source, is_active, is_searchable, display_order)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id;
                """, (payload.key, payload.label, payload.type, payload.selectionMode, payload.queryKey,
                      payload.dataSource, payload.isActive, payload.isSearchable, payload.displayOrder))
                filter_id = cur.fetchone()[0]
                if payload.dataSource == "static" and payload.options:
                    _replace_filter_options(cur, filter_id, payload.options)
                conn.commit()
                return _build_filter_definition(cur, filter_id, include_inactive_options=True)
    except HTTPException:
        raise
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail=f"A filter with key '{payload.key}' already exists.")
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while creating filter")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while creating filter")
        raise HTTPException(status_code=500, detail="Failed to create filter.")


@router.get("/api/cases/filters/{filter_id}", response_model=FilterDefinition, response_model_exclude_none=True)
def get_filter(filter_id: UUID):
    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                result = _build_filter_definition(cur, filter_id, include_inactive_options=True)
                if result is None:
                    raise HTTPException(status_code=404, detail="Filter not found.")
                return result
    except HTTPException:
        raise
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while loading filter %s", filter_id)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while loading filter %s", filter_id)
        raise HTTPException(status_code=500, detail="Failed to load filter.")


@router.patch("/api/cases/filters/{filter_id}", response_model=FilterDefinition, response_model_exclude_none=True)
def update_filter(filter_id: UUID, payload: FilterUpdate):
    updates = payload.model_dump(exclude_unset=True)
    options_provided = "options" in updates
    options = updates.pop("options", None)

    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT data_source FROM filter_definitions WHERE id = %s;", (str(filter_id),))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Filter not found.")
                effective_data_source = updates.get("dataSource", row[0])
                if effective_data_source == "database" and options_provided:
                    raise HTTPException(status_code=400, detail="Cannot set `options` on a 'database'-sourced filter.")

                column_map = {
                    "key": "key", "label": "label", "type": "type", "selectionMode": "selection_mode",
                    "queryKey": "query_key", "dataSource": "data_source", "isActive": "is_active",
                    "isSearchable": "is_searchable", "displayOrder": "display_order",
                }
                set_clauses, params = [], []
                for field_name, column in column_map.items():
                    if field_name in updates:
                        set_clauses.append(f"{column} = %s")
                        params.append(updates[field_name])

                if set_clauses:
                    set_clauses.append("updated_at = CURRENT_TIMESTAMP")
                    params.append(str(filter_id))
                    cur.execute(f"UPDATE filter_definitions SET {', '.join(set_clauses)} WHERE id = %s;", params)

                if options_provided:
                    option_inputs = [FilterOptionInput(**opt) for opt in (options or [])]
                    _replace_filter_options(cur, filter_id, option_inputs)

                conn.commit()
                return _build_filter_definition(cur, filter_id, include_inactive_options=True)
    except HTTPException:
        raise
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="A filter with that key already exists.")
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while updating filter %s", filter_id)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while updating filter %s", filter_id)
        raise HTTPException(status_code=500, detail="Failed to update filter.")


@router.delete("/api/cases/filters/{filter_id}", status_code=204)
def delete_filter(filter_id: UUID):
    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM filter_definitions WHERE id = %s RETURNING id;", (str(filter_id),))
                deleted = cur.fetchone()
                conn.commit()
                if not deleted:
                    raise HTTPException(status_code=404, detail="Filter not found.")
                return None
    except HTTPException:
        raise
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while deleting filter %s", filter_id)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while deleting filter %s", filter_id)
        raise HTTPException(status_code=500, detail="Failed to delete filter.")
