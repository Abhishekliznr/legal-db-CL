"""
Filter Router: Dedicated Endpoints for Dynamic Legal Search Filters & Facets
----------------------------------------------------------------------------
Handles:
- GET /api/filters        : Dynamic database filter values (Courts, Judges, Acts, Years, Treatments)
- GET /api/cases/facets   : Manupatra-style facet aggregations with live case counts
"""

from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException

try:
    from backend import db_manager
except ImportError:
    import db_manager

router = APIRouter(tags=["Dynamic Filters & Facets"])


@router.get("/api/filters", response_model=Dict[str, Any])
def get_dynamic_filters():
    """
    Returns dynamic list of available dropdown filters from PostgreSQL:
    - Courts
    - Judgment Years
    - Citator Treatment Statuses (GOOD_LAW, OVERRULED, DOUBTED, DISTINGUISHED)
    - Canonical Judges
    - Canonical Acts / Laws
    """
    conn = db_manager.get_connection()
    try:
        with conn.cursor() as cur:
            # 1. Courts
            cur.execute("SELECT court_id, name FROM courts ORDER BY name;")
            courts = [{"court_id": r[0], "name": r[1]} for r in cur.fetchall()]

            # 2. Years
            cur.execute("""
                SELECT DISTINCT COALESCE(
                    EXTRACT(YEAR FROM judgment_date)::INT,
                    EXTRACT(YEAR FROM registration_date)::INT,
                    2025
                ) AS year
                FROM cases
                ORDER BY year DESC;
            """)
            years = [r[0] for r in cur.fetchall() if r[0] is not None]
            if not years:
                years = [2025, 2024, 2023, 2022, 2021]

            # 3. Treatment Statuses
            cur.execute("SELECT DISTINCT treatment_status FROM cases WHERE treatment_status IS NOT NULL ORDER BY treatment_status;")
            treatments = [r[0] for r in cur.fetchall()]
            if not treatments:
                treatments = ["GOOD_LAW", "OVERRULED", "DOUBTED", "DISTINGUISHED"]

            # 4. Canonical Judges
            cur.execute("SELECT DISTINCT canonical_name FROM judge_master ORDER BY canonical_name LIMIT 100;")
            judges = [r[0] for r in cur.fetchall()]

            # 5. Canonical Acts
            cur.execute("SELECT DISTINCT canonical_name FROM act_master ORDER BY canonical_name LIMIT 100;")
            acts = [r[0] for r in cur.fetchall()]

            return {
                "courts": courts,
                "years": years,
                "treatments": treatments,
                "judges": judges,
                "acts": acts
            }
    finally:
        conn.close()


@router.get("/api/cases/facets", response_model=Dict[str, Any])
def get_case_facets():
    """
    Manupatra-style Facet Aggregations API:
    Returns live category counts directly from PostgreSQL for the sidebar:
    - Court counts (e.g. Supreme Court of India: 104)
    - Treatment counts (Good Law, Overruled, Doubted, Distinguished)
    - Year / Period counts (2020 & Above, 2010-2019)
    - Top Judges by case frequency
    - Top Acts / Laws by case frequency
    """
    conn = db_manager.get_connection()
    try:
        with conn.cursor() as cur:
            # 1. Courts with Case Counts
            cur.execute("""
                SELECT COALESCE(c.court_id, 'SCIN'), COALESCE(ct.name, 'Supreme Court of India'), COUNT(c.id)
                FROM cases c
                LEFT JOIN courts ct ON c.court_id = ct.court_id
                GROUP BY c.court_id, ct.name
                ORDER BY COUNT(c.id) DESC;
            """)
            courts_facets = [{"court_id": r[0], "name": r[1], "count": r[2]} for r in cur.fetchall()]

            # 2. Treatment Statuses with Case Counts
            cur.execute("""
                SELECT treatment_status, COUNT(id)
                FROM cases
                WHERE treatment_status IS NOT NULL
                GROUP BY treatment_status
                ORDER BY COUNT(id) DESC;
            """)
            treatment_facets = [{"status": r[0], "count": r[1]} for r in cur.fetchall()]

            # 3. Top Judges with Case Counts
            cur.execute("""
                SELECT j.canonical_name, COUNT(DISTINCT cj.case_id)
                FROM case_judges cj
                JOIN judge_master j ON cj.judge_id = j.id
                GROUP BY j.canonical_name
                ORDER BY COUNT(DISTINCT cj.case_id) DESC
                LIMIT 25;
            """)
            judge_facets = [{"name": r[0], "count": r[1]} for r in cur.fetchall()]

            # 4. Top Acts with Case Counts
            cur.execute("""
                SELECT COALESCE(a.canonical_name, p.raw_act_name), COUNT(DISTINCT p.case_id)
                FROM provisions p
                LEFT JOIN act_master a ON p.act_id = a.id
                WHERE COALESCE(a.canonical_name, p.raw_act_name) IS NOT NULL
                GROUP BY COALESCE(a.canonical_name, p.raw_act_name)
                ORDER BY COUNT(DISTINCT p.case_id) DESC
                LIMIT 25;
            """)
            act_facets = [{"name": r[0], "count": r[1]} for r in cur.fetchall()]

            # 5. Period Aggregations
            cur.execute("""
                SELECT 
                    COUNT(CASE WHEN EXTRACT(YEAR FROM judgment_date) >= 2020 THEN 1 END) AS count_2020_above,
                    COUNT(CASE WHEN EXTRACT(YEAR FROM judgment_date) BETWEEN 2010 AND 2019 THEN 1 END) AS count_2010_2019,
                    COUNT(CASE WHEN EXTRACT(YEAR FROM judgment_date) BETWEEN 2000 AND 2009 THEN 1 END) AS count_2000_2009,
                    COUNT(CASE WHEN EXTRACT(YEAR FROM judgment_date) < 2000 THEN 1 END) AS count_pre_2000
                FROM cases;
            """)
            p_row = cur.fetchone() or (0, 0, 0, 0)
            periods = [
                {"label": "2020 and Above", "count": p_row[0]},
                {"label": "Between 2010 and 2019", "count": p_row[1]},
                {"label": "Between 2000 and 2009", "count": p_row[2]},
                {"label": "Before 2000", "count": p_row[3]}
            ]

            return {
                "courts": courts_facets,
                "treatments": treatment_facets,
                "judges": judge_facets,
                "acts": act_facets,
                "periods": periods
            }
    finally:
        conn.close()
