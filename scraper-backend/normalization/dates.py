"""
Generic date-string parsing, shared by every court's own promotion.py --
not specific to any one source's format, just the handful of conventions
Indian court sites/portals actually use.
"""

import re
from datetime import date, datetime
from typing import Optional

_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %B %Y", "%d %b %Y")


def parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    cleaned = re.sub(r"(\d{1,2})(st|nd|rd|th)", r"\1", value, flags=re.IGNORECASE).replace(",", "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None
