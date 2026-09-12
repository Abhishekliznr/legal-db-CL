"""
Semantic HTML renderer for the StructuredJudgment JSON (parsers/schema.py,
produced by parsers/judgment_parser.py) — the "Structured JSON -> HTML
Renderer" stage. Deliberately separate from the parser: this module only
ever reads the already-structured dict, never the raw OCR text, and never
alters any text content, only wraps it in markup.

Every text node is passed through html.escape() before insertion, so this
is safe against a judgment's own text containing "<"/"&"/etc. (real OCR
text regularly does, e.g. "Section 302 <the Penal Code>"-shaped OCR noise
or literal angle-bracket citations). No inline styling is emitted — only
semantic tags plus stable class names (`judgment-*`) — so the consuming
frontend's own styling system (Tailwind, in legal-ui's case) can target
them; this renderer does not assume any particular CSS framework.

Paragraph and document-extract sections get `id="para-{number}"` (spec
requirement: deep links, copy-link, search highlighting, per-paragraph
annotations all key off this exact id shape).
"""

from html import escape
from typing import Any, Dict, List, Optional


def _escaped_lines_to_html(text: str) -> str:
    """Turns internal blank-line breaks into paragraph breaks, and single line breaks into <br> -- text itself is escaped first, so no markup from the source text can leak through."""
    escaped = escape(text)
    blocks = [b.strip() for b in escaped.split("\n\n") if b.strip()]
    if not blocks:
        return ""
    return "".join(f"<p>{block.replace(chr(10), '<br>')}</p>" for block in blocks)


def _render_front_matter(section: Dict[str, Any]) -> str:
    return f'<div class="judgment-front-matter">{_escaped_lines_to_html(section["text"])}</div>'


def _render_heading(section: Dict[str, Any]) -> str:
    heading = escape(section.get("heading") or section["text"])
    return f'<h2 class="judgment-heading">{heading}</h2>'


def _render_paragraph(section: Dict[str, Any]) -> str:
    number = section.get("number")
    anchor_id = f' id="para-{number}"' if number is not None else ""
    number_html = f'<span class="judgment-paragraph-number">{number}.</span> ' if number is not None else ""
    return (
        f'<div class="judgment-paragraph"{anchor_id} data-paragraph-number="{escape(str(number)) if number is not None else ""}">'
        f'{number_html}<span class="judgment-paragraph-text">{_escaped_lines_to_html(section["text"])}</span>'
        f"</div>"
    )


def _render_document_extract(section: Dict[str, Any]) -> str:
    number = section.get("number")
    anchor_id = f' id="para-{number}"' if number is not None else ""
    extract_type = escape(section.get("extract_type") or "")
    number_html = f'<span class="judgment-paragraph-number">{number}.</span> ' if number is not None else ""
    return (
        f'<div class="judgment-document-extract"{anchor_id} data-extract-type="{extract_type}" data-paragraph-number="{escape(str(number)) if number is not None else ""}">'
        f'{number_html}<span class="judgment-paragraph-text">{_escaped_lines_to_html(section["text"])}</span>'
        f"</div>"
    )


_SECTION_RENDERERS = {
    "front_matter": _render_front_matter,
    "heading": _render_heading,
    "paragraph": _render_paragraph,
    "document_extract": _render_document_extract,
}


def _render_final_order(final_order: Optional[Dict[str, Any]]) -> str:
    if not final_order or not final_order.get("text"):
        return ""
    paragraph_number = final_order.get("paragraph_number")
    link = f'<a href="#para-{paragraph_number}">Paragraph {paragraph_number}</a>' if paragraph_number is not None else ""
    category = escape(final_order["disposition_category"]) if final_order.get("disposition_category") else None
    parts = [f'<p class="judgment-final-order-text">{escape(final_order["text"])}</p>']
    if category:
        parts.append(f'<p class="judgment-final-order-category">{category}</p>')
    if link:
        parts.append(f'<p class="judgment-final-order-link">{link}</p>')
    return f'<div class="judgment-final-order" id="final-order">{"".join(parts)}</div>'


def render_judgment_html(structured: Dict[str, Any]) -> str:
    """
    Renders a StructuredJudgment dict (parsers.schema.StructuredJudgment.to_dict(),
    or the equivalent JSON round-tripped from cr_cases.structured_content)
    into a single semantic HTML string. Purely presentational -- assumes
    the input is already structured; does not parse or clean OCR text
    itself (see parsers/judgment_parser.py for that).
    """
    sections: List[Dict[str, Any]] = structured.get("sections") or []
    body_html = []
    for section in sections:
        renderer = _SECTION_RENDERERS.get(section.get("type"))
        if renderer is None:
            continue
        body_html.append(renderer(section))

    final_order_html = _render_final_order(structured.get("final_order"))
    parser_version = escape(structured.get("parser_version") or "")

    return (
        f'<article class="judgment" data-parser-version="{parser_version}">'
        f'{"".join(body_html)}'
        f"{final_order_html}"
        f"</article>"
    )
