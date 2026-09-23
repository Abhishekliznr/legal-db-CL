"""
Thread-local (contextvars) pipeline logging context.

batch_runner.run_batch() enters scope(batch_id, court_code) once for the
whole batch, on the worker thread Starlette's BackgroundTasks runs it on
(see live_logs.py's own threading note — one thread per batch, so a plain
ContextVar is enough, no async propagation to worry about). Every module
several calls deep from there — promote_fn (adapters/*/promotion.py),
pipeline/legal_ner_extraction.py, pipeline/llm_enrichment.py,
pipeline/ocr.py — then calls plog() instead of its own module logger
directly, and automatically gets both a '[<COURT_CODE>]' tag and a mirror
into the batch's live SSE log buffer (live_logs.py), with no
batch_id/court_code threaded through every function signature down the
call chain.

Outside any scope() (e.g. a promotion module driven from a one-off script
or test) plog() just logs untagged, logger-only — it never raises over
missing context.
"""

import contextvars
import logging
from contextlib import contextmanager
from typing import Optional

from orchestrator import live_logs

_batch_id: "contextvars.ContextVar[Optional[int]]" = contextvars.ContextVar("batch_id", default=None)
_court_code: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar("court_code", default=None)


@contextmanager
def scope(batch_id: int, court_code: str):
    b_token = _batch_id.set(batch_id)
    c_token = _court_code.set(court_code)
    try:
        yield
    finally:
        _court_code.reset(c_token)
        _batch_id.reset(b_token)


def plog(logger: logging.Logger, level: str, msg: str, *args) -> None:
    """logger.<level>(msg, *args), tagged with the current scope()'s court_code and mirrored into that batch's live SSE log buffer, when inside one."""
    formatted = msg % args if args else msg
    court_code = _court_code.get()
    tagged = f"[{court_code}] {formatted}" if court_code else formatted
    getattr(logger, level)(tagged)
    batch_id = _batch_id.get()
    if batch_id is not None:
        live_logs.log(batch_id, "error" if level == "exception" else level, tagged)
