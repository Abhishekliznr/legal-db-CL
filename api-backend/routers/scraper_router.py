"""
Scraper control endpoints (moved here from scraper-backend, which is now an on-demand worker)
-------------------------------------------------------------------------------------------
- POST /api/scraper/start                         : queue a batch for any court_id and launch its worker Job
- POST /api/scraper/sc/start                      : same, Supreme Court — {from_date, to_date, headless}
- POST /api/scraper/mp/start                      : same, MPHC — {year, headless}
- GET  /api/scraper/batches                       : recent batch history
- GET  /api/scraper/batches/{batch_id}            : one batch's header/summary fields
- GET  /api/scraper/batches/{batch_id}/records    : per-record (cr_raw_ingestions) breakdown
- POST /api/scraper/batches/{batch_id}/cancel     : stop a QUEUED/RUNNING batch
- POST /api/scraper/batches/{batch_id}/resume     : queue another run of a stopped batch (new Job)
- GET  /api/scraper/batches/{batch_id}/logs/stream: SSE tail of cr_batch_logs
- GET  /api/scraper/status                        : raw_ingestions counts per pipeline stage

Every start/resume creates a new K8s Job (one pod) through scraper/job_launcher.py; this
service never talks to a running worker, only to Postgres (db/scrape_jobs.py).
"""

import asyncio
import json
import logging
from datetime import date, timedelta
from typing import Callable, Optional, TypeVar

import psycopg2
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from db import court_config, scrape_jobs
from scraper import courts
from scraper.job_launcher import LaunchError, launch_batch

logger = logging.getLogger("api_backend_v2.scraper_router")

router = APIRouter(prefix="/api/scraper", tags=["Scraper Control"])

_LOG_STREAM_POLL_SECONDS = 1.0

T = TypeVar("T")


def _db(what: str, fn: Callable[[], T]) -> T:
    try:
        return fn()
    except (HTTPException, scrape_jobs.CourtBusyError):
        raise
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while %s", what)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while %s", what)
        raise HTTPException(status_code=500, detail=f"Failed while {what}.")


def _sweep() -> None:
    try:
        for batch in scrape_jobs.sweep_stale_batches():
            logger.warning("Batch %s marked %s: %s", batch["batch_id"], batch["status"], batch["error_message"])
    except Exception:
        logger.exception("Could not sweep stale scraper batches")


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field} must be YYYY-MM-DD, got {value!r}.")


def _resolve_court(court_id: int) -> dict:
    config = _db(f"resolving court {court_id} config", lambda: court_config.get_court_scrape_config(court_id))
    if config is None:
        raise HTTPException(
            status_code=404,
            detail=f"No court_scrape_config for court_id={court_id}. Seed it first (PUT /api/courts/{{court_id}}/config or python -m db.seed_courts).",
        )
    if not config["is_active"]:
        raise HTTPException(status_code=400, detail=f"court_id={court_id} is marked inactive in court_scrape_config.")
    if not courts.is_supported(config["adapter"]):
        raise HTTPException(status_code=500, detail=f"court_scrape_config has unknown adapter '{config['adapter']}' — the scraper worker can't run it.")
    return config


def _court_id_by_code(court_code: str) -> int:
    court_id = _db(f"looking up court {court_code}", lambda: court_config.get_court_id_by_code(court_code))
    if court_id is None:
        raise HTTPException(status_code=404, detail=f"No court with court_code='{court_code}' — seed it first (python -m db.seed_courts).")
    return court_id


def _launch_or_fail(batch_id: int, run_number: int, headless: bool) -> str:
    try:
        job_name = launch_batch(batch_id, run_number, headless)
    except LaunchError as exc:
        logger.error("Could not launch scraper job for batch %s run %s: %s", batch_id, run_number, exc)
        _db(f"closing batch {batch_id} after a failed launch", lambda: scrape_jobs.fail_launch(batch_id, f"Could not launch scraper job: {exc}"))
        raise HTTPException(status_code=502, detail=f"Batch #{batch_id} could not be started: {exc}")
    _db(f"recording job name for batch {batch_id}", lambda: scrape_jobs.set_job_name(batch_id, job_name))
    return job_name


def _queue_and_launch(court_id: int, from_date: str, to_date: str, headless: bool) -> dict:
    config = _resolve_court(court_id)
    date_from, date_to = _parse_date(from_date, "from_date"), _parse_date(to_date, "to_date")
    if date_from > date_to:
        raise HTTPException(status_code=400, detail="from_date must be on or before to_date.")

    try:
        batch_id = _db("creating batch", lambda: scrape_jobs.create_queued_batch(court_id, date_from, date_to))
    except scrape_jobs.CourtBusyError as exc:
        raise HTTPException(status_code=409, detail=f"{config['court_name']}: {exc}")

    job_name = _launch_or_fail(batch_id, 1, headless)
    return {
        "status": "queued",
        "batch_id": batch_id,
        "job_name": job_name,
        "adapter": config["adapter"],
        "court_id": court_id,
        "court_name": config["court_name"],
        "from_date": from_date,
        "to_date": to_date,
        "message": "Batch queued — its scraper job is starting. Poll GET /api/scraper/batches/{batch_id} for progress.",
    }


class ScraperStartRequest(BaseModel):
    court_id: int = Field(..., description="cr_courts.court_id to scrape — its court_scrape_config row decides the adapter")
    court_code: Optional[str] = Field(None, description="Ignored; resolved from cr_courts. Kept so older callers still validate.")
    from_date: Optional[str] = Field(None, description="YYYY-MM-DD; defaults to 7 days ago")
    to_date: Optional[str] = Field(None, description="YYYY-MM-DD; defaults to today")
    headless: bool = Field(True, description="Set False for a supervised local run against a real browser window")


@router.post("/start")
def start_scrape(req: ScraperStartRequest):
    from_date = req.from_date or (date.today() - timedelta(days=7)).isoformat()
    to_date = req.to_date or date.today().isoformat()
    return _queue_and_launch(req.court_id, from_date, to_date, req.headless)


class SCScraperStartRequest(BaseModel):
    from_date: Optional[str] = Field(None, description="YYYY-MM-DD; defaults to 7 days ago")
    to_date: Optional[str] = Field(None, description="YYYY-MM-DD; defaults to today")
    headless: bool = Field(True, description="Set False for a supervised local run against a real browser window")


@router.post("/sc/start")
def start_sc_scrape(req: SCScraperStartRequest):
    from_date = req.from_date or (date.today() - timedelta(days=7)).isoformat()
    to_date = req.to_date or date.today().isoformat()
    return _queue_and_launch(_court_id_by_code("SCIN"), from_date, to_date, req.headless)


class MPScraperStartRequest(BaseModel):
    year: int = Field(..., ge=1956, le=date.today().year, description="ILR year to search on portal.mphc.gov.in/ilrs, e.g. 2024")
    headless: bool = Field(True, description="Set False for a supervised local run against a real browser window")


@router.post("/mp/start")
def start_mp_scrape(req: MPScraperStartRequest):
    # MP is queried by ILR year; the worker's MP adapter reads the year back out of this range.
    return _queue_and_launch(_court_id_by_code("MPHC"), f"{req.year}-01-01", f"{req.year}-12-31", req.headless)


@router.get("/batches")
def list_batches(limit: int = 25, offset: int = 0):
    _sweep()
    return _db("listing batches", lambda: scrape_jobs.list_batches(limit=limit, offset=offset))


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int):
    _sweep()
    batch = _db(f"reading batch {batch_id}", lambda: scrape_jobs.get_batch(batch_id))
    if batch is None:
        raise HTTPException(status_code=404, detail=f"No batch with batch_id={batch_id}.")
    batch["resumable"] = _supports_resume(batch["court_id"]) and _has_work_to_resume(batch, retry_skipped=True)
    return batch


def _supports_resume(court_id: int) -> bool:
    try:
        config = court_config.get_court_scrape_config(court_id)
    except Exception:
        logger.exception("Could not resolve court_scrape_config for court_id=%s", court_id)
        return False
    return config is not None and courts.is_resumable(config["adapter"])


def _has_work_to_resume(batch: dict, retry_skipped: bool) -> bool:
    if batch["status"] in scrape_jobs.ACTIVE_STATUSES:
        return False
    if not batch["discovered"]:
        # Stopped before its case list was saved: resuming re-runs discovery. A COMPLETED
        # batch without one predates resumable batches, so there's nothing to resume.
        return batch["status"] != "COMPLETED"
    counts = batch["item_counts"]
    return bool(counts.get("PENDING") or counts.get("FAILED") or (retry_skipped and counts.get("SKIPPED")))


class BatchResumeRequest(BaseModel):
    retry_skipped: bool = Field(False, description="Also retry cases skipped in earlier runs (e.g. no judgment PDF yet)")
    headless: bool = Field(True, description="Set False for a supervised local run against a real browser window")


@router.post("/batches/{batch_id}/resume")
def resume_batch(batch_id: int, req: Optional[BatchResumeRequest] = None):
    req = req or BatchResumeRequest()
    batch = _db(f"reading batch {batch_id}", lambda: scrape_jobs.get_batch(batch_id))
    if batch is None:
        raise HTTPException(status_code=404, detail=f"No batch with batch_id={batch_id}.")

    config = _resolve_court(batch["court_id"])
    if not courts.is_resumable(config["adapter"]):
        raise HTTPException(status_code=400, detail=f"{config['court_name']} batches can't be resumed yet — use Run Again.")
    if not _has_work_to_resume(batch, retry_skipped=req.retry_skipped):
        raise HTTPException(status_code=409, detail="Nothing left to resume for this batch.")

    try:
        claim = _db(f"queueing a resume of batch {batch_id}", lambda: scrape_jobs.claim_batch_resume(batch_id, retry_skipped=req.retry_skipped))
    except scrape_jobs.CourtBusyError as exc:
        raise HTTPException(status_code=409, detail=f"{config['court_name']}: {exc}")
    if claim is None:
        raise HTTPException(status_code=409, detail="This batch is already queued or running.")

    job_name = _launch_or_fail(batch_id, claim["run_count"], req.headless)
    return {
        "status": "resumed",
        "batch_id": batch_id,
        "run_count": claim["run_count"],
        "job_name": job_name,
        "message": f"Resuming batch #{batch_id} (run {claim['run_count']}) — its scraper job is starting.",
    }


@router.get("/batches/{batch_id}/records")
def list_batch_records(batch_id: int, limit: int = 100, offset: int = 0):
    return _db(f"listing records for batch {batch_id}", lambda: scrape_jobs.list_ingestions_by_batch(batch_id, limit=limit, offset=offset))


_CANCEL_MESSAGES = {
    "cancelled": "The batch was stopped before its scraper job started.",
    "cancelling": "Cancellation requested — the batch will stop after its current record.",
    "not_running": "This batch is not queued or running, nothing to cancel.",
}


@router.post("/batches/{batch_id}/cancel")
def cancel_batch(batch_id: int):
    result = _db(f"cancelling batch {batch_id}", lambda: scrape_jobs.request_cancel(batch_id))
    return {"status": result, "batch_id": batch_id, "message": _CANCEL_MESSAGES[result]}


@router.get("/batches/{batch_id}/logs/stream")
async def stream_batch_logs(batch_id: int):
    """SSE tail of cr_batch_logs: replays the batch's full history (every run), then follows new lines until it finishes."""
    async def event_source():
        cursor = 0
        sent_any = False
        while True:
            try:
                # Status before lines: the worker flushes its last lines before it marks the batch
                # finished, so a finished status here means every line is already committed.
                status = await asyncio.to_thread(scrape_jobs.get_batch_status, batch_id)
                lines = await asyncio.to_thread(scrape_jobs.get_logs_since, batch_id, cursor)
            except Exception as exc:
                logger.exception("Could not read logs for batch %s", batch_id)
                yield _sse_event({"level": "system", "message": f"Could not read logs: {exc}"})
                return

            if status is None:
                yield _sse_event({"level": "system", "message": f"No batch with batch_id={batch_id}."})
                return
            for line in lines:
                cursor = line.pop("log_id")
                sent_any = True
                yield _sse_event(line)
            if lines:
                continue
            if status not in scrape_jobs.ACTIVE_STATUSES:
                message = "Batch finished — log stream closed." if sent_any else \
                    "No log lines stored for this batch (it ran before logs were persisted, or they were pruned after 7 days)."
                yield _sse_event({"level": "system", "message": message})
                return
            await asyncio.sleep(_LOG_STREAM_POLL_SECONDS)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@router.get("/status")
def pipeline_status():
    return {"counts_by_status": _db("reading pipeline status", scrape_jobs.pipeline_status_counts)}
