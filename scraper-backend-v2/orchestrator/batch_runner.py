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
from typing import Iterable, Optional

from adapters.base import RawJudgmentRecord, ScraperAdapter
from db import scrape_jobs
from pipeline import extraction, ocr, promotion
from storage import azure_blob

logger = logging.getLogger("scraper_backend_v2.orchestrator")


def _checksum_file(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_batch(
    adapter: ScraperAdapter,
    court_id: int,
    court_code: str,
    date_from: str,
    date_to: str,
    data_source: str,
    **adapter_kwargs,
) -> dict:
    """
    Runs one full scrape+ingest batch for a single court and date range.
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
    batch_id = scrape_jobs.create_batch(court_id, _to_date(date_from), _to_date(date_to))
    logger.info("Batch %s started: court_id=%s %s -> %s", batch_id, court_id, date_from, date_to)

    total_found = 0
    total_downloaded = 0
    total_promoted = 0
    total_skipped_duplicate = 0
    total_needs_review = 0
    total_errored = 0

    try:
        for record in adapter.scrape(date_from, date_to, **adapter_kwargs):
            total_found += 1
            try:
                ingestion_id = _ingest_one_record(record, batch_id, court_id, court_code, data_source)
            except Exception:
                # Checksum/blob-upload failure for this one record — the
                # adapter's generator is still good, so keep going rather
                # than losing every record after this one in the batch.
                logger.exception("Failed to ingest one record (source_url=%s) in batch %s", record.source_url, batch_id)
                total_errored += 1
                continue

            if ingestion_id is None:
                total_skipped_duplicate += 1
                continue

            total_downloaded += 1
            try:
                document_id = _run_pipeline_for_one(ingestion_id, record)
            except Exception:
                # A pipeline-stage bug on this one record shouldn't cost the
                # rest of the batch its progress — see promotion.py's own
                # enum validation for the specific failure mode this guards
                # against, this is the defense-in-depth backstop for others.
                logger.exception("Pipeline failed for ingestion_id=%s in batch %s", ingestion_id, batch_id)
                scrape_jobs.update_status(ingestion_id, status="EXTRACTION_FAILED", extraction_error="Unhandled pipeline exception — see server logs")
                total_errored += 1
                continue

            if document_id is not None:
                total_promoted += 1
            else:
                # _run_pipeline_for_one returns None for three different
                # reasons (OCR_FAILED, EXTRACTION_FAILED, or a genuine
                # NEEDS_REVIEW from promotion) — check which one actually
                # happened instead of lumping OCR/extraction failures in
                # with "needs review", which undersells them as errors.
                final_status = scrape_jobs.get_ingestion(ingestion_id)["status"]
                if final_status == "NEEDS_REVIEW":
                    total_needs_review += 1
                else:
                    total_errored += 1

        scrape_jobs.finish_batch(batch_id, status="COMPLETED", total_found=total_found, total_downloaded=total_downloaded)
    except Exception:
        # Only an adapter-level failure (the scrape() generator itself
        # raising, e.g. a browser crash) reaches here — per-record failures
        # are caught above and never propagate this far.
        scrape_jobs.finish_batch(batch_id, status="FAILED", total_found=total_found, total_downloaded=total_downloaded)
        raise

    summary = {
        "batch_id": batch_id,
        "total_found": total_found,
        "total_downloaded": total_downloaded,
        "total_skipped_duplicate": total_skipped_duplicate,
        "total_promoted": total_promoted,
        "total_needs_review": total_needs_review,
        "total_errored": total_errored,
    }
    logger.info("Batch %s finished: %s", batch_id, summary)
    return summary


def _ingest_one_record(record: RawJudgmentRecord, batch_id: int, court_id: int, court_code: str, data_source: str) -> Optional[int]:
    """Checksum-dedups, uploads to blob if configured, and inserts the raw_ingestions row. Returns None if it was a duplicate."""
    checksum = _checksum_file(record.pdf_path)

    blob_path = None
    if azure_blob.is_configured():
        blob_path = azure_blob.upload_pdf(record.pdf_path, blob_name=f"{court_code}/{checksum}.pdf")

    return scrape_jobs.insert_raw_ingestion(
        batch_id=batch_id,
        court_id=court_id,
        source_url=record.source_url,
        file_checksum=checksum,
        data_source=data_source,
        blob_path=blob_path,
    )


def _run_pipeline_for_one(ingestion_id: int, record: RawJudgmentRecord) -> Optional[int]:
    """
    Runs OCR -> extraction -> promotion in sequence, but stops at the first
    stage that didn't actually succeed — each stage's own status/
    extraction_error is the real, specific reason something failed, and it
    must not be overwritten by the next stage tripping over the resulting
    empty/partial data and reporting a confusing, misleading symptom
    instead (e.g. promotion failing on "judgment_date could not be parsed"
    when the real cause was an OCR or extraction failure upstream that left
    ocr_text/raw_ai_extraction empty).
    """
    ocr.process_ingestion_ocr(ingestion_id, record.pdf_path)
    if scrape_jobs.get_ingestion(ingestion_id)["status"] != "OCR_DONE":
        return None

    extraction.process_ingestion_extraction(ingestion_id, record)
    if scrape_jobs.get_ingestion(ingestion_id)["status"] != "EXTRACTED":
        return None

    return promotion.promote_ingestion(ingestion_id)


def _to_date(date_str: str):
    from datetime import datetime
    if "-" in date_str and len(date_str.split("-")[0]) == 4:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    return datetime.strptime(date_str, "%d-%m-%Y").date()
