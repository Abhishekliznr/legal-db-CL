"""
One-off data population for Phase 4 interop verification — NOT part of the
test suite proper. Pushes 3 varied documents (different courts, provisions,
citations, a judge shared across two of them) through the real orchestrator
pipeline with mocked LLM extraction, so api-backend's routers have
something real to search/filter/aggregate against.

    python3 -m tests.populate_for_phase4
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pymupdf

from adapters.base import RawJudgmentRecord
from db.connection import get_pooled_connection, init_connection_pool
from orchestrator import batch_runner


def _make_pdf(path: Path, text: str) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=10)
    doc.save(str(path))
    doc.close()


def _court_id_for(name: str) -> int:
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT court_id FROM courts WHERE court_name = %s;", (name,))
            return cur.fetchone()[0]


_CASES = [
    {
        "court": "Supreme Court of India",
        "pdf_text": "SUPREME COURT judgment on arbitration.",
        "neutral_citation": "2026 INSC 100",  # so another case in this corpus can cite-and-overrule it (see Delhi HC below)
        "llm_result": {
            "case_note_ai": "The Supreme Court examined the scope of Section 34 of the Arbitration Act and dismissed the appeal.",
            "cases": [{"case_number": "C.A. No.-100 - 2026", "cnr_number": "SC0001002026",
                       "parties": [{"name": "Ajay Traders", "side": "PETITIONER_SIDE"}, {"name": "Union of India", "side": "RESPONDENT_SIDE"}]}],
            "coram": [{"name": "Justice B.V. Nagarathna", "is_author": True}],
            "provisions": [{"statute_name": "Arbitration and Conciliation Act", "section_number": "34"}],
            "citations": [{"cited_case_name": "Old Case v. State", "cited_reporter_citation": "(2019) 3 SCC 55", "treatment": "Followed"}],
            "disposition_category": "Dismissed",
            "favoring_party_side": "RESPONDENT_SIDE",
            "date_of_judgment": "2026-01-10",
        },
    },
    {
        "court": "Delhi High Court",
        "pdf_text": "DELHI HIGH COURT judgment on criminal appeal.",
        "llm_result": {
            "case_note_ai": "The Delhi High Court allowed the criminal appeal under Section 482 CrPC, quashing the FIR, and held the Supreme Court's arbitration ruling in C.A. No. 100 no longer good law.",
            "cases": [{"case_number": "Crl.A. No.-200 - 2026", "cnr_number": "DL0002002026",
                       "parties": [{"name": "Ravi Kumar", "side": "PETITIONER_SIDE"}, {"name": "State of NCT of Delhi", "side": "RESPONDENT_SIDE"}]}],
            "coram": [{"name": "Justice B.V. Nagarathna", "is_author": False}],
            "provisions": [{"statute_name": "Code of Criminal Procedure", "section_number": "482"}],
            # Deliberately cites the Supreme Court case above (by its neutral_citation) with
            # treatment=Overruled, so pipeline.citator.reconcile_citations() has something real
            # to resolve cited_document_id against, and case_search_view's computed
            # treatment_status has a genuine in-corpus OVERRULED case to compute (§5.3).
            "citations": [{"cited_case_name": "Ajay Traders v. Union of India", "cited_reporter_citation": "2026 INSC 100", "treatment": "Overruled"}],
            "disposition_category": "Allowed",
            "favoring_party_side": "PETITIONER_SIDE",
            "date_of_judgment": "2026-02-15",
        },
    },
    {
        "court": "Bombay High Court",
        "pdf_text": "BOMBAY HIGH COURT judgment on property dispute.",
        "llm_result": {
            "case_note_ai": "The Bombay High Court set aside the trial court's decree concerning a property partition dispute.",
            "cases": [{"case_number": "C.S. No.-300 - 2026", "cnr_number": "MH0003002026",
                       "parties": [{"name": "Sunita Sharma", "side": "PETITIONER_SIDE"}, {"name": "Prakash Sharma", "side": "RESPONDENT_SIDE"}]}],
            "coram": [{"name": "Justice Anil Deshmukh", "is_author": True}],
            "provisions": [{"statute_name": "Code of Civil Procedure", "section_number": "96"}],
            "citations": [{"cited_case_name": "Prior Partition Case", "cited_reporter_citation": "(2020) 2 SCC 200", "treatment": "Overruled"}],
            "disposition_category": "Set Aside",
            "favoring_party_side": "PETITIONER_SIDE",
            "date_of_judgment": "2026-03-20",
        },
    },
]


class OneRecordAdapter:
    def __init__(self, pdf_path, case_number, neutral_citation=None):
        self.pdf_path = pdf_path
        self.case_number = case_number
        self.neutral_citation = neutral_citation

    def scrape(self, date_from, date_to, **kwargs):
        yield RawJudgmentRecord(
            pdf_path=self.pdf_path, source_url=f"https://test/{self.case_number}.pdf",
            neutral_citation_raw=self.neutral_citation,
        )


def main():
    init_connection_pool()

    with tempfile.TemporaryDirectory() as tmp:
        for i, case in enumerate(_CASES):
            pdf_path = Path(tmp) / f"case_{i}.pdf"
            _make_pdf(pdf_path, case["pdf_text"])
            court_id = _court_id_for(case["court"])

            data_source = "SCI_WEBSITE" if case["court"] == "Supreme Court of India" else "ECOURTS"
            with patch("pipeline.extraction.call_llm_extraction", return_value=case["llm_result"]):
                import os
                os.environ["AZURE_OPENAI_ENDPOINT"] = "https://test-fake-resource.openai.azure.com"
                os.environ["AZURE_OPENAI_API_KEY"] = "test-fake-key"
                os.environ["AZURE_OPENAI_DEPLOYMENT"] = "test-fake-deployment"
                summary = batch_runner.run_batch(
                    OneRecordAdapter(pdf_path, case["llm_result"]["cases"][0]["case_number"], case.get("neutral_citation")),
                    court_id, case["court"][:4].upper(), "2026-01-01", "2026-12-31", data_source,
                )
            print(f"{case['court']}: {summary}")

    from pipeline import citator
    resolved = citator.reconcile_citations()
    print(f"\nreconcile_citations(): resolved {resolved} citation(s) to an in-corpus document.")

    print("\nPopulation complete.")


if __name__ == "__main__":
    main()
