"""
Thread-local (contextvars) pipeline logging context.

batch_runner.run_batch() enters scope(batch_id, court_code) once per batch,
on the BackgroundTasks worker thread that runs it (one thread per batch, so
a plain ContextVar is enough). Per-case work is wrapped in case_scope(), so
every slog() call several frames down (adapters, pipeline/*) is tagged with
its court, case and position and mirrored into the batch's live SSE buffer
(live_logs.py) without threading ids through every signature.

tally() counts outcomes per batch (e.g. skip reasons, where acts came from) for
batch_runner's end-of-batch summary; labels come from the calling court/stage
code, so court-specific wording stays in that court's adapter folder.

Outside any scope() slog() just logs untagged, logger-only, and tally() is a no-op.
"""

import contextvars
import logging
from collections import Counter
from contextlib import contextmanager
from typing import Dict, Optional, Tuple

from orchestrator import live_logs

CaseContext = Tuple[str, Optional[int], Optional[int]]  # (label, index, total)

_batch_id: "contextvars.ContextVar[Optional[int]]" = contextvars.ContextVar("batch_id", default=None)
_court_code: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar("court_code", default=None)
_case: "contextvars.ContextVar[Optional[CaseContext]]" = contextvars.ContextVar("case", default=None)
_tallies: "contextvars.ContextVar[Optional[Dict[str, Counter]]]" = contextvars.ContextVar("tallies", default=None)


@contextmanager
def scope(batch_id: int, court_code: str):
    b_token = _batch_id.set(batch_id)
    c_token = _court_code.set(court_code)
    t_token = _tallies.set({})
    try:
        yield
    finally:
        _tallies.reset(t_token)
        _court_code.reset(c_token)
        _batch_id.reset(b_token)


@contextmanager
def case_scope(label: str, index: Optional[int] = None, total: Optional[int] = None):
    token = _case.set((label, index, total))
    try:
        yield
    finally:
        _case.reset(token)


def _case_prefix(case: Optional[CaseContext]) -> str:
    if case is None:
        return ""
    label, index, total = case
    return f"[{index}/{total} {label}] " if index is not None and total is not None else f"[{label}] "


def slog(logger: logging.Logger, stage: str, level: str, msg: str, *args) -> None:
    # "debug" is server-log only: raw HTML/page dumps never reach the live panel.
    formatted = msg % args if args else msg
    court_code = _court_code.get()
    case = _case.get()
    court_prefix = f"[{court_code}] " if court_code else ""
    getattr(logger, level)(f"{court_prefix}{_case_prefix(case)}{stage}: {formatted}")

    batch_id = _batch_id.get()
    if batch_id is None or level == "debug":
        return
    label, index, total = case if case is not None else (None, None, None)
    live_logs.log(batch_id, "error" if level == "exception" else level, stage, label, index, total, formatted)


def tally(group: str, label: str, count: int = 1) -> None:
    groups = _tallies.get()
    if groups is not None and count:
        groups.setdefault(group, Counter())[label] += count


def tallies() -> Dict[str, Counter]:
    return _tallies.get() or {}
