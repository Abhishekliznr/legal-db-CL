"""
PROMOTED -> enriched: fills the fields regex genuinely cannot get
(cases.case_note, cases.industries) plus two low-coverage regex fallbacks
(cases.conclusion, cases.disposition) with one compact LLM call per case.

Runs automatically right after promotion (orchestrator/batch_runner.py),
not as a separate backfill pass — a deliberate choice: simpler pipeline,
at the cost of coupling LLM cost/latency directly to scrape volume. A
failure here never un-promotes a case or fails the batch; it's an
enrichment on top of an already-valid row, not a requirement for one.

Token economy — this is the actual point of this module, not an
afterthought: the old pipeline/extraction.py sent up to 12,000 characters
of OCR text (~3,000 tokens) per call, asking the LLM to rediscover facts
regex already knows (case number, parties, provisions). This module:

1. Truncates to a head+tail excerpt only (~1,200 head + ~2,000 tail chars,
   ~3,200 total, roughly 70% smaller than the old 12,000-char budget) — a
   case note / industry tag / conclusion draws mostly on the facts (start)
   and the court's reasoning/operative finding (end); the middle of a
   judgment is usually argument-by-argument legal reasoning that's much
   less load-bearing for these specific fields than it would be for a full
   structured extraction.
2. Feeds regex-derived facts (case number, resolved act/section names,
   coarse subject, disposition if already found) as compact hint lines
   instead of asking the LLM to re-derive them from the excerpt — same
   grounding-not-rediscovery approach pipeline/citator.py's
   find_citation_candidates() already uses for citations.
3. Batches case_note + industries + ministries + conclusion + disposition
   into ONE call, since the (now much smaller) excerpt is the expensive
   part of the prompt either way — paying for it once beats paying for it
   five times across five separate calls.
4. Only asks for disposition as a safety net; pipeline/promotion.py's own
   regex classifier is already ~97% accurate on real judgments (see
   pipeline/regex_extraction.py's verification notes) — the caller here
   only uses the LLM's answer when the regex one is NULL, never overwrites
   a regex result that already exists.

Deliberately NOT reusing pipeline/extraction.py's big structured-output
prompt (cases/coram/counsels/citations/held/timeline/etc.) — that module is
now stale (see its own module docstring) and was built for an envelope
this schema doesn't have anymore. This module only asks for the 4 fields
that actually need filling.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

import requests

from db.connection import get_pooled_connection
from normalization.industries import CANONICAL_INDUSTRIES, resolve_industry
from pipeline.extraction import _azure_config, _is_configured  # same Azure config/auth, reused not duplicated
from pipeline.promotion import _get_or_create_industry, _get_or_create_ministry
from pipeline.regex_extraction import KNOWN_MINISTRIES, resolve_ministry

logger = logging.getLogger("scraper_backend_v2.llm_enrichment")

_VALID_DISPOSITION_CATEGORIES = {
    "Allowed", "Dismissed", "Partly Allowed", "Disposed",
    "Remanded", "Withdrawn", "Quashed", "Set Aside", "Other",
}

# Head+tail excerpt budget -- see module docstring for why these specific
# numbers (~70% smaller than pipeline/extraction.py's 8000+4000 budget).
_HEAD_CHARS = 1200
_TAIL_CHARS = 2000

# A real Manupatra headnote, supplied by the user as the exact target
# style: one dash-separated digest (subject - sub-topic - provision -
# procedural posture - "Held," - reasoning chain - disposition), NOT
# flowing prose. Given verbatim as a few-shot example in the system
# prompt below -- telling an LLM "write like Manupatra" without a concrete
# example reliably drifts toward ordinary paragraph summarization instead
# of this specific telegraphic, dash-separated register.
_CASE_NOTE_EXAMPLE = (
    "Criminal - Death sentence - Confirmation thereof - Section 302 of Indian Penal Code, 1860 (I.P.C.) - "
    "Additional Sessions Judge had made a reference to this Court for confirmation of death sentence passed "
    "by him in impugned judgment - Held, Once it is held that statement of accused is admissible in evidence "
    "as 'extra-judicial confession' it is apparent that this alone would be sufficient to sustain his "
    "conviction under Section 302 of I.P.C. - Each link thereof is so strong that when considered as a whole, "
    "one is impelled to conclude that it leads to no other inference but one which is consistent with guilt "
    "of accused - Case against accused under Section 302 of I.P.C. had been proved beyond all reasonable "
    "doubt and trial court had rightly convicted him thereunder - Case falls in category of those rarest of "
    "rare cases where there appears to be complete absence of alternative option, namely, circumstances "
    "justifying passing of lighter sentence - Reference made by trial court was accepted and sentence passed "
    "by trial court was confirmed - Appeal dismissed"
)

_SYSTEM_PROMPT = f"""You are an expert legal editor writing headnotes for an Indian case law database, in the house style of Manupatra.

You will receive a compact excerpt of a Supreme Court judgment (facts + concluding reasoning, not the full text) plus a few known facts already extracted by regex. Return ONLY a compact JSON object with EXACTLY these keys, no markdown fences, no commentary:

{{
  "case_note": "A single dash-separated digest in the EXACT style of the example below — NOT a flowing prose paragraph. Structure: <broad subject area> - <narrower topic> - <specific sub-issue, if any> - <the key statutory provision(s), formatted like 'Section 302 of Indian Penal Code, 1860 (I.P.C.)'> - <one clause stating the procedural posture, i.e. how this matter reached this court> - Held, <the court's core holding> - <supporting reasoning clause> - <supporting reasoning clause> - ... - <the final disposition, in the terse form reporters use, e.g. 'Appeal dismissed' / 'Appeal allowed' / 'Petition disposed of' / 'Appeal partly allowed'>. Telegraphic style throughout: favor concise legal phrasing over full grammatical sentences, each dash-separated segment is its own proposition. Use ONLY facts/reasoning actually present in the supplied excerpt — never invent a fact, provision, or outcome that isn't there.\\n\\nEXAMPLE (match this register exactly):\\n{_CASE_NOTE_EXAMPLE}",
  "conclusion": "1-3 sentences of ORDINARY prose (not dash-separated, unlike case_note) capturing the court's final concluding reasoning that leads directly to the disposition — or null if the excerpt doesn't clearly show this.",
  "industries": ["0 to 3 tags from this EXACT closed list, choose only industries the case is CENTRALLY about (a party's business, the subject of the dispute), not one a name/word merely appears near: {json.dumps(CANONICAL_INDUSTRIES)}. Empty list if none clearly apply — most criminal/service/constitutional matters have none."],
  "ministries": ["0 to 3 names from this EXACT closed list, naming a ministry ONLY when the case is substantively about that ministry's policy, regulation, or scheme — not merely because a ministry is a named party (that's already handled separately): {json.dumps(KNOWN_MINISTRIES)}. Empty list if none apply."],
  "disposition_category": "one of Allowed, Dismissed, Partly Allowed, Disposed, Remanded, Withdrawn, Quashed, Set Aside, Other — or null if genuinely unclear from the excerpt. A regex classifier already handles most documents; this is only used as a fallback when that classifier found nothing, so answer independently from the excerpt rather than guessing to fill the field."
}}

Use ONLY facts present in the supplied excerpt and hints. Never invent a party, provision, date, or outcome that isn't there — use null/empty instead."""


def _build_excerpt(ocr_text: str) -> str:
    if not ocr_text:
        return ""
    if len(ocr_text) <= _HEAD_CHARS + _TAIL_CHARS:
        return ocr_text
    return ocr_text[:_HEAD_CHARS] + "\n...\n" + ocr_text[-_TAIL_CHARS:]


def _build_user_content(case_row: Dict[str, Any]) -> str:
    hints = [f"Case number: {case_row['case_number']}"]
    if case_row.get("subject_name"):
        hints.append(f"Subject (coarse): {case_row['subject_name']}")
    if case_row.get("act_names"):
        hints.append(f"Provisions already identified: {', '.join(case_row['act_names'])}")
    if case_row.get("disposition"):
        hints.append(f"Disposition already determined by regex: {case_row['disposition']} (do not need to re-derive)")

    excerpt = _build_excerpt(case_row.get("ocr_text") or "")
    return "KNOWN FACTS:\n" + "\n".join(hints) + f"\n\nJUDGMENT EXCERPT (facts + concluding portion only):\n{excerpt}"


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


def call_llm_enrichment(case_row: Dict[str, Any]) -> Optional[dict]:
    """One structured-output call to Azure OpenAI. Returns the parsed JSON dict, or None on any failure — caller treats None as 'enrichment skipped for this case', not a promotion failure."""
    config = _azure_config()
    if not _is_configured(config):
        return None

    user_content = _build_user_content(case_row)
    payload = {
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 700,  # a dash-digest case_note + short conclusion + a few tags — no reason for this to run long
        "response_format": {"type": "json_object"},
    }
    url = f"{config['endpoint']}/openai/deployments/{config['deployment']}/chat/completions?api-version={config['api_version']}"
    headers = {"api-key": config["api_key"], "Content-Type": "application/json"}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=45)
        if not response.ok:
            logger.warning("Azure OpenAI enrichment call failed: HTTP %s — %s", response.status_code, response.text[:500])
            return None
        raw_content = response.json()["choices"][0]["message"]["content"]
        return _parse_llm_json(raw_content)
    except Exception as e:
        logger.warning("Azure OpenAI enrichment call failed: %s", e)
        return None


def _fetch_case_row(cur, case_id: int) -> Optional[Dict[str, Any]]:
    cur.execute("""
        SELECT c.case_number, c.ocr_text, c.disposition, c.ministries,
               subj.subject_name,
               (SELECT array_agg(DISTINCT a.act_name) FROM acts a WHERE a.act_id = ANY(c.acts)) AS act_names
        FROM cases c
        LEFT JOIN subjects subj ON subj.subject_id = c.subject
        WHERE c.case_id = %s;
    """, (case_id,))
    row = cur.fetchone()
    if row is None:
        return None
    case_number, ocr_text, disposition, existing_ministry_ids, subject_name, act_names = row
    return {
        "case_number": case_number,
        "ocr_text": ocr_text,
        "disposition": disposition,
        "existing_ministry_ids": existing_ministry_ids or [],
        "subject_name": subject_name,
        "act_names": act_names or [],
    }


def enrich_case(case_id: int) -> bool:
    """
    Fetches the already-promoted case, calls the LLM, and updates
    case_note/conclusion/industries/ministries/disposition on it. Returns
    True if the row was updated, False if enrichment was skipped or failed
    (not configured, no ocr_text, call failed, bad JSON) -- never raises,
    since a caller in the middle of a scrape batch must not lose the rest
    of the batch over one enrichment failure.
    """
    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                case_row = _fetch_case_row(cur, case_id)
                if case_row is None:
                    logger.warning("[ENRICH] case_id=%s: no such case, skipping", case_id)
                    return False
                if not case_row["ocr_text"]:
                    logger.info("[ENRICH] case_id=%s: no ocr_text, skipping", case_id)
                    return False

                llm_result = call_llm_enrichment(case_row)
                if llm_result is None:
                    logger.info("[ENRICH] case_id=%s: LLM call unavailable or failed, skipping", case_id)
                    return False

                case_note = llm_result.get("case_note")
                conclusion = llm_result.get("conclusion")

                industry_ids = []
                for raw in (llm_result.get("industries") or []):
                    resolved = resolve_industry(raw)
                    if resolved:
                        industry_ids.append(_get_or_create_industry(cur, resolved))

                ministry_ids = list(case_row["existing_ministry_ids"])  # keep regex-derived party-based ministries
                for raw in (llm_result.get("ministries") or []):
                    resolved = resolve_ministry(raw)
                    if resolved:
                        new_id = _get_or_create_ministry(cur, resolved)
                        if new_id not in ministry_ids:
                            ministry_ids.append(new_id)

                llm_disposition = llm_result.get("disposition_category")
                if llm_disposition not in _VALID_DISPOSITION_CATEGORIES:
                    llm_disposition = None

                cur.execute("""
                    UPDATE cases SET
                        case_note = COALESCE(%s, case_note),
                        conclusion = COALESCE(conclusion, %s),
                        industries = %s,
                        ministries = %s,
                        disposition = COALESCE(disposition, %s)
                    WHERE case_id = %s;
                """, (case_note, conclusion, industry_ids, ministry_ids, llm_disposition, case_id))
            conn.commit()
        logger.info("[ENRICH] case_id=%s: done — case_note=%s industries=%d ministries=%d",
                    case_id, "set" if case_note else "unchanged", len(industry_ids), len(ministry_ids))
        return True
    except Exception:
        logger.exception("[ENRICH] case_id=%s: unhandled exception", case_id)
        return False
