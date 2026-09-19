"""
https://mphc.gov.in/case-status — the case_type name -> numeric code the
site's own `#case_type` <select> expects, and the parser for the case-detail
HTML that same page's own POST returns directly (no separate AJAX/modal
round-trip for this flow — confirmed from a real saved response for
CRA 6641/2024).

CASE_TYPE_CODES was read directly off that page's `#case_type` <select>
options (confirmed live, not guessed) — this is the exact set the portal
offers as of the saved page; a case_type ILRS emits that isn't a key here
is a real gap to fill in once seen, not a bug in this table.
"""

import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

from normalization.judges import clean_judge_name

CASE_TYPE_CODES: Dict[str, str] = {
    "AA": "63", "AC": "64", "AR": "65", "ARBA": "61", "ARBC": "60",
    "CEA": "74", "CER": "27", "CESR": "30", "COMA": "69", "COMP": "21",
    "COMPA": "31", "CONA": "66", "CONC": "67", "CONCR": "73", "CONT": "15",
    "CONTR": "54", "CR": "19", "CRA": "51", "CRR": "53", "CRRE": "55",
    "CRRF": "71", "CRRFC": "72", "CS": "34", "EP": "20", "FA": "13",
    "FEMA": "75", "ITA": "26", "ITR": "22", "LPA": "17", "MA": "12",
    "MACE": "35", "MACOM": "59", "MACTR": "62", "MAIT": "28", "MAVAT": "58",
    "MCC": "18", "MCOMA": "70", "MCRC": "52", "MP": "16", "MSA": "90",
    "OTA": "79", "RP": "68", "SA": "14", "STR": "29", "TR": "76",
    "VATA": "77", "WA": "57", "WP": "11", "WPS": "32", "WTA": "78",
    "WTR": "23",
}

# Establishment/bench selector (https://mphc.gov.in/bench/select, POST
# bench_code) confirmed off the saved page's own options. Keyed by the
# bench name exactly as ILRS itself renders it in a case's full case
# number ("<Bench>/<Type>/<Number>/<Year>", e.g. "Jabalpur/WP/16962/2018",
# confirmed live) -- adapter.py reads the bench name straight off that
# parsed field and looks it up here, no derivation logic needed.
BENCH_CODES: Dict[str, str] = {
    "Jabalpur": "1",
    "Indore": "2",
    "Gwalior": "3",
}


def resolve_case_type_code(case_type: str) -> Optional[str]:
    """None if `case_type` (whatever string ILRS itself uses) doesn't match one of CASE_TYPE_CODES' keys exactly -- see adapters/high_courts/mp/extraction.py for the ILRS-side normalization that runs before this lookup."""
    return CASE_TYPE_CODES.get(case_type.strip().upper())


# ---------------------------------------------------------------------
# case-status result page parsing
# ---------------------------------------------------------------------

# "NAME[P-1] [3258/1996]" / "ADVOCATE GENERAL [7777/2014]" (no role tag on
# some entries, e.g. Advocate General) -- role tag is optional, enrollment
# no./year is not.
_ADVOCATE_RE = re.compile(
    r"^(?P<name>.+?)\s*(?:\[(?P<role>[A-Z]-\d+)\])?\s*\[(?P<enrollment_no>\d+)\s*/\s*(?P<enrollment_year>\d{4})\]\s*$"
)

# "231 - Code of Criminal Procedure (Section - 374(2))" -- repeated,
# possibly-duplicated lines separated by <br>, confirmed from the real
# saved page (the same line appeared six times for one case -- a source
# data quirk to dedupe against, not a parsing bug).
_ACT_LINE_RE = re.compile(
    r"^(?P<act_code>\S+)\s*-\s*(?P<act_name>.+?)\s*\(Section\s*-\s*(?P<sections>.+?)\)\s*$"
)

# "CRIMINAL APPEAL (CRA) 6641/2024 Registered On 30-05-2024 CNR: MPHC010342172024"
# -- confirmed off the real saved case-status page's own "Case No." cell
# (adapters/high_courts/mp/references/mp.html): case type name, its short
# code in parens, number/year, then the registration date and CNR appended
# to the SAME cell with no separate <tr> of their own. Registered On/CNR
# are optional groups -- not confirmed present on every case-status
# response (e.g. a case with no CNR assigned yet).
_CASE_NO_CELL_RE = re.compile(
    r"^(?P<case_type_name>.+?)\s*\((?P<case_type_code>[A-Z]+)\)\s*(?P<case_no>\d+)\s*/\s*(?P<year>\d{4})"
    r"(?:\s*Registered On\s*(?P<registered_on>\d{2}-\d{2}-\d{4}))?"
    r"(?:\s*CNR\s*:\s*(?P<cnr>\S+))?",
    re.IGNORECASE,
)

# The "Category" row is a DIFFERENT field from "Case No." above -- a
# subject-matter tag, not the case type -- e.g. "CRIMINAL LAW &
# PROCEDURE-12100<br/>Code of Criminal Procedure, 1973-12102<br/>SECTION
# 389" (confirmed live: three <br>-separated lines, each "<name>-<code>").
# Only the first line's name (before its own "-<code>" suffix) is taken, as
# the coarse subject tag -- the remaining lines are the act/section this
# case falls under, already covered separately by the Act row.
_CATEGORY_SUBJECT_RE = re.compile(r"^(?P<subject>.+?)\s*-\s*\d+")

# "10-11-2025 [ HON'BLE SHRI JUSTICE RAJENDRA KUMAR VANI ]" (confirmed live,
# the "Last Listed On" cell) -- judge name(s) sit inside the [ ] bracket. A
# Division Bench case is expected to list more than one "HON'BLE..."
# segment inside that same bracket (unconfirmed live -- no real Division
# Bench example was captured -- so this splits defensively on every
# "HON'BLE" occurrence, same approach as Supreme Court's own bench-cell
# splitting in adapters/supreme_court/extraction.py, rather than assuming
# a single name).
_BRACKETED_JUDGES_RE = re.compile(r"\[(.+?)\]")
_JUDGE_NAME_SPLIT_RE = re.compile(r"(?=HON'?BLE)", re.IGNORECASE)


def _parse_last_listed_judges(text: Optional[str]) -> List[str]:
    if not text:
        return []
    match = _BRACKETED_JUDGES_RE.search(text)
    if not match:
        return []
    names = [clean_judge_name(segment) for segment in _JUDGE_NAME_SPLIT_RE.split(match.group(1))]
    return [name for name in names if name and len(name) > 3]


def parse_advocate(text: str) -> Optional[Dict[str, Any]]:
    match = _ADVOCATE_RE.match(text.strip())
    if not match:
        return None
    return {
        "name": re.sub(r"\s+", " ", match.group("name")).strip(),
        "role": match.group("role"),
        "enrollment_no": match.group("enrollment_no"),
        "enrollment_year": int(match.group("enrollment_year")),
    }


def _cell_text(td) -> str:
    return re.sub(r"\s+", " ", td.get_text(" ", strip=True)).strip() if td else ""


def _parse_advocate_cell(td) -> List[Dict[str, Any]]:
    if td is None:
        return []
    advocates = []
    for span in td.select("span.fw-semibold"):
        parsed = parse_advocate(span.get_text(" ", strip=True))
        if parsed:
            advocates.append(parsed)
    return advocates


def _parse_party_cell(td) -> List[Dict[str, Optional[str]]]:
    if td is None:
        return []
    parties = []
    for block in td.select("div.d-flex.align-items-start"):
        lines = [line.get_text(" ", strip=True) for line in block.select("div.small > div")]
        if not lines:
            continue
        parties.append({
            "name": lines[0] if len(lines) > 0 else None,
            "relation": lines[1] if len(lines) > 1 else None,
            "address": lines[2] if len(lines) > 2 else None,
        })
    return parties


def _parse_act_lines(raw_html: str) -> List[Dict[str, Any]]:
    seen = set()
    acts = []
    for line in raw_html.split("<br"):
        text = re.sub(r"\s+", " ", BeautifulSoup(line, "html.parser").get_text(" ", strip=True)).strip()
        if not text:
            continue
        match = _ACT_LINE_RE.match(text)
        if not match:
            continue
        key = (match.group("act_code"), match.group("act_name").strip(), match.group("sections").strip())
        if key in seen:
            continue
        seen.add(key)
        acts.append({
            "act_code": key[0],
            "act_name": key[1],
            "sections": [s.strip() for s in re.split(r"[/,]", key[2]) if s.strip()],
        })
    return acts


def parse_case_details(html: str) -> Dict[str, Any]:
    """
    Parses the `.case-details-table` on a case-status result page (the
    #home tab) into a plain dict. Returns {} if the table isn't present at
    all (e.g. "no matching case" response) -- callers must treat that as
    "case not found", not as a parsing failure.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.case-details-table")
    if table is None:
        return {}

    rows: Dict[str, Any] = {}
    for tr in table.select("tr"):
        cells = tr.find_all("td")
        if len(cells) != 2:
            continue
        label = _cell_text(cells[0]).strip(": ").lower()
        if not label:
            continue
        rows[label] = cells[1]

    def text_of(label: str) -> Optional[str]:
        td = rows.get(label)
        return _cell_text(td) or None

    case_no_line = text_of("case no.")
    case_no_match = _CASE_NO_CELL_RE.match(case_no_line) if case_no_line else None

    category_line = text_of("category")
    category_match = _CATEGORY_SUBJECT_RE.match(category_line) if category_line else None

    return {
        "case_no_line": case_no_line,
        "case_type_name": case_no_match.group("case_type_name").strip() if case_no_match else None,
        "case_type_code": case_no_match.group("case_type_code") if case_no_match else None,
        "registered_on": case_no_match.group("registered_on") if case_no_match else None,
        "cnr": case_no_match.group("cnr") if case_no_match else None,
        "status_line": text_of("status"),
        "disposition_type": text_of("disp.type"),
        "bench_type": text_of("bench"),
        "category": category_line,
        "subject_category": category_match.group("subject").strip() if category_match else None,
        "judges": _parse_last_listed_judges(text_of("last listed on")),
        "acts": _parse_act_lines(str(rows["act"])) if "act" in rows else [],
        "petitioners": _parse_party_cell(rows.get("petitioner(s)")),
        "respondents": _parse_party_cell(rows.get("respondent(s)")),
        "petitioner_advocates": _parse_advocate_cell(rows.get("petitioner advocate(s)")),
        "respondent_advocates": _parse_advocate_cell(rows.get("respondent. advocate(s)")),
        "u_section": text_of("u/section"),
    }
