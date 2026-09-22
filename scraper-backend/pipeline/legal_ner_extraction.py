"""
Client for the legal-ner-service sibling (../legal-ner-service) -- a local,
non-LLM alternative for pulling acts/sections out of a judgment's raw text,
used wherever a court's own promotion module has no structured act/section
field to fall back on (see adapters/high_courts/mp/promotion.py's
_resolve_acts() for the first consumer; any other court adapter's own
promotion module can call extract_acts_sections() the same way).

Runs as its own Flask service (Python 3.10, pinned by en_legal_ner_sm's own
spacy<3.3.0 requirement -- incompatible with this service's Python 3.11 and
with the pydantic v2 this FastAPI service depends on) rather than an
in-process import -- see legal-ner-service/main.py's own module docstring.

Returns the exact same `[{"act_name": ..., "sections": [...]}, ...]` shape
MP's own _resolve_acts() already takes, so callers resolve/write acts and
sections through the same normalization.acts.resolve_act() +
db.lookups.get_or_create_act()/get_or_create_section() path regardless of
which source (case-status HTML, this NER model, or the LLM) found them.
"""

import logging
import os
from typing import Any, Dict, List

import requests

from orchestrator.log_context import plog

logger = logging.getLogger("scraper_backend_v2.legal_ner_extraction")

_SERVICE_URL = os.environ.get("LEGAL_NER_SERVICE_URL", "http://localhost:8004")
# Matches legal-ner-service's own gunicorn --timeout -- "sent" mode runs the
# model per-sentence for accuracy, which can take a while on a long judgment.
_TIMEOUT_SECONDS = 120


def extract_acts_sections(judgment_text: str) -> List[Dict[str, Any]]:
    """
    Calls legal-ner-service over HTTP and returns acts grouped with their
    section numbers, e.g. [{"act_name": "Indian Penal Code", "sections": ["302", "34"]}].
    Returns [] on any failure (bad/empty text, service down, timeout) rather
    than raising -- callers treat this the same as "no acts found", never
    crash a promotion over it.
    """
    if not judgment_text or not judgment_text.strip():
        return []

    try:
        resp = requests.post(
            f"{_SERVICE_URL}/extract-acts-sections",
            json={"text": judgment_text},
            timeout=_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json().get("acts") or []
    except Exception:
        plog(
            logger, "exception",
            "[LEGAL_NER] request to %s failed for %d chars of text", _SERVICE_URL, len(judgment_text),
        )
        return []
