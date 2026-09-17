"""
Seeds `cr_courts` + `cr_court_scrape_config` for the Supreme Court and
Madhya Pradesh High Court -- the only two courts with a real adapter
registered in routers/scraper_router.py's `_ADAPTER_REGISTRY` right now.

High Courts are no longer scraped via a single generic eCourts adapter
(retired — see adapters/__init__.py); each gets its own adapter under
adapters/high_courts/<code>/, built one court at a time. Madhya Pradesh's
adapter (adapters/high_courts/mp/) landed with three pieces still pending
reference material (see its own docstring) but is safe to register: those
gaps make it a no-op (yields zero records), not a crash.

Every other High Court previously seeded here (Delhi, Bombay, Kerala, ...)
was removed along with the eCourts adapter they were configured for — none
of them are being scraped right now. Re-add a court to `_HIGH_COURTS` below
once it has its own real adapter, following the Madhya Pradesh pattern.

Usage:
    python -m db.seed_courts
"""

from db.connection import get_pooled_connection

# (court_name, state, court_code, adapter) -- court_code is our own short
# code for cr_cases.liznr_id ('LIZNR/<court_code>/<seq>/<year>'); adapter
# must match a key in routers/scraper_router.py's _ADAPTER_REGISTRY.
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
