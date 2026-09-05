"""
Pipeline — one module per raw_ingestions status transition, each
independently runnable. Phase 1 built the shape with stand-ins; Phase 3
(spec §5) replaced the stand-ins with the real thing:

    pipeline/ocr.py              — DOWNLOADED -> OCR_DONE: PyMuPDF text layer,
                                    falling back to Tesseract for scanned PDFs (§5.1)
    pipeline/extraction.py       — OCR_DONE -> EXTRACTED: real structured LLM call (§5.2)
    pipeline/extraction_stub.py  — the Phase 1 stand-in, kept as a no-API-key dev fallback
    pipeline/promotion.py        — EXTRACTED -> PROMOTED: normalize into
                                    documents/cases/parties/document_coram/
                                    document_sections/citations
    pipeline/citator.py          — citation-finding pre-filter + reconciliation (§5.3)
"""
