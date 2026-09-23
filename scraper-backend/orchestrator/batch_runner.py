import hashlib
import logging
from typing import Callable, Optional

from adapters.base import (
    RawJudgmentRecord,
    ScraperAdapter,
    SourceAccessError,
    SourceRateLimitError,
    SourceStructureChangedError,
    SourceUnavailableError,
)
from db import scrape_jobs
from orchestrator import live_logs, log_context
from pipeline import llm_enrichment, ocr
from storage import azure_blob

PromoteFn = Callable[[int, RawJudgmentRecord], Optional[int]]
FindProvisionsFn = Callable[[str], str]

logger = logging.getLogger("scraper_backend_v2.orchestrator")


def _checksum_file(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _log(level: str, msg: str, *args) -> None:
    log_context.plog(logger, level, msg, *args)


def _source_failure_status(exc: Exception) -> str:
    # Subclasses before SourceAccessError, which both of them inherit from.
    if isinstance(exc, SourceRateLimitError):
        return "RATE_LIMITED"
    if isinstance(exc, SourceUnavailableError):
        return "SOURCE_UNAVAILABLE"
    if isinstance(exc, SourceAccessError):
        return "SOURCE_BLOCKED"
    if isinstance(exc, SourceStructureChangedError):
        return "STRUCTURE_CHANGED"
    return "FAILED"


def run_batch(
    adapter: ScraperAdapter,
    promote_fn: PromoteFn,
    batch_id: int,
    court_id: int,
    court_code: str,
    date_from: str,
    date_to: str,
    data_source: str,
    find_provisions_fn: Optional[FindProvisionsFn] = None,
    run_enrichment: bool = True,
    **adapter_kwargs,
) -> dict:
    """
    `promote_fn` and `find_provisions_fn` are this court's own extraction/
    promotion pipeline (e.g. adapters.supreme_court.promotion.promote_ingestion
    and adapters.supreme_court.extraction.find_provision_paragraphs) —
    resolved by the caller (routers/scraper_router.py's adapter registry)
    the same way `adapter` already is, so this orchestrator stays entirely
    court-agnostic. `find_provisions_fn` is optional: omit it for a court
    whose extraction module doesn't have one yet — enrichment simply runs
    without a provision-paragraph block in that case (see
    pipeline.llm_enrichment.enrich_case's own docstring).

    `run_enrichment=False` skips the pipeline.llm_enrichment.enrich_case(...)
    call entirely for every record in this batch — promotion still runs and
    the case is still written, there's just no LLM enrichment pass. This is
    the on/off switch for a court whose pipeline doesn't use LLM enrichment
    yet (e.g. Madhya Pradesh, as of this writing) without needing any
    pipeline.llm_enrichment.py changes to turn it on later.
    """
    live_logs.start_batch(batch_id)

    with log_context.scope(batch_id, court_code):
        _log(
            "info",
            "[BATCH %s] started: court_id=%s %s -> %s (data_source=%s)",
            batch_id,
            court_id,
            date_from,
            date_to,
            data_source,
        )

        total_found = 0
        total_downloaded = 0
        total_promoted = 0
        total_skipped_duplicate = 0
        total_needs_review = 0
        total_errored = 0
        cancelled = False

        try:
            for record in adapter.scrape(date_from, date_to, **adapter_kwargs):
                if scrape_jobs.is_cancel_requested(batch_id):
                    _log(
                        "warning",
                        "[BATCH %s] cancel requested — stopping after %s record(s)",
                        batch_id,
                        total_found,
                    )
                    cancelled = True
                    break

                total_found += 1
                case_label = record.case_number_raw or record.source_url

                try:
                    ingestion_id = _ingest_one_record(
                        record, batch_id, court_id, court_code, data_source
                    )
                except Exception:
                    _log(
                        "exception",
                        "[BATCH %s] [INGEST] %s: failed to ingest",
                        batch_id,
                        case_label,
                    )
                    total_errored += 1
                    continue

                if ingestion_id is None:
                    _log(
                        "info",
                        "[BATCH %s] [INGEST] %s: duplicate — skipped",
                        batch_id,
                        case_label,
                    )
                    total_skipped_duplicate += 1
                    continue

                total_downloaded += 1

                try:
                    case_id = _run_pipeline_for_one(batch_id, ingestion_id, record, promote_fn, find_provisions_fn, run_enrichment)
                except Exception:
                    _log(
                        "exception",
                        "[BATCH %s] [PIPELINE] ingestion_id=%s: unhandled exception",
                        batch_id,
                        ingestion_id,
                    )
                    scrape_jobs.update_status(
                        ingestion_id,
                        status="PROMOTION_FAILED",
                        error_message="Unhandled pipeline exception — see server logs",
                    )
                    total_errored += 1
                    continue

                if case_id is not None:
                    total_promoted += 1
                else:
                    final_status = scrape_jobs.get_ingestion(ingestion_id)["status"]
                    if final_status == "NEEDS_REVIEW":
                        total_needs_review += 1
                    else:
                        total_errored += 1

            final_status = "CANCELLED" if cancelled else "COMPLETED"
            scrape_jobs.finish_batch(
                batch_id,
                status=final_status,
                total_found=total_found,
                total_downloaded=total_downloaded,
                total_promoted=total_promoted,
            )

        except (
            SourceAccessError,
            SourceRateLimitError,
            SourceUnavailableError,
            SourceStructureChangedError,
        ) as exc:
            status = _source_failure_status(exc)
            _log("error", "[BATCH %s] source failure: %s", batch_id, exc)
            scrape_jobs.finish_batch(
                batch_id,
                status=status,
                total_found=total_found,
                total_downloaded=total_downloaded,
                total_promoted=total_promoted,
                error_message=str(exc),
            )
            live_logs.finish_batch(batch_id)

            return {
                "batch_id": batch_id,
                "status": status,
                "error": str(exc),
                "total_found": total_found,
                "total_downloaded": total_downloaded,
                "total_promoted": total_promoted,
                "total_skipped_duplicate": total_skipped_duplicate,
                "total_needs_review": total_needs_review,
                "total_errored": total_errored,
                "cancelled": cancelled,
            }

        except Exception:
            _log("exception", "[BATCH %s] unexpected adapter-level failure", batch_id)
            scrape_jobs.finish_batch(
                batch_id,
                status="FAILED",
                total_found=total_found,
                total_downloaded=total_downloaded,
                total_promoted=total_promoted,
            )
            live_logs.finish_batch(batch_id)
            raise

        summary = {
            "batch_id": batch_id,
            "status": "CANCELLED" if cancelled else "COMPLETED",
            "total_found": total_found,
            "total_downloaded": total_downloaded,
            "total_skipped_duplicate": total_skipped_duplicate,
            "total_promoted": total_promoted,
            "total_needs_review": total_needs_review,
            "total_errored": total_errored,
            "cancelled": cancelled,
        }
        _log("info", "[BATCH %s] finished: %s", batch_id, summary)
        live_logs.finish_batch(batch_id)
        return summary


def _ingest_one_record(
    record: RawJudgmentRecord,
    batch_id: int,
    court_id: int,
    court_code: str,
    data_source: str,
) -> Optional[int]:
    checksum = _checksum_file(record.pdf_path)

    blob_pdf_id = None
    if azure_blob.is_configured():
        blob_pdf_id = azure_blob.upload_pdf(
            record.pdf_path,
            blob_name=f"{court_code}/{checksum}.pdf",
        )

    return scrape_jobs.insert_raw_ingestion(
        batch_id=batch_id,
        court_id=court_id,
        source_pdf_url=record.source_url,
        file_checksum=checksum,
        data_source=data_source,
        blob_pdf_id=blob_pdf_id,
    )


def _run_pipeline_for_one(
    batch_id: int,
    ingestion_id: int,
    record: RawJudgmentRecord,
    promote_fn: PromoteFn,
    find_provisions_fn: Optional[FindProvisionsFn],
    run_enrichment: bool,
) -> Optional[int]:
    ocr.process_ingestion_ocr(ingestion_id, record.pdf_path)

    ingestion = scrape_jobs.get_ingestion(ingestion_id)
    if ingestion["status"] != "OCR_DONE":
        return None

    case_id = promote_fn(ingestion_id, record)

    if case_id is not None and run_enrichment:
        provision_block = ""
        if find_provisions_fn is not None:
            try:
                provision_block = find_provisions_fn(ingestion["ocr_text"] or "")
            except Exception:
                _log(
                    "exception",
                    "[ENRICH] case_id=%s: find_provisions_fn raised; enriching without a provision block",
                    case_id,
                )
        try:
            llm_enrichment.enrich_case(case_id, provision_block=provision_block)
            _log("info", "[ENRICH] case_id=%s: enrichment finished", case_id)
        except Exception:
            _log(
                "exception",
                "[ENRICH] case_id=%s: enrichment raised; promotion remains valid",
                case_id,
            )

    return case_id


def to_date(date_str: str):
    from datetime import datetime

    if "-" in date_str and len(date_str.split("-")[0]) == 4:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    return datetime.strptime(date_str, "%d-%m-%Y").date()
