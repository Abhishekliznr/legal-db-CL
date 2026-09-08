"""
Deterministic case-number parsing: filing year + case-type category.

A case_number like "Civil Appeal No. 4567 of 2021", "SLP (C) No. 15210 of
2020", or "Crl.M.A. No. 61/2012" encodes both the filing year and the case
type in a fixed, mechanical format Indian courts have used for decades --
this is a regex/lookup job, not something worth asking an LLM to re-derive
from the full judgment text it already read the case number out of.

Downstream of pipeline/extraction.py, same as normalization/acts.py and
normalization/judges.py -- the LLM identifies the case number; this module
only classifies the string it already extracted.
"""

import re
from typing import Optional, Tuple

# Order here no longer decides precedence (see classify_category, which
# picks whichever pattern matches EARLIEST in the string) -- it's just
# read-order for humans.
_CATEGORY_PATTERNS = [
    (re.compile(r"\bSLP\s*\(Crl\.?\)|special\s+leave\s+petition\s*\(crim", re.I), "SLP(CRL)", "Special Leave Petition (Criminal)"),
    (re.compile(r"\bSLP\s*\(C\)|special\s+leave\s+petition\s*\(civ", re.I), "SLP(C)", "Special Leave Petition (Civil)"),
    (re.compile(r"\bcontempt\s+petition\b|\bcont\.?\s*pet\.?\b", re.I), "CONT.PET.", "Contempt Petition"),
    (re.compile(r"\bcriminal\s+appeal\b|\bcrl\.?\s*a\.?\b", re.I), "CRL.A.", "Criminal Appeal"),
    (re.compile(r"\bcivil\s+appeal\b|\bc\.?a\.?\b(?!\w)", re.I), "C.A.", "Civil Appeal"),
    (re.compile(r"w\.?\s*p\.?\s*\(crl\.?\)|writ\s+petition\s*\(crim", re.I), "W.P.(CRL)", "Writ Petition (Criminal)"),
    (re.compile(r"w\.?\s*p\.?\s*\(c\)|writ\s+petition\s*\(civ", re.I), "W.P.(C)", "Writ Petition (Civil)"),
    (re.compile(r"crl\.?\s*m(?:isc)?\.?\s*a(?:pp)?\.?\b|criminal\s+miscellaneous\s+application", re.I), "CRL.M.A.", "Criminal Miscellaneous Application"),
    (re.compile(r"\bcrp\b|civil\s+revision\s+petition", re.I), "CRP", "Civil Revision Petition"),
    (re.compile(r"c\.?\s*s\.?\s*\(os\)", re.I), "CS(OS)", "Civil Suit (Original Side)"),
]

# "... of 2021" (SC's own convention) or "61/2012" (eCourts/HC convention).
_YEAR_PATTERN = re.compile(r"\bof\s+(\d{4})\b|/\s*(\d{4})\b", re.I)

_MIN_YEAR, _MAX_YEAR = 1950, 2100


def extract_filing_year(case_number: Optional[str]) -> Optional[int]:
    """Best-effort filing year parsed out of the case number string itself, or None."""
    if not case_number:
        return None
    match = _YEAR_PATTERN.search(case_number)
    if not match:
        return None
    for group in match.groups():
        if group:
            year = int(group)
            if _MIN_YEAR <= year <= _MAX_YEAR:
                return year
    return None


# A trailing "(Arising out of SLP (C) No. X of Y)" / "(Arising out of ... )"
# parenthetical -- that reference belongs in case_appellate_history's
# prior_case_number, not smashed into the case number itself.
_ARISING_OUT_OF_PATTERN = re.compile(
    r"\(\s*(?:arising\s+out\s+of|arisen\s+from)\s*[:\-]?\s*(.+?)\)\s*$",
    re.IGNORECASE,
)

# Whether the text before that parenthetical actually contains a number
# token at all (a digit, or a hyphenated/comma-separated run of them).
_HAS_NUMBER_TOKEN = re.compile(r"no\.?s?\.?\s*[\d,\-–]+", re.IGNORECASE)


def split_arising_out_of(case_number: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Splits a trailing "(Arising out of ...)" parenthetical off case_number.
    Returns (cleaned_case_number, reference_or_None).

    Exception: if the appeal's OWN number is blank/not yet assigned -- a
    real, common convention ("Civil Appeal No. ___ of 2026" -- the registry
    allots the number after the order is passed, so the order text itself
    has a placeholder) -- the parenthetical is left attached instead of
    stripped. It's the only thing distinguishing this row from another
    blank-numbered case under the (court_id, case_number) unique constraint;
    stripping it would silently collide two different cases into one via
    ON CONFLICT DO NOTHING.
    """
    if not case_number:
        return case_number, None
    match = _ARISING_OUT_OF_PATTERN.search(case_number)
    if not match:
        return case_number, None
    main_part = case_number[: match.start()].strip()
    reference = match.group(1).strip().rstrip(".")
    if not _HAS_NUMBER_TOKEN.search(main_part):
        return case_number, reference  # blank/unassigned -- keep attached
    return main_part, reference


def classify_category(case_number: Optional[str]) -> Optional[Tuple[str, str]]:
    """
    Returns (category_code, category_name) for a recognized case-number
    prefix, or None. Picks whichever pattern matches EARLIEST in the string,
    not the first one tried -- a case number can legitimately contain a
    second case-type keyword after it (e.g. "Civil Appeal ... (Arising out
    of SLP (C) No. ...)", or "C.A. No. 2025 @ SLP(C) No. 30936 of 2025"),
    and the type of THIS case is whichever keyword appears first, not
    whichever happens to be checked first in _CATEGORY_PATTERNS.
    """
    if not case_number:
        return None
    best: Optional[Tuple[int, str, str]] = None
    for pattern, code, name in _CATEGORY_PATTERNS:
        match = pattern.search(case_number)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), code, name)
    return (best[1], best[2]) if best else None
