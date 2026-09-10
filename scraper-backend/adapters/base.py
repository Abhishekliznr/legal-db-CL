"""
Common adapter interface — one per data source (spec §4.1).

Adapters are deliberately dumb: scrape a court's results table for a date
range, download each judgment PDF, and yield a RawJudgmentRecord per PDF.
They never touch the database or blob storage directly — the orchestrator
(orchestrator/batch_runner.py) owns all of that, so a scraper bug can never
corrupt job-tracking state and a single adapter implementation can be
tested/run without a database at all.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Protocol


@dataclass
class RawJudgmentRecord:
    """
    One scraped judgment, PDF already downloaded to a local temp path.
    Nothing here is trusted as final metadata — these are whatever fields
    the court's results table exposes without extra clicks. Real structured
    parsing happens later in the pipeline (pipeline/regex_extraction.py,
    with pipeline/llm_enrichment.py filling the handful of fields regex
    can't), not here.
    """

    pdf_path: Path
    source_url: str
    case_number_raw: Optional[str] = None
    party_name_raw: Optional[str] = None
    judge_raw: Optional[str] = None
    decision_date_raw: Optional[str] = None
    cnr_raw: Optional[str] = None
    neutral_citation_raw: Optional[str] = None
    extra: dict = field(default_factory=dict)  # anything adapter-specific worth keeping


class ScraperAdapter(Protocol):
    """
    Implemented by adapters/supreme_court/adapter.py and (Phase 2)
    adapters/ecourts/adapter.py.
    """

    def scrape(self, date_from: str, date_to: str, **kwargs) -> Iterator[RawJudgmentRecord]:
        """
        Yields one RawJudgmentRecord per judgment found in [date_from, date_to].
        Streaming (a generator, not a list) so the orchestrator can persist
        each record as it arrives instead of losing an entire run's progress
        to a crash partway through a long date range.
        """
        ...
