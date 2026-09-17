"""
Pipeline — stages shared across every court, independently runnable. Each
court's own field extraction and cr_cases promotion lives instead in that
court's own adapter package (e.g. adapters/supreme_court/extraction.py +
promotion.py) — see adapters/__init__.py.

    pipeline/ocr.py            — DOWNLOADED -> OCR_DONE: PyMuPDF text layer,
                                  falling back to Tesseract for scanned PDFs (§5.1)
    pipeline/llm_enrichment.py — runs after promotion, on an already-promoted
                                  case: case_note/industries/conclusion/
                                  provisions + a disposition fallback. Uses
                                  Azure OpenAI (config in pipeline/azure_openai.py).
                                  Court-agnostic — takes a provision-paragraph
                                  block as a parameter rather than extracting
                                  one itself (orchestrator/batch_runner.py
                                  supplies it from that court's own extraction
                                  module). Failures here never un-promote a
                                  case — see its own docstring.
    pipeline/citator.py        — citation-finding pre-filter; not currently
                                  wired into any stage, kept standalone for
                                  when citation tracking comes back (see its
                                  own docstring)
    pipeline/azure_openai.py   — Azure OpenAI config/auth helpers shared by
                                  any pipeline stage that calls it
"""
