import hashlib
import logging
import time
from collections import Counter
from typing import Callable, Optional, Tuple

from adapters.base import (
    BatchItem,
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


class _Counts:
    __slots__ = ("processed", "downloaded", "promoted", "duplicates", "needs_review", "errored")

    def __init__(self) -> None:
        for name in self.__slots__:
            setattr(self, name, 0)


# Several cases in a row crashing means the source or browser is broken, not the
# cases -- stop (resumable) instead of marking every remaining case FAILED.
_MAX_CONSECUTIVE_ITEM_FAILURES = 5

_SOURCE_ERRORS = (SourceAccessError, SourceRateLimitError, SourceUnavailableError, SourceStructureChangedError)


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
    run_number: int = 1,
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
    the case is still written, there's just no LLM enrichment pass.

    An adapter with discover() (adapters.base.ResumableScraperAdapter) runs
    through cr_batch_items so a stopped batch can be resumed: `run_number` > 1
    is a resume of the same batch (routers/scraper_router.py's resume endpoint),
    which skips discovery when the case list was already saved. Adapters with
    only scrape() stream records as before and can't be resumed.
    """
    resumable = hasattr(adapter, "discover")
    live_logs.start_batch(batch_id)
    started_at = time.monotonic()
    counts = _Counts()

    def handle_record(record: RawJudgmentRecord) -> Tuple[str, Optional[int], Optional[str]]:
        """Ingest + OCR + promote (+ enrich) one record. Returns (batch item status, ingestion_id, reason)."""
        counts.processed += 1
        try:
            ingestion_id = _ingest_one_record(record, batch_id, court_id, court_code, data_source)
        except Exception:
            _log(stages.INGEST, "exception", "Failed to save the judgment PDF")
            counts.errored += 1
            return "FAILED", None, "couldn't save the judgment PDF"

        if ingestion_id is None:
            _log(stages.INGEST, "info", "Same PDF was already saved as a case earlier → skipped")
            counts.duplicates += 1
            return "DONE", None, "duplicate PDF"

        counts.downloaded += 1
        try:
            case_id = _run_pipeline_for_one(batch_id, ingestion_id, record, promote_fn, find_provisions_fn, run_enrichment)
        except Exception:
            _log(stages.PROMOTE, "exception", "Unhandled pipeline error on ingestion #%s", ingestion_id)
            scrape_jobs.update_status(
                ingestion_id,
                status="PROMOTION_FAILED",
                error_message="Unhandled pipeline exception — see server logs",
            )
            counts.errored += 1
            return "FAILED", ingestion_id, "unhandled pipeline error"

        if case_id is not None:
            counts.promoted += 1
            return "DONE", ingestion_id, None
        final_status = scrape_jobs.get_ingestion(ingestion_id)["status"]
        if final_status == "NEEDS_REVIEW":
            counts.needs_review += 1
            return "DONE", ingestion_id, "needs review"
        counts.errored += 1
        return "FAILED", ingestion_id, final_status.lower()

    with log_context.scope(batch_id, court_code):
        if run_number > 1:
            _log(stages.BATCH, "info", "Resumed batch #%s (run %d) · %s · %s → %s", batch_id, run_number, court_code, date_from, date_to)
        else:
            _log(stages.BATCH, "info", "Started batch #%s · %s · %s → %s", batch_id, court_code, date_from, date_to)

        error_message = None
        unexpected: Optional[Exception] = None
        try:
            if resumable:
                cancelled = _run_items(adapter, batch_id, handle_record, date_from, date_to, adapter_kwargs)
            else:
                cancelled = _run_stream(adapter, batch_id, handle_record, date_from, date_to, adapter_kwargs)
            status = "CANCELLED" if cancelled else "COMPLETED"
        except _SOURCE_ERRORS as exc:
            status, error_message = _source_failure_status(exc), str(exc)
            _log(stages.BATCH, "error", "Source failure: %s", exc)
        except Exception as exc:
            status, error_message, unexpected = "FAILED", f"{type(exc).__name__}: {exc}", exc
            _log(stages.BATCH, "exception", "Unexpected scraper failure")

        _log_summary(status, started_at, counts, batch_id if resumable else None)
        if resumable:
            totals = scrape_jobs.batch_totals_from_items(batch_id)
        else:
            totals = {"total_found": counts.processed, "total_downloaded": counts.downloaded, "total_promoted": counts.promoted}
        scrape_jobs.finish_batch(
            batch_id, status=status, error_message=error_message, run_details=_run_details(started_at, counts, batch_id if resumable else None), **totals,
        )
        live_logs.finish_batch(batch_id)

    if unexpected is not None:
        raise unexpected
    return {
        "batch_id": batch_id,
        "status": status,
        "error": error_message,
        **totals,
        "total_skipped_duplicate": counts.duplicates,
        "total_needs_review": counts.needs_review,
        "total_errored": counts.errored,
        "cancelled": status == "CANCELLED",
    }


def _run_stream(adapter: ScraperAdapter, batch_id: int, handle_record, date_from: str, date_to: str, adapter_kwargs: dict) -> bool:
    """Non-resumable adapters (scrape() only). Returns True if cancelled."""
    for record in adapter.scrape(date_from, date_to, **adapter_kwargs):
        if scrape_jobs.is_cancel_requested(batch_id):
            _log(stages.BATCH, "warning", "Cancel requested — stopping")
            return True
        index, total = record.position or (None, None)
        with log_context.case_scope(record.case_number_raw or record.source_url, index, total):
            handle_record(record)
    return False


def _run_items(adapter, batch_id: int, handle_record, date_from: str, date_to: str, adapter_kwargs: dict) -> bool:
    """Resumable adapters: discover once into cr_batch_items, then work through the open items. Returns True if cancelled."""
    discovered_earlier = scrape_jobs.is_batch_discovered(batch_id)
    if not discovered_earlier:
        items = adapter.discover(date_from, date_to, **adapter_kwargs)
        scrape_jobs.save_batch_items(
            batch_id, [{"key": i.key, "payload": i.payload, "done_reason": i.done_reason} for i in items],
        )
        done = Counter(i.done_reason for i in items if i.done_reason)
        for reason, count in done.items():
            log_context.tally("Skipped", reason, count)
        skipped = "".join(f"{count} {reason} → skipped · " for reason, count in done.items())
        _log(stages.BATCH, "info", "%s%d to process", skipped, len(items) - sum(done.values()))

    open_items = scrape_jobs.list_open_batch_items(batch_id)
    total = sum(scrape_jobs.batch_item_counts(batch_id).values())
    if discovered_earlier:
        _log(stages.BATCH, "info", "%d of %d cases left · case list already saved, skipping discovery", len(open_items), total)
    if not open_items:
        return False

    consecutive_failures = 0
    with adapter.session(**adapter_kwargs) as session:
        for done_count, row in enumerate(open_items):
            if scrape_jobs.is_cancel_requested(batch_id):
                _log(stages.BATCH, "warning", "Cancel requested — stopping · %d case(s) left to resume", len(open_items) - done_count)
                return True

            item = BatchItem(key=row["item_key"], payload=row["payload"], item_id=row["item_id"], position=row["position"])
            with log_context.case_scope(item.key, item.position, total):
                try:
                    outcome = adapter.process_item(session, item)
                except _SOURCE_ERRORS:
                    raise
                except Exception as exc:
                    _log(stages.BATCH, "exception", "Scraping this case failed: %s", exc)
                    scrape_jobs.mark_batch_item(item.item_id, "FAILED", str(exc)[:500])
                    consecutive_failures += 1
                    if consecutive_failures >= _MAX_CONSECUTIVE_ITEM_FAILURES:
                        raise RuntimeError(
                            f"{consecutive_failures} cases in a row failed — stopping so the rest can be resumed once the cause is fixed"
                        ) from exc
                    continue
                consecutive_failures = 0

                if outcome.skip_reason:
                    log_context.tally("Skipped", outcome.skip_reason)
                    scrape_jobs.mark_batch_item(item.item_id, "SKIPPED", outcome.skip_reason)
                    continue

                outcome.record.position = (item.position, total)
                item_status, ingestion_id, reason = handle_record(outcome.record)
                scrape_jobs.mark_batch_item(item.item_id, item_status, reason, ingestion_id)
    return False


def _format_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def _format_counts(counts) -> str:
    return " · ".join(f"{label} {count}" for label, count in counts.items() if count)


def _run_details(started_at: float, counts: _Counts, resumable_batch_id: Optional[int]) -> dict:
    """This run's numbers for its finish event on the batch timeline (cr_batch_events.details)."""
    details = {"duration_seconds": int(time.monotonic() - started_at)}
    if resumable_batch_id is not None:
        item_counts = scrape_jobs.batch_item_counts(resumable_batch_id)
        details["cases_left"] = item_counts.get("PENDING", 0) + item_counts.get("FAILED", 0)
    details.update({name: getattr(counts, name) for name in _Counts.__slots__})
    skipped = log_context.tallies().get("Skipped")
    if skipped:
        details["skipped"] = dict(skipped)
    return details


def _log_summary(status: str, started_at: float, counts: _Counts, resumable_batch_id: Optional[int]) -> None:
    level = "info" if status == "COMPLETED" else "warning"
    _log(stages.BATCH, level, "Finished in %s · %s", _format_duration(time.monotonic() - started_at), status.lower().replace("_", " "))

    tallies = log_context.tallies()
    if tallies.get("Skipped"):
        _log(stages.BATCH, "info", "Skipped: %s", _format_counts(tallies["Skipped"]))

    outcomes = _format_counts({"needs review": counts.needs_review, "duplicate PDF": counts.duplicates, "errors": counts.errored})
    _log(stages.BATCH, "info", "Processed %d: promoted %d%s", counts.processed, counts.promoted, f" · {outcomes}" if outcomes else "")

    for group, group_counts in tallies.items():
        if group != "Skipped" and group_counts:
            _log(stages.BATCH, "info", "%s: %s", group, _format_counts(group_counts))

    if resumable_batch_id is None:
        return
    item_counts = scrape_jobs.batch_item_counts(resumable_batch_id)
    if not item_counts:
        return
    overall = {status_name.lower(): item_counts.get(status_name, 0) for status_name in ("DONE", "SKIPPED", "FAILED", "PENDING")}
    _log(stages.BATCH, "info", "Overall: %d cases · %s", sum(item_counts.values()), _format_counts(overall))
    left = overall["pending"] + overall["failed"]
    if left:
        _log(stages.BATCH, "warning", "%d case(s) left · use Resume to continue from here", left)


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

    ingestion_id, reused = scrape_jobs.insert_raw_ingestion(
        batch_id=batch_id,
        court_id=court_id,
        source_pdf_url=record.source_url,
        file_checksum=checksum,
        data_source=data_source,
        blob_pdf_id=blob_pdf_id,
    )
    if reused:
        _log(stages.INGEST, "info", "Same PDF was left unfinished by an earlier run · re-processing ingestion #%s", ingestion_id)
    elif ingestion_id is not None:
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
