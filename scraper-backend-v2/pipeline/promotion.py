"""
EXTRACTED -> PROMOTED (spec §5.2/§5.3): normalizes the raw_ai_extraction
JSON envelope into documents/cases/parties/document_coram/document_sections/
citations, plus the Manupatra-parity editorial layer added afterward:
document_paragraphs, document_holdings, case_appellate_history, and
case_timeline_events (see scraper-backend-v2/db/schema.sql's "EDITORIAL
EXTRACTION LAYER" section).

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

document_paragraphs is populated deterministically from ocr_text
(normalization/paragraphs.py — a regex job, not an LLM one) rather than
from the extraction envelope. case_appellate_history.prior_document_id is
left NULL at promotion time, same reasoning as citations.cited_document_id:
resolving which existing document a free-text prior order refers to is a
separate reconciliation concern.
"""

import re
from datetime import datetime
from typing import Any, Dict, Optional

from db import scrape_jobs
from db.connection import get_pooled_connection
from normalization import acts, case_numbers, judges, parties, paragraphs
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
    "Discussed", "Mentioned",
}
_VALID_DISPOSITION_CATEGORIES = {
    "Allowed", "Dismissed", "Partly Allowed", "Disposed",
    "Remanded", "Withdrawn", "Quashed", "Set Aside", "Other",
}
_VALID_PARTY_SIDES = {"PETITIONER_SIDE", "RESPONDENT_SIDE"}
_VALID_APPELLATE_OUTCOMES = {
    "Affirmed", "Reversed", "Partly Reversed", "Set Aside", "Remanded", "Modified",
}
_VALID_DECISION_TYPES = {"Judgment", "Order", "Notification", "Circular"}


def _validate_enum(value: Optional[str], allowed: set) -> Optional[str]:
    return value if value in allowed else None


def _clean_paragraph_ref(value: Any) -> Optional[str]:
    """Coerces an LLM-supplied paragraph reference (int, list, or string, possibly '¶9') into plain text, or None."""
    if value is None:
        return None
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value if v is not None)
    else:
        value = str(value)
    cleaned = value.strip().lstrip("¶").strip()
    return cleaned or None


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


def _get_or_create_advocate(cur, raw_name: str) -> int:
    # No dedicated normalization/advocates.py exists (unlike judges) --
    # matching normalization/parties.py's own fallback style is enough here.
    cleaned = raw_name.strip().upper()
    cur.execute("SELECT advocate_id FROM advocates WHERE normalized_name = %s;", (cleaned,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO advocates (full_name, normalized_name) VALUES (%s, %s) RETURNING advocate_id;",
        (raw_name.strip(), cleaned),
    )
    return cur.fetchone()[0]


def _get_or_create_subject(cur, subject_name: str) -> int:
    cur.execute("SELECT subject_id FROM subjects WHERE subject_name = %s;", (subject_name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO subjects (subject_name) VALUES (%s) RETURNING subject_id;",
        (subject_name,),
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


def _get_or_create_category(cur, case_number: Optional[str]) -> Optional[int]:
    classified = case_numbers.classify_category(case_number)
    if not classified:
        return None
    code, name = classified
    cur.execute("SELECT category_id FROM case_categories WHERE category_code = %s;", (code,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO case_categories (category_code, category_name) VALUES (%s, %s) RETURNING category_id;",
        (code, name),
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


# Our own citation scheme, independent of whether the court ever assigned a
# neutral_citation -- 'LIZNR/<court_code>/<seq>/<year>', e.g. 'LIZNR/SCIN/0001/2026'.
INTERNAL_CITATION_PREFIX = "LIZNR"


def _claim_citation_sequence(cur, court_id: int, year: int) -> int:
    """
    Atomically claims and returns the next per-(court, year) sequence
    number, resetting each year (see citation_sequences in schema.sql).
    The INSERT ... ON CONFLICT DO UPDATE takes a row lock, so concurrent
    promotions for the same court/year serialize correctly instead of
    racing to the same number.
    """
    cur.execute("""
        INSERT INTO citation_sequences (court_id, citation_year, next_seq)
        VALUES (%s, %s, 1)
        ON CONFLICT (court_id, citation_year)
        DO UPDATE SET next_seq = citation_sequences.next_seq + 1
        RETURNING next_seq;
    """, (court_id, year))
    return cur.fetchone()[0]


def _build_internal_citation(cur, court_id: int, year: int) -> Optional[str]:
    """None when the court has no court_code yet (schema.sql's courts.court_code is
    nullable — not backfilled for every seeded court, and adapters can scrape courts
    seed_courts.py doesn't know about) — a document simply goes without one rather
    than blocking promotion on an unrelated backfill."""
    cur.execute("SELECT court_code FROM courts WHERE court_id = %s;", (court_id,))
    row = cur.fetchone()
    court_code = row[0] if row else None
    if not court_code:
        return None
    seq = _claim_citation_sequence(cur, court_id, year)
    return f"{INTERNAL_CITATION_PREFIX}/{court_code}/{seq:04d}/{year}"


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
                internal_citation = _build_internal_citation(cur, ingestion["court_id"], judgment_date.year)

                cur.execute("""
                    INSERT INTO documents (
                        ingestion_id, court_id, data_source, doc_type, decision_type,
                        neutral_citation, internal_citation, judgment_date, case_note_ai, ratio_decidendi,
                        disposition_category, favoring_party_side, overruled_keyword_present,
                        pdf_url, ocr_text, extraction_model, needs_review
                    ) VALUES (
                        %s, %s, %s, 'CaseLaw', %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s
                    ) RETURNING document_id;
                """, (
                    ingestion_id, ingestion["court_id"], ingestion["data_source"],
                    _validate_enum(extraction.get("decision_type"), _VALID_DECISION_TYPES) or "Judgment",
                    extraction.get("neutral_citation"), internal_citation, judgment_date, extraction.get("case_note_ai"),
                    extraction.get("ratio_decidendi"),
                    _validate_enum(extraction.get("disposition_category"), _VALID_DISPOSITION_CATEGORIES),
                    _validate_enum(extraction.get("favoring_party_side"), _VALID_PARTY_SIDES),
                    citator.has_overruled_keyword(ocr_text),
                    pdf_url, ocr_text, extractor, needs_review,
                ))
                document_id = cur.fetchone()[0]

                for para_number, para_text in paragraphs.split_into_paragraphs(ocr_text):
                    cur.execute("""
                        INSERT INTO document_paragraphs (document_id, para_number, para_text)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (document_id, para_number) DO NOTHING;
                    """, (document_id, para_number, para_text))

                case_ids = []
                case_arising_refs = []
                for case_index, case in enumerate(extraction.get("cases", [])):
                    raw_case_number = case.get("case_number")
                    if not raw_case_number:
                        continue  # cases.case_number is NOT NULL — skip rather than fabricate one

                    # Defense-in-depth against the LLM still smashing a
                    # "(Arising out of SLP (C) No. ...)" reference into the
                    # case number despite the prompt now asking it not to —
                    # strip it here too, and carry the reference forward for
                    # case_appellate_history below (unless the appeal's own
                    # number is itself blank/unassigned, in which case the
                    # helper keeps it attached for uniqueness — see its
                    # docstring in normalization/case_numbers.py).
                    case_number, arising_ref = case_numbers.split_arising_out_of(raw_case_number)
                    if arising_ref:
                        case_arising_refs.append(arising_ref)

                    category_id = _get_or_create_category(cur, case_number)
                    filing_year = case_numbers.extract_filing_year(case_number)
                    proceedings_start_date = _parse_date(case.get("proceedings_start_date"))
                    case_filed_date = _parse_date(case.get("case_filed_date"))
                    # Trust an explicit LLM signal; otherwise default to "first
                    # case number listed on this document" -- a defensible but
                    # NOT authoritative heuristic (courts sometimes tag the lead
                    # matter explicitly, sometimes just list it first).
                    is_lead_llm = case.get("is_lead_matter")
                    is_lead = is_lead_llm if isinstance(is_lead_llm, bool) else (case_index == 0)

                    cur.execute("""
                        INSERT INTO cases (
                            document_id, court_id, cnr_number, case_number, category_id, filing_year,
                            proceedings_start_date, case_filed_date, is_lead_matter
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (court_id, case_number) DO NOTHING
                        RETURNING case_id;
                    """, (
                        document_id, ingestion["court_id"], case.get("cnr_number"), case_number,
                        category_id, filing_year, proceedings_start_date, case_filed_date, is_lead,
                    ))
                    case_row = cur.fetchone()
                    if case_row is None:
                        continue  # already existed from a prior run — leave its parties (and timeline) alone
                    case_id = case_row[0]
                    case_ids.append(case_id)

                    # age_years_ai/age_basis_ai: computed here from real DB
                    # dates rather than trusted as LLM arithmetic — an LLM
                    # asked to subtract two dates is exactly the kind of thing
                    # it gets subtly wrong; Python's date subtraction doesn't.
                    if proceedings_start_date and judgment_date:
                        age_years = round((judgment_date - proceedings_start_date).days / 365.25, 2)
                        age_basis = case.get("proceedings_start_basis") or "Computed from proceedings_start_date to judgment_date"
                        cur.execute(
                            "UPDATE cases SET age_years_ai = %s, age_basis_ai = %s WHERE case_id = %s;",
                            (age_years, age_basis, case_id),
                        )

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

                # case_counsels/case_timeline_events are both case-level (NOT
                # NULL case_id), so both are attached to the lead case created
                # above. If every case in this extraction already existed from
                # a prior run, there's no fresh case_id to attach to -- skip
                # rather than guess, consistent with leaving that case's
                # parties untouched in the same situation above.
                lead_case_id = case_ids[0] if case_ids else None
                if lead_case_id is not None:
                    for counsel in extraction.get("counsels", []):
                        name = counsel.get("name")
                        side = _validate_enum(counsel.get("side"), _VALID_PARTY_SIDES)
                        if not name or not side:
                            continue
                        advocate_id = _get_or_create_advocate(cur, name)
                        cur.execute("""
                            INSERT INTO case_counsels (case_id, advocate_id, side, designation)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (case_id, advocate_id, side) DO NOTHING;
                        """, (lead_case_id, advocate_id, side, counsel.get("designation")))

                for subject_name in extraction.get("subjects", []):
                    subject_name = (subject_name or "").strip()
                    if not subject_name:
                        continue
                    subject_id = _get_or_create_subject(cur, subject_name)
                    cur.execute("""
                        INSERT INTO document_subjects (document_id, subject_id)
                        VALUES (%s, %s)
                        ON CONFLICT (document_id, subject_id) DO NOTHING;
                    """, (document_id, subject_id))

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

                # The single editorially "lead" section (Manupatra's "Relevant
                # Section" line) -- upserts on top of the provisions loop above
                # if it already inserted this exact section, or inserts it
                # fresh if the LLM named a lead section it didn't also list in
                # "provisions". Safe against ux_document_sections_one_primary
                # by construction: relevant_section is a single object, never
                # a list, so at most one row per document can be flagged here.
                relevant_section = extraction.get("relevant_section")
                if relevant_section and relevant_section.get("statute_name") and relevant_section.get("section_number"):
                    statute_id = _get_or_create_statute(cur, relevant_section["statute_name"])
                    section_id = _get_or_create_section(cur, statute_id, str(relevant_section["section_number"]))
                    cur.execute("""
                        INSERT INTO document_sections (document_id, section_id, is_primary)
                        VALUES (%s, %s, TRUE)
                        ON CONFLICT (document_id, section_id) DO UPDATE SET is_primary = TRUE;
                    """, (document_id, section_id))

                for citation in extraction.get("citations", []):
                    cited_case_name = citation.get("cited_case_name")
                    if not cited_case_name:
                        continue
                    cur.execute("""
                        INSERT INTO citations (citing_document_id, cited_case_name, cited_reporter_citation, treatment, paragraph_ref)
                        VALUES (%s, %s, %s, %s, %s);
                    """, (
                        document_id, cited_case_name, citation.get("cited_reporter_citation"),
                        _validate_enum(citation.get("treatment"), _VALID_TREATMENTS),
                        _clean_paragraph_ref(citation.get("paragraph_ref")),
                    ))

                for ordinal, held in enumerate(extraction.get("held", []), start=1):
                    holding_text = held.get("holding_text")
                    if not holding_text:
                        continue
                    cur.execute("""
                        INSERT INTO document_holdings (document_id, ordinal, holding_text, paragraph_ref)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (document_id, ordinal) DO NOTHING;
                    """, (document_id, ordinal, holding_text, _clean_paragraph_ref(held.get("paragraph_ref"))))

                # prior_document_id is deliberately left NULL here, same as
                # citations.cited_document_id -- resolving which existing
                # document a free-text prior order actually refers to is a
                # separate reconciliation concern, not a promotion-time one.
                prior_history = extraction.get("prior_history") or {}
                # Fall back to a "(Arising out of ...)" reference stripped out
                # of a case_number above when the LLM didn't separately supply
                # prior_case_number itself.
                prior_case_number = prior_history.get("prior_case_number") or (case_arising_refs[0] if case_arising_refs else None)
                if prior_history.get("prior_court_name") or prior_case_number:
                    cur.execute("""
                        INSERT INTO case_appellate_history (
                            document_id, prior_court_name, prior_case_number, prior_order_date, outcome, notes
                        ) VALUES (%s, %s, %s, %s, %s, %s);
                    """, (
                        document_id,
                        prior_history.get("prior_court_name"),
                        prior_case_number,
                        _parse_date(prior_history.get("prior_order_date")),
                        _validate_enum(prior_history.get("outcome"), _VALID_APPELLATE_OUTCOMES),
                        prior_history.get("notes"),
                    ))

                # lead_case_id was computed above, alongside case_counsels.
                if lead_case_id is not None:
                    for ordinal, event in enumerate(extraction.get("timeline", []), start=1):
                        description = event.get("description")
                        if not description:
                            continue
                        cur.execute("""
                            INSERT INTO case_timeline_events (case_id, ordinal, event_date, event_date_text, description)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (case_id, ordinal) DO NOTHING;
                        """, (
                            lead_case_id, ordinal,
                            _parse_date(event.get("event_date")), event.get("event_date_text"), description,
                        ))

            conn.commit()

        scrape_jobs.update_status(ingestion_id, status="PROMOTED", document_id=document_id)
        return document_id

    except PromotionSkipped as e:
        scrape_jobs.update_status(ingestion_id, status="NEEDS_REVIEW", extraction_error=str(e))
        return None
