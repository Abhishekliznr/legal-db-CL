"""
Scraper control endpoints
--------------------------
- POST /api/scraper/start    : launch a background scrape+ingest batch
- GET  /api/scraper/batches  : recent batch history
- GET  /api/scraper/status   : raw_ingestions counts per pipeline stage

`court_id` is the only thing a caller needs to supply for eCourts High
Courts — state_code/bench_code are resolved from court_scrape_config
(seeded via `python -m db.seed_courts`), not passed in the request. That's
the entire point of court_scrape_config existing (spec §4.3): the caller
shouldn't need to know an eCourts state code any more than they'd need to
know which of the 25 old per-court scripts used to handle a given court.
"""

import logging
from datetime import date, timedelta
from typing import Optional

import psycopg2
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from adapters.ecourts.adapter import EcourtsAdapter
from adapters.supreme_court.adapter import SupremeCourtAdapter
from db import court_config
from orchestrator import batch_runner, job_registry

logger = logging.getLogger("scraper_backend_v2.scraper_router")

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

    background_tasks.add_task(
        _run_and_log, adapter_class(), req.court_id, req.court_code, from_date, to_date,
        _DATA_SOURCE_BY_ADAPTER[config["adapter"]], adapter_kwargs,
    )

    return {
        "status": "started",
        "adapter": config["adapter"],
        "court_id": req.court_id,
        "court_name": config["court_name"],
        "from_date": from_date,
        "to_date": to_date,
        "message": "Batch running in the background — poll GET /api/scraper/batches for progress.",
    }


def _run_and_log(adapter, court_id: int, court_code: str, from_date: str, to_date: str, data_source: str, adapter_kwargs: dict):
    try:
        batch_runner.run_batch(adapter, court_id, court_code, from_date, to_date, data_source, **adapter_kwargs)
    except Exception:
        logger.exception("Batch failed for court_id=%s %s -> %s", court_id, from_date, to_date)


@router.get("/batches")
def list_batches(limit: int = 25):
    try:
        return {"batches": job_registry.list_recent_batches(limit=limit)}
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while listing batches")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")
    except Exception:
        logger.exception("Unexpected error while listing batches")
        raise HTTPException(status_code=500, detail="Failed to list batches.")


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
