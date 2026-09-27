"""
Seeds `cr_courts` + `cr_court_scrape_config` for the Supreme Court and
Madhya Pradesh High Court -- the only two courts with a real adapter in
scraper-backend's orchestrator/registry.py right now. Runs at api-backend
startup: the scraper only exists as an on-demand Job, and POST
/api/scraper/sc|mp/start needs these rows before any Job has ever run.

Add a High Court to `_HIGH_COURTS` only once scraper-backend has its own
adapter for it (adapters/high_courts/<code>/).

Usage:
    python -m db.seed_courts
"""

from db.connection import get_pooled_connection

# (court_name, state, court_code, adapter) -- court_code is our own short
# code for cr_cases.liznr_id ('LIZNR/<court_code>/<seq>/<year>'); adapter
# must match a key in scraper/courts.py's SUPPORTED_ADAPTERS (and the worker's registry).
_HIGH_COURTS = [
    ("Madhya Pradesh High Court", "Madhya Pradesh", "MPHC", "high_court_mp"),
]

_SUPREME_COURT = ("Supreme Court of India", None, "SCIN", "supreme_court")


def seed() -> None:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            for court_name, state, court_code, adapter in [_SUPREME_COURT, *_HIGH_COURTS]:
                court_type = "Supreme Court" if adapter == "supreme_court" else "High Court"
                cur.execute("""
                    INSERT INTO cr_courts (court_name, court_type, state, court_code)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (court_name) DO UPDATE SET court_code = EXCLUDED.court_code
                    RETURNING court_id;
                """, (court_name, court_type, state, court_code))
                court_id = cur.fetchone()[0]

                cur.execute("""
                    INSERT INTO cr_court_scrape_config (court_id, adapter, is_active)
                    VALUES (%s, %s, TRUE)
                    ON CONFLICT (court_id) DO UPDATE SET adapter = EXCLUDED.adapter;
                """, (court_id, adapter))

        conn.commit()
    print(f"Seeded {1 + len(_HIGH_COURTS)} courts (Supreme Court + {len(_HIGH_COURTS)} High Court(s)).")


def ensure_seeded() -> None:
    """
    Startup-safe entry point: seeds courts/court_scrape_config only the
    first time this runs against a database with no cr_courts rows yet;
    every later call is a cheap no-op. seed() itself is already idempotent
    (ON CONFLICT DO UPDATE throughout) so calling it unconditionally would
    also be safe, but this skips even the no-op INSERT round-trips on every
    restart once a database is already seeded.
    """
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT EXISTS (SELECT 1 FROM cr_courts);")
            already_seeded = cur.fetchone()[0]

    if already_seeded:
        print("cr_courts already has rows, skipping auto-seed.")
        return
    print("No courts found — seeding courts/court_scrape_config for the first time...")
    seed()


if __name__ == "__main__":
    from db.connection import init_connection_pool

    init_connection_pool()
    seed()
