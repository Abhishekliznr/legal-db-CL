"""
Seeds `courts` + `court_scrape_config` for the Supreme Court and all 25 High
Courts (spec §4.3, §8 Phase 2 "a data-entry task, not a code port").

state_code values below were read out of the old scraper-backend's 25
per-court scripts (each one had a `{COURT}_STATE_CODE = "X~Y"` constant) —
copied out as data, not as code. bench_code is left NULL for every court:
none of the old scripts pinned a specific bench either, they all fell back
to "first available bench option", which adapters/ecourts/adapter.py
reproduces when bench_code is None.

Usage:
    python -m db.seed_courts
"""

from db.connection import get_pooled_connection

# (court_name, state, ecourts_state_code)
_HIGH_COURTS = [
    ("Allahabad High Court", "Uttar Pradesh", "9~13"),
    ("Andhra Pradesh High Court", "Andhra Pradesh", "28~2"),
    ("Bombay High Court", "Maharashtra", "27~1"),
    ("Chhattisgarh High Court", "Chhattisgarh", "22~18"),
    ("Calcutta High Court", "West Bengal", "19~16"),
    ("Delhi High Court", "Delhi", "7~26"),
    ("Gauhati High Court", "Assam", "18~6"),
    ("Gujarat High Court", "Gujarat", "24~17"),
    ("Himachal Pradesh High Court", "Himachal Pradesh", "2~5"),
    ("High Court of Jammu, Kashmir and Ladakh", "Jammu and Kashmir", "1~12"),
    ("Jharkhand High Court", "Jharkhand", "20~7"),
    ("Karnataka High Court", "Karnataka", "29~3"),
    ("Kerala High Court", "Kerala", "32~4"),
    ("Madhya Pradesh High Court", "Madhya Pradesh", "23~2"),
    ("Madras High Court", "Tamil Nadu", "33~10"),
    ("Manipur High Court", "Manipur", "14~25"),
    ("Meghalaya High Court", "Meghalaya", "17~21"),
    ("Orissa High Court", "Odisha", "21~11"),
    ("Patna High Court", "Bihar", "10~22"),
    ("Punjab and Haryana High Court", "Punjab & Haryana", "3~18"),
    ("Rajasthan High Court", "Rajasthan", "8~9"),
    ("Sikkim High Court", "Sikkim", "11~24"),
    ("Telangana High Court", "Telangana", "36~19"),
    ("Tripura High Court", "Tripura", "16~20"),
    ("Uttarakhand High Court", "Uttarakhand", "5~15"),
]

_SUPREME_COURT = ("Supreme Court of India", None)


def seed() -> None:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO courts (court_name, court_type, state)
                VALUES (%s, 'Supreme Court', %s)
                ON CONFLICT (court_name) DO NOTHING
                RETURNING court_id;
            """, _SUPREME_COURT)
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT court_id FROM courts WHERE court_name = %s;", (_SUPREME_COURT[0],))
                row = cur.fetchone()
            sc_court_id = row[0]

            cur.execute("""
                INSERT INTO court_scrape_config (court_id, adapter, is_active)
                VALUES (%s, 'supreme_court', TRUE)
                ON CONFLICT (court_id) DO NOTHING;
            """, (sc_court_id,))

            for court_name, state, state_code in _HIGH_COURTS:
                cur.execute("""
                    INSERT INTO courts (court_name, court_type, state)
                    VALUES (%s, 'High Court', %s)
                    ON CONFLICT (court_name) DO NOTHING
                    RETURNING court_id;
                """, (court_name, state))
                row = cur.fetchone()
                if row is None:
                    cur.execute("SELECT court_id FROM courts WHERE court_name = %s;", (court_name,))
                    row = cur.fetchone()
                court_id = row[0]

                cur.execute("""
                    INSERT INTO court_scrape_config (court_id, adapter, state_code, is_active)
                    VALUES (%s, 'ecourts', %s, TRUE)
                    ON CONFLICT (court_id) DO UPDATE SET state_code = EXCLUDED.state_code;
                """, (court_id, state_code))

        conn.commit()
    print(f"Seeded {1 + len(_HIGH_COURTS)} courts (Supreme Court + {len(_HIGH_COURTS)} High Courts).")


if __name__ == "__main__":
    from db.connection import init_connection_pool
    init_connection_pool()
    seed()
