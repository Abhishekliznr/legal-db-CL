"""
Azure Blob Storage — PDFs only.

Spec §7: the old service's bronze/silver JSON-in-blob-with-merge-logic is
gone. Postgres (`raw_ingestions`) is now the single source of truth for raw
OCR text and raw AI extraction output; blob storage exists solely to hold
the original PDF bytes so they don't live on local disk indefinitely.
"""

import logging
import os
from pathlib import Path
from typing import Optional, Union

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("scraper_backend_v2.storage.azure_blob")

_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
_PDF_CONTAINER = os.environ.get("AZURE_BLOB_CONTAINER_PDFS", "liznr-legal-dev-judgment-pdfs").strip()


def is_configured() -> bool:
    return bool(_CONNECTION_STRING)


def _get_client():
    if not is_configured():
        return None
    from azure.storage.blob import BlobServiceClient
    return BlobServiceClient.from_connection_string(_CONNECTION_STRING)


def upload_pdf(local_path: Union[str, Path], blob_name: str) -> Optional[str]:
    """
    Uploads a PDF to the configured container under `blob_name` (caller
    decides the naming scheme — e.g. "{court_code}/{checksum}.pdf"). Returns
    the blob URL, or None if Azure isn't configured or the upload failed.

    Every None-returning path below logs *why* — this function used to
    swallow every failure silently (bad connection string, wrong container
    name, network error, auth failure, or `azure-storage-blob` not even
    being installed all looked identical: a quiet None, with raw_ingestions.
    blob_path ending up NULL and no way to tell which of those five things
    actually happened). Matches the same self-diagnosing-on-failure pattern
    already applied to pipeline/extraction.py's Groq call and
    pipeline/promotion.py's date parsing after two rounds of exactly this
    kind of silent failure being hard to debug live.
    """
    if not is_configured():
        return None  # not an error — Azure simply isn't configured, expected in local dev

    try:
        client = _get_client()
    except ImportError:
        logger.error(
            "AZURE_STORAGE_CONNECTION_STRING is set but the 'azure-storage-blob' package "
            "isn't installed — run `pip install -r requirements.txt`. Falling back to "
            "pdf_url = source_url for this record."
        )
        return None
    except Exception as e:
        logger.error("Failed to create Azure BlobServiceClient (check AZURE_STORAGE_CONNECTION_STRING is well-formed): %s", e)
        return None

    path = Path(local_path)
    if not path.exists():
        logger.error("Local PDF path does not exist, cannot upload to blob: %s", path)
        return None

    try:
        from azure.storage.blob import ContentSettings
        blob_client = client.get_blob_client(container=_PDF_CONTAINER, blob=blob_name)
        with open(path, "rb") as f:
            blob_client.upload_blob(
                f, overwrite=True, content_settings=ContentSettings(content_type="application/pdf")
            )
        return blob_client.url
    except Exception as e:
        logger.error("Azure blob upload failed for container=%r blob=%r: %s", _PDF_CONTAINER, blob_name, e)
        return None
