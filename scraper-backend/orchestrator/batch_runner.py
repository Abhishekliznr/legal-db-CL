import hashlib
import logging
import time
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
from orchestrator import live_logs, log_context, stages
from pipeline import llm_enrichment, ocr
from storage import azure_blob

PromoteFn = Callable[[int, RawJudgmentRecord], Optional[int]]
FindProvisionsFn = Callable[[str], str]

logger = logging.getLogger("scraper_backend_v2.orchestrator")


def _checksum_file(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _log(stage: str, level: str, msg: str, *args) -> None:
    log_context.slog(logger, stage, level, msg, *args)


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
    started_at = time.monotonic()

    with log_context.scope(batch_id, court_code):
        _log(stages.BATCH, "info", "Started batch #%s · %s · %s → %s", batch_id, court_code, date_from, date_to)

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
                    _log(stages.BATCH, "warning", "Cancel requested — stopping after %s case(s)", total_found)
                    cancelled = True
                    break

                total_found += 1
                case_label = record.case_number_raw or record.source_url
                index, total = record.position or (None, None)

                with log_context.case_scope(case_label, index, total):
                    try:
                        ingestion_id = _ingest_one_record(
                            record, batch_id, court_id, court_code, data_source
                        )
                    except Exception:
                        _log(stages.INGEST, "exception", "Failed to save the judgment PDF")
                        total_errored += 1
                        continue

                    if ingestion_id is None:
                        _log(stages.INGEST, "info", "Same PDF already ingested earlier → skipped")
                        total_skipped_duplicate += 1
                        continue

                    total_downloaded += 1

                    try:
                        case_id = _run_pipeline_for_one(batch_id, ingestion_id, record, promote_fn, find_provisions_fn, run_enrichment)
                    except Exception:
                        _log(stages.PROMOTE, "exception", "Unhandled pipeline error on ingestion #%s", ingestion_id)
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
            _log(stages.BATCH, "error", "Source failure: %s", exc)
            _log_summary(status, started_at, total_found, total_promoted, total_needs_review, total_skipped_duplicate, total_errored)
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
            _log(stages.BATCH, "exception", "Unexpected scraper failure")
            _log_summary("FAILED", started_at, total_found, total_promoted, total_needs_review, total_skipped_duplicate, total_errored)
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
        _log_summary(summary["status"], started_at, total_found, total_promoted, total_needs_review, total_skipped_duplicate, total_errored)
        live_logs.finish_batch(batch_id)
        return summary


def _format_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def _format_counts(counts) -> str:
    return " · ".join(f"{label} {count}" for label, count in counts.items() if count)


def _log_summary(
    status: str, started_at: float, processed: int, promoted: int, needs_review: int, duplicates: int, errored: int
) -> None:
    level = "info" if status == "COMPLETED" else "warning"
    _log(stages.BATCH, level, "Finished in %s · %s", _format_duration(time.monotonic() - started_at), status.lower().replace("_", " "))

    tallies = log_context.tallies()
    if tallies.get("Skipped"):
        _log(stages.BATCH, "info", "Skipped: %s", _format_counts(tallies["Skipped"]))

    outcomes = _format_counts({"needs review": needs_review, "duplicate PDF": duplicates, "errors": errored})
    _log(stages.BATCH, "info", "Processed %d: promoted %d%s", processed, promoted, f" · {outcomes}" if outcomes else "")

    for group, counts in tallies.items():
        if group != "Skipped" and counts:
            _log(stages.BATCH, "info", "%s: %s", group, _format_counts(counts))


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

    ingestion_id = scrape_jobs.insert_raw_ingestion(
        batch_id=batch_id,
        court_id=court_id,
        source_pdf_url=record.source_url,
        file_checksum=checksum,
        data_source=data_source,
        blob_pdf_id=blob_pdf_id,
    )
    if ingestion_id is not None:
        where = "Uploaded to Azure Blob" if blob_pdf_id else "Saved (Azure Blob not configured)"
        _log(stages.INGEST, "info", "%s · ingestion #%s", where, ingestion_id)
    return ingestion_id


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
                if provision_block:
                    _log(
                        stages.PROVISIONS, "info",
                        "Regex found text citing provisions (%s chars) → sent to LLM", f"{len(provision_block):,}",
                    )
                else:
                    _log(stages.PROVISIONS, "info", "No provision references in judgment text")
            except Exception:
                _log(stages.PROVISIONS, "exception", "Provision-paragraph search failed · enriching without it")
        try:
            llm_enrichment.enrich_case(case_id, provision_block=provision_block)
        except Exception:
            _log(stages.ENRICH, "exception", "Enrichment failed for case #%s · promotion still stands", case_id)
            log_context.tally("Enrichment", "failed")

    return case_id


def to_date(date_str: str):
    from datetime import datetime

    if "-" in date_str and len(date_str.split("-")[0]) == 4:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    return datetime.strptime(date_str, "%d-%m-%Y").date()
