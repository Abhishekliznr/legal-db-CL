"""
Pytest unit tests for pipeline/llm_enrichment.py's 2026-09-10 rewrite.

Everything here is mocked: requests.post is monkeypatched (no network) and
db.connection.get_pooled_connection is monkeypatched with a fake connection/
cursor (no real Postgres). See tests/manual/llm_enrichment_provisions_e2e.py
for the real-Postgres end-to-end check this suite complements rather than
replaces.
"""

import json
import re
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from pipeline import llm_enrichment as le


# ---------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_module_state():
    """_json_schema_unsupported is process-wide by design (see its own docstring) -- reset between tests so one test's fallback doesn't leak into the next."""
    le._json_schema_unsupported = False
    yield
    le._json_schema_unsupported = False


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(le.time, "sleep", lambda *_a, **_kw: None)


@pytest.fixture
def azure_env(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")


def _chat_response(content, finish_reason="stop", refusal=None):
    message = {"role": "assistant", "content": content}
    if refusal is not None:
        message["refusal"] = refusal
    resp = MagicMock()
    resp.ok = True
    resp.json.return_value = {"choices": [{"message": message, "finish_reason": finish_reason}]}
    resp.status_code = 200
    return resp


def _error_response(status_code, text="", headers=None):
    resp = MagicMock()
    resp.ok = False
    resp.status_code = status_code
    resp.text = text
    resp.headers = headers or {}
    return resp


_SAMPLE_RESULT = {
    "case_note": "Criminal - Bail - Section 439 of Code of Criminal Procedure, 1973 - Held, bail granted - Appeal dismissed",
    "conclusion": "The court found in favour of the applicant.",
    "subject": None,
    "industries": [],
    "ministries": [],
    "disposition_category": "Allowed",
    "favouring_party": "Petitioner",
    "provisions": [],
}


class FakeCursor:
    """
    execute() just records calls and asserts %s-placeholder/param parity.
    fetchone() returns queued rows first (used to seed the one-time case
    row fetch in _fetch_case_row), then falls back to an auto-incrementing
    fake id -- every get-or-create helper in db/lookups.py does an
    INSERT...RETURNING (or a follow-up SELECT) expecting exactly one id
    back, and none of these tests care what the id actually is.
    """

    def __init__(self, connection_open_flag):
        self._flag = connection_open_flag
        self.executed = []
        self._id_counter = 0
        self._last_result = (0,)

    def execute(self, sql, params=None):
        self._flag["open"] = True
        self.executed.append((sql, params or ()))
        placeholder_count = sql.count("%s")
        param_count = len(params or ())
        assert placeholder_count == param_count, f"placeholder/param mismatch: {placeholder_count} vs {param_count} in: {sql}"
        self._id_counter += 1
        self._last_result = (self._id_counter,)

    def fetchone(self):
        return self._last_result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, connection_open_flag, cursor):
        self._flag = connection_open_flag
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        self._flag["open"] = False

    def rollback(self):
        self._flag["open"] = False


def _make_pooled_connection_mock(monkeypatch, connection_open_flag, cursor, fetchone_queue=None):
    """Patches db.connection.get_pooled_connection (imported into llm_enrichment as get_pooled_connection) with a context manager yielding a FakeConnection/FakeCursor pair. `fetchone_queue` lets a test script successive fetchone() return values."""
    queue = list(fetchone_queue or [])

    def fake_fetchone():
        if queue:
            return queue.pop(0)
        return cursor._last_result

    cursor.fetchone = fake_fetchone
    conn = FakeConnection(connection_open_flag, cursor)

    @contextmanager
    def fake_get_pooled_connection():
        connection_open_flag["open"] = True
        try:
            yield conn
        finally:
            pass

    monkeypatch.setattr(le, "get_pooled_connection", fake_get_pooled_connection)


# ---------------------------------------------------------------------
# A. Excerpt selection
# ---------------------------------------------------------------------

def test_strip_ocr_noise_removes_signature_block_and_dot_leaders_and_page_numbers():
    text = (
        "Some real content here.\n"
        "Digitally signed by\nJOHN DOE\nDate: 2026-01-01\nReason: Approved\nSignature Not Verified\n"
        "Conclusion .......................... 42\n"
        "17\n"
        "More real content after.\n"
    )
    cleaned = le._strip_ocr_noise(text)
    assert "Digitally signed by" not in cleaned
    assert "Signature Not Verified" not in cleaned
    assert "...." not in cleaned
    assert not re.search(r"^\s*17\s*$", cleaned, re.MULTILINE)
    assert "Some real content here." in cleaned
    assert "More real content after." in cleaned


def test_build_excerpt_short_judgment_is_full_judgment():
    text = "A short judgment. " * 50  # well under _FULL_TEXT_LIMIT
    excerpt, mode = le._build_excerpt(text)
    assert excerpt == text.strip()
    assert "FULL JUDGMENT" in mode


def test_build_excerpt_long_judgment_anchors_tail_on_real_conclusion_heading_in_second_half():
    toc_line = "CONCLUSION .......................... 42\n"  # near the top -- must be ignored
    opening = "OPENING FACTS SECTION.\n"
    filler = "Middle argument-by-argument reasoning text, not the operative finding. " * 4000
    real_heading = "FINAL CONCLUSION\n"
    tail_body = "The appeal is allowed and the conviction is set aside. " * 50

    text = toc_line + opening + filler + real_heading + tail_body
    assert len(text) > le._FULL_TEXT_LIMIT

    excerpt, mode = le._build_excerpt(text)
    assert "long judgment" in mode
    assert "middle omitted" in mode
    # the TOC "CONCLUSION" line was stripped as a dot-leader line, so the
    # only heading left for the anchor search is the real one
    assert "FINAL CONCLUSION" in excerpt
    assert "The appeal is allowed" in excerpt
    assert "[... middle of judgment omitted ...]" in excerpt
    # the excerpt is bounded to roughly head+tail regardless of how long the
    # source judgment is -- the bulk of the 4000x-repeated filler must not
    # survive into it
    assert len(excerpt) < le._HEAD_CHARS + le._TAIL_CHARS + 200
    assert len(excerpt) < len(text) / 5


# ---------------------------------------------------------------------
# B. Connected matters
# ---------------------------------------------------------------------

def test_detect_connected_matters_finds_seven_and_ignores_blank_number_and_toc_repeats():
    cause_title_lines = "\n".join(f"SLP (CRL.) NO. {13980 + i}/2025" for i in range(7))
    blank_number_line = "SLP (CRL.) NO. _____ OF 2026"
    body = (
        cause_title_lines + "\n" + blank_number_line + "\n"
        "J U D G M E N T\n"
        "TABLE OF CONTENTS\n"
        "SLP (CRL.) NO. 13980/2025 .......... 1\n"
        "SLP (CRL.) NO. 13981/2025 .......... 5\n"
        + ("Body text. " * 500)
    )
    matters = le._detect_connected_matters(body)
    assert len(matters) == 7
    assert all("13980" in m or "13981" in m or "13982" in m or "13983" in m
               or "13984" in m or "13985" in m or "13986" in m for m in matters)
    assert not any("_____" in m for m in matters)


def test_detect_connected_matters_single_case_returns_zero_or_one():
    text = "CIVIL APPEAL NO. 123 OF 2024\nJUDGMENT\n" + ("Body text. " * 100)
    matters = le._detect_connected_matters(text)
    assert len(matters) == 1


# ---------------------------------------------------------------------
# C. Schema strictness
# ---------------------------------------------------------------------

def _walk_objects(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for value in schema.values():
            yield from _walk_objects(value)
    elif isinstance(schema, list):
        for item in schema:
            yield from _walk_objects(item)


def test_json_schema_every_object_has_additional_properties_false_and_required_matches_properties():
    objects = list(_walk_objects(le._JSON_SCHEMA))
    assert objects, "expected to find at least one object schema"
    for obj in objects:
        assert obj.get("additionalProperties") is False
        assert set(obj.get("required", [])) == set(obj.get("properties", {}).keys())


def test_json_schema_enums_use_closed_lists():
    industries_enum = le._JSON_SCHEMA["properties"]["industries"]["items"]["enum"]
    ministries_enum = le._JSON_SCHEMA["properties"]["ministries"]["items"]["enum"]
    assert industries_enum == le.CANONICAL_INDUSTRIES
    assert ministries_enum == le.KNOWN_MINISTRIES


# ---------------------------------------------------------------------
# D. LLM call flow
# ---------------------------------------------------------------------

def test_call_llm_enrichment_retries_429_then_succeeds(azure_env, monkeypatch):
    responses = [_error_response(429, "rate limited", {"Retry-After": "0"}), _chat_response(json.dumps(_SAMPLE_RESULT))]
    monkeypatch.setattr(le.requests, "post", MagicMock(side_effect=responses))

    result = le.call_llm_enrichment({"case_number": "X"}, user_content="hello")
    assert result.status == "DONE"
    assert result.data["case_note"].startswith("Criminal")
    assert le.requests.post.call_count == 2


def test_call_llm_enrichment_retries_on_length_then_succeeds_at_higher_budget(azure_env, monkeypatch):
    responses = [_chat_response("{}", finish_reason="length"), _chat_response(json.dumps(_SAMPLE_RESULT), finish_reason="stop")]
    mock_post = MagicMock(side_effect=responses)
    monkeypatch.setattr(le.requests, "post", mock_post)

    result = le.call_llm_enrichment({"case_number": "X"}, user_content="hello")
    assert result.status == "DONE"

    first_payload = mock_post.call_args_list[0].kwargs["json"]
    second_payload = mock_post.call_args_list[1].kwargs["json"]
    assert first_payload["max_tokens"] == le._OUTPUT_TOKEN_STEPS[0]
    assert second_payload["max_tokens"] == le._OUTPUT_TOKEN_STEPS[1]


def test_call_llm_enrichment_length_twice_is_truncated(azure_env, monkeypatch):
    responses = [_chat_response("{}", finish_reason="length"), _chat_response("{}", finish_reason="length")]
    monkeypatch.setattr(le.requests, "post", MagicMock(side_effect=responses))

    result = le.call_llm_enrichment({"case_number": "X"}, user_content="hello")
    assert result.status == "TRUNCATED"
    assert result.data is None


def test_call_llm_enrichment_falls_back_to_json_object_on_400_and_still_parses(azure_env, monkeypatch):
    fenced_json = "```json\n" + json.dumps(_SAMPLE_RESULT) + "\n```"
    responses = [
        _error_response(400, "Unsupported parameter: 'response_format.json_schema' is not supported"),
        _chat_response(fenced_json),
    ]
    mock_post = MagicMock(side_effect=responses)
    monkeypatch.setattr(le.requests, "post", mock_post)

    result = le.call_llm_enrichment({"case_number": "X"}, user_content="hello")
    assert result.status == "DONE"
    assert result.data["disposition_category"] == "Allowed"
    assert le._json_schema_unsupported is True

    second_payload = mock_post.call_args_list[1].kwargs["json"]
    assert second_payload["response_format"] == {"type": "json_object"}


def test_call_llm_enrichment_refusal_is_failed(azure_env, monkeypatch):
    monkeypatch.setattr(le.requests, "post", MagicMock(return_value=_chat_response(None, refusal="cannot help with that")))
    result = le.call_llm_enrichment({"case_number": "X"}, user_content="hello")
    assert result.status == "FAILED"
    assert "refused" in result.error


def test_call_llm_enrichment_not_configured_when_azure_env_missing(monkeypatch):
    for var in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    result = le.call_llm_enrichment({"case_number": "X"}, user_content="hello")
    assert result.status == "NOT_CONFIGURED"


# ---------------------------------------------------------------------
# F/G/H. enrich_case
# ---------------------------------------------------------------------

def test_enrich_case_holds_no_connection_during_the_llm_call(azure_env, monkeypatch):
    flag = {"open": False}
    cursor = FakeCursor(flag)
    _make_pooled_connection_mock(
        monkeypatch, flag, cursor,
        fetchone_queue=[("Case No. 1", "some ocr text with no provisions in it", None, [], [], None, False, False, False)],
    )

    connection_open_during_post = {"value": None}

    def fake_post(*args, **kwargs):
        connection_open_during_post["value"] = flag["open"]
        return _chat_response(json.dumps(_SAMPLE_RESULT))

    monkeypatch.setattr(le.requests, "post", fake_post)

    ok = le.enrich_case(1)
    assert ok is True
    assert connection_open_during_post["value"] is False


def test_enrich_case_success_writes_done(azure_env, monkeypatch):
    flag = {"open": False}
    cursor = FakeCursor(flag)
    _make_pooled_connection_mock(
        monkeypatch, flag, cursor,
        fetchone_queue=[("Case No. 1", "some ocr text", None, [], [], None, False, False, False)],
    )
    monkeypatch.setattr(le.requests, "post", MagicMock(return_value=_chat_response(json.dumps(_SAMPLE_RESULT))))

    ok = le.enrich_case(1)
    assert ok is True

    update_sql, update_params = cursor.executed[-1]
    assert "enrichment_status = 'DONE'" in update_sql
    assert "enrichment_error = NULL" in update_sql


def test_enrich_case_http_failure_writes_failed(azure_env, monkeypatch):
    flag = {"open": False}
    cursor = FakeCursor(flag)
    _make_pooled_connection_mock(
        monkeypatch, flag, cursor,
        fetchone_queue=[("Case No. 1", "some ocr text", None, [], [], None, False, False, False)],
    )
    monkeypatch.setattr(le.requests, "post", MagicMock(return_value=_error_response(500, "server error")))

    ok = le.enrich_case(1)
    assert ok is False

    status_sql, status_params = cursor.executed[-1]
    assert "enrichment_status = %s" in status_sql
    assert status_params[0] == "FAILED"


def test_enrich_case_no_provisions_sent_produces_update_with_no_provision_columns(azure_env, monkeypatch):
    flag = {"open": False}
    cursor = FakeCursor(flag)
    _make_pooled_connection_mock(
        monkeypatch, flag, cursor,
        fetchone_queue=[("Case No. 1", "plain text judgment with no statutory references at all", None, [], [], None, False, False, False)],
    )
    monkeypatch.setattr(le.requests, "post", MagicMock(return_value=_chat_response(json.dumps(_SAMPLE_RESULT))))

    ok = le.enrich_case(1)
    assert ok is True

    update_sql, _ = cursor.executed[-1]
    for column in ("sections", "acts"):
        assert f"{column} = %s" not in update_sql


def test_enrich_case_provision_dedupe_by_statute_and_number(azure_env, monkeypatch):
    flag = {"open": False}
    cursor = FakeCursor(flag)
    ocr_text = "The accused was charged under Section 302 of the Indian Penal Code, 1860."
    _make_pooled_connection_mock(
        monkeypatch, flag, cursor,
        fetchone_queue=[("Case No. 1", ocr_text, None, [], [], None, False, False, False)],
    )

    result_with_dupe_provisions = dict(_SAMPLE_RESULT)
    result_with_dupe_provisions["provisions"] = [
        {"statute_name": "IPC", "number": "302"},
        {"statute_name": "Indian Penal Code, 1860", "number": "302"},
    ]
    monkeypatch.setattr(le.requests, "post", MagicMock(return_value=_chat_response(json.dumps(result_with_dupe_provisions))))

    # provision_block is supplied by the caller since 2026-09-17 (this
    # module no longer extracts one itself -- see enrich_case's docstring);
    # a real caller would build it from this court's own extraction module,
    # here just the same ocr_text stands in for that.
    ok = le.enrich_case(1, provision_block=ocr_text)
    assert ok is True

    update_sql, update_params = cursor.executed[-1]
    # Fixed column order matches enrich_case's own set_clauses/params construction:
    # base columns, then (when a provision block was sent AND the case had no
    # sections yet) sections/acts, then case_id as the final WHERE param.
    base_columns = ["case_note", "conclusion", "subject", "industries", "ministries", "disposition", "favouring_party"]
    provision_columns = ["sections", "acts"]
    all_columns = base_columns + provision_columns
    values_by_column = dict(zip(all_columns, update_params[:-1]))

    # Both raw entries resolve to the SAME (statute, number) after resolve_act()
    # normalizes "IPC" and "Indian Penal Code, 1860" to the same statute name --
    # only one section_id should be written, not two.
    assert len(values_by_column["sections"]) == 1


def test_enrich_case_no_ocr_text_is_skipped(azure_env, monkeypatch):
    flag = {"open": False}
    cursor = FakeCursor(flag)
    _make_pooled_connection_mock(
        monkeypatch, flag, cursor,
        fetchone_queue=[("Case No. 1", None, None, [], [], None, False, False, False)],
    )
    mock_post = MagicMock()
    monkeypatch.setattr(le.requests, "post", mock_post)

    ok = le.enrich_case(1)
    assert ok is False
    mock_post.assert_not_called()

    status_sql, status_params = cursor.executed[-1]
    assert status_params[0] == "SKIPPED"


def test_enrich_case_not_configured_leaves_pending_and_makes_no_call(monkeypatch):
    for var in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)

    flag = {"open": False}
    cursor = FakeCursor(flag)
    _make_pooled_connection_mock(
        monkeypatch, flag, cursor,
        fetchone_queue=[("Case No. 1", "some ocr text", None, [], [], None, False, False, False)],
    )
    mock_post = MagicMock()
    monkeypatch.setattr(le.requests, "post", mock_post)

    ok = le.enrich_case(1)
    assert ok is False
    mock_post.assert_not_called()
    # only the initial fetch executed -- no status-write UPDATE follows
    assert len(cursor.executed) == 1
