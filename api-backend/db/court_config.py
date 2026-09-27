"""
CRUD for `cr_courts` + `cr_court_scrape_config`. `adapter` isn't validated here —
routers/scraper_router.py checks it against scraper/courts.py's SUPPORTED_ADAPTERS at
start time, and scraper-backend's worker resolves it against its real registry.
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
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.court_id, c.court_name, c.court_code, csc.adapter, csc.config, csc.is_active
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
