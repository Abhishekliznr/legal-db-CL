"""
Seeds `cr_filter_definitions` + `cr_search_field_definitions` — the admin-managed
metadata GET /api/cases/filters and GET /api/cases/searches serve.
(ON CONFLICT (key) DO NOTHING, so this is a no-op against an already-seeded
DB and never clobbers an admin's edits).

`treatment_status` dropped in the 2026-09-08 schema rewrite: it read a
computed column off `citations`, which no longer exists (citation/treatment
tracking isn't modeled by this pipeline iteration at all). See
routers/filter_router.py's module docstring.

2026-09-10: added disposition/favouring_party/industry/ministry — scraper-
backend's llm_enrichment.py rewrite now actually populates these fields at
meaningful volume (previously industries/favouring_party had no extraction
path at all, and disposition had no filter facet even though the regex
classifier populated it). See routers/filter_router.py's
`_compute_database_options()` for the dispatch SQL each key runs.

Usage:
    python -m db.seed_filters
"""

from db.connection import get_pooled_connection

# (key, label, type, selection_mode, query_key, data_source, is_searchable, display_order)
_FILTER_DEFINITIONS = [
    ("court", "Court", "select", "multi", "court_id", "database", False, 0),
    ("judge", "Judge / Bench", "select", "multi", None, "database", True, 1),
    ("judgment_year", "Judgment Year", "select", "multi", None, "database", False, 3),
    ("disposition", "Disposition", "select", "multi", None, "database", False, 4),
    ("favouring_party", "Favouring Party", "select", "multi", None, "database", False, 5),
    ("industry", "Industry", "select", "multi", None, "database", True, 6),
    ("ministry", "Ministry / Department", "select", "multi", None, "database", True, 7),
    ("judgment", "Judgment", "select", "multi", None, "database", False, 8),
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
            # Self-heals a database seeded before the 2026-09-08 rewrite --
            # ON CONFLICT DO NOTHING above only skips rows already present,
            # it doesn't remove a stale row that's no longer in the list at
            # all, so a pre-existing 'treatment_status' row would otherwise
            # linger forever and keep showing up (with permanently empty
            # options, since filter_router.py no longer computes it).
            # 'act' (2026-09-26) was superseded by the Act/Section picker's `provisions` search
            # field -- see routers/provision_router.py.
            cur.execute("DELETE FROM cr_filter_definitions WHERE key IN ('treatment_status', 'act');")

            cur.executemany("""
                INSERT INTO cr_filter_definitions (key, label, type, selection_mode, query_key, data_source, is_searchable, display_order)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (key) DO NOTHING;
            """, _FILTER_DEFINITIONS)

            cur.executemany("""
                INSERT INTO cr_search_field_definitions (key, label, placeholder, combinator, display_order)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (key) DO NOTHING;
            """, _SEARCH_FIELD_DEFINITIONS)
        conn.commit()
    print(f"Seeded {len(_FILTER_DEFINITIONS)} filter definitions and {len(_SEARCH_FIELD_DEFINITIONS)} search field definitions.")


if __name__ == "__main__":
    from db.connection import init_connection_pool
    init_connection_pool()
    seed()
