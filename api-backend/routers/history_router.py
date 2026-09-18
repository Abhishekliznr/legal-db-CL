"""
Search History Router: Per-User Case Research Search History
------------------------------------------------------------------------
POST /api/cases/history       : Record one search (upserts — re-searching an
                                 existing query bumps it to the top instead of
                                 duplicating it). History is kept indefinitely
                                 (no per-user row cap).
GET  /api/cases/history        : Paginated searches for a user, newest first.

user_id is opaque text supplied by the caller (legal-ui's Next.js server
actions, keyed off the verified NextAuth session) — this service has no
users table of its own and no auth layer; same trust model as the rest of
api-backend (admin-ness / identity is enforced upstream, not here).
"""

import logging
from typing import List, Optional

import psycopg2
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from db.connection import get_pooled_connection

logger = logging.getLogger("api_backend_v2.history")

router = APIRouter(tags=["Case Search & Details"])

# Allow-list — `sort` is interpolated straight into the ORDER BY clause, so it must never
# come from anywhere but this fixed mapping.
SORT_COLUMNS = {
    "date_desc": "created_at DESC",
    "date_asc": "created_at ASC",
    "query_asc": "lower(query) ASC",
    "query_desc": "lower(query) DESC",
}


class RecordSearchHistoryRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    query: str = Field(..., min_length=1)


class SearchHistoryItem(BaseModel):
    id: str
    query: str
    created_at: str


class SearchHistoryResponse(BaseModel):
    items: List[SearchHistoryItem]
    total: int
    page: int
    limit: int
    total_pages: int


@router.post("/api/cases/history", status_code=204)
def record_search_history(payload: RecordSearchHistoryRequest):
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty.")

    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM cr_search_history WHERE user_id = %s AND lower(query) = lower(%s);",
                    (payload.user_id, query),
                )
                cur.execute(
                    "INSERT INTO cr_search_history (user_id, query) VALUES (%s, %s);",
                    (payload.user_id, query),
                )
                conn.commit()
                return None
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while recording search history")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while recording search history")
        raise HTTPException(status_code=500, detail="Failed to record search history.")


@router.get("/api/cases/history", response_model=SearchHistoryResponse)
def get_search_history(
    user_id: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    search: Optional[str] = Query(None, description="Filter by substring of the recorded query text."),
    sort: str = Query("date_desc", description="One of: " + ", ".join(SORT_COLUMNS)),
    date_from: Optional[str] = Query(None, description="Inclusive lower bound, YYYY-MM-DD."),
    date_to: Optional[str] = Query(None, description="Inclusive upper bound, YYYY-MM-DD."),
):
    order_by = SORT_COLUMNS.get(sort, SORT_COLUMNS["date_desc"])
    offset = (page - 1) * limit

    conditions = ["user_id = %s"]
    params: list = [user_id]

    if search:
        conditions.append("query ILIKE %s")
        params.append(f"%{search}%")
    if date_from:
        conditions.append("created_at::date >= %s")
        params.append(date_from)
    if date_to:
        conditions.append("created_at::date <= %s")
        params.append(date_to)

    where_sql = " AND ".join(conditions)

    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM cr_search_history WHERE {where_sql};", params)
                total = cur.fetchone()[0]

                cur.execute(
                    f"""
                    SELECT id, query, created_at
                    FROM cr_search_history
                    WHERE {where_sql}
                    ORDER BY {order_by}
                    LIMIT %s OFFSET %s;
                    """,
                    params + [limit, offset],
                )
                items = [
                    SearchHistoryItem(id=str(row[0]), query=row[1], created_at=row[2].isoformat())
                    for row in cur.fetchall()
                ]
                total_pages = max(1, -(-total // limit))
                return SearchHistoryResponse(items=items, total=total, page=page, limit=limit, total_pages=total_pages)
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while loading search history")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while loading search history")
        raise HTTPException(status_code=500, detail="Failed to load search history.")
