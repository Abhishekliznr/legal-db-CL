"""
Per-batch live log lines, written to cr_batch_logs for api-backend's SSE endpoint to tail
(the worker pod has no HTTP surface of its own). log_context.slog() mirrors each pipeline
log line here; stdout keeps the full server-log version.

Lines are queued in memory and inserted by a background flusher every second (or sooner
once _FLUSH_AT_LINES pile up), so a busy stage doesn't pay a DB round trip per line.
finish_batch() flushes synchronously: batch_runner calls it before marking the batch
finished, which is what lets the SSE reader treat "finished + no new rows" as the end.
"""

import logging
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from psycopg2.extras import execute_values

from db.connection import get_pooled_connection

logger = logging.getLogger("scraper_backend_v2.live_logs")

_FLUSH_INTERVAL_SECONDS = 1.0
_FLUSH_AT_LINES = 200

_lock = threading.Lock()
_flush_lock = threading.Lock()
_pending: List[Tuple] = []
_run_numbers: Dict[int, int] = {}
_wake = threading.Event()
_flusher: Optional[threading.Thread] = None


def start_batch(batch_id: int, run_number: int) -> None:
    global _flusher
    with _lock:
        _run_numbers[batch_id] = run_number
        if _flusher is None:
            _flusher = threading.Thread(target=_flush_loop, name="live-logs-flusher", daemon=True)
            _flusher.start()


def finish_batch(batch_id: int) -> None:
    flush()
    with _lock:
        _run_numbers.pop(batch_id, None)


def log(
    batch_id: int,
    level: str,
    stage: Optional[str],
    case: Optional[str],
    index: Optional[int],
    total: Optional[int],
    message: str,
) -> None:
    with _lock:
        run_number = _run_numbers.get(batch_id)
        if run_number is None:
            return
        _pending.append((batch_id, run_number, datetime.now(timezone.utc), level, stage, case, index, total, message))
        if len(_pending) >= _FLUSH_AT_LINES:
            _wake.set()


def flush() -> None:
    with _flush_lock:
        with _lock:
            rows = _pending[:]
            _pending.clear()
        if not rows:
            return
        try:
            with get_pooled_connection() as conn:
                with conn.cursor() as cur:
                    execute_values(cur, """
                        INSERT INTO cr_batch_logs
                            (batch_id, run_number, logged_at, level, stage, case_ref, item_index, item_total, message)
                        VALUES %s;
                    """, rows)
                conn.commit()
        except Exception:
            # Never fails the batch: the same lines are already in stdout (kubectl logs).
            logger.exception("Could not write %d live log line(s) to cr_batch_logs — dropped from the admin log view", len(rows))


def _flush_loop() -> None:
    while True:
        _wake.wait(_FLUSH_INTERVAL_SECONDS)
        _wake.clear()
        flush()
