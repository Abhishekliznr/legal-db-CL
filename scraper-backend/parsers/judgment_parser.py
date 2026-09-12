"""
Deterministic judgment parser — structures raw OCR text into the
StructuredJudgment representation (parsers/schema.py) WITHOUT any LLM call.

This is the "OCR -> Deterministic Judgment Parser -> Structured JSON" stage
of the pipeline: pipeline/promotion.py calls parse_judgment() right after
computing cases.judgement (regex_extraction.extract_judgment_body) and
stores the result in cases.structured_content (JSONB). LLM enrichment
(pipeline/llm_enrichment.py) runs afterward, on a separate excerpt of the
same ocr_text, and never touches or replaces anything this module produces
— see llm_enrichment.py's own docstring for that boundary.

Reuses every existing regex-based building block rather than re-deriving
its own: normalization.paragraphs (numbered-paragraph boundaries),
normalization.ocr_artifacts (signature blocks/page numbers/TOC lines),
pipeline.regex_extraction (disposition, provisions, bench parsing), and
pipeline.citator (citation candidates). No new statute/citation/disposition
logic lives here — only structural assembly of what those modules already
detect deterministically.

Deliberately conservative, matching every other extractor in this pipeline
(see regex_extraction.py's own module docstring): a structural signal only
gets tagged when a real, well-attested pattern is found; anything not
confidently structured still comes through as plain "paragraph"/
"front_matter" sections, never dropped, never paraphrased. A failure
anywhere in _populate() below is caught and downgraded to a single
unstructured section holding the verbatim OCR text plus a warning — this
function must never destroy the caller's source text, only fail to
structure it (see tests/unit/parsers/test_judgment_parser.py's malformed-
input cases).
"""

import re
from bisect import bisect_right
from typing import Dict, List, Optional, Set, Tuple

from normalization.ocr_artifacts import ArtifactSpan, find_artifact_spans
from normalization.paragraphs import paragraph_start_matches, split_into_paragraphs_with_offsets
from parsers.schema import (
    Citation,
    FinalOrder,
    Footnote,
    RemovedArtifact,
    Section,
    SourceSpan,
    StatutoryReference,
    StructuredJudgment,
)
from pipeline import citator
from pipeline import regex_extraction as rx

# A standalone heading line marking a known judgment-structure section --
# scanned ONLY within the "preamble" (text before the first numbered
# paragraph, or the whole document when no reliable numbering exists at
# all) -- see _split_preamble()'s own docstring for why the numbered body
# is deliberately never scanned for these.
_HEADING_LINE_PATTERN = re.compile(
    r"(?:^|\n)[ \t]*(J\s*U\s*D\s*G\s*M\s*E\s*N\s*T|O\s*R\s*D\s*E\s*R|"
    r"FACTUAL\s+MATRIX|FACTS?(?:\s+OF\s+THE\s+CASE)?|BRIEF\s+FACTS|"
    r"CONCLUSIONS?|ANALYSIS\s+AND\s+CONCLUSIONS?)[ \t]*:?[ \t]*(?=\n|$)",
    re.IGNORECASE,
)

# Documentary-extract anchors: a paragraph reciting/quoting one of these is
# tagged type="document_extract" rather than a plain "paragraph" -- text is
# untouched either way, this only changes the `type`/`extract_type` tag.
# Checked against the paragraph's own leading text (see _EXTRACT_SCAN_CHARS)
# so an incidental later mention ("as noted in the FIR") in an otherwise
# unrelated paragraph doesn't mistag it.
_EXTRACT_PATTERNS: List[Tuple[str, "re.Pattern"]] = [
    ("fir", re.compile(r"\bFIRST\s+INFORMATION\s+REPORT\b|\bF\.?\s*I\.?\s*R\.?\s*(?:No\.?|Number)\b", re.IGNORECASE)),
    ("recovery_memo", re.compile(r"\bRECOVERY\s+MEMO\b", re.IGNORECASE)),
    ("disclosure_statement", re.compile(r"\bDISCLOSURE\s+STATEMENT\b", re.IGNORECASE)),
    ("demarcation_memo", re.compile(r"\bDEMARCATION\s+MEMO\b", re.IGNORECASE)),
    ("seizure_memo", re.compile(r"\bSEIZURE\s+MEMO\b", re.IGNORECASE)),
    ("panchnama", re.compile(r"\bPANCHNAMA\b", re.IGNORECASE)),
    ("site_plan", re.compile(r"\bSITE\s+PLAN\b", re.IGNORECASE)),
]
_EXTRACT_SCAN_CHARS = 400

# Footnotes — best-effort, low coverage by design (see _find_footnote_
# candidates' own docstring): most judgments in this corpus have none.
_FOOTNOTE_CANDIDATE_PATTERN = re.compile(r"(?:^|\n)[ \t]*(\d{1,2})[.\)][ \t]+(?=[A-Z(])", re.MULTILINE)
_FOOTNOTE_LOOKAHEAD_CHARS = 300

_BENCH_HINT_PATTERN = re.compile(r"HON'?BLE", re.IGNORECASE)

_MAX_CITATIONS = 200
_MAX_STATUTORY_REFERENCES = 100


class _LineIndex:
    """Maps a char offset to a 1-indexed line number in O(log n), for SourceSpan.start_line/end_line."""

    def __init__(self, text: str):
        self._newline_offsets = [i for i, ch in enumerate(text) if ch == "\n"]

    def line_of(self, char_offset: int) -> int:
        return bisect_right(self._newline_offsets, char_offset) + 1


def _span(start_char: int, end_char: int, line_index: "_LineIndex") -> SourceSpan:
    return SourceSpan(
        start_line=line_index.line_of(start_char),
        end_line=line_index.line_of(max(start_char, end_char - 1)),
        start_char=start_char,
        end_char=end_char,
    )


def _normalize_heading_label(raw: str) -> str:
    """
    "J U D G M E N T" -> "JUDGMENT"; "FACTUAL MATRIX" stays "FACTUAL MATRIX"
    -- the lookaround only fires between two lone single-letter tokens (an
    OCR'd letter-spaced heading), never inside a real multi-letter word, so
    a genuine multi-word heading's spacing is untouched.
    """
    condensed = re.sub(r"(?<=\b\w)\s+(?=\w\b)", "", raw)
    return re.sub(r"\s+", " ", condensed).strip().upper()


def _classify_extract(paragraph_text: str) -> Optional[str]:
    window = paragraph_text[:_EXTRACT_SCAN_CHARS]
    for extract_type, pattern in _EXTRACT_PATTERNS:
        if pattern.search(window):
            return extract_type
    return None


def _split_preamble(preamble: str, line_index: "_LineIndex", base_offset: int) -> List[Section]:
    """
    Splits the text before the first numbered paragraph (or the whole
    document, when no reliable numbering was found at all) into
    "front_matter" (case title/court/coram block) and "heading" sections
    for any standalone JUDGMENT/ORDER/FACTS/CONCLUSION-style line found.
    Never drops text: every character of `preamble` ends up in exactly one
    emitted section.
    """
    sections: List[Section] = []
    if not preamble or not preamble.strip():
        return sections

    cursor = 0
    for match in _HEADING_LINE_PATTERN.finditer(preamble):
        before = preamble[cursor:match.start()]
        if before.strip():
            sections.append(Section(
                type="front_matter", text=before.strip(),
                source=_span(base_offset + cursor, base_offset + match.start(), line_index),
            ))
        heading_label = _normalize_heading_label(match.group(1))
        sections.append(Section(
            type="heading", text=match.group(0).strip(), heading=heading_label,
            source=_span(base_offset + match.start(), base_offset + match.end(), line_index),
        ))
        cursor = match.end()

    tail = preamble[cursor:]
    if tail.strip():
        sections.append(Section(
            type="front_matter", text=tail.strip(),
            source=_span(base_offset + cursor, base_offset + len(preamble), line_index),
        ))

    return sections


def _strip_artifacts_from_paragraph(
    paragraph_text: str, start_char: int, end_char: int, artifact_spans: List[ArtifactSpan],
) -> Tuple[str, List[Tuple[int, int, str, str]]]:
    """
    Removes any artifact span overlapping [start_char, end_char) from
    paragraph_text (which must equal ocr_text[start_char:end_char]),
    clipped to that range -- an artifact regex's own optional trailing
    "\\n?" can match one character past a paragraph's own (whitespace-
    trimmed) end, so overlap rather than strict containment is what's
    checked; only the clipped, exact matched substring is ever removed,
    every other character of the paragraph is untouched.
    """
    overlapping = [a for a in artifact_spans if a["start"] < end_char and a["end"] > start_char]
    if not overlapping:
        return paragraph_text, []

    removed: List[Tuple[int, int, str, str]] = []
    cleaned_parts: List[str] = []
    cursor = start_char
    for artifact in sorted(overlapping, key=lambda a: a["start"]):
        clipped_start = max(artifact["start"], start_char)
        clipped_end = min(artifact["end"], end_char)
        if clipped_start < cursor or clipped_start >= clipped_end:
            continue
        cleaned_parts.append(paragraph_text[cursor - start_char: clipped_start - start_char])
        removed.append((
            clipped_start, clipped_end, artifact["artifact_type"],
            paragraph_text[clipped_start - start_char: clipped_end - start_char],
        ))
        cursor = clipped_end
    cleaned_parts.append(paragraph_text[cursor - start_char:])

    cleaned_text = re.sub(r"\n{3,}", "\n\n", "".join(cleaned_parts)).strip()
    return cleaned_text, removed


def _find_footnote_candidates(ocr_text: str, kept_digit_starts: Set[int]) -> List[Footnote]:
    """
    Best-effort, low-coverage on purpose: flags a numeral-prefixed line as a
    candidate footnote only when (a) it is NOT one of the real numbered-
    paragraph markers (kept_digit_starts) and (b) the text right after it
    looks like a citation or a statutory reference -- the shape a real
    footnote in this corpus actually takes (quoting a case or a provision).
    Most judgments have none; returning [] is the expected common case, not
    a detection failure (same treatment as regex_extraction.py's own
    extract_facts/extract_conclusion give their low-coverage headings).

    Compares on the DIGIT's own start offset (match.start(1)), not the
    whole match's start -- _PARA_START's leading "\\s*" can swallow a blank
    line's extra newline that this pattern's "[ \\t]*" (line-scoped, on
    purpose) does not, which would otherwise shift the two patterns'
    match.start() apart by one character on every blank-line-separated
    paragraph and defeat the exclusion entirely.
    """
    footnotes: List[Footnote] = []
    for match in _FOOTNOTE_CANDIDATE_PATTERN.finditer(ocr_text):
        if match.start(1) in kept_digit_starts:
            continue
        window = ocr_text[match.end():match.end() + _FOOTNOTE_LOOKAHEAD_CHARS]
        if not (citator.find_citation_candidates(window) or rx._PROVISION_PATTERN.search(window)):
            continue
        line_end = ocr_text.find("\n", match.end())
        if line_end == -1:
            line_end = len(ocr_text)
        text = ocr_text[match.start():line_end].strip()
        if text:
            footnotes.append(Footnote(marker=match.group(1), text=text))
    return footnotes


def parse_judgment(
    ocr_text: Optional[str],
    *,
    court: Optional[str] = "Supreme Court of India",
    bench: Optional[List[str]] = None,
    judgment_date: Optional[str] = None,
    case_number: Optional[str] = None,
    neutral_citation: Optional[str] = None,
) -> Dict:
    """
    Deterministically structures raw judgment OCR text into the
    StructuredJudgment JSON shape (parsers/schema.py). Never summarizes,
    paraphrases, or corrects the source text -- only tags structure and
    strips known OCR/layout artifacts out of the STRUCTURED copy (never out
    of the caller's own ocr_text, which this function doesn't mutate).

    `bench`/`judgment_date`/`case_number`/`neutral_citation` are accepted as
    already-resolved values (e.g. from pipeline/promotion.py, which parses
    them from the scraper's own table-cell fields far more reliably than
    OCR-text regex ever could) -- passing them in avoids re-deriving a
    lower-precision copy here. When `bench` is omitted, a conservative OCR
    fallback looks for a "HON'BLE ..." coram line in the document's own
    preamble; when it isn't found, `bench` stays empty rather than guessing.
    """
    doc = StructuredJudgment(
        court=court,
        bench=list(bench) if bench else [],
        judgment_date=judgment_date,
        case_number=case_number,
        neutral_citation=neutral_citation,
    )

    if not ocr_text or not ocr_text.strip():
        doc.warnings.append("empty_ocr_text")
        return doc.to_dict()

    try:
        _populate(doc, ocr_text)
    except Exception as exc:  # noqa: BLE001 -- a parser bug must never destroy the OCR text
        doc.sections = [Section(type="paragraph", text=ocr_text)]
        doc.document_extracts = []
        doc.footnotes = []
        doc.citations = []
        doc.statutory_references = []
        doc.final_order = None
        doc.removed_artifacts = []
        doc.warnings = [f"parser_failed: {exc!r}"]

    return doc.to_dict()


def _populate(doc: StructuredJudgment, ocr_text: str) -> None:
    line_index = _LineIndex(ocr_text)
    artifact_spans = find_artifact_spans(ocr_text)
    paragraphs = split_into_paragraphs_with_offsets(ocr_text)
    kept_matches = paragraph_start_matches(ocr_text)

    # The preamble ends at paragraph 1's own MARKER ("1."), not at the start
    # of its (whitespace-trimmed) text -- paragraphs[0][2] points past the
    # marker, which would otherwise leak "1." itself as a stray trailing
    # front_matter chunk.
    preamble_end = kept_matches[0].start() if kept_matches else len(ocr_text)
    preamble_text = ocr_text[:preamble_end]

    sections: List[Section] = list(_split_preamble(preamble_text, line_index, 0))
    document_extracts: List[Section] = []
    removed_artifacts: List[RemovedArtifact] = []

    for number, text, start_char, end_char in paragraphs:
        cleaned_text, removed = _strip_artifacts_from_paragraph(text, start_char, end_char, artifact_spans)
        for a_start, a_end, a_type, a_text in removed:
            removed_artifacts.append(RemovedArtifact(
                artifact_type=a_type, text=a_text, source=_span(a_start, a_end, line_index),
            ))

        extract_type = _classify_extract(cleaned_text)
        section = Section(
            type="document_extract" if extract_type else "paragraph",
            text=cleaned_text,
            number=number,
            extract_type=extract_type,
            source=_span(start_char, end_char, line_index),
        )
        sections.append(section)
        if extract_type:
            document_extracts.append(section)

    doc.sections = sections
    doc.document_extracts = document_extracts
    doc.removed_artifacts = removed_artifacts

    disposition = rx.extract_disposition(ocr_text)
    if disposition.get("disposition_raw"):
        matching_paragraph_number = next(
            (
                s.number for s in sections
                if s.type in ("paragraph", "document_extract") and disposition["disposition_raw"] in s.text
            ),
            None,
        )
        doc.final_order = FinalOrder(
            text=disposition["disposition_raw"],
            paragraph_number=matching_paragraph_number,
            disposition_category=disposition.get("disposition_category"),
        )

    doc.citations = [
        Citation(raw_text=c["raw_citation"], context=c.get("context"))
        for c in citator.find_citation_candidates(ocr_text)[:_MAX_CITATIONS]
    ]

    doc.statutory_references = [
        StatutoryReference(
            provision_type=p["provision_type"],
            number=p["section_number"],
            statute_name=p["statute_name"],
            short_code=p["short_code"],
            statute_year=p["statute_year"],
        )
        for p in rx.extract_provisions(ocr_text, max_results=_MAX_STATUTORY_REFERENCES)
    ]

    if not doc.bench:
        hint = _BENCH_HINT_PATTERN.search(preamble_text)
        if hint:
            doc.bench = rx.parse_bench(preamble_text[hint.start():])

    kept_digit_starts = {m.start(1) for m in kept_matches}
    doc.footnotes = _find_footnote_candidates(ocr_text, kept_digit_starts)
