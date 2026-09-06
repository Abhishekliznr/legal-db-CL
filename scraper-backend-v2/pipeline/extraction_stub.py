"""
The Phase 1 stand-in extractor, kept as a fallback for local dev without a
GROQ_API_KEY configured (pipeline/extraction.py uses this when no key is
set — see that module's docstring for why a missing key falls back here
but a failed LLM call does not).

Originally pipeline/extraction.py's entire content in Phase 1; moved here
unchanged when Phase 3 replaced the main module with the real LLM call, so
the same envelope-shape guarantee promotion.py depends on still holds
regardless of which extractor produced a given row.
"""

from typing import Any, Dict

from adapters.base import RawJudgmentRecord


def build_stub_extraction(record: RawJudgmentRecord) -> Dict[str, Any]:
    """Reshapes the scraper's own parsed fields into the same envelope pipeline/extraction.py's real LLM call produces — no judge/act/party cleaning, no provision/citation parsing. See pipeline/promotion.py for what reads this."""
    parties = []
    if record.party_name_raw:
        parts = [p.strip() for p in record.party_name_raw.replace(" Vs ", " VS ").split(" VS ") if p.strip()]
        if len(parts) >= 1:
            parties.append({"name": parts[0], "side": "PETITIONER_SIDE"})
        if len(parts) >= 2:
            parties.append({"name": parts[1], "side": "RESPONDENT_SIDE"})

    coram = []
    if record.judge_raw:
        coram.append({"name": record.judge_raw, "is_author": True})

    return {
        "extractor": "scraper_fields_stub_v1",
        "case_note_ai": None,
        "decision_type": None,
        "ratio_decidendi": None,
        "cases": [
            {
                "case_number": record.case_number_raw,
                "cnr_number": record.cnr_raw,
                "parties": parties,
            }
        ],
        "coram": coram,
        "counsels": [],
        "subjects": [],
        "provisions": [],
        "relevant_section": None,
        "citations": [],
        "held": [],
        "prior_history": None,
        "timeline": [],
        "disposition_category": None,
        "favoring_party_side": None,
        "neutral_citation": record.neutral_citation_raw,
        "judgment_date_raw": record.decision_date_raw,
    }
