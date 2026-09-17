"""
CRUD for `cr_courts` + `cr_court_scrape_config` — the table that turns "N
courts" into "N rows" (docs/scraper-backend-revamp-spec.md §4.3).

This module has no dependency on the adapters/orchestrator/pipeline
packages — it's pure config data, usable (and seedable) before any scraper
adapter exists, which is why it ships in Phase 0. It also does not
validate `adapter` against a fixed set of names — that set grows one court
at a time and is enforced where it matters, at dispatch time, by
routers/scraper_router.py's `_ADAPTER_REGISTRY` (a court_scrape_config row
naming an adapter not yet in that registry is a clean 500 at scrape-start
time, not a write-time rejection here).
"""

import json
from typing import Any, Dict, List, Optional

from db.connection import get_pooled_connection


def list_courts(active_only: bool = False) -> List[Dict[str, Any]]:
    """Lists courts joined with their scrape config, if any."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            where = "WHERE csc.is_active" if active_only else ""
            cur.execute(f"""
                SELECT c.court_id, c.court_name, c.court_type, c.state, c.ecourts_code, c.court_code,
                       csc.adapter, csc.config, csc.is_active,
                       csc.last_scraped_to, csc.notes
                FROM cr_courts c
                LEFT JOIN cr_court_scrape_config csc ON csc.court_id = c.court_id
                {where}
                ORDER BY c.court_name;
            """)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_court_id_by_code(court_code: str) -> Optional[int]:
    """Looks up a court's court_id by its short code (e.g. 'MPHC') — used by a court-specific convenience endpoint that doesn't want its caller to know/pass a numeric court_id."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT court_id FROM cr_courts WHERE court_code = %s;", (court_code,))
            row = cur.fetchone()
            return row[0] if row else None


def get_court_scrape_config(court_id: int) -> Optional[Dict[str, Any]]:
    """
    Fetches one court's scrape config — what routers/scraper_router.py uses
    to resolve which adapter to run and what free-form `config` to pass it,
    so callers of POST /api/scraper/start never need to know that court's
    own adapter-specific settings themselves.
    """
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.court_id, c.court_name, csc.adapter, csc.config, csc.is_active
                FROM cr_courts c
                JOIN cr_court_scrape_config csc ON csc.court_id = c.court_id
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
    config: Optional[Dict[str, Any]] = None,
    is_active: bool = True,
    notes: Optional[str] = None,
) -> None:
    """Creates or updates the scrape config row for a court. `config` is that court's own free-form adapter settings, stored as JSONB."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cr_court_scrape_config (court_id, adapter, config, is_active, notes)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (court_id) DO UPDATE SET
                    adapter = EXCLUDED.adapter,
                    config = EXCLUDED.config,
                    is_active = EXCLUDED.is_active,
                    notes = EXCLUDED.notes;
            """, (court_id, adapter, json.dumps(config or {}), is_active, notes))
        conn.commit()


def update_watermark(court_id: int, last_scraped_to) -> None:
    """Advances a court's resume watermark after a successful scrape run."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cr_court_scrape_config SET last_scraped_to = %s WHERE court_id = %s;",
                (last_scraped_to, court_id),
            )
        conn.commit()
