"""
OCR_DONE -> EXTRACTED (spec §5.2).

The real structured-output LLM call, replacing Phase 1's build_stub_extraction()
stand-in. Same JSON envelope shape as before — promotion.py needed zero
changes to its reading of this envelope when this swap happened, which was
the entire point of designing Phase 1 the way it was.

Design choices worth being explicit about:

- If GROQ_API_KEY isn't configured, this degrades to the Phase 1 stub
  (build_stub_extraction) rather than failing every ingestion outright —
  logged once as a warning, not silently. A production deployment is
  expected to have the key set; this is a "still usable in local dev
  without one" allowance, not a recommended steady state.
- If the key IS configured but the call fails or returns something that
  doesn't validate, the row goes to EXTRACTION_FAILED — it does NOT
  silently fall back to the stub, since that would hide a real production
  problem (bad key, model deprecated, rate limited) behind data that looks
  fine but is actually low-quality.
- NOT LIVE-VERIFIED: the actual API request/response handling below was
  checked against a mocked HTTP call (matches Groq's OpenAI-compatible
  chat completions contract), not a real call — this sandbox has network
  access but doing a real call would spend the user's API credits without
  being asked to. The prompt/schema design itself is therefore also
  unverified against a real judgment's extraction quality.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import requests

from adapters.base import RawJudgmentRecord
from db import scrape_jobs
from pipeline import citator
from pipeline.extraction_stub import build_stub_extraction  # Phase 1 fallback, kept for no-API-key dev use

logger = logging.getLogger("scraper_backend_v2.extraction")

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
# llama-3.3-70b-versatile (the original default here) turned out to be
# retired on Groq's side — confirmed 404 in production use. openai/gpt-oss-120b
# is confirmed working as of 2026-09-05 against a real account, but Groq's
# catalog changes over time; always override via GROQ_MODEL rather than
# relying on this fallback long-term. Check `curl https://api.groq.com/openai/v1/models
# -H "Authorization: Bearer $GROQ_API_KEY"` for your account's current options.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

_SYSTEM_PROMPT = """You are an expert legal metadata extractor for Indian court judgments.

You will receive OCR text from a Supreme Court or High Court judgment, plus
a list of citation-shaped strings already found in the text by a regex
pre-filter. Extract structured metadata as ONE compact JSON object, with
EXACTLY these top-level keys:

{
  "case_note_ai": "one paragraph, 80-150 words, MANUPATRA-style headnote summarizing the legal issue/finding/disposition. Use ONLY facts present in the supplied text — do not invent anything.",
  "cases": [{"case_number": "...", "cnr_number": "... or null", "parties": [{"name": "...", "side": "PETITIONER_SIDE" or "RESPONDENT_SIDE"}]}],
  "coram": [{"name": "judge full name, no honorifics needed", "is_author": true or false}],
  "provisions": [{"statute_name": "...", "section_number": "..."}],
  "citations": [{"cited_case_name": "...", "cited_reporter_citation": "... or null", "treatment": one of "Overruled","Affirmed","Distinguished","Followed","Referred","Relied Upon","Explained","Doubted"}],
  "disposition_category": one of "Allowed","Dismissed","Partly Allowed","Disposed","Remanded","Withdrawn","Quashed","Set Aside","Other", or null if unclear,
  "favoring_party_side": "PETITIONER_SIDE", "RESPONDENT_SIDE", or null,
  "date_of_judgment": "YYYY-MM-DD or null if not stated in the text"
}

Only use the citation candidates list to decide which ones are real cited
cases worth including in "citations" — most candidates are false positives
(page numbers, section references) and should be excluded. Return ONLY the
JSON object, no markdown fences, no commentary."""


def _parse_llm_json(raw_content: str) -> Optional[dict]:
    if not raw_content:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_content.strip(), flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start:end + 1]
    try:
        result = json.loads(cleaned)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        return None


def call_llm_extraction(ocr_text: str, citation_candidates: List[Dict[str, str]]) -> Optional[dict]:
    """
    One structured-output call to Groq's chat completions API. Returns the
    parsed JSON dict, or None if the call failed / didn't return valid JSON
    — caller treats None as "route to EXTRACTION_FAILED", not as "fall back
    to the stub" (see module docstring on why those are different).
    """
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        return None

    # Long judgments: cap at ~12k chars to stay well under context limits
    # while keeping the whole disposition (usually near the end).
    truncated_text = ocr_text if len(ocr_text) <= 12000 else ocr_text[:8000] + "\n...\n" + ocr_text[-4000:]

    user_content = f"JUDGMENT TEXT:\n{truncated_text}"
    if citation_candidates:
        candidate_lines = "\n".join(f"- {c['raw_citation']} (context: {c['context'][:100]})" for c in citation_candidates[:20])
        user_content += f"\n\nCITATION CANDIDATES FOUND BY REGEX PRE-FILTER:\n{candidate_lines}"

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        response = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=45)
        if not response.ok:
            # raise_for_status()'s exception message doesn't include the
            # response body, which is exactly where Groq puts the actual
            # reason (e.g. "model `x` does not exist") — log it explicitly
            # instead of leaving a bare "404 Client Error" to guess from.
            logger.warning("Groq extraction call failed: HTTP %s — %s", response.status_code, response.text[:500])
            return None
        raw_content = response.json()["choices"][0]["message"]["content"]
        return _parse_llm_json(raw_content)
    except Exception as e:
        logger.warning("Groq extraction call failed: %s", e)
        return None


def _merge_scraper_fields(llm_result: dict, record: RawJudgmentRecord) -> Dict[str, Any]:
    """
    Scraper-provided fields (from the court's own results table) win over
    the LLM's re-derivation of the same fact where both exist — the results
    table is a more reliable source for case_number/date/citation than an
    LLM reading OCR'd text, especially for scanned judgments.
    """
    cases = llm_result.get("cases") or []
    if not cases and record.case_number_raw:
        parties = []
        if record.party_name_raw:
            parts = [p.strip() for p in record.party_name_raw.replace(" Vs ", " VS ").split(" VS ") if p.strip()]
            if len(parts) >= 1:
                parties.append({"name": parts[0], "side": "PETITIONER_SIDE"})
            if len(parts) >= 2:
                parties.append({"name": parts[1], "side": "RESPONDENT_SIDE"})
        cases = [{"case_number": record.case_number_raw, "cnr_number": record.cnr_raw, "parties": parties}]

    return {
        "extractor": f"llm_v1:{GROQ_MODEL}",
        "case_note_ai": llm_result.get("case_note_ai"),
        "cases": cases,
        "coram": llm_result.get("coram") or [],
        "provisions": llm_result.get("provisions") or [],
        "citations": llm_result.get("citations") or [],
        "disposition_category": llm_result.get("disposition_category"),
        "favoring_party_side": llm_result.get("favoring_party_side"),
        "neutral_citation": record.neutral_citation_raw,
        "judgment_date_raw": record.decision_date_raw or llm_result.get("date_of_judgment"),
    }


def process_ingestion_extraction(ingestion_id: int, record: RawJudgmentRecord) -> None:
    """Advances a raw_ingestions row from OCR_DONE to EXTRACTED, or to EXTRACTION_FAILED if the LLM call fails (module docstring explains why that's not a silent stub fallback)."""
    ingestion = scrape_jobs.get_ingestion(ingestion_id)
    ocr_text = (ingestion or {}).get("ocr_text") or ""

    if not os.environ.get("GROQ_API_KEY", "").strip():
        logger.warning("GROQ_API_KEY not configured — using Phase 1 stub extraction for ingestion_id=%s. Not recommended in production.", ingestion_id)
        extraction = build_stub_extraction(record)
        scrape_jobs.update_status(ingestion_id, status="EXTRACTED", raw_ai_extraction=extraction)
        return

    try:
        citation_candidates = citator.find_citation_candidates(ocr_text)
        llm_result = call_llm_extraction(ocr_text, citation_candidates)
        if llm_result is None:
            raise RuntimeError("LLM extraction call failed or returned invalid JSON")

        extraction = _merge_scraper_fields(llm_result, record)
        scrape_jobs.update_status(ingestion_id, status="EXTRACTED", raw_ai_extraction=extraction)
    except Exception as e:
        scrape_jobs.update_status(ingestion_id, status="EXTRACTION_FAILED", extraction_error=str(e))
