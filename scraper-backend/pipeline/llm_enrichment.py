"""
PROMOTED -> enriched: fills the fields regex genuinely cannot get
(cr_cases.case_note, cr_cases.industries, cr_cases.favouring_party) plus
low-coverage regex fallbacks (cr_cases.conclusion, cr_cases.disposition,
cr_cases.subject), plus a paragraph-filtered fallback extraction of
cr_cases.sections/acts for a case whose own adapter found none directly
(2026-09-09, simplified 2026-09-19 when the sections_relevant/_other split
and the separate rules/orders columns were dropped entirely — see
enrich_case()'s provision_block parameter below)
— all in one Azure OpenAI Structured Outputs call per case.

Runs automatically right after promotion (orchestrator/batch_runner.py),
not as a separate backfill pass — a deliberate choice: simpler pipeline,
at the cost of coupling LLM cost/latency directly to scrape volume. A
failure here never un-promotes a case or fails the batch; it's an
enrichment on top of an already-valid row, not a requirement for one.

Rewritten 2026-09-10 after real-world testing against a 142-page, ~298,000
char Supreme Court judgment (7 connected criminal appeals) surfaced several
production problems with the original head+tail-excerpt version of this
module:

- The old fixed 1,200/2,000-char head+tail excerpt covered ~1% of that
  judgment and missed the operative outcome entirely — the head was a
  cause title/signature block/table of contents, the tail was routine
  trial directions. _build_excerpt() now cleans OCR noise first (signature
  blocks, dot-leader TOC lines, bare page numbers), sends the FULL text for
  anything under _FULL_TEXT_LIMIT, and for longer judgments finds an actual
  conclusion/operative-section heading in the second half of the text to
  anchor the tail on, rather than blindly taking the last N characters.
- A fixed max_tokens silently truncated the JSON on judgments citing many
  provisions (165 in the real case), and a truncated response made the old
  lenient parser return None — enrichment was discarded with no record of
  why. call_llm_enrichment() now steps through _OUTPUT_TOKEN_STEPS, checks
  finish_reason explicitly, and reports a distinct TRUNCATED outcome
  instead of silently vanishing.
- response_format=json_object plus a prompt describing "provisions" as
  pseudo-JSON let the model return provisions as a string that got quietly
  treated as empty. Azure OpenAI Structured Outputs (response_format=
  json_schema, strict=True) now enforces the real shape; the old lenient
  parser is kept only as a fallback for deployments that reject json_schema.
- The pooled DB connection used to stay open, inside a transaction, for the
  whole LLM HTTP call (up to 45s, now up to 120s with retries) — under
  concurrency this exhausts the pool with idle-in-transaction connections.
  enrich_case() now fetches the case row on one short-lived connection,
  makes the LLM call with no connection held at all, then opens a second
  connection only to write the result.
- Failures used to be indistinguishable from "case has no provisions" and
  impossible to find and retry. cr_cases now carries enrichment_status/
  enrichment_error/enriched_at/enrichment_attempts (db/migrations/0001_...);
  find_cases_needing_enrichment() lists cases worth retrying.
- A re-run that returned empty provisions used to overwrite good data, and
  favouring_party could be overwritten with null. Provision columns are now
  only written when a provision-paragraph block was actually sent to the
  model, and favouring_party/case_note both use COALESCE(new, existing).
- Judgments deciding several connected matters with different outcomes
  (e.g. bail granted to some appellants, refused to others) used to give
  the model no signal that this was happening, so the digest collapsed a
  mixed result into one word. _detect_connected_matters() scans the cause
  title for multiple case numbers and, when found, hints the model to
  state each outcome separately.
- The case_note few-shot example was a verbatim third-party (Manupatra)
  headnote; replaced with an original example, explicitly labeled in the
  prompt as a FORMAT example only.
"""

import json
import logging
import re
import time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import requests

from db.connection import get_pooled_connection
from db.lookups import get_or_create_industry, get_or_create_ministry, get_or_create_subject, resolve_provisions
from normalization.acts import resolve_act
from normalization.industries import CANONICAL_INDUSTRIES, resolve_industry
from normalization.ministries import KNOWN_MINISTRIES, resolve_ministry
from normalization.ocr_artifacts import strip_ocr_noise as _strip_ocr_noise
from pipeline.azure_openai import azure_config, is_configured

logger = logging.getLogger("scraper_backend_v2.llm_enrichment")

_VALID_DISPOSITION_CATEGORIES = {
    "Allowed", "Dismissed", "Partly Allowed", "Disposed",
    "Remanded", "Withdrawn", "Quashed", "Set Aside", "Other",
}

_VALID_FAVOURING_PARTIES = {"Petitioner", "Respondent", "Partly", "Neither"}

# ---------------------------------------------------------------------
# Excerpt selection
# ---------------------------------------------------------------------

# Below this, the cleaned OCR text is sent in full ("FULL JUDGMENT" mode).
# Above it, head+tail excerpting kicks in ("long judgment" mode) -- see
# _build_excerpt().
_FULL_TEXT_LIMIT = 60_000
_HEAD_CHARS = 6_000
_TAIL_CHARS = 24_000

# Caps the PARAGRAPHS CONTAINING STATUTORY REFERENCES block fed alongside
# the excerpt -- provisions are usually cited in the reasoning section the
# head+tail excerpt above deliberately excludes, so this is sent
# independently, sized for judgments citing well over a hundred provisions.
_PROVISION_BLOCK_MAX_CHARS = 40_000

# Matches a conclusion/operative-section heading, e.g. "CONCLUSION",
# "FINAL CONCLUSIONS", "OPERATIVE PORTION", "RESULT", "RELIEF" -- used to
# anchor the tail excerpt of a long judgment on its actual operative
# findings rather than an arbitrary last-N-chars cut, which for the
# 142-page test judgment landed in routine trial directions and thanks to
# counsel instead of the bail outcome.
_CONCLUSION_HEADING_RE = re.compile(
    r"^[\s\d.]*(?:FINAL\s+)?(?:CONCLUSIONS?|OPERATIVE|RESULT|RELIEF)\b[^\n]*$",
    re.MULTILINE,
)

def _build_excerpt(ocr_text: str) -> Tuple[str, str]:
    """
    Returns (excerpt, mode_description). Short/cleaned judgments are sent
    whole. Long ones send an opening block plus a tail anchored on the last
    conclusion-style heading found in the second half of the text (earlier
    matches are usually table-of-contents entries, already mostly stripped
    by _strip_ocr_noise but checked again here defensively) -- falling back
    to a plain last-_TAIL_CHARS cut when no such heading is found there.
    """
    cleaned = _strip_ocr_noise(ocr_text)
    total_len = len(cleaned)

    if total_len <= _FULL_TEXT_LIMIT:
        return cleaned, f"FULL JUDGMENT, {total_len:,} chars"

    head = cleaned[:_HEAD_CHARS]

    tail_start = None
    for match in reversed(list(_CONCLUSION_HEADING_RE.finditer(cleaned))):
        if match.start() >= total_len / 2:
            tail_start = match.start()
            break
    if tail_start is None:
        tail_start = max(0, total_len - _TAIL_CHARS)

    tail = cleaned[tail_start:]
    if len(tail) > _TAIL_CHARS:
        tail = tail[:_TAIL_CHARS]

    excerpt = head + "\n[... middle of judgment omitted ...]\n" + tail
    mode = f"long judgment, {total_len:,} chars total, middle omitted (showing opening {len(head):,} chars + concluding {len(tail):,} chars)"
    return excerpt, mode


# ---------------------------------------------------------------------
# Connected matters (fixes the "several appeals, different outcomes"
# case, where nothing previously told the model this was happening)
# ---------------------------------------------------------------------

_CASE_TYPE_ALTERNATION = (
    r"(?:S(?:PECIAL\s+)?L(?:EAVE\s+)?P(?:ETITION)?\.?)"
    r"|(?:CIVIL\s+APPEAL)"
    r"|(?:CRIMINAL\s+APPEAL)"
    r"|(?:WRIT\s+PETITION)"
    r"|(?:TRANSFER\s+PETITION)"
    r"|(?:REVIEW\s+PETITION)"
    r"|(?:CONTEMPT\s+PETITION)"
    r"|(?:ARBITRATION\s+PETITION)"
)

# e.g. "SLP (CRL.) NO. 13988/2025", "CIVIL APPEAL NO. 123 OF 2024",
# "WRIT PETITION (C) NO. 456/2023" -- a number with no digits at all (the
# registry's own "NO. _____ OF 2026" placeholder for a not-yet-assigned
# number) does not match \d+, so it's correctly excluded.
_CONNECTED_MATTER_RE = re.compile(
    r"\b(?:" + _CASE_TYPE_ALTERNATION + r")"
    r"\s*(?:\([A-Za-z.\s]+\))?"
    r"\s*NO\.?\s*"
    r"(\d[\d\-/]*)"
    r"\s*(?:/|OF)\s*"
    r"(\d{4})\b",
    re.IGNORECASE,
)

# A standalone "JUDGMENT" (or letter-spaced "J U D G M E N T") or "ORDER"
# heading -- the cause title ends here; matter numbers repeated afterward
# (e.g. in a table of contents) must not be counted again.
_BODY_HEADING_RE = re.compile(r"^\s*(?:J\s*U\s*D\s*G\s*M\s*E\s*N\s*T|O\s*R\s*D\s*E\s*R)\s*$", re.MULTILINE | re.IGNORECASE)

_CAUSE_TITLE_SCAN_CHARS = 8_000


def _detect_connected_matters(ocr_text: str) -> List[str]:
    """Looks only at the cause title (the first ~8,000 chars, cut at the first body heading) for distinct case numbers, deduped by (number, year). Used to warn the model when one judgment decides several matters that may have different outcomes."""
    if not ocr_text:
        return []

    cause_title = ocr_text[:_CAUSE_TITLE_SCAN_CHARS]
    heading = _BODY_HEADING_RE.search(cause_title)
    if heading:
        cause_title = cause_title[:heading.start()]

    seen = set()
    matters: List[str] = []
    for match in _CONNECTED_MATTER_RE.finditer(cause_title):
        number = re.sub(r"\s+", "", match.group(1))
        year = match.group(2)
        key = (number, year)
        if key in seen:
            continue
        seen.add(key)
        matters.append(re.sub(r"\s+", " ", match.group(0)).strip())

    return matters


# A real original example (not a third-party reporter's headnote), given
# verbatim as a few-shot FORMAT example in the system prompt below --
# telling the model "write a dash-separated digest" without a concrete
# example reliably drifts toward ordinary paragraph summarization instead
# of this specific telegraphic, dash-separated register.
_CASE_NOTE_EXAMPLE = (
    "Criminal - Bail - Unlawful activities - Prolonged pre-trial incarceration - "
    "Section 43D(5) of Unlawful Activities (Prevention) Act, 1967 (UAPA) - Article 21 of "
    "Constitution of India - Appellants, accused in larger conspiracy case arising out of "
    "riots, challenged common judgment of High Court affirming rejection of their bail "
    "applications - Held, In prosecutions under special statute, delay does not operate as "
    "trump card displacing statutory restraint but triggers heightened judicial scrutiny - "
    "Inquiry under Section 43D(5) is accused-specific and tests whether prosecution material, "
    "taken at face value, discloses prima facie case against each accused - Prosecution itself "
    "attributed central and formative role in planning and mobilisation to two appellants, "
    "attracting statutory embargo - Remaining five appellants projected as local facilitators "
    "acting on directions of others, and their continued detention not shown to be necessary "
    "where stringent conditions could secure trial - Denial of bail where allegations "
    "substantially identical to co-accused already on bail would offend parity - Bail declined "
    "to two appellants with liberty to renew after examination of protected witnesses or one "
    "year, whichever earlier - Bail granted to five appellants on conditions - Appeals of two "
    "appellants dismissed; appeals of five appellants allowed"
)

_SYSTEM_PROMPT = f"""You are an expert legal editor writing headnotes for an Indian case law database.

You will receive either the FULL text of a judgment, or its opening (facts) plus its concluding/operative section when the judgment is too long to send in full -- the input tells you which, and marks where the middle was omitted if so. You will also receive a few facts already extracted by regex (case number, resolved act/section names, coarse subject, disposition if already found), and, when this judgment decides more than one matter, a list of those connected matters. Return ONLY a compact JSON object with EXACTLY these keys, no markdown fences, no commentary:

{{
  "case_note": "A single dash-separated digest in the dash-separated digest format used by Indian law reporters -- NOT a flowing prose paragraph. Structure: <broad subject area> - <narrower topic> - <specific sub-issue, if any> - <the key statutory provision(s), formatted like 'Section 302 of Indian Penal Code, 1860 (I.P.C.)'> - <one clause stating the procedural posture, i.e. how this matter reached this court> - Held, <the court's core holding> - <supporting reasoning clause> - <supporting reasoning clause> - ... - <the final disposition, in the terse form reporters use, e.g. 'Appeal dismissed' / 'Appeal allowed' / 'Petition disposed of' / 'Appeal partly allowed'>. Telegraphic style throughout: favor concise legal phrasing over full grammatical sentences, each dash-separated segment is its own proposition. Rules: (1) If several connected matters or parties have different outcomes, state each outcome separately in the disposition segment(s) -- never collapse a mixed result into one word. (2) Phrase prima facie or interim-stage findings as such (e.g. 'held there was a prima facie case') -- an allegation is not a finding. (3) Return null if the supplied text does not clearly show both the court's holding and its disposition. Use ONLY facts/reasoning actually present in the supplied text -- never invent a fact, provision, or outcome that isn't there.\n\nFORMAT EXAMPLE ONLY -- match this register and structure exactly, but NEVER copy its facts, subject, provisions, or outcome into your answer:\n{_CASE_NOTE_EXAMPLE}",
  "conclusion": "1-3 sentences of ORDINARY prose (not dash-separated, unlike case_note) capturing the court's final concluding reasoning that leads directly to the disposition -- or null if the supplied text doesn't clearly show this.",
  "subject": "A short (1-4 word) coarse subject/jurisdiction tag for this case, in the style of a docket label -- e.g. 'Criminal', 'Civil', 'Writ - Service Matter', 'Constitutional', 'Arbitration'. Only used as a fallback when no subject was already determined by regex (see KNOWN FACTS) -- if one was already given there, return that same value. Return null if genuinely unclear.",
  "industries": ["0 to 3 tags from this EXACT closed list, choose only industries the case is CENTRALLY about (a party's business, the subject of the dispute), not one a name/word merely appears near: {json.dumps(CANONICAL_INDUSTRIES)}. Empty list if none clearly apply -- most criminal/service/constitutional matters have none."],
  "ministries": ["0 to 3 names from this EXACT closed list, naming a ministry ONLY when the case is substantively about that ministry's policy, regulation, or scheme -- not merely because a ministry is a named party (that's already handled separately): {json.dumps(KNOWN_MINISTRIES)}. Empty list if none apply."],
  "disposition_category": "one of Allowed, Dismissed, Partly Allowed, Disposed, Remanded, Withdrawn, Quashed, Set Aside, Other -- or null if genuinely unclear from the text. A judgment deciding connected matters with different outcomes for different parties is 'Partly Allowed'. A regex classifier already handles most documents; this is only used as a fallback when that classifier found nothing, so answer independently from the text rather than guessing to fill the field.",
  "favouring_party": "one of Petitioner, Respondent, Partly, Neither -- or null if genuinely unclear. 'Petitioner': the petitioner's plea was substantially granted (appeal/petition allowed, conviction set aside, relief granted). 'Respondent': the petitioner's plea was rejected/dismissed, respondent's position upheld. 'Partly': a mixed outcome, including connected matters with different outcomes for different parties. 'Neither': a procedural order with no substantive winner (adjournment, notice issued, interim direction, remand without a clear beneficiary).",
  "provisions": "Extract every unique statutory provision referenced in the PARAGRAPHS CONTAINING STATUTORY REFERENCES block below (not from the judgment text above it), following these rules: (1) Extract ONLY from that block; if it is absent from the input, return an empty list. (2) List each unique (statute, number) pair once. (3) A constitutional article's number is the article number only (e.g. '21', not 'Article 21'), statute_name 'Constitution of India'; a Rule/Order is still just a number+statute pair, e.g. 'Order XXI' -> number 'XXI' of the relevant Code. (4) number is the bare provision number/designation only, e.g. '43D(5)', not the surrounding sentence. (5) Resolve the governing act from the surrounding paragraph text, not just the words immediately next to the number."
}}

Use ONLY facts present in the supplied text and hints. Never invent a party, provision, date, or outcome that isn't there -- use null/empty instead."""


# ---------------------------------------------------------------------
# Structured Outputs JSON schema (response_format=json_schema, strict=True)
# ---------------------------------------------------------------------

_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["case_note", "conclusion", "subject", "industries", "ministries", "disposition_category", "favouring_party", "provisions"],
    "properties": {
        "case_note": {"type": ["string", "null"]},
        "conclusion": {"type": ["string", "null"]},
        "subject": {"type": ["string", "null"]},
        "industries": {
            "type": "array",
            "items": {"type": "string", "enum": CANONICAL_INDUSTRIES},
        },
        "ministries": {
            "type": "array",
            "items": {"type": "string", "enum": KNOWN_MINISTRIES},
        },
        "disposition_category": {
            "type": ["string", "null"],
            "enum": sorted(_VALID_DISPOSITION_CATEGORIES) + [None],
        },
        "favouring_party": {
            "type": ["string", "null"],
            "enum": sorted(_VALID_FAVOURING_PARTIES) + [None],
        },
        "provisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["statute_name", "number"],
                "properties": {
                    "statute_name": {"type": "string"},
                    "number": {"type": "string"},
                },
            },
        },
    },
}

# Set true the first time an Azure deployment rejects response_format=
# json_schema with HTTP 400 -- from then on this process uses the more
# widely-supported response_format=json_object instead. Deliberately
# process-wide (not per-call): a deployment that doesn't support Structured
# Outputs won't start supporting it mid-process, so there's no value in
# re-discovering this on every single case.
_json_schema_unsupported = False


def _response_format() -> dict:
    if _json_schema_unsupported:
        return {"type": "json_object"}
    return {"type": "json_schema", "json_schema": {"name": "case_enrichment", "strict": True, "schema": _JSON_SCHEMA}}


# ---------------------------------------------------------------------
# Output token budget + reasoning-model detection
# ---------------------------------------------------------------------

# First call at 6,000; if finish_reason=="length", retry once at 12,000
# before giving up and reporting TRUNCATED. A dash-digest case_note + short
# conclusion + a few tags + a long provisions array (165+ entries on the
# real judgment that prompted this rewrite) can legitimately need more
# than a single small budget.
_OUTPUT_TOKEN_STEPS = (6_000, 12_000)

# gpt-5 and the o<digit> series (o1, o3, o4, ...) are reasoning models on
# Azure OpenAI -- they take max_completion_tokens instead of max_tokens and
# reject a temperature parameter entirely.
_REASONING_MODEL_RE = re.compile(r"gpt-5|(?<![a-z0-9])o\d", re.IGNORECASE)


def _is_reasoning_model(deployment: str) -> bool:
    return bool(_REASONING_MODEL_RE.search(deployment or ""))


def _build_payload(deployment: str, user_content: str, max_output_tokens: int) -> dict:
    payload = {
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "response_format": _response_format(),
    }
    if _is_reasoning_model(deployment):
        payload["max_completion_tokens"] = max_output_tokens
    else:
        payload["temperature"] = 0.1
        payload["max_tokens"] = max_output_tokens
    return payload


# ---------------------------------------------------------------------
# HTTP call with retry/backoff
# ---------------------------------------------------------------------

_HTTP_TIMEOUT_SECONDS = 120
_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
_MAX_HTTP_RETRIES = 2


def _sleep_before_retry(attempt: int, retry_after: Optional[str]) -> None:
    delay = None
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            delay = None
    if delay is None:
        delay = 2 ** attempt
    time.sleep(delay)


def _post_once(config: dict, payload: dict):
    url = f"{config['endpoint']}/openai/deployments/{config['deployment']}/chat/completions?api-version={config['api_version']}"
    headers = {"api-key": config["api_key"], "Content-Type": "application/json"}
    return requests.post(url, headers=headers, json=payload, timeout=_HTTP_TIMEOUT_SECONDS)


def _post_with_retries(config: dict, user_content: str, max_output_tokens: int) -> Tuple[Optional[dict], Optional[str]]:
    """Returns (response_json, error). Handles the json_schema -> json_object fallback (retried immediately, doesn't consume the transient-error retry budget) and retries transient HTTP/network failures with backoff honouring Retry-After."""
    global _json_schema_unsupported
    payload = _build_payload(config["deployment"], user_content, max_output_tokens)

    attempt = 0
    while True:
        try:
            response = _post_once(config, payload)
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt >= _MAX_HTTP_RETRIES:
                logger.warning("Azure OpenAI enrichment call failed after %d retries: %s", attempt, e)
                return None, str(e)
            _sleep_before_retry(attempt, None)
            attempt += 1
            continue

        if response.ok:
            try:
                return response.json(), None
            except ValueError as e:
                return None, f"non-JSON HTTP response: {e}"

        if response.status_code == 400 and not _json_schema_unsupported:
            body_lower = response.text.lower()
            if "response_format" in body_lower or "json_schema" in body_lower:
                logger.warning(
                    "Azure OpenAI enrichment call: deployment %r rejected response_format=json_schema (HTTP 400) — "
                    "falling back to json_object for the rest of this process. Body: %s",
                    config["deployment"], response.text[:500],
                )
                _json_schema_unsupported = True
                payload = _build_payload(config["deployment"], user_content, max_output_tokens)
                continue

        if response.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_HTTP_RETRIES:
            _sleep_before_retry(attempt, response.headers.get("Retry-After"))
            attempt += 1
            continue

        logger.warning("Azure OpenAI enrichment call failed: HTTP %s — %s", response.status_code, response.text[:500])
        return None, f"HTTP {response.status_code}: {response.text[:300]}"


def _parse_llm_json(raw_content: str) -> Optional[dict]:
    """Lenient parser (strips markdown fences, takes the outer {...} span) -- kept only for the response_format=json_object fallback path; the json_schema strict path returns clean JSON directly."""
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


def _parse_llm_response(raw_content: str) -> Optional[dict]:
    if _json_schema_unsupported:
        return _parse_llm_json(raw_content)
    if not raw_content:
        return None
    try:
        result = json.loads(raw_content)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        return None


class EnrichmentCallResult(NamedTuple):
    data: Optional[dict]
    status: str  # "DONE" | "TRUNCATED" | "FAILED" | "NOT_CONFIGURED"
    error: Optional[str]


def call_llm_enrichment(case_row: Dict[str, Any], user_content: Optional[str] = None) -> EnrichmentCallResult:
    """
    One (or, on truncation, two) Structured Outputs call(s) to Azure OpenAI.
    `user_content` may be pre-built by the caller (enrich_case builds it
    once, before opening the DB connection that will eventually write the
    result) -- built from `case_row` if not supplied.
    """
    config = azure_config()
    if not is_configured(config):
        return EnrichmentCallResult(None, "NOT_CONFIGURED", None)

    if user_content is None:
        user_content, _ = _build_user_content(case_row)

    last_error = None
    for max_output_tokens in _OUTPUT_TOKEN_STEPS:
        response_json, error = _post_with_retries(config, user_content, max_output_tokens)
        if response_json is None:
            return EnrichmentCallResult(None, "FAILED", error)

        try:
            choice = response_json["choices"][0]
            message = choice.get("message", {})
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as e:
            return EnrichmentCallResult(None, "FAILED", f"unexpected response shape: {e}")

        if message.get("refusal"):
            return EnrichmentCallResult(None, "FAILED", f"model refused: {str(message['refusal'])[:300]}")
        if finish_reason == "content_filter":
            return EnrichmentCallResult(None, "FAILED", "response blocked by Azure OpenAI content filter")
        if finish_reason == "length":
            last_error = f"response truncated at max_output_tokens={max_output_tokens} (finish_reason=length)"
            continue

        parsed = _parse_llm_response(message.get("content") or "")
        if parsed is None:
            return EnrichmentCallResult(None, "FAILED", "could not parse JSON from LLM response")
        return EnrichmentCallResult(parsed, "DONE", None)

    return EnrichmentCallResult(None, "TRUNCATED", last_error)


# ---------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------

def _build_user_content(case_row: Dict[str, Any], provision_block: str = "") -> Tuple[str, bool]:
    """
    Returns (user_content, has_provision_block) -- the caller needs to know
    whether a provision-paragraph block was actually sent, since provision
    columns are only written when it was (see enrich_case).

    `provision_block` is supplied by the caller (enrich_case), built by
    whichever court's own extraction module found the case's OCR text --
    this module has no court-specific extraction logic of its own, by
    design (see this module's own docstring).
    """
    ocr_text = case_row.get("ocr_text") or ""

    hints = [f"Case number: {case_row['case_number']}"]
    if case_row.get("subject_name"):
        hints.append(f"Subject (coarse): {case_row['subject_name']}")
    if case_row.get("disposition"):
        hints.append(f"Disposition already determined by regex: {case_row['disposition']} (do not need to re-derive)")

    connected_matters = _detect_connected_matters(ocr_text)
    if len(connected_matters) > 1:
        hints.append(
            f"This judgment decides {len(connected_matters)} connected matters: {', '.join(connected_matters)}. "
            "If their outcomes differ, state each outcome separately in the case_note disposition."
        )

    excerpt, mode = _build_excerpt(ocr_text)
    content = "KNOWN FACTS:\n" + "\n".join(hints) + f"\n\nJUDGMENT TEXT [{mode}]:\n{excerpt}"

    has_provision_block = bool(provision_block)
    if has_provision_block:
        if len(provision_block) > _PROVISION_BLOCK_MAX_CHARS:
            provision_block = provision_block[:_PROVISION_BLOCK_MAX_CHARS] + "\n[... truncated ...]"
        content += f"\n\nPARAGRAPHS CONTAINING STATUTORY REFERENCES (extract provisions ONLY from here):\n{provision_block}"

    return content, has_provision_block


# ---------------------------------------------------------------------
# DB read/write
# ---------------------------------------------------------------------

def _fetch_case_row(cur, case_id: int) -> Optional[Dict[str, Any]]:
    cur.execute("""
        SELECT c.case_number, c.ocr_text, c.disposition, c.ministries, c.sections,
               subj.subject_name
        FROM cr_cases c
        LEFT JOIN cr_subjects subj ON subj.subject_id = c.subject
        WHERE c.case_id = %s;
    """, (case_id,))
    row = cur.fetchone()
    if row is None:
        return None
    case_number, ocr_text, disposition, existing_ministry_ids, existing_sections, subject_name = row
    return {
        "case_number": case_number,
        "ocr_text": ocr_text,
        "disposition": disposition,
        "existing_ministry_ids": existing_ministry_ids or [],
        "existing_sections": existing_sections or [],
        "subject_name": subject_name,
    }


_ERROR_MESSAGE_MAX_CHARS = 500


def _write_status(case_id: int, status: str, error: Optional[str]) -> None:
    """Records a non-DONE outcome (FAILED/TRUNCATED/SKIPPED) in its own short transaction, incrementing enrichment_attempts -- never raises, since a caller mid-batch must not lose the rest of the batch over this bookkeeping."""
    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE cr_cases SET
                        enrichment_status = %s,
                        enrichment_error = %s,
                        enrichment_attempts = enrichment_attempts + 1
                    WHERE case_id = %s;
                    """,
                    (status, (error[:_ERROR_MESSAGE_MAX_CHARS] if error else None), case_id),
                )
            conn.commit()
    except Exception:
        logger.exception("[ENRICH] case_id=%s: failed to record enrichment_status=%s", case_id, status)


def find_cases_needing_enrichment(limit: int = 100) -> List[int]:
    """Cases worth (re)enriching: PENDING/FAILED/TRUNCATED, under the retry cap, with ocr_text to work from."""
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT case_id FROM cr_cases
                WHERE enrichment_status IN ('PENDING', 'FAILED', 'TRUNCATED')
                  AND enrichment_attempts < 3
                  AND ocr_text IS NOT NULL
                ORDER BY case_id
                LIMIT %s;
                """,
                (limit,),
            )
            return [row[0] for row in cur.fetchall()]


def enrich_case(case_id: int, provision_block: str = "") -> bool:
    """
    Fetches the already-promoted case on a short-lived connection, makes
    the LLM call with NO database connection held (the call can take up to
    ~120s x 3 attempts), then opens a second short-lived connection to
    resolve lookup ids and write the result. Returns True only when the
    row was actually updated with a successful enrichment; never raises --
    a caller in the middle of a scrape batch must not lose the rest of the
    batch over one enrichment failure.

    `provision_block` is this court's own paragraph-filtered excerpt of the
    OCR text around statutory references (e.g. Supreme Court's
    adapters.supreme_court.extraction.find_provision_paragraphs) — this
    module has no extraction logic of its own (see module docstring), so
    the orchestrator passes it in per-court. Omit it (or pass "") to skip
    provision extraction for this call; provision columns are then simply
    left untouched, same as today when no block was found.
    """
    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                case_row = _fetch_case_row(cur, case_id)
            conn.commit()
    except Exception:
        logger.exception("[ENRICH] case_id=%s: failed to fetch case row", case_id)
        return False

    if case_row is None:
        logger.warning("[ENRICH] case_id=%s: no such case, skipping", case_id)
        return False

    if not case_row["ocr_text"]:
        logger.info("[ENRICH] case_id=%s: no ocr_text, skipping", case_id)
        _write_status(case_id, "SKIPPED", "no ocr_text")
        return False

    user_content, has_provision_block = _build_user_content(case_row, provision_block=provision_block)
    result = call_llm_enrichment(case_row, user_content=user_content)

    if result.status == "NOT_CONFIGURED":
        logger.info("[ENRICH] case_id=%s: Azure OpenAI not configured, leaving enrichment_status=PENDING", case_id)
        return False

    if result.status in ("FAILED", "TRUNCATED"):
        logger.warning("[ENRICH] case_id=%s: %s — %s", case_id, result.status, result.error)
        _write_status(case_id, result.status, result.error)
        return False

    llm_result = result.data

    case_note = llm_result.get("case_note")
    if case_note:
        case_note = re.sub(r"\s+", " ", case_note).strip()
        if "held," not in case_note.lower():
            logger.warning("[ENRICH] case_id=%s: case_note has no 'Held' segment — saving anyway", case_id)
    conclusion = llm_result.get("conclusion")
    llm_subject = (llm_result.get("subject") or "").strip() or None

    industry_ids: List[int] = []
    ministry_ids = list(case_row["existing_ministry_ids"])  # keep regex-derived party-based ministries
    llm_disposition = llm_result.get("disposition_category")
    if llm_disposition not in _VALID_DISPOSITION_CATEGORIES:
        llm_disposition = None
    favouring_party = llm_result.get("favouring_party")
    if favouring_party not in _VALID_FAVOURING_PARTIES:
        favouring_party = None

    raw_provisions = llm_result.get("provisions")
    if not isinstance(raw_provisions, list):
        if raw_provisions is not None:
            logger.warning("[ENRICH] case_id=%s: 'provisions' was not a list (%r), treating as empty", case_id, type(raw_provisions))
        raw_provisions = []

    try:
        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                subject_id = get_or_create_subject(cur, llm_subject) if llm_subject else None

                for raw in (llm_result.get("industries") or [])[:3]:
                    resolved = resolve_industry(raw)
                    if resolved:
                        industry_ids.append(get_or_create_industry(cur, resolved))

                for raw in (llm_result.get("ministries") or [])[:3]:
                    resolved = resolve_ministry(raw)
                    if resolved:
                        new_id = get_or_create_ministry(cur, resolved)
                        if new_id not in ministry_ids:
                            ministry_ids.append(new_id)

                # Dedupe by (statute, year, number) *after* resolve_act(), so
                # two raw statute-name spellings that resolve to the same
                # act aren't double-counted.
                deduped: Dict[Tuple[Any, Any, str], Dict[str, Any]] = {}
                for entry in raw_provisions:
                    number = str(entry.get("number") or "").strip()
                    if not number:
                        continue
                    statute_name, short_code, year = resolve_act(entry.get("statute_name") or "")
                    key = (statute_name, year, number)
                    deduped[key] = {
                        "statute_name": statute_name,
                        "short_code": short_code,
                        "statute_year": year,
                        "section_number": number,
                    }

                set_clauses = [
                    "case_note = COALESCE(%s, case_note)",
                    "conclusion = COALESCE(conclusion, %s)",
                    "subject = COALESCE(subject, %s)",
                    "industries = %s",
                    "ministries = %s",
                    "disposition = COALESCE(disposition, %s)",
                    "favouring_party = COALESCE(%s, favouring_party)",
                    "enrichment_status = 'DONE'",
                    "enrichment_error = NULL",
                    "enriched_at = now()",
                    "enrichment_attempts = enrichment_attempts + 1",
                ]
                params: List[Any] = [case_note, conclusion, subject_id, industry_ids, ministry_ids, llm_disposition, favouring_party]

                # sections/acts are only touched when a provision block was
                # actually sent AND nothing already found them directly
                # (e.g. Madhya Pradesh's own case-status Act lines,
                # adapters/high_courts/mp/promotion.py) -- this is the LLM
                # fallback path for a court whose direct extraction came up
                # empty, never allowed to clobber already-good data with a
                # weaker LLM guess.
                sections: List[int] = []
                if has_provision_block and not case_row["existing_sections"]:
                    acts, sections = resolve_provisions(cur, list(deduped.values()))
                    set_clauses += ["sections = %s", "acts = %s"]
                    params += [sections, acts]

                params.append(case_id)
                cur.execute(f"UPDATE cr_cases SET {', '.join(set_clauses)} WHERE case_id = %s;", tuple(params))
            conn.commit()
    except Exception:
        logger.exception("[ENRICH] case_id=%s: DB write failed after a successful LLM call", case_id)
        return False

    logger.info(
        "[ENRICH] case_id=%s: done — case_note=%s industries=%d ministries=%d sections=%d",
        case_id, "set" if case_note else "unchanged", len(industry_ids), len(ministry_ids), len(sections),
    )
    return True
