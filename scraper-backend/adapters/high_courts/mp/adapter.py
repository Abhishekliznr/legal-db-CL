"""
Madhya Pradesh High Court adapter — two sites, one Playwright session:

1. Discovery: portal.mphc.gov.in/ilrs's "Search By ILR Details" tab --
   adapters/high_courts/mp/ilrs.py drives the year field + captcha modal +
   results list + detail pane, all confirmed live against the real site
   (real captcha solved, 582 real result rows found for ILR year 2024, one
   clicked through to a real parsed detail pane). That detail pane also
   gives the bench name directly (case_status.py's BENCH_CODES) -- no
   separate "derive bench from case number" step needed.
2. Lookup, per candidate: mphc.gov.in/bench/select (pick the Establishment
   the case belongs to, from ilrs.py's own parsed `bench` field) then
   mphc.gov.in/case-status (case_type/case_no/year_registration) -- that
   single page load already contains the full case-detail table
   (adapters/high_courts/mp/case_status.py parses it) and a Judgement/Orders
   tab whose real markup is confirmed from a saved response (see
   `_find_judgment_pdf_url` below): a reverse-chronological list of every
   order in the case, each with its own "Download" link. Only the first
   (most recent) row is used -- the rest are earlier interim orders in the
   same case's procedural history, not the judgment itself.

ocr_text/judgement always come from that real PDF (OCR'd by the unmodified
shared pipeline/ocr.py, same as Supreme Court) -- ILRS's own inline
judgement text is not used for those fields (its headnote IS used, for
case_note). A candidate whose Judgement/Orders tab has no PDF is skipped
(logged, not yielded), not promoted as a partial record.

Every step of this flow (the case-status form fields, the case_type code
map, the case-detail table's own structure, the Judgement/Orders tab, the
full ILRS discovery flow) is confirmed against real live responses — see
adapters/high_courts/mp/case_status.py and ilrs.py's own docstrings. The
one thing genuinely not yet exercised end-to-end is a real PDF download
actually completing through Playwright's expect_download() (written
against the confirmed markup, but the download itself untested live).
"""

import logging
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Dict, Iterator, Optional

from playwright.sync_api import sync_playwright

from adapters.base import RawJudgmentRecord
from adapters.high_courts.mp import case_status, ilrs
from adapters.high_courts.mp.extraction import clean_headnote, normalize_case_no, normalize_case_type

logger = logging.getLogger("scraper_backend_v2.mp_adapter")

BENCH_SELECT_URL = "https://mphc.gov.in/bench/select"
CASE_STATUS_URL = "https://mphc.gov.in/case-status"

PDF_CAPTURE_WAIT_SECONDS = 6


def _year_from_range(date_from: str, date_to: str) -> int:
    """MP is queried by a single year (the ILRS portal's own "ILR year" field), not a date range -- derive it from date_from/to, requiring both to fall in the same calendar year rather than changing /api/scraper/start's request shape."""
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if start.year != end.year:
        raise ValueError(
            f"MPHighCourtAdapter is queried by year, not a date range — "
            f"date_from={date_from} and date_to={date_to} must fall in the same calendar year"
        )
    return start.year


def _find_judgment_pdf_url(page) -> Optional[str]:
    """
    Confirmed live: the Judgement/Orders tab (#judgement) renders a
    reverse-chronological <ul class="list-group..."> of every order in the
    case's history, each a <li> with a date, a label ("Final Order"/
    "Order"/etc.), and a "Download" <a href="https://mphc.gov.in/order/download/<token>">.
    Only the FIRST (most recent) row's link is used, per spec -- every
    other row is an earlier interim order in the same case's procedural
    history, not the judgment itself; the most recent entry is the one
    consistently labeled "Final Order" in the confirmed real example.

    Confirmed live (the actual reason this reads raw HTML instead of using
    a CSS locator): the server renders this entire <ul> wrapped in a
    literal `<!-- ... -->` HTML comment. A browser's DOM parser never turns
    tags inside a comment into real elements, so page.locator() can't see
    any of it -- inner_html() still returns the comment's raw text though,
    which is why this reads the string directly with a regex instead of
    querying the DOM for it.
    """
    pane = page.locator("#judgement")
    try:
        pane.wait_for(state="attached", timeout=5000)
    except Exception:
        return None

    match = re.search(r'href="(https://mphc\.gov\.in/order/download/[^"]+)"', pane.inner_html())
    return match.group(1) if match else None


def _download_pdf(context, page, pdf_url: str, download_dir: Path) -> Optional[Path]:
    """
    The Download link's own label/icon ("Download <i class='bi bi-download'>")
    and its target strongly suggest the server responds with
    Content-Disposition: attachment -- a real browser download, not an
    inline-viewable response -- so this uses Playwright's download API
    (expect_download), not a response-body sniff. page.goto() itself is
    EXPECTED to raise/abort once the browser hands the response off to its
    download manager instead of loading it as a page; that's not treated as
    a failure, only the absence of a captured Download object is.
    """
    try:
        with page.expect_download(timeout=PDF_CAPTURE_WAIT_SECONDS * 1000) as download_info:
            try:
                page.goto(pdf_url, timeout=30000)
            except Exception:
                pass  # navigation aborting here is the normal/expected shape of a triggered download
        download = download_info.value
    except Exception:
        return None

    local_path = download_dir / f"{abs(hash(pdf_url))}.pdf"
    download.save_as(str(local_path))
    if not local_path.exists() or not local_path.read_bytes().startswith(b"%PDF"):
        return None
    return local_path


def _submit_case_status_form(page, case_type_code: str, case_no: str, registration_year: str) -> None:
    page.goto(CASE_STATUS_URL, wait_until="domcontentloaded", timeout=30000)
    page.locator("#case_type").select_option(case_type_code)
    page.locator("#case_no").fill(str(case_no))
    page.locator("#year_registration").select_option(str(registration_year))
    page.locator("#case_status_form button[type='submit']").click()
    page.wait_for_load_state("domcontentloaded")


class MPHighCourtAdapter:
    """Implements ScraperAdapter (adapters/base.py) for Madhya Pradesh High Court."""

    def scrape(
        self,
        date_from: str,
        date_to: str,
        headless: bool = True,
        **kwargs,
    ) -> Iterator[RawJudgmentRecord]:
        year = _year_from_range(date_from, date_to)

        with tempfile.TemporaryDirectory(prefix="mphc_pdfs_") as tmp_dir:
            download_dir = Path(tmp_dir)

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=headless)
                context = browser.new_context(accept_downloads=True)
                page = context.new_page()

                try:
                    candidates = ilrs.discover_candidates(page, year)

                    current_bench_code: Optional[str] = None
                    for candidate in candidates:
                        record, current_bench_code = self._process_candidate(
                            context, page, candidate, download_dir, current_bench_code
                        )
                        if record is not None:
                            yield record
                finally:
                    browser.close()

    def _process_candidate(
        self, context, page, candidate: Dict[str, object], download_dir: Path, current_bench_code: Optional[str]
    ) -> tuple:
        """Returns (record_or_None, bench_code_now_selected) -- the caller threads the second value back in as `current_bench_code` on the next call, so the bench-select POST only fires when the bench actually changes between consecutive candidates."""
        # ILRS zero-pads some case numbers (e.g. "01598") -- case-status's
        # #case_no field and its own case-detail pages never do, so this
        # must be stripped before it's used to search, not just for display.
        case_no = normalize_case_no(candidate["case_no"])
        case_type_code = case_status.resolve_case_type_code(normalize_case_type(candidate["case_type"]))
        if case_type_code is None:
            logger.warning("[MP ADAPTER] case_no=%s: unrecognized case_type=%r, skipping", case_no, candidate["case_type"])
            return None, current_bench_code

        bench_code = case_status.BENCH_CODES.get(candidate.get("bench"))
        if bench_code is None:
            logger.warning("[MP ADAPTER] case_no=%s: unrecognized bench=%r, skipping", case_no, candidate.get("bench"))
            return None, current_bench_code
        if bench_code != current_bench_code:
            page.goto(BENCH_SELECT_URL, timeout=30000)
            page.evaluate(
                "(code) => { const el = document.querySelector('select[name=\"bench_code\"]'); "
                "if (el) { el.value = code; el.closest('form').submit(); } }",
                bench_code,
            )
            page.wait_for_load_state("domcontentloaded")
            current_bench_code = bench_code

        _submit_case_status_form(page, case_type_code, case_no, str(candidate["registration_year"]))
        details = case_status.parse_case_details(page.content())
        if not details:
            logger.warning("[MP ADAPTER] case_no=%s: case-status returned no matching case, skipping", case_no)
            return None, current_bench_code

        try:
            page.locator("button[data-link-type='judgement']").click(timeout=5000)
        except Exception:
            pass
        pdf_url = _find_judgment_pdf_url(page)
        if pdf_url is None:
            logger.warning("[MP ADAPTER] case_no=%s: no judgment PDF found in Judgement/Orders tab, skipping", case_no)
            return None, current_bench_code

        pdf_path = _download_pdf(context, page, pdf_url, download_dir)
        if pdf_path is None:
            logger.warning("[MP ADAPTER] case_no=%s: judgment PDF link found but download failed, skipping", case_no)
            return None, current_bench_code

        record = RawJudgmentRecord(
            pdf_path=pdf_path,
            source_url=pdf_url,
            case_number_raw=str(case_no),
            decision_date_raw=candidate.get("decision_date"),
            cnr_raw=details.get("cnr"),
            extra={
                **details,
                "headnote": clean_headnote(candidate.get("headnote")),
                "registration_year": candidate.get("registration_year"),
            },
        )
        return record, current_bench_code
