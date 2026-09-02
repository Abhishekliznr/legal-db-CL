"""
Filter Router: Configuration-Driven Filter Metadata & Facets API
-----------------------------------------------------------------
Provides:
- GET /api/case-research/filters : Configuration-driven metadata with live options & counts
- GET /api/filters               : Legacy/standard format for backward compatibility
- GET /api/cases/facets          : Facet counts dictionary for direct lookup
"""

from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException

try:
    from backend import db_manager
except ImportError:
    import db_manager

router = APIRouter(tags=["Dynamic Filters & Facets"])


@router.get("/api/case-research/filters", response_model=Dict[str, Any])
@router.get("/api/filters", response_model=Dict[str, Any])
def get_configuration_driven_filters():
    """
    Configuration-Driven Filter Metadata API:
    Returns frontend-ready filter definitions with live counts from PostgreSQL.
    The frontend can render the sidebar dynamically with zero hardcoding.
    """
    conn = db_manager.get_connection()
    try:
        with conn.cursor() as cur:
            # 1. Courts with live case count
            cur.execute("""
                SELECT COALESCE(c.court_id, 'SCIN'), COALESCE(ct.name, 'Supreme Court of India'), COUNT(c.id)
                FROM cases c
                LEFT JOIN courts ct ON c.court_id = ct.court_id
                GROUP BY c.court_id, ct.name
                ORDER BY COUNT(c.id) DESC;
            """)
            court_options = [
                {"value": r[0], "label": r[1], "count": r[2]}
                for r in cur.fetchall()
            ]
            if not court_options:
                court_options = [{"value": "SCIN", "label": "Supreme Court of India", "count": 0}]

            # 2. Treatment Statuses with live counts
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
            treatment_options = [
                {
                    "value": st,
                    "label": treatment_labels.get(st, st),
                    "count": treatment_counts.get(st, 0)
                }
                for st in ["GOOD_LAW", "OVERRULED", "DOUBTED", "DISTINGUISHED"]
            ]

            # 3. Top Judges with live counts
            cur.execute("""
                SELECT j.canonical_name, COUNT(DISTINCT cj.case_id)
                FROM case_judges cj
                JOIN judge_master j ON cj.judge_id = j.id
                GROUP BY j.canonical_name
                ORDER BY COUNT(DISTINCT cj.case_id) DESC
                LIMIT 30;
            """)
            judge_options = [
                {"value": r[0], "label": r[0], "count": r[1]}
                for r in cur.fetchall()
            ]

            # 4. Top Acts / Laws with live counts
            cur.execute("""
                SELECT COALESCE(a.canonical_name, p.raw_act_name), COUNT(DISTINCT p.case_id)
                FROM provisions p
                LEFT JOIN act_master a ON p.act_id = a.id
                WHERE COALESCE(a.canonical_name, p.raw_act_name) IS NOT NULL
                GROUP BY COALESCE(a.canonical_name, p.raw_act_name)
                ORDER BY COUNT(DISTINCT p.case_id) DESC
                LIMIT 30;
            """)
            act_options = [
                {"value": r[0], "label": r[0], "count": r[1]}
                for r in cur.fetchall()
            ]

            # 5. Judgment Years with live counts
            cur.execute("""
                SELECT 
                    COALESCE(EXTRACT(YEAR FROM judgment_date)::INT, 2025) AS yr,
                    COUNT(id)
                FROM cases
                GROUP BY yr
                ORDER BY yr DESC;
            """)
            year_options = [
                {"value": str(r[0]), "label": str(r[0]), "count": r[1]}
                for r in cur.fetchall()
            ]
            if not year_options:
                year_options = [{"value": "2025", "label": "2025", "count": 0}]

            # Configuration-driven filters list
            filters = [
                {
                    "key": "court",
                    "label": "Court",
                    "type": "select",
                    "selectionMode": "multi",
                    "dataSource": "database",
                    "queryKey": "court_id",
                    "options": court_options
                },
                {
                    "key": "treatment_status",
                    "label": "Treatment Status",
                    "type": "select",
                    "selectionMode": "multi",
                    "dataSource": "database",
                    "queryKey": "treatment_status",
                    "options": treatment_options
                },
                {
                    "key": "judge",
                    "label": "Judge / Bench",
                    "type": "select",
                    "selectionMode": "multi",
                    "dataSource": "database",
                    "queryKey": "judge",
                    "options": judge_options
                },
                {
                    "key": "act",
                    "label": "Act / Law",
                    "type": "select",
                    "selectionMode": "multi",
                    "dataSource": "database",
                    "queryKey": "act",
                    "options": act_options
                },
                {
                    "key": "judgment_year",
                    "label": "Judgment Year",
                    "type": "select",
                    "selectionMode": "multi",
                    "dataSource": "database",
                    "queryKey": "judgment_year",
                    "options": year_options
                },
                {
                    "key": "date",
                    "label": "Decision Date Range",
                    "type": "date_range",
                    "selectionMode": "single",
                    "queryKey": "date_range"
                }
            ]

            # Return both modern "filters" list and backward-compatible flat structure
            return {
                "filters": filters,
                "courts": court_options,
                "treatments": treatment_options,
                "judges": [j["label"] for j in judge_options],
                "acts": [a["label"] for a in act_options],
                "years": [int(y["value"]) for y in year_options]
            }
    finally:
        conn.close()


@router.get("/api/cases/facets", response_model=Dict[str, Any])
def get_case_facets():
    """Returns dynamic facet counts for all filter dimensions."""
    data = get_configuration_driven_filters()
    return {
        "court": data.get("courts", []),
        "treatment_status": data.get("treatments", []),
        "judge": data.get("filters", [])[2].get("options", []) if len(data.get("filters", [])) > 2 else [],
        "act": data.get("filters", [])[3].get("options", []) if len(data.get("filters", [])) > 3 else [],
        "judgment_year": data.get("filters", [])[4].get("options", []) if len(data.get("filters", [])) > 4 else []
    }
