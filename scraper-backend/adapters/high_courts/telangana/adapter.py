from __future__ import annotations

import logging
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from adapters.base import (
    JUDGMENT_DOWNLOAD_FAILED,
    JUDGMENT_NOT_PUBLISHED,
    BatchItem,
    ItemOutcome,
    RawJudgmentRecord,
    SourceUnavailableError,
)
from adapters.high_courts.telangana import csis, ecourts, ehcr, extraction, stages
from db import court_config, scrape_jobs
from orchestrator.log_context import slog

logger = logging.getLogger("scraper_backend_v2.tshc_adapter")

PDF_CAPTURE_WAIT_SECONDS = 25
NETWORK_RETRY_DELAYS_SECONDS = (5, 15, 30)


@dataclass
class _Session:
    context: object
    page: object
    download_dir: Path
    csis_client: csis.CSISClient


def _launch_browser(playwright, headless: bool):
    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context(accept_downloads=True, ignore_https_errors=True)
    return browser, context, context.new_page()


def _is_network_error(exc: Exception) -> bool:
    return isinstance(exc, PlaywrightTimeoutError) or (
        isinstance(exc, PlaywrightError) and "net::ERR_" in str(exc)
    )


def _download_pdf(context, page, pdf_url: str, download_dir: Path) -> Tuple[Optional[Path], Optional[str]]:
    local_path = download_dir / f"{abs(hash(pdf_url))}.pdf"
    method = "direct fetch"
    headers = {"Referer": "https://hcservices.ecourts.gov.in/hcservices/main.php"}
    try:
        response = context.request.get(pdf_url, headers=headers, timeout=30000, ignore_https_errors=True)
        if not response.ok:
            return None, f"HTTP {response.status}"
        local_path.write_bytes(response.body())
    except Exception as e:
        slog(logger, stages.JUDGMENT, "debug", "direct fetch failed for %s (%s), trying browser download", pdf_url, e)

        method = "browser download"
        try:
            with page.expect_download(timeout=PDF_CAPTURE_WAIT_SECONDS * 1000) as download_info:
                try:
                    page.goto(pdf_url, timeout=30000)
                except Exception:
                    pass
            download_info.value.save_as(str(local_path))
        except Exception as dl_err:
            return None, str(dl_err)

    content = local_path.read_bytes() if local_path.exists() else b""
    header_at = content.find(b"%PDF", 0, 1024)
    if header_at == -1:
        snippet = " ".join(content[:200].decode("utf-8", "replace").split())
        return None, f"response is not a PDF · {len(content)} bytes · body starts: {snippet!r}"
    if header_at:
        local_path.write_bytes(content[header_at:])
    slog(logger, stages.JUDGMENT, "info", "PDF downloaded (%d KB, via %s)", local_path.stat().st_size // 1024, method)
    return local_path, None


class TelanganaHighCourtAdapter:
    """Implements ResumableScraperAdapter for Telangana High Court (TSHC)."""

    def discover(self, date_from: str, date_to: str, headless: bool = True, **kwargs) -> List[BatchItem]:
        court_id = court_config.get_court_id_by_code("TSHC")
        already_promoted = scrape_jobs.get_promoted_case_numbers(court_id) if court_id is not None else set()

        slog(logger, stages.DISCOVER, "info", "Discovering reportable cases from EHCR (%s → %s)", date_from, date_to)
        client = ehcr.EHCRClient()
        records = client.search_with_auto_captcha(date_from, date_to)
        slog(logger, stages.DISCOVER, "info", "EHCR returned %d reported records", len(records))

        items: Dict[str, BatchItem] = {}
        for rec in records:
            label = rec.case_number_raw or f"{rec.case_type} {rec.case_number}/{rec.case_year}"
            label = label.strip()
            if label not in items:
                items[label] = BatchItem(
                    key=label,
                    payload=rec.to_dict(),
                    done_reason="already in database" if label in already_promoted else None,
                )
        return list(items.values())

    @contextmanager
    def session(self, headless: bool = True, **kwargs) -> Iterator[_Session]:
        with tempfile.TemporaryDirectory(prefix="tshc_pdfs_") as tmp_dir:
            with sync_playwright() as playwright:
                browser, context, page = _launch_browser(playwright, headless)
                csis_client = csis.CSISClient()
                try:
                    csis_client.open()
                except Exception as e:
                    logger.warning("Initial CSIS open warning: %s", e)

                try:
                    yield _Session(
                        context=context,
                        page=page,
                        download_dir=Path(tmp_dir),
                        csis_client=csis_client,
                    )
                finally:
                    browser.close()

    def process_item(self, session: _Session, item: BatchItem) -> ItemOutcome:
        return self._process_candidate_with_retry(
            session.context,
            session.page,
            session.csis_client,
            item.payload,
            session.download_dir,
        )

    def _process_candidate_with_retry(
        self,
        context,
        page,
        csis_client: csis.CSISClient,
        payload: Dict[str, object],
        download_dir: Path,
    ) -> ItemOutcome:
        case_label = payload.get("case_number_raw") or "Unknown"
        for attempt, delay in enumerate((*NETWORK_RETRY_DELAYS_SECONDS, None), start=1):
            try:
                return self._process_candidate(context, page, csis_client, payload, download_dir)
            except Exception as e:
                if not _is_network_error(e):
                    raise
                if delay is None:
                    raise SourceUnavailableError(
                        f"TSHC sources unreachable after {attempt} attempts on {case_label}: {e}"
                    ) from e
                slog(
                    logger, stages.CSIS, "warning",
                    "Network error (attempt %d of %d) · retrying in %ds: %s",
                    attempt, len(NETWORK_RETRY_DELAYS_SECONDS) + 1, delay, e,
                )
                time.sleep(delay)

    def _process_candidate(
        self,
        context,
        page,
        csis_client: csis.CSISClient,
        payload: Dict[str, object],
        download_dir: Path,
    ) -> ItemOutcome:
        case_type_str = extraction.normalize_case_type(str(payload.get("case_type") or ""))
        case_no = extraction.normalize_case_no(str(payload.get("case_number") or ""))
        case_year_str = str(payload.get("case_year") or "").strip()
        case_label = payload.get("case_number_raw") or f"{case_type_str} {case_no}/{case_year_str}"

        if not case_year_str or not case_year_str.isdigit():
            slog(logger, stages.CSIS, "warning", "Invalid year for %s → skipped", case_label)
            return ItemOutcome(skip_reason="invalid case year")

        case_year = int(case_year_str)

        # 1. Resolve case type against CSIS
        type_map = csis_client.get_case_type_map()
        case_type_id = type_map.get(case_type_str)
        if not case_type_id:
            slog(logger, stages.CSIS, "warning", "Unknown case type %r in CSIS → skipped", case_type_str)
            return ItemOutcome(skip_reason=f"unknown case type {case_type_str}")

        # 2. CSIS lookup
        slog(logger, stages.CSIS, "info", "Searching CSIS for %s", case_label)
        csis_res = csis_client.fetch_case_details(
            case_type=case_type_id,
            case_number=case_no,
            case_year=case_year,
        )

        if not csis_res or not csis_res.cnr:
            slog(logger, stages.CSIS, "warning", "Case not found or CNR missing on CSIS for %s", case_label)
            return ItemOutcome(skip_reason="not found on CSIS or CNR missing")

        cnr = csis_res.cnr.strip()
        slog(logger, stages.CSIS, "info", "Found CNR %s for %s", cnr, case_label)

        # 3. Build preliminary record
        record = RawJudgmentRecord(
            pdf_path=None,
            source_url=None,
            case_number_raw=case_label,
            decision_date_raw=payload.get("order_date"),
            cnr_raw=cnr,
            neutral_citation_raw=None,
            extra={
                **payload,
                "cnr": cnr,
                "primary": csis_res.primary,
                "order_details": csis_res.order_details,
                "raw_csis": csis_res.raw_response,
            },
        )

        # 4. Search eCourts using CNR
        slog(logger, stages.ECOURTS, "info", "Searching eCourts for CNR %s", cnr)
        pdf_url, _ = ecourts.search_cnr_and_get_pdf_url(page, cnr)

        if not pdf_url:
            slog(logger, stages.JUDGMENT, "warning", "No PDF found on eCourts for CNR %s → saving metadata without judgment", cnr)
            return ItemOutcome(record=record, judgment_missing=(JUDGMENT_NOT_PUBLISHED, "no PDF found on eCourts"))

        # 5. Download PDF
        slog(logger, stages.JUDGMENT, "info", "Downloading PDF from %s", pdf_url)
        pdf_path, failure = _download_pdf(context, page, pdf_url, download_dir)
        if pdf_path is None:
            slog(logger, stages.JUDGMENT, "warning", "PDF download failed (%s) → saving metadata", failure)
            record.source_url = pdf_url
            return ItemOutcome(record=record, judgment_missing=(JUDGMENT_DOWNLOAD_FAILED, f"PDF download failed: {failure}"[:500]))

        record.pdf_path = pdf_path
        record.source_url = pdf_url
        return ItemOutcome(record=record)
