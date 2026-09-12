"""
Deterministic OCR/layout artifact detection — patterns for boilerplate that
a court PDF's text layer emits but that carries no legal meaning of its own
(digital-signature blocks, dot-leader table-of-contents lines, bare page
numbers).

Moved out of pipeline/llm_enrichment.py (2026-09-10 rewrite introduced
these for its own excerpt-building) so parsers/judgment_parser.py can reuse
the exact same, already-verified patterns instead of re-deriving its own —
two independently-tuned artifact regexes drifting apart over time is worse
than one shared definition. llm_enrichment.py now imports from here too;
behavior is unchanged, only the location moved.
"""

import re
from typing import List, TypedDict

# A digital-signature block, e.g. "Digitally signed by\nJohn Doe\nDate: ...
# \nReason: ...\nSignature Not Verified" -- boilerplate on every e-filed
# judgment. Capped span so a missing closing marker can't scan unbounded.
SIGNATURE_BLOCK_RE = re.compile(r"Digitally signed by.{0,500}?Signature Not Verified", re.IGNORECASE | re.DOTALL)

# A table-of-contents line with dot leaders, e.g. "Conclusion .......... 42".
DOT_LEADER_LINE_RE = re.compile(r"^.*\.{5,}.*$\n?", re.MULTILINE)

# A line containing only a 1-3 digit page number.
PAGE_NUMBER_LINE_RE = re.compile(r"^[ \t]*\d{1,3}[ \t]*$\n?", re.MULTILINE)


class ArtifactSpan(TypedDict):
    artifact_type: str
    start: int
    end: int
    text: str


def strip_ocr_noise(text: str) -> str:
    """Removes signature blocks, dot-leader TOC lines, bare page-number lines, and collapses blank runs."""
    if not text:
        return ""
    cleaned = SIGNATURE_BLOCK_RE.sub("", text)
    cleaned = DOT_LEADER_LINE_RE.sub("", cleaned)
    cleaned = PAGE_NUMBER_LINE_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


_ARTIFACT_PATTERNS = (
    ("signature_block", SIGNATURE_BLOCK_RE),
    ("page_number", PAGE_NUMBER_LINE_RE),
    ("toc_dot_leader", DOT_LEADER_LINE_RE),
)


def find_artifact_spans(text: str) -> List[ArtifactSpan]:
    """
    Returns every artifact match's (type, start, end, raw text) in document
    order, non-overlapping (a span already claimed by an earlier-checked
    pattern is not reported again by a later one). Positions are offsets
    into `text` as given -- callers needing this scoped to one paragraph's
    text should pass that paragraph's own substring, not the whole document.
    """
    if not text:
        return []

    claimed: List[List[int]] = []
    spans: List[ArtifactSpan] = []
    for artifact_type, pattern in _ARTIFACT_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.start(), match.end()
            if any(start < c_end and end > c_start for c_start, c_end in claimed):
                continue
            claimed.append([start, end])
            spans.append({"artifact_type": artifact_type, "start": start, "end": end, "text": match.group(0)})

    spans.sort(key=lambda s: s["start"])
    return spans
