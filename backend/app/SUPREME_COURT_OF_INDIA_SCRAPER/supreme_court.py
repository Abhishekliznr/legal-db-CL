import io
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
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
# UTF-8 OUTPUT FOR WINDOWS
# ============================================================

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ============================================================
# ENTERPRISE BACKEND & AZURE INTEGRATION
# ============================================================

_script_dir = Path(__file__).resolve().parent
_backend_dir = _script_dir.parent.parent
_project_root = _backend_dir.parent

for _p in [str(_project_root), str(_backend_dir)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from backend import db_manager, azure_blob
except ImportError:
    try:
        import db_manager
        import azure_blob
    except Exception as _e:
        print(f"⚠️ Warning importing backend/azure_blob: {_e}")
        db_manager = None
        azure_blob = None

# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = "https://www.sci.gov.in"
SEARCH_URL = f"{BASE_URL}/judgements-judgement-date/"

HEADLESS = True

PAGE_LOAD_DELAY = 3.0
SEARCH_DELAY = 4.0
PAGE_DELAY = 2.0
PDF_WAIT = 5.0
MAX_PDF_RETRIES = 3
MAX_CAPTCHA_RETRIES = 50


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

PDF_DIR = BASE_DIR / "pdf"
JSON_FILE = BASE_DIR / "supreme_court_judgments.json"
DEBUG_DIR = BASE_DIR / "debug"

PDF_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# STEALTH SCRIPT
# ============================================================

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
"""


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def safe_filename(text):
    if not text:
        return "unknown"
    text = str(text)
    text = re.sub(r'[<>:"/\\|?*]', "_", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip(". ")
    return text[:150]


def clean_text(text):
    if not text:
        return None
    text = re.sub(r"\s+", " ", str(text))
    return text.strip()


def save_json(records):
    data = {
        "Supreme Court of India": records
    }
    temp_file = JSON_FILE.with_suffix(".tmp")
    try:
        with open(temp_file, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4, ensure_ascii=False)
        temp_file.replace(JSON_FILE)
        print(f"\nJSON saved successfully: {JSON_FILE} ({len(records)} records)")
    except Exception as error:
        print(f"\nCould not save JSON: {error}")


def save_debug_page(page, filename):
    try:
        path = DEBUG_DIR / filename
        with open(path, "w", encoding="utf-8") as file:
            file.write(page.content())
        print(f"Debug HTML saved: {path}")
    except Exception as error:
        print(f"Could not save debug HTML: {error}")


# ============================================================
# CAPTCHA OCR ENGINE & MATH EVALUATOR
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


def parse_captcha_result(ocr_text):
    if not ocr_text:
        return None
    ocr_text = str(ocr_text).strip()
    
    # Check if math equation: e.g. "4 + 3", "4+3=", "12-5", "8x2", "7*2", "9-4"
    math_match = re.search(r'(\d+)\s*([\+\-\*xX\/])\s*(\d+)', ocr_text)
    if math_match:
        n1 = int(math_match.group(1))
        op = math_match.group(2).lower()
        n2 = int(math_match.group(3))
        if op == '+':
            val = n1 + n2
        elif op == '-':
            val = n1 - n2
        elif op in ['*', 'x']:
            val = n1 * n2
        elif op == '/':
            val = n1 // n2 if n2 != 0 else 0
        else:
            val = None
        if val is not None:
            print(f"[Captcha Solver] Evaluated math expression: {n1} {op} {n2} = {val}")
            return str(val)

    clean = re.sub(r'[^a-zA-Z0-9]', '', ocr_text).strip()
    return clean


def solve_captcha(page):
    try:
        captcha_img = page.locator("#siwp_captcha_image_0, .siwp_captcha_image")
        if captcha_img.count() == 0 or not captcha_img.first.is_visible():
            return None

        raw_bytes = captcha_img.first.screenshot()
        ocr = get_ocr_engine()
        if ocr is None:
            return None

        # Attempt 1: Raw image
        res1 = ocr.classification(raw_bytes)
        parsed1 = parse_captcha_result(res1)
        if parsed1:
            print(f"[Captcha Solver] Raw OCR -> '{parsed1}'")
            return parsed1

        # Attempt 2: Enhanced Contrast & Resize
        try:
            image = Image.open(io.BytesIO(raw_bytes))
            w, h = image.size
            img_enh = image.resize((w * 2, h * 2), Image.Resampling.BICUBIC)
            img_enh = ImageEnhance.Contrast(img_enh).enhance(2.0)
            buf = io.BytesIO()
            img_enh.save(buf, format="PNG")
            res2 = ocr.classification(buf.getvalue())
            parsed2 = parse_captcha_result(res2)
            if parsed2:
                print(f"[Captcha Solver] Enhanced OCR -> '{parsed2}'")
                return parsed2
        except Exception:
            pass

        return None
    except Exception as error:
        print(f"Captcha Solver note: {error}")
        return None


# ============================================================
# PDF DOWNLOADER
# ============================================================

def download_pdf(pdf_url, filename):
    filepath = PDF_DIR / filename
    if filepath.exists() and filepath.stat().st_size > 1000:
        return str(filepath)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/pdf,application/octet-stream,*/*",
        "Referer": "https://www.sci.gov.in/",
    }

    for attempt in range(1, MAX_PDF_RETRIES + 1):
        try:
            res = requests.get(pdf_url, headers=headers, timeout=30, verify=False)
            if res.status_code == 200 and (res.content.startswith(b"%PDF") or len(res.content) > 500):
                with open(filepath, "wb") as f:
                    f.write(res.content)
                print(f"  -> Downloaded PDF: {filepath.name} ({len(res.content)} bytes)")
                return str(filepath)
            else:
                print(f"  -> Attempt {attempt}: HTTP {res.status_code}")
        except Exception as e:
            print(f"  -> Attempt {attempt} failed to download PDF: {e}")
        time.sleep(1.5)

    return None


# ============================================================
# PARSE TABLE ROWS (WITH ENTERPRISE DEDUPLICATION)
# ============================================================

def parse_results_table(page, records_list, seen_keys, upload_azure: bool = False, stream_cloud: bool = False):
    """
    Parses the results table from sci.gov.in.
    Performs fast O(1) deduplication check against PostgreSQL & local cache.
    If already scraped, skips PDF download entirely.
    """
    rows = page.locator(".distTableContent table tbody tr")
    total_rows = rows.count()
    if total_rows == 0:
        print("No result rows found in table.")
        return

    headers = [clean_text(h.inner_text()) for h in page.locator(".distTableContent table thead th").all()]
    print(f"Parsing {total_rows} row(s) from results table...")

    skipped_count = 0
    new_count = 0

    for i in range(total_rows):
        row = rows.nth(i)
        cells = row.locator("td")
        if cells.count() == 0:
            continue

        data_map = {}
        for col_idx, cell in enumerate(cells.all()):
            header_name = headers[col_idx] if col_idx < len(headers) else f"col_{col_idx}"
            data_map[header_name] = clean_text(cell.inner_text())

        # Extract links (PDFs)
        pdf_urls = []
        for a_tag in row.locator("a").all():
            href = a_tag.get_attribute("href")
            if href:
                pdf_urls.append(urljoin(BASE_URL, href))

        diary_number = data_map.get("Diary Number")
        case_number = data_map.get("Case Number")
        decision_date = data_map.get("Order / Judgment By Date") or data_map.get("Judgment Date") or data_map.get("Date")
        neutral_citation = data_map.get("Neutral Citation")
        serial_no = data_map.get("Serial Number", str(len(records_list) + 1))
        primary_pdf_url = pdf_urls[0] if pdf_urls else None

        # Build unique deduplication identifiers
        keys_to_check = []
        if diary_number and decision_date:
            keys_to_check.append(f"{diary_number}_{decision_date}")
        if diary_number and case_number:
            keys_to_check.append(f"{diary_number}_{case_number}")
        if diary_number:
            keys_to_check.append(diary_number)
        if neutral_citation:
            keys_to_check.append(neutral_citation)
        if primary_pdf_url:
            keys_to_check.append(primary_pdf_url)

        # FAST DEDUPLICATION CHECK: Skip if already exists in PostgreSQL or session cache
        if any(k in seen_keys for k in keys_to_check):
            print(f"⏩ [SKIP DEDUPLICATED] Case already exists in database (Diary: {diary_number} | Case: {case_number}). Skipping PDF download.")
            skipped_count += 1
            continue

        pet_resp = data_map.get("Petitioner / Respondent", "")
        pet_resp_parts = [p.strip() for p in re.split(r'\bVS\b|\bvs\b', pet_resp) if p.strip()]
        petitioner = pet_resp_parts[0] if len(pet_resp_parts) > 0 else pet_resp
        respondent = pet_resp_parts[1] if len(pet_resp_parts) > 1 else ""

        advocate = data_map.get("Petitioner/Respondent Advocate", "")
        advocate = clean_text(re.sub(r'__+', '', advocate))

        bench = clean_text(data_map.get("Bench", ""))
        judgment_by = clean_text(data_map.get("Judgment By", ""))

        # Download PDF if available
        pdf_path = None
        azure_pdf_url = None
        if primary_pdf_url:
            pdf_name = safe_filename(f"{diary_number}_{case_number}_{decision_date}") + ".pdf"
            pdf_path = download_pdf(primary_pdf_url, pdf_name)

            # Direct Azure Streaming Option: Upload instantly to Azure and delete local copy
            if upload_azure and azure_blob and pdf_path:
                try:
                    azure_pdf_url = azure_blob.upload_pdf_to_blob(
                        pdf_path,
                        court_code="SCIN",
                        diary_number=diary_number,
                        case_number=case_number,
                        judgment_date=decision_date,
                        delete_local_after=stream_cloud
                    )
                except Exception as e:
                    print(f"⚠️ Error streaming PDF to Azure: {e}")

        record = {
            "serial_no": serial_no,
            "diary_number": diary_number,
            "case_number": case_number,
            "party_name": clean_text(pet_resp.replace("\n", " ")),
            "petitioner": petitioner,
            "respondent": respondent,
            "advocate": advocate,
            "bench": bench,
            "judge": judgment_by,
            "court": "Supreme Court of India",
            "decision_date": decision_date,
            "neutral_citation": neutral_citation,
            "pdf_url": azure_pdf_url or primary_pdf_url,
            "pdf_path": None if (stream_cloud and azure_pdf_url) else pdf_path,
            "source_page": SEARCH_URL,
        }

        records_list.append(record)
        for k in keys_to_check:
            seen_keys.add(k)
        new_count += 1
        # Save JSON incrementally after every scraped record
        save_json(records_list)

    if skipped_count > 0:
        print(f"✅ Batch Summary: Skipped {skipped_count} duplicate records. Ingested {new_count} new unique records. Total: {len(records_list)}")
    else:
        print(f"✅ Batch Summary: Ingested {new_count} new unique records. Total: {len(records_list)}")


# ============================================================
# SEARCH SINGLE DATE BATCH (MAX 30 DAYS)
# ============================================================

def search_date_batch(page, batch_from_str, batch_to_str, records_list, seen_keys, upload_azure: bool = False, stream_cloud: bool = False):
    print("\n" + "=" * 80)
    print(f"SEARCHING BATCH: {batch_from_str} TO {batch_to_str}")
    print("=" * 80)

    page.goto(SEARCH_URL, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2000)

    from_input = page.locator("#from_date")
    to_input = page.locator("#to_date")
    captcha_input = page.locator("#siwp_captcha_value_0, input[name='siwp_captcha_value']")
    submit_btn = page.locator("input[type='submit'][name='submit']")

    if from_input.count() == 0 or to_input.count() == 0:
        raise RuntimeError("Date input fields not found on page.")

    # Fill dates
    from_input.click()
    from_input.fill(batch_from_str)
    to_input.click()
    to_input.fill(batch_to_str)

    print(f"Dates populated: {batch_from_str} -> {batch_to_str}")

    # Solve captcha loop
    solved = False
    for attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
        print(f"\n[Captcha Attempt {attempt}/{MAX_CAPTCHA_RETRIES}] Solving CAPTCHA...")
        captcha_val = solve_captcha(page)

        if captcha_val and len(captcha_val) >= 1:
            print(f"[Captcha Attempt {attempt}] Filling solved code: '{captcha_val}'")
            captcha_input.fill(captcha_val)
            submit_btn.click()

            page.wait_for_timeout(4000)

            # Check if results appear or error popup
            results = page.locator(".resultsHolder, #cnrResults, .distTableContent table")
            if results.count() > 0 and page.locator(".distTableContent table tbody tr").count() > 0:
                print("\nSUCCESS: Results table loaded!")
                solved = True
                break

            # Check if empty results message
            no_results = page.locator(":has-text('No Records Found'), :has-text('No Data Available')")
            if no_results.count() > 0 and no_results.first.is_visible():
                print("Search returned 'No Records Found' for this date batch.")
                return

        # Refresh captcha
        refresh_btn = page.locator(".captcha-refresh-btn, a[title='Refresh Image']")
        if refresh_btn.count() > 0:
            try:
                refresh_btn.first.click()
                page.wait_for_timeout(1500)
            except Exception:
                pass

    if not solved:
        print(f"Warning: Could not solve CAPTCHA for batch {batch_from_str} to {batch_to_str}")
        save_debug_page(page, f"failed_captcha_{batch_from_str}.html")
        return

    # Parse first page
    parse_results_table(page, records_list, seen_keys, upload_azure=upload_azure, stream_cloud=stream_cloud)

    # Handle pagination
    page_num = 1
    while True:
        next_btn = page.locator("#paginationHtml a:has-text('Next'), #paginationHtml a.next, .pagination a:has-text('>')")
        if next_btn.count() == 0 or not next_btn.first.is_visible():
            break

        print(f"\nNavigating to Page {page_num + 1}...")
        try:
            next_btn.first.click()
            page.wait_for_timeout(3000)
            page_num += 1
            parse_results_table(page, records_list, seen_keys, upload_azure=upload_azure, stream_cloud=stream_cloud)
        except Exception as err:
            print(f"Pagination completed or failed: {err}")
            break


# ============================================================
# MAIN SCRAPER ENTRYPOINT
# ============================================================

def run_scraper(from_date, to_date, upload_azure: bool = False, stream_cloud: bool = False, headless: bool = HEADLESS):
    print("=" * 80)
    print("SUPREME COURT OF INDIA JUDGMENT SCRAPER (ENTERPRISE DEDUPLICATION)")
    print("=" * 80)
    print(f"Requested Date Range : {from_date} to {to_date}")
    print(f"Azure Cloud Upload   : {upload_azure}")
    print(f"Stream-Cloud (0-Disk): {stream_cloud}")
    print("=" * 80)

    # Standardize input date formats YYYY-MM-DD or DD-MM-YYYY
    try:
        if "-" in from_date and len(from_date.split("-")[0]) == 4:
            dt_start = datetime.strptime(from_date, "%Y-%m-%d")
        else:
            dt_start = datetime.strptime(from_date, "%d-%m-%Y")

        if "-" in to_date and len(to_date.split("-")[0]) == 4:
            dt_end = datetime.strptime(to_date, "%Y-%m-%d")
        else:
            dt_end = datetime.strptime(to_date, "%d-%m-%Y")
    except ValueError as e:
        print(f"Error parsing dates ({from_date}, {to_date}): {e}")
        return

    if dt_start > dt_end:
        print("ERROR: from_date cannot be after to_date.")
        return

    # 1. Load existing keys from PostgreSQL for O(1) duplicate skipping
    seen_keys = set()
    job_id = None
    if db_manager:
        db_keys = db_manager.get_existing_cases_keys("SUPREME_COURT_OF_INDIA")
        seen_keys.update(db_keys)
        print(f"🛡️ Loaded {len(db_keys)} existing case keys from PostgreSQL for instant duplicate skipping!")
        job_id = db_manager.log_scraper_job_start("SUPREME_COURT_OF_INDIA", from_date, to_date)

    # 2. Load existing JSON records if present
    records_list = []
    if JSON_FILE.exists():
        try:
            with open(JSON_FILE, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
                raw_list = existing_data.get("Supreme Court of India", [])
                
                deduped = []
                for r in raw_list:
                    d_no = str(r.get("diary_number", "")).strip()
                    c_no = str(r.get("case_number", "")).strip()
                    d_date = str(r.get("decision_date", "")).strip()
                    pdf_u = str(r.get("pdf_url", "")).strip()

                    keys = []
                    if d_no and d_date:
                        keys.append(f"{d_no}_{d_date}")
                    if d_no and c_no:
                        keys.append(f"{d_no}_{c_no}")
                    if d_no:
                        keys.append(d_no)
                    if pdf_u:
                        keys.append(pdf_u)

                    if not any(k in seen_keys for k in keys):
                        deduped.append(r)
                        for k in keys:
                            seen_keys.add(k)

                records_list = deduped
            print(f"Loaded and verified {len(records_list)} existing local records from {JSON_FILE.name}")
        except Exception as err:
            print(f"Warning reading existing JSON: {err}")
            records_list = []

    initial_count = len(records_list)

    # Split date range into <= 30 day batches as required by sci.gov.in
    batches = []
    curr_start = dt_start
    while curr_start <= dt_end:
        curr_end = min(curr_start + timedelta(days=29), dt_end)
        batches.append((curr_start.strftime("%d-%m-%Y"), curr_end.strftime("%d-%m-%Y")))
        curr_start = curr_end + timedelta(days=1)

    print(f"Split date range into {len(batches)} batch(es) of <= 30 days:")
    for b_from, b_to in batches:
        print(f" - {b_from} to {b_to}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--window-size=1920,1080",
            ]
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Upgrade-Insecure-Requests": "1",
            }
        )
        context.add_init_script(STEALTH_JS)
        page = context.new_page()

        for b_from, b_to in batches:
            try:
                search_date_batch(page, b_from, b_to, records_list, seen_keys, upload_azure=upload_azure, stream_cloud=stream_cloud)
            except Exception as batch_error:
                print(f"Error processing batch {b_from} - {b_to}: {batch_error}")
                save_debug_page(page, f"batch_error_{b_from}.html")

        browser.close()

    new_scraped = len(records_list) - initial_count

    # Upload Bronze JSON to Azure Blob Storage if requested
    if upload_azure and azure_blob and JSON_FILE.exists():
        year_str = str(dt_start.year)
        azure_blob.upload_json_to_blob(
            JSON_FILE,
            court_code="SCIN",
            layer="Bronze",
            filename=f"supreme_court_judgments_{year_str}.json"
        )

    # Log Job Finish in PostgreSQL
    if db_manager and job_id:
        db_manager.log_scraper_job_finish(
            job_id,
            status="COMPLETED",
            total_found=len(seen_keys),
            new_scraped=new_scraped,
            skipped=len(seen_keys) - new_scraped
        )

    print("\n" + "=" * 80)
    print(f"SCRAPING FINISHED: Collected {new_scraped} new records. Total: {len(records_list)} judgment records.")
    print("=" * 80)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Supreme Court of India Judgment Scraper with Enterprise Deduplication")
    parser.add_argument("--from", dest="from_date", default="2025-01-01", help="From date (YYYY-MM-DD or DD-MM-YYYY)")
    parser.add_argument("--to", dest="to_date", default="2025-01-05", help="To date (YYYY-MM-DD or DD-MM-YYYY)")
    parser.add_argument("--upload-azure", action="store_true", help="Upload new PDFs and JSON to Azure Blob Storage")
    parser.add_argument("--stream-cloud", action="store_true", help="Delete local PDFs after uploading to Azure (Zero Disk Usage)")
    parser.add_argument("--headful", action="store_true", help="Run browser in visible mode")

    args = parser.parse_args()
    run_scraper(
        from_date=args.from_date,
        to_date=args.to_date,
        upload_azure=args.upload_azure,
        stream_cloud=args.stream_cloud,
        headless=not args.headful
    )
