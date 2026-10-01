"""
OCR_DONE -> PROMOTED.

Rewritten 2026-09-08 for the flattened `cr_cases` schema and the regex-first
extraction approach (adapters/supreme_court/extraction.py) — there is no separate
LLM-extraction stage in this pipeline iteration, so this module reads
straight from the scraper's own RawJudgmentRecord (table-cell fields) plus
cr_raw_ingestions.ocr_text (OCR-text fields), not from a raw_ai_extraction
JSON envelope the way the old LLM-driven promotion.py did.

acts/sections come from pipeline/legal_ner_extraction.py's Legal NER over the
OCR text; when NER finds nothing, pipeline/llm_enrichment.py fills them from
the provision paragraphs instead (its `existing_sections` guard keeps it from
overwriting NER's result).

case_note and industries are left NULL/empty by THIS function (see
cr_cases.industries' comment in db/schema.sql and adapters/supreme_court/extraction.py's
module docstring for why industries specifically has no regex source) —
pipeline/llm_enrichment.py fills both in immediately afterward
(orchestrator/batch_runner.py calls it right after promote_ingestion()
returns a case_id), not as part of promotion's own transaction.
"""

import logging
from typing import List, Optional, Tuple

from adapters.base import RawJudgmentRecord
from db import scrape_jobs
from db.connection import get_pooled_connection
from db.lookups import (
    get_or_create_category,
    get_or_create_judge,
    get_or_create_ministry,
    get_or_create_subject,
    resolve_act_entries,
    resolve_bench,
)
from normalization import advocates, case_numbers, judges, parties
from normalization.dates import parse_date
from normalization.ministries import find_ministry_in_party_name
from adapters.supreme_court import extraction as rx
from orchestrator import stages
from orchestrator.log_context import slog, tally
from pipeline.legal_ner_extraction import extract_acts_sections

logger = logging.getLogger("scraper_backend_v2.promotion")

_VALID_DISPOSITION_CATEGORIES = {
    "Allowed", "Dismissed", "Partly Allowed", "Disposed",
    "Remanded", "Withdrawn", "Quashed", "Set Aside", "Other",
}


def _validate_enum(value: Optional[str], allowed: set) -> Optional[str]:
    return value if value in allowed else None


# get-or-create lookup helpers (judges, acts, sections,
# subjects, ministries, industries, case categories) live in db/lookups.py
# — schema-wide, shared with pipeline/llm_enrichment.py, not specific to
# this court's promotion pipeline. Only the ministry resolution below
# (which fields feed the lookup, using this court's own extracted party
# names) is Supreme-Court-specific and stays here.

def _resolve_ministries(cur, petitioner: Optional[str], respondent: Optional[str]) -> List[int]:
    """Scoped to the parsed party names only, never the whole OCR text — see normalization.ministries.find_ministry_in_party_name's own docstring on why."""
    ministry_ids = []
    seen = set()
    for party_name in (petitioner, respondent):
        ministry_name = find_ministry_in_party_name(party_name)
        if ministry_name and ministry_name not in seen:
            seen.add(ministry_name)
            ministry_ids.append(get_or_create_ministry(cur, ministry_name))
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
    onto a cr_cases row that has none yet, in the caller's own transaction.

    Must only be called once `case_id` is known to refer to a row this save
    actually landed on (never when _save_case's write was a no-op, and never
    while judgment_date is NULL) -- claiming a sequence number and
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
                slog(logger, stages.PROMOTE, "warning", "Case #%s not found · can't assign a LIZNR id", case_id)
                return None
            court_id, judgment_date, existing_liznr_id = row
            if existing_liznr_id is not None:
                slog(logger, stages.PROMOTE, "info", "Case #%s already has LIZNR id %s · not reassigning", case_id, existing_liznr_id)
                return existing_liznr_id
            if judgment_date is None:
                slog(logger, stages.PROMOTE, "info", "Case #%s still has no judgment date · can't assign a LIZNR id yet", case_id)
                return None

            liznr_id = _assign_liznr_id(cur, case_id, court_id, judgment_date.year)
        conn.commit()
    return liznr_id


class PromotionSkipped(Exception):
    """Raised when an ingestion can't be promoted at all (missing case_number, NOT NULL on `cr_cases`) — row goes to NEEDS_REVIEW, not a crash."""


# A metadata-only row (judgment_status <> 'AVAILABLE') is the one kind of existing case a
# save may overwrite: with a judgment it's filled in completely, without one only its
# "why is it missing" fields are refreshed. A case that already has a judgment is never touched.
_FILL_IN_JUDGMENT_SET = """
    petitioner = EXCLUDED.petitioner, respondent = EXCLUDED.respondent,
    petitioner_advocate = EXCLUDED.petitioner_advocate, respondent_advocate = EXCLUDED.respondent_advocate,
    filing_year = EXCLUDED.filing_year, bench = EXCLUDED.bench, judgment_by = EXCLUDED.judgment_by,
    judgment_date = EXCLUDED.judgment_date, language = EXCLUDED.language, neutral_citation = EXCLUDED.neutral_citation,
    sections = EXCLUDED.sections, acts = EXCLUDED.acts, subject = EXCLUDED.subject,
    conclusion = EXCLUDED.conclusion, judgement = EXCLUDED.judgement, ocr_text = EXCLUDED.ocr_text,
    source_pdf_url = EXCLUDED.source_pdf_url, blob_pdf_id = EXCLUDED.blob_pdf_id,
    ministries = EXCLUDED.ministries, disposition = EXCLUDED.disposition, case_category = EXCLUDED.case_category,
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
    """
    Promotes one OCR_DONE cr_raw_ingestions row into a single `cr_cases` row,
    using RawJudgmentRecord (table-cell fields, already scraped) and
    ocr_text (OCR-text fields, via adapters/supreme_court/extraction.py) — no LLM
    call in this pipeline iteration. Returns the case_id, or None if
    routed to NEEDS_REVIEW (missing case_number, or the case already has a
    judgment) or PROMOTION_FAILED (an unexpected DB error). A case saved
    earlier without a judgment is filled in, keeping its case_id/liznr_id.

    sections/acts come from Legal NER over the OCR text (regex act-name
    capture was dropped 2026-09-09 for storing fragments like "Arbitrator
    Would Be Ineligible To Act" as acts). NER runs before the pooled
    connection is taken, so a long judgment doesn't hold one open. When NER
    finds nothing both columns stay empty and pipeline/llm_enrichment.py
    fills them from the provision paragraphs.

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
    only claimed AFTER the INSERT/fill-in is confirmed to have landed on a row
    that has none yet -- claiming one earlier and then discarding it on a
    re-run permanently burns that number.
    """
    ingestion = scrape_jobs.get_ingestion(ingestion_id)
    if ingestion is None:
        raise ValueError(f"No cr_raw_ingestions row with ingestion_id={ingestion_id}")

    try:
        return _save_case(
            record, ingestion["court_id"], ocr_text=ingestion["ocr_text"] or "",
            blob_pdf_id=ingestion["blob_pdf_id"], ingestion_id=ingestion_id,
        )
    except PromotionSkipped as e:
        slog(logger, stages.PROMOTE, "warning", "Not saved: %s → needs review", e)
        scrape_jobs.update_status(ingestion_id, status="NEEDS_REVIEW", error_message=str(e))
        return None
    except Exception as e:
        slog(logger, stages.PROMOTE, "exception", "Failed to save case (ingestion #%s)", ingestion_id)
        scrape_jobs.update_status(ingestion_id, status="PROMOTION_FAILED", error_message=str(e))
        return None


def save_case_without_judgment(court_id: int, record: RawJudgmentRecord, judgment_status: str, reason: str) -> Optional[int]:
    """Saves the table-row metadata of a case whose PDF is missing; None if it's already saved with a judgment. Raises PromotionSkipped/DB errors to the caller."""
    return _save_case(record, court_id, ocr_text="", blob_pdf_id=None, ingestion_id=None, judgment_missing=(judgment_status, reason))


def _save_case(
    record: RawJudgmentRecord,
    court_id: int,
    ocr_text: str,
    blob_pdf_id: Optional[str],
    ingestion_id: Optional[int],
    judgment_missing: Optional[Tuple[str, str]] = None,
) -> Optional[int]:
    case_number = (record.case_number_raw or "").strip()
    if not case_number:
        raise PromotionSkipped("case_number_raw is missing/blank — required NOT NULL on cr_cases")

    judgment_date = parse_date(record.decision_date_raw)
    needs_review = judgment_date is None

    party_names = rx.parse_party_names(record.party_name_raw)
    petitioner = parties.clean_party_name(party_names["petitioner"]) if party_names["petitioner"] else None
    respondent = parties.clean_party_name(party_names["respondent"]) if party_names["respondent"] else None

    advocate_names = rx.parse_advocates(record.extra.get("advocate_raw"))
    petitioner_advocate = advocates.clean_advocate_name(advocate_names["petitioner_advocate"]) or None
    respondent_advocate = advocates.clean_advocate_name(advocate_names["respondent_advocate"]) or None

    filing_year = case_numbers.extract_filing_year(case_number)

    bench_names = rx.parse_bench(record.extra.get("bench_raw"))
    judgment_by_name = judges.clean_judge_name(record.judge_raw) if record.judge_raw else None

    disposition = _validate_enum(
        rx.extract_disposition(ocr_text).get("disposition_category"), _VALID_DISPOSITION_CATEGORIES
    )
    subject_word = rx.extract_subject_from_jurisdiction(ocr_text)
    conclusion = rx.extract_conclusion(ocr_text)
    judgement_body = rx.extract_judgment_body(ocr_text)
    classified_category = case_numbers.classify_category(case_number) is not None
    ner_acts = extract_acts_sections(ocr_text) if ocr_text else []

    judgment_status, missing_reason = judgment_missing or ("AVAILABLE", None)

    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            act_ids, section_ids = resolve_act_entries(cur, ner_acts)
            judge_ids = resolve_bench(cur, bench_names)
            judgment_by_id = get_or_create_judge(cur, judgment_by_name) if judgment_by_name else None
            subject_id = get_or_create_subject(cur, subject_word) if subject_word else None
            ministry_ids = _resolve_ministries(cur, petitioner, respondent)
            category_ids = [get_or_create_category(cur, case_number)] if classified_category else []
            category_ids = [c for c in category_ids if c is not None]

            cur.execute(f"""
                INSERT INTO cr_cases (
                    liznr_id, court_id, case_number, petitioner, respondent,
                    petitioner_advocate, respondent_advocate, filing_year,
                    bench, judgment_by, judgment_date, language, neutral_citation,
                    sections, acts, subject,
                    conclusion, judgement, ocr_text,
                    source_pdf_url, blob_pdf_id,
                    ministries, industries,
                    disposition, document_type, case_category,
                    needs_review,
                    judgment_status, judgment_missing_reason, judgment_checked_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, '{{}}',
                    %s, 'CaseLaw', %s,
                    %s,
                    %s, %s, now()
                )
                ON CONFLICT (court_id, case_number) DO UPDATE SET
                    {_STILL_MISSING_SET if judgment_missing else _FILL_IN_JUDGMENT_SET}
                WHERE cr_cases.judgment_status <> 'AVAILABLE'
                RETURNING case_id, liznr_id, (xmax = 0) AS inserted;
            """, (
                # liznr_id starts NULL -- assigned below, only once we know which row
                # this landed on. See _assign_liznr_id's docstring for why: claiming a
                # citation sequence number before knowing the write will land burns
                # that number forever on every re-run.
                None, court_id, case_number, petitioner, respondent,
                petitioner_advocate, respondent_advocate, filing_year,
                judge_ids, judgment_by_id, judgment_date, record.extra.get("language"), record.neutral_citation_raw,
                section_ids, act_ids, subject_id,
                conclusion, judgement_body, ocr_text or None,
                record.source_url, blob_pdf_id,
                ministry_ids,
                disposition, category_ids,
                needs_review,
                judgment_status, missing_reason,
            ))
            row = cur.fetchone()
            if row is None:
                if ingestion_id is None:
                    conn.commit()
                    slog(logger, stages.PROMOTE, "info", "Case already in database with its judgment · nothing to save")
                    return None
                # Already promoted with a judgment by a prior run -- not an error.
                # Status update happens on this same cursor, before the single commit below, so the
                # (no-op) cases INSERT and the NEEDS_REVIEW status flip land in one transaction.
                scrape_jobs.update_status_in_tx(
                    cur, ingestion_id, status="NEEDS_REVIEW",
                    error_message="case_number already exists for this court — likely a re-run",
                )
                conn.commit()
                slog(logger, stages.PROMOTE, "warning", "Case already exists in database (likely a re-run) → needs review")
                return None
            case_id, liznr_id, inserted = row

            # Only when judgment_date is real: assigning one against date.today().year
            # for a NULL-date row would stay wrong forever after the date is later
            # fixed in review (the row is already flagged via needs_review=TRUE above;
            # a later corrected promotion can call assign_liznr_id_for_reviewed_case(case_id)).
            if liznr_id is None and judgment_date is not None:
                liznr_id = _assign_liznr_id(cur, case_id, court_id, judgment_date.year)

            # Same cursor/transaction as the cases write above — a crash between
            # committing the case and updating raw_ingestions can no longer leave a
            # promoted case with no case_id back-reference on its ingestion row.
            if ingestion_id is not None:
                scrape_jobs.update_status_in_tx(cur, ingestion_id, status="PROMOTED", case_id=case_id)

        conn.commit()

    if judgment_missing:
        slog(
            logger, stages.PROMOTE, "info", "%s case #%s without judgment (%s) · %s",
            "Saved" if inserted else "Still no judgment for", case_id, missing_reason, liznr_id or "no LIZNR id yet",
        )
        return case_id

    if act_ids:
        slog(
            logger, stages.NER, "info",
            "Ran Legal NER on judgment text · found %d act(s), %d section(s)", len(act_ids), len(section_ids),
        )
    else:
        slog(logger, stages.NER, "info", "Ran Legal NER · nothing found → LLM enrichment will extract provisions")
    tally("Acts from", "Legal NER" if act_ids else "none found (left to LLM enrichment)")
    if not inserted:
        slog(logger, stages.PROMOTE, "info", "Judgment filled in for case #%s (saved earlier without one) · %s", case_id, liznr_id or "no LIZNR id yet")
    elif needs_review:
        slog(logger, stages.PROMOTE, "warning", "Saved as case #%s · judgment date missing → needs review, no LIZNR id yet", case_id)
    else:
        slog(logger, stages.PROMOTE, "info", "Saved as case #%s · %s", case_id, liznr_id)
    return case_id
