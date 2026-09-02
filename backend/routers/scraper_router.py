"""
Scraper Router: Dedicated Endpoints for Court Judgment Scraping & Azure Sync
-----------------------------------------------------------------------------
Handles:
- POST /api/scraper/start         : Start background scraping job
- GET  /api/scraper/status/{id}   : Get live execution status and stream logs
- POST /api/scraper/cancel/{id}   : Cancel running scraping job
- GET  /api/scraper/jobs          : List historical scraper execution runs
"""

from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from pydantic import BaseModel, Field

try:
    from backend import db_manager, scraper_pipeline
except ImportError:
    import db_manager
    import scraper_pipeline

router = APIRouter(prefix="/api/scraper", tags=["Court Scraper Engine"])


class ScraperJobRequest(BaseModel):
    court_id: str = Field("SCIN", description="Court code: SCIN (Supreme Court of India)")
    from_date: Optional[str] = Field(None, description="Start date (YYYY-MM-DD or DD-MM-YYYY)")
    to_date: Optional[str] = Field(None, description="End date (YYYY-MM-DD or DD-MM-YYYY)")
    upload_azure: bool = Field(True, description="Upload PDFs and Bronze/Silver JSON to Azure Blob Storage")
    stream_cloud: bool = Field(True, description="Stream directly to Azure and delete local temporary PDFs")
    extract_metadata: bool = Field(True, description="Automatically run AI CaseNote and Statutory metadata extractor")


@router.post("/start", response_model=Dict[str, Any])
def start_scraper_job(req: ScraperJobRequest, background_tasks: BackgroundTasks):
    """
    Launches an automated court scraping job in the background.
    Tracks execution in PostgreSQL and streams live logs.
    """
    try:
        from datetime import datetime
        if not req.from_date:
            req.from_date = datetime.now().strftime("%Y-%m-01")
        if not req.to_date:
            req.to_date = datetime.now().strftime("%Y-%m-%d")

        job_id = db_manager.log_scraper_job_start(
            court_id=req.court_id,
            from_date=req.from_date,
            to_date=req.to_date,
            notes="Triggered via API"
        )

        if not job_id:
            raise HTTPException(status_code=500, detail="Failed to initialize scraper job in database.")

        background_tasks.add_task(
            scraper_pipeline.run_pipeline_worker,
            job_id=job_id,
            court_id=req.court_id,
            from_date=req.from_date,
            to_date=req.to_date,
            upload_azure=req.upload_azure,
            stream_cloud=req.stream_cloud,
            extract_metadata=req.extract_metadata
        )

        return {
            "status": "success",
            "job_id": job_id,
            "court_id": req.court_id,
            "from_date": req.from_date,
            "to_date": req.to_date,
            "message": f"Scraper job {job_id} launched successfully in background."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/{job_id}", response_model=Dict[str, Any])
def get_scraper_job_status(job_id: str):
    """Retrieves current execution status, live progress, and log messages for a scraper job."""
    job = db_manager.get_scraper_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Scraper job not found.")
    return job


@router.post("/cancel/{job_id}", response_model=Dict[str, Any])
def cancel_scraper_job(job_id: str):
    """Cancels a running background scraping job."""
    if job_id in scraper_pipeline.ACTIVE_JOBS:
        scraper_pipeline.CANCEL_FLAGS[job_id] = True
        return {"status": "success", "message": f"Cancellation requested for job {job_id}."}
    else:
        job = db_manager.get_scraper_job(job_id)
        if job and job.get("status") == "RUNNING":
            db_manager.log_scraper_job_finish(job_id, status="CANCELLED")
            return {"status": "success", "message": f"Job {job_id} marked as cancelled."}
        return {"status": "info", "message": f"Job {job_id} is not currently running."}


@router.get("/jobs", response_model=Dict[str, Any])
def list_scraper_jobs(limit: int = Query(25, ge=1, le=100)):
    """Lists recent scraper execution jobs and their statuses for admin dashboard history."""
    jobs = db_manager.list_scraper_jobs(limit=limit)
    return {"total": len(jobs), "jobs": jobs}
