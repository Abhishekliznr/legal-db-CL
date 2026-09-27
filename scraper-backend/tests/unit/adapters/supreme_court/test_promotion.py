"""
Pytest unit tests for the 2026-09-10 promote_ingestion() fixes in
adapters/supreme_court/promotion.py:

  Bug 1: a re-run hitting the cr_cases ON CONFLICT DO NOTHING branch used to
  still commit a citation sequence number claimed BEFORE the INSERT, and a
  NULL judgment_date used to get a liznr_id built off date.today().year that
  stayed wrong forever once the real date was fixed in review.

  Bug 2: cr_acts's UNIQUE (act_name, act_year) never fires for NULL
  act_year (Postgres default NULLS DISTINCT) -- covered by
  db/migrations/0002_dedupe_acts_nulls_not_distinct.sql, not by Python code
  (see that migration's own header for why no promotion.py change was
  needed), so it isn't re-tested here.

Everything here is mocked: db.connection.get_pooled_connection is replaced
with a fake connection/cursor (no real Postgres), and db.scrape_jobs.get_ingestion
is replaced with a canned ingestion row (no real Postgres there either).
"""

from contextlib import contextmanager
from pathlib import Path

import pytest

from adapters.base import RawJudgmentRecord
from adapters.supreme_court import promotion


class FakeCursor:
    """
    Routes fetchone() results by matching against the executed SQL, so the
    real db.lookups get-or-create helpers and this module's own
    _build_liznr_id/_claim_citation_sequence/_resolve_* run unmodified
    against believable fake data. Anything not specifically recognized (the
    various get-or-create lookups for judge/subject/ministry/category) gets
    an auto-incrementing fake id -- these tests don't care what those ids
    are, only what happens to cr_cases/cr_citation_sequences.
    """

    def __init__(self, insert_returns_case_id, court_code="SCIN"):
        self.executed = []
        self.insert_returns_case_id = insert_returns_case_id
        self.court_code = court_code
        self.claim_count = 0
        self._next_seq = 0
        self._id_counter = 0
        self._last_result = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params or ()))
        sql_upper = sql.upper()

        if "INSERT INTO CR_CASES" in sql_upper and "RETURNING CASE_ID" in sql_upper:
            self._last_result = (self.insert_returns_case_id,) if self.insert_returns_case_id is not None else None
        elif "SELECT COURT_CODE FROM CR_COURTS" in sql_upper:
            self._last_result = (self.court_code,) if self.court_code is not None else (None,)
        elif "INSERT INTO CR_CITATION_SEQUENCES" in sql_upper:
            self.claim_count += 1
            self._next_seq += 1
            self._last_result = (self._next_seq,)
        elif "UPDATE CR_CASES" in sql_upper and "LIZNR_ID" in sql_upper:
            self._last_result = None
        elif "UPDATE CR_RAW_INGESTIONS" in sql_upper:
            self._last_result = None
        else:
            self._id_counter += 1
            self._last_result = (self._id_counter,)

    def fetchone(self):
        return self._last_result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass


def _patch_pooled_connection(monkeypatch, cursor):
    conn = FakeConnection(cursor)

    @contextmanager
    def fake_get_pooled_connection():
        yield conn

    monkeypatch.setattr(promotion, "get_pooled_connection", fake_get_pooled_connection)


def _fake_ingestion(court_id=1, ocr_text="Plain judgment text with no special references."):
    return {
        "ingestion_id": 99,
        "court_id": court_id,
        "ocr_text": ocr_text,
        "blob_pdf_id": None,
    }


def _record(case_number="Civil Appeal No. 1 of 2026", decision_date_raw=None):
    return RawJudgmentRecord(
        pdf_path=Path("/nonexistent/does-not-matter.pdf"),
        source_url="https://example.court.gov.in/judgment/1",
        case_number_raw=case_number,
        party_name_raw="ABC Traders VS XYZ Corporation",
        judge_raw=None,
        decision_date_raw=decision_date_raw,
        cnr_raw=None,
        neutral_citation_raw=None,
        extra={},
    )


@pytest.fixture(autouse=True)
def _no_ner(monkeypatch):
    monkeypatch.setattr(promotion, "extract_acts_sections", lambda text: [])


def _update_sql_for(cursor, needle):
    return [(sql, params) for sql, params in cursor.executed if needle.upper() in sql.upper()]


def _liznr_update_calls(cursor):
    """Statements that actually assign liznr_id (UPDATE ... SET liznr_id = ...) -- narrower than _update_sql_for(cursor, "LIZNR_ID"), which also matches the INSERT INTO cr_cases column list that always names liznr_id."""
    return [
        (sql, params) for sql, params in cursor.executed
        if sql.strip().upper().startswith("UPDATE CR_CASES") and "LIZNR_ID" in sql.upper()
    ]


# ---------------------------------------------------------------------
# Bug 1: re-run must not touch the citation sequence
# ---------------------------------------------------------------------

def test_rerun_conflict_does_not_claim_a_citation_sequence(monkeypatch):
    monkeypatch.setattr(promotion.scrape_jobs, "get_ingestion", lambda ingestion_id: _fake_ingestion())
    cursor = FakeCursor(insert_returns_case_id=None)  # simulates ON CONFLICT DO NOTHING (a re-run)
    _patch_pooled_connection(monkeypatch, cursor)

    result = promotion.promote_ingestion(99, _record(decision_date_raw="2026-01-15"))

    assert result is None
    assert cursor.claim_count == 0
    assert not _update_sql_for(cursor, "cr_citation_sequences")
    # the conflict path must route the ingestion to NEEDS_REVIEW, never touch cr_cases again
    assert not _update_sql_for(cursor, "UPDATE cr_cases")


# ---------------------------------------------------------------------
# Bug 1: NULL judgment_date must not get a liznr_id
# ---------------------------------------------------------------------

def test_null_judgment_date_gets_no_liznr_id(monkeypatch):
    monkeypatch.setattr(promotion.scrape_jobs, "get_ingestion", lambda ingestion_id: _fake_ingestion())
    cursor = FakeCursor(insert_returns_case_id=42)
    _patch_pooled_connection(monkeypatch, cursor)

    result = promotion.promote_ingestion(99, _record(decision_date_raw=None))

    assert result == 42
    assert cursor.claim_count == 0
    assert not _update_sql_for(cursor, "cr_citation_sequences")
    assert not _liznr_update_calls(cursor)

    insert_calls = _update_sql_for(cursor, "INSERT INTO cr_cases")
    assert len(insert_calls) == 1
    _, insert_params = insert_calls[0]
    assert insert_params[0] is None  # liznr_id param is NULL at INSERT time


# ---------------------------------------------------------------------
# A normal (fresh insert, real date) case gets exactly one claim
# ---------------------------------------------------------------------

def test_normal_case_claims_exactly_once(monkeypatch):
    monkeypatch.setattr(promotion.scrape_jobs, "get_ingestion", lambda ingestion_id: _fake_ingestion())
    cursor = FakeCursor(insert_returns_case_id=7, court_code="SCIN")
    _patch_pooled_connection(monkeypatch, cursor)

    result = promotion.promote_ingestion(99, _record(decision_date_raw="2026-03-01"))

    assert result == 7
    assert cursor.claim_count == 1

    liznr_update_calls = _liznr_update_calls(cursor)
    assert len(liznr_update_calls) == 1
    sql, params = liznr_update_calls[0]
    assert "UPDATE cr_cases" in sql or "UPDATE cr_cases".upper() in sql.upper()
    liznr_id, updated_case_id = params
    assert updated_case_id == 7
    assert liznr_id == "LIZNR/SCIN/0001/2026"

    insert_calls = _update_sql_for(cursor, "INSERT INTO cr_cases")
    _, insert_params = insert_calls[0]
    assert insert_params[0] is None  # still NULL at INSERT time -- assigned only afterward


def test_missing_court_code_claims_no_sequence_and_assigns_no_liznr_id(monkeypatch):
    """_build_liznr_id's existing contract (unrelated to these two bugs, but exercised by the same code path): no court_code means no liznr_id, and therefore no sequence claim either."""
    monkeypatch.setattr(promotion.scrape_jobs, "get_ingestion", lambda ingestion_id: _fake_ingestion())
    cursor = FakeCursor(insert_returns_case_id=8, court_code=None)
    _patch_pooled_connection(monkeypatch, cursor)

    result = promotion.promote_ingestion(99, _record(decision_date_raw="2026-03-01"))

    assert result == 8
    assert cursor.claim_count == 0
    assert not _liznr_update_calls(cursor)


# ---------------------------------------------------------------------
# assign_liznr_id_for_reviewed_case -- the "assign later" helper
# ---------------------------------------------------------------------

class _SingleRowCursor(FakeCursor):
    """For assign_liznr_id_for_reviewed_case's own SELECT ... FROM cr_cases lookup, layered on top of the same routing FakeCursor uses for the sequence/court-code/UPDATE calls."""

    def __init__(self, case_row, **kwargs):
        super().__init__(**kwargs)
        self._case_row = case_row

    def execute(self, sql, params=None):
        sql_upper = sql.upper()
        if "SELECT COURT_ID, JUDGMENT_DATE, LIZNR_ID" in sql_upper:
            self.executed.append((sql, params or ()))
            self._last_result = self._case_row
            return
        super().execute(sql, params)


def test_assign_liznr_id_for_reviewed_case_assigns_once_date_is_fixed(monkeypatch):
    import datetime
    cursor = _SingleRowCursor(case_row=(1, datetime.date(2026, 2, 1), None), insert_returns_case_id=None, court_code="SCIN")
    _patch_pooled_connection(monkeypatch, cursor)

    liznr_id = promotion.assign_liznr_id_for_reviewed_case(55)

    assert liznr_id == "LIZNR/SCIN/0001/2026"
    assert cursor.claim_count == 1


def test_assign_liznr_id_for_reviewed_case_noop_while_date_still_null(monkeypatch):
    cursor = _SingleRowCursor(case_row=(1, None, None), insert_returns_case_id=None, court_code="SCIN")
    _patch_pooled_connection(monkeypatch, cursor)

    liznr_id = promotion.assign_liznr_id_for_reviewed_case(55)

    assert liznr_id is None
    assert cursor.claim_count == 0


# ---------------------------------------------------------------------
# Legal NER fills acts/sections at promotion
# ---------------------------------------------------------------------

def _insert_params(cursor):
    insert_calls = _update_sql_for(cursor, "INSERT INTO cr_cases")
    assert len(insert_calls) == 1
    sql, params = insert_calls[0]
    columns = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    placeholder_columns = [c for c in columns if c not in ("industries", "document_type")]
    return dict(zip(placeholder_columns, params))


def test_ner_acts_and_sections_are_written_at_promotion(monkeypatch):
    monkeypatch.setattr(promotion.scrape_jobs, "get_ingestion", lambda ingestion_id: _fake_ingestion())
    monkeypatch.setattr(promotion, "extract_acts_sections", lambda text: [
        {"act_name": "Indian Penal Code, 1860", "sections": ["302", "34"]},
        {"act_name": "Code of Criminal Procedure, 1973", "sections": ["439"]},
    ])
    cursor = FakeCursor(insert_returns_case_id=11)
    _patch_pooled_connection(monkeypatch, cursor)

    assert promotion.promote_ingestion(99, _record(decision_date_raw="2026-03-01")) == 11

    params = _insert_params(cursor)
    assert len(params["acts"]) == 2
    assert len(params["sections"]) == 3


def test_ner_finding_nothing_leaves_acts_and_sections_empty(monkeypatch):
    monkeypatch.setattr(promotion.scrape_jobs, "get_ingestion", lambda ingestion_id: _fake_ingestion())
    cursor = FakeCursor(insert_returns_case_id=12)
    _patch_pooled_connection(monkeypatch, cursor)

    assert promotion.promote_ingestion(99, _record(decision_date_raw="2026-03-01")) == 12

    params = _insert_params(cursor)
    assert params["acts"] == []
    assert params["sections"] == []
