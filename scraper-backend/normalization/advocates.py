"""
Advocate name cleaning -- strips designations ("Sr. Adv.", "AOR", "Adv.")
and honorifics, same treatment as normalization/judges.py's
clean_judge_name (text cleanup only, no LLM). Callers already have a raw
name string out of pipeline/regex_extraction.py's parse_advocates(), which
only splits sci.gov.in's advocate cell on its "__"/"-" separator(s) -- this
is the second pass that turns that split-out fragment into a clean name
before pipeline/promotion.py writes it to
cr_cases.petitioner_advocate/respondent_advocate.
"""

import re

_DESIGNATION_WORDS = re.compile(
    r"\b(?:SR\.?\s*ADV(?:OCATE)?\.?|SENIOR\s+ADVOCATE|ADVOCATE|ADV\.?|AOR|AMICUS\s+CURIAE|"
    r"MR\.?|MRS\.?|MS\.?|DR\.?|SHRI|SMT\.?)\b",
    re.IGNORECASE,
)
_PUNCTUATION_EDGES = re.compile(r"^[,\-_\s\.\(\)\[\]]+|[,\-_\s\.\(\)\[\]]+$")


def clean_advocate_name(raw_name: str) -> str:
    if not raw_name:
        return ""

    name = _DESIGNATION_WORDS.sub("", raw_name)
    name = _PUNCTUATION_EDGES.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()

    return name.upper() if name else ""
