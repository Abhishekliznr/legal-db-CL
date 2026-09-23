import logging
import re
import threading
from typing import Dict, List

import spacy
from flask import Flask, jsonify, request

from legal_ner_lib.legal_ner import extract_entities_from_judgment_text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("legal_ner_service")

app = Flask(__name__)

# en_legal_ner_sm (not the _trf transformer variant) -- no torch/spacy-transformers
# dependency, smaller image and faster CPU inference, since this runs for every
# court adapter that has no structured act/section source of its own, not just
# one rare fallback case.
#
# Flask, not FastAPI, on purpose: spacy 3.2.x (the newest release this model's
# pinned pipeline works with) requires pydantic<1.9, which is incompatible with
# the pydantic v2 every other service here (built on modern FastAPI) depends on
# -- Flask has no pydantic dependency at all, sidestepping that conflict rather
# than pinning FastAPI back to a ~2021-era release.
_LEGAL_NER_MODEL = "en_legal_ner_sm"
_PREAMBLE_SPLITTING_MODEL = "en_core_web_sm"

_load_lock = threading.Lock()
_legal_nlp = None
_preamble_nlp = None


def _get_models():
    global _legal_nlp, _preamble_nlp
    if _legal_nlp is not None and _preamble_nlp is not None:
        return _legal_nlp, _preamble_nlp
    with _load_lock:
        if _legal_nlp is None:
            _legal_nlp = spacy.load(_LEGAL_NER_MODEL)
        if _preamble_nlp is None:
            _preamble_nlp = spacy.load(_PREAMBLE_SPLITTING_MODEL)
    return _legal_nlp, _preamble_nlp


# PROVISION entity text is e.g. "Section 302", "Article 21", "Order XXI" --
# strip the leading keyword to match the bare section_number convention
# callers (e.g. legal-db/scraper-backend's db.lookups.get_or_create_section())
# already use.
_LEADING_KEYWORD_RE = re.compile(r"^(?:sections?|articles?|orders?|rules?|clauses?|sec\.?|art\.?)\s+", re.IGNORECASE)


def _bare_section_number(provision_text: str) -> str:
    return _LEADING_KEYWORD_RE.sub("", provision_text.strip()).strip()


@app.post("/extract-acts-sections")
def extract_acts_sections():
    text = (request.get_json(silent=True) or {}).get("text") or ""
    if not text.strip():
        return jsonify({"acts": []})

    try:
        legal_nlp, preamble_nlp = _get_models()
        doc = extract_entities_from_judgment_text(
            text, legal_nlp, preamble_nlp, text_type="sent", do_postprocess=True,
        )
        pairs = doc.user_data.get("provision_statute_pairs") or []
    except Exception:
        logger.exception("[LEGAL_NER] extraction failed on %d chars of text", len(text))
        return jsonify({"acts": []})

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

    return jsonify({"acts": [{"act_name": name, "sections": secs} for name, secs in acts.items()]})


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


with app.app_context():
    _get_models()
