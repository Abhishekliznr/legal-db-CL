"""
Party name cleaning — strips procedural noise ("THROUGH ITS SECRETARY",
trailing alias markers) and normalizes whitespace/casing. Post-LLM
normalization, not extraction: pipeline/extraction.py identifies who the
parties are; this only cleans the name string before it's written to the
`parties` table.
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
