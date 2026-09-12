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

from psycopg2.extras import Json

from adapters.base import RawJudgmentRecord
from db import scrape_jobs
from db.connection import get_pooled_connection
from normalization import case_numbers, judges, parties
from parsers.judgment_parser import parse_judgment
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


def _assign_liznr_id(cur, case_id: int, court_id: int, year: int) -> Optional[str]:
    """
    Claims the next per-(court, year) citation sequence number and writes it
    onto an ALREADY-INSERTED cr_cases row, in the caller's own transaction.

    Must only be called once `case_id` is known to refer to a real, newly
    inserted row (never on promote_ingestion's ON CONFLICT DO NOTHING branch,
    and never while judgment_date is NULL) -- claiming a sequence number and
    then not committing it against a real row permanently burns that number
    (2026-09-10 fix: the previous code claimed the sequence via
    _build_liznr_id BEFORE the cr_cases INSERT, so a re-run that hit the
    ON CONFLICT branch still committed the incremented cr_citation_sequences
    row, leaving a permanent gap in LIZNR/<court>/<seq>/<year>).
    """
    liznr_id = _build_liznr_id(cur, court_id, year)
    if liznr_id is not None:
        cur.execute("UPDATE cr_cases SET liznr_id = %s WHERE case_id = %s;", (liznr_id, case_id))
    return liznr_id


def assign_liznr_id_for_reviewed_case(case_id: int) -> Optional[str]:
    """
    For a case promoted with judgment_date NULL (promote_ingestion leaves
    liznr_id NULL and needs_review=TRUE in that case, deliberately never
    guessing a year from date.today() -- see promote_ingestion's docstring)
    whose judgment_date has since been corrected by review: claims a
    citation sequence number and writes the resulting liznr_id, in one
    transaction. No-op (returns None) if the case doesn't exist or still
    has no judgment_date; returns the existing id without reclaiming a new
    one if the case somehow already has a liznr_id.
    """
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT court_id, judgment_date, liznr_id FROM cr_cases WHERE case_id = %s;", (case_id,))
            row = cur.fetchone()
            if row is None:
                logger.warning("[PROMOTE] case_id=%s: assign_liznr_id_for_reviewed_case — no such case", case_id)
                return None
            court_id, judgment_date, existing_liznr_id = row
            if existing_liznr_id is not None:
                logger.info("[PROMOTE] case_id=%s: already has liznr_id=%s, not reassigning", case_id, existing_liznr_id)
                return existing_liznr_id
            if judgment_date is None:
                logger.info("[PROMOTE] case_id=%s: judgment_date still NULL, cannot assign a liznr_id yet", case_id)
                return None

            liznr_id = _assign_liznr_id(cur, case_id, court_id, judgment_date.year)
        conn.commit()
    return liznr_id


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

    sections/acts/rules/orders are deliberately left empty here (2026-09-09)
    — regex's own act-name capture proved actively wrong at real production
    scale (fragments like "Arbitrator Would Be Ineligible To Act" ending up
    in cr_acts), not just low-recall, so those columns are now populated
    entirely by pipeline/llm_enrichment.py's paragraph-filtered LLM call
    after promotion, same treatment as case_note/industries already got.

    Unlike the old documents.judgment_date NOT NULL, a missing/unparseable
    judgment_date here does NOT block promotion — the row still gets
    inserted (case_number, ocr_text, etc. are all still useful), just with
    judgment_date NULL and needs_review=TRUE so it's easy to find and fix
    later instead of being silently dropped from the batch.

    liznr_id (2026-09-10 fix) is left NULL in that same case rather than
    built off date.today().year -- a fabricated year would stay permanently
    wrong even after the real judgment_date is fixed in review, since a
    citation number, once issued, is never reassigned. Once the date is
    corrected, call assign_liznr_id_for_reviewed_case(case_id) to assign one
    for real. When judgment_date IS known, the citation sequence number is
    only claimed AFTER this INSERT is confirmed to have landed a new row
    (never on the ON CONFLICT/re-run branch below) -- claiming one earlier
    and then discarding it on a re-run permanently burns that number.
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
        conclusion = rx.extract_conclusion(ocr_text)
        judgement_body = rx.extract_judgment_body(ocr_text)
        classified_category = case_numbers.classify_category(case_number) is not None

        # Deterministic structural parse of the raw OCR text (parsers/judgment_parser.py)
        # -- entirely separate from, and computed before, pipeline/llm_enrichment.py's
        # later LLM pass. Never raises: a parser bug degrades to one unstructured
        # section holding the verbatim ocr_text (see parse_judgment's own docstring),
        # so it can never block promotion or lose the source text.
        structured_content = parse_judgment(
            ocr_text,
            bench=bench_names,
            judgment_date=judgment_date.isoformat() if judgment_date else None,
            case_number=case_number,
            neutral_citation=record.neutral_citation_raw,
        )

        with get_pooled_connection() as conn:
            with conn.cursor() as cur:
                judge_ids = _resolve_bench(cur, bench_names)
                judgment_by_id = _get_or_create_judge(cur, judgment_by_name) if judgment_by_name else None
                subject_id = _get_or_create_subject(cur, subject_word) if subject_word else None
                ministry_ids = _resolve_ministries(cur, petitioner, respondent)
                category_ids = [_get_or_create_category(cur, case_number)] if classified_category else []
                category_ids = [c for c in category_ids if c is not None]

                cur.execute("""
                    INSERT INTO cr_cases (
                        liznr_id, court_id, case_number, petitioner, respondent,
                        bench, judgment_by, judgment_date, language, neutral_citation,
                        sections, acts, rules, orders, subject,
                        conclusion, judgement, ocr_text, structured_content,
                        source_pdf_url, blob_pdf_id,
                        ministries, industries,
                        disposition, document_type, case_category,
                        needs_review
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        '{}', '{}', '{}', '{}', %s,
                        %s, %s, %s, %s,
                        %s, %s,
                        %s, '{}',
                        %s, 'CaseLaw', %s,
                        %s
                    )
                    ON CONFLICT (court_id, case_number) DO NOTHING
                    RETURNING case_id;
                """, (
                    # liznr_id starts NULL -- assigned below, AFTER we know this INSERT
                    # actually landed a new row, not on the ON CONFLICT (re-run) branch.
                    # See _assign_liznr_id's docstring for why: claiming a citation
                    # sequence number before knowing the INSERT will land burns that
                    # number forever on every re-run.
                    None, court_id, case_number, petitioner, respondent,
                    judge_ids, judgment_by_id, judgment_date, record.extra.get("language"), record.neutral_citation_raw,
                    subject_id,
                    conclusion, judgement_body, ocr_text, Json(structured_content),
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

                # Only claim a citation sequence number now that we know the INSERT
                # actually landed a new row -- and only when judgment_date is real:
                # assigning one against date.today().year for a NULL-date row would
                # stay wrong forever after the date is later fixed in review (the row
                # is already flagged via needs_review=TRUE above; a later corrected
                # promotion can call assign_liznr_id_for_reviewed_case(case_id)).
                liznr_id = _assign_liznr_id(cur, case_id, court_id, judgment_date.year) if judgment_date is not None else None

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
