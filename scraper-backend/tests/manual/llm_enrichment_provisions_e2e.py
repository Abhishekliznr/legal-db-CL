"""
Manual end-to-end check for pipeline/llm_enrichment.py's sections/acts
fallback extraction + favouring_party field, after the 2026-09-19
simplification that dropped rules/orders as a distinct kind and the
sections_relevant/_other (+ matching rules_/orders_) LLM-classified split
entirely -- enrich_case() now writes only sections/acts, and only when the
case had none already (see enrich_case's existing_sections guard, used by
Madhya Pradesh's fallback when case-status's own Act lines find nothing).
NOT a pytest suite, run directly.

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
    "subject": None,
    "industries": [],
    "ministries": [],
    "disposition_category": "Allowed",
    "favouring_party": "Petitioner",
    "provisions": [
        {"statute_name": "IPC", "number": "302"},
        {"statute_name": "Code of Criminal Procedure, 1973", "number": "439"},
        {"statute_name": "Constitution", "number": "226"},
        # duplicate of the first entry under a different spelling -- must
        # dedupe down to one section_id, not two
        {"statute_name": "Indian Penal Code, 1860", "number": "302"},
        # malformed: no number -- must be skipped, not raise
        {"statute_name": "IPC", "number": ""},
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
    # sections/acts are left to their schema defaults ('{}') -- promotion.py
    # doesn't always set them (e.g. a court whose own extraction found
    # nothing), matching the real case enrich_case()'s fallback exists for.
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
                "SELECT case_note, favouring_party, disposition, sections, acts FROM cr_cases WHERE case_id = %s;",
                (case_id,),
            )
            case_note, favouring_party, disposition, sections, acts = cur.fetchone()

            _check("case_note was written", bool(case_note))
            _check("favouring_party == 'Petitioner'", favouring_party == "Petitioner")
            _check("disposition == 'Allowed'", disposition == "Allowed")

            _check(
                "sections has exactly 3 entries (IPC 302, CrPC 439, Constitution 226; "
                "the IPC 302 duplicate deduped, the no-number entry skipped)",
                len(sections) == 3,
            )
            _check("acts[] has no duplicates (IPC referenced by 2 differently-spelled entries appears once)", len(acts) == len(set(acts)))
            _check("acts[] has exactly 3 distinct acts (IPC, CrPC, Constitution)", len(acts) == 3)

            cur.execute(
                "SELECT a.act_name, s.section_number FROM cr_sections s JOIN cr_acts a ON a.act_id = s.act_id "
                "WHERE s.section_id = ANY(%s) ORDER BY s.section_number;",
                (sections,),
            )
            resolved_rows = cur.fetchall()
            _check(
                "resolved sections are (Constitution->226, IPC->302, CrPC->439)",
                sorted(resolved_rows) == sorted([
                    ("Indian Penal Code, 1860", "302"),
                    ("Code of Criminal Procedure, 1973", "439"),
                    ("Constitution of India, 1950", "226"),
                ]),
            )

    # A second enrich_case() call must NOT overwrite the sections/acts this
    # case already has -- the existing_sections guard makes this a
    # fallback-only write, never a clobber of already-good data (this is
    # exactly what protects Madhya Pradesh's case-status-derived
    # sections/acts from being overwritten by a weaker LLM guess).
    llm_enrichment.call_llm_enrichment = lambda case_row, user_content=None: llm_enrichment.EnrichmentCallResult(
        {**_MOCK_LLM_RESULT, "provisions": [{"statute_name": "Some Other Act", "number": "99"}]}, "DONE", None,
    )
    try:
        ok_rerun = llm_enrichment.enrich_case(case_id, provision_block=ocr_text)
    finally:
        llm_enrichment.call_llm_enrichment = original
    _check("re-running enrich_case() on an already-sectioned case still returns True", ok_rerun is True)

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT sections, acts FROM cr_cases WHERE case_id = %s;", (case_id,))
            (still_sections, still_acts) = cur.fetchone()
            _check("a re-run does not overwrite previously-written sections", sorted(still_sections) == sorted(sections))
            _check("a re-run does not overwrite previously-written acts", sorted(still_acts) == sorted(acts))

    # Not-configured / call-fails path: confirm enrich_case() degrades
    # cleanly and does NOT touch sections/acts when the LLM call itself
    # returns None (still mocked -- still zero real HTTP calls).
    llm_enrichment.call_llm_enrichment = lambda case_row, user_content=None: llm_enrichment.EnrichmentCallResult(None, "FAILED", "mocked failure")
    try:
        ok2 = llm_enrichment.enrich_case(case_id)
    finally:
        llm_enrichment.call_llm_enrichment = original
    _check("enrich_case() returns False when the LLM call fails/unavailable", ok2 is False)

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT sections, acts FROM cr_cases WHERE case_id = %s;", (case_id,))
            (final_sections, final_acts) = cur.fetchone()
            _check("a failed LLM call does not wipe previously-written sections", sorted(final_sections) == sorted(sections))
            _check("a failed LLM call does not wipe previously-written acts", sorted(final_acts) == sorted(acts))

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} check(s) FAILED: {_FAILURES}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
