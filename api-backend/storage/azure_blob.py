"""
Azure Blob Storage — read side.

scraper-backend/storage/azure_blob.py writes the PDFs into a private
container (AZURE_STORAGE_CONNECTION_STRING/AZURE_BLOB_CONTAINER_PDFS — same
two env vars, same names, on purpose, since it's the same storage account).
This is the read-side counterpart: routers/pdf_router.py uses
`generate_pdf_read_url` to hand the frontend a short-lived, read-only SAS
URL for a case's judgment PDF instead of a bare
`https://<account>.blob.core.windows.net/...` URL (which 403s on a private
container regardless of who's asking) and instead of streaming the PDF's
bytes through this service itself (wasteful — the browser can pull straight
from Azure once it has a token scoped to exactly that one blob, for exactly
`_SAS_TTL_SECONDS`).
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("api_backend_v2.storage.azure_blob")

_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
_PDF_CONTAINER = os.environ.get("AZURE_BLOB_CONTAINER_PDFS", "liznr-legal-dev-judgment-pdfs").strip()

# Long enough for the browser's own fetch (triggered right after this URL is minted) to
# complete even on a slow connection; short enough that the URL is useless to anyone who
# didn't get it from that same request a moment ago.
_SAS_TTL_SECONDS = 60


def is_configured() -> bool:
    return bool(_CONNECTION_STRING)


def _get_client():
    if not is_configured():
        return None
    from azure.storage.blob import BlobServiceClient
    return BlobServiceClient.from_connection_string(_CONNECTION_STRING)


def generate_pdf_read_url(blob_name: str) -> Optional[str]:
    """Mints a read-only SAS URL for `blob_name` (cr_cases.blob_pdf_id, e.g.
    "SCIN/<checksum>.pdf") in the private PDF container, valid for `_SAS_TTL_SECONDS`.
    Returns None (logging why) if Azure isn't configured, the account key can't be read off
    the connection string (a non-key credential, e.g. a SAS-only or AAD connection string,
    can't mint further SAS tokens), or blob storage otherwise refuses -- callers turn that
    into a 404/503, not a 500."""
    if not is_configured():
        logger.error("AZURE_STORAGE_CONNECTION_STRING is not set — cannot generate a read URL for blob %r", blob_name)
        return None

    try:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas
        client = _get_client()
    except ImportError:
        logger.error("'azure-storage-blob' package isn't installed — run `pip install -r requirements.txt`.")
        return None
    except Exception as e:
        logger.error("Failed to create Azure BlobServiceClient (check AZURE_STORAGE_CONNECTION_STRING is well-formed): %s", e)
        return None

    account_key = getattr(client.credential, "account_key", None)
    if not account_key:
        # e.g. AZURE_STORAGE_CONNECTION_STRING built from a SAS token or AAD credential
        # instead of an account key -- neither can mint further SAS tokens.
        logger.error("Azure credential has no account_key -- can't mint a SAS URL (need an account-key connection string)")
        return None

    try:
        sas_token = generate_blob_sas(
            account_name=client.account_name,
            container_name=_PDF_CONTAINER,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(seconds=_SAS_TTL_SECONDS),
        )
        return f"{client.get_blob_client(container=_PDF_CONTAINER, blob=blob_name).url}?{sas_token}"
    except Exception as e:
        logger.error("Azure SAS generation failed for container=%r blob=%r: %s", _PDF_CONTAINER, blob_name, e)
        return None
