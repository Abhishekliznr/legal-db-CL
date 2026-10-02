from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup


BASE_URL = "https://csis.tshc.gov.in/"


@dataclass
class IARecord:
    ia_number: str = ""
    filing_date: str = ""
    advocate: str = ""
    misc_type: str = ""
    status: str = ""
    prayer: str = ""
    order_date: str = ""
    order_url: str = ""


@dataclass
class PartyRecord:
    serial_number: str = ""
    name: str = ""
    details: str = ""


@dataclass
class OrderRecord:
    order_on: str = ""
    judge_name: str = ""
    date: str = ""
    order_type: str = ""
    order_url: str = ""


@dataclass
class TelanganaCaseStatus:
    # Primary details
    main_number: str = ""
    sr_number: str = ""
    cnr_no: str = ""
    petitioner: str = ""
    respondent: str = ""
    petitioner_advocate: str = ""
    respondent_advocate: str = ""
    case_category: str = ""
    district: str = ""
    filing_date: str = ""
    registration_date: str = ""
    listing_date: str = ""
    case_status: str = ""
    disposal_date: str = ""
    disposal_type: str = ""
    purpose: str = ""
    honble_judges: str = ""

    # Category
    category: str = ""
    sub_category: str = ""
    sub_sub_category: str = ""

    # Other sections
    prayer: str = ""

    ia_records: list[IARecord] = field(default_factory=list)
    petitioners: list[PartyRecord] = field(default_factory=list)
    respondents: list[PartyRecord] = field(default_factory=list)
    orders: list[OrderRecord] = field(default_factory=list)


def _clean_text(value: str) -> str:
    """Normalize whitespace in extracted text."""
    return " ".join(value.split()).strip()


def _get_section_table(
    result_card,
    section_title: str,
):
    """
    Find the table immediately following a section-title heading.
    """

    for heading in result_card.select("h3.section-title"):
        title = _clean_text(heading.get_text(" ", strip=True))

        if title.upper() == section_title.upper():
            return heading.find_next("table")

    return None


def _parse_primary_details(result_card) -> dict[str, str]:
    """
    Parse PRIMARY DETAILS table.

    The table contains rows such as:

    Main Number | value | SR Number | value
    CNR No. | value
    Petitioner | value | Respondent | value
    ...
    """

    table = _get_section_table(result_card, "PRIMARY DETAILS")

    if table is None:
        return {}

    data: dict[str, str] = {}

    for row in table.select("tr"):
        cells = row.find_all(["th", "td"])

        if not cells:
            continue

        # Most rows contain label/value pairs.
        index = 0

        while index < len(cells):
            label = _clean_text(cells[index].get_text(" ", strip=True))

            if index + 1 >= len(cells):
                break

            value = _clean_text(cells[index + 1].get_text(" ", strip=True))

            if label:
                data[label] = value

            index += 2

    return data


def _parse_category(result_card) -> dict[str, str]:
    """Parse CATEGORY section."""

    table = _get_section_table(result_card, "CATEGORY")

    if table is None:
        return {}

    data: dict[str, str] = {}

    for row in table.select("tr"):
        cells = row.find_all(["th", "td"])

        index = 0

        while index < len(cells):
            label = _clean_text(cells[index].get_text(" ", strip=True))

            if index + 1 >= len(cells):
                break

            value = _clean_text(cells[index + 1].get_text(" ", strip=True))

            if label:
                data[label] = value

            index += 2

    return data


def _parse_ia_details(result_card) -> list[IARecord]:
    """Parse IA DETAILS table."""

    table = _get_section_table(result_card, "IA DETAILS")

    if table is None:
        return []

    records: list[IARecord] = []

    rows = table.select("tr")

    # First row is the header.
    for row in rows[1:]:
        cells = row.find_all("td")

        if len(cells) < 7:
            continue

        prayer_link = cells[5].find("a")

        prayer = ""

        if prayer_link:
            prayer = (
                prayer_link.get("data-content")
                or prayer_link.get_text(" ", strip=True)
            )

        order_url = ""

        order_link = cells[7].find("a") if len(cells) > 7 else None

        if order_link and order_link.get("href"):
            order_url = urljoin(BASE_URL, order_link["href"])

        records.append(
            IARecord(
                ia_number=_clean_text(cells[0].get_text(" ", strip=True)),
                filing_date=_clean_text(cells[1].get_text(" ", strip=True)),
                advocate=_clean_text(cells[2].get_text(" ", strip=True)),
                misc_type=_clean_text(cells[3].get_text(" ", strip=True)),
                status=_clean_text(cells[4].get_text(" ", strip=True)),
                prayer=_clean_text(prayer),
                order_date=_clean_text(cells[6].get_text(" ", strip=True)),
                order_url=order_url,
            )
        )

    return records


def _parse_parties(
    result_card,
    section_title: str,
) -> list[PartyRecord]:
    """Parse PETITIONER(S) or RESPONDENT(S) section."""

    table = _get_section_table(result_card, section_title)

    if table is None:
        return []

    records: list[PartyRecord] = []

    for row in table.select("tr"):
        cells = row.find_all("td")

        if len(cells) < 2:
            continue

        serial_number = _clean_text(cells[0].get_text(" ", strip=True))

        # Preserve line separation between name and address/details.
        raw_lines = list(cells[1].stripped_strings)

        if not raw_lines:
            continue

        name = _clean_text(raw_lines[0])
        details = _clean_text(" ".join(raw_lines[1:]))

        records.append(
            PartyRecord(
                serial_number=serial_number,
                name=name,
                details=details,
            )
        )

    return records


def _parse_orders(result_card) -> list[OrderRecord]:
    """Parse ORDERS section."""

    table = _get_section_table(result_card, "ORDERS")

    if table is None:
        return []

    records: list[OrderRecord] = []

    rows = table.select("tr")

    # First row is header.
    for row in rows[1:]:
        cells = row.find_all("td")

        if len(cells) < 5:
            continue

        order_link = cells[4].find("a")

        order_url = ""

        if order_link and order_link.get("href"):
            order_url = urljoin(BASE_URL, order_link["href"])

        records.append(
            OrderRecord(
                order_on=_clean_text(cells[0].get_text(" ", strip=True)),
                judge_name=_clean_text(cells[1].get_text(" ", strip=True)),
                date=_clean_text(cells[2].get_text(" ", strip=True)),
                order_type=_clean_text(cells[3].get_text(" ", strip=True)),
                order_url=order_url,
            )
        )

    return records


def parse_case_status_html(html: str) -> TelanganaCaseStatus:
    """
    Parse Telangana High Court CSIS case-detail HTML.

    Expected root element:

        <div id="resultCard" ...>

    The parser extracts:
        - Primary case details
        - Category
        - IA details
        - Prayer
        - Petitioners
        - Respondents
        - Orders
    """

    soup = BeautifulSoup(html, "html.parser")

    result_card = soup.select_one("#resultCard")

    if result_card is None:
        raise ValueError(
            "CSIS result card '#resultCard' was not found"
        )

    primary = _parse_primary_details(result_card)
    category = _parse_category(result_card)

    prayer = ""

    prayer_element = result_card.select_one("p.prayer-text")

    if prayer_element:
        prayer = _clean_text(
            prayer_element.get_text(" ", strip=True)
        )

    return TelanganaCaseStatus(
        main_number=primary.get("Main Number", ""),
        sr_number=primary.get("SR Number", ""),
        cnr_no=primary.get("CNR No.", ""),
        petitioner=primary.get("Petitioner", ""),
        respondent=primary.get("Respondent", ""),
        petitioner_advocate=primary.get(
            "Petitioner Advocate",
            "",
        ),
        respondent_advocate=primary.get(
            "Respondent Advocate",
            "",
        ),
        case_category=primary.get(
            "Case Category",
            "",
        ),
        district=primary.get("District", ""),
        filing_date=primary.get("Filing Date", ""),
        registration_date=primary.get(
            "Registration Date",
            "",
        ),
        listing_date=primary.get(
            "Listing Date",
            "",
        ),
        case_status=primary.get(
            "Case Status",
            "",
        ),
        disposal_date=primary.get(
            "Disposal Date",
            "",
        ),
        disposal_type=primary.get(
            "Type",
            "",
        ),
        purpose=primary.get("Purpose", ""),
        honble_judges=primary.get(
            "Hon'ble Judges",
            "",
        ),
        category=category.get("Category", ""),
        sub_category=category.get(
            "Sub Category",
            "",
        ),
        sub_sub_category=category.get(
            "Sub Sub Category",
            "",
        ),
        prayer=prayer,
        ia_records=_parse_ia_details(result_card),
        petitioners=_parse_parties(
            result_card,
            "PETITIONER(S)",
        ),
        respondents=_parse_parties(
            result_card,
            "RESPONDENT(S)",
        ),
        orders=_parse_orders(result_card),
    )