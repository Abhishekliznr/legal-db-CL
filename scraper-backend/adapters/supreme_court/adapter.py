import logging
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from adapters.base import (
    JUDGMENT_DOWNLOAD_FAILED,
    JUDGMENT_NOT_PUBLISHED,
    BatchItem,
    ItemOutcome,
    RawJudgmentRecord,
    SourceStructureChangedError,
    SourceUnavailableError,
)
from adapters.captcha_ocr import solve_captcha_image
from adapters.supreme_court import stages
from db import court_config, scrape_jobs
from orchestrator.log_context import slog, tally

from .client import BASE_URL, SupremeCourtBrowserClient, download_pdf

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


def _solve_and_submit_captcha(page, from_input, to_input, batch_from: str, batch_to: str) -> Tuple[bool, Optional[int]]:
    """Returns (has_results, attempt): (True, N) results table shown, (False, N) "No Records Found", (False, None) captcha never solved."""
    from_input.fill(batch_from)
    to_input.fill(batch_to)

    captcha_input = page.locator(
        "#siwp_captcha_value_0, input[name='siwp_captcha_value']"
    )
    submit_button = page.locator("input[type='submit'][name='submit']")
    captcha_image = page.locator("#siwp_captcha_image_0, .siwp_captcha_image")

    for attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
        if captcha_image.count() == 0 or not captcha_image.first.is_visible():
            slog(logger, stages.DISCOVER, "debug", "captcha image not visible on attempt %d", attempt)
            return False, None

        solved_value = solve_captcha_image(captcha_image.first.screenshot())
        if solved_value:
            captcha_input.fill(solved_value)
            submit_button.click()
            page.wait_for_timeout(4000)

            if page.locator(".distTableContent table tbody tr").count() > 0:
                return True, attempt
            if page.locator(":has-text('No Records Found')").count() > 0:
                return False, attempt
            slog(logger, stages.DISCOVER, "debug", "captcha attempt %d/%d rejected, retrying", attempt, MAX_CAPTCHA_RETRIES)

        refresh_button = page.locator("a[title='Refresh Image']")
        if refresh_button.count() > 0:
            refresh_button.first.click()
            page.wait_for_timeout(1500)

    return False, None


def _read_row(row, headers: List[Optional[str]]) -> Optional[Dict[str, Any]]:
    """One results-table row as a JSON-serialisable cr_batch_items payload, None for a row with no cells."""
    cells = row.locator("td")
    if cells.count() == 0:
        return None

    data = {}
    for col_index, cell in enumerate(cells.all()):
        header = headers[col_index] if col_index < len(headers) else f"col_{col_index}"
        data[header] = _clean_text(cell.inner_text())

    pdf_links = [
        urljoin(BASE_URL, href)
        for href in [a.get_attribute("href") for a in row.locator("a").all()]
        if href
    ]
    decision_date_raw, neutral_citation_raw, language = _parse_judgment_cell(data.get("Judgment"))

    return {
        "pdf_url": pdf_links[0] if pdf_links else None,
        "case_number": data.get("Case Number"),
        "diary_number": data.get("Diary Number"),
        "party_name": data.get("Petitioner / Respondent"),
        "judge": data.get("Judgment By") or data.get("Bench"),
        "decision_date": decision_date_raw,
        "neutral_citation": neutral_citation_raw,
        "advocate_raw": data.get("Petitioner/Respondent Advocate"),
        "bench_raw": data.get("Bench"),
        "language": language,
    }


def _discover_window(client, batch_from: str, batch_to: str, window: int, windows: int) -> List[Dict[str, Any]]:
    slog(
        logger, stages.DISCOVER, "info",
        "Searching sci.gov.in judgments %s → %s (window %d of %d)", batch_from, batch_to, window, windows,
    )
    client.open_search_page()
    page = client.page

    from_input = page.locator("#from_date")
    to_input = page.locator("#to_date")
    if from_input.count() == 0 or to_input.count() == 0:
        raise SourceStructureChangedError(
            "SCI date input fields #from_date / #to_date were not found."
        )

    has_results, attempt = _solve_and_submit_captcha(page, from_input, to_input, batch_from, batch_to)
    if attempt is None:
        # Raising (not returning []) keeps a captcha failure from looking like "no judgments"
        # and stops the batch before its case list is saved, so Resume re-runs discovery.
        raise SourceUnavailableError(
            f"Could not solve the sci.gov.in captcha for {batch_from} → {batch_to} "
            f"after {MAX_CAPTCHA_RETRIES} attempts"
        )
    slog(logger, stages.DISCOVER, "info", "Captcha solved (attempt %d of %d)", attempt, MAX_CAPTCHA_RETRIES)
    if not has_results:
        slog(logger, stages.DISCOVER, "info", "No judgments in this window")
        return []

    headers = [
        _clean_text(h.inner_text())
        for h in page.locator(".distTableContent table thead th").all()
    ]
    slog(logger, stages.DISCOVER, "debug", "SCI results headers: %s", headers)

    found: List[Dict[str, Any]] = []
    page_number = 1
    while True:
        rows = page.locator(".distTableContent table tbody tr")
        page_rows = [r for r in (_read_row(rows.nth(i), headers) for i in range(rows.count())) if r is not None]
        slog(logger, stages.DISCOVER, "info", "Page %d · %d judgment rows", page_number, len(page_rows))
        found.extend(page_rows)

        next_button = page.locator("#paginationHtml a:has-text('Next')")
        if next_button.count() == 0 or not next_button.first.is_visible():
            break

        try:
            next_button.first.click()
            page.wait_for_timeout(3000)
        except PlaywrightTimeoutError:
            slog(logger, stages.DISCOVER, "warning", "Next page didn't load after page %d · keeping rows read so far", page_number)
            break
        page_number += 1

    slog(logger, stages.DISCOVER, "info", "%d judgment row(s) found in this window", len(found))
    return found


def _item_key(row: Dict[str, Any]) -> Optional[str]:
    # Case Number is what promotion stores as cr_cases.case_number, so the
    # "already in database" skip matches on it.
    return row.get("case_number") or row.get("diary_number") or row.get("pdf_url")


@dataclass
class _Session:
    download_dir: Path


class SupremeCourtAdapter:
    """Implements ResumableScraperAdapter (adapters/base.py) for sci.gov.in: one item per judgment row."""

    def discover(
        self,
        date_from: str,
        date_to: str,
        headless: bool = True,
        source_delay_seconds: float = 2.0,
        **kwargs,
    ) -> List[BatchItem]:
        windows = _split_into_batches(date_from, date_to)
        court_id = court_config.get_court_id_by_code("SCIN")
        already_promoted = scrape_jobs.get_promoted_case_numbers(court_id) if court_id is not None else set()

        rows: List[Dict[str, Any]] = []
        with SupremeCourtBrowserClient(
            headless=headless,
            source_delay_seconds=source_delay_seconds,
        ) as client:
            client.check_access()
            for index, (window_from, window_to) in enumerate(windows, start=1):
                if index > 1:
                    time.sleep(source_delay_seconds)
                rows.extend(_discover_window(client, window_from, window_to, index, len(windows)))

        items: Dict[str, BatchItem] = {}
        unidentified = repeated = 0
        for row in rows:
            key = _item_key(row)
            if key is None:
                unidentified += 1
                continue
            if key in items:
                repeated += 1
                continue
            items[key] = BatchItem(
                key=key,
                payload=row,
                done_reason="already in database" if key in already_promoted else None,
            )

        if unidentified:
            slog(logger, stages.DISCOVER, "warning", "%d row(s) with no case number, diary number or PDF link → skipped", unidentified)
            tally("Skipped", "unidentifiable row", unidentified)
        if repeated:
            slog(logger, stages.DISCOVER, "info", "%d row(s) repeat a case already listed → skipped", repeated)
            tally("Skipped", "repeated case row", repeated)
        slog(logger, stages.DISCOVER, "info", "%d judgment row(s) found across %d window(s)", len(rows), len(windows))
        return list(items.values())

    @contextmanager
    def session(self, **kwargs) -> Iterator[_Session]:
        with tempfile.TemporaryDirectory(prefix="scin_pdfs_") as tmp_dir:
            yield _Session(download_dir=Path(tmp_dir))

    def process_item(self, session: _Session, item: BatchItem) -> ItemOutcome:
        row = item.payload
        pdf_url = row.get("pdf_url")
        record = _record_from_row(row)
        if not pdf_url:
            slog(logger, stages.JUDGMENT, "warning", "No judgment PDF link in row → saving case details without judgment")
            return ItemOutcome(record=record, judgment_missing=(JUDGMENT_NOT_PUBLISHED, "no PDF link"))

        pdf_path = session.download_dir / f"{abs(hash(pdf_url))}.pdf"
        failure = download_pdf(pdf_url, pdf_path)
        if failure is not None:
            slog(logger, stages.JUDGMENT, "warning", "PDF download failed (%s) → saving case details without judgment", failure)
            record.source_url = pdf_url
            return ItemOutcome(record=record, judgment_missing=(JUDGMENT_DOWNLOAD_FAILED, f"PDF download failed: {failure}"[:500]))
        slog(logger, stages.JUDGMENT, "info", "PDF downloaded (%d KB)", pdf_path.stat().st_size // 1024)

        record.pdf_path, record.source_url = pdf_path, pdf_url
        return ItemOutcome(record=record)


def _record_from_row(row: Dict[str, Any]) -> RawJudgmentRecord:
    return RawJudgmentRecord(
        pdf_path=None,
        source_url=None,
        case_number_raw=row.get("case_number"),
        party_name_raw=row.get("party_name"),
        judge_raw=row.get("judge"),
        decision_date_raw=row.get("decision_date"),
        cnr_raw=row.get("diary_number"),
        neutral_citation_raw=row.get("neutral_citation"),
        extra={
            "advocate_raw": row.get("advocate_raw"),
            "bench_raw": row.get("bench_raw"),
            "language": row.get("language"),
        },
    )
