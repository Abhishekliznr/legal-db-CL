"""
CSS selectors / regex patterns for judgments.ecourts.gov.in.

Kept separate from adapter.py so a selector-drift fix (the portal changes
an element id) touches one small file instead of the control-flow logic.
Every High Court goes through this exact same page structure — only the
`state_code`/`bench_code` values passed into the adapter differ per court.
"""

import re
from urllib.parse import unquote

BASE_URL = "https://judgments.ecourts.gov.in"
SEARCH_URL = f"{BASE_URL}/pdfsearch/"

# Initial keyword search step
SEARCH_KEYWORD_INPUT = "#search_text"
SEARCH_ALL_WORDS_RADIO = "#inlineRadio3"
SEARCH_CAPTCHA_INPUT = "#captcha"
SEARCH_CAPTCHA_IMAGE = "#captcha_image"
SEARCH_SUBMIT_BUTTON = "#main_search"

# Court selection step
COURT_MENU_TOGGLE = "a.nav-link.dropdown-toggle"
STATE_SELECT = "#state_code, select[name='state_code']"
BENCH_SELECT = "#dist_code, select[name='dist_code']"

# Decision date step
DATE_MENU_TOGGLE = "a.nav-link.dropdown-toggle"
CUSTOM_DATE_RADIO = "#exampleRadios5"
FROM_DATE_INPUT = "#from_date"
TO_DATE_INPUT = "#to_date"
FINAL_SEARCH_BUTTON = "button[onclick*='get_details_searchclick']:visible"

# Results
RESULT_ROWS = "#report_body tr"
NEXT_PAGE_BUTTON = "#example_pdf_next"

# PDF capture / session recovery
PDF_CAPTCHA_INPUTS = (
    "#captchapdf, input[name='captchapdf'], input[placeholder*='captcha' i], "
    "input[name*='captcha' i], input[id*='captcha' i], #pdf_captcha, #modal_captcha, #captcha"
)
PDF_CAPTCHA_IMAGES = (
    "#captcha_image_pdf, #viewFiles-body img[src*='securimage'], "
    ".modal img[src*='captcha' i], img[src*='captcha' i], img[id*='captcha' i]"
)
MODAL_CLOSE_BUTTONS = "#modal_close, #viewFiles .btn-close, .modal.show .btn-close, button[data-bs-dismiss='modal']"

_ROW_JUDGE_PATTERN = re.compile(
    r"Judge\s*:\s*(.*?)(?:No\.\s*\d+\s+Supplementary|No\.\s*\d+\s+Regular|CNR\s*:|$)", re.IGNORECASE
)
_ROW_CNR_PATTERN = re.compile(r"CNR\s*:\s*([A-Z0-9]+)", re.IGNORECASE)
_ROW_REGISTRATION_DATE_PATTERN = re.compile(r"Date of registration\s*:\s*([0-9-]+)", re.IGNORECASE)
_ROW_DECISION_DATE_PATTERN = re.compile(r"Decision Date\s*:\s*([0-9-]+)", re.IGNORECASE)
_ROW_DISPOSAL_PATTERN = re.compile(r"Disposal Nature\s*:\s*(.*?)(?:Court\s*:|$)", re.IGNORECASE)
_PDF_ONCLICK_PATTERN = re.compile(r"open_pdf\s*\(\s*['\"][^'\"]*['\"]\s*,\s*['\"][^'\"]*['\"]\s*,\s*['\"]([^'\"]+)['\"]")


def parse_pdf_source(onclick_attr: str) -> "str | None":
    if not onclick_attr:
        return None
    match = _PDF_ONCLICK_PATTERN.search(onclick_attr)
    if not match:
        return None
    return unquote(match.group(1).replace("&amp;", "&"))


def parse_row_description(description: str) -> dict:
    """Pulls judge/CNR/registration-date/decision-date/disposal-nature out of a result row's free-text description column."""
    def _search(pattern):
        match = pattern.search(description or "")
        return match.group(1).strip() if match else None

    return {
        "judge": _search(_ROW_JUDGE_PATTERN),
        "cnr": _search(_ROW_CNR_PATTERN),
        "registration_date": _search(_ROW_REGISTRATION_DATE_PATTERN),
        "decision_date": _search(_ROW_DECISION_DATE_PATTERN),
        "disposal_nature": _search(_ROW_DISPOSAL_PATTERN),
    }
