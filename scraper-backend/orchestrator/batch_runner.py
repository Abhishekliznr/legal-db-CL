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
from pipeline import llm_enrichment, ocr, promotion
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
    logger.info("[BATCH %s] started: court_id=%s %s -> %s (data_source=%s)", batch_id, court_id, date_from, date_to, data_source)

    total_found = 0
    total_downloaded = 0
    total_promoted = 0
    total_skipped_duplicate = 0
    total_needs_review = 0
    total_errored = 0

    try:
        for record in adapter.scrape(date_from, date_to, **adapter_kwargs):
            total_found += 1
            case_label = record.case_number_raw or record.source_url
            try:
                ingestion_id = _ingest_one_record(record, batch_id, court_id, court_code, data_source)
            except Exception:
                # Checksum/blob-upload failure for this one record — the
                # adapter's generator is still good, so keep going rather
                # than losing every record after this one in the batch.
                logger.exception("[BATCH %s] [INGEST] %s: failed to ingest (source_url=%s)", batch_id, case_label, record.source_url)
                total_errored += 1
                continue

            if ingestion_id is None:
                logger.info("[BATCH %s] [INGEST] %s: duplicate (checksum already seen) — skipped", batch_id, case_label)
                total_skipped_duplicate += 1
                continue

            total_downloaded += 1
            logger.info("[BATCH %s] [INGEST] %s: new PDF downloaded — ingestion_id=%s", batch_id, case_label, ingestion_id)
            try:
                case_id = _run_pipeline_for_one(ingestion_id, record)
            except Exception:
                # A pipeline-stage bug on this one record shouldn't cost the
                # rest of the batch its progress — see promotion.py's own
                # enum validation for the specific failure mode this guards
                # against, this is the defense-in-depth backstop for others.
                logger.exception("[BATCH %s] [PIPELINE] ingestion_id=%s: unhandled exception", batch_id, ingestion_id)
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
                logger.info("[BATCH %s] [PIPELINE] ingestion_id=%s: ended at status=%s (not promoted)", batch_id, ingestion_id, final_status)

        scrape_jobs.finish_batch(batch_id, status="COMPLETED", total_found=total_found, total_downloaded=total_downloaded)
    except Exception:
        # Only an adapter-level failure (the scrape() generator itself
        # raising, e.g. a browser crash) reaches here — per-record failures
        # are caught above and never propagate this far.
        logger.exception("[BATCH %s] failed — adapter-level exception, aborting batch", batch_id)
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
    logger.info("[BATCH %s] finished: %s", batch_id, summary)
    return summary


def _ingest_one_record(record: RawJudgmentRecord, batch_id: int, court_id: int, court_code: str, data_source: str) -> Optional[int]:
    """Checksum-dedups, uploads to blob if configured, and inserts the raw_ingestions row. Returns None if it was a duplicate."""
    checksum = _checksum_file(record.pdf_path)

    blob_pdf_id = None
    if azure_blob.is_configured():
        blob_pdf_id = azure_blob.upload_pdf(record.pdf_path, blob_name=f"{court_code}/{checksum}.pdf")
        if blob_pdf_id:
            logger.info("[BATCH %s] [BLOB] uploaded %s/%s.pdf", batch_id, court_code, checksum)

    return scrape_jobs.insert_raw_ingestion(
        batch_id=batch_id,
        court_id=court_id,
        source_pdf_url=record.source_url,
        file_checksum=checksum,
        data_source=data_source,
        blob_pdf_id=blob_pdf_id,
    )


def _run_pipeline_for_one(ingestion_id: int, record: RawJudgmentRecord) -> Optional[int]:
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
        except Exception:
            logger.exception("[ENRICH] case_id=%s: enrichment raised despite its own contract not to — promotion still stands", case_id)

    return case_id


def _to_date(date_str: str):
    from datetime import datetime
    if "-" in date_str and len(date_str.split("-")[0]) == 4:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    return datetime.strptime(date_str, "%d-%m-%Y").date()
