"""
DOWNLOADED -> OCR_DONE (spec §5.1).

Tries the PDF's text layer first (PyMuPDF — fast, free, works for the
majority of digitally-filed judgments). If that comes back empty or
near-empty (a scanned judgment — common for older High Court records),
falls back to real OCR: PyMuPDF rasterizes each page to an image, Tesseract
reads it. `ocr_engine` records which path actually produced the text, and
`ocr_confidence` carries Tesseract's page-averaged confidence when that path
ran (the text-layer path has no meaningful confidence score to report — it's
either there or it isn't).

Tesseract is an external system binary (`apt-get install tesseract-ocr` in
the Dockerfile, or `brew install tesseract` locally) — if it isn't
installed, this degrades to the Phase 1 behavior (empty text, OCR_DONE
anyway) rather than crashing the pipeline over a missing binary.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

import pymupdf as fitz

from db import scrape_jobs

logger = logging.getLogger("scraper_backend_v2.ocr")

# Below this character count, treat the text layer as "not really there" —
# a scanned page sometimes still yields a handful of stray characters from
# an embedded stamp or watermark, which isn't a usable text layer.
MIN_TEXT_LAYER_CHARS = 20

OCR_RENDER_DPI = 200


def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


def _ocr_with_tesseract(pdf_bytes: bytes) -> Tuple[Optional[str], Optional[float]]:
    """
    Renders each page to an image via PyMuPDF and runs Tesseract on it.
    Returns (text, avg_confidence), or (None, None) if Tesseract isn't
    available at all (missing binary) — a real per-page OCR failure still
    contributes empty text for that page rather than aborting the document.
    """
    try:
        import pytesseract
        from PIL import Image
        import io
    except ImportError:
        return None, None

    texts = []
    confidences = []

    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page in doc:
                pixmap = page.get_pixmap(dpi=OCR_RENDER_DPI)
                image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                try:
                    page_text = pytesseract.image_to_string(image)
                    texts.append(page_text)

                    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
                    page_confidences = [int(c) for c in data.get("conf", []) if str(c).lstrip("-").isdigit() and int(c) >= 0]
                    if page_confidences:
                        confidences.append(sum(page_confidences) / len(page_confidences))
                except pytesseract.TesseractNotFoundError:
                    return None, None
    except Exception:
        return None, None

    avg_confidence = round(sum(confidences) / len(confidences), 2) if confidences else None
    return "\n".join(texts), avg_confidence


def process_ingestion_ocr(ingestion_id: int, local_pdf_path: Path) -> None:
    """
    Extracts text from a still-local PDF and advances the raw_ingestions row
    to OCR_DONE. Called synchronously by the orchestrator right after
    download, while the temp file still exists (spec §4.4's batch flow).
    """
    logger.info("[OCR] ingestion_id=%s: starting (pdf=%s)", ingestion_id, local_pdf_path.name)
    try:
        pdf_bytes = local_pdf_path.read_bytes()
        text = extract_text_from_pdf_bytes(pdf_bytes)
        engine = "pymupdf"
        confidence = None

        if len(text.strip()) < MIN_TEXT_LAYER_CHARS:
            logger.info(
                "[OCR] ingestion_id=%s: text layer empty/near-empty (%d chars) — falling back to Tesseract",
                ingestion_id, len(text.strip()),
            )
            ocr_text, ocr_confidence = _ocr_with_tesseract(pdf_bytes)
            if ocr_text is not None:
                text, engine, confidence = ocr_text, "tesseract", ocr_confidence
            else:
                logger.warning(
                    "[OCR] ingestion_id=%s: Tesseract unavailable — keeping the empty text-layer result rather than failing the row",
                    ingestion_id,
                )

        page_count = _count_pages(pdf_bytes)
        scrape_jobs.update_status(
            ingestion_id,
            status="OCR_DONE",
            ocr_text=text,
            ocr_engine=engine,
            ocr_confidence=confidence,
            page_count=page_count,
            ocr_completed_at=datetime.now(timezone.utc),
        )
        logger.info(
            "[OCR] ingestion_id=%s: done — engine=%s pages=%s chars=%d%s",
            ingestion_id, engine, page_count, len(text),
            f" confidence={confidence}" if confidence is not None else "",
        )
    except Exception as e:
        logger.exception("[OCR] ingestion_id=%s: failed", ingestion_id)
        # error_message — renamed from extraction_error in the 2026-09-08
        # schema rewrite, which flattened raw_ingestions down to a single
        # error_message column shared by every failure status instead of a
        # name implying it was LLM-extraction-specific.
        scrape_jobs.update_status(ingestion_id, status="OCR_FAILED", error_message=str(e))


def _count_pages(pdf_bytes: bytes) -> Optional[int]:
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            return doc.page_count
    except Exception:
        return None
