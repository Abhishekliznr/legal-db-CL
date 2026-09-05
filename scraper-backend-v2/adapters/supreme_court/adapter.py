"""
Supreme Court of India adapter (sci.gov.in) — spec §4.2.

Written fresh against the ScraperAdapter interface. The old
scraper-backend/app/SUPREME_COURT_OF_INDIA_SCRAPER/supreme_court.py was read
as a reference for what already works against this site (30-day batching,
captcha widget selectors, results-table structure, pagination) — nothing
from it is imported or copied.

LIVE-VERIFIED (partially) 2026-09-06: a real user run against sci.gov.in
confirmed the captcha solving, results-table parsing, and PDF download all
work end-to-end. One real mismatch was found and fixed: the site has no
separate date/citation columns at all (the originally-guessed "Order /
Judgment By Date"/"Judgment Date"/"Date" headers don't exist) — both the
decision date and neutral citation are packed into one "Judgment" cell's
text, now parsed by _parse_judgment_cell(). Not yet verified: a longer date
range / higher-volume run, and whether this same column layout holds for
older judgments (this was checked against a handful of January 2026 rows).
"""

import logging
import re
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger("scraper_backend_v2.adapters.supreme_court")
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from adapters.base import RawJudgmentRecord
from adapters.supreme_court.captcha import solve_captcha_image

BASE_URL = "https://www.sci.gov.in"
SEARCH_URL = f"{BASE_URL}/judgements-judgement-date/"

MAX_CAPTCHA_RETRIES = 20
BATCH_MAX_DAYS = 30  # sci.gov.in rejects date ranges longer than this

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
"""

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def _clean_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return re.sub(r"\s+", " ", text).strip()


# The results table has no separate date/citation columns at all — confirmed
# from a real run's logged headers (2026-09-06): ['Serial Number', 'Diary
# Number', 'Case Number', 'Petitioner / Respondent', 'Petitioner/Respondent
# Advocate', 'Bench', 'Judgment By', 'Judgment']. Both the decision date and
# the neutral citation are packed into the "Judgment" cell's text as e.g.
# "05-01-2026(English) 2026 INSC 5(English)" — one link's visible text
# concatenated with another's via inner_text(). The two guessed column names
# this originally looked for ("Order / Judgment By Date", "Judgment Date",
# "Date") don't exist on the real site; parsing "Judgment" is now the only path.
_JUDGMENT_CELL_DATE_PATTERN = re.compile(r"(\d{1,2}[-/]\d{1,2}[-/]\d{4})")
_JUDGMENT_CELL_CITATION_PATTERN = re.compile(r"\b(\d{4}\s*INSC\s*\d+)\b", re.IGNORECASE)


def _parse_judgment_cell(raw: Optional[str]) -> tuple:
    """Returns (decision_date_raw, neutral_citation_raw) parsed out of the "Judgment" column's combined text."""
    if not raw:
        return None, None
    date_match = _JUDGMENT_CELL_DATE_PATTERN.search(raw)
    citation_match = _JUDGMENT_CELL_CITATION_PATTERN.search(raw)
    return (
        date_match.group(1) if date_match else None,
        citation_match.group(1) if citation_match else None,
    )


def _parse_date(date_str: str) -> datetime:
    if "-" in date_str and len(date_str.split("-")[0]) == 4:
        return datetime.strptime(date_str, "%Y-%m-%d")
    return datetime.strptime(date_str, "%d-%m-%Y")


def _split_into_batches(date_from: str, date_to: str) -> list[tuple[str, str]]:
    start, end = _parse_date(date_from), _parse_date(date_to)
    if start > end:
        raise ValueError(f"date_from ({date_from}) is after date_to ({date_to})")

    batches = []
    current = start
    while current <= end:
        batch_end = min(current + timedelta(days=BATCH_MAX_DAYS - 1), end)
        batches.append((current.strftime("%d-%m-%Y"), batch_end.strftime("%d-%m-%Y")))
        current = batch_end + timedelta(days=1)
    return batches


def _download_pdf(pdf_url: str, dest_dir: Path) -> Optional[Path]:
    headers = {"User-Agent": _USER_AGENT, "Referer": f"{BASE_URL}/"}
    try:
        response = requests.get(pdf_url, headers=headers, timeout=30, verify=False)
        if response.status_code == 200 and response.content.startswith(b"%PDF"):
            dest_path = dest_dir / f"{abs(hash(pdf_url))}.pdf"
            dest_path.write_bytes(response.content)
            return dest_path
    except requests.RequestException:
        pass
    return None


def _solve_and_submit_captcha(page, from_input, to_input, batch_from: str, batch_to: str) -> bool:
    from_input.fill(batch_from)
    to_input.fill(batch_to)

    captcha_input = page.locator("#siwp_captcha_value_0, input[name='siwp_captcha_value']")
    submit_button = page.locator("input[type='submit'][name='submit']")
    captcha_image = page.locator("#siwp_captcha_image_0, .siwp_captcha_image")

    for _ in range(MAX_CAPTCHA_RETRIES):
        if captcha_image.count() == 0 or not captcha_image.first.is_visible():
            return False

        solved_value = solve_captcha_image(captcha_image.first.screenshot())
        if solved_value:
            captcha_input.fill(solved_value)
            submit_button.click()
            page.wait_for_timeout(4000)

            if page.locator(".distTableContent table tbody tr").count() > 0:
                return True
            if page.locator(":has-text('No Records Found')").count() > 0:
                return False  # legitimately empty result, not a captcha failure

        refresh_button = page.locator("a[title='Refresh Image']")
        if refresh_button.count() > 0:
            refresh_button.first.click()
            page.wait_for_timeout(1500)

    return False


def _scrape_one_batch(page, batch_from: str, batch_to: str, download_dir: Path) -> Iterator[RawJudgmentRecord]:
    page.goto(SEARCH_URL, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2000)

    from_input = page.locator("#from_date")
    to_input = page.locator("#to_date")
    if from_input.count() == 0 or to_input.count() == 0:
        raise RuntimeError("sci.gov.in date input fields not found — page structure may have changed.")

    if not _solve_and_submit_captcha(page, from_input, to_input, batch_from, batch_to):
        return  # empty result or unsolved captcha for this batch; move on

    page_number = 1
    headers_logged = False
    while True:
        rows = page.locator(".distTableContent table tbody tr")
        headers = [_clean_text(h.inner_text()) for h in page.locator(".distTableContent table thead th").all()]

        if not headers_logged:
            # Column names assumed below ("Order / Judgment By Date" etc.)
            # were guessed from an old reference script without live access
            # (see module docstring) — log the real ones once per batch so a
            # mismatch is visible immediately instead of silently producing
            # a null decision_date_raw down the line.
            logger.info("sci.gov.in results table headers: %s", headers)
            headers_logged = True

        for row_index in range(rows.count()):
            row = rows.nth(row_index)
            cells = row.locator("td")
            if cells.count() == 0:
                continue

            data = {}
            for col_index, cell in enumerate(cells.all()):
                header_name = headers[col_index] if col_index < len(headers) else f"col_{col_index}"
                data[header_name] = _clean_text(cell.inner_text())

            pdf_links = [urljoin(BASE_URL, a.get_attribute("href")) for a in row.locator("a").all() if a.get_attribute("href")]
            if not pdf_links:
                continue

            pdf_path = _download_pdf(pdf_links[0], download_dir)
            if pdf_path is None:
                continue

            decision_date_raw, neutral_citation_raw = _parse_judgment_cell(data.get("Judgment"))
            if decision_date_raw is None:
                logger.warning("Could not parse a date out of the 'Judgment' cell for this row — full row data: %s", data)

            yield RawJudgmentRecord(
                pdf_path=pdf_path,
                source_url=pdf_links[0],
                case_number_raw=data.get("Case Number"),
                party_name_raw=data.get("Petitioner / Respondent"),
                judge_raw=data.get("Judgment By") or data.get("Bench"),
                decision_date_raw=decision_date_raw,
                cnr_raw=data.get("Diary Number"),
                neutral_citation_raw=neutral_citation_raw,
                extra={"advocate_raw": data.get("Petitioner/Respondent Advocate")},
            )

        next_button = page.locator("#paginationHtml a:has-text('Next')")
        if next_button.count() == 0 or not next_button.first.is_visible():
            break
        try:
            next_button.first.click()
            page.wait_for_timeout(3000)
            page_number += 1
        except PlaywrightTimeoutError:
            break


class SupremeCourtAdapter:
    """Implements ScraperAdapter (adapters/base.py) for sci.gov.in."""

    def scrape(self, date_from: str, date_to: str, headless: bool = True, **kwargs) -> Iterator[RawJudgmentRecord]:
        batches = _split_into_batches(date_from, date_to)

        with tempfile.TemporaryDirectory(prefix="scin_pdfs_") as tmp_dir:
            download_dir = Path(tmp_dir)

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=headless,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                )
                context = browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent=_USER_AGENT,
                )
                context.add_init_script(_STEALTH_JS)
                page = context.new_page()

                try:
                    for batch_from, batch_to in batches:
                        yield from _scrape_one_batch(page, batch_from, batch_to, download_dir)
                        time.sleep(1)  # be polite between batches
                finally:
                    browser.close()
