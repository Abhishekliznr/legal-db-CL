"""
Manual end-to-end check for adapters/high_courts/mp/promotion.py — NOT a
pytest suite, run directly.

Pushes two synthetic-but-realistic RawJudgmentRecords (shaped like the real
saved case-status response for CRA 6641/2024 — parties, advocates with
enrollment numbers, duplicated Act lines) through the actual OCR ->
promotion path against a real local Postgres, confirming:
  1. an advocate shared across both cases (by enrollment_no) gets exactly
     one cr_advocates row, not two;
  2. a duplicated Act line within one case's own `extra["acts"]` doesn't
     produce duplicate cr_acts/cr_sections rows.

Run against a real (ideally throwaway) Postgres with schema.sql already applied:

    export DATABASE_TYPE=postgres DB_HOST=localhost DB_PORT=5433 \\
           DB_NAME=<throwaway_db> DB_USER=postgres DB_PASSWORD=x
    python3 tests/manual/mp_promotion_e2e.py
"""

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pymupdf

from adapters.base import RawJudgmentRecord
from adapters.high_courts.mp.promotion import promote_ingestion
from db import scrape_jobs
from db.connection import get_pooled_connection, init_connection_pool
from pipeline import ocr

_FAILURES = []


def _check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        _FAILURES.append(label)


def _make_fake_pdf(path: Path, text: str) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=11)
    doc.save(str(path))
    doc.close()


# Same act line, repeated -- matches the real saved page's own data quirk
# (the identical "502 - Indian Penal Code (Section - ...)" line six times).
_DUPLICATED_ACT_LINE = {
    "act_code": "502", "act_name": "Indian Penal Code", "sections": ["420", "409"],
}

_SHARED_ADVOCATE = {"name": "MANOJ CHATURVEDI", "role": "P-1", "enrollment_no": "3258", "enrollment_year": 1996}


def _record(court_dir: Path, case_no: str, extra_overrides: dict) -> RawJudgmentRecord:
    pdf_path = court_dir / f"{case_no.replace('/', '_')}.pdf"
    _make_fake_pdf(pdf_path, f"IN THE HIGH COURT OF MADHYA PRADESH\nCRIMINAL APPEAL {case_no}\nJUDGEMENT\nThe appeal is allowed.")
    extra = {
        "petitioners": [{"name": "ABHINAV DUBEY", "relation": None, "address": None}],
        "respondents": [{"name": "THE STATE OF MADHYA PRADESH", "relation": None, "address": None}],
        "petitioner_advocates": [_SHARED_ADVOCATE],
        "respondent_advocates": [{"name": "ADVOCATE GENERAL", "role": None, "enrollment_no": "7777", "enrollment_year": 2014}],
        "acts": [_DUPLICATED_ACT_LINE, dict(_DUPLICATED_ACT_LINE), dict(_DUPLICATED_ACT_LINE)],
        "disposition_type": "Allowed",
        "neutral_citation": f"2025:MPHC-JBP:{case_no.replace('/', '')}",
        "headnote": "Criminal - Bail - Appeal allowed",
        "registration_year": 2024,
    }
    extra.update(extra_overrides)
    return RawJudgmentRecord(
        pdf_path=pdf_path,
        source_url=f"https://mphc.gov.in/fake-judgment-{case_no.replace('/', '-')}.pdf",
        case_number_raw=case_no,
        decision_date_raw="18-11-2025",
        extra=extra,
    )


def main():
    init_connection_pool()

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cr_courts (court_name, court_type, court_code) VALUES "
                "('Madhya Pradesh High Court', 'High Court', 'MPHC') "
                "ON CONFLICT (court_name) DO UPDATE SET court_type = EXCLUDED.court_type "
                "RETURNING court_id;"
            )
            court_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO cr_scrape_batches (court_id, date_from, date_to) VALUES (%s, '2026-01-01', '2026-01-02') RETURNING batch_id;",
                (court_id,),
            )
            batch_id = cur.fetchone()[0]
        conn.commit()

    with tempfile.TemporaryDirectory(prefix="mp_promotion_e2e_") as tmp:
        court_dir = Path(tmp)
        records = [
            _record(court_dir, "CRA 6641/2024", {}),
            _record(court_dir, "CRA 6642/2024", {}),  # same shared advocate, same duplicated act line
        ]

        case_ids = []
        for record in records:
            with get_pooled_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO cr_raw_ingestions (batch_id, court_id, source_pdf_url, data_source, status) "
                        "VALUES (%s, %s, %s, 'MPHC_WEBSITE', 'DOWNLOADED') RETURNING ingestion_id;",
                        (batch_id, court_id, record.source_url),
                    )
                    ingestion_id = cur.fetchone()[0]
                conn.commit()

            ocr.process_ingestion_ocr(ingestion_id, record.pdf_path)
            ingestion = scrape_jobs.get_ingestion(ingestion_id)
            _check(f"ingestion {ingestion_id}: OCR_DONE", ingestion["status"] == "OCR_DONE")

            case_id = promote_ingestion(ingestion_id, record)
            _check(f"ingestion {ingestion_id}: promoted", case_id is not None)
            case_ids.append(case_id)

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT petitioner_advocate_ids, respondent_advocate_ids, acts, sections, disposition "
                "FROM cr_cases WHERE case_id = ANY(%s) ORDER BY case_id;",
                (case_ids,),
            )
            rows = cur.fetchall()

            cur.execute("SELECT advocate_id FROM cr_advocates WHERE enrollment_no = '3258';")
            shared_advocate_rows = cur.fetchall()

    _check("exactly one cr_advocates row for the shared enrollment_no across both cases", len(shared_advocate_rows) == 1)

    pet_ids_case1, resp_ids_case1, acts_case1, sections_case1, disposition_case1 = rows[0]
    pet_ids_case2, _, acts_case2, sections_case2, _ = rows[1]

    _check("case 1 petitioner_advocate_ids has exactly 1 entry", len(pet_ids_case1) == 1)
    _check("case 1 and case 2 share the same advocate_id for the shared advocate", pet_ids_case1 == pet_ids_case2)
    _check("case 1 disposition == 'Allowed'", disposition_case1 == "Allowed")
    _check("case 1 acts[] has exactly 1 entry despite 3 duplicated Act lines in extra", len(acts_case1) == 1)
    _check("case 1 sections[] has exactly 2 entries (420, 409), not 6", len(sections_case1) == 2)
    _check("case 2 acts[] resolves to the SAME act_id as case 1 (same act, different case)", acts_case1 == acts_case2)

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} check(s) failed:")
        for f in _FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
