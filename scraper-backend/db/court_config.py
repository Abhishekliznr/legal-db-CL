"""
Reads/watermark writes for `cr_courts` + `cr_court_scrape_config` (api-backend owns the admin CRUD) — the table that turns "N
courts" into "N rows" (docs/scraper-backend-revamp-spec.md §4.3).

This module has no dependency on the adapters/orchestrator/pipeline
packages — it's pure config data, usable (and seedable) before any scraper
adapter exists, which is why it ships in Phase 0. It also does not
validate `adapter` against a fixed set of names — that set grows one court
at a time and is enforced where it matters, at dispatch time, by
orchestrator/registry.py's `ADAPTER_REGISTRY` (a court_scrape_config row
naming an adapter not yet in that registry fails the batch at claim time
in worker.py, not a write-time rejection here).
"""

from typing import Any, Dict, Optional

from db.connection import get_pooled_connection



def get_court_id_by_code(court_code: str) -> Optional[int]:
    """Looks up a court's court_id by its short code (e.g. 'MPHC') — used by a court-specific convenience endpoint that doesn't want its caller to know/pass a numeric court_id."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT court_id FROM cr_courts WHERE court_code = %s;", (court_code,))
            row = cur.fetchone()
            return row[0] if row else None


def get_court_scrape_config(court_id: int) -> Optional[Dict[str, Any]]:
    """What worker.py resolves the adapter and its free-form `config` from."""
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



def update_watermark(court_id: int, last_scraped_to) -> None:
    """Advances a court's resume watermark after a successful scrape run."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cr_court_scrape_config SET last_scraped_to = %s WHERE court_id = %s;",
                (last_scraped_to, court_id),
            )
        conn.commit()
