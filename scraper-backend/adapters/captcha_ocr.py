"""
Generic image-captcha solving, shared by every adapter that hits a
distorted-text/math-expression captcha (originally written for
sci.gov.in's judgment search widget, since reused by
adapters/high_courts/mp/'s ILRS discovery — nothing here is site-specific).

The captcha is either a short alphanumeric string or a simple math
expression ("4 + 3"). ddddocr is a general-purpose captcha OCR — not tuned
to any one site — so this module retries with a couple of image
preprocessing variants before giving up on one attempt.

Known open risk (spec §10): solve-rate here hasn't been measured at volume
for any site that uses it.
"""

import io
import re
from typing import Optional

from PIL import Image, ImageEnhance

_MATH_PATTERN = re.compile(r"(\d+)\s*([+\-*xX/])\s*(\d+)")

_ocr_engine = None


def _get_ocr_engine():
    global _ocr_engine
    if _ocr_engine is None:
        import ddddocr
        _ocr_engine = ddddocr.DdddOcr(show_ad=False)
    return _ocr_engine


def _evaluate_if_math_expression(text: str) -> Optional[str]:
    """Returns the evaluated result if `text` looks like 'N op N', else None."""
    match = _MATH_PATTERN.search(text)
    if not match:
        return None
    left, op, right = int(match.group(1)), match.group(2).lower(), int(match.group(3))
    if op == "+":
        return str(left + right)
    if op == "-":
        return str(left - right)
    if op in ("*", "x"):
        return str(left * right)
    if op == "/" and right != 0:
        return str(left // right)
    return None


def _clean_alphanumeric(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "", text or "").strip()


def solve_captcha_image(raw_png_bytes: bytes) -> Optional[str]:
    """
    Solves one captcha image, trying the raw bytes first and a
    contrast-enhanced 2x upscale second. Returns None if nothing plausible
    came out of either attempt — caller is responsible for retrying against
    a freshly refreshed captcha image.
    """
    ocr = _get_ocr_engine()

    for candidate_bytes in (raw_png_bytes, _enhance(raw_png_bytes)):
        if candidate_bytes is None:
            continue
        raw_result = ocr.classification(candidate_bytes)
        math_result = _evaluate_if_math_expression(raw_result)
        if math_result:
            return math_result
        cleaned = _clean_alphanumeric(raw_result)
        if cleaned:
            return cleaned

    return None


def _enhance(raw_png_bytes: bytes) -> Optional[bytes]:
    try:
        image = Image.open(io.BytesIO(raw_png_bytes))
        width, height = image.size
        upscaled = image.resize((width * 2, height * 2), Image.Resampling.BICUBIC)
        contrasted = ImageEnhance.Contrast(upscaled).enhance(2.0)
        buffer = io.BytesIO()
        contrasted.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        return None
