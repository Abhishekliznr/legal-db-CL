"""
Local, non-LLM act/section extraction from a judgment's raw text, via the
vendored legal_NER library (pipeline/legal_ner_lib) and en_legal_ner_sm.

Returns the same `[{"act_name": ..., "sections": [...]}, ...]` shape
db.lookups.resolve_act_entries() takes.
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
# a worker process may run promotion from more than one thread.
_infer_lock = threading.Lock()
_legal_nlp = None
_preamble_nlp = None

# PROVISION entity text comes in many shapes ("Section 302", "302/34",
# "section-302,307", "181(2)(r) and (s)", "sub-section (2) of Section 56",
# "85 of BNS") -- reduce each to bare section numbers matching
# db.lookups.get_or_create_section()'s section_number, and drop anything that
# isn't one (years, case numbers, "IX") rather than storing it as a section.
_SUBSECTION_OF_RE = re.compile(
    r"sub-?(?:section|clause)s?\s*\((\w+)\)\s*of\s*(?:section|sec\.?|s\.)\s*(\d+[A-Z]?)", re.IGNORECASE,
)
# CPC "Order XXXIX Rule 1" -- orders/rules aren't tracked, and its "1" isn't a section.
_ORDER_RULE_RE = re.compile(r"\border\s+[IVXLC\d]+\s*,?\s*(?:rules?|r\.)\s*[\w()]+", re.IGNORECASE)
_KEYWORD_RE = re.compile(
    r"\b(?:sub-?(?:section|clause)s?\s+of\s+)?"
    r"(?:(?:sections?|articles?|orders?|rules?|clauses?|regulations?|sec|art)\b\.?|\bs\.)[\s\-]*",
    re.IGNORECASE,
)
_SEPARATOR_RE = re.compile(r"\s*(?:\br/w\b|/|,|;|&|\band\b|\bor\b|\bto\b|\balternatively\b|\bread with\b)\s*", re.IGNORECASE)
# " of BNS", " Part I/II" -- also drops "cases of 4 CRA-..." before its "4" can pass as a section.
_TAIL_RE = re.compile(r"\s+(?:of|part)\b.*$", re.IGNORECASE)
# "319CrPC" (3+ letters glued on; "12AA" stays), "4(2)(f)]".
_TOKEN_NOISE_RE = re.compile(r"(?:(?<=\d)[A-Za-z]{3,}|\]+)$")
_SECTION_NUMBER_RE = re.compile(r"^\d{1,3}(?:-?[A-Z]{1,2})?(?:\([0-9A-Za-z]{1,4}\))*$")


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


def _section_numbers(provision_text: str) -> List[str]:
    text = _ORDER_RULE_RE.sub(" ", _SUBSECTION_OF_RE.sub(r"\2(\1)", provision_text))
    text = re.sub(r"\s+\(", "(", _KEYWORD_RE.sub(" ", text))
    numbers: List[str] = []
    for part in _SEPARATOR_RE.split(text):
        for token in _TAIL_RE.sub("", part.strip()).split():
            token = _TOKEN_NOISE_RE.sub("", token)
            if token.count("(") > token.count(")"):
                token += ")"
            # "181(2)(r) and (s)" -> "(s)" continues the previous number's last level.
            if token.startswith("(") and numbers:
                token = numbers[-1][: numbers[-1].rfind("(")] + token
            if _SECTION_NUMBER_RE.match(token):
                numbers.append(token)
    return numbers


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
        section_numbers = _section_numbers(pair.normalised_provision_text or "")
        if not act_name or not section_numbers:
            continue
        sections = acts.setdefault(act_name, [])
        seen = seen_sections.setdefault(act_name, set())
        for section_number in section_numbers:
            if section_number not in seen:
                seen.add(section_number)
                sections.append(section_number)

    return [{"act_name": name, "sections": secs} for name, secs in acts.items()]
