"""
Provision Router: Act & Section lookups for the Act/Section search filter
------------------------------------------------------------------------
- GET /api/cases/acts           : Acts cited by at least one case, with case counts,
                                  optionally narrowed by `search` (act name or short code).
- GET /api/cases/acts/sections  : Sections of one act (`act` = exact act_name) cited by at
                                  least one case, with case counts.

Both feed the frontend's Act/Section picker, whose selections come back as
SearchRequestModel's `provisions` (db/case_query.py's `_provision_clause`).
Acts are identified by `act_name`, so a name shared by two act_year rows
matches both.

Must be mounted before search_router -- its /api/cases/{case_id:path}
wildcard would otherwise swallow /api/cases/acts.
"""

import logging
from typing import List, Optional

import psycopg2
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from db.connection import get_pooled_connection

logger = logging.getLogger("api_backend_v2.provisions")

router = APIRouter(tags=["Case Filters & Search Metadata"])


class ActOption(BaseModel):
    act_name: str
    short_code: Optional[str] = None
    count: int


class ActsResponse(BaseModel):
    acts: List[ActOption]


class SectionOption(BaseModel):
    section: str
    count: int


class SectionsResponse(BaseModel):
    act_name: str
    sections: List[SectionOption]


@router.get("/api/cases/acts", response_model=ActsResponse)
def list_acts(search: Optional[str] = Query(None), limit: int = Query(50, ge=1, le=200)):
    term = (search or "").strip()
    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    WITH act_counts AS (
                        SELECT unnest(acts) AS act_id, COUNT(*) AS n
                        FROM cr_cases
                        GROUP BY 1
                    )
                    SELECT a.act_name, MAX(a.short_code), SUM(ac.n)::INT
                    FROM cr_acts a
                    JOIN act_counts ac ON ac.act_id = a.act_id
                    WHERE %(term)s = '' OR a.act_name ILIKE %(like)s OR a.short_code ILIKE %(like)s
                    GROUP BY a.act_name
                    ORDER BY SUM(ac.n) DESC, a.act_name
                    LIMIT %(limit)s;
                """, {"term": term, "like": f"%{term}%", "limit": limit})
                return ActsResponse(acts=[
                    ActOption(act_name=r[0], short_code=r[1], count=r[2]) for r in cur.fetchall()
                ])
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while listing acts")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while listing acts (search=%r)", term)
        raise HTTPException(status_code=500, detail="Failed to load acts.")


@router.get("/api/cases/acts/sections", response_model=SectionsResponse)
def list_act_sections(act: str = Query(..., min_length=1), search: Optional[str] = Query(None)):
    term = (search or "").strip()
    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                # Natural order ("2(l)" < "34" < "302" < "302A"): leading integer first, then the
                # raw text -- a plain text sort would put "302" before "34".
                cur.execute("""
                    WITH act_sections AS (
                        SELECT s.section_id, s.section_number
                        FROM cr_sections s
                        JOIN cr_acts a ON a.act_id = s.act_id
                        WHERE a.act_name = %(act)s
                          AND (%(term)s = '' OR s.section_number ILIKE %(like)s)
                    ),
                    section_counts AS (
                        SELECT sid AS section_id, COUNT(*) AS n
                        FROM cr_cases c, unnest(c.sections) AS sid
                        WHERE c.sections && (SELECT COALESCE(array_agg(section_id), '{}') FROM act_sections)
                        GROUP BY sid
                    )
                    SELECT ast.section_number, SUM(sc.n)::INT
                    FROM act_sections ast
                    JOIN section_counts sc ON sc.section_id = ast.section_id
                    GROUP BY ast.section_number
                    ORDER BY NULLIF(substring(ast.section_number FROM '^[0-9]+'), '')::INT NULLS LAST,
                             ast.section_number;
                """, {"act": act, "term": term, "like": f"{term}%"})
                return SectionsResponse(act_name=act, sections=[
                    SectionOption(section=r[0], count=r[1]) for r in cur.fetchall()
                ])
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while listing sections for act %r", act)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while listing sections for act %r", act)
        raise HTTPException(status_code=500, detail="Failed to load sections.")
