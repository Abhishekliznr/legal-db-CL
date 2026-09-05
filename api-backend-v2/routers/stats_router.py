"""
Stats Router: Aggregate Database Counters
------------------------------------------------------------------------
GET /api/cases/stats — total judgments, distinct courts/judges/acts actually
present in the corpus. Each count mirrors the same grouping the corresponding
filter facet in filter_router.py's _compute_database_options() uses, so this
number always matches what a user would see if they scrolled a facet's full
option list (not a marketing estimate).

Rewritten against the new schema (spec §6) — same contract as the old
service, different tables underneath (document_coram/document_sections
instead of case_judges/provisions).
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

                cur.execute("SELECT COUNT(DISTINCT judge_id) FROM document_coram;")
                judges = cur.fetchone()[0]

                cur.execute("""
                    SELECT COUNT(DISTINCT st.statute_id)
                    FROM document_sections ds
                    JOIN sections sec ON sec.section_id = ds.section_id
                    JOIN statutes st ON st.statute_id = sec.statute_id;
                """)
                acts = cur.fetchone()[0]

                return DatabaseStatsResponse(judgments=judgments, courts=courts, judges=judges, acts=acts)
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while computing stats")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while computing stats")
        raise HTTPException(status_code=500, detail="Failed to compute database stats.")
