from __future__ import annotations

import json
from pathlib import Path

from adapters.high_courts.telangana.ehcr import EHCRClient
from adapters.high_courts.telangana.csis import CSISClient


# CSIS case type mapping.
# WP = 63 confirmed from our CSIS testing.
CASE_TYPE_MAP = {
    "WP": 63,
    "CRLA": 19,
    "CRLP": 21,
    "CRLRC": 22,
    "CRP": 23,
}


def normalize_case_type(case_number_raw: str) -> str:
    """
    Extract the case type from values such as:
        WP 13183/2023
        CRLA 113/2017
        CRP 4142/2024
    """
    parts = case_number_raw.strip().split()

    if not parts:
        return ""

    return parts[0].upper()


def parse_case_number(case_number_raw: str):
    """
    Convert:
        WP 13183/2023

    into:
        case_type = WP
        case_number = 13183
        case_year = 2023
    """

    value = case_number_raw.strip()

    parts = value.split()

    if len(parts) < 2:
        raise ValueError(
            f"Invalid case number format: {case_number_raw}"
        )

    case_type = parts[0].upper()

    number_year = parts[1]

    if "/" not in number_year:
        raise ValueError(
            f"Invalid case number/year format: {case_number_raw}"
        )

    case_number, case_year = number_year.split("/", 1)

    return (
        case_type,
        case_number,
        int(case_year),
    )


def main():
    print("=" * 70)
    print("TELANGANA EHCR -> CSIS INTEGRATION TEST")
    print("=" * 70)

    # ---------------------------------------------------------
    # STEP 1
    # Read previously saved EHCR result.
    # ---------------------------------------------------------

    ehcr_file = Path("ehcr_result.html")

    if not ehcr_file.exists():
        raise FileNotFoundError(
            f"EHCR result not found: {ehcr_file.resolve()}"
        )

    html = ehcr_file.read_text(
        encoding="utf-8"
    )

    records = EHCRClient.parse_reported_cases(html)

    print(f"\nEHCR reported records found: {len(records)}")

    if not records:
        print("No EHCR records found.")
        return

    # ---------------------------------------------------------
    # TEST ONLY FIRST 3 RECORDS
    # ---------------------------------------------------------

    test_records = records[:3]

    print(
        f"Testing first {len(test_records)} EHCR records."
    )

    # ---------------------------------------------------------
    # STEP 2
    # Open one CSIS session.
    # ---------------------------------------------------------

    csis = CSISClient()

    print("\nOpening CSIS...")
    csis.open()

    print("CSIS session established.")

    output = []

    # ---------------------------------------------------------
    # STEP 3
    # Process each EHCR record.
    # ---------------------------------------------------------

    for index, record in enumerate(test_records, start=1):

        print("\n" + "=" * 70)
        print(f"CASE {index}/{len(test_records)}")
        print("=" * 70)

        print("EHCR Case:", record.case_number_raw)
        print("Order Date:", record.order_date)

        try:
            (
                case_type_name,
                case_number,
                case_year,
            ) = parse_case_number(
                record.case_number_raw
            )

        except ValueError as exc:
            print("Skipping:", exc)
            continue

        print("Case Type:", case_type_name)
        print("Case Number:", case_number)
        print("Case Year:", case_year)

        # -----------------------------------------------------
        # Check CSIS case type mapping.
        # -----------------------------------------------------

        if case_type_name not in CASE_TYPE_MAP:

            print(
                f"\nCSIS case type mapping not available "
                f"for: {case_type_name}"
            )

            output.append(
                {
                    "case_number_raw": record.case_number_raw,
                    "order_date": record.order_date,
                    "case_type": case_type_name,
                    "case_number": case_number,
                    "case_year": case_year,
                    "cnr": None,
                    "status": "UNSUPPORTED_CASE_TYPE",
                }
            )

            continue

        mtype = CASE_TYPE_MAP[case_type_name]

        print("CSIS mtype:", mtype)

        # -----------------------------------------------------
        # FRESH CAPTCHA FOR THIS CASE
        # -----------------------------------------------------

        captcha_file = Path(
            f"csis_captcha_{index}.jpg"
        )

        print("\nGenerating fresh CAPTCHA...")

        captcha_id = csis.get_captcha(
            save_path=str(captcha_file)
        )

        print(
            "CAPTCHA saved at:",
            captcha_file.resolve()
        )

        print(
            "\nOpen the CAPTCHA image and enter it manually."
        )

        captcha = input("CAPTCHA: ").strip()

        if not captcha:
            print("Empty CAPTCHA. Skipping case.")
            continue

        # -----------------------------------------------------
        # CSIS SEARCH
        # -----------------------------------------------------

        print("\nSearching CSIS...")

        try:
            result = csis.submit_case(
                case_type=mtype,
                case_number=case_number,
                case_year=case_year,
                captcha=captcha,
                captcha_id=captcha_id,
            )

        except Exception as exc:
            print(
                "\nCSIS search failed:",
                exc
            )

            output.append(
                {
                    "case_number_raw": record.case_number_raw,
                    "order_date": record.order_date,
                    "case_type": case_type_name,
                    "case_number": case_number,
                    "case_year": case_year,
                    "cnr": None,
                    "status": "CSIS_ERROR",
                    "error": str(exc),
                }
            )

            continue

        # -----------------------------------------------------
        # RESULT
        # -----------------------------------------------------

        print("\nCSIS Result")
        print("-" * 40)

        print(
            "Case:",
            result.primary.get("mainno")
        )

        print(
            "CNR:",
            result.cnr
        )

        print(
            "Status:",
            result.primary.get("casestatus")
        )

        print(
            "Disposal Date:",
            result.primary.get("disposaldate")
        )

        # -----------------------------------------------------
        # SAVE RESULT
        # -----------------------------------------------------

        output.append(
            {
                "case_number_raw": record.case_number_raw,
                "order_date": record.order_date,
                "ehcr_source_url": record.source_url,
                "case_type": case_type_name,
                "case_number": case_number,
                "case_year": case_year,
                "cnr": result.cnr,
                "case_status": result.primary.get(
                    "casestatus"
                ),
                "disposal_date": result.primary.get(
                    "disposaldate"
                ),
                "judges": result.primary.get(
                    "judges"
                ),
                "order_details": result.order_details,
                "status": "SUCCESS",
            }
        )

    # ---------------------------------------------------------
    # STEP 4
    # Save combined EHCR + CSIS results.
    # ---------------------------------------------------------

    output_dir = Path(
        "csis_results"
    )

    output_dir.mkdir(
        exist_ok=True
    )

    output_file = (
        output_dir /
        "ehcr_csis_test.json"
    )

    output_file.write_text(
        json.dumps(
            output,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print("\n" + "=" * 70)
    print("TEST COMPLETED")
    print("=" * 70)

    print(
        "Combined result saved at:"
    )

    print(
        output_file.resolve()
    )


if __name__ == "__main__":
    main()