import logging
import re
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from adapters.base import RawJudgmentRecord
from adapters.captcha_ocr import solve_captcha_image

from .client import BASE_URL, SupremeCourtBrowserClient
from adapters.base import SourceStructureChangedError

logger = logging.getLogger("scraper_backend_v2.adapters.supreme_court")

MAX_CAPTCHA_RETRIES = 20
BATCH_MAX_DAYS = 30


def _clean_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return re.sub(r"\s+", " ", text).strip()


_JUDGMENT_CELL_DATE_PATTERN = re.compile(r"(\d{1,2}[-/]\d{1,2}[-/]\d{4})")
_JUDGMENT_CELL_CITATION_PATTERN = re.compile(r"\b(\d{4}\s*INSC\s*\d+)\b", re.I)
_JUDGMENT_CELL_LANGUAGE_PATTERN = re.compile(r"\(([A-Za-z]+)\)")


def _parse_judgment_cell(raw: Optional[str]) -> tuple:
    if not raw:
        return None, None, None
    date_match = _JUDGMENT_CELL_DATE_PATTERN.search(raw)
    citation_match = _JUDGMENT_CELL_CITATION_PATTERN.search(raw)
    languages = _JUDGMENT_CELL_LANGUAGE_PATTERN.findall(raw)
    return (
        date_match.group(1) if date_match else None,
        citation_match.group(1) if citation_match else None,
        languages[-1].title() if languages else None,
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
        batches.append(
            (current.strftime("%d-%m-%Y"), batch_end.strftime("%d-%m-%Y"))
        )
        current = batch_end + timedelta(days=1)
    return batches


def _solve_and_submit_captcha(page, from_input, to_input, batch_from: str, batch_to: str) -> bool:
    from_input.fill(batch_from)
    to_input.fill(batch_to)

    captcha_input = page.locator(
        "#siwp_captcha_value_0, input[name='siwp_captcha_value']"
    )
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
                return False

        refresh_button = page.locator("a[title='Refresh Image']")
        if refresh_button.count() > 0:
            refresh_button.first.click()
            page.wait_for_timeout(1500)

    return False


def _scrape_one_batch(client, batch_from: str, batch_to: str, download_dir: Path) -> Iterator[RawJudgmentRecord]:
    logger.info("[SCRAPE] %s -> %s: loading SCI search page", batch_from, batch_to)
    client.open_search_page()
    page = client.page

    from_input = page.locator("#from_date")
    to_input = page.locator("#to_date")
    if from_input.count() == 0 or to_input.count() == 0:
        raise SourceStructureChangedError(
            "SCI date input fields #from_date / #to_date were not found."
        )

    if not _solve_and_submit_captcha(page, from_input, to_input, batch_from, batch_to):
        logger.info("[SCRAPE] %s -> %s: no records or CAPTCHA was not solved", batch_from, batch_to)
        return

    headers = [
        _clean_text(h.inner_text())
        for h in page.locator(".distTableContent table thead th").all()
    ]
    logger.info("[SCRAPE] SCI results headers: %s", headers)

    while True:
        rows = page.locator(".distTableContent table tbody tr")

        for row_index in range(rows.count()):
            row = rows.nth(row_index)
            cells = row.locator("td")
            if cells.count() == 0:
                continue

            data = {}
            for col_index, cell in enumerate(cells.all()):
                header = headers[col_index] if col_index < len(headers) else f"col_{col_index}"
                data[header] = _clean_text(cell.inner_text())

            pdf_links = [
                urljoin(BASE_URL, href)
                for href in [
                    a.get_attribute("href")
                    for a in row.locator("a").all()
                ]
                if href
            ]
            if not pdf_links:
                continue

            pdf_path = download_dir / f"{abs(hash(pdf_links[0]))}.pdf"
            if not client.download_pdf(pdf_links[0], pdf_path):
                logger.warning("[SCRAPE] PDF download failed: %s", pdf_links[0])
                continue

            decision_date_raw, neutral_citation_raw, language = _parse_judgment_cell(
                data.get("Judgment")
            )

            yield RawJudgmentRecord(
                pdf_path=pdf_path,
                source_url=pdf_links[0],
                case_number_raw=data.get("Case Number"),
                party_name_raw=data.get("Petitioner / Respondent"),
                judge_raw=data.get("Judgment By") or data.get("Bench"),
                decision_date_raw=decision_date_raw,
                cnr_raw=data.get("Diary Number"),
                neutral_citation_raw=neutral_citation_raw,
                extra={
                    "advocate_raw": data.get("Petitioner/Respondent Advocate"),
                    "bench_raw": data.get("Bench"),
                    "language": language,
                },
            )

        next_button = page.locator("#paginationHtml a:has-text('Next')")
        if next_button.count() == 0 or not next_button.first.is_visible():
            break

        try:
            next_button.first.click()
            page.wait_for_timeout(3000)
        except PlaywrightTimeoutError:
            break


class SupremeCourtAdapter:
    """ScraperAdapter implementation for sci.gov.in."""

    def scrape(
        self,
        date_from: str,
        date_to: str,
        headless: bool = True,
        source_delay_seconds: float = 2.0,
        **kwargs,
    ) -> Iterator[RawJudgmentRecord]:
        batches = _split_into_batches(date_from, date_to)

        with tempfile.TemporaryDirectory(prefix="scin_pdfs_") as tmp_dir:
            download_dir = Path(tmp_dir)

            with SupremeCourtBrowserClient(
                headless=headless,
                source_delay_seconds=source_delay_seconds,
            ) as client:
                client.check_access()

                for index, (batch_from, batch_to) in enumerate(batches):
                    if index:
                        time.sleep(source_delay_seconds)

                    yield from _scrape_one_batch(
                        client,
                        batch_from,
                        batch_to,
                        download_dir,
                    )
