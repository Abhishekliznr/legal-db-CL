"""
Scraper control endpoints
--------------------------
- POST /api/scraper/start                        : launch a background scrape+ingest batch
- POST /api/scraper/sc/start                      : same, for the Supreme Court specifically — takes {from_date, to_date, headless}, court_id/court_code resolved server-side
- POST /api/scraper/mp/start                      : same, for MPHC specifically — takes {year} instead of {court_id, court_code, from_date, to_date}
- GET  /api/scraper/batches                       : recent batch history
- GET  /api/scraper/batches/{batch_id}             : one batch's header/summary fields
- GET  /api/scraper/batches/{batch_id}/records      : per-record (cr_raw_ingestions) breakdown for one batch
- POST /api/scraper/batches/{batch_id}/cancel       : request early stop of a RUNNING batch
- GET  /api/scraper/batches/{batch_id}/logs/stream  : SSE tail of that batch's in-memory log buffer
- GET  /api/scraper/status                        : raw_ingestions counts per pipeline stage

`court_id` is the only thing a caller needs to supply — which adapter to
run, which of that court's own extraction/promotion functions to call, and
which data_source value to tag records with are all resolved from
`court_scrape_config.adapter` via `_ADAPTER_REGISTRY` below, not passed in
the request. `_ADAPTER_REGISTRY` is the one place a new court gets wired
up: adding a court means adding one entry here, not touching this router's
logic.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import psycopg2
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from adapters.base import ScraperAdapter
from adapters.high_courts.mp.adapter import MPHighCourtAdapter
from adapters.high_courts.mp.promotion import promote_ingestion as _mp_promote
from adapters.supreme_court.adapter import SupremeCourtAdapter
from adapters.supreme_court.extraction import find_provision_paragraphs as _sc_find_provisions
from adapters.supreme_court.promotion import promote_ingestion as _sc_promote
from db import court_config, scrape_jobs
from orchestrator import batch_runner, job_registry, live_logs
from orchestrator.batch_runner import FindProvisionsFn, PromoteFn

logger = logging.getLogger("scraper_backend_v2.scraper_router")

# How often the SSE endpoint polls the in-memory buffer for new lines. Short
# enough to feel live, long enough that tailing a batch doesn't become a
# busy-loop — batch_runner.py itself only logs a handful of times a second
# at most, so there's nothing to gain from polling faster than this.
_LOG_STREAM_POLL_SECONDS = 1.0

router = APIRouter(prefix="/api/scraper", tags=["Scraper Control"])


@dataclass(frozen=True)
class AdapterSpec:
    adapter_class: type  # implements adapters.base.ScraperAdapter
    promote_fn: PromoteFn  # this court's own cr_cases promotion, e.g. adapters.supreme_court.promotion.promote_ingestion
    data_source: str  # data_source_enum value new records get tagged with — resolved here, never left to a DB column DEFAULT (a prior real bug: every source silently landed as 'ECOURTS')
    find_provisions_fn: Optional[FindProvisionsFn] = None  # optional: this court's OCR-text provision-paragraph finder, fed to pipeline.llm_enrichment.enrich_case
    run_enrichment: bool = True  # False for a court whose pipeline doesn't use LLM enrichment yet (e.g. Madhya Pradesh, as of this writing) — see batch_runner.run_batch's own docstring


# The one place a new court gets registered. Each entry names that court's
# own adapter + extraction/promotion pipeline (adapters/high_courts/<code>/
# for a High Court) — nothing generic here, by design (see adapters/__init__.py).
_ADAPTER_REGISTRY = {
    "supreme_court": AdapterSpec(
        adapter_class=SupremeCourtAdapter,
        promote_fn=_sc_promote,
        find_provisions_fn=_sc_find_provisions,
        data_source="SCI_WEBSITE",
    ),
    "high_court_mp": AdapterSpec(
        adapter_class=MPHighCourtAdapter,
        promote_fn=_mp_promote,
        # MP's sections/acts come straight from case-status's own Act lines
        # at promotion time (adapters/high_courts/mp/promotion.py) when
        # present. find_provision_paragraphs is reused as-is from Supreme
        # Court's own extraction module (its paragraph-finding logic isn't
        # SC-specific) so pipeline/llm_enrichment.py's enrich_case() can
        # fall back to the same LLM-based provision extraction Supreme
        # Court uses -- but only actually WRITES sections/acts when MP's
        # own promotion left them empty (see enrich_case()'s own
        # existing_sections guard), never overwriting real case-status data.
        find_provisions_fn=_sc_find_provisions,
        data_source="MPHC_WEBSITE",
    ),
}


class ScraperStartRequest(BaseModel):
    court_id: int = Field(..., description="courts.court_id to scrape — its court_scrape_config row decides the adapter")
    court_code: str = Field(..., description="Short code used in blob paths, e.g. 'SCIN' or 'DHC'")
    from_date: Optional[str] = Field(None, description="YYYY-MM-DD; defaults to 7 days ago")
    to_date: Optional[str] = Field(None, description="YYYY-MM-DD; defaults to today")
    headless: bool = Field(True, description="Set False for a supervised dry run against a real browser window")


def _dispatch_batch(
    court_id: int,
    court_code: str,
    from_date: str,
    to_date: str,
    headless: bool,
    background_tasks: BackgroundTasks,
) -> dict:
    """Shared by every /start-shaped endpoint: resolves court_scrape_config -> _ADAPTER_REGISTRY, creates the batch row, schedules the background run. Raises HTTPException on any resolution failure."""
    try:
        config = court_config.get_court_scrape_config(court_id)
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while resolving court %s config", court_id)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")

    if config is None:
        raise HTTPException(
            status_code=404,
            detail=f"No court_scrape_config for court_id={court_id}. Seed it first (PUT /api/courts/{{court_id}}/config or python -m db.seed_courts).",
        )
    if not config["is_active"]:
        raise HTTPException(status_code=400, detail=f"court_id={court_id} is marked inactive in court_scrape_config.")

    spec = _ADAPTER_REGISTRY.get(config["adapter"])
    if spec is None:
        raise HTTPException(status_code=500, detail=f"court_scrape_config has unknown adapter '{config['adapter']}' — not in _ADAPTER_REGISTRY.")

    # config["config"] is that court's own free-form settings (JSONB), e.g.
    # whatever adapters/high_courts/<code>/adapter.py needs beyond headless —
    # nothing generic depends on its shape, each adapter's scrape() reads
    # only the keys it defined.
    adapter_kwargs = {**(config.get("config") or {}), "headless": headless}

    # Created here, synchronously, rather than inside the background task itself — so the
    # response below can hand batch_id straight back to the caller (the admin UI navigates to
    # /admin/scraping/history/{batch_id} on this response) instead of it only existing once the
    # background task happens to get scheduled.
    batch_id = scrape_jobs.create_batch(court_id, batch_runner.to_date(from_date), batch_runner.to_date(to_date))

    background_tasks.add_task(
        _run_and_log, spec.adapter_class(), spec.promote_fn, spec.find_provisions_fn, spec.run_enrichment,
        batch_id, court_id, court_code, from_date, to_date, spec.data_source, adapter_kwargs,
    )

    return {
        "status": "started",
        "batch_id": batch_id,
        "adapter": config["adapter"],
        "court_id": court_id,
        "court_name": config["court_name"],
        "from_date": from_date,
        "to_date": to_date,
        "message": "Batch running in the background — poll GET /api/scraper/batches for progress.",
    }


@router.post("/start")
def start_scrape(req: ScraperStartRequest, background_tasks: BackgroundTasks):
    from_date = req.from_date or (date.today() - timedelta(days=7)).isoformat()
    to_date = req.to_date or date.today().isoformat()
    return _dispatch_batch(req.court_id, req.court_code, from_date, to_date, req.headless, background_tasks)


class SCScraperStartRequest(BaseModel):
    from_date: Optional[str] = Field(None, description="YYYY-MM-DD; defaults to 7 days ago")
    to_date: Optional[str] = Field(None, description="YYYY-MM-DD; defaults to today")
    headless: bool = Field(True, description="Set False for a supervised dry run against a real browser window")


@router.post("/sc/start")
def start_sc_scrape(req: SCScraperStartRequest, background_tasks: BackgroundTasks):
    """
    Convenience endpoint for the Supreme Court specifically — resolves
    court_id/court_code from court_code='SCIN' (db/seed_courts.py) so the
    caller doesn't need to know/pass them. Equivalent to POST /start with
    court_id resolved for the Supreme Court.
    """
    court_id = court_config.get_court_id_by_code("SCIN")
    if court_id is None:
        raise HTTPException(status_code=404, detail="No court with court_code='SCIN' — seed it first (python -m db.seed_courts).")
    from_date = req.from_date or (date.today() - timedelta(days=7)).isoformat()
    to_date = req.to_date or date.today().isoformat()
    return _dispatch_batch(court_id, "SCIN", from_date, to_date, req.headless, background_tasks)


class MPScraperStartRequest(BaseModel):
    year: int = Field(..., ge=1956, le=date.today().year, description="ILR year to search on portal.mphc.gov.in/ilrs, e.g. 2024")
    headless: bool = Field(True, description="Set False for a supervised dry run against a real browser window")


@router.post("/mp/start")
def start_mp_scrape(req: MPScraperStartRequest, background_tasks: BackgroundTasks):
    """
    Convenience endpoint for Madhya Pradesh High Court specifically — takes
    just a year (MP is queried by ILR year, not a date range; see
    adapters/high_courts/mp/adapter.py's _year_from_range) instead of
    requiring the caller to know MPHC's court_id or construct a date range
    themselves. Equivalent to POST /start with court_id resolved from
    court_code='MPHC' and from_date/to_date spanning Jan 1 - Dec 31 of `year`.
    """
    court_id = court_config.get_court_id_by_code("MPHC")
    if court_id is None:
        raise HTTPException(status_code=404, detail="No court with court_code='MPHC' — seed it first (python -m db.seed_courts).")
    from_date = f"{req.year}-01-01"
    to_date = f"{req.year}-12-31"
    return _dispatch_batch(court_id, "MPHC", from_date, to_date, req.headless, background_tasks)


def _run_and_log(
    adapter: ScraperAdapter,
    promote_fn: PromoteFn,
    find_provisions_fn: Optional[FindProvisionsFn],
    run_enrichment: bool,
    batch_id: int,
    court_id: int,
    court_code: str,
    from_date: str,
    to_date: str,
    data_source: str,
    adapter_kwargs: dict,
):
    try:
        batch_runner.run_batch(
            adapter, promote_fn, batch_id, court_id, court_code, from_date, to_date, data_source,
            find_provisions_fn=find_provisions_fn, run_enrichment=run_enrichment, **adapter_kwargs,
        )
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
