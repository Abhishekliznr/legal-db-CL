"""
OCR_DONE -> PROMOTED, for Madhya Pradesh High Court.

Same shape as adapters/supreme_court/promotion.py (own `cr_cases` INSERT,
reusing db/lookups.py's schema-wide get-or-create helpers), but the source
fields are entirely different: MP's own adapter.py's scrape() already did
the case-status lookup (adapters/high_courts/mp/case_status.py) and stashed
its parsed result on `record.extra`, plus the ILRS-sourced headnote. There
is no LLM enrichment call for MP yet (orchestrator/batch_runner.py's
`run_enrichment=False` for this court, see routers/scraper_router.py's
registry entry) -- case_note comes straight from the ILRS headnote, and
sections/acts are resolved directly from case-status's own Act lines, not
from a paragraph-filtered LLM call the way Supreme Court's are.
"""

import logging
from typing import Any, Dict, List, Optional

from adapters.base import RawJudgmentRecord
from db import scrape_jobs
from db.connection import get_pooled_connection
from db.lookups import (
    get_or_create_act,
    get_or_create_advocate,
    get_or_create_category,
    get_or_create_section,
)
from normalization import case_numbers, parties
from normalization.acts import resolve_act
from normalization.dates import parse_date

logger = logging.getLogger("scraper_backend_v2.mp_promotion")

_VALID_DISPOSITION_CATEGORIES = {
    "Allowed", "Dismissed", "Partly Allowed", "Disposed",
    "Remanded", "Withdrawn", "Quashed", "Set Aside", "Other",
}


def _validate_enum(value: Optional[str], allowed: set) -> Optional[str]:
    return value if value in allowed else None


def _join_party_names(entries: List[Dict[str, Any]]) -> Optional[str]:
    names = [e["name"] for e in entries if e.get("name")]
    return "; ".join(names) or None


def _resolve_advocate_ids(cur, entries: List[Dict[str, Any]]) -> List[int]:
    ids = []
    for entry in entries:
        enrollment_no = entry.get("enrollment_no")
        if not enrollment_no:
            continue
        ids.append(get_or_create_advocate(cur, entry.get("name") or "", enrollment_no, entry.get("enrollment_year")))
    return ids


def _resolve_acts(cur, act_entries: List[Dict[str, Any]]) -> tuple:
    """
    Returns (act_ids, section_ids) -- MP's case-status Act lines are always
    sections (never rules/orders), matching the "U/Section" field on the
    same page. Dedupes on (act_id, section_number) defensively here, not
    just relying on case_status.py's own _parse_act_lines dedup upstream —
    the real saved page this was built against had the identical Act line
    repeated six times, so a second, cheaper dedup layer at the DB-writing
    stage is worth having regardless of whether the parser always catches it.
    """
    act_ids: List[int] = []
    section_ids: List[int] = []
    seen_acts = set()
    seen_sections = set()
    for entry in act_entries:
        statute_name, short_code, year = resolve_act(entry["act_name"])
        act_id = get_or_create_act(cur, statute_name, short_code, year)
        if act_id not in seen_acts:
            seen_acts.add(act_id)
            act_ids.append(act_id)
        for number in entry.get("sections") or []:
            key = (act_id, number)
            if key in seen_sections:
                continue
            seen_sections.add(key)
            section_ids.append(get_or_create_section(cur, act_id, number))
    return act_ids, section_ids


class PromotionSkipped(Exception):
    """Raised when an ingestion can't be promoted at all (missing case_number, NOT NULL on `cr_cases`) — row goes to NEEDS_REVIEW, not a crash."""


def promote_ingestion(ingestion_id: int, record: RawJudgmentRecord) -> Optional[int]:
    """
    Promotes one OCR_DONE cr_raw_ingestions row (a real judgment PDF,
    checksummed/OCR'd through the unmodified shared pipeline -- MP always
    has one or adapter.py doesn't yield a record at all, see its own
    docstring) into a single `cr_cases` row, using `record.extra`'s
    case-status-parsed fields (adapters/high_courts/mp/case_status.py) plus
    the ILRS headnote.
    """
    ingestion = scrape_jobs.get_ingestion(ingestion_id)
    if ingestion is None:
        raise ValueError(f"No cr_raw_ingestions row with ingestion_id={ingestion_id}")

    ocr_text = ingestion["ocr_text"] or ""
    court_id = ingestion["court_id"]
    extra = record.extra or {}

    logger.info("[MP PROMOTE] ingestion_id=%s: starting (case_number_raw=%r)", ingestion_id, record.case_number_raw)

    try:
        case_number = (record.case_number_raw or "").strip()
        if not case_number:
            raise PromotionSkipped("case_number_raw is missing/blank — required NOT NULL on cr_cases")

        judgment_date = parse_date(record.decision_date_raw)
        needs_review = judgment_date is None

        petitioner = parties.clean_party_name(_join_party_names(extra.get("petitioners") or []))
        respondent = parties.clean_party_name(_join_party_names(extra.get("respondents") or []))

        disposition = _validate_enum(extra.get("disposition_type"), _VALID_DISPOSITION_CATEGORIES)
        case_note = extra.get("headnote") or None
        classified_category = case_numbers.classify_category(case_number) is not None
        registration_year = extra.get("registration_year")

        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                petitioner_advocate_ids = _resolve_advocate_ids(cur, extra.get("petitioner_advocates") or [])
                respondent_advocate_ids = _resolve_advocate_ids(cur, extra.get("respondent_advocates") or [])
                act_ids, section_ids = _resolve_acts(cur, extra.get("acts") or [])
                category_ids = [get_or_create_category(cur, case_number)] if classified_category else []
                category_ids = [c for c in category_ids if c is not None]

                cur.execute("""
                    INSERT INTO cr_cases (
                        liznr_id, court_id, case_number, petitioner, respondent,
                        petitioner_advocate_ids, respondent_advocate_ids, filing_year,
                        judgment_date, neutral_citation,
                        sections, acts, rules, orders,
                        case_note, judgement, ocr_text,
                        source_pdf_url, blob_pdf_id,
                        disposition, document_type, case_category,
                        needs_review
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s, %s, '{}', '{}',
                        %s, %s, %s,
                        %s, %s,
                        %s, 'CaseLaw', %s,
                        %s
                    )
                    ON CONFLICT (court_id, case_number) DO NOTHING
                    RETURNING case_id;
                """, (
                    None, court_id, case_number, petitioner, respondent,
                    petitioner_advocate_ids, respondent_advocate_ids, registration_year,
                    judgment_date, extra.get("neutral_citation"),
                    section_ids, act_ids,
                    case_note, ocr_text, ocr_text,
                    record.source_url, ingestion["blob_pdf_id"],
                    disposition, category_ids,
                    needs_review,
                ))
                row = cur.fetchone()
                if row is None:
                    scrape_jobs.update_status_in_tx(
                        cur, ingestion_id, status="NEEDS_REVIEW",
                        error_message="case_number already exists for this court — likely a re-run",
                    )
                    conn.commit()
                    logger.info(
                        "[MP PROMOTE] ingestion_id=%s: case_number=%r already exists for court_id=%s — likely a re-run, routed to NEEDS_REVIEW",
                        ingestion_id, case_number, court_id,
                    )
                    return None
                case_id = row[0]

                scrape_jobs.update_status_in_tx(cur, ingestion_id, status="PROMOTED", case_id=case_id)

            conn.commit()

        logger.info(
            "[MP PROMOTE] ingestion_id=%s: done — case_id=%s disposition=%s needs_review=%s",
            ingestion_id, case_id, disposition, needs_review,
        )
        return case_id

    except PromotionSkipped as e:
        logger.warning("[MP PROMOTE] ingestion_id=%s: skipped — %s", ingestion_id, e)
        scrape_jobs.update_status(ingestion_id, status="NEEDS_REVIEW", error_message=str(e))
        return None
    except Exception as e:
        logger.exception("[MP PROMOTE] ingestion_id=%s: failed", ingestion_id)
        scrape_jobs.update_status(ingestion_id, status="PROMOTION_FAILED", error_message=str(e))
        return None
