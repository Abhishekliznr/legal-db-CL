"""
Captcha solving for the eCourts judgments portal (judgments.ecourts.gov.in).

Separate from adapters/supreme_court/captcha.py deliberately — eCourts'
captcha widget is a different image (typically 5-6 alphanumeric characters,
no math expressions) with its own preprocessing needs, even though both
modules share the same ddddocr-based approach. Duplicating this small amount
of logic per adapter keeps each one self-contained and independently
tunable per site, which matters here since 25 High Courts all go through
this one captcha widget and any fix belongs in exactly one place.
"""

import io
import re
from typing import List, Optional

from PIL import Image, ImageEnhance, ImageOps

_ocr_engine = None


def _get_ocr_engine():
    global _ocr_engine
    if _ocr_engine is None:
        import ddddocr
        _ocr_engine = ddddocr.DdddOcr(show_ad=False)
    return _ocr_engine


def _preprocess_variants(raw_png_bytes: bytes) -> List[bytes]:
    variants = [raw_png_bytes]
    try:
        image = Image.open(io.BytesIO(raw_png_bytes))
        width, height = image.size

        upscaled = image.resize((width * 2, height * 2), Image.Resampling.BICUBIC)
        contrasted = ImageEnhance.Contrast(upscaled).enhance(1.5)
        buffer1 = io.BytesIO()
        contrasted.save(buffer1, format="PNG")
        variants.append(buffer1.getvalue())

        grayscale = ImageOps.autocontrast(ImageOps.grayscale(image))
        buffer2 = io.BytesIO()
        grayscale.save(buffer2, format="PNG")
        variants.append(buffer2.getvalue())
    except Exception:
        pass
    return variants


def solve_captcha_image(raw_png_bytes: bytes) -> Optional[str]:
    """
    eCourts captchas are typically 5-6 alphanumeric characters. Tries the
    raw image plus two preprocessing variants, preferring a result in that
    length range; falls back to the closest candidate if nothing matches
    exactly. Returns None if every variant produced nothing plausible.
    """
    ocr = _get_ocr_engine()
    candidates: List[str] = []

    for variant_bytes in _preprocess_variants(raw_png_bytes):
        try:
            raw_result = ocr.classification(variant_bytes)
        except Exception:
            continue
        cleaned = re.sub(r"[^a-zA-Z0-9]", "", raw_result or "").strip()
        if 5 <= len(cleaned) <= 6:
            return cleaned
        if 4 <= len(cleaned) <= 6:
            candidates.append(cleaned)

    if candidates:
        candidates.sort(key=lambda c: (abs(len(c) - 6), -len(c)))
        return candidates[0]

    return None
