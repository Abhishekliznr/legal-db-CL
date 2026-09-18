"""
Manual Phase 2 dispatch check — NOT a pytest suite, run directly.

Verifies the config-driven dispatch in routers/scraper_router.py: given a
court_id, it resolves the right adapter class and the right state_code/
bench_code from cr_court_scrape_config, without actually running a scrape
(the real EcourtsAdapter/SupremeCourtAdapter network logic still needs a
live dry run — see the "NOT LIVE-VERIFIED" notes in each adapter module).

Run against a real Postgres with schema.sql applied and `python -m
db.seed_courts` already run:

    export DATABASE_TYPE=postgres DB_HOST=localhost DB_PORT=5432 \\
           DB_NAME=liznrlegal DB_USER=postgres DB_PASSWORD=testpass
    python3 -m tests.manual.phase2_dispatch
"""

from unittest.mock import patch

from fastapi.testclient import TestClient

from db.connection import get_pooled_connection, init_connection_pool
import api


def _court_id_for(name: str) -> int:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT court_id FROM cr_courts WHERE court_name = %s;", (name,))
            row = cur.fetchone()
            assert row is not None, f"court not seeded: {name}"
            return row[0]


def main():
    init_connection_pool()
    sc_court_id = _court_id_for("Supreme Court of India")
    delhi_court_id = _court_id_for("Delhi High Court")

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM cr_courts;")
            total = cur.fetchone()[0]
            print(f"Total seeded courts: {total}")
            assert total == 26, f"expected 26 (1 Supreme Court + 25 High Courts), got {total}"

            cur.execute("SELECT count(*) FROM cr_court_scrape_config WHERE adapter = 'ecourts';")
            ecourts_count = cur.fetchone()[0]
            print(f"cr_court_scrape_config rows with adapter='ecourts': {ecourts_count}")
            assert ecourts_count == 25

    with TestClient(api.app) as client:
        # Mock the actual batch execution — we're only testing dispatch, not
        # a live scrape (which needs real network access this sandbox lacks).
        with patch("routers.scraper_router.batch_runner.run_batch"):
            r = client.post("/api/scraper/start", json={
                "court_id": sc_court_id, "court_code": "SCIN",
                "from_date": "2026-01-01", "to_date": "2026-01-05",
            })
            print("\nSupreme Court /start response:", r.status_code, r.json())
            assert r.status_code == 200
            assert r.json()["adapter"] == "supreme_court"

            r = client.post("/api/scraper/start", json={
                "court_id": delhi_court_id, "court_code": "DHC",
                "from_date": "2026-01-01", "to_date": "2026-01-05",
            })
            print("Delhi HC /start response:", r.status_code, r.json())
            assert r.status_code == 200
            assert r.json()["adapter"] == "ecourts"

        # Error paths
        r = client.post("/api/scraper/start", json={"court_id": 999999, "court_code": "XX"})
        print("\nUnseeded court_id:", r.status_code, r.json())
        assert r.status_code == 404

        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE cr_court_scrape_config SET is_active = FALSE WHERE court_id = %s;", (delhi_court_id,))
            conn.commit()

        r = client.post("/api/scraper/start", json={"court_id": delhi_court_id, "court_code": "DHC"})
        print("Inactive court:", r.status_code, r.json())
        assert r.status_code == 400

        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE cr_court_scrape_config SET is_active = TRUE, state_code = NULL WHERE court_id = %s;", (delhi_court_id,))
            conn.commit()

        r = client.post("/api/scraper/start", json={"court_id": delhi_court_id, "court_code": "DHC"})
        print("eCourts court with no state_code:", r.status_code, r.json())
        assert r.status_code == 400

    print("\nALL PHASE 2 DISPATCH CHECKS PASSED")


if __name__ == "__main__":
    main()
