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
from typing import Any, ContextManager, Iterator, List, Optional, Protocol, Tuple


@dataclass
class RawJudgmentRecord:
    """
    One scraped judgment, PDF already downloaded to a local temp path.
    Nothing here is trusted as final metadata — these are whatever fields
    the court's results table exposes without extra clicks. Real structured
    parsing happens later, in that court's own extraction/promotion module
    (e.g. adapters/supreme_court/extraction.py), with pipeline/llm_enrichment.py
    filling the handful of fields regex can't — not here.
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
    position: Optional[Tuple[int, int]] = None  # (index, total) among cases this run processes -- for log context


class ScraperAdapter(Protocol):
    """
    Implemented by adapters/supreme_court/adapter.py and by each
    adapters/high_courts/<code>/adapter.py.
    """

    def scrape(self, date_from: str, date_to: str, **kwargs) -> Iterator[RawJudgmentRecord]:
        """
        Yields one RawJudgmentRecord per judgment found in [date_from, date_to].
        Streaming (a generator, not a list) so the orchestrator can persist
        each record as it arrives instead of losing an entire run's progress
        to a crash partway through a long date range.
        """
        ...


@dataclass
class BatchItem:
    """One case a resumable adapter discovered; saved in cr_batch_items so a stopped batch can resume from it."""

    key: str  # unique within the batch, e.g. MP's "<Bench>/<Type>/<No>/<Year>"
    payload: dict  # whatever process_item() needs to handle this case again; must be JSON-serialisable
    done_reason: Optional[str] = None  # set by discover() when there's nothing left to do, e.g. "already in database"
    item_id: Optional[int] = None
    position: Optional[int] = None


@dataclass
class ItemOutcome:
    """Exactly one of record / skip_reason is set."""

    record: Optional[RawJudgmentRecord] = None
    skip_reason: Optional[str] = None


class ResumableScraperAdapter(Protocol):
    """
    Adapter that splits discovery from per-case work, so batch_runner can save
    the discovered case list and resume a stopped batch without discovering
    again. batch_runner uses this path whenever an adapter has discover().
    """

    def discover(self, date_from: str, date_to: str, **kwargs) -> List[BatchItem]:
        ...

    def session(self, **kwargs) -> ContextManager[Any]:
        ...

    def process_item(self, session: Any, item: BatchItem) -> ItemOutcome:
        ...


# Source-failure exceptions any adapter can raise — orchestrator/batch_runner.py
# catches these generically (not per-adapter) to classify a batch's failure
# status (SOURCE_BLOCKED/RATE_LIMITED/SOURCE_UNAVAILABLE/STRUCTURE_CHANGED).
# Not specific to any one court's adapter, despite Supreme Court's being the
# first (and so far only) adapter to actually raise them.

class SourceAccessError(Exception):
    """The source rejected the request or access is not authorized."""
    pass


class SourceRateLimitError(SourceAccessError):
    """The source explicitly rate-limited the client."""
    pass


class SourceUnavailableError(SourceAccessError):
    """The source returned a transient 5xx/server failure."""
    pass


class SourceStructureChangedError(Exception):
    """The source page no longer matches the adapter contract."""
    pass
