"""
ILRS-side (portal.mphc.gov.in/ilrs) field cleanup — normalizing whatever raw
strings the ILRS discovery listing gives before they're used elsewhere in
this adapter (case_status.py's code lookup, promotion.py's cr_cases INSERT).

Confirmed live: ILRS's own case_type token (parsed from the "<Bench>/<Type>/
<Number>/<Year>" case line, e.g. "WP") already matches
case_status.CASE_TYPE_CODES' key format exactly -- normalize_case_type's
strip/uppercase pass is a no-op for real data, kept only as a defensive
guard against incidental whitespace, not because real normalization is
needed. Decision dates render as "10 August 2023" (%d %B %Y), confirmed live.
"""

import re
from datetime import date
from typing import Optional

_DATE_FORMATS = ("%d %B %Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y")


def normalize_case_type(raw_case_type: str) -> str:
    """Best-effort normalization toward case_status.CASE_TYPE_CODES' key format -- strip punctuation/whitespace, uppercase. Needs verifying against real ILRS output once available."""
    return re.sub(r"[^A-Za-z]", "", raw_case_type or "").upper()


def normalize_case_no(raw_case_no: str) -> str:
    """
    Strips ILRS's zero-padding (e.g. "01598" -> "1598", confirmed live on a
    real Gwalior case) -- mphc.gov.in/case-status's own #case_no field is a
    plain number input, and the site's own case-detail pages never show a
    leading zero (e.g. "Jabalpur/WP/16962/2018"), so a padded value either
    fails to match or matches the wrong case. Non-numeric input is returned
    unchanged rather than raising, since a case number should always be
    numeric here but this must not crash the batch if that ever isn't true.
    """
    stripped = (raw_case_no or "").strip()
    return str(int(stripped)) if stripped.isdigit() else stripped


def parse_ilrs_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    from datetime import datetime

    cleaned = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def clean_headnote(raw_headnote: Optional[str]) -> Optional[str]:
    if not raw_headnote:
        return None
    return re.sub(r"\s+", " ", raw_headnote).strip() or None
