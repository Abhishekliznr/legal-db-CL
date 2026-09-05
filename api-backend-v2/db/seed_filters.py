"""
Seeds `filter_definitions` + `search_field_definitions` — the admin-managed
metadata GET /api/cases/filters and GET /api/cases/searches serve. Same 5+5
rows the old api-backend seeded (ON CONFLICT (key) DO NOTHING, so this is a
no-op against an already-seeded DB and never clobbers an admin's edits).

Usage:
    python -m db.seed_filters
"""

from db.connection import get_pooled_connection

# (key, label, type, selection_mode, query_key, data_source, is_searchable, display_order)
_FILTER_DEFINITIONS = [
    ("court", "Court", "select", "multi", "court_id", "database", False, 0),
    ("treatment_status", "Treatment Status", "select", "multi", None, "database", False, 1),
    ("judge", "Judge / Bench", "select", "multi", None, "database", True, 2),
    ("act", "Act / Law", "select", "multi", None, "database", True, 3),
    ("judgment_year", "Judgment Year", "select", "multi", None, "database", False, 4),
]

# (key, label, placeholder, combinator, display_order)
_SEARCH_FIELD_DEFINITIONS = [
    ("all", "All of these words", "Search items...", "AND", 0),
    ("any", "Any of these words", "Search items...", "OR", 1),
    ("exact", "Exactly this phrase", "Search items...", "PHRASE", 2),
    ("none", "None of these words", "Search items...", "NOT", 3),
    ("text", "These words", "Search items...", "AND", 4),
]


def seed() -> None:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO filter_definitions (key, label, type, selection_mode, query_key, data_source, is_searchable, display_order)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (key) DO NOTHING;
            """, _FILTER_DEFINITIONS)

            cur.executemany("""
                INSERT INTO search_field_definitions (key, label, placeholder, combinator, display_order)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (key) DO NOTHING;
            """, _SEARCH_FIELD_DEFINITIONS)
        conn.commit()
    print(f"Seeded {len(_FILTER_DEFINITIONS)} filter definitions and {len(_SEARCH_FIELD_DEFINITIONS)} search field definitions.")


if __name__ == "__main__":
    from db.connection import init_connection_pool
    init_connection_pool()
    seed()
