"""
In-memory, per-batch log buffer — deliberately NOT a database table.

batch_runner.py already narrates its own progress via `logger.info()`/
`logger.exception()`; this module is a second, explicit sink for those same
call sites (see the `_log()` helper in batch_runner.py) so GET
/api/scraper/batches/{id}/logs/stream (routers/scraper_router.py) can tail a
running batch's log lines as they happen. Logs live only as long as the
process does — nothing here is written to Postgres, and a restart loses
every buffer, same as it always lost stdout. That's a deliberate choice, not
an oversight: an admin watching a batch wants to see it live, not query
history months later (see project memory on this feature).

Thread-safety note: batch_runner.py runs inside a FastAPI BackgroundTasks
callback, which Starlette executes on a worker thread, not the event loop —
so `log()` is called from a different thread than the SSE endpoint reading
it. A plain `threading.Lock` around list access is enough here; there's no
async concurrency to coordinate, just this one shared list per batch.
"""

import threading
import time
from collections import OrderedDict
from typing import Dict, List, Tuple

# Bounds how many batches' buffers are kept around at once (oldest FINISHED
# batch is evicted first) and how many lines a single batch can accumulate,
# so a long-running or high-volume batch can't grow this unbounded for the
# lifetime of the process.
_MAX_TRACKED_BATCHES = 50
_MAX_LINES_PER_BATCH = 5000

LogLine = Tuple[float, str, str]  # (unix_timestamp, level, message)

_lock = threading.Lock()
# Insertion-ordered so eviction can drop the oldest finished batch first.
_buffers: "OrderedDict[int, List[LogLine]]" = OrderedDict()
_finished: Dict[int, bool] = {}


def start_batch(batch_id: int) -> None:
    with _lock:
        _buffers[batch_id] = []
        _finished[batch_id] = False
        _buffers.move_to_end(batch_id)
        _evict_if_over_capacity()


def finish_batch(batch_id: int) -> None:
    with _lock:
        if batch_id in _finished:
            _finished[batch_id] = True


def log(batch_id: int, level: str, message: str) -> None:
    with _lock:
        buf = _buffers.get(batch_id)
        if buf is None:
            return  # nothing subscribed to this batch (or it predates process start) -- fine, drop it
        buf.append((time.time(), level, message))
        if len(buf) > _MAX_LINES_PER_BATCH:
            del buf[: len(buf) - _MAX_LINES_PER_BATCH]


def has_buffer(batch_id: int) -> bool:
    """False for a batch this process never ran (e.g. started before a restart, or evicted) — the SSE endpoint uses this to tell 'no live logs available' apart from 'batch just hasn't logged anything yet'."""
    with _lock:
        return batch_id in _buffers


def get_lines_since(batch_id: int, since_index: int) -> Tuple[List[LogLine], int, bool]:
    """Returns (new_lines, next_index, batch_is_finished) — the SSE endpoint's poll loop stops once batch_is_finished is True and no new lines remain."""
    with _lock:
        buf = _buffers.get(batch_id, [])
        return list(buf[since_index:]), len(buf), _finished.get(batch_id, True)


def _evict_if_over_capacity() -> None:
    """Caller already holds _lock. Drops the oldest FINISHED batch's buffer first; a batch still RUNNING is never evicted out from under an active viewer."""
    while len(_buffers) > _MAX_TRACKED_BATCHES:
        evicted = False
        for candidate_id in list(_buffers.keys()):
            if _finished.get(candidate_id):
                del _buffers[candidate_id]
                del _finished[candidate_id]
                evicted = True
                break
        if not evicted:
            break  # every tracked batch is still RUNNING -- nothing safe to drop, let it grow
