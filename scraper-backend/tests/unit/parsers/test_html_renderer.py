"""Pytest unit tests for parsers/html_renderer.py -- pure function over an already-structured dict, no parsing of its own."""

from parsers.html_renderer import render_judgment_html
from parsers.judgment_parser import parse_judgment

from tests.unit.parsers.test_judgment_parser import SAMPLE_JUDGMENT


def test_paragraphs_get_addressable_para_ids():
    structured = parse_judgment(SAMPLE_JUDGMENT)
    html = render_judgment_html(structured)
    assert 'id="para-1"' in html
    assert 'id="para-6"' in html


def test_document_extract_carries_its_extract_type_attribute():
    structured = parse_judgment(SAMPLE_JUDGMENT)
    html = render_judgment_html(structured)
    assert 'data-extract-type="fir"' in html
    assert 'data-extract-type="recovery_memo"' in html


def test_final_order_block_links_back_to_its_paragraph():
    structured = parse_judgment(SAMPLE_JUDGMENT)
    html = render_judgment_html(structured)
    assert 'id="final-order"' in html
    assert 'href="#para-6"' in html


def test_heading_is_rendered_as_a_heading_tag():
    structured = parse_judgment(SAMPLE_JUDGMENT)
    html = render_judgment_html(structured)
    assert "<h2" in html
    assert "JUDGMENT" in html


def test_paragraph_text_is_html_escaped():
    structured = parse_judgment(
        "1. The clause reads <notice> & \"quoted\" text.\n\n"
        "2. Second paragraph for numbering.\n\n"
        "3. Third paragraph for numbering.\n"
    )
    html = render_judgment_html(structured)
    assert "<notice>" not in html
    assert "&lt;notice&gt;" in html
    assert "&amp;" in html


def test_removed_artifacts_never_appear_in_the_rendered_html():
    structured = parse_judgment(SAMPLE_JUDGMENT)
    html = render_judgment_html(structured)
    assert "Digitally signed by" not in html
    assert "Signature Not Verified" not in html


def test_no_inline_style_attributes_are_emitted():
    structured = parse_judgment(SAMPLE_JUDGMENT)
    html = render_judgment_html(structured)
    assert "style=" not in html


def test_empty_structure_renders_an_empty_but_valid_article():
    structured = parse_judgment("")
    html = render_judgment_html(structured)
    assert html.startswith("<article")
    assert html.endswith("</article>")
