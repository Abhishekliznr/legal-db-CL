"""
CRUD/queries over `cr_scrape_batches` + `cr_raw_ingestions` — the tables
that replace the old in-memory ACTIVE_JOBS/ACTIVE_THREADS/CANCEL_FLAGS
dicts as the source of truth for job state
(docs/scraper-backend-revamp-spec.md §4.4).

This module is pure SQL against tables that already exist after Phase 0's
schema init — it has no dependency on the orchestrator/adapters packages
that consume it in Phase 1, which is why the query layer ships now.
"""

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import Json

from db.connection import get_pooled_connection


def _record_event(cur, batch_id: int, event_type: str, message: Optional[str] = None, details: Optional[Dict[str, Any]] = None) -> None:
    """Appends a cr_batch_events row for the batch's current run, inside the caller's transaction."""
    cur.execute("""
        INSERT INTO cr_batch_events (batch_id, run_number, event_type, message, details)
        SELECT %s, run_count, %s, %s, %s FROM cr_scrape_batches WHERE batch_id = %s;
    """, (batch_id, event_type, message, Json(details or {}), batch_id))


def create_batch(court_id: int, date_from: date, date_to: date) -> int:
    """Creates a cr_scrape_batches row and returns its batch_id."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cr_scrape_batches (court_id, date_from, date_to)
                VALUES (%s, %s, %s)
                RETURNING batch_id;
            """, (court_id, date_from, date_to))
            batch_id = cur.fetchone()[0]
            _record_event(cur, batch_id, "STARTED", details={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()})
        conn.commit()
    return batch_id


def finish_batch(
    batch_id: int,
    status: str,
    total_found: int,
    total_downloaded: int,
    total_promoted: int = 0,
    error_message: Optional[str] = None,
    run_details: Optional[Dict[str, Any]] = None,
) -> None:
    """`run_details` (this run's counts/duration) goes on the finish event, named after `status`."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE cr_scrape_batches
                SET status = %s, total_found = %s, total_downloaded = %s, total_promoted = %s,
                    finished_at = now(), error_message = %s
                WHERE batch_id = %s;
            """, (status, total_found, total_downloaded, total_promoted, error_message, batch_id))
            _record_event(cur, batch_id, status, error_message, run_details)
        conn.commit()


def fail_orphaned_batches() -> List[Dict[str, Any]]:
    """Closes every RUNNING batch at startup — batches run in-process, so none can have survived a restart."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE cr_scrape_batches
                SET status = CASE WHEN cancel_requested THEN 'CANCELLED' ELSE 'FAILED' END,
                    finished_at = now(),
                    error_message = 'Interrupted by server restart'
                WHERE status = 'RUNNING'
                RETURNING batch_id, status;
            """)
            rows = [{"batch_id": r[0], "status": r[1]} for r in cur.fetchall()]
            for row in rows:
                _record_event(
                    cur, row["batch_id"], "INTERRUPTED",
                    "The server restarted while this batch was running" + (" (a stop had been requested)" if row["status"] == "CANCELLED" else ""),
                    {"status": row["status"]},
                )
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
    """
    Single-batch fetch for GET /api/scraper/batches/{id} — same header fields as list_batches()
    plus cancel_requested, a per-stage `counts_by_status` breakdown (cr_raw_ingestions.status,
    scoped to this batch — the pipeline stepper's data), and `enrichment_counts` (cr_cases
    .enrichment_status for the cases THIS batch promoted — the LLM Enrichment card's data).
    Both aggregates are computed here, alongside the header row, rather than making the caller
    page through GET .../records and tally client-side.
    """
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sb.batch_id, sb.court_id, c.court_name, c.court_code, sb.date_from, sb.date_to,
                       sb.requested_at, sb.status, sb.total_found, sb.total_downloaded, sb.total_promoted,
                       sb.cancel_requested, sb.finished_at, sb.error_message,
                       sb.run_count, sb.discovered_at IS NOT NULL AS discovered
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


def request_cancel(batch_id: int) -> bool:
    """
    Flags a RUNNING batch for cancellation — orchestrator/batch_runner.py's per-record loop polls
    this between records (see is_cancel_requested()) and stops early once set, marking the batch
    CANCELLED itself rather than this function touching `status` directly (the loop is the only
    writer that knows how many records it actually got through before stopping).

    Returns False (a no-op, not an error) if the batch isn't RUNNING — there is nothing to cancel
    for a batch that has already finished.
    """
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cancel_requested FROM cr_scrape_batches WHERE batch_id = %s AND status = 'RUNNING' FOR UPDATE;",
                (batch_id,),
            )
            row = cur.fetchone()
            if row is not None and not row[0]:
                cur.execute("UPDATE cr_scrape_batches SET cancel_requested = TRUE WHERE batch_id = %s;", (batch_id,))
                _record_event(cur, batch_id, "STOP_REQUESTED", "Stop requested from the admin panel")
        conn.commit()
    return row is not None


def is_cancel_requested(batch_id: int) -> bool:
    """Cheap per-record poll for batch_runner.py's loop — one indexed lookup on the primary key."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT cancel_requested FROM cr_scrape_batches WHERE batch_id = %s;", (batch_id,))
            row = cur.fetchone()
    return bool(row and row[0])


# An existing ingestion in one of these states never reached cr_cases (the batch
# stopped mid-pipeline), so the same PDF coming back must re-run the pipeline on
# that row, not be treated as a duplicate -- otherwise that case is lost for good.
_UNFINISHED_INGESTION_STATUSES = ("QUEUED", "DOWNLOADED", "DOWNLOAD_FAILED", "OCR_DONE", "OCR_FAILED", "PROMOTION_FAILED")


def insert_raw_ingestion(
    batch_id: int,
    court_id: int,
    source_pdf_url: str,
    file_checksum: str,
    data_source: str,
    blob_pdf_id: Optional[str] = None,
    page_count: Optional[int] = None,
) -> Tuple[Optional[int], bool]:
    """
    Inserts a cr_raw_ingestions row for a freshly-downloaded PDF and returns
    (ingestion_id, reused). When file_checksum already exists:
    - PROMOTED/NEEDS_REVIEW -> (None, False): a genuine duplicate.
    - any unfinished status -> that row is moved to this batch and reset to
      DOWNLOADED, returning (its id, True), so the pipeline runs on it again.

    data_source is required, not defaulted here, even though the column
    itself has a DEFAULT 'ECOURTS' in schema.sql — that default existing at
    all was the bug: every source silently landed as 'ECOURTS' (including
    Supreme Court) because nothing ever passed this column explicitly.
    Callers (orchestrator/batch_runner.py) must say which source produced
    the record; see data_source_enum in schema.sql for the allowed values.
    """
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cr_raw_ingestions (batch_id, court_id, source_pdf_url, file_checksum, data_source, blob_pdf_id, page_count, status, downloaded_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'DOWNLOADED', now())
                ON CONFLICT (file_checksum) DO NOTHING
                RETURNING ingestion_id;
            """, (batch_id, court_id, source_pdf_url, file_checksum, data_source, blob_pdf_id, page_count))
            row = cur.fetchone()
            if row is not None:
                conn.commit()
                return row[0], False

            cur.execute("""
                UPDATE cr_raw_ingestions
                SET batch_id = %s, source_pdf_url = %s, blob_pdf_id = COALESCE(%s, blob_pdf_id),
                    status = 'DOWNLOADED', error_message = NULL, downloaded_at = now(), updated_at = now()
                WHERE file_checksum = %s AND status::text = ANY(%s)
                RETURNING ingestion_id;
            """, (batch_id, source_pdf_url, blob_pdf_id, file_checksum, list(_UNFINISHED_INGESTION_STATUSES)))
            row = cur.fetchone()
        conn.commit()
    return (row[0], True) if row else (None, False)


def get_promoted_case_numbers(court_id: int) -> set:
    """Every cr_cases.case_number already promoted for this court — lets an adapter skip re-scraping them on a re-run."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT case_number FROM cr_cases WHERE court_id = %s;", (court_id,))
            return {row[0] for row in cur.fetchall()}


def get_ingestion(ingestion_id: int) -> Optional[Dict[str, Any]]:
    """Fetches one cr_raw_ingestions row — used by each pipeline stage to read what the previous stage wrote."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ingestion_id, batch_id, court_id, source_pdf_url, blob_pdf_id,
                       file_checksum, ocr_text, status, case_id, data_source
                FROM cr_raw_ingestions
                WHERE ingestion_id = %s;
            """, (ingestion_id,))
            row = cur.fetchone()
            if row is None:
                return None
            columns = [desc[0] for desc in cur.description]
            return dict(zip(columns, row))


def list_ingestions_by_batch(batch_id: int, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    """
    Per-record listing for GET /api/scraper/batches/{id}/records — one row per cr_raw_ingestions
    record in the batch, LEFT JOIN'd to cr_cases for the fields that only exist once a record is
    actually PROMOTED (case_number/judgment_date/enrichment_status/enrichment_error/liznr_id).
    A record that never reached promotion (still in flight, or *_FAILED/NEEDS_REVIEW) has
    case_id IS NULL, so every joined column comes back NULL for it — that's the caller's signal
    to fall back to source_pdf_url/status/error_message instead.
    """
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


def list_by_status(status: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Powers the pipeline workers: 'give me the next N rows in state X'."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ingestion_id, batch_id, court_id, source_pdf_url, blob_pdf_id,
                       file_checksum, ocr_text, status
                FROM cr_raw_ingestions
                WHERE status = %s
                ORDER BY created_at
                LIMIT %s;
            """, (status, limit))
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def update_status_in_tx(cur, ingestion_id: int, status: str, **fields: Any) -> None:
    """
    Same statement as update_status(), but against a cursor the caller
    already has open — so this status flip can commit in the SAME
    transaction as whatever else that caller is doing (e.g.
    pipeline/promotion.py setting status='PROMOTED' alongside the `cr_cases`
    INSERT it just did), instead of opening a second connection/transaction
    that could commit-or-not independently of the first.
    """
    set_clauses = ["status = %s"]
    params: List[Any] = [status]
    for column, value in fields.items():
        set_clauses.append(f"{column} = %s")
        params.append(Json(value) if isinstance(value, (dict, list)) else value)
    params.append(ingestion_id)

    cur.execute(
        f"UPDATE cr_raw_ingestions SET {', '.join(set_clauses)} WHERE ingestion_id = %s;",
        params,
    )


def update_status(ingestion_id: int, status: str, **fields: Any) -> None:
    """
    Advances a cr_raw_ingestions row's status, optionally setting other
    columns in the same statement (e.g. ocr_text=..., status='OCR_DONE'),
    in its own connection/transaction. Callers that already hold an open
    cursor as part of a larger transaction (e.g. promotion's own cases
    INSERT) should use update_status_in_tx(cur, ...) instead, to stay in
    that same transaction.
    """
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            update_status_in_tx(cur, ingestion_id, status, **fields)
        conn.commit()


# ---------------------------------------------------------------------
# Resumable batches (cr_batch_items, db/migrations/0013)
# ---------------------------------------------------------------------

# Statuses a resume picks up again; SKIPPED is added only when the caller asks
# (claim_batch_resume(retry_skipped=True)).
OPEN_ITEM_STATUSES = ("PENDING", "FAILED")


def save_batch_items(batch_id: int, items: List[Dict[str, Any]]) -> None:
    """Saves a discovered case list (dicts with key/payload/done_reason) in order and marks the batch discovered, in one transaction."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            for position, item in enumerate(items, start=1):
                done_reason = item.get("done_reason")
                cur.execute("""
                    INSERT INTO cr_batch_items (batch_id, position, item_key, payload, status, reason)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (batch_id, item_key) DO NOTHING;
                """, (batch_id, position, item["key"], Json(item["payload"]), "DONE" if done_reason else "PENDING", done_reason))
            cur.execute("UPDATE cr_scrape_batches SET discovered_at = now() WHERE batch_id = %s;", (batch_id,))
            already_done = sum(1 for item in items if item.get("done_reason"))
            _record_event(cur, batch_id, "DISCOVERED", details={
                "found": len(items), "already_done": already_done, "to_process": len(items) - already_done,
            })
        conn.commit()


def is_batch_discovered(batch_id: int) -> bool:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT discovered_at IS NOT NULL FROM cr_scrape_batches WHERE batch_id = %s;", (batch_id,))
            row = cur.fetchone()
    return bool(row and row[0])


def list_open_batch_items(batch_id: int) -> List[Dict[str, Any]]:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT item_id, position, item_key, payload
                FROM cr_batch_items
                WHERE batch_id = %s AND status = ANY(%s)
                ORDER BY position;
            """, (batch_id, list(OPEN_ITEM_STATUSES)))
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def mark_batch_item(item_id: int, status: str, reason: Optional[str] = None, ingestion_id: Optional[int] = None) -> None:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE cr_batch_items
                SET status = %s, reason = %s, ingestion_id = COALESCE(%s, ingestion_id),
                    attempts = attempts + 1, updated_at = now()
                WHERE item_id = %s;
            """, (status, reason, ingestion_id, item_id))
        conn.commit()


def batch_item_counts(batch_id: int) -> Dict[str, int]:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) FROM cr_batch_items WHERE batch_id = %s GROUP BY status;", (batch_id,))
            return {status: count for status, count in cur.fetchall()}


def batch_totals_from_items(batch_id: int) -> Dict[str, int]:
    """Whole-batch totals across every run, for finish_batch() on a resumable batch."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*), COUNT(ingestion_id),
                       (SELECT COUNT(*) FROM cr_raw_ingestions WHERE batch_id = %s AND status = 'PROMOTED')
                FROM cr_batch_items WHERE batch_id = %s;
            """, (batch_id, batch_id))
            found, downloaded, promoted = cur.fetchone()
    return {"total_found": found, "total_downloaded": downloaded, "total_promoted": promoted}


def claim_batch_resume(batch_id: int, retry_skipped: bool = False) -> Optional[Dict[str, Any]]:
    """
    Atomically flips a stopped batch back to RUNNING for another run (run_count + 1).
    Returns the batch's court_id/date_from/date_to/run_count, or None if it was already
    RUNNING or doesn't exist — the single UPDATE ... WHERE status <> 'RUNNING' is what
    stops two resume clicks from starting the batch twice.
    """
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE cr_scrape_batches
                SET status = 'RUNNING', cancel_requested = FALSE, finished_at = NULL,
                    error_message = NULL, run_count = run_count + 1
                WHERE batch_id = %s AND status <> 'RUNNING'
                RETURNING court_id, date_from, date_to, run_count;
            """, (batch_id,))
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
    court_id, date_from, date_to, run_count = row
    return {"court_id": court_id, "date_from": date_from, "date_to": date_to, "run_count": run_count}


def _derived_events(batch: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A minimal timeline for batches created before cr_batch_events existed, from the columns they do have."""
    events = [{"event_id": None, "run_number": 1, "event_type": "STARTED", "occurred_at": batch["requested_at"], "message": None,
               "details": {"date_from": batch["date_from"].isoformat(), "date_to": batch["date_to"].isoformat()}}]
    if batch["finished_at"] is not None:
        events.append({"event_id": None, "run_number": batch.get("run_count") or 1, "event_type": batch["status"],
                       "occurred_at": batch["finished_at"], "message": batch["error_message"], "details": {}})
    return events
