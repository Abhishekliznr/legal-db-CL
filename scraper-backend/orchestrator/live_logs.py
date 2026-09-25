"""
In-memory, per-batch log buffer — deliberately NOT a database table.

log_context.slog() mirrors each pipeline log line here so GET
/api/scraper/batches/{id}/logs/stream (routers/scraper_router.py) can tail a
running batch live. Logs live only as long as the process does; stdout keeps
the full history.

Thread-safety note: batches run inside FastAPI BackgroundTasks (worker
threads), while the SSE endpoint reads from the event loop — a plain
threading.Lock around buffer access is enough.
"""

import threading
import time
from collections import OrderedDict, deque
from itertools import islice
from typing import Deque, List, NamedTuple, Optional, Tuple

# Only the latest lines per batch are kept; a full-year MP run writes ~10 lines
# per case, far more than this, and older lines stay in stdout.
MAX_LINES_PER_BATCH = 1000
_MAX_TRACKED_BATCHES = 10


class LogLine(NamedTuple):
    ts: float
    level: str
    stage: Optional[str]
    case: Optional[str]
    index: Optional[int]
    total: Optional[int]
    message: str


class _Buffer:
    __slots__ = ("lines", "written", "finished")

    def __init__(self) -> None:
        self.lines: Deque[LogLine] = deque(maxlen=MAX_LINES_PER_BATCH)
        self.written = 0  # total lines ever appended; cursors are absolute against this
        self.finished = False

    @property
    def first_index(self) -> int:
        return self.written - len(self.lines)


_lock = threading.Lock()
# Insertion-ordered so eviction can drop the oldest finished batch first.
_buffers: "OrderedDict[int, _Buffer]" = OrderedDict()


def start_batch(batch_id: int) -> None:
    # A resumed batch keeps its earlier run's lines (one log page per batch) when
    # they're still in memory; after a server restart it starts a fresh buffer.
    with _lock:
        buf = _buffers.get(batch_id)
        if buf is None:
            _buffers[batch_id] = _Buffer()
        else:
            buf.finished = False
        _buffers.move_to_end(batch_id)
        _evict_if_over_capacity()


def finish_batch(batch_id: int) -> None:
    with _lock:
        buf = _buffers.get(batch_id)
        if buf is not None:
            buf.finished = True


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
        buf = _buffers.get(batch_id)
        if buf is None:
            return
        buf.lines.append(LogLine(time.time(), level, stage, case, index, total, message))
        buf.written += 1


def has_buffer(batch_id: int) -> bool:
    with _lock:
        return batch_id in _buffers


def get_lines_since(batch_id: int, cursor: int) -> Tuple[List[LogLine], int, int, bool]:
    """Returns (new_lines, next_cursor, trimmed_count, batch_is_finished); trimmed_count is how many lines past `cursor` were already dropped from the buffer."""
    with _lock:
        buf = _buffers.get(batch_id)
        if buf is None:
            return [], cursor, 0, True
        start = max(cursor, buf.first_index)
        offset = start - buf.first_index
        new_lines = list(islice(buf.lines, offset, None))
        return new_lines, buf.written, start - cursor, buf.finished


def _evict_if_over_capacity() -> None:
    """Caller holds _lock. Drops the oldest FINISHED batch first; a RUNNING batch is never evicted."""
    while len(_buffers) > _MAX_TRACKED_BATCHES:
        oldest_finished = next((bid for bid, buf in _buffers.items() if buf.finished), None)
        if oldest_finished is None:
            break
        del _buffers[oldest_finished]
