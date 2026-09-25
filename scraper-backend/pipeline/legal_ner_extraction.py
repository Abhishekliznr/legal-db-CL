"""
Local, non-LLM act/section extraction from a judgment's raw text, via the
vendored legal_NER library (pipeline/legal_ner_lib) and en_legal_ner_sm.

Returns the same `[{"act_name": ..., "sections": [...]}, ...]` shape MP's
_resolve_acts() takes (see adapters/high_courts/mp/promotion.py).
"""

import logging
import re
import threading
import warnings
from typing import Any, Dict, List

import spacy

from orchestrator import stages
from orchestrator.log_context import slog
from pipeline.legal_ner_lib.legal_ner import extract_entities_from_judgment_text

logger = logging.getLogger("scraper_backend_v2.legal_ner_extraction")

# en_legal_ner_sm (not the _trf variant): no torch dependency, fast on CPU.
_LEGAL_NER_MODEL = "en_legal_ner_sm"
_PREAMBLE_SPLITTING_MODEL = "en_core_web_sm"

_load_lock = threading.Lock()
# spaCy doesn't guarantee Language objects are safe to call concurrently, and
# promotions run on several BackgroundTasks threads at once.
_infer_lock = threading.Lock()
_legal_nlp = None
_preamble_nlp = None

# PROVISION entity text is e.g. "Section 302", "Article 21" -- strip the
# keyword to match db.lookups.get_or_create_section()'s bare section_number.
_LEADING_KEYWORD_RE = re.compile(r"^(?:sections?|articles?|orders?|rules?|clauses?|sec\.?|art\.?)\s+", re.IGNORECASE)


def load_models():
    global _legal_nlp, _preamble_nlp
    if _legal_nlp is not None and _preamble_nlp is not None:
        return _legal_nlp, _preamble_nlp
    with _load_lock:
        # Both models were packaged for spaCy 3.2 but load and score identically
        # on 3.8 -- W095 is just the version-mismatch notice.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=r"\[W095\]")
            if _legal_nlp is None:
                _legal_nlp = spacy.load(_LEGAL_NER_MODEL)
            if _preamble_nlp is None:
                _preamble_nlp = spacy.load(_PREAMBLE_SPLITTING_MODEL)
    return _legal_nlp, _preamble_nlp


def _bare_section_number(provision_text: str) -> str:
    return _LEADING_KEYWORD_RE.sub("", provision_text.strip()).strip()


def extract_acts_sections(judgment_text: str) -> List[Dict[str, Any]]:
    # Returns [] on failure -- callers treat it as "no acts found".
    if not judgment_text or not judgment_text.strip():
        return []

    try:
        legal_nlp, preamble_nlp = load_models()
        with _infer_lock:
            doc = extract_entities_from_judgment_text(
                judgment_text, legal_nlp, preamble_nlp, text_type="sent", do_postprocess=True,
            )
        pairs = doc.user_data.get("provision_statute_pairs") or []
    except Exception:
        slog(logger, stages.NER, "exception", "extraction failed on %d chars of text", len(judgment_text))
        return []

    acts: Dict[str, List[str]] = {}
    seen_sections: Dict[str, set] = {}
    for pair in pairs:
        act_name = (pair.normalised_statute_text or "").strip()
        section_number = _bare_section_number(pair.normalised_provision_text or "")
        if not act_name or not section_number:
            continue
        sections = acts.setdefault(act_name, [])
        seen = seen_sections.setdefault(act_name, set())
        if section_number not in seen:
            seen.add(section_number)
            sections.append(section_number)

    return [{"act_name": name, "sections": secs} for name, secs in acts.items()]
