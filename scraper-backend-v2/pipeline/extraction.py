"""
OCR_DONE -> EXTRACTED (spec §5.2).

The real structured-output LLM call, replacing Phase 1's build_stub_extraction()
stand-in. Same JSON envelope shape as before — promotion.py needed zero
changes to its reading of this envelope when this swap happened, which was
the entire point of designing Phase 1 the way it was.

Provider: Azure OpenAI (switched from Groq 2026-09-06 — Groq's own
integration had already been live-verified against a real account, but the
user asked for Azure OpenAI as the production provider instead). Azure
OpenAI's request shape differs from Groq/plain OpenAI in three ways this
module has to account for: the model is a *deployment name* baked into the
URL path rather than a "model" field in the JSON body, auth is an `api-key`
header rather than `Authorization: Bearer`, and every request needs an
`api-version` query parameter.

Design choices worth being explicit about:

- If Azure OpenAI isn't fully configured (endpoint, key, and deployment
  name all required), this degrades to the Phase 1 stub
  (build_stub_extraction) rather than failing every ingestion outright —
  logged once as a warning, not silently. A production deployment is
  expected to have these set; this is a "still usable in local dev
  without them" allowance, not a recommended steady state.
- If it IS configured but the call fails or returns something that doesn't
  validate, the row goes to EXTRACTION_FAILED — it does NOT silently fall
  back to the stub, since that would hide a real production problem (bad
  key, wrong deployment name, rate limited) behind data that looks fine
  but is actually low-quality.
- NOT LIVE-VERIFIED against Azure OpenAI specifically: the request/response
  handling below follows Azure OpenAI's documented REST contract, but
  hasn't been exercised against a real Azure OpenAI resource (no
  credentials were available to test with, and a real call would spend the
  user's Azure credits without being asked to). The equivalent Groq
  integration this replaced *was* confirmed working end-to-end against a
  real account on 2026-09-05 — this swap changes the transport (URL shape,
  auth header, api-version) but not the prompt/schema/parsing logic, which
  carries that verification forward as far as it reasonably can without a
  real Azure call.
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
from pipeline.extraction_stub import build_stub_extraction  # Phase 1 fallback, kept for no-credentials dev use

logger = logging.getLogger("scraper_backend_v2.extraction")

# Read fresh from os.environ on every call rather than cached as module-level
# constants at import time — matching db/connection.py's convention
# elsewhere in this service, and specifically so tests can toggle these by
# setting/unsetting the env var mid-run without needing to reload this
# module (the same reason the old Groq integration read GROQ_API_KEY inside
# each function instead of caching it at import time).

def _azure_config() -> dict:
    return {
        "endpoint": os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip().rstrip("/"),
        "api_key": os.environ.get("AZURE_OPENAI_API_KEY", "").strip(),
        # A *deployment name* (what you named the model deployment in Azure
        # AI Foundry / the Azure OpenAI resource), not a base model id like
        # "gpt-4o" — Azure routes by deployment name in the URL path, unlike
        # Groq/OpenAI where the model id goes in the request body.
        "deployment": os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip(),
        # Any Azure OpenAI API version that supports response_format=json_object
        # for your deployment's base model (gpt-4o/gpt-4o-mini/gpt-35-turbo-1106+
        # all support it from 2023-12-01-preview onward).
        "api_version": os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21").strip(),
    }


def _is_configured(config: dict) -> bool:
    return bool(config["endpoint"] and config["api_key"] and config["deployment"])


_SYSTEM_PROMPT = """You are an expert legal metadata extractor for Indian court judgments.

You will receive OCR text from a Supreme Court or High Court judgment, plus
a list of citation-shaped strings already found in the text by a regex
pre-filter. Extract structured metadata as ONE compact JSON object, with
EXACTLY these top-level keys:

{
  "case_note_ai": "one paragraph, 80-150 words, MANUPATRA-style headnote summarizing the legal issue/finding/disposition. Use ONLY facts present in the supplied text — do not invent anything.",
  "decision_type": one of "Judgment","Order","Notification","Circular" — "Judgment" for a full reasoned decision on the merits after hearing arguments (has a coram, discusses the law, gives reasons); "Order" for a short procedural/interlocutory directive without full reasoning (e.g. adjournment, notice issued, interim relief, listing direction); "Notification" or "Circular" only if the text is itself a government/administrative notification or circular rather than a court's decision at all — default to "Judgment" if genuinely unclear,
  "ratio_decidendi": "ONE sentence: the binding legal principle this judgment establishes, distinct from case_note_ai (a summary) — or null if the judgment doesn't state a generalizable principle",
  "cases": [{
    "case_number": "the case's OWN identifier only, e.g. 'Civil Appeal No. 4567 of 2021' — do NOT include a trailing '(Arising out of SLP (C) No. ... of ...)' or similar parenthetical; that reference goes in prior_history.prior_case_number instead. Exception: if the appeal's own number is itself blank/not yet assigned (e.g. 'Civil Appeal No. ___ of 2026' — a real registry convention, not an error), keep the full string including the parenthetical, since it's the only way to tell this case apart from another with the same blank numbering",
    "cnr_number": "... or null",
    "proceedings_start_date": "YYYY-MM-DD or null — the TRUE start of the underlying dispute if stated (e.g. FIR date, date of the original complaint/order under challenge), NOT this instrument's own filing date",
    "proceedings_start_basis": "short phrase for what that date is, e.g. 'FIR registered' or 'original writ petition filed' — or null",
    "case_filed_date": "YYYY-MM-DD or null — when THIS case number itself was filed, only if stated and different from proceedings_start_date",
    "is_lead_matter": true, false, or null if the text doesn't make this clear — true only when this case is explicitly the lead matter among several connected/tagged-along case numbers on the same judgment,
    "parties": [{"name": "...", "side": "PETITIONER_SIDE" or "RESPONDENT_SIDE"}]
  }],
  "coram": [{"name": "judge full name, no honorifics needed", "is_author": true or false}],
  "counsels": [{"name": "advocate full name, no honorifics needed", "side": "PETITIONER_SIDE" or "RESPONDENT_SIDE", "designation": "e.g. 'Sr. Adv.', 'AOR', 'ASG', 'CGSC', 'Adv.', or null"}] — empty if no advocate names are stated in the text (common — many orders only name the parties, not counsel),
  "subjects": ["broad topical tag, e.g. 'Criminal', 'Bail Jurisprudence', 'Land Acquisition', 'Service Matters'"] — 1-3 tags a legal researcher would filter by, empty if the text is too short/procedural to classify,
  "provisions": [{"statute_name": "...", "section_number": "..."}],
  "relevant_section": {"statute_name": "...", "section_number": "..."} or null — the SINGLE section this judgment is most centrally about (must also appear in "provisions"),
  "citations": [{"cited_case_name": "...", "cited_reporter_citation": "... or null", "treatment": one of "Overruled","Affirmed","Distinguished","Followed","Referred","Relied Upon","Explained","Doubted","Discussed","Mentioned", "paragraph_ref": "paragraph number(s) where this case is discussed, e.g. '9' or '4, 8', or null"}],
  "held": [{"holding_text": "one discrete legal holding, in your own concise words", "paragraph_ref": "paragraph number(s) supporting this holding, e.g. '9', or null"}] — ordered list of the judgment's separate holdings, empty if none are clearly separable from case_note_ai,
  "prior_history": {"prior_court_name": "name of the court/forum whose order is under challenge, e.g. 'Judicial Magistrate, Roorkee'", "prior_case_number": "the referenced prior case number, e.g. the SLP number in an 'Arising out of SLP (C) No. ...' reference you excluded from case_number above — or null", "prior_order_date": "YYYY-MM-DD or null", "outcome": one of "Affirmed","Reversed","Partly Reversed","Set Aside","Remanded","Modified" describing what THIS judgment did to that prior order, "notes": "... or null"} or null if this judgment is not reviewing a specific identified lower order,
  "timeline": [{"event_date": "YYYY-MM-DD or null if not stated", "event_date_text": "short label used only when event_date is null, e.g. 'Post-investigation', else null", "description": "one procedural event, e.g. 'FIR registered' or 'Bail granted by Judicial Magistrate'"}] — chronological list reconstructed from the narrative, empty if the judgment has no real procedural history to reconstruct,
  "disposition_category": one of "Allowed","Dismissed","Partly Allowed","Disposed","Remanded","Withdrawn","Quashed","Set Aside","Other", or null if unclear,
  "favoring_party_side": "PETITIONER_SIDE", "RESPONDENT_SIDE", or null,
  "date_of_judgment": "YYYY-MM-DD or null if not stated in the text"
}

Only use the citation candidates list to decide which ones are real cited
cases worth including in "citations" — most candidates are false positives
(page numbers, section references) and should be excluded. Use ONLY facts
stated in the supplied text for every field — never infer or invent a date,
court name, or outcome that isn't actually written there; use null instead.
Return ONLY the JSON object, no markdown fences, no commentary."""


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
    One structured-output call to Azure OpenAI's chat completions API.
    Returns the parsed JSON dict, or None if the call failed / didn't
    return valid JSON — caller treats None as "route to EXTRACTION_FAILED",
    not as "fall back to the stub" (see module docstring on why those are
    different).
    """
    config = _azure_config()
    if not _is_configured(config):
        return None

    # Long judgments: cap at ~12k chars to stay well under context limits
    # while keeping the whole disposition (usually near the end).
    truncated_text = ocr_text if len(ocr_text) <= 12000 else ocr_text[:8000] + "\n...\n" + ocr_text[-4000:]

    user_content = f"JUDGMENT TEXT:\n{truncated_text}"
    if citation_candidates:
        candidate_lines = "\n".join(f"- {c['raw_citation']} (context: {c['context'][:100]})" for c in citation_candidates[:20])
        user_content += f"\n\nCITATION CANDIDATES FOUND BY REGEX PRE-FILTER:\n{candidate_lines}"

    # No "model" field — Azure OpenAI routes by deployment name in the URL,
    # unlike Groq/plain OpenAI where the model id is a body field.
    payload = {
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
    }
    url = f"{config['endpoint']}/openai/deployments/{config['deployment']}/chat/completions?api-version={config['api_version']}"
    headers = {"api-key": config["api_key"], "Content-Type": "application/json"}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=45)
        if not response.ok:
            # Same lesson as the old Groq integration's bug: a bare
            # "404 Client Error" hides the actual reason (wrong deployment
            # name, model doesn't support json_object mode, quota exceeded,
            # etc.) — Azure puts that in the response body, so log it.
            logger.warning("Azure OpenAI extraction call failed: HTTP %s — %s", response.status_code, response.text[:500])
            return None
        raw_content = response.json()["choices"][0]["message"]["content"]
        return _parse_llm_json(raw_content)
    except Exception as e:
        logger.warning("Azure OpenAI extraction call failed: %s", e)
        return None


def _merge_scraper_fields(llm_result: dict, record: RawJudgmentRecord, deployment: str) -> Dict[str, Any]:
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
    elif len(cases) == 1 and not cases[0].get("cnr_number") and record.cnr_raw:
        # "Scraper wins" above only ever applied when the LLM returned NO
        # cases at all -- when it DID return exactly one case (the common
        # case) but left cnr_number null, the scraper's own value was simply
        # never merged in. Only safe to do this when there's exactly one
        # case: with several case numbers on one document there's no way to
        # know which one the record-level cnr_raw actually belongs to.
        cases[0]["cnr_number"] = record.cnr_raw

    return {
        "extractor": f"llm_v1:azure-openai:{deployment}",
        "case_note_ai": llm_result.get("case_note_ai"),
        "decision_type": llm_result.get("decision_type"),
        "ratio_decidendi": llm_result.get("ratio_decidendi"),
        "cases": cases,
        "coram": llm_result.get("coram") or [],
        "counsels": llm_result.get("counsels") or [],
        "subjects": llm_result.get("subjects") or [],
        "provisions": llm_result.get("provisions") or [],
        "relevant_section": llm_result.get("relevant_section"),
        "citations": llm_result.get("citations") or [],
        "held": llm_result.get("held") or [],
        "prior_history": llm_result.get("prior_history"),
        "timeline": llm_result.get("timeline") or [],
        "disposition_category": llm_result.get("disposition_category"),
        "favoring_party_side": llm_result.get("favoring_party_side"),
        "neutral_citation": record.neutral_citation_raw,
        "judgment_date_raw": record.decision_date_raw or llm_result.get("date_of_judgment"),
    }


def process_ingestion_extraction(ingestion_id: int, record: RawJudgmentRecord) -> None:
    """Advances a raw_ingestions row from OCR_DONE to EXTRACTED, or to EXTRACTION_FAILED if the LLM call fails (module docstring explains why that's not a silent stub fallback)."""
    ingestion = scrape_jobs.get_ingestion(ingestion_id)
    ocr_text = (ingestion or {}).get("ocr_text") or ""

    config = _azure_config()
    if not _is_configured(config):
        logger.warning(
            "Azure OpenAI not fully configured (need AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, "
            "AZURE_OPENAI_DEPLOYMENT) — using Phase 1 stub extraction for ingestion_id=%s. Not recommended in production.",
            ingestion_id,
        )
        extraction = build_stub_extraction(record)
        scrape_jobs.update_status(ingestion_id, status="EXTRACTED", raw_ai_extraction=extraction)
        return

    try:
        citation_candidates = citator.find_citation_candidates(ocr_text)
        llm_result = call_llm_extraction(ocr_text, citation_candidates)
        if llm_result is None:
            raise RuntimeError("LLM extraction call failed or returned invalid JSON")

        extraction = _merge_scraper_fields(llm_result, record, config["deployment"])
        scrape_jobs.update_status(ingestion_id, status="EXTRACTED", raw_ai_extraction=extraction)
    except Exception as e:
        scrape_jobs.update_status(ingestion_id, status="EXTRACTION_FAILED", extraction_error=str(e))
