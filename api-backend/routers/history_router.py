"""
Search History Router: Per-User Recent Case Research Searches
------------------------------------------------------------------------
POST /api/cases/history       : Record one search (upserts — re-searching an
                                 existing query bumps it to the top instead of
                                 duplicating it), then trims that user's
                                 history down to the most recent MAX_HISTORY_PER_USER.
GET  /api/cases/history        : Most recent searches for a user, newest first.

user_id is opaque text supplied by the caller (legal-ui's Next.js server
actions, keyed off the verified NextAuth session) — this service has no
users table of its own and no auth layer; same trust model as the rest of
api-backend (admin-ness / identity is enforced upstream, not here).

Logic unchanged from the old api-backend — this feature is entirely
independent of the case-law domain migration (spec §6), it just needed its
own table (case_research_search_history, api-backend-only per
db/schema.sql §9) and the new connection module.
"""

import logging
from typing import List

import psycopg2
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from db.connection import get_pooled_connection

logger = logging.getLogger("api_backend_v2.history")

router = APIRouter(tags=["Case Search & Details"])

MAX_HISTORY_PER_USER = 20


class RecordSearchHistoryRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    query: str = Field(..., min_length=1)


class SearchHistoryItem(BaseModel):
    id: str
    query: str
    created_at: str


class SearchHistoryResponse(BaseModel):
    items: List[SearchHistoryItem]


@router.post("/api/cases/history", status_code=204)
def record_search_history(payload: RecordSearchHistoryRequest):
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty.")

    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM case_research_search_history WHERE user_id = %s AND lower(query) = lower(%s);",
                    (payload.user_id, query),
                )
                cur.execute(
                    "INSERT INTO case_research_search_history (user_id, query) VALUES (%s, %s);",
                    (payload.user_id, query),
                )
                cur.execute(
                    """
                    DELETE FROM case_research_search_history
                    WHERE user_id = %s AND id NOT IN (
                        SELECT id FROM case_research_search_history
                        WHERE user_id = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                    );
                    """,
                    (payload.user_id, payload.user_id, MAX_HISTORY_PER_USER),
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
    limit: int = Query(10, ge=1, le=MAX_HISTORY_PER_USER),
):
    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, query, created_at
                    FROM case_research_search_history
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s;
                    """,
                    (user_id, limit),
                )
                items = [
                    SearchHistoryItem(id=str(row[0]), query=row[1], created_at=row[2].isoformat())
                    for row in cur.fetchall()
                ]
                return SearchHistoryResponse(items=items)
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while loading search history")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while loading search history")
        raise HTTPException(status_code=500, detail="Failed to load search history.")
