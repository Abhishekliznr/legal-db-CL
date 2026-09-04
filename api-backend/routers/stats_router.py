"""
Stats Router: Aggregate Database Counters
------------------------------------------------------------------------
GET /api/cases/stats — total judgments, distinct courts/judges/acts actually
present in the corpus. Each count mirrors the same grouping the corresponding
filter facet in filter_router.py's _compute_database_options() uses, so this
number always matches what a user would see if they scrolled a facet's full
option list (not a marketing estimate).
"""

import logging

import psycopg2
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import db_manager

logger = logging.getLogger("api_backend.stats")

router = APIRouter(tags=["Case Search & Details"])


class DatabaseStatsResponse(BaseModel):
    judgments: int
    courts: int
    judges: int
    acts: int


@router.get("/api/cases/stats", response_model=DatabaseStatsResponse)
def get_database_stats():
    try:
        with db_manager.get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM cases;")
                judgments = cur.fetchone()[0]

                cur.execute("SELECT COUNT(DISTINCT court_id) FROM cases WHERE court_id IS NOT NULL;")
                courts = cur.fetchone()[0]

                cur.execute("SELECT COUNT(DISTINCT judge_id) FROM case_judges;")
                judges = cur.fetchone()[0]

                # Matches filter_router.py's act facet grouping exactly (COALESCE canonical
                # name over raw act name) so this total lines up with that facet's option count.
                cur.execute("""
                    SELECT COUNT(DISTINCT COALESCE(am.canonical_name, p.raw_act_name))
                    FROM provisions p
                    LEFT JOIN act_master am ON p.act_id = am.id
                    WHERE COALESCE(am.canonical_name, p.raw_act_name) IS NOT NULL;
                """)
                acts = cur.fetchone()[0]

                return DatabaseStatsResponse(judgments=judgments, courts=courts, judges=judges, acts=acts)
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while computing stats")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while computing stats")
        raise HTTPException(status_code=500, detail="Failed to compute database stats.")
