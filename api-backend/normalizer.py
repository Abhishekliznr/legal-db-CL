"""
Legal Data Normalizer Layer for Indian Court Metadata
------------------------------------------------------
This module acts as the intermediary cleansing and normalization layer
between raw scraper JSON output and the PostgreSQL master schema.

Key Capabilities:
1. Judge Normalization: Strips honorifics (HON'BLE, MR. JUSTICE, CJI, DR., etc.),
   splits concatenated bench strings, resolves initials, and links to canonical judge records.
2. Act & Provision Normalization: Resolves raw strings (e.g. 'IPC', 'Indian Penal Code, 1860',
   'of Adhiniyam', 'Cr.P.C.', 'the Code') into canonical Act records and clean section numbers.
3. Party & Entity Cleaning: Strips legal noise ('VERSUS', 'THROUGH ITS SECRETARY', '@ alias').
"""

import re
from typing import Dict, Any, List, Optional, Tuple, Set


# ============================================================
# CANONICAL INDIAN ACTS & ALIAS REGISTRY
# ============================================================

CANONICAL_ACTS_SEED = [
    {
        "canonical_name": "Indian Penal Code, 1860",
        "short_code": "IPC",
        "year": 1860,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "ipc", "indian penal code", "i.p.c.", "i.p. code", "the penal code",
            "penal code", "indian penal code 1860", "indian penal code, 1860"
        ]
    },
    {
        "canonical_name": "Code of Criminal Procedure, 1973",
        "short_code": "CrPC",
        "year": 1973,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "crpc", "cr.p.c.", "crl.p.c.", "criminal procedure code",
            "code of criminal procedure", "the code of criminal procedure",
            "the code", "the code of criminal procedure, 1973", "code of criminal procedure 1973",
            "old code", "the criminal procedure code"
        ]
    },
    {
        "canonical_name": "Code of Civil Procedure, 1908",
        "short_code": "CPC",
        "year": 1908,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "cpc", "c.p.c.", "civil procedure code", "code of civil procedure",
            "the code of civil procedure", "code of civil procedure 1908",
            "the code of civil procedure, 1908"
        ]
    },
    {
        "canonical_name": "Constitution of India, 1950",
        "short_code": "CONSTITUTION",
        "year": 1950,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "constitution", "constitution of india", "the constitution of india",
            "the constitution", "const of india", "indian constitution"
        ]
    },
    {
        "canonical_name": "Indian Evidence Act, 1872",
        "short_code": "IEA",
        "year": 1872,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "evidence act", "indian evidence act", "i.e.a.", "the evidence act",
            "indian evidence act 1872", "indian evidence act, 1872"
        ]
    },
    {
        "canonical_name": "Negotiable Instruments Act, 1881",
        "short_code": "NI_ACT",
        "year": 1881,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "ni act", "n.i. act", "negotiable instruments act",
            "negotiable instrument act", "negotiable instruments act, 1881",
            "negotiable instruments act 1881", "n.i act 1881"
        ]
    },
    {
        "canonical_name": "Narcotic Drugs and Psychotropic Substances Act, 1985",
        "short_code": "NDPS",
        "year": 1985,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "ndps", "ndps act", "n.d.p.s. act", "narcotic drugs act",
            "narcotic drugs and psychotropic substances act", "ndps act 1985"
        ]
    },
    {
        "canonical_name": "Prevention of Corruption Act, 1988",
        "short_code": "PC_ACT",
        "year": 1988,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "pc act", "p.c. act", "prevention of corruption act",
            "prevention of corruption act 1988", "corruption act"
        ]
    },
    {
        "canonical_name": "Protection of Children from Sexual Offences Act, 2012",
        "short_code": "POCSO",
        "year": 2012,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "pocso", "pocso act", "p.o.c.s.o. act", "protection of children from sexual offences act",
            "pocso act 2012"
        ]
    },
    {
        "canonical_name": "Specific Relief Act, 1963",
        "short_code": "SRA",
        "year": 1963,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "specific relief act", "sra", "s.r.a.", "specific relief act 1963",
            "specific relief act, 1963"
        ]
    },
    {
        "canonical_name": "Indian Contract Act, 1872",
        "short_code": "CONTRACT_ACT",
        "year": 1872,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "contract act", "indian contract act", "contract act 1872", "indian contract act, 1872"
        ]
    },
    {
        "canonical_name": "Arbitration and Conciliation Act, 1996",
        "short_code": "ARBITRATION_ACT",
        "year": 1996,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "arbitration act", "arbitration and conciliation act", "a&c act",
            "arbitration and conciliation act 1996", "arbitration and conciliation act, 1996"
        ]
    },
    {
        "canonical_name": "Motor Vehicles Act, 1988",
        "short_code": "MV_ACT",
        "year": 1988,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "mv act", "m.v. act", "motor vehicles act", "motor vehicle act",
            "motor vehicles act 1988", "motor vehicles act, 1988"
        ]
    },
    {
        "canonical_name": "Insolvency and Bankruptcy Code, 2016",
        "short_code": "IBC",
        "year": 2016,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "ibc", "i&b code", "insolvency and bankruptcy code", "insolvency code",
            "insolvency and bankruptcy code 2016", "ibc 2016"
        ]
    },
    {
        "canonical_name": "Income Tax Act, 1961",
        "short_code": "IT_ACT",
        "year": 1961,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "income tax act", "it act", "i.t. act", "income tax act 1961", "income tax act, 1961"
        ]
    },
    {
        "canonical_name": "Companies Act, 2013",
        "short_code": "COMPANIES_ACT",
        "year": 2013,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "companies act", "companies act 2013", "companies act, 2013", "companies act 1956"
        ]
    },
    {
        "canonical_name": "Right to Fair Compensation and Transparency in Land Acquisition, Rehabilitation and Resettlement Act, 2013",
        "short_code": "LAND_ACQUISITION_ACT",
        "year": 2013,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "land acquisition act", "land acquisition act 1894", "land acquisition act 2013",
            "rfctlarr act", "rfctlarr act 2013"
        ]
    },
    {
        "canonical_name": "Consumer Protection Act, 2019",
        "short_code": "CONSUMER_PROTECTION_ACT",
        "year": 2019,
        "jurisdiction": "CENTRAL",
        "aliases": [
            "consumer protection act", "consumer protection act 1986", "consumer protection act 2019"
        ]
    }
]


# Build alias lookup map in memory for fast lookup
ACT_ALIAS_MAP: Dict[str, str] = {}
for act in CANONICAL_ACTS_SEED:
    canonical = act["canonical_name"]
    # map canonical lowercase
    ACT_ALIAS_MAP[canonical.lower()] = canonical
    ACT_ALIAS_MAP[act["short_code"].lower()] = canonical
    for alias in act["aliases"]:
        ACT_ALIAS_MAP[alias.lower().strip()] = canonical


# ============================================================
# 1. JUDGE NORMALIZER
# ============================================================

# Honorifics to strip (preserve initials like J.B. or B.V.)
HONORIFICS_PREFIX_REGEX = re.compile(
    r"^\s*(?:HON'?BLE\s+)?(?:MR\.?|MRS\.?|MS\.?|SMT\.?|DR\.?|SH\.?|SHRI\s+)?(?:CHIEF\s+JUSTICE|CJI|JUSTICE|ACTING\s+CHIEF\s+JUSTICE)\s+",
    re.IGNORECASE
)

HONORIFICS_SUFFIX_REGEX = re.compile(
    r"(?:,\s*|\s+)(?:CJI|CHIEF\s+JUSTICE|JUSTICE|JJ?\.|PRESIDING|COMPANION|ACTING)\s*$",
    re.IGNORECASE
)

PUNCTUATION_CLEANUP_REGEX = re.compile(r"^[,\-\s\.\(\)\[\]]+|[,\-\s\.\(\)\[\]]+$")


def clean_judge_name(raw_name: str) -> str:
    """
    Cleans a single judge name by removing legal honorifics, titles, and extra whitespace
    while strictly preserving initials like 'J.B. PARDIWALA' or 'B.V. NAGARATHNA'.
    """
    if not raw_name:
        return ""

    # 1. Remove bench annotations in brackets/parentheses e.g. [PRESIDING], (COMPANION)
    name = re.sub(r"\[.*?\]|\(.*?\)", "", raw_name).strip()
    
    # 2. Strip trailing judge suffix like ", J." or ", JJ." or ", CJI"
    name = re.sub(r"(?:,\s*|\s+)(?:CJI|CHIEF\s+JUSTICE|JUSTICE|JJ?\.|PRESIDING|COMPANION|ACTING)\s*$", "", name, flags=re.IGNORECASE).strip()
    
    # 3. Strip leading honorific words (HON'BLE, MR, MRS, MS, SMT, DR, SH, SHRI, CHIEF JUSTICE, JUSTICE)
    name = re.sub(r"\b(?:HON'?BLE|CHIEF\s+JUSTICE|CJI|JUSTICE|ACTING|SMT\.?|SHRI|SH\.?|MR\.?|MRS\.?|MS\.?|DR\.?)\b", "", name, flags=re.IGNORECASE).strip()
    
    # 4. Clean leading/trailing punctuation and multiple spaces
    name = PUNCTUATION_CLEANUP_REGEX.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    
    return name.upper() if name else ""


def split_judge_bench(raw_bench_str: str) -> List[Tuple[str, str]]:
    """
    Splits concatenated bench strings into individual judges with roles.
    Example:
      "HON'BLE MR. JUSTICE B.V. NAGARATHNA, HON'BLE MR. JUSTICE NONGMEIKAPAM KOTISWAR SINGH"
      -> [("B.V. NAGARATHNA", "PRESIDING"), ("NONGMEIKAPAM KOTISWAR SINGH", "COMPANION")]
    """
    if not raw_bench_str:
        return []

    # Check delimiters: "AND", "&", ",", ";", "WITH"
    # Protect initials like "J.B." before splitting on comma
    delimiters = r"(?:\s+AND\s+|\s*&\s*|\s*;\s*|,\s*(?=HON|MR|JUSTICE|SMT|DR|SH))"
    chunks = re.split(delimiters, raw_bench_str, flags=re.IGNORECASE)
    
    if len(chunks) == 1 and "," in chunks[0] and not any(h in chunks[0].upper() for h in ["HON", "JUSTICE", "MR."]):
        # Check if multiple names separated by comma
        possible_parts = chunks[0].split(",")
        if len(possible_parts) > 1 and all(len(p.strip().split()) >= 2 for p in possible_parts):
            chunks = possible_parts

    judges_with_roles: List[Tuple[str, str]] = []
    for idx, chunk in enumerate(chunks):
        cleaned = clean_judge_name(chunk)
        if cleaned and len(cleaned) >= 3:
            role = "PRESIDING" if idx == 0 else "COMPANION"
            judges_with_roles.append((cleaned, role))

    return judges_with_roles


# ============================================================
# 2. ACT & PROVISION NORMALIZER
# ============================================================

SECTION_REGEX = re.compile(
    r"\b(?:Section|Sec\.?|S\.?|u/s|under\s+section)\s*([0-9]+[A-Za-z]*(?:\([0-9A-Za-z]+\))*)\b",
    re.IGNORECASE
)

ADHINIYAM_REGEX = re.compile(
    r"\b([A-Za-z\s]+)\s+(?:Adhiniyam|Sanstha|Niyam|Act|Vidhik)\b",
    re.IGNORECASE
)


def normalize_act_name(raw_act: str) -> Tuple[str, str]:
    """
    Resolves raw act names against the Canonical Act Registry.
    Returns: (canonical_name, raw_cleaned_name)
    Example:
      "IPC" -> ("Indian Penal Code, 1860", "IPC")
      "the Code s.482" -> ("Code of Criminal Procedure, 1973", "the Code")
      "Madhya Pradesh Uchcha Nyayalaya Adhiniyam" -> ("Madhya Pradesh Uchcha Nyayalaya Adhiniyam", ...)
    """
    if not raw_act:
        return ("Unknown Act", "")

    cleaned = raw_act.strip()
    # Remove leading prepositions like "of the", "under", "with"
    cleaned = re.sub(r"^(?:of\s+the|of|under|with|in)\s+", "", cleaned, flags=re.IGNORECASE).strip()
    # Remove section mentions inside act string
    cleaned = SECTION_REGEX.sub("", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    lookup_key = cleaned.lower()
    
    # 1. Exact match in canonical registry
    if lookup_key in ACT_ALIAS_MAP:
        return (ACT_ALIAS_MAP[lookup_key], cleaned)

    # 2. Prefix / Contains match (e.g. "Indian Penal Code s.302")
    for alias, canonical in ACT_ALIAS_MAP.items():
        if alias in lookup_key or lookup_key in alias:
            return (canonical, cleaned)

    # 3. State Adhiniyam / Special Act Handling
    adhiniyam_match = ADHINIYAM_REGEX.search(cleaned)
    if adhiniyam_match:
        canonical_state_act = cleaned.title()
        return (canonical_state_act, cleaned)

    # 4. Fallback: clean title casing
    return (cleaned.title() if len(cleaned) > 2 else "Special Act", cleaned)


def normalize_provision(raw_provision: str) -> Dict[str, Any]:
    """
    Normalizes a provision string like 'Indian Penal Code s.302' or 'Section 482 of the Code'.
    Returns structured dict:
      {
        "canonical_act": "Indian Penal Code, 1860",
        "raw_act": "Indian Penal Code",
        "section": "302",
        "full_text": "Section 302, Indian Penal Code, 1860"
      }
    """
    section_match = SECTION_REGEX.search(raw_provision)
    section_num = section_match.group(1) if section_match else None
    
    # Extract act part
    act_part = SECTION_REGEX.sub("", raw_provision).strip()
    canonical_act, raw_act = normalize_act_name(act_part)
    
    return {
        "canonical_act": canonical_act,
        "raw_act": raw_act or canonical_act,
        "section": section_num,
        "full_text": f"Section {section_num}, {canonical_act}" if section_num else canonical_act
    }


# ============================================================
# 3. PARTY & ADVOCATE CLEANER
# ============================================================

PARTY_NOISE_REGEX = re.compile(
    r"\b(THROUGH\s+ITS|SECRETARY|DEPARTMENT\s+OF|REPRESENTED\s+BY|AUTH\.?\s+SIGNATORY|REGISTERED\s+OFFICE|ALIAS|@)\b.*$",
    re.IGNORECASE
)


def clean_party_name(raw_name: str) -> str:
    """
    Cleans party names by stripping trailing noise and normalizing spacing.
    """
    if not raw_name:
        return ""
    
    name = re.sub(r"^(?:IN\s+THE\s+MATTER\s+OF|BETWEEN:?)\s*", "", raw_name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip()
    return name.upper()


def clean_advocate_name(raw_adv: str) -> str:
    """
    Cleans advocate names by stripping prefixes (Adv., Mr., Sh., Senior Advocate, etc.)
    """
    if not raw_adv:
        return ""
    
    name = re.sub(r"\b(ADV\.?|ADVOCATE|SR\.?\s+ADV\.?|SENIOR\s+ADVOCATE|AOR|MR\.?|MS\.?|MRS\.?)\b", "", raw_adv, flags=re.IGNORECASE)
    name = PUNCTUATION_CLEANUP_REGEX.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name.upper()
