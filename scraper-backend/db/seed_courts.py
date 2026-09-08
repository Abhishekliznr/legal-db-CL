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

# (court_name, state, ecourts_state_code, court_code)
# court_code is our own short code for cases.liznr_id
# ('LIZNR/<court_code>/<seq>/<year>') -- same short-code convention already
# used ad hoc as the scraper's court_code request param (e.g. "DHC" in
# tests/manual_phase2_dispatch.py), now the persisted source of truth.
_HIGH_COURTS = [
    ("Allahabad High Court", "Uttar Pradesh", "9~13", "ALHC"),
    ("Andhra Pradesh High Court", "Andhra Pradesh", "28~2", "APHC"),
    ("Bombay High Court", "Maharashtra", "27~1", "BHC"),
    ("Chhattisgarh High Court", "Chhattisgarh", "22~18", "CGHC"),
    ("Calcutta High Court", "West Bengal", "19~16", "CHC"),
    ("Delhi High Court", "Delhi", "7~26", "DHC"),
    ("Gauhati High Court", "Assam", "18~6", "GHC"),
    ("Gujarat High Court", "Gujarat", "24~17", "GJHC"),
    ("Himachal Pradesh High Court", "Himachal Pradesh", "2~5", "HPHC"),
    ("High Court of Jammu, Kashmir and Ladakh", "Jammu and Kashmir", "1~12", "JKHC"),
    ("Jharkhand High Court", "Jharkhand", "20~7", "JHHC"),
    ("Karnataka High Court", "Karnataka", "29~3", "KHC"),
    ("Kerala High Court", "Kerala", "32~4", "KLHC"),
    ("Madhya Pradesh High Court", "Madhya Pradesh", "23~2", "MPHC"),
    ("Madras High Court", "Tamil Nadu", "33~10", "MHC"),
    ("Manipur High Court", "Manipur", "14~25", "MNHC"),
    ("Meghalaya High Court", "Meghalaya", "17~21", "MLHC"),
    ("Orissa High Court", "Odisha", "21~11", "OHC"),
    ("Patna High Court", "Bihar", "10~22", "PHC"),
    ("Punjab and Haryana High Court", "Punjab & Haryana", "3~18", "PHHC"),
    ("Rajasthan High Court", "Rajasthan", "8~9", "RHC"),
    ("Sikkim High Court", "Sikkim", "11~24", "SKHC"),
    ("Telangana High Court", "Telangana", "36~19", "THC"),
    ("Tripura High Court", "Tripura", "16~20", "TRHC"),
    ("Uttarakhand High Court", "Uttarakhand", "5~15", "UKHC"),
]

_SUPREME_COURT = ("Supreme Court of India", None, "SCIN")


def seed() -> None:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            court_name, state, court_code = _SUPREME_COURT
            cur.execute("""
                INSERT INTO courts (court_name, court_type, state, court_code)
                VALUES (%s, 'Supreme Court', %s, %s)
                ON CONFLICT (court_name) DO UPDATE SET court_code = EXCLUDED.court_code
                RETURNING court_id;
            """, (court_name, state, court_code))
            sc_court_id = cur.fetchone()[0]

            cur.execute("""
                INSERT INTO court_scrape_config (court_id, adapter, is_active)
                VALUES (%s, 'supreme_court', TRUE)
                ON CONFLICT (court_id) DO NOTHING;
            """, (sc_court_id,))

            for court_name, state, state_code, court_code in _HIGH_COURTS:
                cur.execute("""
                    INSERT INTO courts (court_name, court_type, state, court_code)
                    VALUES (%s, 'High Court', %s, %s)
                    ON CONFLICT (court_name) DO UPDATE SET court_code = EXCLUDED.court_code
                    RETURNING court_id;
                """, (court_name, state, court_code))
                court_id = cur.fetchone()[0]

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
