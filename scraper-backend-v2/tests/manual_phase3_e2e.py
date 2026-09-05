"""
Manual Phase 3 end-to-end check — NOT a pytest suite, run directly.

Covers what Phase 3 added on top of Phase 1's already-verified pipeline
shape: real Tesseract OCR fallback on an actual scanned (no-text-layer) PDF,
the LLM extraction call's request/response handling (mocked — no live call,
see pipeline/extraction.py's docstring on why), the no-API-key stub
fallback, and promotion.py's new provisions/citations/enum-validation
paths.

Run against a real Postgres with schema.sql applied, courts seeded:

    export DATABASE_TYPE=postgres DB_HOST=localhost DB_PORT=5432 \\
           DB_NAME=liznrlegal DB_USER=postgres DB_PASSWORD=testpass
    python3 -m tests.manual_phase3_e2e
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from adapters.base import RawJudgmentRecord
from db import scrape_jobs
from db.connection import get_pooled_connection, init_connection_pool
from orchestrator import batch_runner
from pipeline import extraction, ocr, promotion


def _court_id_for(name: str) -> int:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT court_id FROM courts WHERE court_name = %s;", (name,))
            row = cur.fetchone()
            assert row is not None, f"court not seeded: {name}"
            return row[0]


def make_scanned_pdf(path: Path) -> None:
    """A real image-only PDF (no text layer) — forces the Tesseract fallback path."""
    img = Image.new("RGB", (900, 400), "white")
    draw = ImageDraw.Draw(img)
    draw.text(
        (20, 20),
        "IN THE SUPREME COURT OF INDIA\nCIVIL APPEAL NO. 777 OF 2026\n\n"
        "AJAY SINGH ... PETITIONER\nVERSUS\nUNION OF INDIA ... RESPONDENT\n\n"
        "The appeal is dismissed.\nSection 34 of the Arbitration and Conciliation Act was invoked.\n"
        "Pronounced on: 10-03-2026",
        fill="black",
    )
    img.save(str(path), "PDF")


def test_ocr_fallback_on_real_scanned_pdf():
    print("\n=== TEST: real Tesseract fallback on a scanned PDF ===")
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "scanned.pdf"
        make_scanned_pdf(pdf_path)

        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO raw_ingestions (court_id, source_url, file_checksum, status)
                    VALUES (1, 'https://test/scanned.pdf', 'phase3-ocr-test-checksum', 'DOWNLOADED')
                    RETURNING ingestion_id;
                """)
                ingestion_id = cur.fetchone()[0]
            conn.commit()

        ocr.process_ingestion_ocr(ingestion_id, pdf_path)
        row = scrape_jobs.get_ingestion(ingestion_id)
        print("ocr result:", row["status"], "| engine used should be tesseract")

        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT ocr_engine, ocr_confidence FROM raw_ingestions WHERE ingestion_id = %s;", (ingestion_id,))
                engine, confidence = cur.fetchone()

        print(f"ocr_engine={engine} ocr_confidence={confidence}")
        assert row["status"] == "OCR_DONE"
        assert engine == "tesseract"
        assert confidence is not None and confidence > 0
        assert "SUPREME COURT" in row["ocr_text"].upper()
    print("PASSED")


def test_extraction_no_api_key_uses_stub():
    print("\n=== TEST: no GROQ_API_KEY falls back to Phase 1 stub ===")
    os.environ.pop("GROQ_API_KEY", None)
    record = RawJudgmentRecord(
        pdf_path=Path("/dev/null"), source_url="https://test/x.pdf",
        case_number_raw="C.A. No.-1 - 2026", party_name_raw="A VS B",
        judge_raw="JUSTICE X", decision_date_raw="01-01-2026",
    )
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO raw_ingestions (court_id, source_url, file_checksum, status, ocr_text)
                VALUES (1, 'https://test/stub.pdf', 'phase3-stub-test-checksum', 'OCR_DONE', 'some text')
                RETURNING ingestion_id;
            """)
            ingestion_id = cur.fetchone()[0]
        conn.commit()

    extraction.process_ingestion_extraction(ingestion_id, record)
    row = scrape_jobs.get_ingestion(ingestion_id)
    print("extractor used:", row["raw_ai_extraction"].get("extractor"))
    assert row["status"] == "EXTRACTED"
    assert row["raw_ai_extraction"]["extractor"] == "scraper_fields_stub_v1"
    print("PASSED")


def test_extraction_with_mocked_llm_call():
    print("\n=== TEST: real extraction path with a mocked LLM response ===")
    os.environ["GROQ_API_KEY"] = "test-fake-key-not-real"

    fake_llm_response = {
        "case_note_ai": "The Supreme Court dismissed the appeal concerning arbitration under Section 34.",
        "cases": [{"case_number": "C.A. No.-777 - 2026", "cnr_number": None,
                   "parties": [{"name": "Ajay Singh", "side": "PETITIONER_SIDE"}, {"name": "Union of India", "side": "RESPONDENT_SIDE"}]}],
        "coram": [{"name": "Justice B.V. Nagarathna", "is_author": True}],
        "provisions": [{"statute_name": "Arbitration and Conciliation Act", "section_number": "34"}],
        "citations": [{"cited_case_name": "Some Prior Case v. State", "cited_reporter_citation": "(2020) 5 SCC 100", "treatment": "Followed"}],
        "disposition_category": "Dismissed",
        "favoring_party_side": "RESPONDENT_SIDE",
        "date_of_judgment": "2026-03-10",
    }

    record = RawJudgmentRecord(
        pdf_path=Path("/dev/null"), source_url="https://test/llm.pdf",
        case_number_raw=None, party_name_raw=None, judge_raw=None, decision_date_raw=None,
    )

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO raw_ingestions (court_id, source_url, file_checksum, status, ocr_text)
                VALUES (1, 'https://test/llm.pdf', 'phase3-llm-test-checksum', 'OCR_DONE', 'the appeal is dismissed under section 34 arbitration act')
                RETURNING ingestion_id;
            """)
            ingestion_id = cur.fetchone()[0]
        conn.commit()

    with patch("pipeline.extraction.call_llm_extraction", return_value=fake_llm_response):
        extraction.process_ingestion_extraction(ingestion_id, record)

    row = scrape_jobs.get_ingestion(ingestion_id)
    env = row["raw_ai_extraction"]
    print("extractor:", env["extractor"], "| judgment_date_raw:", env["judgment_date_raw"])
    assert row["status"] == "EXTRACTED"
    assert env["extractor"].startswith("llm_v1")
    assert env["judgment_date_raw"] == "2026-03-10"  # scraper had none, so LLM's date_of_judgment wins
    assert len(env["cases"][0]["parties"]) == 2

    document_id = promotion.promote_ingestion(ingestion_id)
    print("promoted document_id:", document_id)
    assert document_id is not None

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT needs_review, disposition_category, favoring_party_side FROM documents WHERE document_id = %s;", (document_id,))
            needs_review, disposition, favoring = cur.fetchone()
            print(f"needs_review={needs_review} disposition_category={disposition} favoring_party_side={favoring}")
            assert needs_review is False, "an llm_v1 extraction should NOT be flagged needs_review"
            assert disposition == "Dismissed"
            assert favoring == "RESPONDENT_SIDE"

            cur.execute("""
                SELECT s.statute_name, sec.section_number FROM document_sections ds
                JOIN sections sec ON sec.section_id = ds.section_id
                JOIN statutes s ON s.statute_id = sec.statute_id
                WHERE ds.document_id = %s;
            """, (document_id,))
            provisions = cur.fetchall()
            print("provisions:", provisions)
            assert provisions == [("Arbitration and Conciliation Act, 1996", "34")], "should have resolved to the canonical statute name"

            cur.execute("SELECT cited_case_name, treatment FROM citations WHERE citing_document_id = %s;", (document_id,))
            citations_rows = cur.fetchall()
            print("citations:", citations_rows)
            assert citations_rows == [("Some Prior Case v. State", "Followed")]
    print("PASSED")


def test_invalid_enum_values_dont_crash_promotion():
    print("\n=== TEST: bad enum values from a (hypothetically misbehaving) LLM don't crash promotion ===")
    bad_extraction = {
        "extractor": "llm_v1:test",
        "case_note_ai": "test",
        "cases": [{"case_number": "C.A. No.-999 - 2026", "cnr_number": None,
                   "parties": [{"name": "A", "side": "petitioner"}]}],  # wrong case — invalid enum value
        "coram": [],
        "provisions": [],
        "citations": [{"cited_case_name": "X v. Y", "cited_reporter_citation": None, "treatment": "SUPER_OVERRULED"}],  # not a real enum value
        "disposition_category": "Won",  # not a real enum value
        "favoring_party_side": None,
        "neutral_citation": None,
        "judgment_date_raw": "2026-04-01",
    }

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO raw_ingestions (court_id, source_url, file_checksum, status, ocr_text, raw_ai_extraction)
                VALUES (1, 'https://test/badenum.pdf', 'phase3-badenum-test-checksum', 'EXTRACTED', 'text', %s::jsonb)
                RETURNING ingestion_id;
            """, (__import__("json").dumps(bad_extraction),))
            ingestion_id = cur.fetchone()[0]
        conn.commit()

    document_id = promotion.promote_ingestion(ingestion_id)
    print("promoted despite bad enum values, document_id:", document_id)
    assert document_id is not None, "should have promoted successfully, just with the bad enum fields nulled"

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT disposition_category FROM documents WHERE document_id = %s;", (document_id,))
            print("disposition_category (expect None):", cur.fetchone()[0])
            cur.execute("SELECT count(*) FROM parties WHERE case_id IN (SELECT case_id FROM cases WHERE document_id = %s);", (document_id,))
            print("parties inserted despite bad side value (expect 0 — skipped, not crashed):", cur.fetchone()[0])
            cur.execute("SELECT treatment FROM citations WHERE citing_document_id = %s;", (document_id,))
            print("citation treatment (expect None, not a crash):", cur.fetchone()[0])
    print("PASSED")


def main():
    init_connection_pool()
    test_ocr_fallback_on_real_scanned_pdf()
    test_extraction_no_api_key_uses_stub()
    test_extraction_with_mocked_llm_call()
    test_invalid_enum_values_dont_crash_promotion()
    print("\nALL PHASE 3 CHECKS PASSED")


if __name__ == "__main__":
    main()
