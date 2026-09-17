"""
portal.mphc.gov.in/ilrs (Indian Law Reports — Madhya Pradesh) discovery —
the "Search By ILR Details" tab specifically (there are several other
search tabs on this same page: free text, headnote, act & section, case
no., judge name, party name — none of those are used here).

Confirmed live against the real site (a full run: real captcha solved,
real search submitted, a real result clicked through to its detail pane):
- `#custom-tabs-five-ILR-tab` must be clicked first -- the page loads with
  the "Free Text" tab active (Bootstrap pills); "Search By ILR Details" is
  a separate, initially-hidden pane whose own fields/Search button aren't
  interactable until its pill is activated.
- `#lst_year` (a select2-enhanced <select name="lst_year">) is the ILR year
  field; `volno`/`pageno`/`part` are separate, independently-optional
  refinement fields on the SAME tab — leaving them blank returns every
  reported judgment for that year, which is what we want. select2 hides
  the native element, so it's set via JS + a dispatched 'change' event
  (Playwright's select_option() times out waiting for it to become
  "visible", since select2 deliberately keeps it hidden).
- Its own "Search" button is `input[onclick*='validate(2)']` (this page
  reuses id="search" across every tab, so selecting on the onclick's
  `validate(N)` call is the only reliable way to hit the right one).
- Clicking Search opens a shared captcha modal (#modal-default): a
  distorted-image captcha (#captcha, refreshed by #reloadCaptcha) +
  #securityCode input + #submitcap button. #submitcap's own click handler
  validates the code via an AJAX call and, on success, fires the actual
  search AJAX call itself and closes the modal — this module only needs to
  drive the DOM (fill/click/wait), never replicate those AJAX calls by hand.
  ddddocr solved the real captcha image on the first live attempt.
- The search response lands in #content as a raw XMLHttpRequest (not
  something Playwright's networkidle reliably tracks) -- the site's own JS
  sets a loading.gif placeholder the instant the request fires, so the
  real wait condition is "the placeholder is gone", not "#content exists".
- Each result row is a `<tr>` whose case-number link carries
  `onclick="get_details(fn, ilr, yr, srch, opt, rad, innr_srch)"` -- 582
  real rows matched `#content [onclick*='get_details']` for ILR year 2024.
  Clicking one renders the full case detail into #third (confirmed: a real
  6.6KB detail pane, parsed by `_parse_detail_pane` below), including the
  FULL case number as "<Bench>/<CaseType>/<Number>/<Year>" (e.g.
  "Jabalpur/WP/16962/2018") -- this is also the answer to "how do you know
  which Establishment a case belongs to" (case_status.py's BENCH_CODES),
  no separate derivation needed: the bench name is right there, plain text.
- Confirmed real lag between decision date and ILR publication year in the
  one example pulled live: decided 10 August 2023, published under ILR
  year 2024. The requested year is matched against each row's own decision
  date (not the ILR year searched), exactly as originally specified --
  this means a single ILR-year search can legitimately yield few or zero
  rows whose OWN decision date matches that same year, since most of a
  given ILR year's publications were decided the year before. Confirmed
  intentional (not a bug to route around) -- flagging in case broader
  year coverage is wanted later (e.g. also searching year+1).
"""

import logging
import re
from typing import Any, Dict, List

from bs4 import BeautifulSoup

from adapters.captcha_ocr import solve_captcha_image
from adapters.high_courts.mp.extraction import parse_ilrs_date

logger = logging.getLogger("scraper_backend_v2.mp_ilrs")

ILRS_URL = "https://portal.mphc.gov.in/ilrs/index.php"

MAX_CAPTCHA_RETRIES = 20
_SEARCH_BUTTON_SELECTOR = "input[onclick*='validate(2)']"


def _captcha_error_visible(page) -> bool:
    try:
        return "incorrect" in page.locator("#msg").inner_text().lower()
    except Exception:
        return False


def _solve_and_submit_captcha(page) -> bool:
    """Drives #modal-default to completion. Returns True once the site's own JS has accepted the code and closed the modal (a real search request is then already in flight/done), False if every retry was exhausted."""
    for attempt in range(MAX_CAPTCHA_RETRIES):
        captcha_image = page.locator("#captcha")
        try:
            captcha_image.wait_for(state="visible", timeout=10000)
        except Exception:
            return False

        solved = solve_captcha_image(captcha_image.screenshot())
        if not solved:
            page.locator("#reloadCaptcha").click()
            page.wait_for_timeout(800)
            continue

        page.locator("#securityCode").fill(solved)
        page.locator("#submitcap").click()
        page.wait_for_timeout(1500)

        if not page.locator("#modal-default").is_visible():
            return True  # modal closed -- code accepted, search already fired
        if _captcha_error_visible(page):
            logger.info("[MP ILRS] captcha attempt %d/%d rejected, retrying", attempt + 1, MAX_CAPTCHA_RETRIES)
            page.wait_for_timeout(500)
            continue

    return False


def _select_ilr_year(page, year: int) -> None:
    """
    #lst_year is a select2-enhanced <select> -- select2 hides the native
    element (aria-hidden, select2-hidden-accessible) and renders its own
    visible dropdown UI in front of it, so Playwright's select_option()
    times out waiting for the (intentionally hidden) native element to
    become visible. Setting the value via JS and dispatching 'change' is
    what select2 itself listens for to sync its own display, confirmed
    against the real live site.
    """
    page.eval_on_selector(
        "#lst_year",
        "(el, value) => { el.value = value; el.dispatchEvent(new Event('change', {bubbles: true})); }",
        str(year),
    )


def _read_result_rows(page) -> List:
    """Every result row's case-number link inside #content -- confirmed live (582/582 real rows matched for ILR year 2024)."""
    return page.locator("#content [onclick*='get_details']").all()


# "Jabalpur/WP/16962/2018" -- confirmed live, the one place the bench name
# is available as plain text (see module docstring for why this replaces a
# separate "derive bench from case number" step entirely).
_CASE_LINE_RE = re.compile(r"^\s*(?P<bench>[A-Za-z]+)/(?P<case_type>[A-Za-z]+)/(?P<case_no>\d+)/(?P<year>\d{4})\s*$")


def _parse_detail_pane(html: str) -> Dict[str, Any]:
    """
    Parses #third's rendered case-detail HTML (confirmed live against a
    real example: Jabalpur/WP/16962/2018) into {bench, case_type, case_no,
    registration_year, neutral_citation, ilr_citation, bench_type, judges,
    decision_date, petitioners, respondents, headnote}. Returns {} if the
    expected "<Bench>/<Type>/<Number>/<Year>" line isn't found at all --
    callers must treat that as "couldn't parse this one", not as "this
    case doesn't exist".
    """
    soup = BeautifulSoup(html, "html.parser")

    case_line = None
    for p in soup.select("div.card-header p"):
        match = _CASE_LINE_RE.match(p.get_text(strip=True))
        if match:
            case_line = match
            break
    if case_line is None:
        return {}

    result: Dict[str, Any] = {
        "bench": case_line.group("bench"),
        "case_type": case_line.group("case_type"),
        "case_no": case_line.group("case_no"),
        "registration_year": case_line.group("year"),
    }

    full_text = soup.get_text("\n", strip=True)
    citation_match = re.search(r"Neutral Citation\s*-\s*([^\n]+)", full_text)
    if citation_match:
        result["neutral_citation"] = citation_match.group(1).strip()
    published_match = re.search(r"Published in\s*-\s*([^\n]+)", full_text)
    if published_match:
        result["ilr_citation"] = published_match.group(1).strip()
    bench_type_match = re.search(r"\(\s*(Single|Division)\s+Bench\s*\)", full_text)
    if bench_type_match:
        result["bench_type"] = f"{bench_type_match.group(1)} Bench"
    date_match = re.search(r"Decision date-\s*([^\n]+)", full_text)
    if date_match:
        result["decision_date"] = date_match.group(1).strip()

    # A Division Bench's two judges came back concatenated with NO
    # separator at all in one real example ("Mr. Justice Rohit AryaMr.
    # Justice Avanindra Kumar Singh") -- confirmed live: both names sit in
    # what get_text() treats as one unbroken run of letters (no <br>/block
    # boundary for BeautifulSoup to insert whitespace at), so there's no
    # \b word boundary between "Arya" and "Mr" to anchor on either -- the
    # lookahead below matches "Mr./Ms. Justice" without requiring one.
    judges: List[str] = []
    for span in soup.select("div.card-header span.info-box-text.text-left"):
        text = span.get_text(" ", strip=True)
        if not text:
            continue
        for name in re.split(r"(?=Mr\.\s+Justice\b|Ms\.\s+Justice\b)", text):
            name = name.strip()
            if name:
                judges.append(name)
    if judges:
        result["judges"] = judges

    parties = [span.get_text(strip=True) for span in soup.select("div.card-body span.info-box-text")]
    # Alternates name, role, name, role, ... ("A.S. Patel", "... Petitioner", "State Of M.P. & Ors.", "... Respondent")
    petitioners, respondents = [], []
    for i in range(0, len(parties) - 1, 2):
        name, role = parties[i], parties[i + 1].lower()
        if "petitioner" in role:
            petitioners.append(name)
        elif "respondent" in role:
            respondents.append(name)
    if petitioners:
        result["petitioners"] = petitioners
    if respondents:
        result["respondents"] = respondents

    headnote_p = soup.select_one("div.card-body p.info-box-text")
    if headnote_p:
        headnote = headnote_p.get_text(" ", strip=True)
        if headnote:
            result["headnote"] = headnote

    return result


def discover_candidates(page, year: int) -> List[Dict[str, Any]]:
    """
    Runs the full ILR-year search for `year` and returns one dict per
    reported case, whatever _parse_detail_pane found: {bench, case_type,
    case_no, registration_year, neutral_citation, ilr_citation, bench_type,
    judges, decision_date, petitioners, respondents, headnote} -- already
    filtered to rows whose own decision date falls in `year` (see module
    docstring: the ILR year searched is a publication-year grouping, not a
    strict per-row filter, and can legitimately differ from a row's own
    decision year). Returns [] if the captcha couldn't be solved after
    MAX_CAPTCHA_RETRIES attempts (logged, not raised — an empty discovery
    result is a valid, if unfortunate, batch outcome, same as any other
    adapter's captcha-exhausted path).
    """
    page.goto(ILRS_URL, wait_until="domcontentloaded", timeout=60000)
    # The page loads with the "Free Text" tab active (Bootstrap pills) --
    # "Search By ILR Details" is a separate, initially-hidden tab-pane
    # (#custom-tabs-five-ILR) whose own fields/Search button aren't
    # interactable until its nav pill is activated. Confirmed live: without
    # this, #lst_year/the Search button are present in the DOM but not
    # visible, and Playwright times out waiting for them.
    page.locator("#custom-tabs-five-ILR-tab").click()
    _select_ilr_year(page, year)
    page.locator(_SEARCH_BUTTON_SELECTOR).first.click()

    try:
        page.locator("#modal-default").wait_for(state="visible", timeout=10000)
    except Exception:
        logger.warning("[MP ILRS] captcha modal never appeared after searching year=%s", year)
        return []

    if not _solve_and_submit_captcha(page):
        logger.warning("[MP ILRS] could not solve ILRS captcha after %d attempts for year=%s", MAX_CAPTCHA_RETRIES, year)
        return []

    # #content is present in the DOM from page load onward (this is the
    # raw XMLHttpRequest's target, not something Playwright's networkidle
    # reliably tracks) -- get_records()'s own JS sets it to a loading.gif
    # placeholder the instant the search request fires, then overwrites it
    # once the response arrives. Waiting for "attached" resolves instantly
    # against the placeholder itself; poll until that placeholder is gone.
    try:
        page.wait_for_function(
            "() => { const el = document.getElementById('content'); "
            "return el && !el.innerHTML.includes('loading1.gif'); }",
            timeout=30000,
        )
    except Exception:
        logger.warning("[MP ILRS] #content still showing the loading placeholder after 30s for year=%s", year)

    candidates: List[Dict[str, Any]] = []
    for row in _read_result_rows(page):
        try:
            row.click()
            page.wait_for_timeout(1000)
        except Exception:
            continue
        detail = _parse_detail_pane(page.locator("#third").inner_html())
        if not detail:
            continue
        parsed_date = parse_ilrs_date(detail.get("decision_date"))
        if parsed_date and parsed_date.year != year:
            # ILR year is a publication-year grouping, not a strict per-row
            # filter -- keep only rows whose own decision date matches.
            continue
        candidates.append(detail)

    return candidates
