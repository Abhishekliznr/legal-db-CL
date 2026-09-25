"""
PDF Router: Mints a short-lived, read-only URL for a case's judgment PDF out
of the private Azure Blob container it lives in (see storage/azure_blob.py's
module docstring).

- GET /api/cases/{case_id}/pdf-url : {"url": "https://...&sig=..."}, valid for
                                      storage/azure_blob.py's `_SAS_TTL_SECONDS`.

`blob_pdf_url` (search_router.py's raw `BLOB_BASE_URL/BLOB_CONTAINER/blob_pdf_id`
URL) is a bare Azure Blob Storage URL against a PRIVATE container — no SAS
token, no auth — so it 403s for literally everyone, in-app or not. This
endpoint exists so the frontend's judgment viewer never points a browser at
that URL directly: it instead calls its own same-origin, session-checked
Next.js route, which calls THIS endpoint (case-research's `caseResearchApi`
already carries the app's own session token, same as every other
api-backend call) to get a real, working — but short-lived and scoped to
exactly this one blob — URL, then fetches THAT directly (browser <-> Azure,
bypassing this service for the actual PDF bytes) and turns the response
into an in-memory `blob:` object URL for the viewer. `blob_pdf_id` itself is
looked up server-side from `case_id` here, not taken from the caller, so
this can't be used to mint a URL for an arbitrary blob path.
"""

import logging
from typing import Optional

import psycopg2
from fastapi import APIRouter, HTTPException

from db.connection import get_pooled_connection
from routers.search_router import _to_valid_int
from storage import azure_blob

logger = logging.getLogger("api_backend_v2.pdf")

router = APIRouter(tags=["Case PDF"])


def _get_blob_pdf_id(case_id: str) -> Optional[str]:
    valid_int_id = _to_valid_int(case_id)
    with get_pooled_connection() as conn:
        with conn.cursor() as cur:
            id_clause = "case_id = %s OR " if valid_int_id is not None else ""
            id_params = [valid_int_id] if valid_int_id is not None else []
            cur.execute(f"""
                SELECT blob_pdf_id FROM cr_cases
                WHERE {id_clause}case_number = %s OR liznr_id = %s;
            """, (*id_params, case_id, case_id))
            row = cur.fetchone()
            return row[0] if row else None


@router.get("/api/cases/{case_id:path}/pdf-url")
def get_case_pdf_url(case_id: str):
    try:
        blob_pdf_id = _get_blob_pdf_id(case_id)
    except psycopg2.OperationalError:
        logger.exception("Database connection failed while resolving PDF for case %s", case_id)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again shortly.")

    if not blob_pdf_id:
        raise HTTPException(status_code=404, detail="No PDF on file for this case.")

    url = azure_blob.generate_pdf_read_url(blob_pdf_id)
    if url is None:
        raise HTTPException(status_code=404, detail="PDF could not be retrieved.")

    return {"url": url}
