from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from adapters.base import RawJudgmentRecord
from db import scrape_jobs
from db.connection import get_pooled_connection
from db.lookups import (
    get_or_create_advocate,
    get_or_create_case_category,
    get_or_create_subject,
    resolve_act_entries,
    resolve_bench,
)
from normalization import parties
from normalization.dates import parse_date
from orchestrator import stages
from orchestrator.log_context import slog, tally
from pipeline.legal_ner_extraction import extract_acts_sections

logger = logging.getLogger("scraper_backend_v2.tshc_promotion")

_VALID_DISPOSITION_CATEGORIES = {
    "Allowed", "Dismissed", "Partly Allowed", "Disposed",
    "Remanded", "Withdrawn", "Quashed", "Set Aside", "Other",
}

LIZNR_ID_PREFIX = "LIZNR"


def _validate_enum(value: Optional[str], allowed: set) -> Optional[str]:
    return value if value in allowed else None


def _claim_citation_sequence(cur, court_id: int, year: int) -> int:
    cur.execute("""
        INSERT INTO cr_citation_sequences (court_id, citation_year, next_seq)
        VALUES (%s, %s, 1)
        ON CONFLICT (court_id, citation_year)
        DO UPDATE SET next_seq = cr_citation_sequences.next_seq + 1
        RETURNING next_seq;
    """, (court_id, year))
    return cur.fetchone()[0]


def _build_liznr_id(cur, court_id: int, year: int) -> Optional[str]:
    cur.execute("SELECT court_code FROM cr_courts WHERE court_id = %s;", (court_id,))
    row = cur.fetchone()
    court_code = row[0] if row else None
    if not court_code:
        return None
    seq = _claim_citation_sequence(cur, court_id, year)
    return f"{LIZNR_ID_PREFIX}/{court_code}/{seq:04d}/{year}"


def _assign_liznr_id(cur, case_id: int, court_id: int, year: int) -> Optional[str]:
    liznr_id = _build_liznr_id(cur, court_id, year)
    if liznr_id is not None:
        cur.execute("UPDATE cr_cases SET liznr_id = %s WHERE case_id = %s;", (liznr_id, case_id))
    return liznr_id


class PromotionSkipped(Exception):
    pass


_FILL_IN_JUDGMENT_SET = """
    cnr = COALESCE(EXCLUDED.cnr, cr_cases.cnr),
    petitioner = EXCLUDED.petitioner, respondent = EXCLUDED.respondent,
    petitioner_advocate_ids = EXCLUDED.petitioner_advocate_ids, respondent_advocate_ids = EXCLUDED.respondent_advocate_ids,
    filing_year = EXCLUDED.filing_year, bench = EXCLUDED.bench, judgment_by = EXCLUDED.judgment_by,
    judgment_date = EXCLUDED.judgment_date, language = EXCLUDED.language, neutral_citation = EXCLUDED.neutral_citation,
    sections = EXCLUDED.sections, acts = EXCLUDED.acts, subject = EXCLUDED.subject,
    case_note = EXCLUDED.case_note, judgement = EXCLUDED.judgement, ocr_text = EXCLUDED.ocr_text,
    source_pdf_url = EXCLUDED.source_pdf_url, blob_pdf_id = EXCLUDED.blob_pdf_id,
    disposition = EXCLUDED.disposition, case_category = EXCLUDED.case_category,
    needs_review = EXCLUDED.needs_review,
    judgment_status = 'AVAILABLE', judgment_missing_reason = NULL, judgment_checked_at = now(),
    enrichment_status = 'PENDING', enrichment_error = NULL, updated_at = now()
"""

_STILL_MISSING_SET = """
    judgment_status = EXCLUDED.judgment_status, judgment_missing_reason = EXCLUDED.judgment_missing_reason,
    judgment_checked_at = now(), source_pdf_url = COALESCE(EXCLUDED.source_pdf_url, cr_cases.source_pdf_url),
    updated_at = now()
"""


def promote_ingestion(ingestion_id: int, record: RawJudgmentRecord) -> Optional[int]:
    ingestion = scrape_jobs.get_ingestion(ingestion_id)
    if ingestion is None:
        raise ValueError(f"No cr_raw_ingestions row with ingestion_id={ingestion_id}")

    try:
        return _save_case(
            record,
            ingestion["court_id"],
            ocr_text=ingestion["ocr_text"] or "",
            blob_pdf_id=ingestion["blob_pdf_id"],
            ingestion_id=ingestion_id,
        )
    except PromotionSkipped as e:
        slog(logger, stages.PROMOTE, "warning", "Not saved: %s → needs review", e)
        scrape_jobs.update_status(ingestion_id, status="NEEDS_REVIEW", error_message=str(e))
        return None
    except Exception as e:
        slog(logger, stages.PROMOTE, "exception", "Failed to save case (ingestion #%s)", ingestion_id)
        scrape_jobs.update_status(ingestion_id, status="PROMOTION_FAILED", error_message=str(e))
        return None


def save_case_without_judgment(
    court_id: int,
    record: RawJudgmentRecord,
    judgment_status: str,
    reason: str,
) -> Optional[int]:
    return _save_case(
        record,
        court_id,
        ocr_text="",
        blob_pdf_id=None,
        ingestion_id=None,
        judgment_missing=(judgment_status, reason),
    )


def _resolve_advocate_names(cur, names: List[str]) -> List[int]:
    ids = []
    for name in names:
        cleaned = (name or "").strip()
        if not cleaned:
            continue
        ids.append(get_or_create_advocate(cur, cleaned, enrollment_no=f"TSHC_{abs(hash(cleaned)) % 1000000}", enrollment_year=None))
    return ids



def _save_case(
    record: RawJudgmentRecord,
    court_id: int,
    ocr_text: str,
    blob_pdf_id: Optional[str],
    ingestion_id: Optional[int],
    judgment_missing: Optional[Tuple[str, str]] = None,
) -> Optional[int]:
    extra = record.extra or {}
    primary = extra.get("primary") or {}

    case_number = (record.case_number_raw or "").strip()
    if not case_number:
        raise PromotionSkipped("case_number_raw is missing/blank — required NOT NULL on cr_cases")

    decision_raw = record.decision_date_raw or primary.get("disposaldate") or primary.get("orderdate")
    judgment_date = parse_date(decision_raw)
    needs_review = judgment_date is None

    petitioner = parties.clean_party_name(primary.get("petitioner") or extra.get("title") or "")
    respondent = parties.clean_party_name(primary.get("respondent") or "")

    disposition_raw = primary.get("disposaltype") or ""
    disposition = _validate_enum(disposition_raw.capitalize(), _VALID_DISPOSITION_CATEGORIES)

    cnr = record.cnr_raw or primary.get("cnrno") or None
    language = "English"

    reg_date = parse_date(primary.get("registrationdate") or primary.get("filingdate"))
    filing_year = reg_date.year if reg_date else (extra.get("case_year") or (judgment_date.year if judgment_date else None))

    judgment_status, missing_reason = judgment_missing or ("AVAILABLE", None)

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            pet_adv_raw = [primary.get("petitioneradv")] if primary.get("petitioneradv") else []
            resp_adv_raw = [primary.get("respondentadv")] if primary.get("respondentadv") else []
            petitioner_advocate_ids = _resolve_advocate_names(cur, pet_adv_raw)
            respondent_advocate_ids = _resolve_advocate_names(cur, resp_adv_raw)

            act_ids, section_ids = [], []
            acts_source = "none"
            if ocr_text:
                act_ids, section_ids = resolve_act_entries(cur, extract_acts_sections(ocr_text))
                acts_source = "Legal NER" if act_ids else "none"

            case_type_code = extra.get("case_type") or "CASE"
            case_type_name = primary.get("casecategory") or case_type_code
            category_ids = [get_or_create_case_category(cur, case_type_code, case_type_name)]

            judges_raw = primary.get("judges") or ""
            judge_names = [j.strip() for j in judges_raw.split(",") if j.strip()] if judges_raw else []
            judge_ids = resolve_bench(cur, judge_names)
            judgment_by_id = judge_ids[0] if judge_ids else None

            subject_raw = primary.get("casecategory") or primary.get("district")
            subject_id = get_or_create_subject(cur, subject_raw) if subject_raw else None

            cur.execute(f"""
                INSERT INTO cr_cases (
                    liznr_id, court_id, case_number, cnr, petitioner, respondent,
                    petitioner_advocate_ids, respondent_advocate_ids, filing_year,
                    bench, judgment_by, judgment_date, language, neutral_citation,
                    sections, acts, subject,
                    case_note, judgement, ocr_text,
                    source_pdf_url, blob_pdf_id,
                    disposition, document_type, case_category,
                    needs_review,
                    judgment_status, judgment_missing_reason, judgment_checked_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, 'CaseLaw', %s,
                    %s,
                    %s, %s, now()
                )
                ON CONFLICT (court_id, case_number) DO UPDATE SET
                    {_STILL_MISSING_SET if judgment_missing else _FILL_IN_JUDGMENT_SET}
                WHERE cr_cases.judgment_status <> 'AVAILABLE'
                RETURNING case_id, liznr_id, (xmax = 0) AS inserted;
            """, (
                None, court_id, case_number, cnr, petitioner, respondent,
                petitioner_advocate_ids, respondent_advocate_ids, filing_year,
                judge_ids, judgment_by_id, judgment_date, language, record.neutral_citation_raw,
                section_ids, act_ids, subject_id,
                None, ocr_text or None, ocr_text or None,
                record.source_url, blob_pdf_id,
                disposition, category_ids,
                needs_review,
                judgment_status, missing_reason,
            ))
            row = cur.fetchone()
            if row is None:
                if ingestion_id is None:
                    conn.commit()
                    slog(logger, stages.PROMOTE, "info", "Case already in database with judgment · nothing to save")
                    return None
                scrape_jobs.update_status_in_tx(
                    cur, ingestion_id, status="NEEDS_REVIEW",
                    error_message="case_number already exists for this court — likely a re-run",
                )
                conn.commit()
                slog(logger, stages.PROMOTE, "warning", "Case already exists in database (re-run) → needs review")
                return None

            case_id, liznr_id, inserted = row

            if liznr_id is None and judgment_date is not None:
                liznr_id = _assign_liznr_id(cur, case_id, court_id, judgment_date.year)

            if ingestion_id is not None:
                scrape_jobs.update_status_in_tx(cur, ingestion_id, status="PROMOTED", case_id=case_id)

        conn.commit()

    if judgment_missing:
        slog(
            logger, stages.PROMOTE, "info", "%s case #%s without judgment (%s) · %s",
            "Saved" if inserted else "Still no judgment for", case_id, missing_reason, liznr_id or "no LIZNR id yet",
        )
        return case_id

    tally("Acts from", acts_source)
    slog(logger, stages.PROMOTE, "info", "Saved as case #%s · %s", case_id, liznr_id)
    return case_id
