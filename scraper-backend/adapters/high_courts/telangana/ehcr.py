"""
Telangana High Court - EHCR adapter

Source:
https://tshc.gov.in/ehcr/orderdate

Workflow:
1. Open EHCR order-date search page
2. Extract CSRF token
3. Fetch CAPTCHA image
4. User enters CAPTCHA manually
5. POST date range + status=Y
6. Parse reported judgments
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup


# ----------------------------------------------------------------------
# Temporary SSL diagnostic workaround
# ----------------------------------------------------------------------

urllib3.disable_warnings(
    urllib3.exceptions.InsecureRequestWarning
)


BASE_URL = "https://tshc.gov.in"
ORDER_DATE_URL = f"{BASE_URL}/ehcr/orderdate"
CAPTCHA_URL = f"{BASE_URL}/ehcr/generateCaptcha"


# ----------------------------------------------------------------------
# DATA MODEL
# ----------------------------------------------------------------------

@dataclass
class EHCRRecord:
    """One reported judgment discovered from EHCR."""

    case_type: Optional[str] = None
    case_number: Optional[str] = None
    case_year: Optional[str] = None

    case_number_raw: Optional[str] = None
    order_date: Optional[str] = None

    title: Optional[str] = None
    source_url: Optional[str] = None

    raw: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------------
# EHCR CLIENT
# ----------------------------------------------------------------------

class EHCRClient:
    """Client for Telangana High Court EHCR Order Date search."""

    def __init__(
        self,
        timeout: int = 30,
        verify: bool = False,
    ) -> None:

        self.timeout = timeout

        self.session = requests.Session()

        # Temporary workaround because tshc.gov.in CAPTCHA endpoint
        # may have TLS/SSL handshake issues.
        self.session.verify = verify

        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/154.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/xml;q=0.9,image/avif,image/webp,"
                    "*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
            }
        )

        self.csrf_token: Optional[str] = None

    # ------------------------------------------------------------------
    # EHCR PAGE
    # ------------------------------------------------------------------

    def open_order_date_page(self) -> str:
        """Open EHCR order-date page and extract CSRF token."""

        response = self.session.get(
            ORDER_DATE_URL,
            timeout=self.timeout,
        )

        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        csrf = soup.select_one(
            'input[name="_csrf"]'
        )

        if csrf is None:
            raise RuntimeError(
                "EHCR CSRF token was not found on the page."
            )

        self.csrf_token = csrf.get("value")

        if not self.csrf_token:
            raise RuntimeError(
                "EHCR CSRF token is empty."
            )

        return response.text

    # ------------------------------------------------------------------
    # CAPTCHA
    # ------------------------------------------------------------------

    def download_captcha(
        self,
        output_path: str | Path = "ehcr_captcha.jpg",
    ) -> Path:
        """
        Download the current EHCR CAPTCHA image.

        CAPTCHA is intentionally not solved automatically.
        """

        output_path = Path(output_path)

        response = self.session.get(
            CAPTCHA_URL,
            params={
                "t": str(int(time.time() * 1000)),
            },
            headers={
                "Referer": ORDER_DATE_URL,
                "Accept": (
                    "image/avif,image/webp,image/apng,"
                    "image/svg+xml,image/*,*/*;q=0.8"
                ),
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
            timeout=self.timeout,
        )

        response.raise_for_status()

        content_type = response.headers.get(
            "Content-Type",
            "",
        ).lower()

        if "image" not in content_type:
            raise RuntimeError(
                "EHCR CAPTCHA endpoint did not return an image. "
                f"Content-Type={content_type}"
            )

        output_path.write_bytes(
            response.content
        )

        print(
            f"CAPTCHA HTTP status: {response.status_code}"
        )

        print(
            "Session cookies after CAPTCHA:",
            self.session.cookies.get_dict(),
        )

        return output_path

    # ------------------------------------------------------------------
    # SEARCH
    # ------------------------------------------------------------------

    def search_reported_cases(
        self,
        from_date: str,
        to_date: str,
        captcha: str,
        save_html: str | Path = "ehcr_result.html",
    ) -> str:
        """
        Search EHCR for REPORTABLE judgments only.

        Dates:
            YYYY-MM-DD
        """

        if not self.csrf_token:
            self.open_order_date_page()

        payload = {
            "_csrf": self.csrf_token,
            "fromdt": from_date,
            "todt": to_date,

            # Y = LR (Reportable Judgements)
            "status": "Y",

            "captcha": captcha,
        }

        print(
            "POST cookies:",
            self.session.cookies.get_dict(),
        )

        response = self.session.post(
            ORDER_DATE_URL,
            data=payload,
            headers={
                "Referer": ORDER_DATE_URL,
                "Origin": BASE_URL,
                "Content-Type": (
                    "application/x-www-form-urlencoded"
                ),
                "Cache-Control": "no-cache",
            },
            timeout=self.timeout,
            allow_redirects=True,
        )

        response.raise_for_status()

        html = response.text

        # Save raw response for debugging/parser development.
        save_path = Path(save_html)

        save_path.write_text(
            html,
            encoding="utf-8",
        )

        return html

    # ------------------------------------------------------------------
    # PARSER
    # ------------------------------------------------------------------

    @staticmethod
    def parse_reported_cases(
        html: str,
    ) -> list[EHCRRecord]:
        """
        Parse reported judgment records from EHCR response.
        """

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        records: list[EHCRRecord] = []

        # --------------------------------------------------------------
        # Check CAPTCHA error first
        # --------------------------------------------------------------

        page_text = EHCRClient._clean_text(
            soup.get_text(" ", strip=True)
        )

        if re.search(
            r"invalid\s+captcha",
            page_text,
            re.IGNORECASE,
        ):
            print(
                "\nERROR: EHCR returned 'Invalid captcha'."
            )

            print(
                "Please generate a NEW CAPTCHA and try again."
            )

            return []

        # --------------------------------------------------------------
        # Parse tables
        # --------------------------------------------------------------

        # EHCR result table is reportTable.
        tables = soup.select("table#reportTable")

        # Fallback in case the ID changes.
        if not tables:
            tables = soup.find_all("table")

        for table in tables:

            rows = table.find_all("tr")

            if not rows:
                continue

            headers: list[str] = []

            first_row_cells = rows[0].find_all(
                ["th", "td"]
            )

            if first_row_cells:
                headers = [
                    EHCRClient._clean_text(
                        cell.get_text(
                            " ",
                            strip=True,
                        )
                    )
                    for cell in first_row_cells
                ]

            # ----------------------------------------------------------
            # Parse each result row
            # ----------------------------------------------------------

            for row in rows[1:]:

                cells = row.find_all(
                    ["td", "th"]
                )

                if not cells:
                    continue

                values = [
                    EHCRClient._clean_text(
                        cell.get_text(
                            " ",
                            strip=True,
                        )
                    )
                    for cell in cells
                ]

                if not values:
                    continue

                row_text = " | ".join(values)

                case_number_raw = (
                    EHCRClient._extract_case_number(
                        row_text
                    )
                )

                if not case_number_raw:
                    continue

                (
                    case_type,
                    case_number,
                    case_year,
                ) = EHCRClient._parse_case_number(
                    case_number_raw
                )

                # ------------------------------------------------------
                # EHCR result table columns:
                #
                # 0 = Judge
                # 1 = Case Number
                # 2 = Other / Connected Case Number
                # 3 = Party Name
                # 4 = Date of Decision
                # 5 = View / PDF
                # ------------------------------------------------------

                order_date = None

                if len(values) > 4:
                    order_date = values[4]

                # ------------------------------------------------------
                # PDF URL
                # ------------------------------------------------------

                source_url = None

                link = row.find(
                    "a",
                    href=True,
                )

                if link:
                    source_url = urljoin(
                        BASE_URL,
                        link["href"],
                    )

                # ------------------------------------------------------
                # Party/title
                # ------------------------------------------------------

                title = (
                    values[3]
                    if len(values) > 3
                    else None
                )

                # ------------------------------------------------------
                # Raw row
                # ------------------------------------------------------

                if (
                    headers
                    and len(headers) == len(values)
                ):
                    record_raw = dict(
                        zip(
                            headers,
                            values,
                        )
                    )
                else:
                    record_raw = {
                        "columns": values
                    }

                # ------------------------------------------------------
                # Create record
                # ------------------------------------------------------

                records.append(
                    EHCRRecord(
                        case_type=case_type,
                        case_number=case_number,
                        case_year=case_year,
                        case_number_raw=case_number_raw,
                        order_date=order_date,
                        title=title,
                        source_url=source_url,
                        raw=record_raw,
                    )
                )

        # --------------------------------------------------------------
        # Remove duplicates
        # --------------------------------------------------------------

        unique_records: list[EHCRRecord] = []

        seen: set[str] = set()

        for record in records:

            key = (
                record.case_number_raw
                or (
                    f"{record.case_type}-"
                    f"{record.case_number}-"
                    f"{record.case_year}"
                )
            )

            key = key.strip().upper()

            if key in seen:
                continue

            seen.add(key)

            unique_records.append(record)

        return unique_records

    # ------------------------------------------------------------------
    # HELPERS
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(
        value: str,
    ) -> str:

        return re.sub(
            r"\s+",
            " ",
            value or "",
        ).strip()

    @staticmethod
    def _extract_case_number(
        text: str,
    ) -> Optional[str]:
        """
        Extract common Telangana case-number formats.

        Examples:
            WP 3975/2023
            CRLP 5435/2018
            W.P. 3975/2023
        """

        if not text:
            return None

        pattern = re.compile(
            r"\b("
            r"[A-Z]{2,15}"
            r"(?:\.[A-Z]{1,5})?"
            r")"
            r"\s*"
            r"(\d{1,8})"
            r"\s*/\s*"
            r"(\d{4})"
            r"\b",
            re.IGNORECASE,
        )

        match = pattern.search(text)

        if not match:
            return None

        case_type = match.group(1)
        case_number = match.group(2)
        case_year = match.group(3)

        case_type = case_type.replace(
            ".",
            "",
        ).upper()

        return (
            f"{case_type} "
            f"{case_number}/"
            f"{case_year}"
        )

    @staticmethod
    def _parse_case_number(
        case_number_raw: str,
    ) -> tuple[
        Optional[str],
        Optional[str],
        Optional[str],
    ]:
        """
        Convert:

            WP 3975/2023

        into:

            WP
            3975
            2023
        """

        if not case_number_raw:
            return None, None, None

        pattern = re.compile(
            r"^([A-Z0-9]+)\s+(\d+)/(\d{4})$",
            re.IGNORECASE,
        )

        match = pattern.search(
            case_number_raw.strip()
        )

        if not match:
            return None, None, None

        return (
            match.group(1).upper(),
            match.group(2),
            match.group(3),
        )


# ----------------------------------------------------------------------
# SIMPLE MANUAL TEST
# ----------------------------------------------------------------------

def interactive_test() -> None:
    """
    Manual EHCR test.

    This does NOT solve CAPTCHA automatically.
    """

    client = EHCRClient()

    print("=" * 70)
    print("TELANGANA HIGH COURT - EHCR TEST")
    print("=" * 70)

    print("\nOpening EHCR page...")

    client.open_order_date_page()

    print("CSRF token obtained successfully.")

    captcha_path = client.download_captcha(
        "ehcr_captcha.jpg"
    )

    print(
        f"\nCAPTCHA saved at:\n"
        f"{captcha_path.resolve()}"
    )

    print(
        "\nOpen the CAPTCHA image and enter it manually."
    )

    from_date = input(
        "\nFrom date (YYYY-MM-DD): "
    ).strip()

    to_date = input(
        "To date (YYYY-MM-DD): "
    ).strip()

    captcha = input(
        "CAPTCHA: "
    ).strip()

    print(
        "\nSearching reportable judgments..."
    )

    html = client.search_reported_cases(
        from_date=from_date,
        to_date=to_date,
        captcha=captcha,
        save_html="ehcr_result.html",
    )

    print(
        "\nRaw response saved to:"
        "\nehcr_result.html"
    )

    records = client.parse_reported_cases(
        html
    )

    print(
        f"\nReported records found: "
        f"{len(records)}"
    )

    for index, record in enumerate(
        records[:20],
        start=1,
    ):
        print(
            f"{index}. "
            f"{record.case_number_raw} | "
            f"{record.order_date or '-'}"
        )

        if record.source_url:
            print(
                f"   URL: {record.source_url}"
            )


if __name__ == "__main__":
    interactive_test()