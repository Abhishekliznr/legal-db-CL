"""
Scraper control endpoints
--------------------------
- POST /api/scraper/start                        : launch a background scrape+ingest batch
- GET  /api/scraper/batches                       : recent batch history
- GET  /api/scraper/batches/{batch_id}             : one batch's header/summary fields
- GET  /api/scraper/batches/{batch_id}/records      : per-record (cr_raw_ingestions) breakdown for one batch
- POST /api/scraper/batches/{batch_id}/cancel       : request early stop of a RUNNING batch
- GET  /api/scraper/batches/{batch_id}/logs/stream  : SSE tail of that batch's in-memory log buffer
- GET  /api/scraper/status                        : raw_ingestions counts per pipeline stage

`court_id` is the only thing a caller needs to supply for eCourts High
Courts — state_code/bench_code are resolved from court_scrape_config
(seeded via `python -m db.seed_courts`), not passed in the request. That's
the entire point of court_scrape_config existing (spec §4.3): the caller
shouldn't need to know an eCourts state code any more than they'd need to
know which of the 25 old per-court scripts used to handle a given court.
"""

import asyncio
import json
import logging
from datetime import date, timedelta
from typing import Optional

import psycopg2
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from adapters.ecourts.adapter import EcourtsAdapter
from adapters.supreme_court.adapter import SupremeCourtAdapter
from db import court_config, scrape_jobs
from orchestrator import batch_runner, job_registry, live_logs

logger = logging.getLogger("scraper_backend_v2.scraper_router")

# How often the SSE endpoint polls the in-memory buffer for new lines. Short
# enough to feel live, long enough that tailing a batch doesn't become a
# busy-loop — batch_runner.py itself only logs a handful of times a second
# at most, so there's nothing to gain from polling faster than this.
_LOG_STREAM_POLL_SECONDS = 1.0

router = APIRouter(prefix="/api/scraper", tags=["Scraper Control"])

_ADAPTER_CLASSES = {
    "supreme_court": SupremeCourtAdapter,
    "ecourts": EcourtsAdapter,
}

# data_source_enum value each adapter's records get tagged with — resolved
# here (not left to raw_ingestions/documents' DEFAULT 'ECOURTS') since that
# default being silently relied upon was the actual bug: every source,
# including Supreme Court, was landing as 'ECOURTS' in the DB.
_DATA_SOURCE_BY_ADAPTER = {
    "supreme_court": "SCI_WEBSITE",
    "ecourts": "ECOURTS",
}


class ScraperStartRequest(BaseModel):
    court_id: int = Field(..., description="courts.court_id to scrape — its court_scrape_config row decides the adapter")
    court_code: str = Field(..., description="Short code used in blob paths, e.g. 'SCIN' or 'DHC'")
    from_date: Optional[str] = Field(None, description="YYYY-MM-DD; defaults to 7 days ago")
    to_date: Optional[str] = Field(None, description="YYYY-MM-DD; defaults to today")
    headless: bool = Field(True, description="Set False for a supervised dry run against a real browser window")


@router.post("/start")
def start_scrape(req: ScraperStartRequest, background_tasks: BackgroundTasks):
    try:
        config = court_config.get_court_scrape_config(req.court_id)
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while resolving court %s config", req.court_id)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")

    if config is None:
        raise HTTPException(
            status_code=404,
            detail=f"No court_scrape_config for court_id={req.court_id}. Seed it first (PUT /api/courts/{{court_id}}/config or python -m db.seed_courts).",
        )
    if not config["is_active"]:
        raise HTTPException(status_code=400, detail=f"court_id={req.court_id} is marked inactive in court_scrape_config.")

    adapter_class = _ADAPTER_CLASSES.get(config["adapter"])
    if adapter_class is None:
        raise HTTPException(status_code=500, detail=f"court_scrape_config has unknown adapter '{config['adapter']}'.")

    adapter_kwargs = {"headless": req.headless}
    if config["adapter"] == "ecourts":
        if not config["state_code"]:
            raise HTTPException(status_code=400, detail=f"court_id={req.court_id} has adapter='ecourts' but no state_code configured.")
        adapter_kwargs["state_code"] = config["state_code"]
        adapter_kwargs["bench_code"] = config["bench_code"]

    from_date = req.from_date or (date.today() - timedelta(days=7)).isoformat()
    to_date = req.to_date or date.today().isoformat()

    # Created here, synchronously, rather than inside the background task itself — so the
    # response below can hand batch_id straight back to the caller (the admin UI navigates to
    # /admin/scraping/history/{batch_id} on this response) instead of it only existing once the
    # background task happens to get scheduled.
    batch_id = scrape_jobs.create_batch(req.court_id, batch_runner.to_date(from_date), batch_runner.to_date(to_date))

    background_tasks.add_task(
        _run_and_log, adapter_class(), batch_id, req.court_id, req.court_code, from_date, to_date,
        _DATA_SOURCE_BY_ADAPTER[config["adapter"]], adapter_kwargs,
    )

    return {
        "status": "started",
        "batch_id": batch_id,
        "adapter": config["adapter"],
        "court_id": req.court_id,
        "court_name": config["court_name"],
        "from_date": from_date,
        "to_date": to_date,
        "message": "Batch running in the background — poll GET /api/scraper/batches for progress.",
    }


def _run_and_log(adapter, batch_id: int, court_id: int, court_code: str, from_date: str, to_date: str, data_source: str, adapter_kwargs: dict):
    try:
        batch_runner.run_batch(adapter, batch_id, court_id, court_code, from_date, to_date, data_source, **adapter_kwargs)
    except Exception:
        logger.exception("Batch %s failed for court_id=%s %s -> %s", batch_id, court_id, from_date, to_date)


@router.get("/batches")
def list_batches(limit: int = 25, offset: int = 0):
    try:
        return job_registry.list_recent_batches(limit=limit, offset=offset)
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while listing batches")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while listing batches")
        raise HTTPException(status_code=500, detail="Failed to list batches.")


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int):
    try:
        batch = scrape_jobs.get_batch(batch_id)
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while reading batch %s", batch_id)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while reading batch %s", batch_id)
        raise HTTPException(status_code=500, detail="Failed to read batch.")
    if batch is None:
        raise HTTPException(status_code=404, detail=f"No batch with batch_id={batch_id}.")
    return batch


@router.get("/batches/{batch_id}/records")
def list_batch_records(batch_id: int, limit: int = 100, offset: int = 0):
    try:
        return scrape_jobs.list_ingestions_by_batch(batch_id, limit=limit, offset=offset)
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while listing records for batch %s", batch_id)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while listing records for batch %s", batch_id)
        raise HTTPException(status_code=500, detail="Failed to list batch records.")


@router.post("/batches/{batch_id}/cancel")
def cancel_batch(batch_id: int):
    try:
        cancelled = scrape_jobs.request_cancel(batch_id)
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while cancelling batch %s", batch_id)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while cancelling batch %s", batch_id)
        raise HTTPException(status_code=500, detail="Failed to cancel batch.")
    return {
        "status": "cancelling" if cancelled else "not_running",
        "batch_id": batch_id,
        "message": (
            "Cancellation requested — the batch will stop after its current record."
            if cancelled else
            "This batch is not RUNNING, nothing to cancel."
        ),
    }


@router.get("/batches/{batch_id}/logs/stream")
async def stream_batch_logs(batch_id: int):
    """
    Server-Sent Events tail of one batch's in-memory log buffer (orchestrator/live_logs.py) —
    deliberately not backed by a database table, see that module's docstring. Replays whatever's
    already buffered, then polls for new lines every _LOG_STREAM_POLL_SECONDS, and closes once
    the batch has reached a terminal status and no lines remain unsent.
    """
    async def event_source():
        if not live_logs.has_buffer(batch_id):
            yield _sse_event({
                "level": "system",
                "message": "No live log buffer for this batch — it finished (or started) before this server "
                            "process's current uptime, or its buffer was evicted. Logs are in-memory only, not persisted.",
            })
            return

        cursor = 0
        while True:
            lines, cursor, finished = live_logs.get_lines_since(batch_id, cursor)
            for ts, level, message in lines:
                yield _sse_event({"ts": ts, "level": level, "message": message})
            if finished and not lines:
                yield _sse_event({"level": "system", "message": "Batch finished — log stream closed."})
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
    try:
        return {"counts_by_status": job_registry.pipeline_status_counts()}
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while reading pipeline status")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while reading pipeline status")
        raise HTTPException(status_code=500, detail="Failed to read pipeline status.")
