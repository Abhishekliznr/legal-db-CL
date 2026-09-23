"""
Manual end-to-end check for the rewritten (2026-09-08) flattened `cases`
schema + regex-only adapters/supreme_court/promotion.py — NOT a pytest suite, run directly.

Pushes several REAL judgment PDFs (from the old scraper's own
SUPREME_COURT_OF_INDIA_SCRAPER/pdf/ output, already used throughout the
extraction.py verification) through the actual OCR -> promotion path
against a real local Postgres, using synthetic-but-realistic RawJudgmentRecord
table-cell values (real scraped party/bench/date/citation strings couldn't
be re-obtained for this exact batch of PDFs — the OCR-text side is 100%
real; only the table-cell inputs below are constructed to match the
confirmed real column formats).

Run against a real Postgres with schema.sql already applied:

    export DATABASE_TYPE=postgres DB_HOST=localhost DB_PORT=5432 \\
           DB_NAME=<db> DB_USER=<user> DB_PASSWORD=<password>
    python3 tests/manual/promotion_e2e.py
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from adapters.base import RawJudgmentRecord
from adapters.supreme_court import promotion
from db import scrape_jobs
from db.connection import get_pooled_connection, init_connection_pool
from pipeline import ocr

_OLD_SCRAPER_DIR = _REPO_ROOT.parent / "scraper-backend" / "app" / "SUPREME_COURT_OF_INDIA_SCRAPER"
_PDF_DIR = _OLD_SCRAPER_DIR / "pdf"

# (pdf_filename, case_number_raw, party_name_raw, bench_raw, judge_raw,
#  decision_date_raw, neutral_citation_raw, advocate_raw, language)
_FIXTURES = [
    (
        "31329_2010_Crl.A._No.-001730-001730_-_2015_None.pdf",
        "Criminal Appeal No. 1730 of 2015",
        "PARDESHIRAM VS STATE OF M.P. (NOW CHHATTISGARH)",
        "HON'BLE MR. JUSTICE HEMANT GUPTA HON'BLE MR. JUSTICE S. RAVINDRA BHAT",
        "HON'BLE MR. JUSTICE HEMANT GUPTA",
        "09-02-2015",
        None,
        "Sanjay R. Hegde",
        "English",
    ),
    (
        "15864_2009_C.A._No.-001919-001922_-_2016_None.pdf",
        "Civil Appeal Nos. 1919-1922 of 2016",
        "REVENUE DIVISIONAL OFFICER, CHEVELLA DIVISION & ORS. VS MOHD. SYEED ATHER & ORS.",
        "HON'BLE MR. JUSTICE C.T. RAVIKUMAR",
        "HON'BLE MR. JUSTICE C.T. RAVIKUMAR",
        "02-01-2025",
        "2025 INSC 5",
        None,
        "English",
    ),
    (
        "10067_2024_Crl.A._No.-000011-000011_-_2025_None.pdf",
        "Criminal Appeal No. 11 of 2025",
        "UNION OF INDIA THROUGH MINISTRY OF RAILWAYS VS SOME COMPANY LTD.",
        "HON'BLE MR. JUSTICE J.B. PARDIWALA HON'BLE MR. JUSTICE R. MAHADEVAN",
        "HON'BLE MR. JUSTICE J.B. PARDIWALA",
        "02-01-2025",
        "2025 INSC 11",
        "Some Advocate - Another Advocate",
        "English",
    ),
]


def main() -> None:
    init_connection_pool()

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cr_courts (court_name, court_type, court_code)
                VALUES ('Supreme Court of India', 'Supreme Court', 'SCIN')
                ON CONFLICT (court_name) DO UPDATE SET court_name = EXCLUDED.court_name
                RETURNING court_id;
            """)
            court_id = cur.fetchone()[0]
        conn.commit()

    batch_id = scrape_jobs.create_batch(court_id, __import__("datetime").date(2025, 1, 1), __import__("datetime").date(2025, 1, 31))
    print(f"court_id={court_id} batch_id={batch_id}\n")

    case_ids = []
    for (filename, case_number_raw, party_name_raw, bench_raw, judge_raw,
         decision_date_raw, neutral_citation_raw, advocate_raw, language) in _FIXTURES:
        pdf_path = _PDF_DIR / filename
        if not pdf_path.exists():
            print(f"SKIP (missing file): {filename}")
            continue

        checksum = f"testfixture-{filename}"
        ingestion_id = scrape_jobs.insert_raw_ingestion(
            batch_id=batch_id,
            court_id=court_id,
            source_pdf_url=f"https://www.sci.gov.in/fake/{filename}",
            file_checksum=checksum,
            data_source="SCI_WEBSITE",
            # A bare path within the blob container, matching what
            # storage/azure_blob.py's real upload_pdf() actually returns
            # since 2026-09-08 (not a full URL — see that module's
            # docstring) -- "SCIN/<checksum>.pdf", same shape the real
            # orchestrator/batch_runner.py would produce.
            blob_pdf_id=f"SCIN/{checksum}.pdf",
        )
        if ingestion_id is None:
            print(f"Already ingested (checksum dedup) — looking up existing ingestion for {filename}")
            with get_pooled_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT ingestion_id FROM cr_raw_ingestions WHERE file_checksum = %s;", (checksum,))
                    ingestion_id = cur.fetchone()[0]

        record = RawJudgmentRecord(
            pdf_path=pdf_path,
            source_url=f"https://www.sci.gov.in/fake/{filename}",
            case_number_raw=case_number_raw,
            party_name_raw=party_name_raw,
            judge_raw=judge_raw,
            decision_date_raw=decision_date_raw,
            cnr_raw=None,
            neutral_citation_raw=neutral_citation_raw,
            extra={"advocate_raw": advocate_raw, "bench_raw": bench_raw, "language": language},
        )

        ocr.process_ingestion_ocr(ingestion_id, pdf_path)
        ingestion = scrape_jobs.get_ingestion(ingestion_id)
        print(f"{filename}: OCR status={ingestion['status']}, ocr_text length={len(ingestion['ocr_text'] or '')}")
        if ingestion["status"] != "OCR_DONE":
            continue

        case_id = promotion.promote_ingestion(ingestion_id, record)
        final_status = scrape_jobs.get_ingestion(ingestion_id)["status"]
        print(f"  -> promotion result: case_id={case_id}, ingestion status={final_status}")
        if case_id:
            case_ids.append(case_id)

    print("\n" + "=" * 90)
    print("PROMOTED CASES — full row dump")
    print("=" * 90)
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            for case_id in case_ids:
                cur.execute("SELECT * FROM cr_cases WHERE case_id = %s;", (case_id,))
                columns = [desc[0] for desc in cur.description]
                row = dict(zip(columns, cur.fetchone()))

                # Resolve array-of-id columns to human-readable names for the printout.
                cur.execute("SELECT full_name FROM cr_judges WHERE judge_id = ANY(%s);", (row["bench"],))
                bench_names = [r[0] for r in cur.fetchall()]
                judgment_by_name = None
                if row["judgment_by"]:
                    cur.execute("SELECT full_name FROM cr_judges WHERE judge_id = %s;", (row["judgment_by"],))
                    judgment_by_name = cur.fetchone()[0]
                cur.execute("SELECT act_name FROM cr_acts WHERE act_id = ANY(%s);", (row["acts"],))
                act_names = [r[0] for r in cur.fetchall()]
                cur.execute("SELECT section_number FROM cr_sections WHERE section_id = ANY(%s);", (row["sections"],))
                section_numbers = [r[0] for r in cur.fetchall()]
                subject_name = None
                if row["subject"]:
                    cur.execute("SELECT subject_name FROM cr_subjects WHERE subject_id = %s;", (row["subject"],))
                    subject_name = cur.fetchone()[0]
                cur.execute("SELECT ministry_name FROM cr_ministries WHERE ministry_id = ANY(%s);", (row["ministries"],))
                ministry_names = [r[0] for r in cur.fetchall()]
                cur.execute("SELECT category_name FROM cr_case_categories WHERE category_id = ANY(%s);", (row["case_category"],))
                category_names = [r[0] for r in cur.fetchall()]

                print(f"\n--- case_id={case_id} ---")
                print(f"  liznr_id          : {row['liznr_id']}")
                print(f"  case_number       : {row['case_number']}")
                print(f"  petitioner        : {row['petitioner']}")
                print(f"  respondent        : {row['respondent']}")
                print(f"  bench             : {bench_names}")
                print(f"  judgment_by       : {judgment_by_name}")
                print(f"  judgment_date     : {row['judgment_date']}")
                print(f"  language          : {row['language']}")
                print(f"  neutral_citation  : {row['neutral_citation']}")
                print(f"  acts              : {act_names}")
                print(f"  sections          : {section_numbers}")
                print(f"  subject           : {subject_name}")
                print(f"  ministries        : {ministry_names}")
                print(f"  disposition       : {row['disposition']}")
                print(f"  document_type     : {row['document_type']}")
                print(f"  case_category     : {category_names}")
                print(f"  needs_review      : {row['needs_review']}")
                print(f"  source_pdf_url    : {row['source_pdf_url']}")
                print(f"  blob_pdf_id      : {row['blob_pdf_id']}")
                print(f"  case_note (LLM)   : {row['case_note']}")
                print(f"  conclusion        : {(row['conclusion'] or '')[:100] or None}")
                print(f"  judgement (len)   : {len(row['judgement'] or '')}")
                print(f"  ocr_text (len)    : {len(row['ocr_text'] or '')}")

    # Re-run the exact same fixtures a second time to confirm checksum +
    # (court_id, case_number) dedup both correctly no-op instead of erroring
    # or duplicating -- the real thing a retried batch run needs.
    print("\n" + "=" * 90)
    print("RE-RUN (checksum + case_number dedup check)")
    print("=" * 90)
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM cr_cases;")
            count_before = cur.fetchone()[0]
    for (filename, case_number_raw, party_name_raw, bench_raw, judge_raw,
         decision_date_raw, neutral_citation_raw, advocate_raw, language) in _FIXTURES:
        pdf_path = _PDF_DIR / filename
        if not pdf_path.exists():
            continue
        checksum = f"testfixture-{filename}"
        ingestion_id = scrape_jobs.insert_raw_ingestion(
            batch_id=batch_id, court_id=court_id,
            source_pdf_url=f"https://www.sci.gov.in/fake/{filename}",
            file_checksum=checksum, data_source="SCI_WEBSITE",
        )
        print(f"{filename}: re-ingest returned ingestion_id={ingestion_id} (None = correctly deduped)")
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM cr_cases;")
            count_after = cur.fetchone()[0]
    print(f"cases count before={count_before}, after={count_after} (should be equal)")


if __name__ == "__main__":
    main()
