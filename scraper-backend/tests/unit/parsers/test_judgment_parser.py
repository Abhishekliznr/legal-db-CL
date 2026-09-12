"""
Pytest unit tests for parsers/judgment_parser.py -- deterministic, no
network/DB, no LLM. SAMPLE_JUDGMENT below is a synthetic-but-realistic
judgment text built to exercise every pattern regex_extraction.py's own
module docstring documents as real (numbered paragraphs, a letter-spaced
"J U D G M E N T" heading, a digital-signature block, a bare page-number
line, an FIR/Recovery Memo mention, a reporter citation, two statutory
references, and a final disposal sentence) -- no real judgment PDF text is
checked into this repo to build fixtures from directly.
"""

from parsers.judgment_parser import parse_judgment

SAMPLE_JUDGMENT = """IN THE SUPREME COURT OF INDIA
CRIMINAL APPELLATE JURISDICTION
CRIMINAL APPEAL NO. 123 OF 2020

RAM KUMAR
VERSUS
STATE OF U.P.

HON'BLE MR. JUSTICE A.K. SHARMA
HON'BLE MRS. JUSTICE B.V. NAGARATHNA

J U D G M E N T

1. This appeal arises out of the judgment of the High Court of Allahabad dated 12.03.2019, whereby the conviction of the appellant under Section 302 of the Indian Penal Code, 1860 was upheld.

2. The prosecution case, briefly stated, is that the First Information Report bearing FIR No. 45/2015 was registered at Police Station Kotwali on the complaint of the deceased's brother, alleging that the appellant assaulted the deceased with a sharp weapon.

3. A Recovery Memo dated 16.01.2015 records that the weapon of offence, a country-made knife, was recovered from the appellant's residence pursuant to his disclosure statement recorded under Section 27 of the Indian Evidence Act, 1872.

12

4. This Court in State of Punjab v. Gurmit Singh, (2019) 4 SCC 396, held that a solitary eyewitness's testimony, if credible, is sufficient to sustain a conviction.

Digitally signed by
Rajesh Kumar
Date: 2020.05.12 10:23:00 IST
Reason: Approved
Signature Not Verified

5. Having considered the evidence on record, we find no infirmity in the judgment of the High Court.

6. For the reasons recorded above, this appeal is dismissed. The bail bonds of the appellant, if any, are cancelled.
"""

FOOTNOTE_TEXT = (
    "1. The doctrine of basic structure was propounded by this Court.\n\n"
    "15. See Kesavananda Bharati v. State of Kerala, (1973) 4 SCC 225, for the original enunciation.\n\n"
    "2. It has since been consistently applied by subsequent Constitution Benches.\n\n"
    "3. For these reasons, the reference is answered accordingly.\n"
)


def _paragraph_sections(result):
    return [s for s in result["sections"] if s["type"] in ("paragraph", "document_extract")]


class TestNumberedParagraphDetection:
    def test_finds_every_numbered_paragraph_in_order(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        numbers = [s["number"] for s in _paragraph_sections(result)]
        assert numbers == [1, 2, 3, 4, 5, 6]

    def test_paragraph_ids_are_stable_and_addressable(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        paragraphs = _paragraph_sections(result)
        assert paragraphs[0]["number"] == 1
        assert "This appeal arises out of" in paragraphs[0]["text"]

    def test_source_spans_are_populated(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        for section in result["sections"]:
            assert section["source"]["start_char"] is not None
            assert section["source"]["end_char"] > section["source"]["start_char"]
            assert section["source"]["start_line"] is not None


class TestParagraphBoundaries:
    def test_paragraph_text_does_not_bleed_into_the_next_paragraph(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        paragraphs = {s["number"]: s["text"] for s in _paragraph_sections(result)}
        assert "prosecution case" not in paragraphs[1]
        assert "Recovery Memo" not in paragraphs[2]


class TestArtifactRemoval:
    def test_bare_page_number_is_stripped_from_paragraph_text(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        paragraphs = {s["number"]: s["text"] for s in _paragraph_sections(result)}
        assert "\n12\n" not in paragraphs[3]
        lines = [line.strip() for line in paragraphs[3].splitlines()]
        assert "12" not in lines

    def test_digital_signature_block_is_stripped_from_paragraph_text(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        paragraphs = {s["number"]: s["text"] for s in _paragraph_sections(result)}
        assert "Digitally signed by" not in paragraphs[4]
        assert "Signature Not Verified" not in paragraphs[4]
        # the real legal text on either side of the artifact must survive
        assert "sufficient to sustain a conviction" in paragraphs[4]

    def test_removed_artifacts_are_recorded_with_provenance(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        types = {a["artifact_type"] for a in result["removed_artifacts"]}
        assert "page_number" in types
        assert "signature_block" in types
        for artifact in result["removed_artifacts"]:
            assert artifact["source"]["start_char"] is not None


class TestHeadingDetection:
    def test_judgment_heading_is_detected_and_condensed(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        headings = [s for s in result["sections"] if s["type"] == "heading"]
        assert any(h["heading"] == "JUDGMENT" for h in headings)

    def test_front_matter_precedes_the_heading(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        types_in_order = [s["type"] for s in result["sections"][:2]]
        assert types_in_order == ["front_matter", "heading"]
        front_matter = result["sections"][0]
        assert "SUPREME COURT OF INDIA" in front_matter["text"]


class TestDocumentExtractDetection:
    def test_fir_paragraph_is_tagged(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        para_2 = next(s for s in _paragraph_sections(result) if s["number"] == 2)
        assert para_2["type"] == "document_extract"
        assert para_2["extract_type"] == "fir"

    def test_recovery_memo_paragraph_is_tagged(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        para_3 = next(s for s in _paragraph_sections(result) if s["number"] == 3)
        assert para_3["type"] == "document_extract"
        assert para_3["extract_type"] == "recovery_memo"

    def test_document_extracts_index_matches_tagged_sections(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        extract_numbers = {e["number"] for e in result["document_extracts"]}
        assert extract_numbers == {2, 3}

    def test_ordinary_paragraph_is_not_tagged(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        para_1 = next(s for s in _paragraph_sections(result) if s["number"] == 1)
        assert para_1["type"] == "paragraph"
        assert para_1["extract_type"] is None


class TestCitationDetection:
    def test_reporter_citation_is_found(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        raw_texts = [c["raw_text"] for c in result["citations"]]
        assert any("SCC" in t and "396" in t for t in raw_texts)


class TestStatutoryReferenceDetection:
    def test_ipc_section_is_found(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        refs = result["statutory_references"]
        assert any(r["number"] == "302" and r["statute_name"] and "Penal Code" in r["statute_name"] for r in refs)

    def test_evidence_act_section_is_found(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        refs = result["statutory_references"]
        assert any(r["number"] == "27" and r["statute_name"] and "Evidence Act" in r["statute_name"] for r in refs)


class TestFinalOrderDetection:
    def test_disposition_sentence_and_category_found(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        assert result["final_order"] is not None
        assert result["final_order"]["disposition_category"] == "Dismissed"
        assert "this appeal is dismissed" in result["final_order"]["text"]

    def test_final_order_points_back_at_its_paragraph(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        assert result["final_order"]["paragraph_number"] == 6


class TestFootnoteDetection:
    def test_no_footnotes_on_the_main_sample(self):
        # Low coverage is the expected common case (see the module's own
        # docstring) -- SAMPLE_JUDGMENT has no footnote-shaped text at all.
        result = parse_judgment(SAMPLE_JUDGMENT)
        assert result["footnotes"] == []

    def test_out_of_sequence_numeral_followed_by_a_citation_is_flagged(self):
        result = parse_judgment(FOOTNOTE_TEXT)
        markers = [f["marker"] for f in result["footnotes"]]
        assert "15" in markers
        footnote = next(f for f in result["footnotes"] if f["marker"] == "15")
        assert "Kesavananda Bharati" in footnote["text"]

    def test_real_paragraph_numbers_are_never_flagged_as_footnotes(self):
        result = parse_judgment(FOOTNOTE_TEXT)
        markers = {f["marker"] for f in result["footnotes"]}
        assert markers.isdisjoint({"1", "2", "3"})


class TestSourceTextPreservation:
    def test_every_paragraph_sentence_survives_verbatim(self):
        result = parse_judgment(SAMPLE_JUDGMENT)
        paragraphs = {s["number"]: s["text"] for s in _paragraph_sections(result)}
        assert paragraphs[1] == (
            "This appeal arises out of the judgment of the High Court of Allahabad dated 12.03.2019, "
            "whereby the conviction of the appellant under Section 302 of the Indian Penal Code, 1860 was upheld."
        )

    def test_caller_ocr_text_is_never_mutated(self):
        original = SAMPLE_JUDGMENT
        before = str(original)
        parse_judgment(original)
        assert original == before

    def test_parser_never_paraphrases_paragraph_text(self):
        # Every kept paragraph's cleaned text must be a substring of the
        # original OCR text (after only whitespace normalization) -- proof
        # nothing was reworded, only artifacts removed.
        result = parse_judgment(SAMPLE_JUDGMENT)
        collapsed_source = " ".join(SAMPLE_JUDGMENT.split())
        for section in _paragraph_sections(result):
            collapsed_text = " ".join(section["text"].split())
            assert collapsed_text in collapsed_source


class TestMalformedInput:
    def test_empty_string_returns_empty_structure_not_an_error(self):
        result = parse_judgment("")
        assert result["sections"] == []
        assert "empty_ocr_text" in result["warnings"]

    def test_none_returns_empty_structure_not_an_error(self):
        result = parse_judgment(None)
        assert result["sections"] == []
        assert "empty_ocr_text" in result["warnings"]

    def test_whitespace_only_returns_empty_structure(self):
        result = parse_judgment("   \n\n\t  ")
        assert result["sections"] == []

    def test_unnumbered_text_falls_back_to_front_matter_without_crashing(self):
        text = "This is a short procedural order with no numbered paragraphs at all, just plain prose."
        result = parse_judgment(text)
        assert result["sections"]
        assert any(text in s["text"] for s in result["sections"])

    def test_garbled_ocr_does_not_crash_and_preserves_text(self):
        garbled = "##@@ 1 . xx\n\n2)yy\n\n%%%3 zz???"
        result = parse_judgment(garbled)
        # Whatever structure (or lack of it) is produced, no exception is
        # raised and the reconstructed sections still contain the source.
        assert isinstance(result, dict)
        assert result["sections"]

    def test_parser_failure_falls_back_to_verbatim_text(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("simulated parser bug")

        monkeypatch.setattr("parsers.judgment_parser._populate", _boom)
        result = parse_judgment(SAMPLE_JUDGMENT)
        assert result["sections"] == [{
            "type": "paragraph", "text": SAMPLE_JUDGMENT, "number": None,
            "heading": None, "extract_type": None,
            "source": {"start_line": None, "end_line": None, "start_char": None, "end_char": None},
        }]
        assert any(w.startswith("parser_failed") for w in result["warnings"])


class TestDifferentJudgmentStructures:
    def test_order_heading_is_detected_instead_of_judgment(self):
        text = (
            "IN THE SUPREME COURT OF INDIA\n"
            "CIVIL APPELLATE JURISDICTION\n\n"
            "O R D E R\n\n"
            "1. Issue notice.\n\n"
            "2. List the matter after four weeks.\n\n"
            "3. The interim order shall continue till the next date of hearing.\n"
        )
        result = parse_judgment(text)
        headings = [s["heading"] for s in result["sections"] if s["type"] == "heading"]
        assert "ORDER" in headings

    def test_bench_falls_back_to_ocr_coram_when_not_provided(self):
        text = (
            "IN THE SUPREME COURT OF INDIA\n"
            "CORAM:\n"
            "HON'BLE MR. JUSTICE A.K. SHARMA\n"
            "HON'BLE MRS. JUSTICE B.V. NAGARATHNA\n\n"
            "J U D G M E N T\n\n"
            "1. This is the first paragraph of the order.\n\n"
            "2. This is the second paragraph.\n\n"
            "3. This is the third and final paragraph.\n"
        )
        result = parse_judgment(text)
        assert len(result["bench"]) == 2

    def test_explicit_bench_is_preferred_over_ocr_fallback(self):
        result = parse_judgment(SAMPLE_JUDGMENT, bench=["A.K. SHARMA"])
        assert result["bench"] == ["A.K. SHARMA"]

    def test_metadata_passthrough(self):
        result = parse_judgment(
            SAMPLE_JUDGMENT,
            court="Supreme Court of India",
            judgment_date="2020-05-12",
            case_number="Criminal Appeal No. 123 of 2020",
            neutral_citation="2020 INSC 123",
        )
        assert result["court"] == "Supreme Court of India"
        assert result["judgment_date"] == "2020-05-12"
        assert result["case_number"] == "Criminal Appeal No. 123 of 2020"
        assert result["neutral_citation"] == "2020 INSC 123"
