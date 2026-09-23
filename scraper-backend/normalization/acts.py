"""
Canonical Indian Acts/Statutes registry + alias resolution.

Alias data below is the same canonical-acts list the old normalizer.py
built up (IPC, CrPC, CPC, etc. and their common abbreviations/misspellings)
— reused as reference data, not as code, since it's genuinely just a lookup
table with no logic worth rewriting. Resolution targets the `acts` table
shape (act_name, act_year, short_code) — renamed from the earlier
`statutes` in the 2026-09-08 schema rewrite, same table otherwise.

Downstream of extraction, not the primary extraction mechanism — a caller
(adapters/supreme_court/extraction.py's extract_provisions(), or an LLM call later)
identifies "Section 302 IPC" is invoked; this module only resolves "IPC"
to the canonical "Indian Penal Code, 1860" act row.
"""

import re
from typing import Optional, Tuple

CANONICAL_STATUTES = [
    {"name": "Indian Penal Code, 1860", "short_code": "IPC", "year": 1860,
     "aliases": ["ipc", "indian penal code", "i.p.c.", "the penal code", "penal code"]},
    {"name": "Code of Criminal Procedure, 1973", "short_code": "CrPC", "year": 1973,
     "aliases": ["crpc", "cr.p.c.", "crl.p.c.", "criminal procedure code", "code of criminal procedure", "the code"]},
    {"name": "Code of Civil Procedure, 1908", "short_code": "CPC", "year": 1908,
     "aliases": ["cpc", "c.p.c.", "civil procedure code", "code of civil procedure"]},
    {"name": "Constitution of India, 1950", "short_code": "CONSTITUTION", "year": 1950,
     "aliases": ["constitution", "constitution of india", "the constitution", "indian constitution"]},
    {"name": "Indian Evidence Act, 1872", "short_code": "IEA", "year": 1872,
     "aliases": ["evidence act", "indian evidence act", "i.e.a."]},
    {"name": "Negotiable Instruments Act, 1881", "short_code": "NI_ACT", "year": 1881,
     "aliases": ["ni act", "n.i. act", "negotiable instruments act", "negotiable instrument act"]},
    {"name": "Narcotic Drugs and Psychotropic Substances Act, 1985", "short_code": "NDPS", "year": 1985,
     "aliases": ["ndps", "ndps act", "narcotic drugs act"]},
    {"name": "Prevention of Corruption Act, 1988", "short_code": "PC_ACT", "year": 1988,
     "aliases": ["pc act", "p.c. act", "prevention of corruption act"]},
    {"name": "Protection of Children from Sexual Offences Act, 2012", "short_code": "POCSO", "year": 2012,
     "aliases": ["pocso", "pocso act"]},
    {"name": "Specific Relief Act, 1963", "short_code": "SRA", "year": 1963,
     "aliases": ["specific relief act", "sra"]},
    {"name": "Indian Contract Act, 1872", "short_code": "CONTRACT_ACT", "year": 1872,
     "aliases": ["contract act", "indian contract act"]},
    {"name": "Arbitration and Conciliation Act, 1996", "short_code": "ARBITRATION_ACT", "year": 1996,
     "aliases": ["arbitration act", "arbitration and conciliation act", "a&c act"]},
    {"name": "Motor Vehicles Act, 1988", "short_code": "MV_ACT", "year": 1988,
     "aliases": ["mv act", "m.v. act", "motor vehicles act", "motor vehicle act"]},
    {"name": "Insolvency and Bankruptcy Code, 2016", "short_code": "IBC", "year": 2016,
     "aliases": ["ibc", "i&b code", "insolvency and bankruptcy code"]},
    {"name": "Income Tax Act, 1961", "short_code": "IT_ACT", "year": 1961,
     "aliases": ["income tax act", "it act", "i.t. act"]},
    {"name": "Companies Act, 2013", "short_code": "COMPANIES_ACT", "year": 2013,
     "aliases": ["companies act", "companies act 2013"]},
    {"name": "Consumer Protection Act, 2019", "short_code": "CONSUMER_PROTECTION_ACT", "year": 2019,
     "aliases": ["consumer protection act"]},
]

_ALIAS_TO_STATUTE = {}
for _statute in CANONICAL_STATUTES:
    _ALIAS_TO_STATUTE[_statute["name"].lower()] = _statute
    _ALIAS_TO_STATUTE[_statute["short_code"].lower()] = _statute
    for _alias in _statute["aliases"]:
        _ALIAS_TO_STATUTE[_alias.lower().strip()] = _statute


def resolve_act(raw_act_name: str) -> Tuple[str, Optional[str], Optional[int]]:
    """
    Resolves a raw act name (as the LLM extraction produced it, e.g. "IPC",
    "the Code", "Indian Penal Code") to (statute_name, short_code, year).
    Falls back to title-casing the raw string as its own statute_name for
    acts not in the canonical registry (mostly state-specific Adhiniyams) —
    still a valid `statutes` row, just without a known short_code/year.
    """
    if not raw_act_name or not raw_act_name.strip():
        return ("Unknown Act", None, None)

    cleaned = re.sub(r"^(?:of\s+the|of|under|with|in)\s+", "", raw_act_name.strip(), flags=re.IGNORECASE).strip()
    lookup_key = cleaned.lower()

    if lookup_key in _ALIAS_TO_STATUTE:
        statute = _ALIAS_TO_STATUTE[lookup_key]
        return (statute["name"], statute["short_code"], statute["year"])

    for alias, statute in _ALIAS_TO_STATUTE.items():
        if alias in lookup_key or lookup_key in alias:
            return (statute["name"], statute["short_code"], statute["year"])

    return (cleaned.title() if len(cleaned) > 2 else "Special Act", None, None)
