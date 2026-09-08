"""
CRUD/queries over `scrape_batches` + `raw_ingestions` — the tables that
replace the old in-memory ACTIVE_JOBS/ACTIVE_THREADS/CANCEL_FLAGS dicts as
the source of truth for job state (docs/scraper-backend-revamp-spec.md §4.4).

This module is pure SQL against tables that already exist after Phase 0's
schema init — it has no dependency on the orchestrator/adapters packages
that consume it in Phase 1, which is why the query layer ships now.
"""

from datetime import date
from typing import Any, Dict, List, Optional

from psycopg2.extras import Json

from db.connection import get_pooled_connection


def create_batch(court_id: int, date_from: date, date_to: date) -> int:
    """Creates a scrape_batches row and returns its batch_id."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scrape_batches (court_id, date_from, date_to)
                VALUES (%s, %s, %s)
                RETURNING batch_id;
            """, (court_id, date_from, date_to))
            batch_id = cur.fetchone()[0]
        conn.commit()
    return batch_id


def finish_batch(batch_id: int, status: str, total_found: int, total_downloaded: int) -> None:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE scrape_batches
                SET status = %s, total_found = %s, total_downloaded = %s
                WHERE batch_id = %s;
            """, (status, total_found, total_downloaded, batch_id))
        conn.commit()


def list_batches(limit: int = 50) -> List[Dict[str, Any]]:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sb.batch_id, sb.court_id, c.court_name, sb.date_from, sb.date_to,
                       sb.requested_at, sb.status, sb.total_found, sb.total_downloaded, sb.total_promoted
                FROM scrape_batches sb
                LEFT JOIN courts c ON c.court_id = sb.court_id
                ORDER BY sb.requested_at DESC
                LIMIT %s;
            """, (limit,))
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def insert_raw_ingestion(
    batch_id: int,
    court_id: int,
    source_pdf_url: str,
    file_checksum: str,
    data_source: str,
    blob_pdf_id: Optional[str] = None,
    page_count: Optional[int] = None,
) -> Optional[int]:
    """
    Inserts a raw_ingestions row for a freshly-downloaded PDF. Returns None
    (not an error) if file_checksum already exists — that's the dedup path,
    not a failure: this court's judgment was already scraped in a prior run.

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
                INSERT INTO raw_ingestions (batch_id, court_id, source_pdf_url, file_checksum, data_source, blob_pdf_id, page_count, status, downloaded_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'DOWNLOADED', now())
                ON CONFLICT (file_checksum) DO NOTHING
                RETURNING ingestion_id;
            """, (batch_id, court_id, source_pdf_url, file_checksum, data_source, blob_pdf_id, page_count))
            row = cur.fetchone()
        conn.commit()
    return row[0] if row else None


def get_ingestion(ingestion_id: int) -> Optional[Dict[str, Any]]:
    """Fetches one raw_ingestions row — used by each pipeline stage to read what the previous stage wrote."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ingestion_id, batch_id, court_id, source_pdf_url, blob_pdf_id,
                       file_checksum, ocr_text, status, case_id, data_source
                FROM raw_ingestions
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
                FROM raw_ingestions
                WHERE status = %s
                ORDER BY created_at
                LIMIT %s;
            """, (status, limit))
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def update_status(ingestion_id: int, status: str, **fields: Any) -> None:
    """
    Advances a raw_ingestions row's status, optionally setting other columns
    in the same statement (e.g. ocr_text=..., status='OCR_DONE').
    """
    set_clauses = ["status = %s"]
    params: List[Any] = [status]
    for column, value in fields.items():
        set_clauses.append(f"{column} = %s")
        params.append(Json(value) if isinstance(value, (dict, list)) else value)
    params.append(ingestion_id)

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE raw_ingestions SET {', '.join(set_clauses)} WHERE ingestion_id = %s;",
                params,
            )
        conn.commit()
