"""
Party name cleaning — strips procedural noise ("THROUGH ITS SECRETARY",
trailing alias markers) and normalizes whitespace/casing. No LLM involved:
adapters/supreme_court/extraction.py's parse_party_names() splits the adapter's raw
party_name_raw cell into petitioner/respondent; this only cleans each name
string before adapters/supreme_court/promotion.py writes it to the `parties` table.
"""

import re

_TRAILING_NOISE = re.compile(
    r"\b(THROUGH\s+ITS|SECRETARY|DEPARTMENT\s+OF|REPRESENTED\s+BY|AUTH\.?\s+SIGNATORY|REGISTERED\s+OFFICE|ALIAS|@)\b.*$",
    re.IGNORECASE,
)
_LEADING_NOISE = re.compile(r"^(?:IN\s+THE\s+MATTER\s+OF|BETWEEN:?)\s*", re.IGNORECASE)


def clean_party_name(raw_name: str) -> str:
    if not raw_name:
        return ""
    name = _LEADING_NOISE.sub("", raw_name)
    name = _TRAILING_NOISE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name.upper()
