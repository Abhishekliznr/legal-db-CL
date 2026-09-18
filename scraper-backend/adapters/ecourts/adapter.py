"""
Generic eCourts adapter (judgments.ecourts.gov.in) — spec §4.3.

One adapter for all 25 High Courts (Supreme Court stays on its own adapter,
adapters/supreme_court/, since it's a different site). Per-court behavior
is entirely driven by the `state_code`/`bench_code` parameters, sourced from
`court_scrape_config` (db/court_config.py) — nothing here is Delhi-specific
or hardcoded to any one court.

Written fresh against the ScraperAdapter interface. The old
scraper-backend/app/Dehli_High_Court_Scraper/delhi_high_court.py was read as
a reference for what already works against this portal (captcha widget,
state/bench dropdown JS events, results-table structure, the session-
timeout-captcha recovery loop, PDF capture via response listener) — nothing
from it is imported or copied.

NOT LIVE-VERIFIED: building this required no network access to
judgments.ecourts.gov.in from the sandbox this was written in. See spec §10
("HC results-row format uniformity") — this needs a real dry run per court,
starting with one, before an unattended multi-court rollout.
"""

import base64
import tempfile
import time
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from adapters.base import RawJudgmentRecord
from adapters.ecourts import selectors
from adapters.ecourts.captcha import solve_captcha_image

MAX_SEARCH_CAPTCHA_RETRIES = 20
MAX_SESSION_RECOVERY_RETRIES = 30
PDF_CAPTURE_WAIT_SECONDS = 6


def _clean(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return " ".join(text.split()).strip()


def _split_case_and_party(case_title: str) -> tuple:
    cleaned = _clean(case_title)
    if not cleaned:
        return None, None
    if " of " not in cleaned.lower():
        return cleaned, cleaned
    idx = cleaned.lower().rindex(" of ")
    return _clean(cleaned[:idx]), _clean(cleaned[idx + 4:])


def _captcha_or_timeout_visible(page) -> bool:
    try:
        for selector in (selectors.PDF_CAPTCHA_IMAGES, selectors.PDF_CAPTCHA_INPUTS):
            locator = page.locator(selector)
            for i in range(locator.count()):
                if locator.nth(i).is_visible():
                    return True
        body_text = page.locator("body").inner_text().lower()
        return any(phrase in body_text for phrase in ("enter captcha", "invalid captcha", "session timeout"))
    except Exception:
        return False


def _recover_session_timeout(page) -> bool:
    """Solves the mid-session captcha modal eCourts throws up after inactivity. Reference: old service's proven `handle_session_timeout_captcha`."""
    if not _captcha_or_timeout_visible(page):
        return True

    for _ in range(MAX_SESSION_RECOVERY_RETRIES):
        if not _captcha_or_timeout_visible(page):
            return True

        captcha_input = page.locator(selectors.PDF_CAPTCHA_INPUTS).first
        captcha_image = page.locator(selectors.PDF_CAPTCHA_IMAGES).first
        if captcha_image.count() == 0:
            return False

        solved = solve_captcha_image(captcha_image.screenshot())
        if solved and captcha_input.count() > 0:
            captcha_input.fill(solved)
            page.keyboard.press("Enter")
            page.wait_for_timeout(2000)
        else:
            refresh = page.locator("a[title='Refresh Image']")
            if refresh.count() > 0:
                refresh.first.click()
                page.wait_for_timeout(1000)

    return not _captcha_or_timeout_visible(page)


def _solve_initial_search_captcha(page) -> bool:
    page.locator(selectors.SEARCH_ALL_WORDS_RADIO).check()
    captcha_input = page.locator(selectors.SEARCH_CAPTCHA_INPUT)
    submit_button = page.locator(selectors.SEARCH_SUBMIT_BUTTON)
    captcha_image = page.locator(selectors.SEARCH_CAPTCHA_IMAGE)

    for _ in range(MAX_SEARCH_CAPTCHA_RETRIES):
        if captcha_image.count() == 0:
            return False
        solved = solve_captcha_image(captcha_image.screenshot())
        if solved:
            captcha_input.fill(solved)
            submit_button.click()
            page.wait_for_timeout(3000)
            if page.locator(selectors.STATE_SELECT).count() > 0:
                return True
        refresh = page.locator("a[title='Refresh Image']")
        if refresh.count() > 0:
            refresh.first.click()
            page.wait_for_timeout(1500)

    return False


def _select_court(page, state_code: str, bench_code: Optional[str]) -> None:
    state_select = page.locator(selectors.STATE_SELECT)
    state_select.select_option(state_code)
    page.evaluate(
        """(value) => {
            const el = document.querySelector('#state_code') || document.querySelector("select[name='state_code']");
            if (el) { el.value = value; el.dispatchEvent(new Event('change', {bubbles: true})); }
            if (window.jQuery) { window.jQuery('#state_code').val(value).trigger('change'); }
        }""",
        state_code,
    )
    page.wait_for_timeout(2500)

    bench_select = page.locator(selectors.BENCH_SELECT)
    if bench_select.count() == 0:
        return

    try:
        page.wait_for_function(
            """() => {
                const el = document.querySelector('#dist_code') || document.querySelector("select[name='dist_code']");
                return el && el.options.length > 1;
            }""",
            timeout=15000,
        )
    except PlaywrightTimeoutError:
        return

    target_bench = bench_code
    if not target_bench:
        options = bench_select.locator("option")
        for i in range(options.count()):
            value = options.nth(i).get_attribute("value")
            if value:
                target_bench = value
                break

    if target_bench:
        bench_select.select_option(target_bench)
        page.evaluate(
            """(value) => {
                const el = document.querySelector('#dist_code') || document.querySelector("select[name='dist_code']");
                if (el) { el.value = value; el.dispatchEvent(new Event('change', {bubbles: true})); }
                if (window.jQuery) { window.jQuery('#dist_code').val(value).trigger('change'); }
            }""",
            target_bench,
        )
    page.wait_for_timeout(1000)


def _set_decision_date(page, date_from: str, date_to: str) -> None:
    page.locator(selectors.CUSTOM_DATE_RADIO).check()
    page.locator(selectors.FROM_DATE_INPUT).fill(date_from)
    page.locator(selectors.TO_DATE_INPUT).fill(date_to)
    for selector in (selectors.FROM_DATE_INPUT, selectors.TO_DATE_INPUT):
        page.locator(selector).dispatch_event("change")
    page.wait_for_timeout(800)


def _run_final_search(page) -> None:
    page.locator(selectors.FINAL_SEARCH_BUTTON).first.click()
    page.wait_for_selector("#report_body", timeout=60000)
    page.wait_for_function(
        "() => { const b = document.querySelector('#report_body'); return b && b.querySelectorAll('tr').length > 0; }",
        timeout=60000,
    )


def _fetch_pdf_bytes_in_page(page, pdf_url: str) -> Optional[bytes]:
    try:
        data_url = page.evaluate(
            """async (url) => {
                const resp = await fetch(url, {headers: {'Accept': 'application/pdf,*/*'}});
                if (!resp.ok) return null;
                const blob = await resp.blob();
                return new Promise((resolve) => {
                    const reader = new FileReader();
                    reader.onloadend = () => resolve(reader.result);
                    reader.onerror = () => resolve(null);
                    reader.readAsDataURL(blob);
                });
            }""",
            pdf_url,
        )
        if data_url and "," in data_url:
            pdf_bytes = base64.b64decode(data_url.split(",", 1)[1])
            if pdf_bytes.startswith(b"%PDF"):
                return pdf_bytes
    except Exception:
        pass
    return None


def _capture_pdf(context, page, row) -> tuple:
    """Returns (pdf_bytes, resolved_url) or (None, None). Mirrors the old service's proven response-listener + modal-click capture pattern."""
    pdf_button = row.locator("button[role='link']").first
    if pdf_button.count() == 0:
        return None, None

    onclick = pdf_button.get_attribute("onclick") or ""
    pdf_source = selectors.parse_pdf_source(onclick)
    fallback_url = (
        urljoin(selectors.SEARCH_URL, pdf_source.lstrip("/")) if pdf_source and not pdf_source.startswith("http") else pdf_source
    )

    captured = {"bytes": None, "url": None}

    def on_response(response):
        try:
            content_type = response.headers.get("content-type", "").lower()
            if "application/pdf" in content_type or response.url.lower().endswith(".pdf"):
                body = response.body()
                if body and body.startswith(b"%PDF"):
                    captured["bytes"] = body
                    captured["url"] = response.url
        except Exception:
            pass

    page.on("response", on_response)
    try:
        try:
            pdf_button.click(timeout=3000)
        except Exception:
            pass

        deadline = time.time() + PDF_CAPTURE_WAIT_SECONDS
        while time.time() < deadline and captured["bytes"] is None:
            if _captcha_or_timeout_visible(page):
                break
            time.sleep(0.5)

        if captured["bytes"] is None and fallback_url:
            body = _fetch_pdf_bytes_in_page(page, fallback_url)
            if body:
                captured["bytes"], captured["url"] = body, fallback_url

        return captured["bytes"], (captured["url"] or fallback_url)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        try:
            close_button = page.locator(selectors.MODAL_CLOSE_BUTTONS).first
            if close_button.count() > 0 and close_button.is_visible():
                close_button.click(timeout=1500)
        except Exception:
            pass


class EcourtsAdapter:
    """Implements ScraperAdapter (adapters/base.py) for judgments.ecourts.gov.in, parameterized per court."""

    def scrape(
        self,
        date_from: str,
        date_to: str,
        state_code: str,
        bench_code: Optional[str] = None,
        headless: bool = True,
        **kwargs,
    ) -> Iterator[RawJudgmentRecord]:
        if not state_code:
            raise ValueError("EcourtsAdapter requires state_code (from court_scrape_config)")

        with tempfile.TemporaryDirectory(prefix="ecourts_pdfs_") as tmp_dir:
            download_dir = Path(tmp_dir)

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
                context = browser.new_context(no_viewport=True, accept_downloads=True)
                page = context.new_page()

                try:
                    page.goto(selectors.SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2000)

                    if not _solve_initial_search_captcha(page):
                        return  # couldn't get past the search captcha for this run

                    _select_court(page, state_code, bench_code)
                    _set_decision_date(page, date_from, date_to)
                    _run_final_search(page)

                    yield from self._iterate_pages(context, page, download_dir)
                finally:
                    browser.close()

    def _iterate_pages(self, context, page, download_dir: Path) -> Iterator[RawJudgmentRecord]:
        while True:
            rows = page.locator(selectors.RESULT_ROWS)
            row_count = rows.count()
            if row_count == 0:
                break

            for row_index in range(row_count):
                current_rows = page.locator(selectors.RESULT_ROWS)
                if row_index >= current_rows.count():
                    continue
                row = current_rows.nth(row_index)

                record = self._parse_and_capture_row(context, page, row, download_dir)
                if record is not None:
                    yield record

            next_button = page.locator(selectors.NEXT_PAGE_BUTTON)
            if next_button.count() == 0 or not next_button.is_visible():
                break
            try:
                next_button.click()
                page.wait_for_timeout(2000)
            except Exception:
                break

    def _parse_and_capture_row(self, context, page, row, download_dir: Path) -> Optional[RawJudgmentRecord]:
        cells = row.locator("td")
        if cells.count() < 2:
            return None

        case_button = cells.nth(1).locator("button[role='link']")
        if case_button.count() == 0:
            return None

        case_number, party_name = _split_case_and_party(_clean(case_button.first.inner_text()))
        description = _clean(cells.nth(1).inner_text())
        parsed = selectors.parse_row_description(description or "")

        if _captcha_or_timeout_visible(page) and not _recover_session_timeout(page):
            return None  # couldn't recover — skip this row rather than hang the whole batch

        pdf_bytes, pdf_url = _capture_pdf(context, page, row)
        if pdf_bytes is None:
            return None

        local_path = download_dir / f"{abs(hash(pdf_url or case_number))}.pdf"
        local_path.write_bytes(pdf_bytes)

        return RawJudgmentRecord(
            pdf_path=local_path,
            source_url=pdf_url or page.url,
            case_number_raw=case_number,
            party_name_raw=party_name,
            judge_raw=parsed.get("judge"),
            decision_date_raw=parsed.get("decision_date"),
            cnr_raw=parsed.get("cnr"),
            extra={"registration_date_raw": parsed.get("registration_date"), "disposal_nature_raw": parsed.get("disposal_nature")},
        )
