"""
Manual demo — NOT a pytest suite, run directly. Prints every field
pipeline/regex_extraction.py can produce WITHOUT calling the LLM, so it's
easy to eyeball how much of a real judgment's metadata that module covers
on its own.

Two sections:

1. TABLE-CELL FIELDS — sourced from the old scraper's saved JSON output
   (SUPREME_COURT_OF_INDIA_SCRAPER/supreme_court_judgments.json), which
   already carries a "party_name" cell in the exact "X VS Y" combined form
   the real results table exposes. Its "advocate"/"decision_date"/
   "neutral_citation" fields are null/single-value in this particular saved
   batch (an older scrape, from before the site started combining date +
   citation into one "Judgment" cell) -- those two are instead demoed
   against the literal example string confirmed live against the real site
   2026-09-06 (see adapters/supreme_court/adapter.py's own module
   docstring), clearly labeled as such below rather than presented as if
   pulled from this run's data.

2. OCR-TEXT FIELDS — sourced from real judgment PDFs already sitting in
   that same old scraper's pdf/ directory (its own genuine scrape output,
   not synthesized for this demo). Text is pulled via
   pipeline/ocr.py's real extract_text_from_pdf_bytes(), the same function
   the actual pipeline uses.

Run from the scraper-backend directory:

    python3 tests/manual/regex_extraction_demo.py [N]

N (default 5) is how many PDFs to show OCR-text fields for.
"""

import json
import sys
from pathlib import Path

# Defensive: works whether invoked as `python3 tests/manual_...py` (cwd
# somewhere else) or from the scraper-backend root as the existing
# manual_phase*.py scripts assume.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pymupdf  # noqa: E402

from pipeline import regex_extraction as rx  # noqa: E402
from pipeline.ocr import extract_text_from_pdf_bytes  # noqa: E402

_OLD_SCRAPER_DIR = _REPO_ROOT.parent / "scraper-backend" / "app" / "SUPREME_COURT_OF_INDIA_SCRAPER"
_JUDGMENTS_JSON = _OLD_SCRAPER_DIR / "supreme_court_judgments.json"
_PDF_DIR = _OLD_SCRAPER_DIR / "pdf"

_VERIFIED_JUDGMENT_CELL_EXAMPLE = "06-01-2026(English) 2026 INSC 15(English)"


def _print_header(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def demo_table_cell_fields(limit: int = 3) -> None:
    _print_header("SECTION 1 — TABLE-CELL FIELDS (no OCR, no LLM)")

    if not _JUDGMENTS_JSON.exists():
        print(f"  (skipped — {_JUDGMENTS_JSON} not found)")
        return

    records = json.loads(_JUDGMENTS_JSON.read_text())["Supreme Court of India"]

    for record in records[:limit]:
        print(f"\n--- Case Number: {record.get('case_number')} ---")
        print(f"  case_number_raw       : {record.get('case_number')}")
        print(f"  diary_number_raw      : {record.get('diary_number')}")
        print(f"  bench_raw             : {record.get('bench')}")
        print(f"  judgment_by_raw       : {record.get('judge')}")

        parties = rx.parse_party_names(record.get("party_name"))
        print(f"  petitioner (regex)    : {parties['petitioner']}")
        print(f"  respondent (regex)    : {parties['respondent']}")

        advocates = rx.parse_advocates(record.get("advocate"))
        print(f"  petitioner_advocate   : {advocates['petitioner_advocate']}")
        print(f"  respondent_advocate   : {advocates['respondent_advocate']}"
              f"  (this batch's advocate cell has no '-' — most real rows "
              f"only carry the petitioner side anyway, per the user's own "
              f"observation)")

    print(f"\n--- Judgment-cell parsing (date + neutral citation + language) ---")
    print(f"  This saved batch has decision_date/neutral_citation = null for every")
    print(f"  record (an older scrape, predating the combined 'Judgment' cell).")
    print(f"  Demoing parse_judgment_cell() against the real format confirmed live")
    print(f"  2026-09-06 instead (adapters/supreme_court/adapter.py docstring):")
    print(f"    raw input  : {_VERIFIED_JUDGMENT_CELL_EXAMPLE!r}")
    print(f"    parsed     : {rx.parse_judgment_cell(_VERIFIED_JUDGMENT_CELL_EXAMPLE)}")


def demo_ocr_text_fields(limit: int = 5) -> None:
    _print_header(f"SECTION 2 — OCR-TEXT FIELDS (real PDFs, no LLM) — {limit} documents")

    if not _PDF_DIR.exists():
        print(f"  (skipped — {_PDF_DIR} not found)")
        return

    pdf_paths = sorted(_PDF_DIR.glob("*.pdf"))[:limit]
    if not pdf_paths:
        print(f"  (no PDFs found in {_PDF_DIR})")
        return

    for pdf_path in pdf_paths:
        pdf_bytes = pdf_path.read_bytes()
        ocr_text = extract_text_from_pdf_bytes(pdf_bytes)
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            actual_page_count = doc.page_count

        disposition = rx.extract_disposition(ocr_text)
        provisions = rx.extract_provisions(ocr_text)
        page_check = rx.validate_page_count(ocr_text, actual_page_count)
        facts = rx.extract_facts(ocr_text)
        conclusion = rx.extract_conclusion(ocr_text)

        print(f"\n--- {pdf_path.name} ({actual_page_count} pages) ---")
        print(f"  subject (jurisdiction)    : {rx.extract_subject_from_jurisdiction(ocr_text)}")
        print(f"  disposition_category      : {disposition['disposition_category']}")
        print(f"  disposition_raw           : {disposition['disposition_raw']}")
        print(f"  provisions (Section/Act)  : {provisions if provisions else '[]'}")

        if facts:
            print(f"  facts (Factual Matrix)    : {facts[:120]}...")
        else:
            print("  facts (Factual Matrix)    : None (no literal heading in this document — expected for ~95% of judgments)")

        if conclusion:
            print(f"  conclusion (heading)      : {conclusion[:120]}...")
        else:
            print("  conclusion (heading)      : None (no literal heading in this document — expected for the large majority)")

        if page_check is None:
            print("  page_count_valid          : None (no 'Page N of M' marker present — expected for ~84% of judgments)")
        else:
            print(f"  page_count_valid          : {page_check}")


if __name__ == "__main__":
    ocr_limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    demo_table_cell_fields()
    demo_ocr_text_fields(limit=ocr_limit)
