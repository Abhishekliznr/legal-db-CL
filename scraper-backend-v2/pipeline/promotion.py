"""
EXTRACTED -> PROMOTED (spec §5.2/§5.3): normalizes the raw_ai_extraction
JSON envelope into documents/cases/parties/document_coram/document_sections/
citations.

Reads only the envelope shape defined in pipeline/extraction.py (and its
Phase 1 fallback, pipeline/extraction_stub.py) — this module doesn't know or
care whether that envelope came from the stub or the real LLM call, which
is the whole point of keeping the envelope shape stable across that swap.

Phase 3 adds what Phase 1 deliberately left out: real judge/party name
cleaning (normalization/ package, replacing the Phase 1 version's inline
uppercase-and-strip), provisions -> statutes/sections/document_sections,
citations -> the citations table (cited_document_id resolution stays a
separate periodic job, pipeline/citator.py's reconcile_citations() — a
cited case may not exist in this database yet at promotion time), and the
overruled-keyword flag on `documents`.
"""

import re
from datetime import datetime
from typing import Any, Dict, Optional

from db import scrape_jobs
from db.connection import get_pooled_connection
from normalization import acts, judges, parties
from pipeline import citator

_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %B %Y", "%d %b %Y")

# Exact allowed values for the enum columns an LLM response feeds directly —
# an LLM can return a string that's close-but-not-exact ("overruled" instead
# of "Overruled", or inventing a category outside the enum entirely), and
# Postgres has zero tolerance for that on an enum column. Validate to NULL
# rather than letting one bad value 500 the whole promotion.
_VALID_TREATMENTS = {
    "Overruled", "Affirmed", "Distinguished", "Followed",
    "Referred", "Relied Upon", "Explained", "Doubted",
}
_VALID_DISPOSITION_CATEGORIES = {
    "Allowed", "Dismissed", "Partly Allowed", "Disposed",
    "Remanded", "Withdrawn", "Quashed", "Set Aside", "Other",
}
_VALID_PARTY_SIDES = {"PETITIONER_SIDE", "RESPONDENT_SIDE"}


def _validate_enum(value: Optional[str], allowed: set) -> Optional[str]:
    return value if value in allowed else None


def _parse_date(value: Optional[str]):
    if not value:
        return None
    cleaned = re.sub(r"(\d{1,2})(st|nd|rd|th)", r"\1", value, flags=re.IGNORECASE).replace(",", "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _get_or_create_judge(cur, raw_name: str) -> int:
    cleaned = judges.clean_judge_name(raw_name) or raw_name.strip().upper()
    cur.execute("SELECT judge_id FROM judges WHERE normalized_name = %s;", (cleaned,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO judges (full_name, normalized_name) VALUES (%s, %s) RETURNING judge_id;",
        (cleaned, cleaned),
    )
    return cur.fetchone()[0]


def _get_or_create_statute(cur, raw_act_name: str) -> int:
    name, short_code, year = acts.resolve_act(raw_act_name)
    cur.execute("SELECT statute_id FROM statutes WHERE statute_name = %s AND (statute_year = %s OR (statute_year IS NULL AND %s IS NULL));",
                (name, year, year))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO statutes (statute_name, statute_year, short_code) VALUES (%s, %s, %s) RETURNING statute_id;",
        (name, year, short_code),
    )
    return cur.fetchone()[0]


def _get_or_create_section(cur, statute_id: int, section_number: str) -> int:
    cur.execute("SELECT section_id FROM sections WHERE statute_id = %s AND section_number = %s;", (statute_id, section_number))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO sections (statute_id, section_number) VALUES (%s, %s) RETURNING section_id;",
        (statute_id, section_number),
    )
    return cur.fetchone()[0]


class PromotionSkipped(Exception):
    """Raised when an ingestion can't be promoted as-is (missing a required field) — row goes to NEEDS_REVIEW, not a crash."""


def promote_ingestion(ingestion_id: int) -> Optional[int]:
    """
    Promotes one EXTRACTED raw_ingestions row into documents/cases/parties/
    document_coram/document_sections/citations. Returns the new document_id,
    or None if the row was routed to NEEDS_REVIEW instead (missing
    judgment_date or pdf reference — both NOT NULL on `documents`).
    """
    ingestion = scrape_jobs.get_ingestion(ingestion_id)
    if ingestion is None:
        raise ValueError(f"No raw_ingestions row with ingestion_id={ingestion_id}")

    extraction: Dict[str, Any] = ingestion["raw_ai_extraction"] or {}
    ocr_text = ingestion["ocr_text"] or ""
    pdf_url = ingestion["blob_path"] or ingestion["source_url"]

    try:
        raw_date = extraction.get("judgment_date_raw")
        judgment_date = _parse_date(raw_date)
        if judgment_date is None:
            raise PromotionSkipped(f"judgment_date could not be parsed from {raw_date!r} — required NOT NULL on documents")
        if not pdf_url:
            raise PromotionSkipped("no pdf_url available (neither blob_path nor source_url) — required NOT NULL on documents")

        extractor = extraction.get("extractor", "")
        needs_review = not extractor.startswith("llm")  # non-LLM extractions (the stub) always need a human look

        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO documents (
                        ingestion_id, court_id, data_source, doc_type, decision_type,
                        neutral_citation, judgment_date, case_note_ai,
                        disposition_category, favoring_party_side, overruled_keyword_present,
                        pdf_url, ocr_text, extraction_model, needs_review
                    ) VALUES (
                        %s, %s, %s, 'CaseLaw', 'Judgment',
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s
                    ) RETURNING document_id;
                """, (
                    ingestion_id, ingestion["court_id"], ingestion["data_source"],
                    extraction.get("neutral_citation"), judgment_date, extraction.get("case_note_ai"),
                    _validate_enum(extraction.get("disposition_category"), _VALID_DISPOSITION_CATEGORIES),
                    _validate_enum(extraction.get("favoring_party_side"), _VALID_PARTY_SIDES),
                    citator.has_overruled_keyword(ocr_text),
                    pdf_url, ocr_text, extractor, needs_review,
                ))
                document_id = cur.fetchone()[0]

                for case in extraction.get("cases", []):
                    case_number = case.get("case_number")
                    if not case_number:
                        continue  # cases.case_number is NOT NULL — skip rather than fabricate one

                    cur.execute("""
                        INSERT INTO cases (document_id, court_id, cnr_number, case_number)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (court_id, case_number) DO NOTHING
                        RETURNING case_id;
                    """, (document_id, ingestion["court_id"], case.get("cnr_number"), case_number))
                    case_row = cur.fetchone()
                    if case_row is None:
                        continue  # already existed from a prior run — leave its parties alone
                    case_id = case_row[0]

                    for order, party in enumerate(case.get("parties", []), start=1):
                        party_side = _validate_enum(party.get("side"), _VALID_PARTY_SIDES)
                        if not party.get("name") or not party_side:
                            continue
                        cleaned_name = parties.clean_party_name(party["name"]) or party["name"].strip().upper()
                        cur.execute("""
                            INSERT INTO parties (case_id, party_name, party_side, party_order)
                            VALUES (%s, %s, %s, %s);
                        """, (case_id, cleaned_name, party_side, order))

                for bench_order, judge in enumerate(extraction.get("coram", []), start=1):
                    if not judge.get("name"):
                        continue
                    judge_id = _get_or_create_judge(cur, judge["name"])
                    cur.execute("""
                        INSERT INTO document_coram (document_id, judge_id, is_author, bench_order)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (document_id, judge_id) DO NOTHING;
                    """, (document_id, judge_id, judge.get("is_author", False), bench_order))

                for provision in extraction.get("provisions", []):
                    statute_name = provision.get("statute_name")
                    section_number = provision.get("section_number")
                    if not statute_name or not section_number:
                        continue
                    statute_id = _get_or_create_statute(cur, statute_name)
                    section_id = _get_or_create_section(cur, statute_id, str(section_number))
                    cur.execute("""
                        INSERT INTO document_sections (document_id, section_id)
                        VALUES (%s, %s)
                        ON CONFLICT (document_id, section_id) DO NOTHING;
                    """, (document_id, section_id))

                for citation in extraction.get("citations", []):
                    cited_case_name = citation.get("cited_case_name")
                    if not cited_case_name:
                        continue
                    cur.execute("""
                        INSERT INTO citations (citing_document_id, cited_case_name, cited_reporter_citation, treatment)
                        VALUES (%s, %s, %s, %s);
                    """, (
                        document_id, cited_case_name, citation.get("cited_reporter_citation"),
                        _validate_enum(citation.get("treatment"), _VALID_TREATMENTS),
                    ))

            conn.commit()

        scrape_jobs.update_status(ingestion_id, status="PROMOTED", document_id=document_id)
        return document_id

    except PromotionSkipped as e:
        scrape_jobs.update_status(ingestion_id, status="NEEDS_REVIEW", extraction_error=str(e))
        return None
