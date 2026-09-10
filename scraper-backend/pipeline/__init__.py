"""
Pipeline — one module per raw_ingestions status transition, each
independently runnable.

    pipeline/ocr.py              — DOWNLOADED -> OCR_DONE: PyMuPDF text layer,
                                    falling back to Tesseract for scanned PDFs (§5.1)
    pipeline/regex_extraction.py — LLM-free extraction (case number/parties/
                                    dates/coram/provisions) straight from
                                    ocr_text; replaced the old Phase 3
                                    LLM-extraction stage 2026-09-08 (there is
                                    no separate OCR_DONE -> EXTRACTED status
                                    transition anymore)
    pipeline/promotion.py        — OCR_DONE -> PROMOTED: reads regex_extraction's
                                    output + RawJudgmentRecord and normalizes
                                    into documents/cases/parties/document_coram/
                                    document_sections
    pipeline/llm_enrichment.py   — runs after promotion, on an already-promoted
                                    case: case_note/industries/conclusion/
                                    provisions + a disposition fallback. Uses
                                    Azure OpenAI (config in pipeline/azure_openai.py).
                                    Failures here never un-promote a case — see
                                    its own docstring.
    pipeline/citator.py          — citation-finding pre-filter; not currently
                                    wired into any stage, kept standalone for
                                    when citation tracking comes back (see its
                                    own docstring)
"""
