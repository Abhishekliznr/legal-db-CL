"""
Owns every scrape_batches/raw_ingestions write (spec §4.4).

Adapters are dumb (scrape + download only, adapters/base.py); this module is
where checksum dedup, blob upload, and DB writes actually happen — so a bug
in one court's scraping logic can never corrupt job-tracking state, and the
same orchestration code runs unchanged regardless of which adapter produced
the record.

Phase 1 runs OCR -> extraction -> promotion synchronously, one record at a
time, right after each download — simplest thing that proves the pipeline
shape end-to-end. Decoupling these into independently-scheduled workers
(so a burst of downloads doesn't block on LLM latency) is real future work,
not done here — see spec §4.4's "separate always-running worker" note.
"""

import hashlib
import logging
from typing import Optional

from adapters.base import RawJudgmentRecord, ScraperAdapter
from db import scrape_jobs
from orchestrator import live_logs
from pipeline import llm_enrichment, ocr, promotion
from storage import azure_blob

logger = logging.getLogger("scraper_backend_v2.orchestrator")


def _checksum_file(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _log(batch_id: int, level: str, msg: str, *args) -> None:
    """
    Logs to the normal server logger AND, in the same call, to the in-memory
    live-logs buffer GET /api/scraper/batches/{id}/logs/stream reads from
    (orchestrator/live_logs.py) — one call site so the two never drift.
    """
    formatted = msg % args if args else msg
    getattr(logger, level)(formatted)
    live_logs.log(batch_id, level, formatted)


def run_batch(
    adapter: ScraperAdapter,
    batch_id: int,
    court_id: int,
    court_code: str,
    date_from: str,
    date_to: str,
    data_source: str,
    **adapter_kwargs,
) -> dict:
    """
    Runs one full scrape+ingest batch for a single court and date range,
    against a `cr_scrape_batches` row the CALLER already created (see
    routers/scraper_router.py's start_scrape — it creates the row
    synchronously, before dispatching this function as a background task,
    specifically so POST /api/scraper/start can hand the batch_id back to
    the caller immediately instead of it only coming into existence once
    this background task happens to start running).

    Returns a summary dict — total records seen, new vs. deduped, promoted
    vs. needs-review counts — for the caller (scraper_router, or a script)
    to log or return to whoever triggered it.

    data_source (one of data_source_enum's values, e.g. 'SCI_WEBSITE' for
    the Supreme Court adapter or 'ECOURTS' for the eCourts one) is the
    caller's responsibility to supply — this function has no way to infer
    it from `adapter` alone without an isinstance check coupling the
    orchestrator to specific adapter classes, which the ScraperAdapter
    protocol is deliberately designed to avoid.
    """
    live_logs.start_batch(batch_id)
    _log(batch_id, "info", "[BATCH %s] started: court_id=%s %s -> %s (data_source=%s)", batch_id, court_id, date_from, date_to, data_source)

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
                _log(batch_id, "warning", "[BATCH %s] cancel requested — stopping after %s record(s) found", batch_id, total_found)
                cancelled = True
                break

            total_found += 1
            case_label = record.case_number_raw or record.source_url
            try:
                ingestion_id = _ingest_one_record(record, batch_id, court_id, court_code, data_source)
            except Exception:
                # Checksum/blob-upload failure for this one record — the
                # adapter's generator is still good, so keep going rather
                # than losing every record after this one in the batch.
                logger.exception("[BATCH %s] [INGEST] %s: failed to ingest (source_url=%s)", batch_id, case_label, record.source_url)
                live_logs.log(batch_id, "error", f"[BATCH {batch_id}] [INGEST] {case_label}: failed to ingest (source_url={record.source_url})")
                total_errored += 1
                continue

            if ingestion_id is None:
                _log(batch_id, "info", "[BATCH %s] [INGEST] %s: duplicate (checksum already seen) — skipped", batch_id, case_label)
                total_skipped_duplicate += 1
                continue

            total_downloaded += 1
            _log(batch_id, "info", "[BATCH %s] [INGEST] %s: new PDF downloaded — ingestion_id=%s", batch_id, case_label, ingestion_id)
            try:
                case_id = _run_pipeline_for_one(batch_id, ingestion_id, record)
            except Exception:
                # A pipeline-stage bug on this one record shouldn't cost the
                # rest of the batch its progress — see promotion.py's own
                # enum validation for the specific failure mode this guards
                # against, this is the defense-in-depth backstop for others.
                logger.exception("[BATCH %s] [PIPELINE] ingestion_id=%s: unhandled exception", batch_id, ingestion_id)
                live_logs.log(batch_id, "error", f"[BATCH {batch_id}] [PIPELINE] ingestion_id={ingestion_id}: unhandled exception — see server logs")
                scrape_jobs.update_status(ingestion_id, status="PROMOTION_FAILED", error_message="Unhandled pipeline exception — see server logs")
                total_errored += 1
                continue

            if case_id is not None:
                total_promoted += 1
            else:
                # _run_pipeline_for_one returns None for a few different
                # reasons (OCR_FAILED, PROMOTION_FAILED, or a genuine
                # NEEDS_REVIEW from promotion) — check which one actually
                # happened instead of lumping OCR/promotion failures in
                # with "needs review", which undersells them as errors.
                final_status = scrape_jobs.get_ingestion(ingestion_id)["status"]
                if final_status == "NEEDS_REVIEW":
                    total_needs_review += 1
                else:
                    total_errored += 1
                _log(batch_id, "info", "[BATCH %s] [PIPELINE] ingestion_id=%s: ended at status=%s (not promoted)", batch_id, ingestion_id, final_status)

        final_status = "CANCELLED" if cancelled else "COMPLETED"
        scrape_jobs.finish_batch(batch_id, status=final_status, total_found=total_found, total_downloaded=total_downloaded, total_promoted=total_promoted)
    except Exception:
        # Only an adapter-level failure (the scrape() generator itself
        # raising, e.g. a browser crash) reaches here — per-record failures
        # are caught above and never propagate this far.
        logger.exception("[BATCH %s] failed — adapter-level exception, aborting batch", batch_id)
        live_logs.log(batch_id, "error", f"[BATCH {batch_id}] failed — adapter-level exception, aborting batch")
        scrape_jobs.finish_batch(batch_id, status="FAILED", total_found=total_found, total_downloaded=total_downloaded, total_promoted=total_promoted)
        live_logs.finish_batch(batch_id)
        raise

    summary = {
        "batch_id": batch_id,
        "total_found": total_found,
        "total_downloaded": total_downloaded,
        "total_skipped_duplicate": total_skipped_duplicate,
        "total_promoted": total_promoted,
        "total_needs_review": total_needs_review,
        "total_errored": total_errored,
        "cancelled": cancelled,
    }
    _log(batch_id, "info", "[BATCH %s] finished: %s", batch_id, summary)
    live_logs.finish_batch(batch_id)
    return summary


def _ingest_one_record(record: RawJudgmentRecord, batch_id: int, court_id: int, court_code: str, data_source: str) -> Optional[int]:
    """Checksum-dedups, uploads to blob if configured, and inserts the raw_ingestions row. Returns None if it was a duplicate."""
    checksum = _checksum_file(record.pdf_path)

    blob_pdf_id = None
    if azure_blob.is_configured():
        blob_pdf_id = azure_blob.upload_pdf(record.pdf_path, blob_name=f"{court_code}/{checksum}.pdf")
        if blob_pdf_id:
            _log(batch_id, "info", "[BATCH %s] [BLOB] uploaded %s/%s.pdf", batch_id, court_code, checksum)

    return scrape_jobs.insert_raw_ingestion(
        batch_id=batch_id,
        court_id=court_id,
        source_pdf_url=record.source_url,
        file_checksum=checksum,
        data_source=data_source,
        blob_pdf_id=blob_pdf_id,
    )


def _run_pipeline_for_one(batch_id: int, ingestion_id: int, record: RawJudgmentRecord) -> Optional[int]:
    """
    Runs OCR -> promotion -> LLM enrichment in sequence, stopping if OCR
    didn't actually succeed. pipeline/promotion.py reads RawJudgmentRecord +
    ocr_text directly via pipeline/regex_extraction.py (no separate
    LLM-extraction stage the way the old pipeline had one); LLM enrichment
    (pipeline/llm_enrichment.py — case_note/industries/conclusion, plus a
    disposition fallback) then runs automatically on top of the already-
    promoted row.

    Enrichment failures are swallowed here on purpose: a case that's
    already validly promoted (has case_number, ocr_text, regex fields)
    must not be un-promoted or reported as a batch error just because an
    enhancement on top of it didn't work — see llm_enrichment.enrich_case's
    own docstring, which never raises for exactly this reason. Still
    caught defensively in case that contract is ever violated.
    """
    ocr.process_ingestion_ocr(ingestion_id, record.pdf_path)
    if scrape_jobs.get_ingestion(ingestion_id)["status"] != "OCR_DONE":
        return None

    case_id = promotion.promote_ingestion(ingestion_id, record)
    if case_id is not None:
        try:
            llm_enrichment.enrich_case(case_id)
            _log(batch_id, "info", "[ENRICH] case_id=%s: enrichment finished", case_id)
        except Exception:
            logger.exception("[ENRICH] case_id=%s: enrichment raised despite its own contract not to — promotion still stands", case_id)
            live_logs.log(batch_id, "warning", f"[ENRICH] case_id={case_id}: enrichment raised despite its own contract not to — promotion still stands")

    return case_id


def to_date(date_str: str):
    """Parses either 'YYYY-MM-DD' (API request bodies) or 'DD-MM-YYYY' (adapters' own sub-batch strings) — used by scraper_router.py to build the cr_scrape_batches row before dispatching this module's background task."""
    from datetime import datetime
    if "-" in date_str and len(date_str.split("-")[0]) == 4:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    return datetime.strptime(date_str, "%d-%m-%Y").date()
