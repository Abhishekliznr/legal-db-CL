"""
Stats Router: Aggregate Database Counters
------------------------------------------------------------------------
GET /api/cases/stats — total judgments, distinct courts/judges/acts actually
present in the corpus. Each count mirrors the same grouping the corresponding
filter facet in filter_router.py's _compute_database_options() uses, so this
number always matches what a user would see if they scrolled a facet's full
option list (not a marketing estimate).

Rewritten again 2026-09-08 for the flattened `cases` schema — judges/acts
counts now come from unnesting cases.bench/cases.acts instead of joining
document_coram/document_sections (both dropped).
"""

import logging

import psycopg2
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db.connection import get_pooled_connection

logger = logging.getLogger("api_backend_v2.stats")

router = APIRouter(tags=["Case Search & Details"])


class DatabaseStatsResponse(BaseModel):
    judgments: int
    courts: int
    judges: int
    acts: int


@router.get("/api/cases/stats", response_model=DatabaseStatsResponse)
def get_database_stats():
    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM cases;")
                judgments = cur.fetchone()[0]

                cur.execute("SELECT COUNT(DISTINCT court_id) FROM cases WHERE court_id IS NOT NULL;")
                courts = cur.fetchone()[0]

                cur.execute("SELECT COUNT(DISTINCT judge_id) FROM (SELECT unnest(bench) AS judge_id FROM cases) sub;")
                judges = cur.fetchone()[0]

                cur.execute("SELECT COUNT(DISTINCT act_id) FROM (SELECT unnest(acts) AS act_id FROM cases) sub;")
                acts = cur.fetchone()[0]

                return DatabaseStatsResponse(judgments=judgments, courts=courts, judges=judges, acts=acts)
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while computing stats")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while computing stats")
        raise HTTPException(status_code=500, detail="Failed to compute database stats.")
