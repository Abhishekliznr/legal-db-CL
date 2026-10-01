from pathlib import Path

from adapters.base import BatchItem
from adapters.supreme_court import adapter


def _row(**overrides):
    row = {
        "pdf_url": "https://www.sci.gov.in/view-pdf/?diary_no=1",
        "case_number": "C.A. No. 1/2025",
        "diary_number": "1/2025",
        "party_name": "A VS B",
        "judge": None,
        "decision_date": "01-02-2025",
        "neutral_citation": "2025 INSC 1",
        "advocate_raw": None,
        "bench_raw": None,
        "language": "English",
    }
    row.update(overrides)
    return row


def test_process_item_keeps_metadata_of_row_without_pdf_link(tmp_path):
    outcome = adapter.SupremeCourtAdapter().process_item(
        adapter._Session(download_dir=tmp_path), BatchItem(key="k", payload=_row(pdf_url=None)),
    )
    assert outcome.skip_reason is None
    assert outcome.judgment_missing == ("NOT_PUBLISHED", "no PDF link")
    assert outcome.record.pdf_path is None
    assert outcome.record.source_url is None
    assert outcome.record.case_number_raw == "C.A. No. 1/2025"


def test_process_item_keeps_metadata_when_download_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(adapter, "download_pdf", lambda url, dest: "HTTP 404")
    outcome = adapter.SupremeCourtAdapter().process_item(
        adapter._Session(download_dir=tmp_path), BatchItem(key="k", payload=_row()),
    )
    assert outcome.skip_reason is None
    assert outcome.judgment_missing == ("DOWNLOAD_FAILED", "PDF download failed: HTTP 404")
    assert outcome.record.pdf_path is None
    assert outcome.record.source_url == "https://www.sci.gov.in/view-pdf/?diary_no=1"


def test_process_item_builds_record_from_payload(monkeypatch, tmp_path):
    def fake_download(url, dest: Path):
        dest.write_bytes(b"%PDF-1.4 fake")
        return None

    monkeypatch.setattr(adapter, "download_pdf", fake_download)
    outcome = adapter.SupremeCourtAdapter().process_item(
        adapter._Session(download_dir=tmp_path), BatchItem(key="k", payload=_row()),
    )
    record = outcome.record
    assert outcome.skip_reason is None
    assert outcome.judgment_missing is None
    assert record.pdf_path.exists()
    assert record.case_number_raw == "C.A. No. 1/2025"
    assert record.cnr_raw == "1/2025"
    assert record.neutral_citation_raw == "2025 INSC 1"
    assert record.extra["language"] == "English"


def test_discover_dedupes_rows_and_marks_already_promoted(monkeypatch):
    rows = [
        _row(case_number="C.A. No. 1/2025"),
        _row(case_number="C.A. No. 1/2025", pdf_url="https://www.sci.gov.in/other.pdf"),
        _row(case_number="C.A. No. 2/2025"),
        _row(case_number=None, diary_number=None, pdf_url=None),
    ]

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def check_access(self):
            pass

    monkeypatch.setattr(adapter, "SupremeCourtBrowserClient", FakeClient)
    monkeypatch.setattr(adapter, "_discover_window", lambda client, f, t, i, n: rows if i == 1 else [])
    monkeypatch.setattr(adapter.court_config, "get_court_id_by_code", lambda code: 1)
    monkeypatch.setattr(adapter.scrape_jobs, "get_promoted_case_numbers", lambda court_id: {"C.A. No. 2/2025"})
    monkeypatch.setattr(adapter.time, "sleep", lambda s: None)

    items = adapter.SupremeCourtAdapter().discover("2025-01-01", "2025-02-15")

    assert [i.key for i in items] == ["C.A. No. 1/2025", "C.A. No. 2/2025"]
    assert items[0].done_reason is None
    assert items[0].payload["pdf_url"] == "https://www.sci.gov.in/view-pdf/?diary_no=1"
    assert items[1].done_reason == "already in database"


def test_split_into_batches_caps_windows_at_30_days():
    assert adapter._split_into_batches("2025-01-01", "2025-02-15") == [
        ("01-01-2025", "30-01-2025"),
        ("31-01-2025", "15-02-2025"),
    ]
