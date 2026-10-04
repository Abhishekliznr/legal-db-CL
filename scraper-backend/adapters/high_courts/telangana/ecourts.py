from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urljoin

from playwright.sync_api import Page

from adapters.captcha_ocr import solve_captcha_image

logger = logging.getLogger("scraper_backend_v2.tshc_ecourts")

ECOURTS_MAIN_URL = "https://hcservices.ecourts.gov.in/hcservices/main.php"
BASE_URL = "https://hcservices.ecourts.gov.in/hcservices/"


def search_cnr_and_get_pdf_url(
    page: Page,
    cnr: str,
    max_attempts: int = 5,
) -> Tuple[Optional[str], Optional[dict]]:
    """
    Automates eCourts CNR search using Playwright.
    Returns (pdf_url, case_info_dict).
    """
    for attempt in range(1, max_attempts + 1):
        try:
            page.goto(ECOURTS_MAIN_URL, timeout=30000, wait_until="domcontentloaded")
            
            cnr_input = page.locator("#cino")
            cnr_input.wait_for(state="visible", timeout=10000)
            
            captcha_img = page.locator("#captcha_image")
            captcha_img.wait_for(state="visible", timeout=10000)
            
            img_bytes = captcha_img.screenshot()
            solved_captcha = solve_captcha_image(img_bytes)
            if not solved_captcha:
                logger.debug("eCourts captcha solve failed on attempt %d/%d, retrying...", attempt, max_attempts)
                time.sleep(1)
                continue

            cnr_input.fill(cnr)
            page.locator("#captcha").fill(solved_captcha)
            page.locator("#searchbtn").click()

            # Wait for either #caseHistoryDiv or error alert / message
            page.wait_for_timeout(3000)

            # Check if captcha was invalid
            err_alert = page.locator(".err_msg, #err_msg, .alert-danger")
            if err_alert.count() > 0:
                err_text = err_alert.first.inner_text().strip().lower()
                if "invalid captcha" in err_text or "captcha" in err_text:
                    logger.debug("eCourts invalid captcha response, retrying...")
                    continue

            # Look for history div or view links
            history_div = page.locator("#caseHistoryDiv")
            if history_div.count() > 0:
                inner_text = history_div.inner_text()
                if "invalid captcha" in inner_text.lower():
                    continue
                if "record not found" in inner_text.lower():
                    logger.info("eCourts: Record not found for CNR %s", cnr)
                    return None, {"error": "record not found"}

            # Search for "View" or display_pdf.php links
            links = page.locator("a[href*='display_pdf.php']").all()
            if not links:
                links = page.locator("a:has-text('View'), a:has-text('Order'), a:has-text('Judgment')").all()

            pdf_url = None
            if links:
                # The latest order / judgment is usually the last or first row. Let's find valid display_pdf link
                for link in reversed(links):
                    href = link.get_attribute("href")
                    if href and "display_pdf.php" in href:
                        pdf_url = urljoin(BASE_URL, href)
                        break
                if not pdf_url and links:
                    href = links[-1].get_attribute("href")
                    if href:
                        pdf_url = urljoin(BASE_URL, href)

            case_meta = {"cnr": cnr}
            return pdf_url, case_meta

        except Exception as e:
            logger.warning("eCourts search attempt %d/%d failed for CNR %s: %s", attempt, max_attempts, cnr, e)
            time.sleep(1)

    return None, None
