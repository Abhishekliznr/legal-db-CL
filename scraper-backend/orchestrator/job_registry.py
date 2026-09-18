"""
Status queries over cr_raw_ingestions/cr_scrape_batches (spec §4.4) —
replaces the old service's in-memory ACTIVE_JOBS/ACTIVE_THREADS/CANCEL_FLAGS
dicts. There is no in-memory job state here at all: a process restart loses
nothing, since every batch's progress is already durable in Postgres the
moment each record is processed.
"""

from typing import Any, Dict

from db.connection import get_pooled_connection
from db import scrape_jobs


def list_recent_batches(limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    return scrape_jobs.list_batches(limit=limit, offset=offset)


def pipeline_status_counts() -> Dict[str, int]:
    """
    Counts cr_raw_ingestions rows per status — the at-a-glance dashboard view:
    how many are stuck OCR_FAILED, how many are waiting on extraction, etc.
    """
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) FROM cr_raw_ingestions GROUP BY status;")
            return {status: count for status, count in cur.fetchall()}
