import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests
from playwright.sync_api import sync_playwright

from .exceptions import (
    SourceAccessError,
    SourceRateLimitError,
    SourceUnavailableError,
)

logger = logging.getLogger("scraper_backend_v2.adapters.supreme_court.client")

BASE_URL = "https://www.sci.gov.in"
SEARCH_URL = f"{BASE_URL}/judgements-judgement-date/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


@dataclass
class SourceCheck:
    ok: bool
    status_code: Optional[int]
    title: Optional[str]
    detail: str


class SupremeCourtBrowserClient:
    """Browser transport only. Parsing and business logic stay in the adapter."""

    def __init__(
        self,
        *,
        headless: bool = True,
        navigation_timeout_ms: int = 60000,
        source_delay_seconds: float = 2.0,
    ):
        self.headless = headless
        self.navigation_timeout_ms = navigation_timeout_ms
        self.source_delay_seconds = source_delay_seconds
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None

    def __enter__(self):
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox"],
        )
        self._context = self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=USER_AGENT,
        )
        self.page = self._context.new_page()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def _classify_response(self, status: Optional[int], title: str) -> None:
        if status is None:
            return

        if status == 403:
            raise SourceAccessError(
                f"sci.gov.in rejected access with HTTP 403 "
                f"(title={title!r}). This is a source-access/WAF decision; "
                "the batch will not retry automatically."
            )

        if status == 429:
            raise SourceRateLimitError(
                "sci.gov.in returned HTTP 429. Retry later using the "
                "configured backoff policy."
            )

        if 500 <= status <= 599:
            raise SourceUnavailableError(
                f"sci.gov.in returned HTTP {status}."
            )

        if 400 <= status <= 499:
            raise SourceAccessError(
                f"sci.gov.in returned HTTP {status} (title={title!r})."
            )

    def check_access(self) -> SourceCheck:
        """Perform one source check before starting a scrape."""
        try:
            response = self.page.goto(
                SEARCH_URL,
                wait_until="domcontentloaded",
                timeout=self.navigation_timeout_ms,
            )
            self.page.wait_for_timeout(1500)
            title = (self.page.title() or "").strip()
            status = response.status if response else None
            self._classify_response(status, title)

            if not self.page.locator("#from_date").count():
                return SourceCheck(
                    ok=False,
                    status_code=status,
                    title=title,
                    detail="Search page loaded but #from_date was not found.",
                )

            return SourceCheck(
                ok=True,
                status_code=status,
                title=title,
                detail="SCI search page is reachable.",
            )
        except (SourceAccessError, SourceRateLimitError, SourceUnavailableError):
            raise
        except Exception as exc:
            raise SourceUnavailableError(
                f"Could not reach sci.gov.in: {exc}"
            ) from exc

    def open_search_page(self) -> None:
        response = self.page.goto(
            SEARCH_URL,
            wait_until="domcontentloaded",
            timeout=self.navigation_timeout_ms,
        )
        self.page.wait_for_timeout(1500)
        title = (self.page.title() or "").strip()
        self._classify_response(response.status if response else None, title)
        time.sleep(self.source_delay_seconds)

    def download_pdf(self, pdf_url: str, dest_path) -> bool:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": f"{BASE_URL}/",
        }
        try:
            response = requests.get(
                pdf_url,
                headers=headers,
                timeout=30,
            )
            if response.status_code == 403:
                raise SourceAccessError(
                    f"SCI PDF endpoint rejected access with HTTP 403: {pdf_url}"
                )
            if response.status_code == 429:
                raise SourceRateLimitError(
                    f"SCI PDF endpoint returned HTTP 429: {pdf_url}"
                )
            if 500 <= response.status_code <= 599:
                raise SourceUnavailableError(
                    f"SCI PDF endpoint returned HTTP {response.status_code}: {pdf_url}"
                )
            if response.status_code == 200 and response.content.startswith(b"%PDF"):
                dest_path.write_bytes(response.content)
                return True
            return False
        except requests.RequestException as exc:
            raise SourceUnavailableError(
                f"PDF download failed for {pdf_url}: {exc}"
            ) from exc
