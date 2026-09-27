"""
SIGTERM (spot eviction, kubectl delete, activeDeadlineSeconds) sets a flag instead of killing
the worker mid-record. batch_runner checks it between records, like a cancel, and ends the run
FAILED with the reason, so the batch stays resumable. The Job's terminationGracePeriodSeconds
is what bounds how long the current record gets to finish.
"""

import threading
from typing import Optional

_requested = threading.Event()
_reason: Optional[str] = None


class WorkerTerminated(Exception):
    pass


def request(reason: str) -> None:
    global _reason
    _reason = reason
    _requested.set()


def check() -> None:
    if _requested.is_set():
        raise WorkerTerminated(_reason or "Worker was asked to stop")
