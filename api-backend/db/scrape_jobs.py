"""
Batch control/read queries over cr_scrape_batches, cr_raw_ingestions, cr_batch_items,
cr_batch_events and cr_batch_logs, for routers/scraper_router.py.

Batches are run by scraper-backend's worker (python -m worker), one K8s Job per run,
launched through scraper/job_launcher.py. The database is the only channel between
the two services: this module queues a run (status QUEUED), the worker claims it
(QUEUED -> RUNNING), heartbeats while it works and finishes it. See
scraper-backend/db/migrations/0015_on_demand_worker.sql.
"""

import time
from datetime import date
from typing import Any, Dict, List, Optional

from psycopg2.extras import Json

from db.connection import get_pooled_connection

ACTIVE_STATUSES = ("QUEUED", "RUNNING")
OPEN_ITEM_STATUSES = ("PENDING", "FAILED")

# A QUEUED run whose pod hasn't claimed it by then never started (image pull failure,
# unschedulable, trigger lost it). The worker heartbeats every 30s, so 5 min of silence
# means the pod is gone (OOM-killed, evicted past its grace period, node lost).
_QUEUED_TIMEOUT = "15 minutes"
_HEARTBEAT_TIMEOUT = "5 minutes"
_LOG_RETENTION = "7 days"
_SWEEP_INTERVAL_SECONDS = 60

# Serializes "does this range overlap an active batch?" + insert/claim across concurrent requests.
_COURT_LOCK_NAMESPACE = 41_207

_last_sweep = 0.0


class CourtBusyError(Exception):
    def __init__(self, batch_id: int, date_from: date, date_to: date):
        super().__init__(
            f"Batch #{batch_id} ({date_from.isoformat()} to {date_to.isoformat()}) is already queued or running "
            f"for this court with an overlapping date range."
        )
        self.batch_id = batch_id


def _record_event(cur, batch_id: int, event_type: str, message: Optional[str] = None, details: Optional[Dict[str, Any]] = None) -> None:
    cur.execute("""
        INSERT INTO cr_batch_events (batch_id, run_number, event_type, message, details)
        SELECT %s, run_count, %s, %s, %s FROM cr_scrape_batches WHERE batch_id = %s;
    """, (batch_id, event_type, message, Json(details or {}), batch_id))


def _lock_court_and_check_no_overlap(cur, court_id: int, date_from: date, date_to: date, exclude_batch_id: Optional[int] = None) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s);", (_COURT_LOCK_NAMESPACE, court_id))
    cur.execute("""
        SELECT batch_id, date_from, date_to FROM cr_scrape_batches
        WHERE court_id = %s AND status = ANY(%s) AND batch_id IS DISTINCT FROM %s
          AND date_from <= %s AND date_to >= %s
        ORDER BY batch_id LIMIT 1;
    """, (court_id, list(ACTIVE_STATUSES), exclude_batch_id, date_to, date_from))
    row = cur.fetchone()
    if row is not None:
        raise CourtBusyError(row[0], row[1], row[2])


def create_queued_batch(court_id: int, date_from: date, date_to: date) -> int:
    """Raises CourtBusyError if an active batch for this court overlaps the date range."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            _lock_court_and_check_no_overlap(cur, court_id, date_from, date_to)
            cur.execute("""
                INSERT INTO cr_scrape_batches (court_id, date_from, date_to, status, queued_at)
                VALUES (%s, %s, %s, 'QUEUED', now())
                RETURNING batch_id;
            """, (court_id, date_from, date_to))
            batch_id = cur.fetchone()[0]
            _record_event(cur, batch_id, "STARTED", details={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()})
        conn.commit()
    return batch_id


def claim_batch_resume(batch_id: int, retry_skipped: bool = False) -> Optional[Dict[str, Any]]:
    """Queues another run of a stopped batch (run_count + 1). None if it's already QUEUED/RUNNING or missing; CourtBusyError if another active batch overlaps its date range."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT court_id, date_from, date_to FROM cr_scrape_batches WHERE batch_id = %s;", (batch_id,))
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return None
            _lock_court_and_check_no_overlap(cur, row[0], row[1], row[2], exclude_batch_id=batch_id)

            cur.execute("""
                UPDATE cr_scrape_batches
                SET status = 'QUEUED', cancel_requested = FALSE, finished_at = NULL, error_message = NULL,
                    run_count = run_count + 1, queued_at = now(), claimed_at = NULL, heartbeat_at = NULL, job_name = NULL
                WHERE batch_id = %s AND status <> ALL(%s)
                RETURNING run_count;
            """, (batch_id, list(ACTIVE_STATUSES)))
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return None
            retried_skipped = 0
            if retry_skipped:
                cur.execute(
                    "UPDATE cr_batch_items SET status = 'PENDING', updated_at = now() WHERE batch_id = %s AND status = 'SKIPPED';",
                    (batch_id,),
                )
                retried_skipped = cur.rowcount
            cur.execute(
                "SELECT COUNT(*) FROM cr_batch_items WHERE batch_id = %s AND status = ANY(%s);",
                (batch_id, list(OPEN_ITEM_STATUSES)),
            )
            cases_left = cur.fetchone()[0]
            _record_event(cur, batch_id, "RESUMED", details={"cases_left": cases_left, "retried_skipped": retried_skipped})
        conn.commit()
    return {"run_count": row[0]}


def set_job_name(batch_id: int, job_name: str) -> None:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE cr_scrape_batches SET job_name = %s WHERE batch_id = %s;", (job_name, batch_id))
        conn.commit()


def fail_launch(batch_id: int, error_message: str) -> None:
    """The trigger call itself failed: close the QUEUED run right away instead of waiting for the stale sweep."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE cr_scrape_batches
                SET status = 'FAILED', finished_at = now(), error_message = %s
                WHERE batch_id = %s AND status = 'QUEUED'
                RETURNING batch_id;
            """, (error_message, batch_id))
            if cur.fetchone() is not None:
                _record_event(cur, batch_id, "FAILED", error_message)
        conn.commit()


def request_cancel(batch_id: int) -> str:
    """Returns 'cancelled' (was QUEUED, closed now), 'cancelling' (RUNNING, worker stops after its current record) or 'not_running'."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, cancel_requested FROM cr_scrape_batches WHERE batch_id = %s AND status = ANY(%s) FOR UPDATE;",
                (batch_id, list(ACTIVE_STATUSES)),
            )
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return "not_running"
            status, already_requested = row
            if status == "QUEUED":
                cur.execute(
                    "UPDATE cr_scrape_batches SET status = 'CANCELLED', cancel_requested = TRUE, finished_at = now() WHERE batch_id = %s;",
                    (batch_id,),
                )
                _record_event(cur, batch_id, "STOP_REQUESTED", "Stop requested from the admin panel")
                _record_event(cur, batch_id, "CANCELLED", "Stopped before the scraper job started")
                result = "cancelled"
            else:
                if not already_requested:
                    cur.execute("UPDATE cr_scrape_batches SET cancel_requested = TRUE WHERE batch_id = %s;", (batch_id,))
                    _record_event(cur, batch_id, "STOP_REQUESTED", "Stop requested from the admin panel")
                result = "cancelling"
        conn.commit()
    return result


def sweep_stale_batches(force: bool = False) -> List[Dict[str, Any]]:
    """Fails runs whose pod never started or stopped heartbeating, and prunes old log lines. Throttled to once a minute."""
    global _last_sweep
    now = time.monotonic()
    if not force and now - _last_sweep < _SWEEP_INTERVAL_SECONDS:
        return []
    _last_sweep = now

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE cr_scrape_batches
                SET status = CASE WHEN cancel_requested THEN 'CANCELLED' ELSE 'FAILED' END,
                    finished_at = now(),
                    error_message = CASE WHEN status = 'QUEUED'
                        THEN 'The scraper job never started (no pod picked this run up within {_QUEUED_TIMEOUT})'
                        ELSE 'The scraper worker stopped responding (pod killed, evicted or out of memory) — resume to continue'
                    END
                WHERE (status = 'QUEUED' AND COALESCE(queued_at, requested_at) < now() - interval '{_QUEUED_TIMEOUT}')
                   OR (status = 'RUNNING' AND COALESCE(heartbeat_at, claimed_at, requested_at) < now() - interval '{_HEARTBEAT_TIMEOUT}')
                RETURNING batch_id, status, error_message;
            """)
            rows = [{"batch_id": r[0], "status": r[1], "error_message": r[2]} for r in cur.fetchall()]
            for row in rows:
                _record_event(cur, row["batch_id"], "INTERRUPTED", row["error_message"], {"status": row["status"]})
            cur.execute(f"DELETE FROM cr_batch_logs WHERE logged_at < now() - interval '{_LOG_RETENTION}';")
        conn.commit()
    return rows


def list_batches(limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sb.batch_id, sb.court_id, c.court_name, c.court_code, sb.date_from, sb.date_to,
                       sb.requested_at, sb.status, sb.total_found, sb.total_downloaded, sb.total_promoted,
                       sb.finished_at, sb.error_message
                FROM cr_scrape_batches sb
                LEFT JOIN cr_courts c ON c.court_id = sb.court_id
                ORDER BY sb.requested_at DESC
                LIMIT %s OFFSET %s;
            """, (limit, offset))
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]

            cur.execute("SELECT COUNT(*) FROM cr_scrape_batches;")
            total = cur.fetchone()[0]

    return {"batches": rows, "total": total}


def get_batch(batch_id: int) -> Optional[Dict[str, Any]]:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sb.batch_id, sb.court_id, c.court_name, c.court_code, sb.date_from, sb.date_to,
                       sb.requested_at, sb.status, sb.total_found, sb.total_downloaded, sb.total_promoted,
                       sb.cancel_requested, sb.finished_at, sb.error_message,
                       sb.run_count, sb.discovered_at IS NOT NULL AS discovered,
                       sb.queued_at, sb.claimed_at, sb.heartbeat_at, sb.job_name
                FROM cr_scrape_batches sb
                LEFT JOIN cr_courts c ON c.court_id = sb.court_id
                WHERE sb.batch_id = %s;
            """, (batch_id,))
            row = cur.fetchone()
            if row is None:
                return None
            columns = [desc[0] for desc in cur.description]
            batch = dict(zip(columns, row))

            cur.execute("SELECT status, COUNT(*) FROM cr_raw_ingestions WHERE batch_id = %s GROUP BY status;", (batch_id,))
            batch["counts_by_status"] = {status: count for status, count in cur.fetchall()}

            cur.execute("""
                SELECT c.enrichment_status, COUNT(*)
                FROM cr_cases c
                JOIN cr_raw_ingestions ri ON ri.case_id = c.case_id
                WHERE ri.batch_id = %s
                GROUP BY c.enrichment_status;
            """, (batch_id,))
            batch["enrichment_counts"] = {status: count for status, count in cur.fetchall()}

            cur.execute("SELECT status, COUNT(*) FROM cr_batch_items WHERE batch_id = %s GROUP BY status;", (batch_id,))
            batch["item_counts"] = {status: count for status, count in cur.fetchall()}

            cur.execute("""
                SELECT event_id, run_number, event_type, occurred_at, message, details
                FROM cr_batch_events WHERE batch_id = %s
                ORDER BY occurred_at, event_id;
            """, (batch_id,))
            columns = [desc[0] for desc in cur.description]
            batch["events"] = [dict(zip(columns, row)) for row in cur.fetchall()] or _derived_events(batch)

    return batch


def get_batch_status(batch_id: int) -> Optional[str]:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM cr_scrape_batches WHERE batch_id = %s;", (batch_id,))
            row = cur.fetchone()
    return row[0] if row else None


def list_ingestions_by_batch(batch_id: int, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ri.ingestion_id, ri.status, ri.source_pdf_url, ri.error_message,
                       ri.downloaded_at, ri.ocr_completed_at, ri.case_id,
                       c.liznr_id, c.case_number, c.judgment_date,
                       c.enrichment_status, c.enrichment_error, c.enriched_at
                FROM cr_raw_ingestions ri
                LEFT JOIN cr_cases c ON c.case_id = ri.case_id
                WHERE ri.batch_id = %s
                ORDER BY ri.ingestion_id
                LIMIT %s OFFSET %s;
            """, (batch_id, limit, offset))
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]

            cur.execute("SELECT COUNT(*) FROM cr_raw_ingestions WHERE batch_id = %s;", (batch_id,))
            total = cur.fetchone()[0]

    return {"records": rows, "total": total}


def pipeline_status_counts() -> Dict[str, int]:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) FROM cr_raw_ingestions GROUP BY status;")
            return {status: count for status, count in cur.fetchall()}


def get_logs_since(batch_id: int, after_log_id: int, limit: int = 500) -> List[Dict[str, Any]]:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT log_id, EXTRACT(EPOCH FROM logged_at)::float8 AS ts, level, stage,
                       case_ref AS "case", item_index AS "index", item_total AS total, message
                FROM cr_batch_logs
                WHERE batch_id = %s AND log_id > %s
                ORDER BY log_id
                LIMIT %s;
            """, (batch_id, after_log_id, limit))
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def _derived_events(batch: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A minimal timeline for batches created before cr_batch_events existed."""
    events = [{"event_id": None, "run_number": 1, "event_type": "STARTED", "occurred_at": batch["requested_at"], "message": None,
               "details": {"date_from": batch["date_from"].isoformat(), "date_to": batch["date_to"].isoformat()}}]
    if batch["finished_at"] is not None:
        events.append({"event_id": None, "run_number": batch.get("run_count") or 1, "event_type": batch["status"],
                       "occurred_at": batch["finished_at"], "message": batch["error_message"], "details": {}})
    return events
