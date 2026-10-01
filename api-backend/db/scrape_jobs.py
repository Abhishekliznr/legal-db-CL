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
from typing import Any, Dict, List, Optional, Sequence, Tuple

from psycopg2.extras import Json

from db.connection import get_pooled_connection

ACTIVE_STATUSES = ("QUEUED", "RUNNING")
OPEN_ITEM_STATUSES = ("PENDING", "FAILED")

# The admin panel's status filter: every way a run can end early without an admin stopping it.
STATUS_GROUPS = {
    "running": ("QUEUED", "RUNNING"),
    "completed": ("COMPLETED",),
    "cancelled": ("CANCELLED",),
    "stopped": ("FAILED", "RATE_LIMITED", "SOURCE_BLOCKED", "SOURCE_UNAVAILABLE", "STRUCTURE_CHANGED"),
}

RECORD_STATUS_GROUPS = {
    "promoted": ("PROMOTED",),
    "in_progress": ("QUEUED", "DOWNLOADED", "OCR_DONE"),
    "needs_review": ("NEEDS_REVIEW",),
    "failed": ("DOWNLOAD_FAILED", "OCR_FAILED", "PROMOTION_FAILED"),
}

BATCH_SORTS = {
    "newest": "sb.requested_at DESC, sb.batch_id DESC",
    "oldest": "sb.requested_at ASC, sb.batch_id ASC",
    "promoted": "sb.total_promoted DESC, sb.requested_at DESC",
    "duration": "COALESCE(sb.finished_at, now()) - sb.requested_at DESC",
}

# Day boundaries for "cases added per day" — the admins using it are in India.
_REPORT_TZ = "Asia/Kolkata"

# SQL twin of routers/scraper_router.py's _has_work_to_resume(retry_skipped=False), so the batch list can
# flag rows whose plain Resume button will work, without a per-row query. Skipped-only batches are left
# to the detail page's "Retry skipped". %(resumable_adapters)s is scraper/courts.py's resumable set.
_RESUMABLE_SQL = """(
    sb.status <> ALL(%(active_statuses)s)
    AND csc.adapter = ANY(%(resumable_adapters)s)
    AND (
        (sb.discovered_at IS NULL AND sb.status <> 'COMPLETED')
        OR EXISTS (SELECT 1 FROM cr_batch_items bi
                   WHERE bi.batch_id = sb.batch_id AND bi.status IN ('PENDING', 'FAILED'))
    )
)"""

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


def _actor_fields(actor: Optional[Dict[str, Any]]) -> tuple:
    actor = actor or {}
    return actor.get("id"), actor.get("name"), actor.get("email")


def _actor_from_row(actor_id: Optional[str], name: Optional[str], email: Optional[str]) -> Optional[Dict[str, Any]]:
    return {"id": actor_id, "name": name, "email": email} if actor_id else None


def _record_event(cur, batch_id: int, event_type: str, message: Optional[str] = None, details: Optional[Dict[str, Any]] = None,
                  actor: Optional[Dict[str, Any]] = None) -> None:
    cur.execute("""
        INSERT INTO cr_batch_events (batch_id, run_number, event_type, message, details, actor_id, actor_name, actor_email)
        SELECT %s, run_count, %s, %s, %s, %s, %s, %s FROM cr_scrape_batches WHERE batch_id = %s;
    """, (batch_id, event_type, message, Json(details or {}), *_actor_fields(actor), batch_id))


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


def create_queued_batch(court_id: int, date_from: date, date_to: date, actor: Optional[Dict[str, Any]] = None) -> int:
    """Raises CourtBusyError if an active batch for this court overlaps the date range."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            _lock_court_and_check_no_overlap(cur, court_id, date_from, date_to)
            cur.execute("""
                INSERT INTO cr_scrape_batches (court_id, date_from, date_to, status, queued_at,
                                               requested_by_id, requested_by_name, requested_by_email)
                VALUES (%s, %s, %s, 'QUEUED', now(), %s, %s, %s)
                RETURNING batch_id;
            """, (court_id, date_from, date_to, *_actor_fields(actor)))
            batch_id = cur.fetchone()[0]
            _record_event(cur, batch_id, "STARTED", details={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}, actor=actor)
        conn.commit()
    return batch_id


def claim_batch_resume(
    batch_id: int, retry_skipped: bool = False, actor: Optional[Dict[str, Any]] = None, retry_no_judgment: bool = False,
) -> Optional[Dict[str, Any]]:
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
            retried_skipped = retried_no_judgment = 0
            if retry_skipped:
                cur.execute(
                    "UPDATE cr_batch_items SET status = 'PENDING', updated_at = now() WHERE batch_id = %s AND status = 'SKIPPED';",
                    (batch_id,),
                )
                retried_skipped = cur.rowcount
            if retry_no_judgment:
                cur.execute(
                    "UPDATE cr_batch_items SET status = 'PENDING', updated_at = now() WHERE batch_id = %s AND status = 'NO_JUDGMENT';",
                    (batch_id,),
                )
                retried_no_judgment = cur.rowcount
            cur.execute(
                "SELECT COUNT(*) FROM cr_batch_items WHERE batch_id = %s AND status = ANY(%s);",
                (batch_id, list(OPEN_ITEM_STATUSES)),
            )
            cases_left = cur.fetchone()[0]
            _record_event(cur, batch_id, "RESUMED", details={
                "cases_left": cases_left, "retried_skipped": retried_skipped, "retried_no_judgment": retried_no_judgment,
            }, actor=actor)
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


def request_cancel(batch_id: int, actor: Optional[Dict[str, Any]] = None) -> str:
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
                _record_event(cur, batch_id, "STOP_REQUESTED", "Stop requested from the admin panel", actor=actor)
                _record_event(cur, batch_id, "CANCELLED", "Stopped before the scraper job started")
                result = "cancelled"
            else:
                if not already_requested:
                    cur.execute("UPDATE cr_scrape_batches SET cancel_requested = TRUE WHERE batch_id = %s;", (batch_id,))
                    _record_event(cur, batch_id, "STOP_REQUESTED", "Stop requested from the admin panel", actor=actor)
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


_BATCH_FROM = """
    FROM cr_scrape_batches sb
    LEFT JOIN cr_courts c ON c.court_id = sb.court_id
    LEFT JOIN cr_court_scrape_config csc ON csc.court_id = sb.court_id
"""


def _batch_filters(court_ids: Optional[Sequence[int]], requested_within_hours: Optional[int],
                   q: Optional[str], started_by: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    clauses, params = ["TRUE"], {}
    if court_ids:
        clauses.append("sb.court_id = ANY(%(court_ids)s)")
        params["court_ids"] = list(court_ids)
    if requested_within_hours:
        clauses.append("sb.requested_at >= now() - make_interval(hours => %(within_hours)s)")
        params["within_hours"] = requested_within_hours
    q = (q or "").strip().lstrip("#")
    if q.isdigit():
        clauses.append("sb.batch_id::text LIKE %(q_prefix)s")
        params["q_prefix"] = f"{q}%"
    elif q:
        clauses.append("(c.court_name ILIKE %(q_like)s OR c.court_code ILIKE %(q)s)")
        params["q_like"], params["q"] = f"%{q}%", q
    if started_by == "none":
        clauses.append("sb.requested_by_id IS NULL")
    elif started_by:
        clauses.append("sb.requested_by_id = %(started_by)s")
        params["started_by"] = started_by
    return " AND ".join(clauses), params


def list_batches(limit: int = 50, offset: int = 0, court_ids: Optional[Sequence[int]] = None, status_group: Optional[str] = None,
                 requested_within_hours: Optional[int] = None, q: Optional[str] = None, sort: str = "newest",
                 started_by: Optional[str] = None, resumable_adapters: Sequence[str] = ()) -> Dict[str, Any]:
    where, params = _batch_filters(court_ids, requested_within_hours, q, started_by)
    params.update(active_statuses=list(ACTIVE_STATUSES), resumable_adapters=list(resumable_adapters), limit=limit, offset=offset)
    status_where = ""
    if status_group:
        status_where = " AND sb.status = ANY(%(group_statuses)s)"
        params["group_statuses"] = list(STATUS_GROUPS[status_group])

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT sb.batch_id, sb.court_id, c.court_name, c.court_code, sb.date_from, sb.date_to,
                       sb.requested_at, sb.status, sb.total_found, sb.total_downloaded, sb.total_promoted,
                       sb.finished_at, sb.error_message, sb.run_count,
                       sb.requested_by_id, sb.requested_by_name, sb.requested_by_email,
                       {_RESUMABLE_SQL} AS resumable
                {_BATCH_FROM}
                WHERE {where}{status_where}
                ORDER BY {BATCH_SORTS[sort]}
                LIMIT %(limit)s OFFSET %(offset)s;
            """, params)
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            for row in rows:
                row["requested_by"] = _actor_from_row(row.pop("requested_by_id"), row.pop("requested_by_name"), row.pop("requested_by_email"))

            cur.execute(f"SELECT COUNT(*) {_BATCH_FROM} WHERE {where}{status_where};", params)
            total = cur.fetchone()[0]

            # Ignores status_group on purpose: the status filter chips show every group's count.
            cur.execute(f"""
                SELECT sb.status, COUNT(*), COALESCE(SUM(sb.total_promoted), 0), COUNT(*) FILTER (WHERE {_RESUMABLE_SQL})
                {_BATCH_FROM}
                WHERE {where}
                GROUP BY sb.status;
            """, params)
            summary_rows = cur.fetchall()

    counts_by_status = {status: count for status, count, _, _ in summary_rows}
    summary = {
        "total": sum(counts_by_status.values()),
        "promoted": sum(promoted for _, _, promoted, _ in summary_rows),
        "resumable": sum(resumable for _, _, _, resumable in summary_rows),
        "counts_by_status": counts_by_status,
        "counts_by_group": {group: sum(counts_by_status.get(s, 0) for s in statuses) for group, statuses in STATUS_GROUPS.items()},
    }
    return {"batches": rows, "total": total, "summary": summary}


def courts_summary() -> Dict[str, Any]:
    """Active (configured) courts with their case and batch counts, plus case totals across every court."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.court_id, c.court_name, c.court_code, c.court_type, csc.adapter, csc.last_scraped_to,
                       COALESCE(cc.cases, 0) AS cases, COALESCE(cc.cases_last_24h, 0) AS cases_last_24h,
                       lb.last_batch_at
                FROM cr_courts c
                JOIN cr_court_scrape_config csc ON csc.court_id = c.court_id AND csc.is_active
                LEFT JOIN (
                    SELECT court_id, COUNT(*) AS cases,
                           COUNT(*) FILTER (WHERE created_at >= now() - interval '24 hours') AS cases_last_24h
                    FROM cr_cases GROUP BY court_id
                ) cc ON cc.court_id = c.court_id
                LEFT JOIN (
                    SELECT court_id, MAX(requested_at) AS last_batch_at FROM cr_scrape_batches GROUP BY court_id
                ) lb ON lb.court_id = c.court_id
                ORDER BY c.court_name;
            """)
            columns = [desc[0] for desc in cur.description]
            courts = [dict(zip(columns, row)) for row in cur.fetchall()]

            cur.execute("SELECT court_id, status, COUNT(*) FROM cr_scrape_batches GROUP BY court_id, status;")
            counts: Dict[int, Dict[str, int]] = {}
            for court_id, status, count in cur.fetchall():
                counts.setdefault(court_id, {})[status] = count

            cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE created_at >= now() - interval '24 hours') FROM cr_cases;")
            total_cases, cases_last_24h = cur.fetchone()

    for court in courts:
        court["batch_counts"] = counts.get(court["court_id"], {})
    return {
        "courts": courts,
        "total_cases": total_cases,
        "cases_last_24h": cases_last_24h,
        "unconfigured_cases": total_cases - sum(court["cases"] for court in courts),
    }


def cases_daily(days: int = 14, court_id: Optional[int] = None) -> List[Dict[str, Any]]:
    court_filter = "AND court_id = %(court_id)s" if court_id else ""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                WITH bounds AS (SELECT (now() AT TIME ZONE %(tz)s)::date AS today),
                days AS (
                    SELECT generate_series(today - (%(days)s - 1), today, interval '1 day')::date AS day FROM bounds
                ),
                counts AS (
                    SELECT (created_at AT TIME ZONE %(tz)s)::date AS day, COUNT(*) AS n
                    FROM cr_cases, bounds
                    WHERE created_at >= (bounds.today - (%(days)s - 1))::timestamp AT TIME ZONE %(tz)s {court_filter}
                    GROUP BY 1
                )
                SELECT days.day, COALESCE(counts.n, 0)::int FROM days LEFT JOIN counts USING (day) ORDER BY days.day;
            """, {"tz": _REPORT_TZ, "days": days, "court_id": court_id})
            return [{"date": day.isoformat(), "count": n} for day, n in cur.fetchall()]


def get_batch(batch_id: int) -> Optional[Dict[str, Any]]:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sb.batch_id, sb.court_id, c.court_name, c.court_code, sb.date_from, sb.date_to,
                       sb.requested_at, sb.status, sb.total_found, sb.total_downloaded, sb.total_promoted,
                       sb.cancel_requested, sb.finished_at, sb.error_message,
                       sb.run_count, sb.discovered_at IS NOT NULL AS discovered,
                       sb.queued_at, sb.claimed_at, sb.heartbeat_at, sb.job_name,
                       sb.requested_by_id, sb.requested_by_name, sb.requested_by_email
                FROM cr_scrape_batches sb
                LEFT JOIN cr_courts c ON c.court_id = sb.court_id
                WHERE sb.batch_id = %s;
            """, (batch_id,))
            row = cur.fetchone()
            if row is None:
                return None
            columns = [desc[0] for desc in cur.description]
            batch = dict(zip(columns, row))
            batch["requested_by"] = _actor_from_row(batch.pop("requested_by_id"), batch.pop("requested_by_name"), batch.pop("requested_by_email"))

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

            # The worker only writes the total_* columns when a run finishes, so derive them live
            # the same way scraper-backend's batch_totals_from_items() does. Batches with no case
            # list (never discovered, or pre-resume) keep the stored columns.
            if batch["item_counts"]:
                cur.execute("SELECT COUNT(*), COUNT(ingestion_id) FROM cr_batch_items WHERE batch_id = %s;", (batch_id,))
                batch["total_found"], batch["total_downloaded"] = cur.fetchone()
                batch["total_promoted"] = batch["counts_by_status"].get("PROMOTED", 0)

            cur.execute("""
                SELECT event_id, run_number, event_type, occurred_at, message, details, actor_id, actor_name, actor_email
                FROM cr_batch_events WHERE batch_id = %s
                ORDER BY occurred_at, event_id;
            """, (batch_id,))
            columns = [desc[0] for desc in cur.description]
            events = [dict(zip(columns, row)) for row in cur.fetchall()]
            for event in events:
                event["actor"] = _actor_from_row(event.pop("actor_id"), event.pop("actor_name"), event.pop("actor_email"))
            batch["events"] = events or _derived_events(batch)

    return batch


def get_batch_status(batch_id: int) -> Optional[str]:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM cr_scrape_batches WHERE batch_id = %s;", (batch_id,))
            row = cur.fetchone()
    return row[0] if row else None


def list_ingestions_by_batch(batch_id: int, limit: int = 100, offset: int = 0, status_group: Optional[str] = None) -> Dict[str, Any]:
    status_filter = "AND ri.status = ANY(%(statuses)s::ingestion_status_enum[])" if status_group else ""
    params = {"batch_id": batch_id, "limit": limit, "offset": offset,
              "statuses": list(RECORD_STATUS_GROUPS[status_group]) if status_group else None}
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT ri.ingestion_id, ri.status, ri.source_pdf_url, ri.error_message,
                       ri.downloaded_at, ri.ocr_completed_at, ri.case_id,
                       c.liznr_id, c.case_number, c.judgment_date,
                       c.enrichment_status, c.enrichment_error, c.enriched_at
                FROM cr_raw_ingestions ri
                LEFT JOIN cr_cases c ON c.case_id = ri.case_id
                WHERE ri.batch_id = %(batch_id)s {status_filter}
                ORDER BY ri.ingestion_id
                LIMIT %(limit)s OFFSET %(offset)s;
            """, params)
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]

            cur.execute(f"SELECT COUNT(*) FROM cr_raw_ingestions ri WHERE ri.batch_id = %(batch_id)s {status_filter};", params)
            total = cur.fetchone()[0]

    return {"records": rows, "total": total}


def list_batch_items(batch_id: int, status: str, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    params = {"batch_id": batch_id, "status": status, "limit": limit, "offset": offset}
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT bi.item_id, bi.position, bi.item_key, bi.reason, bi.attempts, bi.updated_at,
                       COALESCE(bi.payload->>'pdf_url', c.source_pdf_url) AS pdf_url,
                       bi.case_id, c.liznr_id, c.case_number
                FROM cr_batch_items bi
                LEFT JOIN cr_cases c ON c.case_id = bi.case_id
                WHERE bi.batch_id = %(batch_id)s AND bi.status = %(status)s
                ORDER BY bi.position
                LIMIT %(limit)s OFFSET %(offset)s;
            """, params)
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]

            cur.execute("SELECT COUNT(*) FROM cr_batch_items WHERE batch_id = %(batch_id)s AND status = %(status)s;", params)
            total = cur.fetchone()[0]

    return {"items": rows, "total": total}


# Newest batch that handled each case, so the admin tracker can link to it and retry through it.
_MISSING_JUDGMENTS_FROM = """
    FROM cr_cases c
    JOIN cr_courts crt ON crt.court_id = c.court_id
    LEFT JOIN LATERAL (
        SELECT bi.batch_id, bi.attempts FROM cr_batch_items bi WHERE bi.case_id = c.case_id ORDER BY bi.item_id DESC LIMIT 1
    ) last_item ON TRUE
    LEFT JOIN cr_scrape_batches sb ON sb.batch_id = last_item.batch_id
"""


def list_missing_judgments(limit: int = 50, offset: int = 0, court_ids: Optional[Sequence[int]] = None,
                           judgment_status: Optional[str] = None, q: Optional[str] = None) -> Dict[str, Any]:
    """Cases saved without a judgment (NOT_PUBLISHED / DOWNLOAD_FAILED), newest check first, plus per-court/status counts."""
    clauses, params = ["c.judgment_status <> 'AVAILABLE'"], {"limit": limit, "offset": offset}
    if court_ids:
        clauses.append("c.court_id = ANY(%(court_ids)s)")
        params["court_ids"] = list(court_ids)
    q = (q or "").strip()
    if q:
        clauses.append("(c.case_number ILIKE %(q_like)s OR c.liznr_id ILIKE %(q_like)s OR c.petitioner ILIKE %(q_like)s OR c.respondent ILIKE %(q_like)s)")
        params["q_like"] = f"%{q}%"
    base_where = " AND ".join(clauses)
    where = base_where
    if judgment_status:
        where += " AND c.judgment_status = %(judgment_status)s"
        params["judgment_status"] = judgment_status

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT c.case_id, c.liznr_id, c.case_number, c.court_id, crt.court_name, crt.court_code,
                       c.petitioner, c.respondent, c.judgment_date, c.judgment_status, c.judgment_missing_reason,
                       c.judgment_checked_at, c.source_pdf_url, c.created_at,
                       last_item.batch_id, last_item.attempts, sb.status AS batch_status
                {_MISSING_JUDGMENTS_FROM}
                WHERE {where}
                ORDER BY c.judgment_checked_at DESC NULLS LAST, c.case_id DESC
                LIMIT %(limit)s OFFSET %(offset)s;
            """, params)
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]

            cur.execute(f"SELECT COUNT(*) {_MISSING_JUDGMENTS_FROM} WHERE {where};", params)
            total = cur.fetchone()[0]

            # Ignores the status filter on purpose: the status chips show every status's count.
            cur.execute(f"""
                SELECT c.court_id, crt.court_name, crt.court_code, c.judgment_status, COUNT(*)
                FROM cr_cases c JOIN cr_courts crt ON crt.court_id = c.court_id
                WHERE {base_where}
                GROUP BY c.court_id, crt.court_name, crt.court_code, c.judgment_status
                ORDER BY crt.court_name;
            """, params)
            summary_rows = cur.fetchall()

    by_court: Dict[int, Dict[str, Any]] = {}
    counts_by_status: Dict[str, int] = {}
    for court_id, court_name, court_code, status, count in summary_rows:
        court = by_court.setdefault(court_id, {"court_id": court_id, "court_name": court_name, "court_code": court_code, "counts_by_status": {}})
        court["counts_by_status"][status] = count
        counts_by_status[status] = counts_by_status.get(status, 0) + count
    return {
        "cases": rows,
        "total": total,
        "summary": {"total": sum(counts_by_status.values()), "counts_by_status": counts_by_status, "courts": list(by_court.values())},
    }


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
               "details": {"date_from": batch["date_from"].isoformat(), "date_to": batch["date_to"].isoformat()},
               "actor": batch.get("requested_by")}]
    if batch["finished_at"] is not None:
        events.append({"event_id": None, "run_number": batch.get("run_count") or 1, "event_type": batch["status"],
                       "occurred_at": batch["finished_at"], "message": batch["error_message"], "details": {}, "actor": None})
    return events
