"""
Manual end-to-end check for pipeline/llm_enrichment.py's provisions
relevance split + favouring_party field (2026-09-09), including the
2026-09-09 follow-up where enrich_case() became the SOLE writer of
cr_cases.sections/acts/rules/orders (promotion.py no longer populates them
at all -- see promote_ingestion()'s docstring for why) — NOT a pytest
suite, run directly.

Uses a MOCKED call_llm_enrichment() throughout (monkeypatched at the module
level, before any Azure config is read) -- this makes zero real Azure
OpenAI calls regardless of what's in the environment's AZURE_OPENAI_* vars,
which is the safe way to test this: see feedback_dont_unset_blank_with_export
in project memory for why blanking-via-env-var was the wrong tool for a
similar case twice before. Mocking the function itself sidesteps that
entirely.

Run against a real (ideally throwaway) Postgres with the current schema.sql
already applied:

    export DATABASE_TYPE=postgres DB_HOST=localhost DB_PORT=5433 \\
           DB_NAME=<throwaway_db> DB_USER=postgres DB_PASSWORD=x
    python3 tests/manual/llm_enrichment_provisions_e2e.py
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from db.connection import get_pooled_connection, init_connection_pool
from pipeline import llm_enrichment

_FAILURES = []


def _check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        _FAILURES.append(label)


_MOCK_LLM_RESULT = {
    "case_note": "Criminal - Bail - Section 439 of Code of Criminal Procedure, 1973 - Held, bail granted - Application allowed",
    "conclusion": "The court found the applicant entitled to bail given the nature of allegations.",
    "industries": [],
    "ministries": [],
    "disposition_category": "Allowed",
    "favouring_party": "Petitioner",
    "provisions": [
        {"statute_name": "IPC", "provision_type": "section", "number": "302", "relevance": "RELEVANT"},
        {"statute_name": "Code of Criminal Procedure, 1973", "provision_type": "section", "number": "439", "relevance": "RELEVANT"},
        {"statute_name": "Constitution", "provision_type": "section", "number": "226", "relevance": "OTHER"},
        {"statute_name": "Order 21 Rule 32", "provision_type": "rule", "number": "32", "relevance": "OTHER"},
        {"statute_name": "Some Act", "provision_type": "order", "number": "XXI", "relevance": "OTHER"},
        # malformed: no number -- must be skipped, not raise
        {"statute_name": "IPC", "provision_type": "section", "number": "", "relevance": "RELEVANT"},
        # unrecognized relevance -- must fall into OTHER, not raise/crash
        {"statute_name": "IPC", "provision_type": "section", "number": "34", "relevance": "MAYBE"},
    ],
}


def _setup_case(cur):
    cur.execute(
        "INSERT INTO cr_courts (court_name, court_type) VALUES ('Test Court', 'Supreme Court') "
        "ON CONFLICT (court_name) DO UPDATE SET court_type = EXCLUDED.court_type RETURNING court_id;"
    )
    court_id = cur.fetchone()[0]

    ocr_text = (
        "1. This is a synthetic OCR text for verification.\n"
        "2. The accused was charged u/s 302 of the Indian Penal Code, 1860.\n"
        "3. Bail is sought under Section 439 of the Code of Criminal Procedure, 1973.\n"
    )
    # sections/acts/rules/orders are left to their schema defaults ('{}') --
    # promotion.py no longer sets them (2026-09-09), matching what a real
    # promote_ingestion() call now produces.
    cur.execute(
        """
        INSERT INTO cr_cases (court_id, case_number, ocr_text)
        VALUES (%s, 'Test Case No. 1 of 2026', %s)
        RETURNING case_id;
        """,
        (court_id, ocr_text),
    )
    return cur.fetchone()[0], ocr_text


def main():
    init_connection_pool()

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            case_id, ocr_text = _setup_case(cur)
        conn.commit()

    # Monkeypatch call_llm_enrichment BEFORE enrich_case runs -- no Azure
    # config is ever read, no real HTTP request is ever made. Matches the
    # 2026-09-10 EnrichmentCallResult(data, status, error) contract and the
    # optional user_content kwarg enrich_case now calls it with.
    original = llm_enrichment.call_llm_enrichment
    llm_enrichment.call_llm_enrichment = lambda case_row, user_content=None: llm_enrichment.EnrichmentCallResult(dict(_MOCK_LLM_RESULT), "DONE", None)
    try:
        # provision_block is supplied by the caller since 2026-09-17 (this
        # module has no extraction logic of its own) -- a real caller would
        # build it via that court's own extraction module (e.g.
        # adapters.supreme_court.extraction.find_provision_paragraphs); the
        # synthetic ocr_text stands in for that here.
        ok = llm_enrichment.enrich_case(case_id, provision_block=ocr_text)
    finally:
        llm_enrichment.call_llm_enrichment = original

    _check("enrich_case() returns True on a successful mocked call", ok is True)

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT case_note, favouring_party, disposition,
                       sections, acts, rules, orders,
                       sections_relevant, sections_other,
                       rules_relevant, rules_other,
                       orders_relevant, orders_other
                FROM cr_cases WHERE case_id = %s;
                """,
                (case_id,),
            )
            (
                case_note, favouring_party, disposition,
                sections, acts, rules, orders,
                sections_relevant, sections_other,
                rules_relevant, rules_other,
                orders_relevant, orders_other,
            ) = cur.fetchone()

            _check("case_note was written", bool(case_note))
            _check("favouring_party == 'Petitioner'", favouring_party == "Petitioner")
            _check("disposition == 'Allowed'", disposition == "Allowed")

            _check("sections_relevant has exactly 2 entries (IPC 302, CrPC 439; the no-number entry skipped)", len(sections_relevant) == 2)
            _check("sections_other has exactly 2 entries (Constitution 226 + unrecognized-relevance IPC '34' -> OTHER)", len(sections_other) == 2)
            _check("rules_other has exactly 1 entry (Order 21 Rule 32, tagged OTHER)", len(rules_other) == 1)
            _check("rules_relevant is empty", len(rules_relevant) == 0)
            _check("orders_other has exactly 1 entry (Some Act order XXI, tagged OTHER)", len(orders_other) == 1)
            _check("orders_relevant is empty", len(orders_relevant) == 0)

            # sections/acts/rules/orders (2026-09-09 follow-up): now the
            # union of relevant+other, written by enrich_case() itself --
            # promotion.py no longer sets them at all.
            _check("baseline sections == sections_relevant + sections_other (union, no cross-bucket dedup needed)", sorted(sections) == sorted(sections_relevant + sections_other))
            _check("baseline rules == rules_relevant + rules_other", sorted(rules) == sorted(rules_relevant + rules_other))
            _check("baseline orders == orders_relevant + orders_other", sorted(orders) == sorted(orders_relevant + orders_other))
            # IPC is referenced by both a RELEVANT section (302) and an
            # OTHER one (unrecognized-relevance '34') -- must appear in
            # `acts` exactly once, proving the cross-bucket act_id dedup.
            _check("acts[] has no duplicates (an act referenced by both buckets appears once)", len(acts) == len(set(acts)))
            _check("acts[] has exactly 5 distinct acts (IPC, CrPC, Constitution, the 2 synthetic act names from the rule/order entries)", len(acts) == 5)

            # Confirm the actual resolved act names for the two RELEVANT sections.
            cur.execute(
                "SELECT a.act_name, s.section_number FROM cr_sections s JOIN cr_acts a ON a.act_id = s.act_id "
                "WHERE s.section_id = ANY(%s) ORDER BY s.section_number;",
                (sections_relevant,),
            )
            relevant_rows = cur.fetchall()
            _check(
                "resolved relevant sections are (IPC->302, CrPC->439)",
                relevant_rows == [("Indian Penal Code, 1860", "302"), ("Code of Criminal Procedure, 1973", "439")],
            )

    # Not-configured / call-fails path: confirm enrich_case() degrades
    # cleanly and does NOT touch the new columns when the LLM call itself
    # returns None (still mocked -- still zero real HTTP calls).
    llm_enrichment.call_llm_enrichment = lambda case_row, user_content=None: llm_enrichment.EnrichmentCallResult(None, "FAILED", "mocked failure")
    try:
        ok2 = llm_enrichment.enrich_case(case_id)
    finally:
        llm_enrichment.call_llm_enrichment = original
    _check("enrich_case() returns False when the LLM call fails/unavailable", ok2 is False)

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT sections_relevant, sections, acts FROM cr_cases WHERE case_id = %s;", (case_id,))
            (still_sections_relevant, still_sections, still_acts) = cur.fetchone()
            _check("a failed LLM call does not wipe previously-written sections_relevant", len(still_sections_relevant) == 2)
            _check("a failed LLM call does not wipe previously-written baseline sections", len(still_sections) == 4)
            _check("a failed LLM call does not wipe previously-written baseline acts", len(still_acts) == 5)

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} check(s) FAILED: {_FAILURES}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
