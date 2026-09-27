"""
OCR_DONE -> PROMOTED, for Madhya Pradesh High Court.

Same shape as adapters/supreme_court/promotion.py (own `cr_cases` INSERT,
reusing db/lookups.py's schema-wide get-or-create helpers), but the source
fields are entirely different: MP's own adapter.py's scrape() already did
the case-status lookup (adapters/high_courts/mp/case_status.py) and stashed
its parsed result on `record.extra`, plus the ILRS-sourced headnote/neutral
citation. case_note comes straight from the ILRS headnote, and
sections/acts are resolved primarily from case-status's own Act lines --
rules/orders are deliberately left '{}' always (case-status's own Act lines
never distinguish a rule/order from a section, and this court's spec only
wants acts+sections tracked). When case-status has no Act row at all,
db.lookups.resolve_act_entries() is retried against pipeline/legal_ner_extraction.py's Legal
NER output over the judgment's own OCR text before falling through any
further -- LLM enrichment (pipeline/llm_enrichment.py, enabled for MP as of
orchestrator/registry.py's entry) only ever touches sections/acts
as a last resort, when both of these came up empty (its own
`existing_sections` guard). Enrichment otherwise fills
conclusion/industries/ministries/favouring_party/subject the same way it
does for Supreme Court, COALESCE'd on top of whatever this module already
set directly from case-status/ILRS/NER data.

liznr_id assignment (_claim_citation_sequence/_build_liznr_id/
_assign_liznr_id below) is a straight copy of
adapters/supreme_court/promotion.py's own version -- kept local rather than
factored into a shared module, matching how _VALID_DISPOSITION_CATEGORIES/
_validate_enum are already duplicated between the two files rather than
shared, and to avoid touching Supreme Court's own promotion.py/tests for
this MP-specific fix.
"""

import logging
from typing import Any, Dict, List, Optional

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


# Our own citation scheme, independent of whether the court ever assigned a
# neutral_citation -- 'LIZNR/<court_code>/<seq>/<year>', e.g. 'LIZNR/MPHC/0001/2026'.
LIZNR_ID_PREFIX = "LIZNR"


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
    """
    Claims the next per-(court, year) citation sequence number and writes it
    onto an ALREADY-INSERTED cr_cases row -- must only be called once
    `case_id` is known to refer to a real, newly inserted row (never on the
    ON CONFLICT DO NOTHING branch, and never while judgment_date is NULL),
    same contract as adapters/supreme_court/promotion.py's own version.
    """
    liznr_id = _build_liznr_id(cur, court_id, year)
    if liznr_id is not None:
        cur.execute("UPDATE cr_cases SET liznr_id = %s WHERE case_id = %s;", (liznr_id, case_id))
    return liznr_id


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
        cnr = record.cnr_raw or None
        # No language field exists anywhere in MP's own source data (ILRS or
        # case-status) -- always English, not a per-case fallback.
        language = "English"

        # case-status's exact "Registered On" date is more precise than
        # ILRS's own registration_year (a coarser publication-year value) --
        # prefer it for filing_year (used to compute case age) when present.
        registered_on = parse_date(extra.get("registered_on"))
        filing_year = registered_on.year if registered_on else extra.get("registration_year")

        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                petitioner_advocate_ids = _resolve_advocate_ids(cur, extra.get("petitioner_advocates") or [])
                respondent_advocate_ids = _resolve_advocate_ids(cur, extra.get("respondent_advocates") or [])
                act_ids, section_ids = resolve_act_entries(cur, extra.get("acts") or [])
                acts_source = "case-status"
                if not act_ids:
                    # case-status had no Act row for this case (new/pending
                    # filing, or the parser found nothing) -- run the local
                    # Legal NER model (pipeline/legal_ner_extraction.py) over
                    # the judgment's OCR text as a second, still-deterministic
                    # source before this falls all the way through to LLM
                    # enrichment's own provisions guess (pipeline/llm_enrichment.py).
                    act_ids, section_ids = resolve_act_entries(cur, extract_acts_sections(ocr_text))
                    slog(
                        logger, stages.NER, "info",
                        "case-status had no acts → ran Legal NER on judgment text · found %d act(s), %d section(s)",
                        len(act_ids), len(section_ids),
                    )
                    acts_source = "Legal NER" if act_ids else "none found (left to LLM enrichment)"
                else:
                    slog(logger, stages.NER, "info", "Skipped · case-status already listed acts")

                # case-status's own "Case No." cell already names both the
                # case-type code and its full name (e.g. "CRA"/"CRIMINAL
                # APPEAL") -- a direct, more reliable source than
                # classify_category()'s regex-guess off the case_number
                # string (built for Supreme Court's differently-shaped
                # numbers, and MP's case_number is now the full "Bench/Type/
                # Number/Year" string anyway, not a shape that regex knows).
                case_type_code = extra.get("case_type_code")
                case_type_name = extra.get("case_type_name")
                category_ids = (
                    [get_or_create_case_category(cur, case_type_code, case_type_name)]
                    if case_type_code and case_type_name else []
                )

                # Bench judges come from case-status's own "Last Listed On"
                # field (adapters/high_courts/mp/case_status.py's own
                # `judges` parse) -- there is no separate "who authored"
                # signal in this source, so judgment_by is just the first
                # judge of that resolved bench, when any were found.
                judge_ids = resolve_bench(cur, extra.get("judges") or [])
                judgment_by_id = judge_ids[0] if judge_ids else None

                # First-pass, non-LLM subject source: case-status's own
                # "Category" row (e.g. "CRIMINAL LAW & PROCEDURE") -- LLM
                # enrichment (pipeline/llm_enrichment.py, now enabled for MP)
                # only fills subject via COALESCE when this is None.
                subject_category = extra.get("subject_category")
                subject_id = get_or_create_subject(cur, subject_category) if subject_category else None

                cur.execute("""
                    INSERT INTO cr_cases (
                        liznr_id, court_id, case_number, cnr, petitioner, respondent,
                        petitioner_advocate_ids, respondent_advocate_ids, filing_year,
                        bench, judgment_by, judgment_date, language, neutral_citation,
                        sections, acts, subject,
                        case_note, judgement, ocr_text,
                        source_pdf_url, blob_pdf_id,
                        disposition, document_type, case_category,
                        needs_review
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s, 'CaseLaw', %s,
                        %s
                    )
                    ON CONFLICT (court_id, case_number) DO NOTHING
                    RETURNING case_id;
                """, (
                    # liznr_id starts NULL -- assigned below, only after we
                    # know this INSERT actually landed a new row (never on
                    # the ON CONFLICT/re-run branch), same contract as
                    # Supreme Court's own promotion.py.
                    None, court_id, case_number, cnr, petitioner, respondent,
                    petitioner_advocate_ids, respondent_advocate_ids, filing_year,
                    judge_ids, judgment_by_id, judgment_date, language, record.neutral_citation_raw,
                    section_ids, act_ids, subject_id,
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
                    slog(logger, stages.PROMOTE, "warning", "Case already exists in database (likely a re-run) → needs review")
                    return None
                case_id = row[0]

                liznr_id = _assign_liznr_id(cur, case_id, court_id, judgment_date.year) if judgment_date is not None else None

                scrape_jobs.update_status_in_tx(cur, ingestion_id, status="PROMOTED", case_id=case_id)

            conn.commit()

        tally("Acts from", acts_source)
        if needs_review:
            slog(logger, stages.PROMOTE, "warning", "Saved as case #%s · judgment date missing → needs review, no LIZNR id yet", case_id)
        else:
            slog(logger, stages.PROMOTE, "info", "Saved as case #%s · %s", case_id, liznr_id)
        return case_id

    except PromotionSkipped as e:
        slog(logger, stages.PROMOTE, "warning", "Not saved: %s → needs review", e)
        scrape_jobs.update_status(ingestion_id, status="NEEDS_REVIEW", error_message=str(e))
        return None
    except Exception as e:
        slog(logger, stages.PROMOTE, "exception", "Failed to save case (ingestion #%s)", ingestion_id)
        scrape_jobs.update_status(ingestion_id, status="PROMOTION_FAILED", error_message=str(e))
        return None
