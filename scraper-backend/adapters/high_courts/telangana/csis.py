from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests
import urllib3


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


BASE_URL = "https://csis.tshc.gov.in"
CASE_DETAILS_URL = f"{BASE_URL}/getCaseDetails"
CAPTCHA_URL = f"{BASE_URL}/generateCaptcha"


@dataclass
class CSISCaseResult:
    case_number: str
    case_year: int
    case_type: int
    cnr: Optional[str]
    primary: dict[str, Any]
    order_details: list[dict[str, Any]]
    raw_response: dict[str, Any]


class CSISClient:
    """
    Telangana High Court CSIS client.

    Flow:
        1. Generate fresh CAPTCHA
        2. Capture captchaId
        3. User enters CAPTCHA manually
        4. Submit getCaseDetails request
        5. Extract CNR and case details

    A fresh CAPTCHA is generated for every case search.
    """

    def __init__(
        self,
        verify: bool = False,
        timeout: int = 30,
    ) -> None:
        self.timeout = timeout

        self.session = requests.Session()

        # CSIS may have TLS/certificate issues in the local environment.
        # Keep this configurable.
        self.session.verify = verify

        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/154.0.0.0 Safari/537.36"
                ),
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def open(self) -> requests.Response:
        """Open CSIS home page and establish the session."""
        response = self.session.get(
            BASE_URL,
            timeout=self.timeout,
        )

        response.raise_for_status()

        return response

    def get_case_type_map(self) -> dict[str, int]:
        """Fetch all official case types from CSIS endpoint."""
        mapping = {
            "WP": 63,
            "CRLA": 19,
            "CRLP": 21,
            "CRLRC": 22,
            "CRP": 23,
            "CC": 6,
        }
        try:
            resp = self.session.get(f"{BASE_URL}/getMaincasetype", timeout=self.timeout)
            if resp.ok and resp.text:
                import base64
                data = json.loads(base64.b64decode(resp.text))
                for item in data:
                    c_id = item.get("caseType")
                    name = str(item.get("typeName") or "").strip().upper()
                    if c_id and name:
                        mapping[name] = int(c_id)
                        mapping[name.replace(".", "")] = int(c_id)
        except Exception:
            pass
        return mapping

    def fetch_case_details(
        self,
        case_type: int,
        case_number: str | int,
        case_year: int,
        max_retries: int = 5,
    ) -> Optional[CSISCaseResult]:
        """Fetch case details headlessly using automated captcha solving."""
        import tempfile
        import time
        from adapters.captcha_ocr import solve_captcha_image

        for attempt in range(max_retries):
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                captcha_id = self.get_captcha(save_path=str(tmp_path))
                solved = solve_captcha_image(tmp_path.read_bytes())
            except Exception:
                time.sleep(1)
                continue
            finally:
                if tmp_path.exists():
                    tmp_path.unlink()

            if not solved:
                time.sleep(0.5)
                continue

            try:
                res = self.submit_case(
                    case_type=case_type,
                    case_number=case_number,
                    case_year=case_year,
                    captcha=solved,
                    captcha_id=captcha_id,
                )
                if res and res.raw_response and not res.raw_response.get("error"):
                    return res
            except Exception:
                time.sleep(1)
                continue

        return None

    def get_captcha(self, save_path: str = "csis_captcha.jpg") -> str:
        """
        Generate a fresh CAPTCHA.

        Returns:
            captchaId
        """

        response = self.session.get(
            CAPTCHA_URL,
            params={
                "_": str(int(__import__("time").time() * 1000))
            },
            headers={
                "Referer": BASE_URL + "/",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
            timeout=self.timeout,
        )

        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")

        if "image" not in content_type.lower():
            raise RuntimeError(
                f"Unexpected CAPTCHA response Content-Type: {content_type}"
            )

        captcha_path = Path(save_path)
        captcha_path.write_bytes(response.content)

        # CSIS normally provides captchaId through the response/session flow.
        captcha_id = (
            response.headers.get("captchaId")
            or response.headers.get("CaptchaId")
            or response.headers.get("captcha-id")
        )

        if captcha_id:
            return captcha_id

        # If captchaId is not present in headers, print useful information.
        print("CAPTCHA image saved:", captcha_path.resolve())
        print("CAPTCHA response headers:")

        for key, value in response.headers.items():
            if "captcha" in key.lower():
                print(f"  {key}: {value}")

        captcha_id = input("Enter captchaId manually (if available): ").strip()

        if not captcha_id:
            raise RuntimeError(
                "captchaId was not found automatically."
            )

        return captcha_id

    def submit_case(
        self,
        case_type: int,
        case_number: str | int,
        case_year: int,
        captcha: str,
        captcha_id: str,
    ) -> CSISCaseResult:
        """
        Search one case through CSIS.

        Example:
            case_type = 63
            case_number = 13183
            case_year = 2023
        """

        payload = {
            "mtype": str(case_type),
            "mno": str(case_number),
            "myear": str(case_year),
            "captcha": captcha,
            "captchaId": captcha_id,
        }

        response = self.session.post(
            CASE_DETAILS_URL,
            data=payload,
            headers={
                "Referer": BASE_URL + "/",
                "Origin": BASE_URL,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
            timeout=self.timeout,
        )

        response.raise_for_status()

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                "CSIS returned a non-JSON response."
            ) from exc

        # Save raw response for debugging/audit.
        raw_dir = Path("csis_results")
        raw_dir.mkdir(exist_ok=True)

        raw_file = raw_dir / (
            f"{case_type}_{case_number}_{case_year}.json"
        )

        raw_file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Detect common error responses.
        if not isinstance(data, dict):
            raise RuntimeError(
                f"Unexpected CSIS response: {data!r}"
            )

        primary = data.get("primary") or {}

        # CSIS uses cnrno in the observed response.
        cnr = primary.get("cnrno")

        order_details = data.get("orderdetails") or []

        return CSISCaseResult(
            case_number=f"{case_number}",
            case_year=int(case_year),
            case_type=int(case_type),
            cnr=cnr,
            primary=primary,
            order_details=order_details,
            raw_response=data,
        )

    def search_case(
        self,
        case_type: int,
        case_number: str | int,
        case_year: int,
        captcha_id: str,
    ) -> CSISCaseResult:
        """
        Submit a case after the caller has manually entered CAPTCHA.
        """

        captcha = input("Enter CAPTCHA: ").strip()

        if not captcha:
            raise ValueError("CAPTCHA cannot be empty.")

        return self.submit_case(
            case_type=case_type,
            case_number=case_number,
            case_year=case_year,
            captcha=captcha,
            captcha_id=captcha_id,
        )


def interactive_test() -> None:
    print("=" * 70)
    print("TELANGANA HIGH COURT - CSIS TEST")
    print("=" * 70)

    client = CSISClient()

    print("\nOpening CSIS...")
    client.open()

    print("CSIS session established.")

    # ---------------------------------------------------------
    # IMPORTANT:
    # Replace these values according to the case being searched.
    # WP 13183/2023 was confirmed during testing.
    # mtype=63 corresponds to WP in the observed CSIS request.
    # ---------------------------------------------------------

    case_type = 63
    case_number = input("\nCase number (example 13183): ").strip()
    case_year = int(input("Case year (example 2023): ").strip())

    # Fresh CAPTCHA for THIS search.
    print("\nGenerating fresh CAPTCHA...")

    captcha_id = client.get_captcha(
        save_path="csis_captcha.jpg"
    )

    print("\nCAPTCHA saved at:")
    print(Path("csis_captcha.jpg").resolve())

    print("\nOpen the CAPTCHA image and enter it manually.")

    captcha = input("CAPTCHA: ").strip()

    print("\nSearching CSIS...")

    result = client.submit_case(
        case_type=case_type,
        case_number=case_number,
        case_year=case_year,
        captcha=captcha,
        captcha_id=captcha_id,
    )

    print("\n" + "=" * 70)
    print("CSIS RESULT")
    print("=" * 70)

    print("Case:", result.primary.get("mainno"))
    print("CNR:", result.cnr)
    print("Petitioner:", result.primary.get("petitioner"))
    print("Respondent:", result.primary.get("respondent"))
    print("Status:", result.primary.get("casestatus"))
    print("Disposal Date:", result.primary.get("disposaldate"))
    print("Judge:", result.primary.get("judges"))

    print("\nOrder Details:")

    for order in result.order_details:
        print(
            order.get("dateOfOrders"),
            "|",
            order.get("orderType"),
            "|",
            order.get("orderDetails"),
        )

    print("\nRaw response saved in:")
    print(
        Path(
            "csis_results",
            f"{case_type}_{case_number}_{case_year}.json",
        ).resolve()
    )


if __name__ == "__main__":
    interactive_test()

