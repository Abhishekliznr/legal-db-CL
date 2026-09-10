"""
Citation finding — a pre-filter, not a classifier.

Reporter-citation regexes are good at spotting "there's a citation here" —
that part of the old citator.py's approach survives, reused as reference
patterns rather than copied code. What does NOT survive is
keyword-proximity treatment classification (deciding OVERRULED vs FOLLOWED
by which word sits closest) — that's unreliable; classifying how a cited
case was actually treated needs an LLM reading the surrounding meaning,
not word proximity.

find_citation_candidates() is meant to feed a future LLM call's prompt with
candidates worth asking it to classify — it does not itself decide a
treatment, and isn't currently wired into pipeline/promotion.py (the
flattened `cases` schema doesn't carry a citations table right now; see
db/schema.sql's rewrite note). Kept as a standalone utility for whenever
citation tracking comes back.
"""

import re
from typing import Dict, List

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
    Returns raw citation-looking strings with surrounding context, meant to
    feed a future LLM call as "here are citation-shaped strings found in
    the text — classify any that are real case citations." Not currently
    called by any pipeline stage — see this module's docstring.
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
    """Coarse keyword-hit flag — a fast heuristic, not the real overrule graph (that needs a citations table with treatment classification, which the current flattened `cases` schema doesn't carry — see db/schema.sql's rewrite note). Currently unused by pipeline/promotion.py; kept as a standalone utility for whenever citation tracking comes back."""
    return bool(text and _OVERRULED_KEYWORDS.search(text))


# reconcile_citations() removed in the 2026-09-08 schema rewrite -- it
# resolved citations.cited_document_id against documents.neutral_citation,
# and both `citations` and `documents` were dropped along with the rest of
# the old LLM-envelope-shaped tables (see db/schema.sql's rewrite note).
# find_citation_candidates() above is unaffected -- it's a pure text
# function with no DB dependency, kept for whenever citation tracking is
# reintroduced.
