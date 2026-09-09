"""
OCR_DONE -> PROMOTED.

Rewritten 2026-09-08 for the flattened `cr_cases` schema and the regex-first
extraction approach (pipeline/regex_extraction.py) — there is no separate
LLM-extraction stage in this pipeline iteration, so this module reads
straight from the scraper's own RawJudgmentRecord (table-cell fields) plus
cr_raw_ingestions.ocr_text (OCR-text fields), not from a raw_ai_extraction
JSON envelope the way the old LLM-driven promotion.py did.

case_note and industries are left NULL/empty by THIS function (see
cr_cases.industries' comment in db/schema.sql and pipeline/regex_extraction.py's
module docstring for why industries specifically has no regex source) —
pipeline/llm_enrichment.py fills both in immediately afterward
(orchestrator/batch_runner.py calls it right after promote_ingestion()
returns a case_id), not as part of promotion's own transaction.
"""

import logging
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from adapters.base import RawJudgmentRecord
from db import scrape_jobs
from db.connection import get_pooled_connection
from normalization import case_numbers, judges, parties
from pipeline import regex_extraction as rx

logger = logging.getLogger("scraper_backend_v2.promotion")

_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %B %Y", "%d %b %Y")

_VALID_DISPOSITION_CATEGORIES = {
    "Allowed", "Dismissed", "Partly Allowed", "Disposed",
    "Remanded", "Withdrawn", "Quashed", "Set Aside", "Other",
}


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    cleaned = re.sub(r"(\d{1,2})(st|nd|rd|th)", r"\1", value, flags=re.IGNORECASE).replace(",", "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _validate_enum(value: Optional[str], allowed: set) -> Optional[str]:
    return value if value in allowed else None


# ---------------------------------------------------------------------
# get-or-create lookup helpers. Array columns on `cr_cases` (bench, sections,
# acts, rules, orders, ministries, case_category) mean Postgres can't
# FK-constrain membership the way the old junction tables did — these
# helpers are where that integrity actually gets enforced instead.
#
# Each does INSERT ... ON CONFLICT (<natural key>) DO NOTHING RETURNING
# <id>, falling back to a SELECT only when nothing came back (another
# concurrent promotion won the race) — the same pattern
# _claim_citation_sequence below already uses correctly. A plain
# SELECT-then-INSERT (the previous shape here) lets two concurrent
# promotions both pass the SELECT before either INSERTs, so the loser's
# INSERT throws an unhandled UniqueViolation; INSERT ... ON CONFLICT
# takes a row lock on the conflicting key, so the loser blocks until the
# winner commits and then safely reads back the winner's row instead.
# ---------------------------------------------------------------------

def _get_or_create_judge(cur, cleaned_name: str) -> int:
    cur.execute(
        "INSERT INTO cr_judges (full_name, normalized_name) VALUES (%s, %s) ON CONFLICT (normalized_name) DO NOTHING RETURNING judge_id;",
        (cleaned_name, cleaned_name),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT judge_id FROM cr_judges WHERE normalized_name = %s;", (cleaned_name,))
    return cur.fetchone()[0]


def _get_or_create_act(cur, act_name: str, short_code: Optional[str], act_year: Optional[int]) -> int:
    cur.execute(
        "INSERT INTO cr_acts (act_name, act_year, short_code) VALUES (%s, %s, %s) ON CONFLICT (act_name, act_year) DO NOTHING RETURNING act_id;",
        (act_name, act_year, short_code),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "SELECT act_id FROM cr_acts WHERE act_name = %s AND (act_year = %s OR (act_year IS NULL AND %s IS NULL));",
        (act_name, act_year, act_year),
    )
    return cur.fetchone()[0]


def _get_or_create_section(cur, act_id: int, section_number: str) -> int:
    cur.execute(
        "INSERT INTO cr_sections (act_id, section_number) VALUES (%s, %s) ON CONFLICT (act_id, section_number) DO NOTHING RETURNING section_id;",
        (act_id, section_number),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT section_id FROM cr_sections WHERE act_id = %s AND section_number = %s;", (act_id, section_number))
    return cur.fetchone()[0]


def _get_or_create_rule(cur, act_id: int, rule_number: str) -> int:
    cur.execute(
        "INSERT INTO cr_rules (act_id, rule_number) VALUES (%s, %s) ON CONFLICT (act_id, rule_number) DO NOTHING RETURNING rule_id;",
        (act_id, rule_number),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT rule_id FROM cr_rules WHERE act_id = %s AND rule_number = %s;", (act_id, rule_number))
    return cur.fetchone()[0]


def _get_or_create_order(cur, act_id: int, order_number: str) -> int:
    cur.execute(
        "INSERT INTO cr_orders (act_id, order_number) VALUES (%s, %s) ON CONFLICT (act_id, order_number) DO NOTHING RETURNING order_id;",
        (act_id, order_number),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT order_id FROM cr_orders WHERE act_id = %s AND order_number = %s;", (act_id, order_number))
    return cur.fetchone()[0]


def _get_or_create_subject(cur, subject_name: str) -> int:
    cur.execute(
        "INSERT INTO cr_subjects (subject_name) VALUES (%s) ON CONFLICT (subject_name) DO NOTHING RETURNING subject_id;",
        (subject_name,),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT subject_id FROM cr_subjects WHERE subject_name = %s;", (subject_name,))
    return cur.fetchone()[0]


def _get_or_create_ministry(cur, ministry_name: str) -> int:
    cur.execute(
        "INSERT INTO cr_ministries (ministry_name) VALUES (%s) ON CONFLICT (ministry_name) DO NOTHING RETURNING ministry_id;",
        (ministry_name,),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT ministry_id FROM cr_ministries WHERE ministry_name = %s;", (ministry_name,))
    return cur.fetchone()[0]


# Not called from anywhere in THIS module -- promotion never populates
# cr_cases.industries (no regex signal exists for it, see
# pipeline/regex_extraction.py's module docstring). Lives here anyway,
# alongside every other get-or-create lookup helper, since
# pipeline/llm_enrichment.py imports it rather than duplicating the same
# lines a second time.
def _get_or_create_industry(cur, industry_name: str) -> int:
    cur.execute(
        "INSERT INTO cr_industries (industry_name) VALUES (%s) ON CONFLICT (industry_name) DO NOTHING RETURNING industry_id;",
        (industry_name,),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT industry_id FROM cr_industries WHERE industry_name = %s;", (industry_name,))
    return cur.fetchone()[0]


def _get_or_create_category(cur, case_number: Optional[str]) -> Optional[int]:
    classified = case_numbers.classify_category(case_number)
    if not classified:
        return None
    code, name = classified
    cur.execute(
        "INSERT INTO cr_case_categories (category_code, category_name) VALUES (%s, %s) ON CONFLICT (category_code) DO NOTHING RETURNING category_id;",
        (code, name),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT category_id FROM cr_case_categories WHERE category_code = %s;", (code,))
    return cur.fetchone()[0]


def _resolve_provisions(cur, provisions: List[Dict[str, Any]]) -> Tuple[List[int], List[int], List[int], List[int]]:
    """Returns (act_ids, section_ids, rule_ids, order_ids), each de-duplicated, act_ids covering every act referenced by any of the other three."""
    act_ids: List[int] = []
    section_ids: List[int] = []
    rule_ids: List[int] = []
    order_ids: List[int] = []
    seen_acts = set()

    for provision in provisions:
        act_id = _get_or_create_act(cur, provision["statute_name"], provision["short_code"], provision["statute_year"])
        if act_id not in seen_acts:
            seen_acts.add(act_id)
            act_ids.append(act_id)

        number = provision["section_number"]
        provision_type = provision["provision_type"]
        if provision_type == "rule":
            rule_ids.append(_get_or_create_rule(cur, act_id, number))
        elif provision_type == "order":
            order_ids.append(_get_or_create_order(cur, act_id, number))
        else:
            section_ids.append(_get_or_create_section(cur, act_id, number))

    return act_ids, section_ids, rule_ids, order_ids


def _resolve_bench(cur, bench_names: List[str]) -> List[int]:
    judge_ids = []
    seen = set()
    for name in bench_names:
        if name in seen:
            continue
        seen.add(name)
        judge_ids.append(_get_or_create_judge(cur, name))
    return judge_ids


def _resolve_ministries(cur, petitioner: Optional[str], respondent: Optional[str]) -> List[int]:
    """Scoped to the parsed party names only, never the whole OCR text — see regex_extraction.find_ministry_in_party_name's own docstring on why."""
    ministry_ids = []
    seen = set()
    for party_name in (petitioner, respondent):
        ministry_name = rx.find_ministry_in_party_name(party_name)
        if ministry_name and ministry_name not in seen:
            seen.add(ministry_name)
            ministry_ids.append(_get_or_create_ministry(cur, ministry_name))
    return ministry_ids


# Our own citation scheme, independent of whether the court ever assigned a
# neutral_citation -- 'LIZNR/<court_code>/<seq>/<year>', e.g. 'LIZNR/SCIN/0001/2026'.
LIZNR_ID_PREFIX = "LIZNR"


def _claim_citation_sequence(cur, court_id: int, year: int) -> int:
    """
    Atomically claims and returns the next per-(court, year) sequence
    number, resetting each year (see cr_citation_sequences in schema.sql).
    The INSERT ... ON CONFLICT DO UPDATE takes a row lock, so concurrent
    promotions for the same court/year serialize correctly instead of
    racing to the same number.
    """
    cur.execute("""
        INSERT INTO cr_citation_sequences (court_id, citation_year, next_seq)
        VALUES (%s, %s, 1)
        ON CONFLICT (court_id, citation_year)
        DO UPDATE SET next_seq = cr_citation_sequences.next_seq + 1
        RETURNING next_seq;
    """, (court_id, year))
    return cur.fetchone()[0]


def _build_liznr_id(cur, court_id: int, year: int) -> Optional[str]:
    """None when the court has no court_code yet (schema.sql's cr_courts.court_code is
    nullable) -- a case simply goes without one rather than blocking promotion
    on an unrelated backfill."""
    cur.execute("SELECT court_code FROM cr_courts WHERE court_id = %s;", (court_id,))
    row = cur.fetchone()
    court_code = row[0] if row else None
    if not court_code:
        return None
    seq = _claim_citation_sequence(cur, court_id, year)
    return f"{LIZNR_ID_PREFIX}/{court_code}/{seq:04d}/{year}"


class PromotionSkipped(Exception):
    """Raised when an ingestion can't be promoted at all (missing case_number, NOT NULL on `cr_cases`) — row goes to NEEDS_REVIEW, not a crash."""


def promote_ingestion(ingestion_id: int, record: RawJudgmentRecord) -> Optional[int]:
    """
    Promotes one OCR_DONE cr_raw_ingestions row into a single `cr_cases` row,
    using RawJudgmentRecord (table-cell fields, already scraped) and
    ocr_text (OCR-text fields, via pipeline/regex_extraction.py) — no LLM
    call in this pipeline iteration. Returns the new case_id, or None if
    routed to NEEDS_REVIEW (missing case_number) or PROMOTION_FAILED (an
    unexpected DB error).

    Unlike the old documents.judgment_date NOT NULL, a missing/unparseable
    judgment_date here does NOT block promotion — the row still gets
    inserted (case_number, ocr_text, provisions etc. are all still useful),
    just with judgment_date NULL and needs_review=TRUE so it's easy to find
    and fix later instead of being silently dropped from the batch.
    """
    ingestion = scrape_jobs.get_ingestion(ingestion_id)
    if ingestion is None:
        raise ValueError(f"No cr_raw_ingestions row with ingestion_id={ingestion_id}")

    ocr_text = ingestion["ocr_text"] or ""
    court_id = ingestion["court_id"]

    logger.info("[PROMOTE] ingestion_id=%s: starting (case_number_raw=%r)", ingestion_id, record.case_number_raw)

    try:
        case_number = (record.case_number_raw or "").strip()
        if not case_number:
            raise PromotionSkipped("case_number_raw is missing/blank — required NOT NULL on cr_cases")

        judgment_date = _parse_date(record.decision_date_raw)
        needs_review = judgment_date is None

        party_names = rx.parse_party_names(record.party_name_raw)
        petitioner = parties.clean_party_name(party_names["petitioner"]) if party_names["petitioner"] else None
        respondent = parties.clean_party_name(party_names["respondent"]) if party_names["respondent"] else None

        bench_names = rx.parse_bench(record.extra.get("bench_raw"))
        judgment_by_name = judges.clean_judge_name(record.judge_raw) if record.judge_raw else None

        disposition = _validate_enum(
            rx.extract_disposition(ocr_text).get("disposition_category"), _VALID_DISPOSITION_CATEGORIES
        )
        subject_word = rx.extract_subject_from_jurisdiction(ocr_text)
        provisions = rx.extract_provisions(ocr_text)
        conclusion = rx.extract_conclusion(ocr_text)
        judgement_body = rx.extract_judgment_body(ocr_text)
        classified_category = case_numbers.classify_category(case_number) is not None

        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                liznr_id = _build_liznr_id(cur, court_id, (judgment_date or date.today()).year)

                judge_ids = _resolve_bench(cur, bench_names)
                judgment_by_id = _get_or_create_judge(cur, judgment_by_name) if judgment_by_name else None
                act_ids, section_ids, rule_ids, order_ids = _resolve_provisions(cur, provisions)
                subject_id = _get_or_create_subject(cur, subject_word) if subject_word else None
                ministry_ids = _resolve_ministries(cur, petitioner, respondent)
                category_ids = [_get_or_create_category(cur, case_number)] if classified_category else []
                category_ids = [c for c in category_ids if c is not None]

                cur.execute("""
                    INSERT INTO cr_cases (
                        liznr_id, court_id, case_number, petitioner, respondent,
                        bench, judgment_by, judgment_date, language, neutral_citation,
                        sections, acts, rules, orders, subject,
                        conclusion, judgement, ocr_text,
                        source_pdf_url, blob_pdf_id,
                        ministries, industries,
                        disposition, document_type, case_category,
                        needs_review
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s, '{}',
                        %s, 'CaseLaw', %s,
                        %s
                    )
                    ON CONFLICT (court_id, case_number) DO NOTHING
                    RETURNING case_id;
                """, (
                    liznr_id, court_id, case_number, petitioner, respondent,
                    judge_ids, judgment_by_id, judgment_date, record.extra.get("language"), record.neutral_citation_raw,
                    section_ids, act_ids, rule_ids, order_ids, subject_id,
                    conclusion, judgement_body, ocr_text,
                    record.source_url, ingestion["blob_pdf_id"],
                    ministry_ids,
                    disposition, category_ids,
                    needs_review,
                ))
                row = cur.fetchone()
                if row is None:
                    # Already promoted from a prior run of this exact (court_id, case_number) -- not an error.
                    # Status update happens on this same cursor, before the single commit below, so the
                    # (no-op) cases INSERT and the NEEDS_REVIEW status flip land in one transaction.
                    scrape_jobs.update_status_in_tx(
                        cur, ingestion_id, status="NEEDS_REVIEW",
                        error_message="case_number already exists for this court — likely a re-run",
                    )
                    conn.commit()
                    logger.info(
                        "[PROMOTE] ingestion_id=%s: case_number=%r already exists for court_id=%s — likely a re-run, routed to NEEDS_REVIEW",
                        ingestion_id, case_number, court_id,
                    )
                    return None
                case_id = row[0]

                # Same cursor/transaction as the cases INSERT above — a crash between
                # committing the case and updating raw_ingestions can no longer leave a
                # promoted case with no case_id back-reference on its ingestion row.
                scrape_jobs.update_status_in_tx(cur, ingestion_id, status="PROMOTED", case_id=case_id)

            conn.commit()

        logger.info(
            "[PROMOTE] ingestion_id=%s: done — case_id=%s liznr_id=%s disposition=%s needs_review=%s",
            ingestion_id, case_id, liznr_id, disposition, needs_review,
        )
        return case_id

    except PromotionSkipped as e:
        logger.warning("[PROMOTE] ingestion_id=%s: skipped — %s", ingestion_id, e)
        scrape_jobs.update_status(ingestion_id, status="NEEDS_REVIEW", error_message=str(e))
        return None
    except Exception as e:
        logger.exception("[PROMOTE] ingestion_id=%s: failed", ingestion_id)
        scrape_jobs.update_status(ingestion_id, status="PROMOTION_FAILED", error_message=str(e))
        return None
