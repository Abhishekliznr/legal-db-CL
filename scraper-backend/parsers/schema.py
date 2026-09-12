"""
Canonical structured-judgment representation produced by
parsers/judgment_parser.py and consumed by parsers/html_renderer.py (and,
via cr_cases.structured_content JSONB, by api-backend).

Plain dataclasses, same convention as adapters/base.py's RawJudgmentRecord --
to_dict() (dataclasses.asdict) is what actually gets JSON-serialized into
the DB, so field names here ARE the JSON schema.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

PARSER_VERSION = "1.0"

# Every value parsers/judgment_parser.py may put in a Section's `type`.
SECTION_TYPES = ("front_matter", "heading", "paragraph", "document_extract")

# Recognized document_extract categories (see judgment_parser._EXTRACT_PATTERNS).
EXTRACT_TYPES = (
    "fir", "recovery_memo", "disclosure_statement", "demarcation_memo",
    "seizure_memo", "panchnama", "site_plan",
)


@dataclass
class SourceSpan:
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    start_char: Optional[int] = None
    end_char: Optional[int] = None


@dataclass
class Section:
    type: str  # one of SECTION_TYPES
    text: str
    number: Optional[int] = None          # paragraph number, for type="paragraph"/"document_extract"
    heading: Optional[str] = None         # heading label, for type="heading"
    extract_type: Optional[str] = None    # one of EXTRACT_TYPES, for type="document_extract"
    source: SourceSpan = field(default_factory=SourceSpan)


@dataclass
class Footnote:
    text: str
    marker: Optional[str] = None
    source: SourceSpan = field(default_factory=SourceSpan)


@dataclass
class Citation:
    raw_text: str
    context: Optional[str] = None


@dataclass
class StatutoryReference:
    provision_type: str  # "section" | "rule" | "order"
    number: str
    statute_name: Optional[str] = None
    short_code: Optional[str] = None
    statute_year: Optional[int] = None


@dataclass
class RemovedArtifact:
    artifact_type: str  # "signature_block" | "page_number" | "toc_dot_leader"
    text: str
    source: SourceSpan = field(default_factory=SourceSpan)


@dataclass
class FinalOrder:
    text: Optional[str] = None
    paragraph_number: Optional[int] = None
    disposition_category: Optional[str] = None


@dataclass
class StructuredJudgment:
    document_type: str = "judgment"
    court: Optional[str] = None
    bench: List[str] = field(default_factory=list)
    judgment_date: Optional[str] = None
    case_number: Optional[str] = None
    neutral_citation: Optional[str] = None
    sections: List[Section] = field(default_factory=list)
    # Convenience index into `sections` above (same Section objects, same
    # document order) -- every section whose type=="document_extract", so a
    # caller wanting just "which documents does this judgment quote" doesn't
    # have to filter `sections` itself. `sections` (not this list) is the
    # source of truth for document order/position.
    document_extracts: List[Section] = field(default_factory=list)
    footnotes: List[Footnote] = field(default_factory=list)
    citations: List[Citation] = field(default_factory=list)
    statutory_references: List[StatutoryReference] = field(default_factory=list)
    final_order: Optional[FinalOrder] = None
    removed_artifacts: List[RemovedArtifact] = field(default_factory=list)
    parser_version: str = PARSER_VERSION
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
