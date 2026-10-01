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
    """Manual worker runs only (python -m worker --court ...): api-backend queues every UI-started batch itself."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cr_scrape_batches (court_id, date_from, date_to, status, queued_at)
                VALUES (%s, %s, %s, 'QUEUED', now())
                RETURNING batch_id;
            """, (court_id, date_from, date_to))
            batch_id = cur.fetchone()[0]
            _record_event(cur, batch_id, "STARTED", details={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()})
        conn.commit()
    return batch_id


def claim_queued_batch(batch_id: int, job_name: Optional[str]) -> Optional[Dict[str, Any]]:
    """QUEUED -> RUNNING for this worker. None if the batch isn't QUEUED (cancelled first, already claimed, or missing)."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE cr_scrape_batches sb
                SET status = 'RUNNING', claimed_at = now(), heartbeat_at = now(),
                    job_name = COALESCE(sb.job_name, %s)
                FROM cr_courts c
                WHERE sb.batch_id = %s AND sb.status = 'QUEUED' AND c.court_id = sb.court_id
                RETURNING sb.court_id, c.court_code, sb.date_from, sb.date_to, sb.run_count;
            """, (job_name, batch_id))
            row = cur.fetchone()
        conn.commit()
    if row is None:
        return None
    court_id, court_code, date_from, date_to, run_count = row
    return {"court_id": court_id, "court_code": court_code, "date_from": date_from, "date_to": date_to, "run_count": run_count}


def fail_queued_batch(batch_id: int, error_message: str) -> None:
    """The worker couldn't start (e.g. NER models missing): record why instead of leaving the run QUEUED until it goes stale."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE cr_scrape_batches SET status = 'FAILED', finished_at = now(), error_message = %s
                WHERE batch_id = %s AND status = 'QUEUED'
                RETURNING batch_id;
            """, (error_message, batch_id))
            if cur.fetchone() is not None:
                _record_event(cur, batch_id, "FAILED", error_message)
        conn.commit()


def get_batch_status(batch_id: int) -> Optional[str]:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM cr_scrape_batches WHERE batch_id = %s;", (batch_id,))
            row = cur.fetchone()
    return row[0] if row else None


def heartbeat(batch_id: int) -> None:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE cr_scrape_batches SET heartbeat_at = now() WHERE batch_id = %s AND status = 'RUNNING';", (batch_id,))
        conn.commit()

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
    """Case numbers already saved WITH a judgment for this court — a re-run skips these but retries cases still missing one."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT case_number FROM cr_cases WHERE court_id = %s AND judgment_status = 'AVAILABLE';", (court_id,))
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

# Statuses a run picks up; api-backend's resume can reset SKIPPED / NO_JUDGMENT items to PENDING first.
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


def mark_batch_item(
    item_id: int, status: str, reason: Optional[str] = None, ingestion_id: Optional[int] = None, case_id: Optional[int] = None,
) -> None:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE cr_batch_items
                SET status = %s, reason = %s, ingestion_id = COALESCE(%s, ingestion_id), case_id = COALESCE(%s, case_id),
                    attempts = attempts + 1, updated_at = now()
                WHERE item_id = %s;
            """, (status, reason, ingestion_id, case_id, item_id))
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



