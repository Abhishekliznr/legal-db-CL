"""
Citation finding (a pre-filter, not a classifier) + treatment reconciliation
(spec §5.3).

Reporter-citation regexes are good at spotting "there's a citation here" —
that part of the old citator.py's approach survives, reused as reference
patterns rather than copied code. What does NOT survive is
keyword-proximity treatment classification (deciding OVERRULED vs FOLLOWED
by which word sits closest) — that's unreliable, and the new schema's
citations.treatment comes from pipeline/extraction.py's LLM call instead,
which reads the actual surrounding meaning rather than word proximity.

find_citation_candidates() feeds pipeline/extraction.py's prompt with
candidates worth asking the LLM to classify — it does not itself decide a
treatment. reconcile_citations() is the periodic job that resolves
citations.cited_document_id by matching cited_reporter_citation against
other documents' neutral_citation/equivalent_citations — separate from
promotion because a case being cited might not exist in this database yet
(it may get ingested in a later batch).
"""

import re
from typing import Dict, List

from db.connection import get_pooled_connection

_CITATION_PATTERNS = [
    # Neutral citations: 2024 INSC 123, 2023:DHC:4567
    re.compile(r"\b([12][90]\d{2})\s*(?:INSC|:\s*[A-Z]{2,4}\s*:)\s*(\d+)\b", re.IGNORECASE),
    # Standard reporters: (2024) 1 SCC 234, AIR 2023 SC 567
    re.compile(
        r"(?:\(?([12][90]\d{2})\)?\s*)?(?:(\d+)\s+)?(SCC|AIR|SCR|SCALE|ILR|DLT|Cri\s*LJ|CrLJ)\s+(?:([A-Z]{2,4})\s+)?(\d+)",
        re.IGNORECASE,
    ),
]

_OVERRULED_KEYWORDS = re.compile(
    r"\b(overruled?|overruling|no\s+longer\s+good\s+law|per\s+incuriam|stands\s+overruled)\b", re.IGNORECASE
)


def find_citation_candidates(text: str, context_chars: int = 150) -> List[Dict[str, str]]:
    """
    Returns raw citation-looking strings with surrounding context, for
    pipeline/extraction.py to hand to the LLM as "here are citation-shaped
    strings found in the text — classify any that are real case citations."
    Not a source of truth on its own; a candidate list only.
    """
    if not text:
        return []

    candidates = []
    seen = set()
    for pattern in _CITATION_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(0).strip()
            if raw in seen or len(raw) < 5:
                continue
            seen.add(raw)
            start = max(0, match.start() - context_chars)
            end = min(len(text), match.end() + context_chars)
            candidates.append({"raw_citation": raw, "context": text[start:end].replace("\n", " ").strip()})

    return candidates


def has_overruled_keyword(text: str) -> bool:
    """Coarse keyword-hit flag for documents.overruled_keyword_present — the real overrule graph lives in citations.treatment, this is just a fast heuristic flag."""
    return bool(text and _OVERRULED_KEYWORDS.search(text))


def reconcile_citations(limit: int = 500) -> int:
    """
    Resolves citations.cited_document_id for rows where it's still NULL, by
    matching cited_reporter_citation against other documents'
    neutral_citation or equivalent_citations. Meant to run periodically
    (after each ingestion batch, or on a schedule) — a cited case may not
    have existed in this database yet at promotion time. Returns the number
    of rows resolved.
    """
    resolved = 0
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT citation_id, cited_reporter_citation, cited_case_name
                FROM citations
                WHERE cited_document_id IS NULL AND cited_reporter_citation IS NOT NULL
                LIMIT %s;
            """, (limit,))
            pending = cur.fetchall()

            for citation_id, cited_reporter_citation, _cited_case_name in pending:
                cur.execute("""
                    SELECT document_id FROM documents
                    WHERE neutral_citation = %s
                       OR %s = ANY(equivalent_citations)
                    LIMIT 1;
                """, (cited_reporter_citation, cited_reporter_citation))
                match = cur.fetchone()
                if match:
                    cur.execute("UPDATE citations SET cited_document_id = %s WHERE citation_id = %s;", (match[0], citation_id))
                    resolved += 1

        conn.commit()
    return resolved
