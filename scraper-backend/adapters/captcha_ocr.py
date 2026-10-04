"""
Generic image-captcha solving, shared by every adapter that hits a
distorted-text/math-expression captcha.

Handles:
- Automatic background inversion (for dark-background captchas like TSHC).
- Alphanumeric and basic math evaluations.
- 2x upscale with contrast enhancement for distorted characters.
"""

import io
import re
from typing import Optional

from PIL import Image, ImageEnhance, ImageOps

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


def _get_image_variants(raw_png_bytes: bytes) -> list[bytes]:
    """
    Generates preprocessing variants:
    1. Inverted if dark background (light text on dark background -> dark text on light).
    2. Upscaled 2x + contrast enhanced.
    3. Original bytes as fallback.
    """
    variants = []
    try:
        image = Image.open(io.BytesIO(raw_png_bytes)).convert("RGB")

        # Check average pixel brightness
        grayscale = image.convert("L")
        pixels = list(grayscale.getdata())
        avg_brightness = sum(pixels) / len(pixels)

        # Invert if the image is predominantly dark (avg brightness < 128)
        if avg_brightness < 128:
            processed_img = ImageOps.invert(image)
        else:
            processed_img = image

        # Variant A: Inverted/Normalized
        buf_norm = io.BytesIO()
        processed_img.save(buf_norm, format="PNG")
        variants.append(buf_norm.getvalue())

        # Variant B: 2x Upscale + Contrast Boost
        w, h = processed_img.size
        upscaled = processed_img.resize((w * 2, h * 2), Image.Resampling.BICUBIC)
        contrasted = ImageEnhance.Contrast(upscaled).enhance(1.8)
        buf_contrasted = io.BytesIO()
        contrasted.save(buf_contrasted, format="PNG")
        variants.append(buf_contrasted.getvalue())

    except Exception:
        pass

    # Always include original raw bytes as last resort
    variants.append(raw_png_bytes)
    return variants


def solve_captcha_image(raw_png_bytes: bytes) -> Optional[str]:
    """
    Solves CAPTCHA by testing preprocessed image variants against OCR.
    """
    ocr = _get_ocr_engine()

    for candidate_bytes in _get_image_variants(raw_png_bytes):
        if not candidate_bytes:
            continue
        try:
            raw_result = ocr.classification(candidate_bytes)
        except Exception:
            continue

        math_result = _evaluate_if_math_expression(raw_result)
        if math_result:
            return math_result

        cleaned = _clean_alphanumeric(raw_result)
        if cleaned:
            return cleaned

    return None