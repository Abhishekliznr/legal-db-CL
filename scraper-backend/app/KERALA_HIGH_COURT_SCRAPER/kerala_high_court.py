import base64
import io
import json
import re
import sys
import time
from pathlib import Path
from datetime import datetime
from urllib.parse import unquote

from PIL import Image, ImageEnhance, ImageOps

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
    Error as PlaywrightError,
)

try:
    from playwright._impl._errors import TargetClosedError
except ImportError:
    TargetClosedError = PlaywrightError



# ============================================================
# UTF-8 OUTPUT
# ============================================================

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = "https://judgments.ecourts.gov.in"
SEARCH_URL = f"{BASE_URL}/pdfsearch/"


# ============================================================
# KERALA HIGH COURT
# ============================================================

KERALA_STATE_CODE = "32~4"
KERALA_BENCH_CODE = ""


# ============================================================
# DATE RANGE
# ============================================================

FROM_DATE = "2025-01-01"
TO_DATE = "2025-01-02"

# ============================================================
# SCRAPING SETTINGS
# ============================================================

HEADLESS = False

PAGE_LOAD_DELAY = 3.0
SEARCH_DELAY = 4.0
RECORD_DELAY = 1.0
PAGE_DELAY = 2.0

PDF_WAIT = 5.0
MAX_PDF_RETRIES = 3


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

PDF_DIR = BASE_DIR / "pdf"
JSON_FILE = BASE_DIR / "kerala_judgments.json"
DEBUG_DIR = BASE_DIR / "debug"

PDF_DIR.mkdir(
    parents=True,
    exist_ok=True
)

DEBUG_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# SAFE WINDOWS FILENAME
# ============================================================

def safe_filename(text):

    if not text:
        return "unknown"

    text = str(text)

    text = re.sub(
        r'[<>:"/\\|?*]',
        "_",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    text = text.strip().rstrip(". ")

    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }

    if text.upper() in reserved:
        text = "_" + text

    return text[:150]


# ============================================================
# CLEAN TEXT
# ============================================================

def clean_text(text):

    if not text:
        return None

    text = re.sub(
        r"\s+",
        " ",
        str(text)
    )

    return text.strip()


# ============================================================
# SAVE JSON
# ============================================================

def save_json(records):

    data = {
        "Kerala": records
    }

    temp_file = JSON_FILE.with_suffix(".tmp")

    try:

        with open(
            temp_file,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                data,
                file,
                indent=4,
                ensure_ascii=False
            )

        temp_file.replace(
            JSON_FILE
        )

        print(
            f"\nJSON saved: {JSON_FILE}"
        )

    except Exception as error:

        print(
            f"\nCould not save JSON: {error}"
        )


# ============================================================
# SAVE DEBUG HTML
# ============================================================

def save_debug_page(page, filename):

    try:

        path = DEBUG_DIR / filename

        with open(
            path,
            "w",
            encoding="utf-8"
        ) as file:

            file.write(
                page.content()
            )

        print(
            f"\nDebug HTML saved: {path}"
        )

    except Exception as error:

        print(
            f"Could not save debug HTML: {error}"
        )


# ============================================================
# DATE VALIDATION
# ============================================================

def validate_dates():

    try:

        start = datetime.strptime(
            FROM_DATE,
            "%Y-%m-%d"
        )

        end = datetime.strptime(
            TO_DATE,
            "%Y-%m-%d"
        )

    except ValueError:

        print(
            "\nERROR: Invalid date format. Use YYYY-MM-DD."
        )

        return None, None

    if start > end:

        print(
            "\nERROR: From Date cannot be greater than To Date."
        )

        return None, None

    return start, end


# ============================================================
# CAPTCHA OCR ENGINE (SINGLETON)
# ============================================================

GLOBAL_OCR = None


def get_ocr_engine():
    global GLOBAL_OCR
    if GLOBAL_OCR is None:
        try:
            import ddddocr
            GLOBAL_OCR = ddddocr.DdddOcr(show_ad=False)
        except Exception:
            try:
                from ddddocr.compat.v1 import DdddOcr
                GLOBAL_OCR = DdddOcr(show_ad=False)
            except Exception as error:
                print(f"Warning: Failed to load ddddocr engine: {error}")
    return GLOBAL_OCR


def preprocess_captcha_variants(raw_bytes):
    variants = [raw_bytes]
    try:
        image = Image.open(io.BytesIO(raw_bytes))
        w, h = image.size

        # Variant 1: 2x Bicubic + 1.5x Contrast boost
        img1 = image.resize((w * 2, h * 2), Image.Resampling.BICUBIC)
        img1 = ImageEnhance.Contrast(img1).enhance(1.5)
        buf1 = io.BytesIO()
        img1.save(buf1, format="PNG")
        variants.append(buf1.getvalue())

        # Variant 2: Grayscale + Autocontrast
        img2 = ImageOps.grayscale(image)
        img2 = ImageOps.autocontrast(img2)
        buf2 = io.BytesIO()
        img2.save(buf2, format="PNG")
        variants.append(buf2.getvalue())

    except Exception:
        pass

    return variants


def solve_captcha_automatically(page, img_selector=None):
    try:
        if img_selector:
            captcha_img = page.locator(img_selector)
        else:
            captcha_img = page.locator(
                "#captcha_image_pdf, "
                "#captcha_image, "
                "img[src*='captcha'], "
                "img[id*='captcha']"
            )

        if captcha_img.count() == 0 or not captcha_img.first.is_visible():
            return None

        raw_bytes = captcha_img.first.screenshot()
        ocr = get_ocr_engine()

        if ocr is None:
            return None

        variants = preprocess_captcha_variants(raw_bytes)
        candidates = []

        for idx, var_bytes in enumerate(variants):
            try:
                res = ocr.classification(var_bytes)
                clean = re.sub(r"[^a-zA-Z0-9]", "", str(res)).strip()
                if 5 <= len(clean) <= 6:
                    print(f"[Fast OCR Variant {idx}] -> '{clean}'")
                    return clean
                if 4 <= len(clean) <= 6:
                    candidates.append(clean)
            except Exception:
                pass

        if candidates:
            candidates.sort(key=lambda c: (abs(len(c) - 6), -len(c)))
            best = candidates[0]
            print(f"[Fast OCR Fallback Candidate] -> '{best}'")
            return best

        return None

    except Exception as error:
        print(f"Auto OCR note: {error}")
        return None


# ============================================================
# DIAGNOSTIC LOGGING HELPERS
# ============================================================

def log_cookie_snapshot(context, title):
    try:
        cookies = context.cookies()
        session_cookies = {
            c.get("name"): c.get("value")
            for c in cookies
            if "PHPSESSID" in c.get("name", "") or "app_token" in c.get("name", "") or "token" in c.get("name", "").lower()
        }
        print(f"[{title}] Active Cookies ({len(cookies)} total, {len(session_cookies)} session tokens):")
        for k, v in session_cookies.items():
            print(f"  - {k}: {v[:15]}...{v[-6:] if len(v)>20 else v}")
    except Exception as e:
        print(f"[{title}] Could not log cookies: {e}")


# ============================================================
# CLOSE CAPTCHA POPUP
# ============================================================

def dismiss_invalid_captcha_popup(page):

    try:

        popup_buttons = page.locator(
            "button:has-text('OK'), "
            "button:has-text('Ok'), "
            "button:has-text('Close'), "
            ".modal.show button, "
            ".swal2-confirm, "
            "button[data-bs-dismiss='modal']"
        )

        if popup_buttons.count() > 0:

            for i in range(
                popup_buttons.count()
            ):

                button = popup_buttons.nth(i)

                try:

                    if button.is_visible():

                        print(
                            "Closing CAPTCHA popup..."
                        )

                        button.click(
                            timeout=1500
                        )

                        page.wait_for_timeout(
                            500
                        )

                        return True

                except Exception:
                    pass

        page.keyboard.press("Enter")
        page.wait_for_timeout(300)

        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

    except Exception:
        pass

    return False


# ============================================================
# INITIAL SEARCH
# ============================================================

def initial_search(page):

    print(
        "\n" + "=" * 80
    )

    print(
        "INITIAL KERALA SEARCH"
    )

    print(
        "=" * 80
    )

    keyword = page.locator(
        "#search_text"
    )

    if keyword.count() > 0:

        try:
            keyword.fill("")
        except Exception:
            pass

    print(
        "Keyword: EMPTY"
    )

    all_words = page.locator(
        "#inlineRadio3"
    )

    if all_words.count() == 0:

        save_debug_page(
            page,
            "all_words_not_found.html"
        )

        raise RuntimeError(
            "#inlineRadio3 not found."
        )

    all_words.check()

    print(
        "Search option: ALL WORDS"
    )

    captcha_input = page.locator(
        "#captcha"
    )

    search_button = page.locator(
        "#main_search"
    )

    if captcha_input.count() == 0:

        raise RuntimeError(
            "#captcha not found."
        )

    if search_button.count() == 0:

        raise RuntimeError(
            "#main_search not found."
        )

    for attempt in range(1, 51):

        dismiss_invalid_captcha_popup(
            page
        )

        print(
            f"\n[Auto Solver] Initial CAPTCHA attempt {attempt}/50"
        )

        captcha = (
            solve_captcha_automatically(
                page,
                img_selector="#captcha_image"
            )
        )

        if captcha and len(captcha) >= 4:

            print(
                f"[Auto Solver] Filling CAPTCHA: {captcha}"
            )

            captcha_input.fill(
                captcha
            )

            search_button.click()

            page.wait_for_timeout(
                3500
            )

            dismiss_invalid_captcha_popup(
                page
            )

            if (
                "p=pdf_search/home"
                in page.url
                or
                page.locator(
                    "#state_code"
                ).count() > 0
            ):

                print(
                    "\n[Auto Solver] Search CAPTCHA solved successfully!"
                )

                print(
                    f"URL:\n{page.url}"
                )

                return

        refresh_button = page.locator(
            "a[title='Refresh Image']"
        )

        if refresh_button.count() > 0:

            try:

                refresh_button.first.click()

                page.wait_for_timeout(
                    1500
                )

            except Exception:
                pass

    raise RuntimeError("Initial CAPTCHA auto-solve reached max 50 retries.")


# ============================================================
# SELECT KERALA COURT
# ============================================================

def select_kerala(page):

    print(
        "\n" + "=" * 80
    )

    print(
        "SELECTING HIGH COURT OF KERALA"
    )

    print(
        "=" * 80
    )

    court_menu = page.locator(
        "a.nav-link.dropdown-toggle"
    ).filter(
        has_text=re.compile(
            r"Court",
            re.IGNORECASE
        )
    )

    if court_menu.count() > 0:

        try:

            court_menu.first.click()

            page.wait_for_timeout(
                800
            )

        except Exception:
            pass

    state_select = page.locator(
        "#state_code"
    )

    if state_select.count() == 0:

        state_select = page.locator(
            "select[name='state_code']"
        )

    if state_select.count() == 0:

        save_debug_page(
            page,
            "kerala_state_not_found.html"
        )

        raise RuntimeError(
            "Kerala state selector not found."
        )

    print(
        f"Selecting Kerala "
        f"({KERALA_STATE_CODE})..."
    )

    try:

        state_select.select_option(
            KERALA_STATE_CODE
        )

    except Exception:

        state_select.select_option(
            label=re.compile(
                r"Kerala",
                re.IGNORECASE
            )
        )

    page.evaluate(
        """
        (value) => {

            const select =
                document.querySelector(
                    "#state_code"
                ) ||
                document.querySelector(
                    "select[name='state_code']"
                );

            if (select) {

                select.value = value;

                select.dispatchEvent(
                    new Event(
                        "change",
                        {
                            bubbles: true
                        }
                    )
                );
            }

            if (
                typeof get_distData ===
                "function"
            ) {

                try {

                    get_distData(
                        "pdf_search/get_district",
                        value
                    );

                } catch(e) {}
            }

            if (window.jQuery) {

                try {

                    window.jQuery(
                        "#state_code"
                    )
                    .val(value)
                    .trigger("change");

                } catch(e) {}
            }
        }
        """,
        KERALA_STATE_CODE
    )

    page.wait_for_timeout(
        2500
    )

    print(
        "Kerala High Court selected."
    )

    bench_select = page.locator(
        "#dist_code"
    )

    if bench_select.count() == 0:

        bench_select = page.locator(
            "select[name='dist_code']"
        )

    if bench_select.count() == 0:

        print(
            "Bench selector not found."
        )

        return

    print(
        "Waiting for Kerala bench options..."
    )

    try:

        page.wait_for_function(
            """
            () => {

                const select =
                    document.querySelector(
                        "#dist_code"
                    ) ||
                    document.querySelector(
                        "select[name='dist_code']"
                    );

                if (!select) {
                    return false;
                }

                return select.options.length > 1;
            }
            """,
            timeout=20000
        )

    except Exception:
        pass

    option_data = []

    try:

        options = bench_select.locator(
            "option"
        )

        for i in range(
            options.count()
        ):

            option = options.nth(i)

            value = (
                option
                .get_attribute(
                    "value"
                )
                or
                ""
            )

            label = clean_text(
                option.inner_text()
            )

            if value and label:

                option_data.append(
                    (value, label)
                )

        print(
            f"Kerala bench options: "
            f"{option_data}"
        )

    except Exception as error:

        print(
            f"Could not read bench options: "
            f"{error}"
        )

    selected_bench = None

    if KERALA_BENCH_CODE:

        try:

            bench_select.select_option(
                KERALA_BENCH_CODE
            )

            selected_bench = (
                KERALA_BENCH_CODE
            )

        except Exception:
            pass

    if (
        not selected_bench
        and
        option_data
    ):

        selected_bench = (
            option_data[0][0]
        )

        bench_select.select_option(
            selected_bench
        )

    if selected_bench:

        page.evaluate(
            """
            (value) => {

                const select =
                    document.querySelector(
                        "#dist_code"
                    ) ||
                    document.querySelector(
                        "select[name='dist_code']"
                    );

                if (select) {

                    select.value = value;

                    select.dispatchEvent(
                        new Event(
                            "change",
                            {
                                bubbles: true
                            }
                        )
                    );
                }

                if (window.jQuery) {

                    try {

                        window.jQuery(
                            "#dist_code"
                        )
                        .val(value)
                        .trigger("change");

                    } catch(e) {}
                }
            }
            """,
            selected_bench
        )

        print(
            f"Selected Kerala bench: "
            f"{selected_bench}"
        )

    page.wait_for_timeout(
        1000
    )


# ============================================================
# DECISION DATE
# ============================================================

def set_decision_date(page):

    print(
        "\n" + "=" * 80
    )

    print(
        "SETTING KERALA DECISION DATE"
    )

    print(
        "=" * 80
    )

    date_menu = page.locator(
        "a.nav-link.dropdown-toggle"
    ).filter(
        has_text=re.compile(
            r"Decision Date",
            re.IGNORECASE
        )
    )

    if date_menu.count() == 0:

        raise RuntimeError(
            "Decision Date menu not found."
        )

    date_menu.first.click()

    page.wait_for_timeout(
        700
    )

    custom_radio = page.locator(
        "#exampleRadios5"
    )

    if custom_radio.count() == 0:

        raise RuntimeError(
            "#exampleRadios5 not found."
        )

    custom_radio.check()

    try:

        page.evaluate(
            """
            () => {

                const radio =
                    document.querySelector(
                        "#exampleRadios5"
                    );

                if (radio) {

                    radio.checked = true;

                    radio.dispatchEvent(
                        new Event(
                            "change",
                            {
                                bubbles: true
                            }
                        )
                    );
                }

                if (window.jQuery) {

                    window.jQuery(
                        "#exampleRadios5"
                    )
                    .prop(
                        "checked",
                        true
                    )
                    .trigger(
                        "change"
                    );
                }
            }
            """
        )

    except Exception:
        pass

    print(
        "Selected: Custom range"
    )

    page.wait_for_timeout(
        500
    )

    from_input = page.locator(
        "#from_date"
    )

    to_input = page.locator(
        "#to_date"
    )

    if from_input.count() == 0:

        raise RuntimeError(
            "#from_date not found."
        )

    if to_input.count() == 0:

        raise RuntimeError(
            "#to_date not found."
        )

    from_input.fill(
        FROM_DATE
    )

    to_input.fill(
        TO_DATE
    )

    for selector in (
        "#from_date",
        "#to_date"
    ):

        try:

            page.locator(
                selector
            ).dispatch_event(
                "input"
            )

            page.locator(
                selector
            ).dispatch_event(
                "change"
            )

            page.locator(
                selector
            ).dispatch_event(
                "blur"
            )

        except Exception:
            pass

    print(
        f"From Date: {FROM_DATE}"
    )

    print(
        f"To Date:   {TO_DATE}"
    )

    page.wait_for_timeout(
        800
    )


# ============================================================
# FINAL SEARCH
# ============================================================

def final_search(page):

    print(
        "\n" + "=" * 80
    )

    print(
        "RUNNING FINAL KERALA DATE SEARCH"
    )

    print(
        "=" * 80
    )

    search_button = page.locator(
        "button[onclick*='get_details_searchclick']:visible"
    )

    if search_button.count() == 0:

        save_debug_page(
            page,
            "final_search_not_found.html"
        )

        raise RuntimeError(
            "Final search button not found."
        )

    search_button.first.click()

    print(
        "Final Search clicked."
    )

    try:

        page.wait_for_selector(
            "#report_body",
            timeout=60000
        )

    except PlaywrightTimeoutError:

        save_debug_page(
            page,
            "report_body_not_found.html"
        )

        raise RuntimeError(
            "#report_body not found."
        )

    try:

        page.wait_for_function(
            """
            () => {

                const body =
                    document.querySelector(
                        "#report_body"
                    );

                if (!body) {
                    return false;
                }

                return (
                    body.querySelectorAll(
                        "tr"
                    ).length > 0
                );
            }
            """,
            timeout=60000
        )

    except PlaywrightTimeoutError:

        save_debug_page(
            page,
            "empty_results.html"
        )

        raise RuntimeError(
            "Result table stayed empty."
        )

    page.wait_for_timeout(
        int(
            SEARCH_DELAY * 1000
        )
    )

    print(
        "\nKerala search results loaded."
    )

    try:

        info = page.locator(
            "#example_pdf_info"
        )

        if info.count() > 0:

            print(
                "DataTables:",
                clean_text(
                    info.inner_text()
                )
            )

    except Exception:
        pass


# ============================================================
# EXTRACT PDF SOURCE
# ============================================================

def extract_pdf_source(onclick):

    if not onclick:
        return None

    pattern = (
        r"open_pdf\s*\(\s*"
        r"['\"][^'\"]*['\"]\s*,\s*"
        r"['\"][^'\"]*['\"]\s*,\s*"
        r"['\"]([^'\"]+)['\"]"
    )

    match = re.search(
        pattern,
        onclick,
        re.IGNORECASE
    )

    if not match:
        return None

    source = match.group(1)

    source = source.replace(
        "&amp;",
        "&"
    )

    source = unquote(
        source
    )

    return source


# ============================================================
# SPLIT CASE AND PARTY
# ============================================================

def split_case_and_party(case_title):

    case_title = clean_text(
        case_title
    )

    if not case_title:
        return None, None

    match = re.match(
        r"^(.*?)\s+of\s+(.+)$",
        case_title,
        re.IGNORECASE
    )

    if not match:

        return case_title, case_title

    case_number = clean_text(
        match.group(1)
    )

    party = clean_text(
        match.group(2)
    )

    return case_number, party


# ============================================================
# PARSE RESULT ROW
# ============================================================

def parse_result_row(row):

    cells = row.locator(
        "td"
    )

    if cells.count() < 2:

        return None

    serial_no = clean_text(
        cells.nth(0).inner_text()
    )

    case_button = (
        cells.nth(1)
        .locator(
            "button[role='link']"
        )
    )

    if case_button.count() == 0:

        return None

    raw_case_title = clean_text(
        case_button.first.inner_text()
    )

    onclick = (
        case_button.first
        .get_attribute(
            "onclick"
        )
        or
        ""
    )

    case_number, party_name = (
        split_case_and_party(
            raw_case_title
        )
    )

    description = clean_text(
        cells.nth(1).inner_text()
    )

    judge = None

    judge_match = re.search(
        r"Judge\s*:\s*(.*?)(?:"
        r"No\.\s*\d+\s+Supplementary|"
        r"No\.\s*\d+\s+Regular|"
        r"CNR\s*:|$)",
        description or "",
        re.IGNORECASE
    )

    if judge_match:

        judge = clean_text(
            judge_match.group(1)
        )

    cnr = None

    match = re.search(
        r"CNR\s*:\s*([A-Z0-9]+)",
        description or "",
        re.IGNORECASE
    )

    if match:

        cnr = match.group(1)

    registration_date = None

    match = re.search(
        r"Date of registration\s*:\s*([0-9-]+)",
        description or "",
        re.IGNORECASE
    )

    if match:

        registration_date = match.group(1)

    decision_date = None

    match = re.search(
        r"Decision Date\s*:\s*([0-9-]+)",
        description or "",
        re.IGNORECASE
    )

    if match:

        decision_date = match.group(1)

    disposal_nature = None

    match = re.search(
        r"Disposal Nature\s*:\s*(.*?)(?:Court\s*:|$)",
        description or "",
        re.IGNORECASE
    )

    if match:

        disposal_nature = clean_text(
            match.group(1)
        )

    pdf_source = extract_pdf_source(
        onclick
    )

    return {
        "serial_no": serial_no,
        "case_number": case_number,
        "party_name": party_name,
        "judge": judge,
        "court": "High Court of Kerala",
        "bench": "Kerala High Court",
        "cnr": cnr,
        "registration_date": registration_date,
        "decision_date": decision_date,
        "disposal_nature": disposal_nature,
        "pdf_source": pdf_source,
        "pdf_url": None,
        "pdf_path": None,
        "source_page": None,
    }


# ============================================================
# RESULT ROWS
# ============================================================

def get_result_rows(page):

    rows = page.locator(
        "#report_body tr"
    )

    print(
        f"Visible result rows: {rows.count()}"
    )

    return rows


# ============================================================
# CLOSE PDF MODAL
# ============================================================

def close_pdf_modal(page):

    try:

        modal_close = page.locator(
            "#modal_close"
        )

        if modal_close.count() > 0:

            if modal_close.first.is_visible():

                modal_close.first.click(
                    timeout=2000
                )

                page.wait_for_timeout(
                    500
                )

                return

        btn_close = page.locator(
            "#viewFiles .btn-close, "
            ".modal.show .btn-close, "
            "button[data-bs-dismiss='modal']"
        )

        if btn_close.count() > 0:

            if btn_close.first.is_visible():

                btn_close.first.click(
                    timeout=2000
                )

                page.wait_for_timeout(
                    500
                )

                return

        page.keyboard.press(
            "Escape"
        )

        page.wait_for_timeout(
            300
        )

        page.evaluate(
            """
            () => {

                if (window.$) {

                    try {
                        $('#viewFiles').modal('hide');
                    } catch(e) {}

                    $('.modal-backdrop').remove();

                    $('body')
                        .removeClass('modal-open')
                        .css(
                            'overflow',
                            ''
                        );
                }
            }
            """
        )

    except Exception:
        pass


# ============================================================
# CAPTCHA DETECTION
# ============================================================

def captcha_is_visible(page):

    try:

        captcha_img = page.locator(
            "#captcha_image, "
            "img[src*='captcha'], "
            "img[id*='captcha']"
        )

        captcha_input = page.locator(
            "#captcha, "
            "input[name='captcha']"
        )

        if captcha_img.count() > 0:

            for i in range(
                captcha_img.count()
            ):

                if captcha_img.nth(
                    i
                ).is_visible():

                    return True

        if captcha_input.count() > 0:

            for i in range(
                captcha_input.count()
            ):

                if captcha_input.nth(
                    i
                ).is_visible():

                    return True

        body_text = (
            page.locator(
                "body"
            )
            .inner_text()
            .lower()
        )

        if (
            "enter captcha" in body_text
            or
            "invalid captcha" in body_text
            or
            "captcha required" in body_text
            or
            "session timeout" in body_text
        ):

            return True

    except Exception:
        pass

    return False


# ============================================================
# PDF CAPTCHA DETECTION & HANDLING
# ============================================================

def pdf_captcha_visible(page):

    try:
        captcha_inputs = page.locator(
            "#captchapdf, "
            "input[name='captchapdf'], "
            "input[placeholder*='captcha' i], "
            "input[name*='captcha' i], "
            "input[id*='captcha' i], "
            "#pdf_captcha, "
            "#modal_captcha, "
            "#captcha"
        )

        if captcha_inputs.count() > 0:
            for i in range(captcha_inputs.count()):
                try:
                    if captcha_inputs.nth(i).is_visible():
                        return True
                except Exception:
                    pass

        captcha_imgs = page.locator(
            "#captcha_image_pdf, "
            "#viewFiles-body img[src*='securimage'], "
            ".modal img[src*='captcha' i], "
            "#example_pdf_info img[src*='captcha' i], "
            "img[src*='captcha' i], "
            "img[id*='captcha' i]"
        )

        if captcha_imgs.count() > 0:
            for i in range(captcha_imgs.count()):
                try:
                    if captcha_imgs.nth(i).is_visible():
                        return True
                except Exception:
                    pass

        body_text = (
            page.locator("body")
            .inner_text()
            .lower()
        )

        if (
            "enter captcha" in body_text
            or "invalid captcha" in body_text
            or "captcha required" in body_text
            or "session timeout" in body_text
        ):
            return True

    except Exception:
        pass

    return False


def handle_session_timeout_captcha(context, page):
    """
    Handles eCourts 'Oops! Session timeout..!!!' modal and validateCaptcha('Z')
    resynchronizing cookies and Playwright context session.
    """
    if not pdf_captcha_visible(page) and not captcha_is_visible(page):
        return True

    print("\n" + "!" * 80)
    print("SESSION TIMEOUT CAPTCHA DETECTED - validateCaptcha('Z') RECOVERY")
    print("!" * 80)

    log_cookie_snapshot(context, "Cookies Before Session Recovery")
    print(f"[Page URL Before]: {page.url}")

    validation_info = {"url": None, "status": None, "method": None, "body": None}

    def track_validation_response(response):
        try:
            url = response.url
            if "validateCaptcha" in url or "get_pdf_dtls" in url or "pdf_search" in url:
                validation_info["url"] = url
                validation_info["status"] = response.status
                validation_info["method"] = response.request.method
                try:
                    validation_info["body"] = response.text()[:200]
                except Exception:
                    pass
                print(f"\n[AJAX Track] {response.request.method} {url} -> Status {response.status}")
        except Exception:
            pass

    page.on("response", track_validation_response)

    try:
        captcha_input = None
        inputs = page.locator(
            "#captchapdf, "
            "input[name='captchapdf'], "
            "input[placeholder*='captcha' i], "
            "input[name*='captcha' i], "
            "input[id*='captcha' i], "
            "#captcha, "
            "#pdf_captcha"
        )

        for i in range(inputs.count()):
            try:
                inp = inputs.nth(i)
                if inp.is_visible():
                    captcha_input = inp
                    break
            except Exception:
                pass

        for attempt in range(1, 101):
            if not pdf_captcha_visible(page) and not captcha_is_visible(page):
                print("\n[Session Recovery] CAPTCHA modal dismissed successfully!")
                break

            print(f"\n[Session Recovery OCR] Attempt {attempt}/100")

            captcha_code = solve_captcha_automatically(
                page,
                img_selector="#captcha_image_pdf, #captcha_image, #viewFiles-body img[src*='securimage'], .modal img[src*='captcha' i], img[src*='captcha']"
            )

            if captcha_code and len(captcha_code) >= 4 and captcha_input:
                try:
                    page.wait_for_timeout(300)
                    print(f"Submitting CAPTCHA code: '{captcha_code}'")
                    captcha_input.fill(captcha_code)
                    page.wait_for_timeout(350)

                    submit_btn = None
                    buttons = page.locator(
                        "input[value='submit' i], "
                        "input[onclick*='validateCaptcha'], "
                        "input[onclick*='get_pdf_dtls'], "
                        "#main_search, "
                        "#viewFiles-body input.btn-success, "
                        "#viewFiles-body input[type='button'], "
                        "#viewFiles-body button, "
                        "button:has-text('submit')"
                    )

                    for i in range(buttons.count()):
                        try:
                            btn = buttons.nth(i)
                            if btn.is_visible():
                                submit_btn = btn
                                break
                        except Exception:
                            pass

                    if submit_btn:
                        submit_btn.click()
                        print("Clicked session validation submit button.")
                    else:
                        captcha_input.press("Enter")
                        print("Pressed Enter on CAPTCHA input.")

                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    page.wait_for_timeout(2500)

                    if validation_info["url"]:
                        print(f"[Validation Response Info] URL: {validation_info['url']} | Status: {validation_info['status']} | Method: {validation_info['method']}")
                        if validation_info["body"]:
                            print(f"[Validation Body Snippet]: {validation_info['body']}")

                    if not pdf_captcha_visible(page) and not captcha_is_visible(page):
                        print("[Session Recovery] Session restored and modal dismissed!")
                        break

                except Exception as error:
                    print(f"Error during session CAPTCHA submit: {error}")

            refresh_btn = page.locator(
                "#viewFiles-body a[title='Refresh Image'], "
                ".modal a[title='Refresh Image'], "
                "a[title='Refresh Image']"
            )
            if refresh_btn.count() > 0:
                try:
                    refresh_btn.first.click()
                    print("Auto-refreshed CAPTCHA image...")
                    page.wait_for_timeout(1000)
                except Exception:
                    pass

    finally:
        try:
            page.remove_listener("response", track_validation_response)
        except Exception:
            pass

    log_cookie_snapshot(context, "Cookies After Session Recovery")
    print(f"[Page URL After]: {page.url}")

    solved = not pdf_captcha_visible(page) and not captcha_is_visible(page)
    print(f"[Session Recovery Result]: {'SUCCESS' if solved else 'FAILED'}")
    return solved


def handle_pdf_captcha(page):
    return handle_session_timeout_captcha(page.context, page)


def handle_midrun_captcha(page):
    return handle_session_timeout_captcha(page.context, page)


# ============================================================
# IN-PAGE PDF FETCH HELPER
# ============================================================

def fetch_pdf_via_page_evaluate(page, pdf_url):
    """
    Executes a fetch request directly inside the browser page context.
    This automatically uses the browser's active tab session, cookies, origin, and referer.
    """
    if not pdf_url or not page:
        return None

    try:
        js_code = """
        async (url) => {
            try {
                const resp = await fetch(url, {
                    headers: {
                        'Accept': 'application/pdf,application/octet-stream,*/*'
                    }
                });
                if (!resp.ok) return null;
                const blob = await resp.blob();
                return new Promise((resolve) => {
                    const reader = new FileReader();
                    reader.onloadend = () => resolve(reader.result);
                    reader.onerror = () => resolve(null);
                    reader.readAsDataURL(blob);
                });
            } catch (e) {
                return null;
            }
        }
        """
        data_url = page.evaluate(js_code, pdf_url)
        if data_url and "," in data_url:
            base64_str = data_url.split(",", 1)[1]
            pdf_bytes = base64.b64decode(base64_str)
            if pdf_bytes.startswith(b"%PDF"):
                return pdf_bytes
    except Exception as err:
        print(f"[In-Page Fetch Error] {err}")
    return None


# ============================================================
# CAPTURE PDF
# ============================================================

def capture_pdf_from_click(
    context,
    page,
    row,
    record,
    filepath
):

    pdf_button = row.locator(
        "button[role='link']"
    ).first

    if pdf_button.count() == 0:

        print(
            "PDF/View button not found."
        )

        return False

    captured_pdf = {
        "url": None,
        "body": None,
    }

    def handle_response(response):

        try:

            content_type = (
                response.headers
                .get(
                    "content-type",
                    ""
                )
                .lower()
            )

            url = response.url

            if (
                "application/pdf"
                in content_type
                or
                ".pdf" in url.lower()
                or
                url.startswith("chrome-extension:")
            ):

                body = None
                try:
                    body = response.body()
                except Exception as err:
                    print(
                        f"Response body error: {err} - Attempting fallback fetch..."
                    )

                if not body or not body.startswith(b"%PDF"):
                    pdf_src = record.get("pdf_source", "")
                    target_url = url if (url.startswith("http://") or url.startswith("https://")) else None
                    if not target_url and pdf_src:
                        clean_src = pdf_src.split("#")[0].strip()
                        if clean_src:
                            target_url = f"{BASE_URL}/pdfsearch/{clean_src.lstrip('/')}" if not clean_src.startswith("http") else clean_src

                    if target_url:
                        body = fetch_pdf_via_page_evaluate(page, target_url)
                        if not body:
                            try:
                                headers = {
                                    "Referer": page.url if page else f"{BASE_URL}/pdfsearch/index.php",
                                    "Accept": "application/pdf,application/octet-stream,*/*",
                                }
                                req_res = context.request.get(target_url, headers=headers, timeout=15000)
                                if req_res.ok:
                                    raw = req_res.body()
                                    if raw.startswith(b"%PDF"):
                                        body = raw
                            except Exception as req_err:
                                print(
                                    f"Fallback context fetch error: {req_err}"
                                )

                if body and body.startswith(
                    b"%PDF"
                ):

                    captured_pdf[
                        "url"
                    ] = url if (url and not url.startswith("chrome-extension:")) else (target_url or url)

                    captured_pdf[
                        "body"
                    ] = body

                    print(
                        "\nPDF RESPONSE CAPTURED"
                    )

                    print(
                        f"URL: {captured_pdf['url']}"
                    )

                    print(
                        f"Bytes: "
                        f"{len(body)}"
                    )

        except Exception:
            pass

    page.on(
        "response",
        handle_response
    )

    popup = None

    try:

        try:

            with page.expect_popup(
                timeout=3000
            ) as popup_info:

                pdf_button.click()

            popup = popup_info.value

        except PlaywrightTimeoutError:

            try:
                pdf_button.click()
            except Exception:
                pass

        timeout_at = time.time() + PDF_WAIT

        while time.time() < timeout_at:

            # CRITICAL REQUIREMENT: CHECK CAPTURED PDF FIRST BEFORE CAPTCHA TIMEOUT CHECK!
            if captured_pdf["body"]:

                with open(
                    filepath,
                    "wb"
                ) as file:

                    file.write(
                        captured_pdf["body"]
                    )

                record[
                    "pdf_url"
                ] = captured_pdf["url"]

                record[
                    "pdf_path"
                ] = str(
                    filepath.resolve()
                )

                print(
                    "\n[PDF PRESERVATION] PDF RESPONSE CAPTURED & SAVED LOCALLY!"
                )

                print(
                    f"Path:\n{filepath.resolve()}"
                )

                return True

            if pdf_captcha_visible(page) or captcha_is_visible(page):
                print("[Diagnostic] CAPTCHA / Timeout modal detected during PDF capture wait.")
                break

            time.sleep(0.5)

        # Final check if response arrived right as loop ended
        if captured_pdf["body"]:
            with open(
                filepath,
                "wb"
            ) as file:
                file.write(
                    captured_pdf["body"]
                )

            record[
                "pdf_url"
            ] = captured_pdf["url"]

            record[
                "pdf_path"
            ] = str(
                filepath.resolve()
            )

            print(
                "\n[PDF PRESERVATION] PDF RESPONSE SAVED ON FINAL CHECK!"
            )

            return True

    finally:

        try:

            page.remove_listener(
                "response",
                handle_response
            )

        except Exception:
            pass

        if popup:

            try:

                popup.close()

            except Exception:
                pass

        if not pdf_captcha_visible(page):
            close_pdf_modal(
                page
            )


# ============================================================
# DOWNLOAD PDF
# ============================================================

def download_pdf(
    context,
    page,
    row,
    record
):
    case_number = record.get("case_number") or "unknown_case"
    party = record.get("party_name") or "judgment"
    decision_date = record.get("decision_date") or "unknown_date"

    filename = f"{safe_filename(case_number)}_{safe_filename(party)}_{safe_filename(decision_date)}.pdf"
    filepath = PDF_DIR / filename

    print("\n" + "=" * 70)
    print(f"[PDF DOWNLOAD REQUEST] Case: {case_number}")
    print(f"PDF Source: {record.get('pdf_source')}")
    print(f"Target Path: {filepath}")
    print("=" * 70)

    # 1. Existing valid PDF check
    if filepath.exists() and filepath.stat().st_size > 0:
        try:
            with open(filepath, "rb") as file:
                if file.read(5) == b"%PDF-":
                    record["pdf_path"] = str(filepath.resolve())
                    print(f"[Status] PDF already exists on disk ({filepath.stat().st_size} bytes).")
                    return True
        except Exception:
            pass

    # 2. Layer 1: Direct Session PDF Fetch
    pdf_src = record.get("pdf_source", "")
    if pdf_src:
        try:
            clean_src = pdf_src.split("#")[0].strip()
            if clean_src:
                if not clean_src.startswith("http"):
                    full_url = f"{BASE_URL}/pdfsearch/{clean_src.lstrip('/')}"
                else:
                    full_url = clean_src

                print(f"[Layer 1 Direct Fetch] Attempting in-page fetch for {full_url}")
                body_bytes = fetch_pdf_via_page_evaluate(page, full_url)

                if not body_bytes:
                    print(f"[Layer 1 Context Fetch] GET {full_url}")
                    headers = {
                        "Referer": page.url if page else f"{BASE_URL}/pdfsearch/index.php",
                        "Accept": "application/pdf,application/octet-stream,*/*",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    }
                    response = context.request.get(full_url, headers=headers, timeout=15000)
                    content_type = response.headers.get("content-type", "")
                    raw = response.body() if response.ok else b""
                    if raw.startswith(b"%PDF"):
                        body_bytes = raw
                    print(f"[Layer 1 Response] Status: {response.status} | Content-Type: {content_type} | Size: {len(body_bytes if body_bytes else b'')} bytes")

                if body_bytes and body_bytes.startswith(b"%PDF-"):
                    with open(filepath, "wb") as file:
                        file.write(body_bytes)
                    record["pdf_url"] = full_url
                    record["pdf_path"] = str(filepath.resolve())
                    print(f"[SUCCESS] Direct PDF fetch succeeded! Saved to {filepath.resolve()}")
                    return True
                else:
                    print(f"[Layer 1 Note] Direct fetch did not return valid PDF response. Falling back to modal click...")
        except Exception as err:
            print(f"[Layer 1 Error] Direct fetch error: {err}")

    # 3. Layer 2: Modal Click & Session Recovery Fallback
    if pdf_captcha_visible(page) or captcha_is_visible(page):
        print("[Diagnostic] Session timeout / CAPTCHA modal visible before click. Recovering session...")
        if not handle_session_timeout_captcha(context, page):
            return False

    for attempt in range(1, MAX_PDF_RETRIES + 1):
        print(f"\n[Layer 2 Modal Click] Attempt {attempt}/{MAX_PDF_RETRIES}")

        try:
            success = capture_pdf_from_click(
                context,
                page,
                row,
                record,
                filepath
            )

            if success:
                close_pdf_modal(page)
                print(f"[SUCCESS] PDF captured via modal click!")
                return True

            if pdf_captcha_visible(page) or captcha_is_visible(page):
                print("[Diagnostic] Session timeout CAPTCHA detected during PDF capture attempt.")
                if handle_session_timeout_captcha(context, page):
                    print("[Session Restored] Retrying PDF capture for the SAME record...")
                    continue

        except Exception as error:
            print(f"[Layer 2 Exception] PDF capture error: {error}")
            if pdf_captcha_visible(page) or captcha_is_visible(page):
                if handle_session_timeout_captcha(context, page):
                    continue

        if attempt < MAX_PDF_RETRIES:
            time.sleep(PDF_WAIT)

    print(f"[FAILED] Could not download PDF for case: {case_number}")
    return False


# ============================================================
# NEXT BUTTON
# ============================================================

def get_next_button(page):

    next_button = page.locator(
        "#example_pdf_next"
    )

    if next_button.count() == 0:

        return None

    try:

        if not next_button.is_visible():

            return None

    except Exception:

        return None

    try:

        classes = (
            next_button
            .get_attribute(
                "class"
            )
            or
            ""
        ).lower()

        aria_disabled = (
            next_button
            .get_attribute(
                "aria-disabled"
            )
            or
            ""
        ).lower()

        disabled_attr = (
            next_button
            .get_attribute(
                "disabled"
            )
        )

        if (
            "disabled" in classes
            or
            aria_disabled == "true"
            or
            disabled_attr is not None
        ):

            return None

    except Exception:
        pass

    return next_button


# ============================================================
# RUN SCRAPER
# ============================================================

def run_scraper(from_date=None, to_date=None):
    global FROM_DATE, TO_DATE
    if from_date:
        FROM_DATE = from_date
    if to_date:
        TO_DATE = to_date

    start_date, end_date = validate_dates()

    if not start_date or not end_date:

        print("Exiting.")
        return

    print("=" * 80)
    print("KERALA HIGH COURT")
    print("eSCR JUDGMENT SCRAPER")
    print("KERALA + CUSTOM DATE + ALL PAGES + PDF")
    print("=" * 80)
    print(f"\nFrom Date: {FROM_DATE}")
    print(f"To Date:   {TO_DATE}")

    all_records = []

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
            ]
        )

        context = browser.new_context(
            no_viewport=True,
            accept_downloads=True
        )

        page = context.new_page()

        print(
            f"\nOpening search page:\n{SEARCH_URL}"
        )

        page.goto(
            SEARCH_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )

        page.wait_for_timeout(
            int(
                PAGE_LOAD_DELAY * 1000
            )
        )

        initial_search(page)
        select_kerala(page)
        set_decision_date(page)
        final_search(page)

        page_number = 1

        while True:

            print("\n" + "=" * 80)
            print(f"PAGE {page_number}")
            print("=" * 80)

            rows = get_result_rows(page)
            row_count = rows.count()

            if row_count == 0:

                print(
                    "No rows found on page."
                )

                break

            for row_index in range(row_count):

                print("\n" + "-" * 70)
                print(
                    f"RECORD [{row_index + 1}/"
                    f"{row_count}]"
                )

                current_rows = page.locator(
                    "#report_body tr"
                )

                if (
                    row_index
                    >=
                    current_rows.count()
                ):

                    print(
                        "Row disappeared."
                    )

                    continue

                row = current_rows.nth(
                    row_index
                )

                try:

                    record = (
                        parse_result_row(
                            row
                        )
                    )

                except Exception as error:

                    print(
                        f"Parse error: "
                        f"{error}"
                    )

                    continue

                if not record:

                    continue

                record[
                    "serial_no"
                ] = str(
                    len(all_records) + 1
                )

                record[
                    "source_page"
                ] = page.url

                print(
                    f"Case: "
                    f"{record['case_number']}"
                )

                print(
                    f"Party: "
                    f"{record['party_name']}"
                )

                print(
                    f"Judge: "
                    f"{record['judge']}"
                )

                print(
                    f"CNR: "
                    f"{record['cnr']}"
                )

                print(
                    f"Decision Date: "
                    f"{record['decision_date']}"
                )

                print(
                    f"PDF source: "
                    f"{record['pdf_source']}"
                )

                # =================================================
                # PDF MUST SUCCEED BEFORE MOVING TO NEXT RECORD
                # =================================================

                pdf_success = False
                retry_counter = 0

                while not pdf_success:

                    retry_counter += 1

                    print(
                        f"\nCurrent record PDF retry: "
                        f"{retry_counter}"
                    )

                    try:

                        pdf_success = (
                            download_pdf(
                                context,
                                page,
                                row,
                                record
                            )
                        )

                    except Exception as error:

                        print(
                            f"PDF error: "
                            f"{error}"
                        )

                        pdf_success = False

                    if pdf_success:
                        break

                    if pdf_captcha_visible(page) or captcha_is_visible(page):
                        print("[Record Loop] Session timeout / CAPTCHA present. Restoring session...")
                        handle_session_timeout_captcha(context, page)
                        continue

                    print(
                        f"\nPDF download retry for "
                        f"{record['case_number']}..."
                    )

                    close_pdf_modal(page)
                    time.sleep(PDF_WAIT)

                # =================================================
                # SAVE ONLY AFTER PDF SUCCESS
                # =================================================

                all_records.append(
                    record
                )

                save_json(
                    all_records
                )

                print(
                    "\nRecord + PDF saved successfully."
                )

                time.sleep(
                    RECORD_DELAY
                )

            next_button = get_next_button(
                page
            )

            if not next_button:

                print(
                    "\nNo more pages. Scraping finished."
                )

                break

            print("\nMoving to next page...")

            try:

                next_button.click()

                page.wait_for_timeout(
                    int(
                        PAGE_DELAY * 1000
                    )
                )

                try:

                    page.wait_for_function(
                        """
                        () => {

                            const body =
                                document.querySelector(
                                    "#report_body"
                                );

                            if (!body) {
                                return false;
                            }

                            return (
                                body.querySelectorAll(
                                    "tr"
                                ).length > 0
                            );
                        }
                        """,
                        timeout=30000
                    )

                except Exception:
                    pass

                page_number += 1

            except Exception as error:

                print(
                    f"\nCould not go to next page: {error}"
                )

                break

        print("\n" + "=" * 80)
        print("SCRAPING COMPLETED")
        print("=" * 80)

        print(
            f"\nTotal records saved: "
            f"{len(all_records)}"
        )

        print(
            f"\nJSON:\n"
            f"{JSON_FILE}"
        )

        print(
            f"\nPDF folder:\n"
            f"{PDF_DIR}"
        )

        print(
            "\nClosing browser..."
        )

        browser.close()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    run_scraper()