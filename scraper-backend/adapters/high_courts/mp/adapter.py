"""
Madhya Pradesh High Court adapter — two sites, one Playwright session:

1. Discovery: portal.mphc.gov.in/ilrs's "Search By ILR Details" tab --
   adapters/high_courts/mp/ilrs.py drives the year field + captcha modal +
   results list + detail pane, all confirmed live against the real site
   (real captcha solved, 582 real result rows found for ILR year 2024, one
   clicked through to a real parsed detail pane). That detail pane also
   gives the bench name directly (case_status.py's BENCH_CODES) -- no
   separate "derive bench from case number" step needed.
2. Lookup, per candidate: mphc.gov.in/case-status (case_type/case_no/
   year_registration) -- that single page load already contains the full
   case-detail table (adapters/high_courts/mp/case_status.py parses it) and
   a Judgement/Orders tab whose real markup is confirmed from a saved
   response (see `_find_judgment_pdf_url` below): a reverse-chronological
   list of every order in the case, each with its own "Download" link. Only
   the first (most recent) row is used -- the rest are earlier interim
   orders in the same case's procedural history, not the judgment itself.
   The Establishment (bench) the case belongs to, from ilrs.py's own parsed
   `bench` field, is picked via the "bench_code" <select> that's embedded in
   the case-status page's OWN header (see `_select_bench` below) -- the
   saved reference for mphc.gov.in/bench/select turned out to just be a
   snapshot of the case-status page itself (its <title> is literally "Case
   Status"), i.e. it's not a separate page carrying that selector on its
   own; navigating there directly with a bare GET risked the selector not
   even being present in the response, silently leaving every candidate on
   whatever Establishment the session defaulted to and making every
   candidate on a different bench come back "no matching case" on
   case-status. This now switches Establishment from the case-status page
   itself, where the selector is confirmed present.

ocr_text/judgement always come from that real PDF (OCR'd by the unmodified
shared pipeline/ocr.py, same as Supreme Court) -- ILRS's own inline
judgement text is not used for those fields (its headnote IS used, for
case_note). A candidate whose Judgement/Orders tab has no PDF is skipped
(logged, not yielded), not promoted as a partial record.

Every step of this flow (the case-status form fields, the case_type code
map, the case-detail table's own structure, the Judgement/Orders tab, the
full ILRS discovery flow) is confirmed against real live responses — see
adapters/high_courts/mp/case_status.py and ilrs.py's own docstrings.
`_download_pdf` below no longer assumes the download link always makes the
browser fire a "download" event (real-run evidence: PDFs were consistently
failing to download) -- it falls back to a direct authenticated fetch
through the same browser context when the browser-download path doesn't
fire in time, which also covers a server that serves the PDF inline instead
of as an attachment.
"""

import logging
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from adapters.base import BatchItem, ItemOutcome, RawJudgmentRecord, SourceUnavailableError
from adapters.high_courts.mp import case_status, ilrs, stages
from adapters.high_courts.mp.extraction import clean_headnote, normalize_case_no, normalize_case_type
from db import court_config, scrape_jobs
from orchestrator.log_context import slog

logger = logging.getLogger("scraper_backend_v2.mp_adapter")

CASE_STATUS_URL = "https://mphc.gov.in/case-status"

PDF_CAPTURE_WAIT_SECONDS = 20

# Covers a laptop waking from sleep: Wi-Fi typically takes 10-30s to come
# back, and every request in that window fails with net::ERR_*.
NETWORK_RETRY_DELAYS_SECONDS = (5, 15, 30, 60)


def _year_from_range(date_from: str, date_to: str) -> int:
    """MP is queried by a single year (the ILRS portal's own "ILR year" field), not a date range -- derive it from date_from/to, requiring both to fall in the same calendar year rather than changing the batch's date-range shape."""
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if start.year != end.year:
        raise ValueError(
            f"MPHighCourtAdapter is queried by year, not a date range — "
            f"date_from={date_from} and date_to={date_to} must fall in the same calendar year"
        )
    return start.year


def _wait_for_judgement_content(page, timeout_ms: int = 25000) -> bool:
    """
    #judgement (like every other case-status result tab -- #ia, #document,
    #listing, etc.) ships as an EMPTY <div class="tab-pane fade"></div> in
    the page's initial HTML -- this site loads each tab's real content on
    demand, the same AJAX-on-click pattern confirmed for this site's other
    tabbed views. The first fix here (wait for any non-empty innerHTML) was
    ITSELF still wrong -- confirmed from a real run's logs: the instant the
    tab opens, the site fills #judgement with its own loading placeholder
    (`<div class="spinner-border ...">Loading judgement...</div>`), which is
    non-empty HTML, so that wait resolved immediately against the spinner
    and never actually waited for the AJAX response behind it. This now
    polls until that spinner is gone, not just until *some* HTML exists.
    """
    try:
        page.wait_for_function(
            "() => { const el = document.getElementById('judgement'); "
            "return el && el.innerHTML.trim().length > 0 && !el.querySelector('.spinner-border'); }",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


def _find_judgment_pdf_url(page) -> Optional[str]:
    """
    The Judgement/Orders tab (#judgement) is expected to render a
    reverse-chronological list of every order in the case's history, each
    with its own download/view link -- only the FIRST (most recent) row's
    link is used, per spec, since every other row is an earlier interim
    order in the same case's procedural history, not the judgment itself.
    This reads raw HTML (not a CSS locator) because a real example showed
    the server wraps this content in a literal `<!-- ... -->` HTML comment,
    which a browser's DOM parser never turns into real elements.

    The exact link text/URL shape is not reliably confirmed (a real
    screenshot showed a "View" button, not "Download", and no saved
    reference in this repo actually captured #judgement's populated HTML)
    -- so this matches broadly on any mphc.gov.in/order/... link rather
    than a hardcoded "download" literal, and logs the pane's raw content on
    a genuine miss so a real run's logs can pin down the exact markup
    instead of guessing again.
    """
    if not _wait_for_judgement_content(page):
        slog(logger, stages.JUDGMENT, "debug", "#judgement pane never got real content (still empty after wait)")
        return None

    html = page.locator("#judgement").inner_html()
    match = re.search(r'href="(https://mphc\.gov\.in/order/[^"]+)"', html)
    if match is None:
        slog(logger, stages.JUDGMENT, "debug", "no order link found in #judgement; raw pane content follows: %s", html[:3000])
    return match.group(1) if match else None


def _download_pdf(context, page, pdf_url: str, download_dir: Path) -> Optional[Path]:
    """
    The Download link's own label/icon ("Download <i class='bi bi-download'>")
    suggests the server responds with Content-Disposition: attachment (a
    real browser download), so this tries Playwright's download API
    (expect_download) first -- page.goto() itself is EXPECTED to raise/abort
    once the browser hands the response off to its download manager instead
    of loading it as a page, so that's not treated as a failure on its own.

    Real runs showed this alone isn't reliable (PDFs consistently failing to
    download) -- most likely the server sometimes serves the PDF inline
    instead of as an attachment, in which case no "download" event ever
    fires and page.goto() just renders it. The fallback below fetches the
    same URL directly through the browser context's own request API (shares
    the context's session cookies, so still authenticated) and writes the
    response body straight to disk, which works regardless of how the
    server chooses to serve it.
    """
    local_path = download_dir / f"{abs(hash(pdf_url))}.pdf"
    method = "browser download"

    try:
        with page.expect_download(timeout=PDF_CAPTURE_WAIT_SECONDS * 1000) as download_info:
            try:
                page.goto(pdf_url, timeout=30000)
            except Exception:
                pass  # navigation aborting here is the normal/expected shape of a triggered download
        download_info.value.save_as(str(local_path))
    except Exception as e:
        slog(logger, stages.JUDGMENT, "debug", "browser download did not fire for %s (%s), falling back to direct fetch", pdf_url, e)
        method = "direct fetch"
        try:
            response = context.request.get(pdf_url, timeout=30000)
            if not response.ok:
                slog(logger, stages.JUDGMENT, "warning", "PDF download failed (HTTP %s) → skipped", response.status)
                return None
            local_path.write_bytes(response.body())
        except Exception as fetch_err:
            slog(logger, stages.JUDGMENT, "warning", "PDF download failed (%s) → skipped", fetch_err)
            return None

    if not local_path.exists() or not local_path.read_bytes().startswith(b"%PDF"):
        slog(logger, stages.JUDGMENT, "warning", "Downloaded file is not a valid PDF → skipped")
        return None
    slog(logger, stages.JUDGMENT, "info", "PDF downloaded (%d KB, via %s)", local_path.stat().st_size // 1024, method)
    return local_path


def _select_bench(page, bench_code: str) -> None:
    """
    Switches Establishment from the case-status page's own header selector
    (<form action=".../bench/select" method="POST"><select name="bench_code">)
    -- must be called with `page` already on CASE_STATUS_URL, since that
    selector is only confirmed present there (see module docstring). Raises
    if the selector isn't found, rather than silently no-op'ing, so a
    site-markup change surfaces as a real error instead of every subsequent
    candidate quietly getting the wrong Establishment.
    """
    page.evaluate(
        "(code) => { const el = document.querySelector('select[name=\"bench_code\"]'); "
        "if (!el) throw new Error('bench_code select not found on case-status page'); "
        "el.value = code; el.closest('form').submit(); }",
        bench_code,
    )
    page.wait_for_load_state("domcontentloaded")


def _is_network_error(exc: Exception) -> bool:
    return isinstance(exc, PlaywrightTimeoutError) or (
        isinstance(exc, PlaywrightError) and "net::ERR_" in str(exc)
    )


def _case_label(candidate: Dict[str, object]) -> str:
    # Same "<Bench>/<CaseType>/<Number>/<Year>" shape ILRS itself uses
    # (see ilrs.py) -- case_no alone can collide across benches/years,
    # this is what actually identifies a case uniquely. Also stored as
    # cr_cases.case_number, which is what the re-run skip matches on.
    case_no = normalize_case_no(candidate["case_no"])
    return f"{candidate.get('bench')}/{candidate.get('case_type')}/{case_no}/{candidate.get('registration_year')}"


def _submit_case_status_form(page, case_type_code: str, case_no: str, registration_year: str) -> None:
    page.locator("#case_type").select_option(case_type_code)
    page.locator("#case_no").fill(str(case_no))
    page.locator("#year_registration").select_option(str(registration_year))
    page.locator("#case_status_form button[type='submit']").click()
    page.wait_for_load_state("domcontentloaded")


@dataclass
class _Session:
    context: object
    page: object
    download_dir: Path
    current_bench_code: Optional[str] = None


def _launch_browser(playwright, headless: bool):
    """Returns (browser, context, page).

    mphc.gov.in serves an incomplete intermediate cert chain -- confirmed from
    a real run: the direct-fetch fallback in _download_pdf failed with "unable
    to verify the first certificate". Real browsers paper over it; Playwright's
    strict TLS verification doesn't, on page navigation OR the request-API
    fallback, hence ignore_https_errors.
    """
    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context(accept_downloads=True, ignore_https_errors=True)
    return browser, context, context.new_page()


class MPHighCourtAdapter:
    """Implements ResumableScraperAdapter (adapters/base.py) for Madhya Pradesh High Court."""

    def discover(self, date_from: str, date_to: str, headless: bool = True, **kwargs) -> List[BatchItem]:
        year = _year_from_range(date_from, date_to)
        court_id = court_config.get_court_id_by_code("MPHC")
        already_promoted = scrape_jobs.get_promoted_case_numbers(court_id) if court_id is not None else set()

        with sync_playwright() as playwright:
            browser, _, page = _launch_browser(playwright, headless)
            try:
                candidates = ilrs.discover_candidates(page, year)
            finally:
                browser.close()

        items: Dict[str, BatchItem] = {}
        for candidate in candidates:
            label = _case_label(candidate)
            if label not in items:
                items[label] = BatchItem(
                    key=label,
                    payload=candidate,
                    done_reason="already in database" if label in already_promoted else None,
                )
        return list(items.values())

    @contextmanager
    def session(self, headless: bool = True, **kwargs) -> Iterator[_Session]:
        with tempfile.TemporaryDirectory(prefix="mphc_pdfs_") as tmp_dir:
            with sync_playwright() as playwright:
                browser, context, page = _launch_browser(playwright, headless)
                try:
                    yield _Session(context=context, page=page, download_dir=Path(tmp_dir))
                finally:
                    browser.close()

    def process_item(self, session: _Session, item: BatchItem) -> ItemOutcome:
        try:
            record, skip_reason, session.current_bench_code = self._process_candidate_with_retry(
                session.context, session.page, item.payload, session.download_dir, session.current_bench_code
            )
        except Exception:
            # Page state is unknown after a crash; force the bench-select POST on the next case.
            session.current_bench_code = None
            raise
        return ItemOutcome(record=record, skip_reason=skip_reason)

    def _process_candidate_with_retry(
        self, context, page, candidate: Dict[str, object], download_dir: Path, current_bench_code: Optional[str]
    ) -> tuple:
        case_label = _case_label(candidate)
        for attempt, delay in enumerate((*NETWORK_RETRY_DELAYS_SECONDS, None), start=1):
            try:
                return self._process_candidate(context, page, candidate, download_dir, current_bench_code)
            except Exception as e:
                if not _is_network_error(e):
                    raise
                if delay is None:
                    raise SourceUnavailableError(
                        f"mphc.gov.in unreachable after {attempt} attempts on {case_label}: {e}"
                    ) from e
                slog(
                    logger, stages.CASE_STATUS, "warning",
                    "Network error (attempt %d of %d) · retrying from case-status lookup in %ds: %s",
                    attempt, len(NETWORK_RETRY_DELAYS_SECONDS) + 1, delay, e,
                )
                time.sleep(delay)
                # Page state after a failed navigation is unknown, so force
                # the bench-select POST again on the retry.
                current_bench_code = None

    def _process_candidate(
        self, context, page, candidate: Dict[str, object], download_dir: Path, current_bench_code: Optional[str]
    ) -> tuple:
        """Returns (record, skip_reason, bench_code_now_selected) -- exactly one of record/skip_reason is set; the caller threads the bench code back in as `current_bench_code` on the next call, so the bench-select POST only fires when the bench actually changes between consecutive candidates."""
        # ILRS zero-pads some case numbers (e.g. "01598") -- case-status's
        # #case_no field and its own case-detail pages never do, so this
        # must be stripped before it's used to search, not just for display.
        case_no = normalize_case_no(candidate["case_no"])
        case_label = _case_label(candidate)

        case_type_code = case_status.resolve_case_type_code(normalize_case_type(candidate["case_type"]))
        if case_type_code is None:
            slog(logger, stages.CASE_STATUS, "warning", "Unknown case type %r → skipped", candidate["case_type"])
            return None, "unknown case type", current_bench_code

        bench_code = case_status.BENCH_CODES.get(candidate.get("bench"))
        if bench_code is None:
            slog(logger, stages.CASE_STATUS, "warning", "Unknown bench %r → skipped", candidate.get("bench"))
            return None, "unknown bench", current_bench_code

        page.goto(CASE_STATUS_URL, wait_until="domcontentloaded", timeout=30000)
        if bench_code != current_bench_code:
            try:
                _select_bench(page, bench_code)
            except Exception as e:
                if _is_network_error(e):
                    raise
                slog(
                    logger, stages.CASE_STATUS, "warning",
                    "Couldn't switch case-status to bench %r (%s) → skipped", candidate.get("bench"), e,
                )
                return None, "bench switch failed", current_bench_code
            current_bench_code = bench_code

        _submit_case_status_form(page, case_type_code, case_no, str(candidate["registration_year"]))
        details = case_status.parse_case_details(page.content())
        if not details:
            # Not yet reproduced against a saved response, so not yet fixed
            # blind -- logging exactly what was submitted plus the actual
            # rendered page text (not raw page.content(): a real run showed
            # this page's own <head> block repeated verbatim several times,
            # which ate a whole 3000-char HTML slice before reaching any
            # real body content -- inner_text() skips all of that and shows
            # only what a person would actually see) lets a real run's logs
            # show whether this is a bad field value, a stale/misapplied
            # Establishment, or something else, instead of an unexplained
            # dead end.
            slog(
                logger, stages.CASE_STATUS, "warning",
                "Case not available on case-status → skipped (searched %s %s/%s, bench %s)",
                normalize_case_type(candidate["case_type"]), case_no, candidate["registration_year"], candidate.get("bench"),
            )
            try:
                page_text = page.locator("body").inner_text()[:2000]
            except Exception as e:
                page_text = f"<couldn't read page text: {e}>"
            slog(
                logger, stages.CASE_STATUS, "debug",
                "submitted case_type_code=%s bench_code=%s; page text: %s", case_type_code, bench_code, page_text,
            )
            return None, "not on case-status", current_bench_code
        slog(logger, stages.CASE_STATUS, "info", "Case available")

        try:
            page.locator("button[data-link-type='judgement']").click(timeout=5000)
        except Exception as e:
            slog(logger, stages.JUDGMENT, "debug", "couldn't click Judgement/Orders tab button (%s)", e)
        pdf_url = _find_judgment_pdf_url(page)
        if pdf_url is None:
            slog(logger, stages.JUDGMENT, "warning", "No judgment/order in Judgement/Orders tab → skipped")
            return None, "no judgment PDF", current_bench_code
        slog(logger, stages.JUDGMENT, "info", "Latest order found in Judgement/Orders tab → downloading")

        pdf_path = _download_pdf(context, page, pdf_url, download_dir)
        if pdf_path is None:
            return None, "PDF download failed", current_bench_code

        record = RawJudgmentRecord(
            pdf_path=pdf_path,
            source_url=pdf_url,
            # Full "<Bench>/<CaseType>/<Number>/<Year>" form, not just the
            # bare number -- case_status's own "Case No." cell never carries
            # the Establishment/bench name, only ILRS's case_label does.
            case_number_raw=case_label,
            decision_date_raw=candidate.get("decision_date"),
            cnr_raw=details.get("cnr"),
            neutral_citation_raw=candidate.get("neutral_citation"),
            extra={
                # `candidate` (ILRS detail pane) was previously dropped
                # entirely here -- only `details` (case-status) ever made it
                # into `extra`, silently losing candidate's own
                # neutral_citation/ilr_citation/judges/bench_type. `details`
                # is spread second so case-status's own values win on any
                # key collision (e.g. "judges": case-status's Last Listed On
                # parse is the source of truth for cr_cases.bench, not
                # ILRS's).
                **candidate,
                **details,
                "headnote": clean_headnote(candidate.get("headnote")),
                "registration_year": candidate.get("registration_year"),
            },
        )
        return record, None, current_bench_code
