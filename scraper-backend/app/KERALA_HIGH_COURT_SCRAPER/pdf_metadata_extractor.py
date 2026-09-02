import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import fitz


# ============================================================
# KERALA HIGH COURT
# PDF + EXISTING JSON -> STANDARDIZED METADATA JSON
#
# Put this file inside:
#
# app/
# └── KERALA_HIGH_COURT_SCRAPER/
#     ├── pdf_metadata_extractor.py
#     ├── kerala_judgments.json
#     └── pdf/
#
# Run:
#     python pdf_metadata_extractor.py
#
# Output:
#     kerala_metadata.json
# ============================================================


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

INPUT_JSON = BASE_DIR / "kerala_judgments.json"
PDF_DIR = BASE_DIR / "pdf"
OUTPUT_JSON = BASE_DIR / "kerala_metadata.json"

SCHEMA_VERSION = "1.0"
EXTRACTOR_VERSION = "1.0"


# ============================================================
# CASE TYPES
# ============================================================

CASE_TYPES = {
    "LPA": "Letters Patent Appeal",
    "CRL.A.": "Criminal Appeal",
    "CRL.A": "Criminal Appeal",
    "CRL.REV.P.": "Criminal Revision Petition",
    "CRL.REV.P": "Criminal Revision Petition",
    "W.P.(C)": "Writ Petition Civil",
    "WP(C)": "Writ Petition Civil",
    "WP(C).": "Writ Petition Civil",
    "WPMB": "Writ Petition Miscellaneous",
    "WPMS": "Writ Petition Miscellaneous",
    "W.P.(CRL)": "Writ Petition Criminal",
    "FAO": "First Appeal From Order",
    "RFA": "Regular First Appeal",
    "CS(COMM)": "Commercial Suit",
    "CS(OS)": "Civil Suit (Original Side)",
    "BAIL APPLN.": "Bail Application",
    "BA": "Bail Application",
    "ARB.P.": "Arbitration Petition",
    "ARB. P.": "Arbitration Petition",
    "OMP": "Original Miscellaneous Petition",
    "CONT.CAS(C)": "Contempt Case (Civil)",
    "AR": "Arbitration",
    "OP": "Original Petition",
    "OT.REV.": "Other Revision",
    "MAT.APP.": "Matrimonial Appeal",
}


# ============================================================
# ACT ALIASES
# ============================================================

ACT_ALIASES = {
    "ipc": "Indian Penal Code",
    "i.p.c": "Indian Penal Code",
    "i.p.c.": "Indian Penal Code",
    "indian penal code": "Indian Penal Code",

    "crpc": "Code of Criminal Procedure",
    "cr.p.c": "Code of Criminal Procedure",
    "cr.p.c.": "Code of Criminal Procedure",
    "code of criminal procedure": "Code of Criminal Procedure",

    "cpc": "Code of Civil Procedure",
    "c.p.c": "Code of Civil Procedure",
    "c.p.c.": "Code of Civil Procedure",
    "code of civil procedure": "Code of Civil Procedure",

    "bns": "Bharatiya Nyaya Sanhita",
    "bnss": "Bharatiya Nagarik Suraksha Sanhita",
    "bsa": "Bharatiya Sakshya Adhiniyam",

    "ni act": "Negotiable Instruments Act",
    "negotiable instruments act": "Negotiable Instruments Act",

    "gst act": "Goods and Services Tax Act",
    "goods and services tax act": "Goods and Services Tax Act",

    "evidence act": "Indian Evidence Act",
    "indian evidence act": "Indian Evidence Act",

    "companies act": "Companies Act",
    "companies act 2013": "Companies Act",

    "limitation act": "Limitation Act",

    "arbitration act": "Arbitration and Conciliation Act",
    "arbitration and conciliation act": "Arbitration and Conciliation Act",

    "pocso": "Protection of Children from Sexual Offences Act",
    "pocso act": "Protection of Children from Sexual Offences Act",

    "ndps": "Narcotic Drugs and Psychotropic Substances Act",
    "ndps act": "Narcotic Drugs and Psychotropic Substances Act",

    "constitution": "Constitution of India",
    "constitution of india": "Constitution of India",

    "ker": "Kerala Education Rules",
    "kerala education rules": "Kerala Education Rules",
    "ksr": "Kerala Service Rules",
    "kerala service rules": "Kerala Service Rules",
    "kerala land relinquishment act": "Kerala Land Relinquishment Act",
    "kerala land reforms act": "Kerala Land Reforms Act",
    "kerala high court rules": "Kerala High Court Rules",
}


# ============================================================
# LEGAL KEYWORDS
# ============================================================

LEGAL_KEYWORDS = {
    "murder",
    "rape",
    "dowry",
    "property",
    "service",
    "employment",
    "bank",
    "loan",
    "fraud",
    "cheque",
    "consumer",
    "insurance",
    "tax",
    "gst",
    "arbitration",
    "cyber",
    "bail",
    "ndps",
    "pocso",
    "company",
    "contract",
    "divorce",
    "maintenance",
    "custody",
    "land",
    "eviction",
    "tenant",
    "education",
    "sarfaesi",
    "npa",
    "mortgage",
    "fir",
    "conviction",
    "acquittal",
    "appeal",
    "petition",
    "writ",
    "employment",
    "pension",
    "promotion",
    "termination",
    "suspension",
    "salary",
    "lease",
    "possession",
    "partition",
}


# ============================================================
# SUBJECT KEYWORDS
# ============================================================

SUBJECT_KEYWORDS = {
    "CRIMINAL": [
        "murder",
        "rape",
        "dowry",
        "bail",
        "pocso",
        "ndps",
        "ipc",
        "crpc",
        "bns",
        "bnss",
        "conviction",
        "acquittal",
        "fir",
    ],
    "BANKING": [
        "bank",
        "loan",
        "cheque",
        "sarfaesi",
        "mortgage",
        "secured creditor",
        "npa",
        "drt",
        "drat",
        "debenture",
    ],
    "PROPERTY": [
        "property",
        "land",
        "eviction",
        "tenant",
        "rent",
        "lease",
        "possession",
        "title",
        "partition",
    ],
    "CONSUMER": [
        "consumer",
        "deficiency",
        "insurance",
        "claim",
        "compensation",
    ],
    "SERVICE": [
        "service",
        "employment",
        "promotion",
        "pension",
        "termination",
        "suspension",
        "salary",
    ],
    "EDUCATION": [
        "school",
        "teacher",
        "headmaster",
        "educational",
        "university",
        "college",
        "student",
        "examination",
    ],
    "TAX": [
        "tax",
        "gst",
        "income tax",
        "assessment",
        "customs",
        "excise",
        "vat",
    ],
    "CORPORATE": [
        "insolvency",
        "ibc",
        "nclt",
        "merger",
        "shareholder",
        "director",
        "company law",
        "winding up",
    ],
    "ARBITRATION": [
        "arbitration",
        "arbitrator",
        "award",
        "section 11",
        "section 34",
    ],
    "FAMILY": [
        "divorce",
        "maintenance",
        "custody",
        "matrimonial",
        "marriage",
        "guardianship",
    ],
    "CYBER_LAW": [
        "cyber",
        "information technology",
        "data",
        "hacking",
        "it act",
    ],
    "CONTRACT_LAW": [
        "contract",
        "agreement",
        "breach",
        "damages",
        "specific performance",
    ],
}


# ============================================================
# BAD ADVOCATE NAME PATTERNS
# ============================================================

BAD_NAME_PATTERNS = [
    r"^adv\.?$",
    r"^mr\.?$",
    r"^ms\.?$",
    r"^mrs\.?$",
    r"^dr\.?$",
    r"^nemo\.?$",
    r"^advocates?\.?$",
    r"^advs?\.?$",
    r"^senior panel counsel.*",
    r"^standing counsel.*",
    r"^spc.*",
    r"^cgsc.*",
]


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean_text(text: Any) -> str:
    if text is None:
        return ""

    text = str(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\xad", "")
    text = text.replace("\xa0", " ")
    text = text.replace("\t", " ")

    # Repair line-break hyphenation.
    text = re.sub(r"(\w+)-\n(\w+)", r"\1\2", text)

    # Normalize spaces without destroying paragraph boundaries.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)

    return text.strip()


def normalize_date(value: Any) -> str | None:
    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    # Strip ordinals like 07th, 1st, 2nd, 3rd, 14th
    value = re.sub(r"(\d{1,2})(st|nd|rd|th)", r"\1", value, flags=re.IGNORECASE)
    value = value.replace(",", "").strip()

    formats = (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%d/%m/%Y",
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d %Y",
        "%b %d %Y",
    )

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass

    return value


def unique_preserve(values: list[str]) -> list[str]:
    output = []
    seen = set()

    for value in values:
        value = value.strip()

        if not value:
            continue

        key = value.casefold()

        if key not in seen:
            seen.add(key)
            output.append(value)

    return output


COMPANY_MARKERS = [
    "M/S",
    "MS.",
    "PRIVATE LIMITED",
    "PVT LTD",
    "LIMITED",
    "LTD",
    "LLP",
    "CORPORATION",
    "COMPANY",
    "CO.",
    "APARTMENTS",
    "PROPERTIES",
    "BUILDERS",
    "DEVELOPERS",
    "ENTERPRISES",
    "TRUST",
    "SOCIETY",
    "ASSOCIATION",
    "FIRM",
    "INFRASTRUCTURE",
]


def infer_party_type(name: str) -> str:
    name_upper = name.upper()

    if any(
        keyword in name_upper
        for keyword in [
            "BANK",
            "HDFC",
            "ICICI",
            "SBI",
            "STATE BANK",
            "PUNJAB NATIONAL",
            "CANARA",
            "AXIS",
        ]
    ):
        return "BANK"

    if any(
        keyword in name_upper
        for keyword in [
            "UNION OF INDIA",
            "GOVT",
            "GOVERNMENT",
            "COMMISSIONER OF",
            "DIRECTORATE OF",
        ]
    ):
        return "GOVERNMENT"

    if any(
        keyword in name_upper
        for keyword in [
            "STATE OF",
            "STATE (",
            "STATE NCT",
        ]
    ):
        return "STATE"

    if any(marker in name_upper for marker in COMPANY_MARKERS):
        return "COMPANY"

    return "INDIVIDUAL"


def normalize_disposal(raw_value: str | None) -> str | None:
    if not raw_value:
        return None

    normalized = clean_text(raw_value).upper()

    mapping = {
        "DISPOSED OF": "DISPOSED",
        "DISPOSED": "DISPOSED",
        "CLOSED": "CLOSED",
        "DISMISSED": "DISMISSED",
        "ALLOWED": "ALLOWED",
        "PARTLY ALLOWED": "PARTLY_ALLOWED",
        "BAIL GRANTED": "BAIL_GRANTED",
        "BAIL REJECTED": "BAIL_REJECTED",
    }

    return mapping.get(normalized, normalized)


# ============================================================
# CASE METADATA EXTRACTOR
# ============================================================

class MetadataExtractor:
    def __init__(
        self,
        raw_text: str,
        pdf_path: str,
        scraper_record: dict,
        is_scanned: bool = False,
    ):
        self.raw_text = raw_text
        self.text = clean_text(raw_text)
        self.pdf_path = pdf_path
        self.scraper_record = scraper_record
        self.is_scanned = is_scanned
        self.missing_fields: list[str] = []

    # --------------------------------------------------------
    # CNR
    # --------------------------------------------------------

    def extract_cnr(self) -> str | None:
        scraper_cnr = self.scraper_record.get("cnr")

        if scraper_cnr:
            return str(scraper_cnr).upper()

        patterns = [
            r"(?:CNR\s*(?:No\.?|Number)?|Case\s+CNR|Unique\s+Case\s+ID)\s*:?\s*([A-Z0-9]{16})",
            r"\b([A-Z]{4}[0-9]{12})\b",
        ]

        for pattern in patterns:
            match = re.search(
                pattern,
                self.text,
                re.IGNORECASE,
            )

            if match:
                return match.group(1).upper()

        self.missing_fields.append("cnr")
        return None

    # --------------------------------------------------------
    # Neutral citation
    # --------------------------------------------------------

    def extract_neutral_citation(self) -> str | None:
        patterns = [
            r"Neutral\s+Citation\s*(?:No\.?|Number)?\s*:?\s*([0-9]{4}\s*[:/_-]?\s*[A-Z]{2,10}\s*[:/_-]?\s*\d+(?:-[A-Z0-9]+)?)",
            r"\b([0-9]{4}:[A-Z]{2,10}:\d+(?:-[A-Z0-9]+)?)\b",
            r"\b([0-9]{4}/[A-Z]{2,10}/\d+(?:-[A-Z0-9]+)?)\b",
        ]

        for pattern in patterns:
            match = re.search(
                pattern,
                self.text,
                re.IGNORECASE,
            )

            if match:
                citation = match.group(1).strip()
                citation = re.sub(r"[\s/]+", ":", citation)
                return citation

        # Do not fabricate a citation.
        self.missing_fields.append("neutral_citation")
        return None

    # --------------------------------------------------------
    # Case number
    # --------------------------------------------------------

    def extract_case_number(self) -> dict:
        scraper_case = self.scraper_record.get("case_number")

        patterns = [
            r"\b([A-Z][A-Z0-9.\-()/]*)\s*(?:No\.?\s*)?(\d+\s*/\s*\d{4})\b",
            r"\b([A-Z][A-Z0-9.\-()/]*)\s+(\d+\s+of\s+\d{4})\b",
        ]

        for pattern in patterns:
            match = re.search(
                pattern,
                self.text,
                re.IGNORECASE,
            )

            if not match:
                continue

            type_code = match.group(1).strip()
            raw_number = match.group(2).strip()

            normalized_number = raw_number.replace(
                " of ",
                "/",
            )

            parts = normalized_number.split("/")

            try:
                number_value = int(
                    parts[0].strip()
                )
            except (ValueError, IndexError):
                number_value = None

            try:
                year_value = int(
                    parts[1].strip()
                )
            except (ValueError, IndexError):
                year_value = None

            type_name = CASE_TYPES.get(
                type_code,
                CASE_TYPES.get(
                    type_code.replace(" ", ""),
                    type_code,
                ),
            )

            return {
                "display": (
                    f"{type_code} "
                    f"{raw_number}"
                ),
                "type_code": type_code,
                "type_name": type_name,
                "number": number_value,
                "year": year_value,
            }

        # Important fallback: use scraper JSON.
        if scraper_case:
            display = clean_text(scraper_case)

            match = re.match(
                r"^([A-Z][A-Z0-9.\-()/]*)\s*(?:/|\s)?(\d+)\s*/\s*(\d{4})$",
                display or "",
            )

            if match:
                type_code = match.group(1).strip()

                try:
                    number_value = int(match.group(2))
                except ValueError:
                    number_value = None

                try:
                    year_value = int(match.group(3))
                except ValueError:
                    year_value = None

                return {
                    "display": display,
                    "type_code": type_code,
                    "type_name": CASE_TYPES.get(
                        type_code,
                        type_code,
                    ),
                    "number": number_value,
                    "year": year_value,
                }

            of_match = re.match(
                r"^(.+?)\s+(\d+)\s+of\s+(\d{4})$",
                display or "",
                re.IGNORECASE,
            )

            if of_match:
                type_code = of_match.group(1).strip()

                return {
                    "display": display,
                    "type_code": type_code,
                    "type_name": CASE_TYPES.get(
                        type_code,
                        type_code,
                    ),
                    "number": int(of_match.group(2)),
                    "year": int(of_match.group(3)),
                }

            return {
                "display": display,
                "type_code": None,
                "type_name": None,
                "number": None,
                "year": None,
            }

        self.missing_fields.append("case_number")

        return {
            "display": None,
            "type_code": None,
            "type_name": None,
            "number": None,
            "year": None,
        }

    # --------------------------------------------------------
    # Dates
    # --------------------------------------------------------

    def extract_decision_date(self) -> str | None:
        scraper_date = normalize_date(
            self.scraper_record.get("decision_date")
        )

        if scraper_date:
            return scraper_date

        patterns = [
            r"(?:Date\s+of\s+Decision|Decision\s+Date)\s*:?\s*([0-9]{1,2}[\./-][0-9]{1,2}[\./-][0-9]{4})",
            r"(?:Pronounced\s+on|Delivered\s+on|Judgment\s+Delivered\s+on|Order\s+pronounced\s+on)\s*:?\s*([0-9]{1,2}[\./-][0-9]{1,2}[\./-][0-9]{4})",
            r"Dated\s*:?\s*([0-9]{1,2}[\./-][0-9]{1,2}[\./-][0-9]{4})",
        ]

        for pattern in patterns:
            match = re.search(
                pattern,
                self.text,
                re.IGNORECASE,
            )

            if match:
                return normalize_date(
                    match.group(1)
                )

        self.missing_fields.append("decision_date")
        return None

    def extract_pronounced_on(self) -> str | None:
        patterns = [
            r"(?:Pronounced\s+on|Delivered\s+on|Judgment\s+Delivered\s+on|Order\s+pronounced\s+on)\s*:?\s*([0-9]{1,2}[\./-][0-9]{1,2}[\./-][0-9]{4})",
            r"(?:Pronounced\s+on|Delivered\s+on)\s*:?\s*([0-9]{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+,?\s+[0-9]{4})",
        ]

        for pattern in patterns:
            match = re.search(
                pattern,
                self.text,
                re.IGNORECASE,
            )

            if match:
                return normalize_date(
                    match.group(1)
                )

        # If the scraper has decision_date, use that as fallback.
        decision_date = normalize_date(
            self.scraper_record.get("decision_date")
        )

        return decision_date

    # --------------------------------------------------------
    # Judges
    # --------------------------------------------------------

    def extract_judges(self) -> list[dict]:
        judges = []
        raw_names = []

        header_block = self.text[:2000]
        matches = re.finditer(
            r"(?:THE\s+HON[\'’]?BLE\s+)?(?:MR\.|MS\.|MRS\.|DR\.)?\s*JUSTICE\s+([A-Z][A-Z\s\.\'\-]{2,})",
            header_block,
            re.IGNORECASE,
        )
        for m in matches:
            raw_name = m.group(1).strip()
            clean_n = re.split(
                r"\b(COURT|PRESENT|TUESDAY|MONDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY|DATED|ORDER|JUDGMENT|OF|AT|ERNAKULAM|HIGH)\b|\n",
                raw_name,
                flags=re.IGNORECASE,
            )[0].strip(" ,.;:")
            clean_n = re.sub(r"\s+", " ", clean_n).strip()
            if len(clean_n) >= 3 and clean_n not in raw_names:
                raw_names.append(clean_n)

        # 2. End signature pattern: Sd/- \n <NAME>, \n Judge.
        if not raw_names:
            sig_matches = re.finditer(
                r"Sd/-\s*\n\s*([A-Z][A-Z\s\.\'\-]{2,})\s*,?\s*\n\s*Judge",
                self.text,
                re.IGNORECASE,
            )
            for m in sig_matches:
                clean_n = m.group(1).strip(" ,.;:")
                clean_n = re.sub(r"\s+", " ", clean_n).strip()
                if len(clean_n) >= 3 and clean_n not in raw_names:
                    raw_names.append(clean_n)

        # 3. Fallback to scraper JSON judge text with strict regex parsing
        if not raw_names:
            scraper_judge = clean_text(self.scraper_record.get("judge"))
            if scraper_judge:
                match = re.search(
                    r"JUSTICE\s+([A-Z][A-Z\s\.\'-]{2,})",
                    scraper_judge,
                    re.IGNORECASE,
                )
                if match:
                    clean_n = re.split(
                        r"\b(COURT|PRESENT|TUESDAY|MONDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY|DATED|ORDER|JUDGMENT|WP\(C\)|PETITIONER|RESPONDENT|AGED|SON|DAUGHTER|HOUSE)\b|\n",
                        match.group(1),
                        flags=re.IGNORECASE,
                    )[0].strip(" ,.;:")
                    clean_n = re.sub(r"\s+", " ", clean_n).strip()
                    if clean_n and len(clean_n) >= 3:
                        raw_names.append(clean_n)

        for index, name in enumerate(raw_names):
            judge_id = re.sub(r"[^a-z0-9_]", "", name.lower().replace(" ", "_")).strip("_")
            judges.append(
                {
                    "judge_id": judge_id or "unknown_judge",
                    "name": name,
                    "role": (
                        "PRESIDING"
                        if index == 0
                        else "COMPANION"
                    ),
                }
            )

        if not judges:
            self.missing_fields.append("judges")

        return judges

    # --------------------------------------------------------
    # Parties
    # --------------------------------------------------------

    def extract_parties(self) -> list[dict]:
        parties = []

        case_party = clean_text(
            self.scraper_record.get("party_name")
        )

        if case_party and re.search(
            r"\bVs\b|\bVersus\b",
            case_party,
            re.IGNORECASE,
        ):
            left, right = re.split(
                r"\s+(?:Vs|Versus)\s+",
                case_party,
                maxsplit=1,
                flags=re.IGNORECASE,
            )

            left = clean_text(left)
            right = clean_text(right)

            if left:
                parties.append(
                    {
                        "name": left,
                        "role": "PETITIONER",
                        "party_type": infer_party_type(left),
                    }
                )

            if right:
                parties.append(
                    {
                        "name": right,
                        "role": "RESPONDENT",
                        "party_type": infer_party_type(right),
                    }
                )

            return parties

        # PDF fallback.
        patterns = [
            r"(?:^|\n)\s*([A-Z0-9\s&.,()/'\-]+?)\s*\n\s*\.{2,}\s*(?:Petitioners?|Appellants?|Applicants?|Plaintiffs?)",
            r"(?:^|\n)\s*([A-Z0-9\s&.,()/'\-]+?)\s+versus\s+",
        ]

        petitioner = None

        for pattern in patterns:
            match = re.search(
                pattern,
                self.text,
                re.IGNORECASE,
            )

            if match:
                candidate = clean_text(
                    match.group(1)
                )

                if (
                    candidate
                    and len(candidate) > 2
                    and "HIGH COURT" not in candidate.upper()
                ):
                    petitioner = candidate
                    break

        respondent = None

        patterns = [
            r"versus\s+([A-Z0-9\s&.,()/'\-]+?)(?=\n|CORAM:|Through:|JUDGMENT|ORDER|$)",
            r"versus\s*\n\s*([A-Z0-9\s&.,()/'\-]+)",
        ]

        for pattern in patterns:
            match = re.search(
                pattern,
                self.text,
                re.IGNORECASE,
            )

            if match:
                candidate = clean_text(
                    match.group(1)
                )

                if (
                    candidate
                    and len(candidate) > 2
                    and "HIGH COURT" not in candidate.upper()
                ):
                    respondent = candidate
                    break

        if petitioner:
            parties.append(
                {
                    "name": petitioner,
                    "role": "PETITIONER",
                    "party_type": infer_party_type(petitioner),
                }
            )

        if respondent:
            parties.append(
                {
                    "name": respondent,
                    "role": "RESPONDENT",
                    "party_type": infer_party_type(respondent),
                }
            )

        if not parties:
            self.missing_fields.append("parties")

        return parties

    # --------------------------------------------------------
    # Advocates
    # --------------------------------------------------------

    def extract_advocates(self) -> list[dict]:
        advocates = []
        seen = set()

        pet_match = re.search(
            r"PETITIONER[S\(\)]*:?(.*?)(?=RESPONDENT[S\(\)]*:?|THIS\s+|CORAM:|$)",
            self.text,
            re.DOTALL | re.IGNORECASE,
        )
        resp_match = re.search(
            r"RESPONDENT[S\(\)]*:?(.*?)(?=THIS\s+|CORAM:|JUDGMENT|ORDER|$)",
            self.text,
            re.DOTALL | re.IGNORECASE,
        )

        sections = [
            (pet_match.group(1) if pet_match else "", "PETITIONER"),
            (resp_match.group(1) if resp_match else "", "RESPONDENT"),
        ]

        for section_text, role in sections:
            if not section_text:
                continue

            adv_matches = re.finditer(
                r"(?:BY\s+ADVS?\.?|BY\s+ADVOCATES?|GOVERNMENT\s+PLEADER|SRI\.|SMT\.|ADV\.)\s*(.*?)(?=\n\n|\n[0-9]+\b|RESPONDENT|THIS\s+|JUDGMENT|ORDER|$)",
                section_text,
                re.DOTALL | re.IGNORECASE,
            )

            for adv_m in adv_matches:
                block = adv_m.group(0).strip()
                block = re.split(r"RESPONDENT[S\(\)]*:?|THIS\s+|CORAM:|JUDGMENT|ORDER", block, flags=re.IGNORECASE)[0]
                lines = [l.strip() for l in block.split("\n") if l.strip()]

                for line in lines:
                    line_clean = re.sub(r"^(?:BY\s+ADVS?\.?|BY\s+ADVOCATES?)\s*", "", line, flags=re.IGNORECASE).strip()
                    if not line_clean or len(line_clean) < 3:
                        continue

                    if re.match(r"^\d+$", line_clean) or any(k in line_clean.upper() for k in ["SECRETARIAT", "THIRUVANANTHAPURAM", "KOTTAYAM", "PIN", "HIGH COURT", "DEPARTMENT", "OFFICE", "TEACHER", "SCHOOL", "MANAGER", "PRINCIPAL", "HEADMASTER", "DISTRICT"]):
                        continue

                    title = None
                    title_m = re.match(r"^(SRI\.|SRI|SMT\.|SMT|MR\.|MS\.|MRS\.|DR\.|ADV\.)\s*", line_clean, re.IGNORECASE)
                    if title_m:
                        title = title_m.group(1).title()
                        line_clean = line_clean[len(title_m.group(0)):].strip()

                    designation = "Advocate"
                    if "(SR.)" in line_clean.upper() or "SENIOR ADVOCATE" in line_clean.upper():
                        designation = "Senior Advocate"
                        line_clean = re.sub(r"\(SR\.\)|SENIOR ADVOCATE", "", line_clean, flags=re.IGNORECASE).strip()

                    if "GOVERNMENT PLEADER" in line_clean.upper() or "STANDING COUNSEL" in line_clean.upper():
                        designation = "Government Pleader" if "GOVERNMENT" in line_clean.upper() else "Standing Counsel"
                        line_clean = re.sub(r"GOVERNMENT PLEADER|STANDING COUNSEL", "", line_clean, flags=re.IGNORECASE).strip(" ,")

                    name = clean_text(line_clean).strip(" ,.;:")
                    if len(name) >= 3 and name.casefold() not in seen and not any(re.match(p, name, re.IGNORECASE) for p in BAD_NAME_PATTERNS):
                        seen.add(name.casefold())
                        advocates.append(
                            {
                                "name": name,
                                "title": title,
                                "designation": designation,
                                "for_party_role": role,
                            }
                        )

        # Fallback to Through: block pattern
        if not advocates:
            blocks = re.findall(
                r"Through:\s*(.*?)(?=\n\s*(?:versus|CORAM:|\+|\n\n[A-Z]))",
                self.text,
                re.DOTALL | re.IGNORECASE,
            )
            roles = ["PETITIONER", "RESPONDENT"]
            for index, block in enumerate(blocks):
                role = roles[index] if index < len(roles) else "OTHER"
                cleaned = re.sub(r"\s+", " ", block)
                cleaned = re.sub(r"Advocates?\s+for.*$", "", cleaned, flags=re.IGNORECASE)
                names = re.split(r",|&|\band\b|\bwith\b", cleaned, flags=re.IGNORECASE)
                for raw_name in names:
                    name = raw_name.strip()
                    if not name or len(name) < 3 or any(re.match(p, name, re.IGNORECASE) for p in BAD_NAME_PATTERNS):
                        continue
                    title = ""
                    title_match = re.match(r"^(Mr\.|Ms\.|Mrs\.|Dr\.|Adv\.|Shri|Smt\.)\s*", name, re.IGNORECASE)
                    if title_match:
                        title = title_match.group(1)
                        name = name[len(title_match.group(0)):].strip()
                    designation = "Advocate"
                    if re.search(r"\b(?:Sr\.?\s*Adv\.?|Senior\s+Advocate)\b", name, re.IGNORECASE):
                        designation = "Senior Advocate"
                    elif re.search(r"\b(?:Standing\s+Counsel|ASC|APP|CGSC|SPC)\b", name, re.IGNORECASE):
                        designation = "Standing Counsel"
                    clean_name = re.sub(r"^(?:Mr\.|Ms\.|Mrs\.|Dr\.|Adv\.|Shri|Smt\.)\s*", "", name, flags=re.IGNORECASE).strip()
                    clean_name = re.sub(r",?\s*(?:Adv\.?|Senior\s+Advocate|Sr\.?\s*Adv\.?|Standing\s+Counsel|ASC|APP|CGSC)\.?$", "", clean_name, flags=re.IGNORECASE).strip()
                    if len(clean_name) >= 3 and clean_name.casefold() not in seen:
                        seen.add(clean_name.casefold())
                        advocates.append(
                            {
                                "name": clean_name,
                                "title": title or None,
                                "designation": designation,
                                "for_party_role": role,
                            }
                        )

        if not advocates:
            self.missing_fields.append("advocates")

        return advocates

    # --------------------------------------------------------
    # Legal provisions
    # --------------------------------------------------------

    def extract_acts_and_provisions(self) -> tuple[list[dict], list[dict]]:
        provisions = []
        acts = []
        detected_acts = set()

        for alias, full_name in ACT_ALIASES.items():
            if re.search(r"\b" + re.escape(alias) + r"\b", self.text, re.IGNORECASE):
                detected_acts.add(full_name)

        section_patterns = [
            r"(?:Sections?|u/s|Sec\.|S\.)\s*([\dA-Za-z,\-/ &]+)\s+(?:of\s+(?:the\s+)?)?([A-Za-z0-9\s.,&()\-]+?Act)\b",
            r"(?:Sections?|u/s|Sec\.|S\.)\s*([\dA-Za-z,\-/ &]+)\s+(IPC|I\.P\.C|CrPC|Cr\.P\.C|CPC|C\.P\.C|BNS|BNSS|BSA|NI Act)\b",
            r"(?:Rules?|u/r|R\.)\s*([\dA-Za-z,\-/ &]+)\s+(?:of\s+(?:the\s+)?)?([A-Za-z0-9\s.,&()\-]+?(?:Rules|Act))\b",
        ]

        seen_provisions = set()

        for pattern in section_patterns:
            matches = re.findall(pattern, self.text, re.IGNORECASE)
            for section_text, act_text in matches:
                act_key = clean_text(act_text).lower()
                act_name = ACT_ALIASES.get(act_key, clean_text(act_text)) or "General Statute"
                section_parts = re.split(r"[/,&\s]+", section_text)
                for section in section_parts:
                    section = section.strip()
                    if not section or not re.match(r"^\d+[A-Za-z]?$", section):
                        continue
                    pair = (act_name.casefold(), section.casefold())
                    if pair in seen_provisions:
                        continue
                    seen_provisions.add(pair)
                    provisions.append({"act_name": act_name, "section": section})
                    detected_acts.add(act_name)

        for act_name in sorted(detected_acts):
            if not any(act_name.casefold() == existing["act_name"].casefold() for existing in acts):
                acts.append({"act_name": act_name, "short_name": None})

        if not provisions:
            self.missing_fields.append("provisions")

        return acts, provisions

    # --------------------------------------------------------
    # Constitutional articles
    # --------------------------------------------------------

    def extract_articles(self) -> list[str]:
        articles = re.findall(
            r"\bArticle\s+(\d+[A-Z]?)\b",
            self.text,
            re.IGNORECASE,
        )

        return unique_preserve(
            [
                f"Article {value.upper()}"
                for value in articles
            ]
        )

    # --------------------------------------------------------
    # Outcome
    # --------------------------------------------------------

    def extract_outcome(self) -> tuple[str | None, str | None]:
        raw = clean_text(
            self.scraper_record.get("disposal_nature")
        )

        normalized = normalize_disposal(raw)

        if normalized:
            return normalized, raw

        text_lower = self.text.lower()

        if (
            "appeal is allowed" in text_lower
            or "petition is allowed" in text_lower
        ):
            return "ALLOWED", None

        if (
            "appeal stands dismissed" in text_lower
            or "appeal is dismissed" in text_lower
            or "petition is dismissed" in text_lower
        ):
            return "DISMISSED", None

        if (
            "application stands disposed of"
            in text_lower
            or "stands disposed of"
            in text_lower
            or "disposed of"
            in text_lower
        ):
            return "DISPOSED", None

        if "bail is granted" in text_lower:
            return "BAIL_GRANTED", None

        if "bail application is rejected" in text_lower:
            return "BAIL_REJECTED", None

        self.missing_fields.append("outcome")

        return None, None

    # --------------------------------------------------------
    # Reporter citations
    # --------------------------------------------------------

    # --------------------------------------------------------
    # Reporter citations & Reporting status
    # --------------------------------------------------------

    def extract_candidate_reporter_citations(self, text_segment: str) -> list[dict]:
        citations = []
        seen = set()

        # 1. Year (Volume) Reporter Page format, e.g. 2025 (1) KLT 123
        pat1 = r"\b(\d{4})\s*\(\s*(\d+)\s*\)\s*(KLT|KHC|SCC|SCALE|Scale|SCR|JT|ILR\s+Ker|ILR\s+Kerala)\s+(\d+)\b"
        for match in re.finditer(pat1, text_segment, re.IGNORECASE):
            year = int(match.group(1))
            volume = int(match.group(2))
            reporter_raw = match.group(3).upper()
            page = int(match.group(4))
            
            reporter = "KLT" if "KLT" in reporter_raw else ("KHC" if "KHC" in reporter_raw else ("ILR Ker" if "ILR" in reporter_raw else reporter_raw))
            full_cit = f"{year} ({volume}) {reporter} {page}"
            key = (reporter, year, volume, page)
            if key not in seen:
                seen.add(key)
                citations.append({
                    "reporter": reporter,
                    "citation": full_cit,
                    "year": year,
                    "volume": volume,
                    "page": page,
                })

        # 2. (Year) Volume Reporter Page format, e.g. (2025) 1 SCC 123
        pat2 = r"\(\s*(\d{4})\s*\)\s*(\d+)\s*(SCC|KLT|KHC|SCALE|Scale|SCR|JT)\s+(\d+)\b"
        for match in re.finditer(pat2, text_segment, re.IGNORECASE):
            year = int(match.group(1))
            volume = int(match.group(2))
            reporter_raw = match.group(3).upper()
            page = int(match.group(4))
            
            full_cit = f"({year}) {volume} {reporter_raw} {page}"
            key = (reporter_raw, year, volume, page)
            if key not in seen:
                seen.add(key)
                citations.append({
                    "reporter": reporter_raw,
                    "citation": full_cit,
                    "year": year,
                    "volume": volume,
                    "page": page,
                })

        # 3. AIR Year Reporter Page, e.g. AIR 2025 Ker 123 or AIR 2025 SC 123
        pat3 = r"\bAIR\s+(\d{4})\s+(SC|Ker|Kerala)\s+(\d+)\b"
        for match in re.finditer(pat3, text_segment, re.IGNORECASE):
            year = int(match.group(1))
            court_tag = match.group(2).title()
            page = int(match.group(3))
            reporter = "AIR SC" if court_tag.upper() == "SC" else "AIR Ker"
            full_cit = f"AIR {year} {court_tag} {page}"
            key = (reporter, year, None, page)
            if key not in seen:
                seen.add(key)
                citations.append({
                    "reporter": reporter,
                    "citation": full_cit,
                    "year": year,
                    "volume": None,
                    "page": page,
                })

        # 4. ILR Year Reporter Page, e.g. ILR 2025 Ker 123
        pat4 = r"\bILR\s+(\d{4})\s*(?:\(\s*(\d+)\s*\))?\s*(Ker|Kerala)\s+(\d+)\b"
        for match in re.finditer(pat4, text_segment, re.IGNORECASE):
            year = int(match.group(1))
            volume = int(match.group(2)) if match.group(2) else None
            court_tag = match.group(3).title()
            page = int(match.group(4))
            reporter = "ILR Ker"
            full_cit = f"ILR {year} {court_tag} {page}" if not volume else f"ILR {year} ({volume}) {court_tag} {page}"
            key = (reporter, year, volume, page)
            if key not in seen:
                seen.add(key)
                citations.append({
                    "reporter": reporter,
                    "citation": full_cit,
                    "year": year,
                    "volume": volume,
                    "page": page,
                })

        # 5. SCC OnLine / SCC Online format, e.g. 2025 SCC OnLine Ker 123
        pat5 = r"\b(\d{4})\s+SCC\s+OnLine\s+(Ker|Kerala|SC)\s+(\d+)\b"
        for match in re.finditer(pat5, text_segment, re.IGNORECASE):
            year = int(match.group(1))
            court_tag = match.group(2).upper()
            page = int(match.group(3))
            reporter = "SCC Online Ker" if "KER" in court_tag else "SCC Online SC"
            full_cit = f"{year} SCC OnLine {court_tag} {page}"
            key = (reporter, year, None, page)
            if key not in seen:
                seen.add(key)
                citations.append({
                    "reporter": reporter,
                    "citation": full_cit,
                    "year": year,
                    "volume": None,
                    "page": page,
                })

        # 6. Simple Year Reporter Page, e.g. 2025 KLT 123 or 2025 KHC 123
        pat6 = r"\b(\d{4})\s+(KLT|KHC|SCC|SCR|JT|Scale|SCALE)\s+(\d+)\b"
        for match in re.finditer(pat6, text_segment, re.IGNORECASE):
            year = int(match.group(1))
            reporter_raw = match.group(2).upper()
            page = int(match.group(3))
            full_cit = f"{year} {reporter_raw} {page}"
            key = (reporter_raw, year, None, page)
            if key not in seen:
                seen.add(key)
                citations.append({
                    "reporter": reporter_raw,
                    "citation": full_cit,
                    "year": year,
                    "volume": None,
                    "page": page,
                })

        return citations

    def validate_candidate_citation(self, citation: dict, header_text: str) -> bool:
        cit_str = citation.get("citation", "")
        if not cit_str:
            return False

        # 1. Explicit citation label anywhere in text
        if re.search(r"(?:Reported\s+in|Equivalent\s+Citation|Law\s+Report\s+Citation|Citation)\s*:?\s*" + re.escape(cit_str), self.text, re.IGNORECASE):
            return True

        # Extract party names & tokens
        petitioner = clean_text(self.scraper_record.get("petitioner"))
        respondent = clean_text(self.scraper_record.get("respondent"))
        party_name = clean_text(self.scraper_record.get("party_name"))

        if not petitioner and not respondent and party_name:
            parts = re.split(r"\s+(?:Vs|Versus)\s+", party_name, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                petitioner = clean_text(parts[0])
                respondent = clean_text(parts[1])
            else:
                petitioner = party_name

        case_num = clean_text(self.scraper_record.get("case_number"))
        cnr = clean_text(self.scraper_record.get("cnr") or self.scraper_record.get("diary_number"))

        stop_words = {"THE", "AND", "OTHERS", "ANR", "ORS", "STATE", "KERALA", "INDIA", "UNION", "HIGH", "COURT", "LIMITED", "LTD", "DEPT", "DEPARTMENT"}
        pet_tokens = [t.upper() for t in re.findall(r"\w+", petitioner) if len(t) > 2 and t.upper() not in stop_words]
        resp_tokens = [t.upper() for t in re.findall(r"\w+", respondent) if len(t) > 2 and t.upper() not in stop_words]

        num_digits = re.findall(r"\d+", case_num) if case_num else []

        # 2. Position & proximity check in header_text
        match_cit = re.search(re.escape(cit_str), header_text, re.IGNORECASE)
        if match_cit:
            pos = match_cit.start()
            window = header_text[max(0, pos - 350): min(len(header_text), pos + 350)].upper()

            if any(t in window for t in pet_tokens):
                return True
            if any(t in window for t in resp_tokens):
                return True
            if case_num and case_num.upper() in window:
                return True
            if cnr and cnr.upper() in window:
                return True
            if num_digits and len(num_digits) >= 2 and all(d in window for d in num_digits):
                return True

        return False

    def extract_reporting_status(self, source_tag: str = "HIGH_COURT_PDF") -> dict:
        header_end = 1200
        caption_match = re.search(
            r"\n\s*(?:BEFORE|CORAM|PRESENT|WP\(C\)|Crl\.A|C\.A\.|IN THE HIGH COURT|IN THE SUPREME COURT|JUDGMENT|ORDER)\b",
            self.text,
            re.IGNORECASE,
        )
        if caption_match and caption_match.start() > 100:
            header_end = min(1500, caption_match.start() + 200)

        header_text = self.text[:header_end]

        explicit_match = re.search(
            r"(?:Reported\s+in|Equivalent\s+Citation|Law\s+Report\s+Citation|Citation)\s*:?\s*([^\n]+)",
            self.text,
            re.IGNORECASE,
        )

        header_candidates = self.extract_candidate_reporter_citations(header_text)
        explicit_candidates = []
        if explicit_match:
            explicit_candidates = self.extract_candidate_reporter_citations(explicit_match.group(0))

        candidates = header_candidates + explicit_candidates

        validated_citations = []
        seen = set()
        for cand in candidates:
            cit_str = cand.get("citation")
            if cit_str and cit_str not in seen:
                if self.validate_candidate_citation(cand, header_text):
                    seen.add(cit_str)
                    validated_citations.append(cand)

        if not self.text or len(self.text.strip()) == 0:
            status = "UNKNOWN"
            is_reported = False
        elif len(validated_citations) > 0:
            status = "DETECTED"
            is_reported = True
        else:
            status = "NOT_DETECTED"
            is_reported = False

        return {
            "status": status,
            "is_reported": is_reported,
            "reporter_citations": validated_citations,
            "source": source_tag,
        }

    def extract_reporter_citations(self) -> list[dict]:
        status = self.extract_reporting_status(source_tag="HIGH_COURT_PDF")
        return status["reporter_citations"]

    # --------------------------------------------------------
    # Impugned order
    # --------------------------------------------------------

    def extract_impugned_order(self) -> dict:
        match = re.search(r"Exts?\.\s*P\d+\s+is\s+the\s+order\s+dated\s+([0-9]{1,2}[\./-][0-9]{1,2}[\./-][0-9]{4})", self.text, re.IGNORECASE)
        if not match:
            match = re.search(r"(?:Ext|Exts|Exhibit)\.?\s*P-?\d+.*?(?:dated|DATED)\s+([0-9]{1,2}[\./-][0-9]{1,2}[\./-][0-9]{4})", self.text, re.IGNORECASE)
        if not match:
            match = re.search(r"(?:G\.O\.|order|judgment)\s+(?:dated|DATED)\s+([0-9]{1,2}[\./-][0-9]{1,2}[\./-][0-9]{4})", self.text, re.IGNORECASE)

        if match:
            return {"date": normalize_date(match.group(1))}

        return {"date": None}

    # --------------------------------------------------------
    # Catchwords
    # --------------------------------------------------------

    def extract_catchwords(self) -> list[str]:
        words = self.text.lower()
        counts = Counter()

        for keyword in LEGAL_KEYWORDS:
            frequency = len(
                re.findall(
                    r"\b"
                    + re.escape(keyword)
                    + r"\b",
                    words,
                )
            )

            if frequency > 0:
                counts[keyword] = frequency

        return [
            keyword
            for keyword, _ in counts.most_common(10)
        ]

    def extract_keywords(self) -> list[str]:
        words = self.text.lower()
        counts = Counter()

        for keyword in LEGAL_KEYWORDS:
            frequency = len(
                re.findall(
                    r"\b" + re.escape(keyword) + r"\b",
                    words,
                )
            )

            if frequency > 0:
                counts[keyword] = frequency

        return [
            keyword
            for keyword, _ in counts.most_common(15)
        ]

    def extract_document_type(self) -> str:
        header_text = self.text[:1000]
        if re.search(r"\bO\s*R\s*D\s*E\s*R\b|\bORDER\b", header_text, re.IGNORECASE):
            return "ORDER"
        if re.search(r"\bJ\s*U\s*D\s*G\s*M\s*E\s*N\s*T\b|\bJUDGMENT\b", header_text, re.IGNORECASE):
            return "JUDGMENT"
        return "JUDGMENT"

    # --------------------------------------------------------
    # Subject matter
    # --------------------------------------------------------

    def extract_subject_matter(self) -> list[str]:
        words = self.text.lower()
        scores = {}
        MIN_SUBJECT_SCORE = 2

        for subject, keywords in SUBJECT_KEYWORDS.items():
            score = 0

            for keyword in keywords:
                score += len(
                    re.findall(
                        r"\b"
                        + re.escape(keyword)
                        + r"\b",
                        words,
                    )
                )

            if score >= MIN_SUBJECT_SCORE:
                scores[subject] = score

        if not scores:
            return []

        maximum = max(
            scores.values()
        )

        selected = [
            subject
            for subject, score in scores.items()
            if score >= maximum * 0.7 and score >= MIN_SUBJECT_SCORE
        ]

        return sorted(selected)

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    def generate_long_summary(self) -> str | None:
        header_marker = re.search(
            r"\n\s*(?:JUDGMENT|ORDER|ORAL JUDGMENT|O R D E R|J U D G M E N T)\s*\n",
            self.text,
            re.IGNORECASE,
        )

        body_text = (
            self.text[header_marker.end():]
            if header_marker
            else self.text
        )

        # Stop at APPENDIX or PETITIONER'S EXHIBITS section
        appendix_marker = re.search(r"\n\s*APPENDIX\b", body_text, re.IGNORECASE)
        if appendix_marker:
            body_text = body_text[:appendix_marker.start()]

        raw_paragraphs = [p.strip() for p in body_text.split("\n\n") if p.strip()]
        if not raw_paragraphs or len(raw_paragraphs) <= 1:
            raw_paragraphs = [p.strip() for p in body_text.split("\n") if len(p.strip()) > 30]

        filtered_paragraphs = []
        for p in raw_paragraphs:
            cleaned_p = clean_text(p)
            if len(cleaned_p) < 30:
                continue
            if re.match(
                r"^(?:CORAM|Through|versus|Date of Decision|CM APPL|THURSDAY|TUESDAY|MONDAY|WEDNESDAY|FRIDAY|SATURDAY|SUNDAY|Dated this|IN THE HIGH COURT|PRESENT|WP\(C\)|Sd/-|\d+\s*$)",
                cleaned_p,
                re.IGNORECASE,
            ):
                continue
            if "REPRESENTED BY" in cleaned_p.upper() or "POSTING" in cleaned_p.upper():
                continue
            filtered_paragraphs.append(cleaned_p)

        if filtered_paragraphs:
            long_summary = " ".join(filtered_paragraphs)
            if len(long_summary) > 2000:
                long_summary = long_summary[:2000].rsplit(" ", 1)[0] + "..."
            return long_summary

        self.missing_fields.append("summary")
        return None

    # --------------------------------------------------------
    # Document content
    # --------------------------------------------------------

    def extract_content(self) -> dict:
        paragraph_count = len(
            [
                paragraph
                for paragraph in self.text.split("\n")
                if paragraph.strip()
            ]
        )

        return {
            "paragraph_count": paragraph_count,
            "language": "en",
            "is_scanned": self.is_scanned,
        }

    # --------------------------------------------------------
    # Final PDF metadata
    # --------------------------------------------------------

    def extract(self) -> dict:
        cnr = self.extract_cnr()
        neutral_citation = self.extract_neutral_citation()
        case_number = self.extract_case_number()
        decision_date = self.extract_decision_date()
        pronounced_on = self.extract_pronounced_on()
        judges = self.extract_judges()
        parties = self.extract_parties()
        advocates = self.extract_advocates()
        acts, provisions = self.extract_acts_and_provisions()
        articles = self.extract_articles()
        disposal_nature, raw_disposal = self.extract_outcome()
        reporting_status = self.extract_reporting_status(source_tag="HIGH_COURT_PDF")
        reporter_citations = reporting_status["reporter_citations"]
        impugned_order = self.extract_impugned_order()
        catchwords = self.extract_catchwords()
        subject_matter = self.extract_subject_matter()
        summary = self.generate_long_summary()
        content = self.extract_content()

        # Confidence is based only on fields we actually attempt to extract.
        expected_fields = [
            "cnr",
            "neutral_citation",
            "case_number",
            "decision_date",
            "judges",
            "parties",
            "advocates",
            "provisions",
            "outcome",
            "summary",
        ]

        missing = set(
            self.missing_fields
        )

        extracted_count = sum(
            1
            for field in expected_fields
            if field not in missing
        )

        confidence = round(
            (
                extracted_count
                / len(expected_fields)
            ) * 100,
            2,
        )

        doc_type = self.extract_document_type()
        keywords = self.extract_keywords()

        return {
            "cnr": cnr,
            "neutral_citation": neutral_citation,
            "case_number": case_number,
            "document_type": doc_type,
            "dates": {
                "pronounced_on": pronounced_on,
            },
            "bench": {
                "strength": len(judges),
                "judges": judges,
            },
            "parties": parties,
            "advocates": advocates,
            "acts": acts,
            "provisions": provisions,
            "constitutional_articles": articles,
            "outcome": {
                "disposal_nature": disposal_nature,
                "raw_disposal_nature": raw_disposal,
            },
            "reporting": reporting_status,
            "impugned_order": impugned_order,
            "catchwords": catchwords,
            "keywords": keywords,
            "subject_matter": subject_matter,
            "content": {
                **content,
                "summary": summary,
            },
            "confidence_score": confidence,
            "missing_fields": sorted(
                set(self.missing_fields)
            ),
        }


# ============================================================
# PDF TEXT
# ============================================================

def is_pdf_scanned(document: fitz.Document, text: str) -> bool:
    if text and len(text.strip()) > 50:
        return False

    for page in document:
        if page.get_images(full=True):
            return True

    return False


def extract_pdf_text(pdf_path: Path) -> tuple[str, bool]:
    document = fitz.open(str(pdf_path))
    text_parts = []
    try:
        for page in document:
            text_parts.append(page.get_text())
        full_text = "\n".join(text_parts)
        is_scanned = is_pdf_scanned(document, full_text)
    finally:
        document.close()

    return full_text, is_scanned


# ============================================================
# FIND PDF
# ============================================================

def resolve_pdf_path(
    record: dict,
) -> Path | None:

    raw_path = record.get("pdf_path")

    if raw_path:
        path = Path(
            str(raw_path)
        )

        if path.exists():
            return path

        filename = path.name

        candidate = PDF_DIR / filename

        if candidate.exists():
            return candidate

    case_num = clean_text(record.get("case_number"))
    if case_num:
        safe_num = re.sub(r"[^\w\-]", "_", case_num)
        for pdf_file in PDF_DIR.glob("*.pdf"):
            if safe_num in pdf_file.name:
                return pdf_file

    cnr = clean_text(
        record.get("cnr")
    )

    if cnr:
        candidates = list(
            PDF_DIR.glob(
                f"*{cnr}*.pdf"
            )
        )

        if candidates:
            return candidates[0]

    return None


# ============================================================
# BUILD STANDARDIZED JSON
# ============================================================

def build_standardized_record(
    scraper_record: dict,
    pdf_metadata: dict,
) -> dict:

    court_name = clean_text(
        scraper_record.get("court")
    ) or "High Court of Kerala"

    bench_name = clean_text(
        scraper_record.get("bench")
    ) or "Kerala High Court"

    cnr = (
        pdf_metadata.get("cnr")
        or scraper_record.get("cnr")
    )

    cnr = (
        str(cnr).upper()
        if cnr
        else None
    )

    # Court ID derived from CNR when available.
    court_id = (
        cnr[:4]
        if cnr and len(cnr) >= 4
        else "KLHC"
    )

    # Existing PDF metadata may not have a court block.
    pdf_court = pdf_metadata.get(
        "court",
        {},
    )

    state_code = (
        pdf_court.get("state")
        if isinstance(pdf_court, dict)
        else None
    ) or "KL"

    bench_seat = (
        pdf_court.get("bench_seat")
        if isinstance(pdf_court, dict)
        else None
    ) or "Ernakulam"

    case_number = pdf_metadata.get(
        "case_number",
        {},
    )

    if not isinstance(case_number, dict):
        case_number = {}

    # If PDF parser couldn't get case number,
    # retain the scraper's case number as display.
    if not case_number.get("display"):
        case_number["display"] = clean_text(
            scraper_record.get("case_number")
        )

    registration_date = normalize_date(
        scraper_record.get(
            "registration_date"
        )
    )

    decision_date = normalize_date(
        scraper_record.get(
            "decision_date"
        )
    )

    if not decision_date:
        dates = pdf_metadata.get(
            "dates",
            {},
        )

        if isinstance(dates, dict):
            decision_date = normalize_date(
                dates.get("pronounced_on")
            )

    raw_disposal = (
        clean_text(
            scraper_record.get(
                "disposal_nature"
            )
        )
        or clean_text(
            (
                pdf_metadata.get(
                    "outcome",
                    {},
                )
                or {}
            ).get(
                "disposal_nature"
            )
        )
    )

    normalized_disposal = normalize_disposal(
        raw_disposal
    )

    # PDF metadata source information.
    pdf_meta_source = pdf_metadata.get(
        "source",
        {},
    )

    if not isinstance(
        pdf_meta_source,
        dict,
    ):
        pdf_meta_source = {}

    # Document content.
    document_content = pdf_metadata.get(
        "content",
        {},
    )

    if not isinstance(
        document_content,
        dict,
    ):
        document_content = {}

    summary = document_content.get(
        "summary"
    )

    # The old extractor's catchwords become our
    # extracted keywords. We do not invent new words.
    keywords = pdf_metadata.get(
        "catchwords",
        [],
    )

    if not isinstance(
        keywords,
        list,
    ):
        keywords = []

    keywords = unique_preserve(
        [
            str(keyword)
            for keyword in keywords
            if keyword
        ]
    )

    # Legal information.
    provisions = pdf_metadata.get(
        "provisions",
        [],
    )

    if not isinstance(
        provisions,
        list,
    ):
        provisions = []

    acts_map = {}
    for provision in provisions:
        if not isinstance(
            provision,
            dict,
        ):
            continue

        act_name = clean_text(
            provision.get("act_name")
        )

        if not act_name:
            continue

        key = act_name.casefold()

        if key not in acts_map:
            acts_map[key] = {
                "act_name": act_name,
                "short_name": None,
            }

    acts = list(
        acts_map.values()
    )

    return {
        "schema_version": SCHEMA_VERSION,

        "court": {
            "court_id": court_id,
            "name": court_name,
            "type": "HIGH_COURT",
            "state_code": state_code,
            "state_name": "Kerala",
            "bench": bench_name,
            "bench_seat": bench_seat,
        },

        "case": {
            "case_id": cnr,
            "cnr": cnr,

            "case_number": {
                "display": case_number.get(
                    "display"
                ),
                "type_code": case_number.get(
                    "type_code"
                ),
                "type_name": case_number.get(
                    "type_name"
                ),
                "number": case_number.get(
                    "number"
                ),
                "year": case_number.get(
                    "year"
                ),
            },

            "registration_date": registration_date,

            "decision": {
                "decision_date": decision_date,
                "pronounced_on": normalize_date(
                    (
                        pdf_metadata.get(
                            "dates",
                            {},
                        )
                        or {}
                    ).get(
                        "pronounced_on"
                    )
                ),
                "disposal_nature": normalized_disposal,
                "raw_disposal_nature": raw_disposal,
            },

            "neutral_citation": pdf_metadata.get(
                "neutral_citation"
            ),

            "parties": pdf_metadata.get(
                "parties",
                [],
            ) or [],

            "judges": (
                pdf_metadata.get(
                    "bench",
                    {},
                ) or {}
            ).get(
                "judges",
                []
            ) or [],

            "advocates": pdf_metadata.get(
                "advocates",
                [],
            ) or [],
        },

        "legal_information": {
            "acts": acts,
            "provisions": provisions,
            "constitutional_articles": (
                pdf_metadata.get(
                    "constitutional_articles",
                    [],
                )
                if isinstance(
                    pdf_metadata.get(
                        "constitutional_articles",
                        [],
                    ),
                    list,
                )
                else []
            ),
            "catchwords": keywords,
            "subject_matter": (
                pdf_metadata.get(
                    "subject_matter",
                    [],
                )
                if isinstance(
                    pdf_metadata.get(
                        "subject_matter",
                        [],
                    ),
                    list,
                )
                else []
            ),
        },

        "outcome": {
            "disposal_nature": normalized_disposal,
            "decision": normalized_disposal,
            "result_text": None,
        },

        "reporting": pdf_metadata.get(
            "reporting",
            {
                "status": "NOT_DETECTED",
                "is_reported": False,
                "reporter_citations": [],
                "source": "HIGH_COURT_PDF",
            },
        ),

        "citations": {
            "neutral_citation": pdf_metadata.get(
                "neutral_citation"
            ),
            "impugned_order": (
                pdf_metadata.get(
                    "impugned_order",
                    {},
                )
                if isinstance(
                    pdf_metadata.get(
                        "impugned_order",
                        {},
                    ),
                    dict,
                )
                else {}
            ),
        },

        "document": {
            "document_type": pdf_metadata.get(
                "document_type",
                "JUDGMENT"
            ),
            "language": document_content.get(
                "language",
                "en",
            ),
            "is_scanned": document_content.get(
                "is_scanned",
                False,
            ),
            "paragraph_count": document_content.get(
                "paragraph_count",
                0,
            ),
            "summary": summary,
            "keywords": (
                pdf_metadata.get(
                    "keywords",
                    [],
                )
                if isinstance(
                    pdf_metadata.get(
                        "keywords",
                        [],
                    ),
                    list,
                )
                else []
            ),
        },

        "processing": {
            "status": "SUCCESS" if pdf_metadata else "FAILED",
            "pdf_found": pdf_metadata is not None,
            "text_extracted": bool(pdf_metadata and pdf_metadata.get("content")),
            "error": None if pdf_metadata else "PDF file not found",
        },

        "source": {
            "scraper": {
                "serial_no": clean_text(
                    scraper_record.get(
                        "serial_no"
                    )
                ),
                "source_page": clean_text(
                    scraper_record.get(
                        "source_page"
                    )
                ),
                "pdf_source": clean_text(
                    scraper_record.get(
                        "pdf_source"
                    )
                ),
            },

            "pdf": {
                "pdf_url": clean_text(
                    scraper_record.get(
                        "pdf_url"
                    )
                ),
                "pdf_path": clean_text(
                    scraper_record.get(
                        "pdf_path"
                    )
                ),
                "original_pdf_path": clean_text(
                    scraper_record.get(
                        "pdf_path"
                    )
                ),
            },

            "metadata_extraction": {
                "parsed_at": pdf_meta_source.get(
                    "parsed_at"
                ) or datetime.now().isoformat(),
                "extractor_version": (
                    EXTRACTOR_VERSION
                ),
            },
        },

        "quality": {
            "confidence_score": pdf_metadata.get(
                "confidence_score",
                0,
            ),
            "missing_fields": (
                pdf_metadata.get(
                    "missing_fields",
                    [],
                )
                if isinstance(
                    pdf_metadata.get(
                        "missing_fields",
                        [],
                    ),
                    list,
                )
                else []
            ),
        },
    }


# ============================================================
# MAIN PROCESS
# ============================================================

def process_case(
    scraper_record: dict,
) -> dict | None:

    pdf_path = resolve_pdf_path(
        scraper_record
    )

    if pdf_path is None:
        print(
            "  [SKIP] PDF not found."
        )
        return None

    print(
        f"  PDF: {pdf_path.name}"
    )

    try:
        text, is_scanned = extract_pdf_text(
            pdf_path
        )

        if not text.strip():
            print(
                "  [WARNING] PDF text is empty."
            )

        extractor = MetadataExtractor(
            raw_text=text,
            pdf_path=str(pdf_path),
            scraper_record=scraper_record,
            is_scanned=is_scanned,
        )

        pdf_metadata = extractor.extract()

        standardized = build_standardized_record(
            scraper_record=scraper_record,
            pdf_metadata=pdf_metadata,
        )

        return standardized

    except Exception as error:
        print(
            f"  [ERROR] {error}"
        )

        return None


def main():
    print("=" * 80)
    print("KERALA HIGH COURT")
    print("PDF + SCRAPER JSON METADATA EXTRACTOR")
    print("=" * 80)

    if not INPUT_JSON.exists():
        print(
            f"\nInput JSON not found:\n{INPUT_JSON}"
        )
        return

    if not PDF_DIR.exists():
        print(
            f"\nPDF folder not found:\n{PDF_DIR}"
        )
        return

    with open(
        INPUT_JSON,
        "r",
        encoding="utf-8",
    ) as file:
        scraper_data = json.load(
            file
        )

    if not isinstance(
        scraper_data,
        dict,
    ):
        print(
            "\nERROR: Expected scraper JSON "
            "format: {\"Kerala\": [ ... ]}"
        )
        return

    all_records = []

    total_records = 0
    skipped_records = 0

    for court_name, records in scraper_data.items():

        print(
            f"\nCourt group: {court_name}"
        )

        if not isinstance(
            records,
            list,
        ):
            continue

        for index, record in enumerate(
            records,
            start=1,
        ):
            if not isinstance(
                record,
                dict,
            ):
                continue

            total_records += 1

            print(
                "\n"
                + "-" * 70
            )
            print(
                f"Record {index}/{len(records)}"
            )

            print(
                f"Case: "
                f"{record.get('case_number')}"
            )

            print(
                f"CNR: "
                f"{record.get('cnr')}"
            )

            standardized = process_case(
                record
            )

            if standardized is None:
                skipped_records += 1
                continue

            all_records.append(
                standardized
            )

            print(
                "  [OK] Metadata created"
            )

            print(
                f"  Confidence: "
                f"{standardized['quality']['confidence_score']}%"
            )

    output = {
        "Kerala": all_records
    }

    with open(
        OUTPUT_JSON,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            output,
            file,
            indent=4,
            ensure_ascii=False,
        )

    print(
        "\n"
        + "=" * 80
    )
    print(
        "COMPLETED"
    )
    print(
        "=" * 80
    )

    print(
        f"Total scraper records : {total_records}"
    )

    print(
        f"Standardized records  : {len(all_records)}"
    )

    print(
        f"Skipped records       : {skipped_records}"
    )

    print(
        f"Output JSON           : {OUTPUT_JSON}"
    )


if __name__ == "__main__":
    main()