from normalization.ocr_artifacts import find_artifact_spans, strip_ocr_noise


def test_strip_ocr_noise_removes_signature_block():
    text = "Real text.\n\nDigitally signed by\nJohn Doe\nReason: x\nSignature Not Verified\n\nMore real text."
    cleaned = strip_ocr_noise(text)
    assert "Digitally signed by" not in cleaned
    assert "Real text." in cleaned
    assert "More real text." in cleaned


def test_strip_ocr_noise_removes_bare_page_number_line():
    text = "Paragraph one.\n\n7\n\nParagraph two."
    cleaned = strip_ocr_noise(text)
    assert "\n7\n" not in cleaned
    assert "Paragraph one." in cleaned
    assert "Paragraph two." in cleaned


def test_strip_ocr_noise_removes_toc_dot_leader_line():
    text = "Conclusion .......... 42\nReal heading text"
    cleaned = strip_ocr_noise(text)
    assert "....." not in cleaned
    assert "Real heading text" in cleaned


def test_find_artifact_spans_reports_correct_offsets():
    text = "abc\n42\ndef"
    spans = find_artifact_spans(text)
    assert len(spans) == 1
    span = spans[0]
    assert span["artifact_type"] == "page_number"
    assert text[span["start"]:span["end"]].strip() == "42"


def test_find_artifact_spans_empty_text():
    assert find_artifact_spans("") == []


def test_find_artifact_spans_no_artifacts():
    assert find_artifact_spans("Just ordinary judgment prose with no artifacts.") == []
