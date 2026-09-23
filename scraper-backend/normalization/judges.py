"""
Judge name cleaning — strips honorifics ("HON'BLE MR. JUSTICE"), preserving
initials like "B.V. NAGARATHNA". Same regex approach the old normalizer.py
used, since it's genuinely just text cleanup with no logic worth redesigning.

Text cleanup only, not extraction: callers (adapters/supreme_court/extraction.py's
bench-cell parsing, adapters/supreme_court/promotion.py for record.judge_raw) already have
a raw judge name string — straight from the court's own results table, no
LLM involved — and just need it cleaned before it's resolved to a
`cr_judges` row.
"""

import re

_HONORIFIC_WORDS = re.compile(
    r"\b(?:HON'?BLE|CHIEF\s+JUSTICE|CJI|JUSTICE|ACTING|SMT\.?|SHRI|SH\.?|MR\.?|MRS\.?|MS\.?|DR\.?)\b",
    re.IGNORECASE,
)
_TRAILING_SUFFIX = re.compile(
    r"(?:,\s*|\s+)(?:CJI|CHIEF\s+JUSTICE|JUSTICE|JJ?\.|PRESIDING|COMPANION|ACTING)\s*$", re.IGNORECASE
)
_PUNCTUATION_EDGES = re.compile(r"^[,\-\s\.\(\)\[\]]+|[,\-\s\.\(\)\[\]]+$")


def clean_judge_name(raw_name: str) -> str:
    if not raw_name:
        return ""

    name = re.sub(r"\[.*?\]|\(.*?\)", "", raw_name).strip()
    name = _TRAILING_SUFFIX.sub("", name).strip()
    name = _HONORIFIC_WORDS.sub("", name).strip()
    name = _PUNCTUATION_EDGES.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()

    return name.upper() if name else ""
