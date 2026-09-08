"""
CRUD for `courts` + `court_scrape_config` — the table that turns "25 Python
files" into "25 rows" (docs/scraper-backend-revamp-spec.md §4.3).

This module has no dependency on the adapters/orchestrator/pipeline
packages — it's pure config data, usable (and seedable) before any scraper
adapter exists, which is why it ships in Phase 0.
"""

from typing import Any, Dict, List, Optional

from db.connection import get_pooled_connection


def list_courts(active_only: bool = False) -> List[Dict[str, Any]]:
    """Lists courts joined with their scrape config, if any."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            where = "WHERE csc.is_active" if active_only else ""
            cur.execute(f"""
                SELECT c.court_id, c.court_name, c.court_type, c.state, c.ecourts_code,
                       csc.adapter, csc.state_code, csc.bench_code, csc.is_active,
                       csc.last_scraped_to, csc.notes
                FROM courts c
                LEFT JOIN court_scrape_config csc ON csc.court_id = c.court_id
                {where}
                ORDER BY c.court_name;
            """)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_court_scrape_config(court_id: int) -> Optional[Dict[str, Any]]:
    """
    Fetches one court's scrape config — what routers/scraper_router.py uses
    to resolve which adapter to run and what state_code/bench_code to pass
    it, so callers of POST /api/scraper/start never need to know an eCourts
    state code themselves.
    """
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.court_id, c.court_name, csc.adapter, csc.state_code, csc.bench_code, csc.is_active
                FROM courts c
                JOIN court_scrape_config csc ON csc.court_id = c.court_id
                WHERE c.court_id = %s;
            """, (court_id,))
            row = cur.fetchone()
            if row is None:
                return None
            columns = [desc[0] for desc in cur.description]
            return dict(zip(columns, row))


def upsert_court_scrape_config(
    court_id: int,
    adapter: str,
    state_code: Optional[str] = None,
    bench_code: Optional[str] = None,
    is_active: bool = True,
    notes: Optional[str] = None,
) -> None:
    """Creates or updates the scrape config row for a court."""
    if adapter not in ("supreme_court", "ecourts"):
        raise ValueError(f"adapter must be 'supreme_court' or 'ecourts', got {adapter!r}")

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO court_scrape_config (court_id, adapter, state_code, bench_code, is_active, notes)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (court_id) DO UPDATE SET
                    adapter = EXCLUDED.adapter,
                    state_code = EXCLUDED.state_code,
                    bench_code = EXCLUDED.bench_code,
                    is_active = EXCLUDED.is_active,
                    notes = EXCLUDED.notes;
            """, (court_id, adapter, state_code, bench_code, is_active, notes))
        conn.commit()


def update_watermark(court_id: int, last_scraped_to) -> None:
    """Advances a court's resume watermark after a successful scrape run."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE court_scrape_config SET last_scraped_to = %s WHERE court_id = %s;",
                (last_scraped_to, court_id),
            )
        conn.commit()
