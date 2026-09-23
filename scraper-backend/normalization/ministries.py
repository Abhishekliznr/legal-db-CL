"""
Ministry lookup — a fixed closed list, not court-specific data, shared by
any court's promotion module (party-name matching) and pipeline/llm_enrichment.py
(prompt/schema construction + validating the LLM's ministry output).

Matching is scoped to a single already-identified party name, never the
whole document: a judgment merely CITING a past "Union of India, Ministry
of Railways" case would otherwise get mistagged even when no ministry is a
party to the current case at all.

Reused as reference data (names only, no matching logic) from the old
scraper-backend's MINISTRIES_LIST -- same treatment normalization/acts.py
already gives CANONICAL_STATUTES: a lookup table has no logic worth
rewriting, only the data is carried over. Trimmed to current-name entries
relevant to a party-name match (dropped historical/foreign entries like
"National Parliament of Bangladesh" that clearly don't belong here).
"""

from typing import Optional

KNOWN_MINISTRIES = [
    "Ministry of Agriculture and Farmers Welfare", "Ministry of Ayush",
    "Ministry of Chemicals and Fertilizers", "Ministry of Civil Aviation",
    "Ministry of Coal", "Ministry of Commerce and Industry",
    "Ministry of Communications", "Ministry of Corporate Affairs",
    "Ministry of Culture", "Ministry of Defence", "Ministry of Education",
    "Ministry of Electronics and Information Technology",
    "Ministry of Environment, Forest and Climate Change",
    "Ministry of External Affairs", "Ministry of Finance",
    "Ministry of Fisheries, Animal Husbandry and Dairying",
    "Ministry of Food Processing Industries",
    "Ministry of Health and Family Welfare", "Ministry of Home Affairs",
    "Ministry of Housing and Urban Affairs",
    "Ministry of Information and Broadcasting", "Ministry of Jal Shakti",
    "Ministry of Labour and Employment", "Ministry of Law and Justice",
    "Ministry of Micro, Small and Medium Enterprises", "Ministry of Mines",
    "Ministry of Minority Affairs",
    "Ministry of Panchayati Raj",
    "Ministry of Parliamentary Affairs",
    "Ministry of Personnel, Public Grievances and Pensions",
    "Ministry of Petroleum and Natural Gas", "Ministry of Power",
    "Ministry of Railways", "Ministry of Road Transport and Highways",
    "Ministry of Rural Development", "Ministry of Science and Technology",
    "Ministry of Shipping", "Ministry of Skill Development and Entrepreneurship",
    "Ministry of Social Justice and Empowerment",
    "Ministry of Statistics and Programme Implementation",
    "Ministry of Steel", "Ministry of Textiles",
    "Ministry of Tourism", "Ministry of Tribal Affairs",
    "Ministry of Urban Development", "Ministry of Water Resources",
    "Ministry of Women and Child Development",
    "Cabinet Division", "Reserve Bank of India",
    "Securities and Exchange Board of India",
    "Telecom Regulatory Authority of India",
    "Election Commission of India",
]

_MINISTRY_LOOKUP = {m.lower(): m for m in KNOWN_MINISTRIES}


def find_ministry_in_party_name(party_name: Optional[str]) -> Optional[str]:
    """
    Returns the canonical ministry name if `party_name` (a single already-
    parsed party string, e.g. from a court's own parse_party_names()) names
    one exactly, or None. Deliberately exact/substring match against a
    closed list rather than a whole-document keyword scan -- see module
    docstring.
    """
    if not party_name:
        return None
    lowered = party_name.lower()
    for ministry_lower, canonical in _MINISTRY_LOOKUP.items():
        if ministry_lower in lowered:
            return canonical
    return None


def resolve_ministry(raw_name: str) -> Optional[str]:
    """
    Case-insensitive EXACT match against KNOWN_MINISTRIES, for validating
    pipeline/llm_enrichment.py's LLM output (which is prompted with this
    same list as its only allowed subject-matter-ministry values) --
    deliberately not substring matching like find_ministry_in_party_name
    above, since the LLM is expected to return the canonical name verbatim,
    not a longer string a ministry name happens to appear inside. Returns
    None for anything outside the closed list, dropped rather than stored
    as a fabricated-looking new ministry.
    """
    if not raw_name:
        return None
    return _MINISTRY_LOOKUP.get(raw_name.strip().lower())
