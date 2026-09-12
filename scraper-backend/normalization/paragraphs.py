"""
Deterministic paragraph splitting for judgment OCR text.

Indian judgments overwhelmingly number their paragraphs ("1.", "2." ... at
the start of a line, sometimes OCR'd as "1 ." with a stray space). This is
a regex job, not an LLM one: asking the LLM to reproduce paragraph text
verbatim would burn tokens re-emitting text it already received, and risks
it silently rephrasing a word here and there -- the numbering itself is a
mechanical, unambiguous pattern a regex handles more reliably anyway.

Used by pipeline/regex_extraction.py to scope disposition/facts/conclusion
extraction to the tail of the judgment rather than the whole OCR text (see
its _tail_windows()), and by parsers/judgment_parser.py to build the
structured, paragraph-addressable representation of a judgment. cases.ocr_text
stays the source of truth regardless: a judgment whose paragraphs aren't
numbered, or use letters/roman numerals, simply falls back to a plain
character-count window (regex_extraction.py) or a single unstructured
section (parsers/judgment_parser.py) instead.
"""

import re
from typing import List, Tuple

# A paragraph-opening numeral at the start of a line: "12.", "12 .", "12)".
_PARA_START = re.compile(r"(?:^|\n)\s*(\d{1,3})\s*[.\)]\s+", re.MULTILINE)

# Fewer than this many paragraphs in a strictly-increasing 1, 2, 3... run
# isn't worth trusting -- a couple of accidental matches (a citation year,
# a numbered list inside the judgment) shouldn't be stored as if they were
# real paragraph boundaries.
_MIN_PARAGRAPHS = 3


def _kept_paragraph_starts(ocr_text: str) -> List["re.Match"]:
    """
    The raw paragraph-number regex matches that continue a strictly
    increasing 1, 2, 3... run from the start of the document -- shared by
    both split_into_paragraphs() and split_into_paragraphs_with_offsets()
    so the two never drift apart on what counts as a real paragraph
    boundary. Returns [] if fewer than _MIN_PARAGRAPHS such matches exist.
    """
    raw_matches = list(_PARA_START.finditer(ocr_text))
    if len(raw_matches) < _MIN_PARAGRAPHS:
        return []

    kept = []
    expected = 1
    for match in raw_matches:
        number = int(match.group(1))
        if number == expected:
            kept.append(match)
            expected += 1

    return kept if len(kept) >= _MIN_PARAGRAPHS else []


def paragraph_start_matches(ocr_text: str) -> List["re.Match"]:
    """Public accessor to the same kept paragraph-start regex matches used internally -- lets a caller (parsers/judgment_parser.py) tell a real paragraph marker apart from a look-alike digit elsewhere in the text."""
    if not ocr_text:
        return []
    return _kept_paragraph_starts(ocr_text)


def split_into_paragraphs(ocr_text: str) -> List[Tuple[int, str]]:
    """
    Returns [(para_number, para_text), ...] in document order, or [] if no
    reliable numbering run was found.
    """
    if not ocr_text:
        return []

    kept = _kept_paragraph_starts(ocr_text)
    if not kept:
        return []

    paragraphs: List[Tuple[int, str]] = []
    for i, match in enumerate(kept):
        text_start = match.end()
        text_end = kept[i + 1].start() if i + 1 < len(kept) else len(ocr_text)
        para_text = ocr_text[text_start:text_end].strip()
        if para_text:
            paragraphs.append((int(match.group(1)), para_text))

    return paragraphs


def split_into_paragraphs_with_offsets(ocr_text: str) -> List[Tuple[int, str, int, int]]:
    """
    Same paragraph boundaries as split_into_paragraphs(), but also returns
    each paragraph's [start_char, end_char) span into the ORIGINAL ocr_text
    (before the .strip() that split_into_paragraphs applies to the text
    itself) -- so a caller needing provenance (parsers/judgment_parser.py)
    can trace a paragraph back to its exact source offsets, including the
    leading/trailing whitespace split_into_paragraphs discards from the
    text but not from the span.
    """
    if not ocr_text:
        return []

    kept = _kept_paragraph_starts(ocr_text)
    if not kept:
        return []

    paragraphs: List[Tuple[int, str, int, int]] = []
    for i, match in enumerate(kept):
        text_start = match.end()
        text_end = kept[i + 1].start() if i + 1 < len(kept) else len(ocr_text)
        para_text = ocr_text[text_start:text_end].strip()
        if para_text:
            # Narrow the span to exactly the stripped text's own extent,
            # not the whitespace-padded [text_start, text_end) range --
            # keeps start_char/end_char pointing at real content.
            leading_ws = len(ocr_text[text_start:text_end]) - len(ocr_text[text_start:text_end].lstrip())
            span_start = text_start + leading_ws
            span_end = span_start + len(para_text)
            paragraphs.append((int(match.group(1)), para_text, span_start, span_end))

    return paragraphs
