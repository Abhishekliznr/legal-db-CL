from __future__ import annotations

import re
from datetime import date
from typing import Optional

_DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y", "%d %B %Y")


def normalize_case_type(raw_case_type: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", raw_case_type or "").upper()


def normalize_case_no(raw_case_no: str) -> str:
    stripped = (raw_case_no or "").strip()
    return str(int(stripped)) if stripped.isdigit() else stripped


def parse_date_str(raw: Optional[str]) -> Optional[date]:
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
