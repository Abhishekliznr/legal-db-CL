"""
Regex-only extraction for Supreme Court judgments — an LLM-free alternative
source for the fields sci.gov.in's own results table and its judgments'
formulaic structure already expose deterministically.

Wired into pipeline/promotion.py as of the 2026-09-08 schema rewrite — this
is the non-LLM extraction step (case number/parties/dates/coram/provisions),
with pipeline/llm_enrichment.py filling in the handful of fields regex
genuinely can't get (case_note, industries, ministries, and now provision
relevance — see find_provision_paragraphs() below). This module stays pure
functions only regardless: feed it a raw scraper cell or OCR text, get back
parsed fields; the caller (promotion.py) does the actual DB writes.

Coverage was checked against 102 real judgment PDFs already sitting in
legal-db/scraper-backend/app/SUPREME_COURT_OF_INDIA_SCRAPER/pdf/ (the old
service's own scrape output) — not written from assumptions about judgment
formatting. Two of the fields the user originally proposed sourcing from a
heading ("Factual Matrix", "Conclusion") turned out to appear as literal
section headings in only ~5% and ~1% of sampled judgments respectively
(the phrases exist inline far more often — "the factual matrix of the case
is..." — but essentially never as a standalone ALL-CAPS heading the way
"J U D G M E N T" reliably is). extract_facts()/extract_conclusion() below
are kept because they DO work for the minority of judgments that use those
headings, but callers must treat a None return as "this judgment doesn't
have that heading", not as an extraction failure — there is no full-coverage
substitute for either field without the LLM.

Similarly, "Page N of M" footer validation (validate_page_count) only found
a matching string in 16/102 (16%) of sampled PDFs, with no reliable
correlation to document age or presence of a neutral citation — treat a
None return as "can't validate this document", never as a failed check.

Fields deliberately NOT covered here, with reasoning:
- Industry: the old scraper-backend's reference implementation
  (SUPREME_COURT_OF_INDIA_SCRAPER/pdf_metadata_extractor.py) keyword-scans
  the ENTIRE judgment text against ~44 industry categories. That is a weak
  signal for this specific field — a single incidental mention ("the
  accused works at a bank") mistags an unrelated case. Left to the LLM's
  `subjects` field, which reasons about what the case is actually about
  rather than counting keyword hits.
- Document type (doc_type_enum: CaseLaw/BusinessPolicy): not an extraction
  problem at all for this adapter. Every sci.gov.in record is a court
  judgment/order by construction, so it's always 'CaseLaw' — set that as a
  constant at the call site, same as db/schema.sql's own column default.
"""

import re
from typing import Dict, List, Optional

from normalization.acts import resolve_act
from normalization.judges import clean_judge_name
from normalization.paragraphs import split_into_paragraphs

# ---------------------------------------------------------------------
# Table-cell fields (sci.gov.in results table — no OCR involved)
# ---------------------------------------------------------------------

_VS_SPLIT = re.compile(r"\s+(?:Vs\.?|Versus)\s+", re.IGNORECASE)


def parse_party_names(party_name_raw: Optional[str]) -> Dict[str, Optional[str]]:
    """
    Splits the results table's "Petitioner / Respondent" cell, e.g.
    "Ram Kumar Vs State of U.P.", into {"petitioner": ..., "respondent": ...}.
    Returns both as None if no "Vs"/"Versus" separator is found (rare, but
    seen on some connected-matter rows that list only one side).
    """
    if not party_name_raw or not party_name_raw.strip():
        return {"petitioner": None, "respondent": None}

    parts = _VS_SPLIT.split(party_name_raw.strip(), maxsplit=1)
    if len(parts) != 2:
        return {"petitioner": party_name_raw.strip(), "respondent": None}

    petitioner, respondent = (p.strip(" .") for p in parts)
    return {"petitioner": petitioner or None, "respondent": respondent or None}


# The advocate cell is far less structured than the party cell -- observed
# real values are hyphen-separated ("Petitioner Adv Name - Respondent Adv
# Name") but the respondent side is frequently just missing (no trailing
# "- ..." at all) rather than present-but-blank, matching the user's own
# observation that petitioner-side advocate names are far more often on
# record than respondent-side ones. NOT verified against a large real
# sample of this specific column's values (adapters/supreme_court/adapter.py
# only ever stored this raw, unparsed, into extra["advocate_raw"] -- see
# its module docstring) -- treat this split as a reasonable first pass to
# be corrected against real values once a larger sample is on hand.
_ADVOCATE_SPLIT = re.compile(r"\s*-\s*")


def parse_advocates(advocate_raw: Optional[str]) -> Dict[str, Optional[str]]:
    """
    Splits the results table's "Petitioner/Respondent Advocate" cell by "-".
    A single un-hyphenated value is treated as the petitioner's advocate
    (the side that's almost always present), never the respondent's.
    """
    if not advocate_raw or not advocate_raw.strip():
        return {"petitioner_advocate": None, "respondent_advocate": None}

    parts = [p.strip(" .") for p in _ADVOCATE_SPLIT.split(advocate_raw.strip()) if p.strip(" .")]
    if not parts:
        return {"petitioner_advocate": None, "respondent_advocate": None}
    if len(parts) == 1:
        return {"petitioner_advocate": parts[0], "respondent_advocate": None}
    return {"petitioner_advocate": parts[0], "respondent_advocate": " - ".join(parts[1:])}


# The "Judgment" cell packs three things into one string, e.g.
# "06-01-2026(English) 2026 INSC 15(English)" -- confirmed live 2026-09-06
# against real sci.gov.in rows (adapters/supreme_court/adapter.py's own
# _parse_judgment_cell already covers date + citation; this extends that
# with the language label the date-parsing code currently discards).
_JUDGMENT_DATE_PATTERN = re.compile(r"(\d{1,2}[-/]\d{1,2}[-/]\d{4})\s*\(([A-Za-z]+)\)")
_JUDGMENT_CITATION_PATTERN = re.compile(r"\b(\d{4}\s*INSC\s*\d+)\s*\(([A-Za-z]+)\)", re.IGNORECASE)


def parse_judgment_cell(raw: Optional[str]) -> Dict[str, Optional[str]]:
    """
    Parses the combined "Judgment" cell into date, neutral citation, and
    language. Language is read off whichever of the two "(English)"-style
    labels is present -- normally both agree; if only one link is present
    (a citation not yet assigned, or a date-only row) that label is used.
    NOT re-verified live since the adapter's own date/citation parsing was
    confirmed 2026-09-06 -- this only adds a capture group for the
    parenthetical language label already visible in that same confirmed
    real value, no new selector or page interaction involved.
    """
    if not raw:
        return {"decision_date_raw": None, "neutral_citation_raw": None, "language": None}

    date_match = _JUDGMENT_DATE_PATTERN.search(raw)
    citation_match = _JUDGMENT_CITATION_PATTERN.search(raw)

    language = None
    if citation_match:
        language = citation_match.group(2).title()
    elif date_match:
        language = date_match.group(2).title()

    return {
        "decision_date_raw": date_match.group(1) if date_match else None,
        "neutral_citation_raw": citation_match.group(1) if citation_match else None,
        "language": language,
    }


# The "Bench" cell concatenates every judge on the coram with no separator
# other than each name restarting with "HON'BLE" (or, on a one-judge bench,
# nothing to split at all), e.g. "HON'BLE MRS. JUSTICE B.V. NAGARATHNA
# HON'BLE MR. JUSTICE SATISH CHANDRA SHARMA" -- confirmed against real
# sci.gov.in rows (see the table-cell fields demoed in
# tests/manual/regex_extraction_demo.py). Splitting on a lookahead for
# "HON'BLE" keeps the delimiter word attached to the segment that follows
# it, which normalization.judges.clean_judge_name already strips along with
# every other honorific.
_BENCH_SPLIT = re.compile(r"(?=HON'?BLE)", re.IGNORECASE)


def parse_bench(bench_raw: Optional[str]) -> List[str]:
    """Splits the results table's "Bench" cell into cleaned judge names (normalization.judges.clean_judge_name), in bench order."""
    if not bench_raw or not bench_raw.strip():
        return []
    segments = _BENCH_SPLIT.split(bench_raw.strip())
    names = [clean_judge_name(segment) for segment in segments]
    # "HON'BLE THE CHIEF JUSTICE" (the CJI referred to by title only, no
    # personal name in this cell) cleans down to a bare leftover "THE" --
    # clean_judge_name's honorific list doesn't include "THE" itself, since
    # in every other position it's part of a real name's surrounding text,
    # not the whole result. A stray 1-2 word non-name residue like that is
    # worse to store as a fake judge than to drop.
    return [name for name in names if name and len(name) > 3 and name not in {"THE", "AND"}]


# ---------------------------------------------------------------------
# OCR-text fields
# ---------------------------------------------------------------------

# "IN THE SUPREME COURT OF INDIA" is always immediately followed by a
# "<TYPE> JURISDICTION" heading -- confirmed present in 100/102 (98%) of
# sampled real judgments, always within the first ~300 characters. This is
# a genuinely reliable anchor, unlike the Factual Matrix/Conclusion
# headings below.
_JURISDICTION_PATTERN = re.compile(
    r"\b([A-Z][A-Z\s]{2,40}?)\s+(?:APPELLATE|APPEAL|ORIGINAL|ORDINARY\s+ORIGINAL|EXTRAORDINARY)?\s*JURISDICTION\b",
    re.IGNORECASE,
)


def extract_subject_from_jurisdiction(ocr_text: str) -> Optional[str]:
    """
    Returns a coarse subject tag ("Civil", "Criminal", "Original", ...)
    read off the "<TYPE> JURISDICTION" heading under the court name, or
    None if the heading isn't present in the expected header region. Only
    searches the first 1000 characters -- the same phrase can reappear
    later in the body when the judgment quotes another court's order.
    """
    if not ocr_text:
        return None

    header = ocr_text[:1000]
    match = _JURISDICTION_PATTERN.search(header)
    if not match:
        return None

    prefix = re.sub(r"\s+", " ", match.group(1)).strip().upper()
    # The prefix captures everything back to the previous line break, which
    # can include "IN THE SUPREME COURT OF INDIA" itself on a one-line
    # header -- keep only the last word-ish token (CIVIL/CRIMINAL/etc.).
    tokens = prefix.split()
    subject_word = tokens[-1] if tokens else prefix
    return subject_word.title() if subject_word else None


# "J U D G M E N T" (spaced out like this in almost every real judgment) or
# a plain "O R D E R" marks the start of the actual opinion, after the
# case-number/parties/coram header block. There's no further heading split
# inside that block for facts vs. reasoning vs. conclusion (see
# extract_facts/extract_conclusion above) -- this is the whole body as one
# piece, which is what the cases.judgement field is: not a distinct
# semantic section, just "the opinion text, header stripped off".
_JUDGMENT_BODY_HEADING = re.compile(
    r"\n\s*(?:J\s*U\s*D\s*G\s*M\s*E\s*N\s*T|O\s*R\s*D\s*E\s*R)\s*\n", re.IGNORECASE
)


def extract_judgment_body(ocr_text: str) -> Optional[str]:
    """Everything after the "J U D G M E N T"/"O R D E R" heading, or None if that heading isn't found (falls back to the full ocr_text at the call site if needed)."""
    if not ocr_text:
        return None
    match = _JUDGMENT_BODY_HEADING.search(ocr_text)
    if not match:
        return None
    body = ocr_text[match.end():].strip()
    return body or None


# Section/Article/Rule/Order references. Deliberately does NOT try to
# resolve which act a bare "Section 302" belongs to when no act name is
# nearby -- that ambiguity is exactly why the old scraper-backend's
# equivalent function returned untyped text snippets instead of committing
# to a specific statute, and why pipeline/llm_enrichment.py's LLM prompt asks
# for provisions as {statute_name, section_number} pairs rather than trying
# to regex it. This is a genuine regex-vs-LLM tradeoff, not a bug: keep
# provisions with a resolved act name, drop bare section numbers with no
# nearby act rather than guessing.
#
# The leading \b is load-bearing, not decorative -- without it the trigger
# alternation matches as a bare substring, confirmed via real false
# positives: "Rs. 5000" matches trigger "s." (inside "R" + "s."), and "a
# mental disorder 5 years later" matches trigger "order" (inside
# "dis"+"order"). Both are extremely common in Indian judgments (rupee
# amounts, medical/psychological narration) and were silently inflating the
# candidate pool for both extract_provisions() and find_provision_paragraphs()
# below before this was caught (2026-09-09).
_PROVISION_PATTERN = re.compile(
    r"""
    \b(?P<trigger>u/s\.?|under\s+section|section|sections|sec\.|s\.|article|articles|rule|rules|order)\s*
    (?P<number>[0-9]+[A-Za-z]?(?:\s*\([0-9A-Za-z]+\))*)
    (?:\s*(?:of|,)?\s*(?:the\s+)?(?P<act>[A-Z][A-Za-z.,&\s]{2,60}?\b(?:Act|Code|Rules|Constitution|Sanhita|Adhiniyam)\b|IPC|CrPC|CPC|NDPS|POCSO|IEA))?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Routes the matched trigger word to one of the three provision buckets the
# new flattened `cases` schema keeps separate (cases.sections/rules/orders,
# each an array of ids into its own lookup table, all pointing back to
# `acts`). "Article" (Constitution references, e.g. "Article 226") has no
# bucket of its own in that schema -- treated as a section, since it plays
# the identical structural role (a numbered provision within one act/the
# Constitution), just under a different word.
def _provision_type(trigger: str) -> str:
    trigger_lower = trigger.lower().strip(". ")
    if trigger_lower in ("rule", "rules"):
        return "rule"
    if trigger_lower == "order":
        return "order"
    return "section"  # section/sections/sec./s./u/s/under section/article/articles


# A judgment refers back to an act it already named earlier as "the Act" /
# "the said Rules" / "the above said Act" -- this pattern's own capture
# happily grabs those anaphoric references too, since they're still
# "[The ]<word> Act/Rules"-shaped. Resolving one to a fake statute named
# literally "The Act" or "Said Rules" (title-cased by resolve_act's own
# fallback, since it has no canonical match either) is worse than dropping
# it: it looks like a real, specific statute when it identifies nothing
# without reading back to find whatever antecedent it refers to -- out of
# regex's reach. Found via real output in the verification sample.
_GENERIC_ACT_REFERENCE = re.compile(
    r"^(?:the\s+|said\s+|aforesaid\s+|above[\s-]*(?:said)?\s+)*(?:act|code|rules?|regulations?)\.?$",
    re.IGNORECASE,
)


def extract_provisions(ocr_text: str, max_results: int = 30) -> List[Dict[str, Optional[str]]]:
    """
    Finds "Section N of X Act"-shaped references and resolves the act name
    via normalization.acts.resolve_act(). Entries with no act name found
    nearby, or where the "act name" found is just a generic anaphoric
    reference ("the Act", "said Rules") rather than an actual statute name,
    are skipped rather than emitted with a null/fabricated-looking statute.

    Each result carries a "provision_type" of "section", "rule", or "order"
    (see _provision_type) so a caller populating the flattened `cases`
    schema (separate sections/rules/orders id arrays, all resolving back to
    `acts`) can route it to the right bucket without re-deriving the type
    from the number/act text.
    """
    if not ocr_text:
        return []

    results = []
    seen = set()
    for match in _PROVISION_PATTERN.finditer(ocr_text):
        act_raw = match.group("act")
        number = match.group("number")
        if not act_raw or not number:
            continue

        act_raw = re.sub(r"\s+", " ", act_raw).strip()
        if _GENERIC_ACT_REFERENCE.match(act_raw):
            continue

        statute_name, short_code, year = resolve_act(act_raw)
        provision_type = _provision_type(match.group("trigger"))
        key = (statute_name, provision_type, number)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "statute_name": statute_name,
            "short_code": short_code,
            "statute_year": year,
            "section_number": number.strip(),
            "provision_type": provision_type,
        })
        if len(results) >= max_results:
            break

    return results


def find_provision_paragraphs(
    ocr_text: str,
    max_paragraphs: int = 15,
    max_chars: int = 4000,
    context_chars: int = 200,
) -> str:
    """
    Finds every paragraph (or, when the document isn't reliably numbered, a
    fixed-width window) containing at least one _PROVISION_PATTERN trigger,
    and returns them concatenated as a single block — the filtered context
    pipeline/llm_enrichment.py feeds its LLM call for provision extraction,
    instead of the full OCR text or the head+tail excerpt used for
    case_note/conclusion. Provisions are typically cited in the reasoning
    section in the *middle* of a judgment, which that head+tail excerpt
    deliberately excludes (see llm_enrichment.py's own docstring) — this
    function exists specifically to still reach that text, without paying
    for the full OCR text on every call.

    extract_provisions() above only emits a result when an act name is
    found immediately adjacent to a match; this function makes no such
    requirement — it only needs a trigger word (section/rule/order/article)
    to flag a paragraph as worth sending to the LLM, which then resolves
    the act from the full paragraph's context, not just an 80-char window.

    Returns "" if the document has no provision references anywhere
    (cheap short-circuit) so callers can skip adding an empty section to
    the prompt.
    """
    if not ocr_text or not _PROVISION_PATTERN.search(ocr_text):
        return ""

    chunks: List[str] = []
    total_chars = 0

    paragraphs = split_into_paragraphs(ocr_text)
    if paragraphs:
        for para_number, para_text in paragraphs:
            if not _PROVISION_PATTERN.search(para_text):
                continue
            chunk = f"[Para {para_number}] {para_text}"
            chunks.append(chunk)
            total_chars += len(chunk)
            if len(chunks) >= max_paragraphs or total_chars >= max_chars:
                break
    else:
        # No reliable paragraph numbering (common for short Orders) --
        # fall back to a fixed-context window around each match, same
        # mechanics as pipeline/citator.py's find_citation_candidates(),
        # merging overlapping spans so two nearby triggers don't duplicate
        # the same sentence twice.
        spans: List[List[int]] = []
        for match in _PROVISION_PATTERN.finditer(ocr_text):
            start = max(0, match.start() - context_chars)
            end = min(len(ocr_text), match.end() + context_chars)
            if spans and start <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], end)
            else:
                spans.append([start, end])

        for start, end in spans:
            chunk = re.sub(r"\s+", " ", ocr_text[start:end]).strip()
            if not chunk:
                continue
            chunks.append(chunk)
            total_chars += len(chunk)
            if len(chunks) >= max_paragraphs or total_chars >= max_chars:
                break

    return "\n\n".join(chunks)


# Disposition keyword bank, checked in priority order against real
# sentences within the tail (see _tail_windows/_split_sentences below).
# Order matters: "partly allowed" must be checked before the plain
# "allowed"/"dismissed" patterns since a judgment that is partly allowed
# will also usually contain the word "allowed" on its own.
#
# Verified against all 102 real judgment PDFs in the verification sample
# (not just an initial 30 -- several real bugs below were only found by
# running against the full set): 98/102 (96%) return a category, spot-
# checked by eye across the whole set, not just presence/absence. The 4
# that return None are either genuinely not-yet-disposed procedural orders
# (referred to a larger bench; listed for further hearing -- correctly
# None, not a miss) or one case (a sentence reduction with no "allowed"/
# "dismissed" wording at all) that's a real, hard-to-resolve ambiguity.
# One known residual false-positive CLASS, not fixed: a judgment whose
# final pages are a numeric annexure/schedule (account balances, a table
# of amounts) can coincidentally contain the bare word "allowed" with no
# real disposal meaning -- rare in this sample (~1-2 documents) but a real
# limitation, not something a keyword bank can distinguish from context.
# Treat "Other"/None as a signal to route the row to needs_review rather
# than as a settled classification, and treat a returned category as a
# strong-but-not-certain result at volume.
_DISPOSITION_PATTERNS = [
    (re.compile(r"\bpartl?y\s+allowed\b", re.IGNORECASE), "Partly Allowed"),
    # Word order sometimes reverses ("we are inclined to allow this appeal
    # partly" -- a real closing sentence, not "partly allowed") -- caught
    # separately since the direct-adjacency pattern above won't match it.
    (re.compile(r"\ballow\w*\b.{0,25}\bpartly\b", re.IGNORECASE), "Partly Allowed"),
    # Subject-agnostic, with a short non-punctuation-sensitive gap rather
    # than a fixed adverb list -- real tails contained "is, accordingly,
    # disposed of" and "same is dismissed" (verb not directly adjacent to
    # a named subject, extra commas an exact adverb list didn't cover; see
    # conversation for both real examples this was tuned against).
    (re.compile(r"\b(?:is|are|stands?)\b.{0,25}\ballowed\b", re.IGNORECASE), "Allowed"),
    (re.compile(r"\ballowed\s+(?:as\s+above|in\s+the\s+aforesaid\s+terms|to\s+the\s+above\s+extent|in\s+part)\b", re.IGNORECASE), "Allowed"),
    (re.compile(r"\b(?:is|are|stands?)\b.{0,25}\bdismissed\b", re.IGNORECASE), "Dismissed"),
    (re.compile(r"\bwithdrawn\b", re.IGNORECASE), "Withdrawn"),
    (re.compile(r"\bremand(?:ed)?\s+(?:back\s+)?to\b", re.IGNORECASE), "Remanded"),
    (re.compile(r"\bquashed\b", re.IGNORECASE), "Quashed"),
    (re.compile(r"\bset\s+aside\b", re.IGNORECASE), "Set Aside"),
    (re.compile(r"\b(?:is|are|stands?)\b.{0,25}\bdisposed[\s-]+of\b", re.IGNORECASE), "Disposed"),
]

# "Pending application(s)/I.A.(s), if any, stand disposed of" is near-
# universal boilerplate about ANCILLARY applications, present in almost
# every judgment's final paragraph regardless of what actually happened to
# the appeal/petition itself -- it is not a meaningful "Disposed" result
# for the case as a whole. Found via a real judgment in the verification
# sample where this boilerplate sentence sat one paragraph after the
# judgment's real disposal ("...this appeal succeeds and is hereby
# allowed... is hereby quashed.") and, being closer to the signature
# block, was matched first -- masking the real, more specific outcome.
_ANCILLARY_APPLICATION_PATTERN = re.compile(
    r"\b(?:pending\s+)?(?:application|I\.?A\.?)s?\b(?!.*\b(?:appeal|petition|writ)\b)",
    re.IGNORECASE,
)

# A Supreme Court judgment almost always refers to ITSELF as "this Court"/
# "We" and names the forum under challenge explicitly ("the High Court",
# "the trial Court") when recounting what THAT forum did earlier in the
# case's history, in ACTIVE voice ("the High Court... allowed the Revision,
# set aside the order..." -- found as a real false positive in the
# verification sample, a short procedural referral order whose only
# disposal-shaped language was a facts paragraph like this one). Excluding
# on the forum name alone is too broad, though: it's just as common for
# THIS judgment's own real disposal to read "the judgment of the High
# Court is set aside" or "...passed by the Allahabad High Court in
# Application ... No. 35311 of 2023 is also set aside" -- both real cases
# found in the same verification pass, neither of which should be
# excluded. The distinguishing shape is active vs. passive: a passive
# auxiliary ("is"/"are"/"was"/"stands") SOMEWHERE between the forum name
# and the verb signals "this court is now acting ON the lower court's
# order" (legitimate); its total absence signals "the lower court itself
# did something" (facts narration, exclude). Checked across the whole
# matched span rather than immediately after the forum name specifically,
# since a real modifying phrase ("in Application ... No. 35311 of 2023")
# can sit between the forum name and the passive auxiliary. Imperfect -- a
# sentence that both narrates AND states this judgment's outcome in the
# same short window can still mismatch either way -- but it resolves every
# real case found so far without trading one for another.
_LOWER_FORUM_SUBJECT_PATTERN = re.compile(
    r"\b(?:High\s+Court|trial\s+court|sessions?\s+court|district\s+court|magistrate|tribunal|forum\s+below)\b"
    r".{0,80}\b(?:allowed|dismissed|set\s+aside|quashed|disposed)\b",
    re.IGNORECASE,
)
_PASSIVE_AUXILIARY_PATTERN = re.compile(r"\b(?:is|are|was|were|stands?|stood)\b", re.IGNORECASE)


def _is_lower_forum_narration(sentence: str) -> bool:
    match = _LOWER_FORUM_SUBJECT_PATTERN.search(sentence)
    if not match:
        return False
    return not _PASSIVE_AUXILIARY_PATTERN.search(match.group(0))

# Short titlecase/uppercase tokens that precede a "." without actually
# ending a sentence ("SLP (CIVIL) NO. 6779", "Ors.", "J."). A naive
# "split on every period" sentence-boundary regex breaks on these -- found
# via a real judgment in the verification sample where it truncated the
# real sentence mid-citation and, worse, then matched a keyword bank
# pattern against the WRONG half (a facts-recital fragment describing what
# a lower court did, not this judgment's own disposal).
_SENTENCE_ABBREVIATIONS = {
    "no", "nos", "mr", "mrs", "ms", "dr", "j", "jj", "ors", "smt", "sri",
    "vs", "govt", "anr", "anrs", "etc", "co", "ltd", "rs", "hon'ble",
    "art", "sec", "sh", "shri",
}


def _split_sentences(text: str) -> List[str]:
    """
    Sentence splitter that treats a period as a real sentence boundary only
    when it's followed by whitespace + a capital letter (optionally with a
    bare paragraph number in between -- some PDFs' text layer emits a
    numbered paragraph's number on its own line with no "." or ")" after
    it, e.g. "...become infructuous. 3 The present Appeal is allowed...",
    found as a real case in the verification sample where it otherwise
    merged two unrelated sentences into one, which then wrongly tripped
    the lower-forum-narration check above) or end of text -- AND the word
    immediately before the period isn't a known abbreviation -- "SLP
    (CIVIL) NO. 6779" and "Ors. thereafter" stay one sentence instead of
    splitting at "NO."/"Ors.".
    """
    fragments = re.split(r"\.(?=\s+(?:\d{1,3}\s+)?[A-Z]|\s*$)", text)
    sentences: List[str] = []
    buffer = ""
    for fragment in fragments:
        buffer = f"{buffer}. {fragment}".strip() if buffer else fragment.strip()
        last_word_match = re.search(r"([A-Za-z']+)\s*$", buffer)
        last_word = last_word_match.group(1).lower() if last_word_match else ""
        if last_word in _SENTENCE_ABBREVIATIONS:
            continue  # not a real sentence end -- keep accumulating
        sentences.append(buffer.strip() + ".")
        buffer = ""
    if buffer.strip():
        sentences.append(buffer.strip())
    return [s for s in sentences if s]


def _tail_windows(ocr_text: str) -> List[str]:
    """
    Progressively larger windows onto the end of the judgment, smallest
    first. Checking the smallest window first is deliberate: the real
    operative disposal is almost always in the last 1-2 numbered
    paragraphs, and a wide window risks matching disposal-shaped language
    from a facts recital instead (a real failure mode found in the
    verification sample -- see extract_disposition's docstring).

    The character-based fallback windows are ONLY used when no numbered-
    paragraph structure was found at all -- NOT appended unconditionally
    after the paragraph windows. A real judgment in the verification
    sample (a short procedural referral: "we direct the Registry to place
    the papers before the CJI for passing appropriate orders" -- correctly
    NOT disposed of yet) had disposal-shaped language ONLY in its early
    facts paragraphs recounting what a lower court had done. Once every
    paragraph window (up to all of them) came back empty, falling through
    to a raw character window read past the paragraph structure entirely
    and wrongly matched that facts language. When paragraphs exist, "no
    match in any of them" is itself a meaningful result -- most likely an
    interim/procedural order with no final disposal yet -- not a signal to
    keep searching further back.
    """
    paragraphs = split_into_paragraphs(ocr_text)
    if paragraphs:
        windows = [" ".join(text for _, text in paragraphs[-count:]) for count in (2, 4, 8)]
    else:
        windows = [ocr_text[-chars:] for chars in (800, 1500, 2500)]
    # Collapse line wraps -- a PDF's own word-wrapping ("...inclined to
    # allow\nthis appeal partly.") would otherwise break windowed keyword
    # patterns that scan a few words ahead on the same "line".
    return [re.sub(r"\s+", " ", w).strip() for w in windows]


def extract_disposition(ocr_text: str) -> Dict[str, Optional[str]]:
    """
    Returns {"disposition_raw": verbatim operative sentence or None,
    "disposition_category": one of documents.disposition_category's real
    enum values, or None if no pattern matched (route to needs_review, do
    NOT default to "Other" silently -- "Other" should mean "matched
    something recognizable but none of the named categories fit", not
    "we couldn't find anything").

    disposition_raw and disposition_category always come from the SAME
    sentence -- earlier versions of this function matched a keyword
    pattern against the whole tail for the category, and separately
    pattern-matched for the raw sentence, which could legitimately settle
    on two different, unrelated sentences (e.g. category correctly
    "Allowed" from one sentence, raw text a nearby "pending applications
    stand disposed of" boilerplate line instead of the actual relief
    sentence). Now: split into real sentences (see _split_sentences,
    abbreviation-aware), scan from the END backwards, and take the first
    sentence that matches any category pattern -- both fields point at
    that one sentence.
    """
    if not ocr_text:
        return {"disposition_raw": None, "disposition_category": None}

    for window in _tail_windows(ocr_text):
        sentences = _split_sentences(window)
        for sentence in reversed(sentences):
            for pattern, label in _DISPOSITION_PATTERNS:
                if not pattern.search(sentence):
                    continue
                if label == "Disposed" and _ANCILLARY_APPLICATION_PATTERN.search(sentence):
                    continue  # boilerplate about pending IAs, not the case itself -- keep scanning
                if _is_lower_forum_narration(sentence):
                    continue  # describes what the High Court/trial court did, not this judgment's own order
                return {"disposition_raw": sentence, "disposition_category": label}

    return {"disposition_raw": None, "disposition_category": None}


# ---------------------------------------------------------------------
# Low-coverage, best-effort heading extractors — see module docstring.
# Both return None far more often than not on real judgments; that is
# expected, not a bug to fix by loosening the pattern.
# ---------------------------------------------------------------------

_FACTS_HEADING_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:FACTUAL\s+MATRIX|FACTS?(?:\s+OF\s+THE\s+CASE)?|BRIEF\s+FACTS)\s*:?\s*\n",
    re.IGNORECASE,
)
_CONCLUSION_HEADING_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:CONCLUSION|ANALYSIS\s+AND\s+CONCLUSION?S?)\s*:?\s*\n",
    re.IGNORECASE,
)
# Any of these acts as a stop boundary once a heading-anchored section starts.
_SECTION_STOP_PATTERN = re.compile(
    r"\n\s*(?:[A-Z][A-Z\s]{3,40}:?\s*\n|\d{1,3}\s*[.\)]\s+[A-Z])",
)


def _extract_after_heading(ocr_text: str, heading_pattern: "re.Pattern", max_chars: int = 3000) -> Optional[str]:
    match = heading_pattern.search(ocr_text)
    if not match:
        return None
    remainder = ocr_text[match.end():match.end() + max_chars]
    stop = _SECTION_STOP_PATTERN.search(remainder)
    section_text = remainder[: stop.start()] if stop else remainder
    section_text = re.sub(r"\s+", " ", section_text).strip()
    return section_text or None


def extract_facts(ocr_text: str) -> Optional[str]:
    """Best-effort: only returns non-None when a literal Facts/Factual Matrix heading exists (~5% of sampled judgments)."""
    if not ocr_text:
        return None
    return _extract_after_heading(ocr_text, _FACTS_HEADING_PATTERN)


def extract_conclusion(ocr_text: str) -> Optional[str]:
    """Best-effort: only returns non-None when a literal Conclusion heading exists (rare — most judgments use the word inline, not as a heading)."""
    if not ocr_text:
        return None
    return _extract_after_heading(ocr_text, _CONCLUSION_HEADING_PATTERN)


# "Page N of M" -- found in only 16/102 (16%) of sampled real PDFs, with no
# reliable correlation to document era. A None return means "this document
# has no such marker", which is the common case, not a failure.
_PAGE_MARKER_PATTERN = re.compile(r"\bPage\s+(\d+)\s+of\s+(\d+)\b", re.IGNORECASE)


def validate_page_count(ocr_text: str, actual_page_count: int) -> Optional[bool]:
    """
    Cross-checks the highest "Page N of M" marker found in the text against
    the PDF's real page count (from PyMuPDF's len(document)). Returns None
    when no marker is present at all -- most documents -- rather than
    False, since absence isn't evidence of a mismatch.
    """
    if not ocr_text:
        return None
    matches = _PAGE_MARKER_PATTERN.findall(ocr_text)
    if not matches:
        return None
    declared_totals = {int(total) for _current, total in matches}
    if len(declared_totals) > 1:
        return False  # inconsistent "of M" values across the document's own markers
    declared_total = declared_totals.pop()
    return declared_total == actual_page_count


# ---------------------------------------------------------------------
# Ministry — scoped to a single already-identified party name, never the
# whole document (see module docstring for why whole-document scanning is
# a false-positive trap: a judgment merely CITING a past "Union of India,
# Ministry of Railways" case would otherwise get mistagged even when no
# ministry is a party to the current case at all).
# ---------------------------------------------------------------------

# Reused as reference data (names only, no matching logic) from the old
# scraper-backend's MINISTRIES_LIST -- same treatment normalization/acts.py
# already gives CANONICAL_STATUTES: a lookup table has no logic worth
# rewriting, only the data is carried over. Trimmed to current-name entries
# relevant to a party-name match (dropped historical/foreign entries like
# "National Parliament of Bangladesh" that clearly don't belong here).
KNOWN_MINISTRIES = [
    "Ministry of Agriculture and Farmers Welfare", "Ministry of Ayush",
    "Ministry of Chemicals and Fertilizers", "Ministry of Civil Aviation",
    "Ministry of Coal", "Ministry of Commerce and Industry",
    "Ministry of Communications", "Ministry of Corporate Affairs",
    "Ministry of Culture", "Ministry of Defence", "Ministry of Education",
    "Ministry of Electronics and Information Technology",
    "Ministry of Environment, Forest and Climate Change",
    "Ministry of External Affairs", "Ministry of Finance",
    "Ministry of Fisheries, Animal Husbandry and Dairying",
    "Ministry of Food Processing Industries",
    "Ministry of Health and Family Welfare", "Ministry of Home Affairs",
    "Ministry of Housing and Urban Affairs",
    "Ministry of Information and Broadcasting", "Ministry of Jal Shakti",
    "Ministry of Labour and Employment", "Ministry of Law and Justice",
    "Ministry of Micro, Small and Medium Enterprises", "Ministry of Mines",
    "Ministry of Minority Affairs",
    "Ministry of Panchayati Raj",
    "Ministry of Parliamentary Affairs",
    "Ministry of Personnel, Public Grievances and Pensions",
    "Ministry of Petroleum and Natural Gas", "Ministry of Power",
    "Ministry of Railways", "Ministry of Road Transport and Highways",
    "Ministry of Rural Development", "Ministry of Science and Technology",
    "Ministry of Shipping", "Ministry of Skill Development and Entrepreneurship",
    "Ministry of Social Justice and Empowerment",
    "Ministry of Statistics and Programme Implementation",
    "Ministry of Steel", "Ministry of Textiles",
    "Ministry of Tourism", "Ministry of Tribal Affairs",
    "Ministry of Urban Development", "Ministry of Water Resources",
    "Ministry of Women and Child Development",
    "Cabinet Division", "Reserve Bank of India",
    "Securities and Exchange Board of India",
    "Telecom Regulatory Authority of India",
    "Election Commission of India",
]

_MINISTRY_LOOKUP = {m.lower(): m for m in KNOWN_MINISTRIES}


def find_ministry_in_party_name(party_name: Optional[str]) -> Optional[str]:
    """
    Returns the canonical ministry name if `party_name` (a single already-
    parsed party string, e.g. from parse_party_names()) names one exactly,
    or None. Deliberately exact/substring match against a closed list
    rather than a whole-document keyword scan -- see module docstring.
    """
    if not party_name:
        return None
    lowered = party_name.lower()
    for ministry_lower, canonical in _MINISTRY_LOOKUP.items():
        if ministry_lower in lowered:
            return canonical
    return None


def resolve_ministry(raw_name: str) -> Optional[str]:
    """
    Case-insensitive EXACT match against KNOWN_MINISTRIES, for validating
    pipeline/llm_enrichment.py's LLM output (which is prompted with this
    same list as its only allowed subject-matter-ministry values) --
    deliberately not substring matching like find_ministry_in_party_name
    above, since the LLM is expected to return the canonical name verbatim,
    not a longer string a ministry name happens to appear inside. Returns
    None for anything outside the closed list, dropped rather than stored
    as a fabricated-looking new ministry.
    """
    if not raw_name:
        return None
    return _MINISTRY_LOOKUP.get(raw_name.strip().lower())
