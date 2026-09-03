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
  judgment_year) — options are always computed live via the fixed, whitelisted
  SQL in `_compute_database_options()`, dispatched by `key`. This dispatch is
  NOT admin-configurable; that's what keeps it free of SQL-injection risk.
  CRUD on these rows only controls presentation (label/order/active/queryKey).
- "static" (any new filter an admin creates) — options are real rows in the
  `filter_options` table, supplied inline as `options: [{label, value}]` on
  the same POST/PATCH call that creates/updates the filter. No live counts.
"""

import logging
from typing import List, Literal, Optional
from uuid import UUID

import psycopg2
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import db_manager

logger = logging.getLogger("api_backend.filters")

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
    queryKey: Optional[str] = Field(
        None, description="Only present when it differs from `key` — the field name to use inside the QUERY /api/cases `filters` object."
    )
    isActive: bool
    isSearchable: bool = Field(False, description="Frontend hint: render a search box inside the option list — useful for large option lists.")
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
    options: Optional[List[FilterOptionInput]] = Field(
        None, description="Only meaningful when dataSource='static' — becomes real filter_options rows."
    )


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
    options: Optional[List[FilterOptionInput]] = Field(
        None, description="If included (even as an empty list), fully replaces this filter's options. Omit to leave options untouched."
    )


class DateRangeFilter(BaseModel):
    label: str = "Decision Date Range"
    type: str = "date_range"
    location: str = Field(
        "top-level",
        description="Unlike the entries in `filters`, date range is NOT sent inside the `filters` object — "
                     "it's the separate top-level `date: {from, to}` field on the QUERY /api/cases request body."
    )


class FiltersResponse(BaseModel):
    filters: List[FilterDefinition]
    dateRange: DateRangeFilter


# ============================================================
# OPTION COMPUTATION
# ============================================================

def _compute_database_options(cur, key: str) -> List[dict]:
    """
    Fixed, whitelisted per-key SQL for 'database'-sourced filters. Deliberately
    NOT driven by any admin-supplied table/column name — that's what keeps this
    free of SQL-injection risk. Returns [] for a key with no wired data source.
    """
    if key == "court":
        cur.execute("""
            SELECT COALESCE(c.court_id, 'SCIN'), COALESCE(ct.name, 'Supreme Court of India'), COUNT(c.id)
            FROM cases c
            LEFT JOIN courts ct ON c.court_id = ct.court_id
            GROUP BY c.court_id, ct.name
            ORDER BY COUNT(c.id) DESC;
        """)
        options = [{"value": r[0], "label": r[1], "count": r[2]} for r in cur.fetchall()]
        return options or [{"value": "SCIN", "label": "Supreme Court of India", "count": 0}]

    if key == "treatment_status":
        cur.execute("""
            SELECT treatment_status, COUNT(id)
            FROM cases
            WHERE treatment_status IS NOT NULL
            GROUP BY treatment_status
            ORDER BY COUNT(id) DESC;
        """)
        treatment_counts = {r[0]: r[1] for r in cur.fetchall()}
        treatment_labels = {
            "GOOD_LAW": "Good Law (Affirmed)",
            "OVERRULED": "Overruled",
            "DOUBTED": "Doubted",
            "DISTINGUISHED": "Distinguished"
        }
        return [
            {"value": st, "label": treatment_labels.get(st, st), "count": treatment_counts.get(st, 0)}
            for st in ["GOOD_LAW", "OVERRULED", "DOUBTED", "DISTINGUISHED"]
        ]

    if key == "judge":
        cur.execute("""
            SELECT j.canonical_name, COUNT(DISTINCT cj.case_id)
            FROM case_judges cj
            JOIN judge_master j ON cj.judge_id = j.id
            GROUP BY j.canonical_name
            ORDER BY COUNT(DISTINCT cj.case_id) DESC
            LIMIT 30;
        """)
        return [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

    if key == "act":
        cur.execute("""
            SELECT COALESCE(a.canonical_name, p.raw_act_name), COUNT(DISTINCT p.case_id)
            FROM provisions p
            LEFT JOIN act_master a ON p.act_id = a.id
            WHERE COALESCE(a.canonical_name, p.raw_act_name) IS NOT NULL
            GROUP BY COALESCE(a.canonical_name, p.raw_act_name)
            ORDER BY COUNT(DISTINCT p.case_id) DESC
            LIMIT 30;
        """)
        return [{"value": r[0], "label": r[0], "count": r[1]} for r in cur.fetchall()]

    if key == "judgment_year":
        cur.execute("""
            SELECT
                COALESCE(EXTRACT(YEAR FROM judgment_date)::INT, 2025) AS yr,
                COUNT(id)
            FROM cases
            GROUP BY yr
            ORDER BY yr DESC;
        """)
        options = [{"value": str(r[0]), "label": str(r[0]), "count": r[1]} for r in cur.fetchall()]
        return options or [{"value": "2025", "label": "2025", "count": 0}]

    return []


def _fetch_static_options(cur, filter_id, include_inactive: bool = False) -> List[dict]:
    if include_inactive:
        cur.execute("SELECT value, label FROM filter_options WHERE filter_id = %s ORDER BY display_order;", (str(filter_id),))
    else:
        cur.execute("SELECT value, label FROM filter_options WHERE filter_id = %s AND is_active ORDER BY display_order;", (str(filter_id),))
    return [{"value": r[0], "label": r[1]} for r in cur.fetchall()]


def _replace_filter_options(cur, filter_id, options: List[FilterOptionInput]):
    """Deletes all existing options for this filter and inserts the given list as new rows."""
    cur.execute("DELETE FROM filter_options WHERE filter_id = %s;", (str(filter_id),))
    if options:
        cur.executemany(
            "INSERT INTO filter_options (filter_id, value, label, display_order) VALUES (%s, %s, %s, %s);",
            [(str(filter_id), opt.value, opt.label, idx) for idx, opt in enumerate(options)]
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
        displayOrder=display_order, options=options
    )


# ============================================================
# ENDPOINTS
# ============================================================

@router.get("/api/cases/filters", response_model=FiltersResponse, response_model_exclude_none=True)
def get_configuration_driven_filters(
    include_inactive: bool = Query(False, description="Admin use: also return inactive filter definitions.")
):
    """
    Configuration-Driven Filter Metadata API:
    Returns frontend-ready filter definitions with live/static options from PostgreSQL.
    """
    try:
        with db_manager.get_pooled_connection() as conn:
            with conn.cursor() as cur:
                if include_inactive:
                    cur.execute("""
                        SELECT id, key, label, type, selection_mode, query_key, data_source, is_active, is_searchable, display_order
                        FROM filter_definitions ORDER BY display_order;
                    """)
                else:
                    cur.execute("""
                        SELECT id, key, label, type, selection_mode, query_key, data_source, is_active, is_searchable, display_order
                        FROM filter_definitions WHERE is_active ORDER BY display_order;
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
                        displayOrder=display_order, options=options
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
    """Creates a new filter definition. `options` (if given) only apply when dataSource='static'."""
    if payload.dataSource == "database" and payload.options:
        raise HTTPException(status_code=400, detail="Cannot set `options` on a 'database'-sourced filter; its options are always computed live.")
    try:
        with db_manager.get_pooled_connection() as conn:
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
        with db_manager.get_pooled_connection() as conn:
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
    """Partial update — only fields present in the request body are changed."""
    updates = payload.model_dump(exclude_unset=True)
    options_provided = "options" in updates
    options = updates.pop("options", None)

    try:
        with db_manager.get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT data_source FROM filter_definitions WHERE id = %s;", (str(filter_id),))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Filter not found.")
                current_data_source = row[0]
                effective_data_source = updates.get("dataSource", current_data_source)

                if effective_data_source == "database" and options_provided:
                    raise HTTPException(status_code=400, detail="Cannot set `options` on a 'database'-sourced filter; its options are always computed live.")

                column_map = {
                    "key": "key", "label": "label", "type": "type",
                    "selectionMode": "selection_mode", "queryKey": "query_key",
                    "dataSource": "data_source", "isActive": "is_active",
                    "isSearchable": "is_searchable", "displayOrder": "display_order"
                }
                set_clauses = []
                params: List = []
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
        with db_manager.get_pooled_connection() as conn:
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
