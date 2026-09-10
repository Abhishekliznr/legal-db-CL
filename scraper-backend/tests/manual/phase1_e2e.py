"""
Manual Phase 1 end-to-end check — NOT a pytest suite, run directly.

Uses a fake adapter (real SupremeCourtAdapter needs live network + a real
captcha, which this sandbox can't do) yielding one record that points at a
real, locally-generated PDF, and pushes it through the actual orchestrator:
checksum dedup -> raw_ingestions insert -> OCR -> stub extraction ->
promotion -> documents/cases/parties/document_coram.

Run against a real Postgres with schema.sql already applied and at least
one row in `courts`:

    export DATABASE_TYPE=postgres DB_HOST=localhost DB_PORT=5432 \\
           DB_NAME=liznrlegal DB_USER=postgres DB_PASSWORD=testpass
    python3 tests/manual/phase1_e2e.py <court_id>
"""

import sys
import tempfile
from pathlib import Path

import pymupdf

from adapters.base import RawJudgmentRecord
from db.connection import get_pooled_connection, init_connection_pool
from orchestrator import batch_runner


def make_fake_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "IN THE SUPREME COURT OF INDIA\n"
        "CRIMINAL APPELLATE JURISDICTION\n"
        "CRIMINAL APPEAL NO. 1234 OF 2026\n\n"
        "RAM KUMAR ... PETITIONER\n"
        "VERSUS\n"
        "STATE OF UP ... RESPONDENT\n\n"
        "CORAM: HON'BLE MR. JUSTICE B.V. NAGARATHNA\n\n"
        "JUDGMENT\n"
        "Pronounced on: 15-01-2026\n"
        "The appeal is allowed and the conviction is set aside.",
        fontsize=11,
    )
    doc.save(str(path))
    doc.close()


class FakeAdapter:
    def __init__(self, pdf_path: Path):
        self.pdf_path = pdf_path

    def scrape(self, date_from, date_to, **kwargs):
        yield RawJudgmentRecord(
            pdf_path=self.pdf_path,
            source_url="https://sci.gov.in/fake-test-record.pdf",
            case_number_raw="Crl.A. No.-001234 - 2026",
            party_name_raw="RAM KUMAR VS STATE OF UP",
            judge_raw="HON'BLE MR. JUSTICE B.V. NAGARATHNA",
            decision_date_raw="15-01-2026",
            cnr_raw="SC0012342026",
            neutral_citation_raw="2026 INSC 9999",
        )


def main():
    court_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    init_connection_pool()

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "fake_judgment.pdf"
        make_fake_pdf(pdf_path)
        print(f"Generated fake PDF at {pdf_path} ({pdf_path.stat().st_size} bytes)")

        adapter = FakeAdapter(pdf_path)

        print("\n--- First run (expect: 1 found, 1 downloaded, 1 promoted) ---")
        summary1 = batch_runner.run_batch(adapter, court_id, "SCIN", "2026-01-01", "2026-01-31", "SCI_WEBSITE")
        print(summary1)
        assert summary1["total_found"] == 1
        assert summary1["total_downloaded"] == 1
        assert summary1["total_promoted"] == 1, "expected the fake record to promote cleanly"

        print("\n--- Second run, same PDF (expect: 1 found, 0 downloaded — checksum dedup) ---")
        summary2 = batch_runner.run_batch(adapter, court_id, "SCIN", "2026-01-01", "2026-01-31", "SCI_WEBSITE")
        print(summary2)
        assert summary2["total_found"] == 1
        assert summary2["total_downloaded"] == 0, "checksum dedup should have skipped this PDF"
        assert summary2["total_skipped_duplicate"] == 1

    print("\n--- Verifying promoted rows in Postgres ---")
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT document_id, judgment_date, neutral_citation, needs_review, extraction_model, data_source FROM documents;")
            doc_row = cur.fetchone()
            print("documents row:", doc_row)
            assert doc_row is not None, "no documents row was created"
            document_id = doc_row[0]
            assert doc_row[5] == "SCI_WEBSITE", f"data_source should be SCI_WEBSITE for the Supreme Court adapter, got {doc_row[5]!r}"

            cur.execute("SELECT case_id, case_number, cnr_number FROM cases WHERE document_id = %s;", (document_id,))
            case_row = cur.fetchone()
            print("cases row:", case_row)
            assert case_row is not None, "no cases row was created"
            case_id = case_row[0]

            cur.execute("SELECT party_name, party_side FROM parties WHERE case_id = %s ORDER BY party_order;", (case_id,))
            party_rows = cur.fetchall()
            print("parties rows:", party_rows)
            assert len(party_rows) == 2, f"expected 2 parties, got {len(party_rows)}"
            assert party_rows[0][1] == "PETITIONER_SIDE"
            assert party_rows[1][1] == "RESPONDENT_SIDE"

            cur.execute("""
                SELECT j.full_name, dc.is_author FROM document_coram dc
                JOIN judges j ON j.judge_id = dc.judge_id WHERE dc.document_id = %s;
            """, (document_id,))
            coram_rows = cur.fetchall()
            print("coram rows:", coram_rows)
            assert len(coram_rows) == 1

            cur.execute("SELECT status FROM raw_ingestions WHERE document_id = %s;", (document_id,))
            status = cur.fetchone()[0]
            print("raw_ingestions status:", status)
            assert status == "PROMOTED"

    print("\nALL PHASE 1 END-TO-END CHECKS PASSED")


if __name__ == "__main__":
    main()
