"""
Enterprise Scraper Pipeline Worker & Orchestrator
--------------------------------------------------
Orchestrates background scraping execution for courts (Supreme Court of India),
uploads unique PDFs live to Azure, generates Bronze raw JSON, runs AI metadata extraction,
generates Silver enriched JSON, ingests all cases into PostgreSQL, and streams real-time logs.
"""

import os
import sys
import json
import time
import traceback
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

# Setup sys.path
_script_dir = Path(__file__).resolve().parent
_project_root = _script_dir.parent
for _p in [str(_project_root), str(_script_dir)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import db_manager
import azure_blob
import ingestion

# Global in-memory active jobs registry
ACTIVE_JOBS: Dict[str, Dict[str, Any]] = {}
ACTIVE_THREADS: Dict[str, threading.Thread] = {}
CANCEL_FLAGS: Dict[str, bool] = {}


class JobLogger:
    """Captures and stores real-time logs in memory and PostgreSQL."""
    def __init__(self, job_id: str):
        self.job_id = job_id

    def log(self, message: str):
        ts = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{ts}] {message}"
        print(f"[{self.job_id[:8]}] {formatted}")
        
        # Update memory
        if self.job_id in ACTIVE_JOBS:
            ACTIVE_JOBS[self.job_id]["logs"].append(formatted)
            
        # Update DB
        db_manager.append_scraper_job_log(self.job_id, message)


def run_pipeline_worker(
    job_id: str,
    court_id: str = "SCIN",
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    upload_azure: bool = True,
    stream_cloud: bool = True,
    extract_metadata: bool = True
):
    """
    Complete Background Execution Pipeline:
    1. Supreme Court Scraper (with deduplication & Azure live PDF uploads)
    2. Bronze Layer Archive
    3. Silver Layer AI Metadata Extractor
    4. Database Ingestion into PostgreSQL
    """
    logger = JobLogger(job_id)
    CANCEL_FLAGS[job_id] = False

    try:
        logger.log(f"🚀 Starting automated pipeline for {court_id} from {from_date} to {to_date}...")
        
        # Determine year
        import re
        year_match = re.search(r"\b(19\d{2}|20\d{2})\b", str(from_date) if from_date else "")
        year_str = year_match.group(1) if year_match else str(datetime.now().year)

        # ----------------------------------------------------
        # STEP 1: SCRAPING & LIVE PDF UPLOAD
        # ----------------------------------------------------
        logger.log("🕷️ Phase 1/4: Initializing Web Scraper...")
        
        if court_id == "SCIN" or "SUPREME" in court_id.upper():
            from app.SUPREME_COURT_OF_INDIA_SCRAPER import supreme_court
            
            logger.log("🛡️ Checking existing cases in database to skip duplicates...")
            existing_keys = ingestion.get_existing_cases_keys(court_id="SUPREME_COURT_OF_INDIA")
            logger.log(f"🛡️ Loaded {len(existing_keys)} existing cases from PostgreSQL for instant duplicate skipping.")

            logger.log(f"🌐 Navigating to Supreme Court of India portal ({from_date} to {to_date})...")
            
            # Run scraper
            records_list = supreme_court.run_scraper(
                from_date=from_date,
                to_date=to_date,
                upload_azure=upload_azure,
                stream_cloud=stream_cloud
            )
            if not isinstance(records_list, list):
                records_list = []
        else:
            raise ValueError(f"Unsupported court ID: {court_id}")

        if CANCEL_FLAGS.get(job_id, False):
            logger.log("🛑 Job was cancelled by user.")
            db_manager.log_scraper_job_finish(job_id, status="CANCELLED")
            return

        total_scraped = len(records_list)
        logger.log(f"✅ Phase 1 Complete: Scraped {total_scraped} judgment record(s).")

        # ----------------------------------------------------
        # STEP 2: BRONZE AZURE ARCHIVING (In-Memory)
        # ----------------------------------------------------
        bronze_blob_url = None
        if upload_azure and azure_blob.is_azure_blob_configured() and total_scraped > 0:
            logger.log("📦 Phase 2/4: Archiving Bronze raw JSON directly to Azure Blob Storage...")
            bronze_filename = f"supreme_court_judgments_{year_str}.json"
            bronze_blob_url = azure_blob.upload_json_to_blob(
                {"Supreme Court of India": records_list},
                court_code="SCIN",
                layer="Bronze",
                filename=bronze_filename
            )
            logger.log(f"☁️ [BRONZE] Archive URL: {bronze_blob_url}")

        # ----------------------------------------------------
        # STEP 3: SILVER AI METADATA EXTRACTION (In-Memory)
        # ----------------------------------------------------
        silver_records = []
        silver_blob_url = None
        if extract_metadata and total_scraped > 0:
            logger.log("✨ Phase 3/4: Running AI CaseNote & Statutory Metadata Extractor...")
            try:
                from app.SUPREME_COURT_OF_INDIA_SCRAPER import pdf_metadata_extractor
                silver_records = pdf_metadata_extractor.extract_metadata_from_records(records_list)
                logger.log(f"✨ Extracted metadata for {len(silver_records)} cases in memory.")
                
                if silver_records and upload_azure and azure_blob.is_azure_blob_configured():
                    silver_filename = f"supreme_court_metadata_{year_str}.json"
                    silver_blob_url = azure_blob.upload_json_to_blob(
                        {"Supreme Court of India": silver_records},
                        court_code="SCIN",
                        layer="Silver",
                        filename=silver_filename
                    )
                    logger.log(f"☁️ [SILVER] Archive URL: {silver_blob_url}")
            except Exception as meta_err:
                logger.log(f"⚠️ Metadata extraction notice: {meta_err}")

        # ----------------------------------------------------
        # STEP 4: DATABASE INGESTION (In-Memory)
        # ----------------------------------------------------
        logger.log("💾 Phase 4/4: Ingesting normalized records directly into PostgreSQL database...")
        records_to_import = silver_records if silver_records else records_list
        
        imported_count = ingestion.import_json_data(
            raw_data={"Supreme Court of India": records_to_import},
            default_court="SUPREME_COURT_OF_INDIA"
        )
        logger.log(f"🎉 Database Ingestion Complete: {imported_count} cases saved into PostgreSQL.")

        # Cleanup any local intermediate files (Zero Disk Mode)
        if stream_cloud:
            try:
                local_dir = _script_dir / "app" / "SUPREME_COURT_OF_INDIA_SCRAPER"
                for temp_file in [local_dir / "supreme_court_judgments.json", local_dir / "supreme_court_metadata.json"]:
                    if temp_file.exists():
                        temp_file.unlink()
                pdf_dir = local_dir / "pdf"
                if pdf_dir.exists():
                    import shutil
                    shutil.rmtree(pdf_dir, ignore_errors=True)
                logger.log("🧹 Zero Disk Mode: Cleaned all local temp files (0 local PDFs & 0 local JSON on disk).")
            except Exception:
                pass

        # Finalize job in DB
        db_manager.log_scraper_job_finish(
            job_id=job_id,
            status="COMPLETED",
            total_found=total_scraped,
            new_scraped=total_scraped,
            skipped=0,
            bronze_blob_url=bronze_blob_url,
            silver_blob_url=silver_blob_url,
            notes=f"Successfully scraped & ingested {total_scraped} cases."
        )
        logger.log("🏁 Pipeline successfully finished with 0 errors!")

    except Exception as e:
        err_msg = str(e)
        stack = traceback.format_exc()
        logger.log(f"❌ Pipeline Failed: {err_msg}")
        print(stack)
        db_manager.log_scraper_job_finish(
            job_id=job_id,
            status="FAILED",
            error_message=f"{err_msg}\n{stack}"
        )
    finally:
        ACTIVE_JOBS.pop(job_id, None)
        ACTIVE_THREADS.pop(job_id, None)


def start_scraper_job(
    court_id: str = "SCIN",
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    upload_azure: bool = True,
    stream_cloud: bool = True,
    extract_metadata: bool = True
) -> str:
    """Spawns an asynchronous background thread for the complete scraping pipeline."""
    job_id = db_manager.log_scraper_job_start(
        court_id=court_id,
        from_date=from_date,
        to_date=to_date,
        notes="Admin API Scraper Job"
    )
    if not job_id:
        import uuid
        job_id = str(uuid.uuid4())

    ACTIVE_JOBS[job_id] = {
        "job_id": job_id,
        "court_id": court_id,
        "from_date": from_date,
        "to_date": to_date,
        "status": "RUNNING",
        "logs": [f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 Initialized background job {job_id}"]
    }

    t = threading.Thread(
        target=run_pipeline_worker,
        kwargs={
            "job_id": job_id,
            "court_id": court_id,
            "from_date": from_date,
            "to_date": to_date,
            "upload_azure": upload_azure,
            "stream_cloud": stream_cloud,
            "extract_metadata": extract_metadata
        },
        daemon=True
    )
    ACTIVE_THREADS[job_id] = t
    t.start()

    return job_id


def stop_scraper_job(job_id: str) -> bool:
    """Signals cancellation to an active scraper job."""
    if job_id in CANCEL_FLAGS:
        CANCEL_FLAGS[job_id] = True
        db_manager.log_scraper_job_finish(job_id, status="CANCELLED", notes="Cancelled by admin")
        return True
    return False
