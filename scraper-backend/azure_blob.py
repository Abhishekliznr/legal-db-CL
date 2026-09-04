"""
Azure Blob Storage Integration Module
-------------------------------------
Handles uploading and managing physical judgment PDFs and raw scraper JSON
archives directly to Microsoft Azure Blob Storage (Liznr Case Research).

Containers:
- PDF Container : liznr-legal-dev-judgment-pdfs  (Default)
- RAW Container : liznr-legal-dev-raw-judgments   (Default)
"""

import os
import json
from pathlib import Path
from typing import Optional, Dict, Any, Union
from dotenv import load_dotenv

# Load root .env file
root_env = Path(__file__).resolve().parent.parent / ".env"
if root_env.exists():
    load_dotenv(dotenv_path=root_env)
else:
    load_dotenv()

# Azure Storage credentials from environment
ACCOUNT_NAME = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME", "").strip()
ACCOUNT_KEY = os.environ.get("AZURE_STORAGE_ACCOUNT_KEY", "").strip()
CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()

PDF_CONTAINER = os.environ.get("AZURE_BLOB_CONTAINER_PDFS", "liznr-legal-dev-judgment-pdfs").strip()
RAW_CONTAINER = os.environ.get("AZURE_BLOB_CONTAINER_RAW", "liznr-legal-dev-raw-judgments").strip()


def is_azure_blob_configured() -> bool:
    """Returns True if Azure Blob Storage credentials are provided."""
    return bool(CONNECTION_STRING or (ACCOUNT_NAME and ACCOUNT_KEY))


def get_blob_service_client():
    """Initializes and returns the Azure BlobServiceClient."""
    if not is_azure_blob_configured():
        return None

    try:
        from azure.storage.blob import BlobServiceClient
        if CONNECTION_STRING:
            return BlobServiceClient.from_connection_string(CONNECTION_STRING)
        elif ACCOUNT_NAME and ACCOUNT_KEY:
            account_url = f"https://{ACCOUNT_NAME}.blob.core.windows.net"
            return BlobServiceClient(account_url=account_url, credential=ACCOUNT_KEY)
    except Exception as e:
        print(f"⚠️ Failed to initialize Azure BlobServiceClient: {e}")
        return None


def extract_valid_year(
    judgment_date: Optional[str] = None,
    diary_number: Optional[str] = None,
    filename: Optional[str] = None
) -> str:
    """
    Extracts a strict, valid 4-digit calendar year (1950-2099).
    Never mistakes a 5-digit diary number like 19121 for year 1912!
    """
    import re
    # 1. From judgment date (e.g. 02-01-2025 or 2025-01-02)
    if judgment_date and str(judgment_date).lower() != "none":
        m = re.search(r"\b(19\d{2}|20\d{2})\b", str(judgment_date))
        if m:
            return m.group(1)

    # 2. From diary number (e.g. 19121/2009 or 19121_2009)
    if diary_number and str(diary_number).lower() != "none":
        m = re.search(r"[/\-_](19\d{2}|20\d{2})\b", str(diary_number))
        if m:
            return m.group(1)

    # 3. From filename (e.g. ..._2025.pdf or ..._2009_...)
    if filename:
        matches = re.findall(r"(?:^|[_\-.])(19\d{2}|20\d{2})(?:[_\-.]|$)", filename)
        if matches:
            return matches[-1]

    return "general"


def build_unique_pdf_name(
    diary_number: Optional[str] = None,
    case_number: Optional[str] = None,
    judgment_date: Optional[str] = None,
    original_filename: Optional[str] = None
) -> str:
    """
    Constructs a clean, collision-free, identifiable PDF filename.
    Pattern: {diary_no}_{case_no}_{date}.pdf
    """
    import re
    parts = []

    if diary_number and str(diary_number).lower() != "none":
        clean_diary = re.sub(r"[^\w]", "_", str(diary_number).strip())
        parts.append(clean_diary)

    if case_number and str(case_number).lower() != "none":
        clean_case = re.sub(r"[^\w]", "_", str(case_number).strip())
        clean_case = re.sub(r"_+", "_", clean_case).strip("_")
        if clean_case:
            parts.append(clean_case[:40])

    if judgment_date and str(judgment_date).lower() != "none":
        clean_date = re.sub(r"[^\w]", "-", str(judgment_date).strip())
        if clean_date:
            parts.append(clean_date)

    if not parts and original_filename:
        clean_orig = Path(original_filename).name
        clean_orig = re.sub(r"_None(?=\.pdf)", "", clean_orig, flags=re.IGNORECASE)
        return clean_orig

    combined = "_".join(parts)
    combined = re.sub(r"_+", "_", combined).strip("_")
    combined = re.sub(r"_None(?=\.pdf|$)", "", combined, flags=re.IGNORECASE)

    if not combined.lower().endswith(".pdf"):
        combined += ".pdf"
    return combined


def upload_pdf_to_blob(
    local_pdf_path: Union[str, Path],
    court_code: str = "SCIN",
    year: Optional[str] = None,
    custom_blob_name: Optional[str] = None,
    diary_number: Optional[str] = None,
    case_number: Optional[str] = None,
    judgment_date: Optional[str] = None,
    delete_local_after: bool = False,
    container_name: Optional[str] = None
) -> Optional[str]:
    """
    Uploads a physical PDF judgment file to Azure Blob Storage container: liznr-legal-dev-judgment-pdfs
    Naming format: {court_code}/{year}/{diary_no}_{case_no}_{date}.pdf
    Returns: The permanent Azure Blob URL or None on failure.
    """
    p = Path(local_pdf_path)
    if not p.exists() or not p.is_file():
        print(f"⚠️ Local PDF file not found: {local_pdf_path}")
        return None

    client = get_blob_service_client()
    if not client:
        print("⚠️ Azure Blob Storage is not configured. Skipping cloud upload.")
        return None

    target_container = container_name or PDF_CONTAINER

    # Determine accurate 4-digit year
    if not year:
        year = extract_valid_year(
            judgment_date=judgment_date,
            diary_number=diary_number,
            filename=p.name
        )

    # Construct unique standardized filename (Flat structure: SCIN/{pdf_filename})
    if custom_blob_name:
        blob_name = custom_blob_name
    else:
        pdf_filename = build_unique_pdf_name(
            diary_number=diary_number,
            case_number=case_number,
            judgment_date=judgment_date,
            original_filename=p.name
        )
        blob_name = f"{court_code}/{pdf_filename}"

    try:
        from azure.storage.blob import ContentSettings
        blob_client = client.get_blob_client(container=target_container, blob=blob_name)
        
        with open(p, "rb") as data:
            blob_client.upload_blob(
                data,
                overwrite=True,
                content_settings=ContentSettings(content_type="application/pdf")
            )

        blob_url = blob_client.url
        print(f"[AZURE PDF] Uploaded to {target_container}/{blob_name}")

        # If direct cloud streaming is active, remove the temporary local PDF to save disk
        if delete_local_after:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

        return blob_url
    except Exception as e:
        print(f"[ERROR] Error uploading PDF to Azure Blob ({blob_name}): {e}")
        return None


def upload_pdf_bytes_to_blob(
    pdf_bytes: bytes,
    court_code: str = "SCIN",
    diary_number: Optional[str] = None,
    case_number: Optional[str] = None,
    judgment_date: Optional[str] = None,
    custom_blob_name: Optional[str] = None,
    container_name: Optional[str] = None
) -> Optional[str]:
    """
    Direct in-memory upload of PDF bytes to Azure Blob Storage without saving to local disk.
    Flat structure: SCIN/{pdf_filename}
    """
    client = get_blob_service_client()
    if not client:
        print("[WARNING] Azure Blob Storage is not configured.")
        return None

    target_container = container_name or PDF_CONTAINER

    if custom_blob_name:
        blob_name = custom_blob_name
    else:
        pdf_filename = build_unique_pdf_name(
            diary_number=diary_number,
            case_number=case_number,
            judgment_date=judgment_date
        )
        blob_name = f"{court_code}/{pdf_filename}"

    try:
        from azure.storage.blob import ContentSettings
        blob_client = client.get_blob_client(container=target_container, blob=blob_name)
        blob_client.upload_blob(
            pdf_bytes,
            overwrite=True,
            content_settings=ContentSettings(content_type="application/pdf")
        )
        blob_url = blob_client.url
        print(f"[AZURE PDF STREAM] Uploaded in-memory to {target_container}/{blob_name}")
        return blob_url
    except Exception as e:
        print(f"[ERROR] Error uploading in-memory PDF ({blob_name}): {e}")
        return None


def upload_json_to_blob(
    data_or_path: Union[str, Path, Dict[str, Any], list],
    court_code: str = "SCIN",
    layer: str = "Bronze",
    filename: Optional[str] = None,
    year: Optional[str] = None,
    custom_blob_name: Optional[str] = None,
    container_name: Optional[str] = None,
    merge: bool = True
) -> Optional[str]:
    """
    Uploads JSON archive to Azure Blob Storage: liznr-legal-dev-raw-judgments
    Prefix format: {court_code}_{Bronze|Silver}/{filename}.json
    Automatically merges with existing records in Azure to accumulate cases for the year.
    Returns: The permanent Azure Blob URL or None on failure.
    """
    client = get_blob_service_client()
    if not client:
        print("[WARNING] Azure Blob Storage is not configured. Skipping cloud upload.")
        return None

    target_container = container_name or RAW_CONTAINER

    # Parse incoming new JSON data
    new_data = None
    src_name = "data.json"
    if isinstance(data_or_path, (dict, list)):
        new_data = data_or_path
    else:
        p = Path(data_or_path)
        src_name = p.name
        if p.exists() and p.is_file():
            try:
                new_data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass

    # Build standardized blob path
    if custom_blob_name:
        blob_name = custom_blob_name
    else:
        layer_folder = f"{court_code}_{layer.capitalize()}"
        target_file = filename or src_name
        if not target_file.endswith(".json"):
            target_file += ".json"
        blob_name = f"{layer_folder}/{target_file}"

    # Smart Merging with existing Azure Blob content
    final_payload = new_data
    if merge and new_data is not None:
        try:
            blob_client = client.get_blob_client(container=target_container, blob=blob_name)
            if blob_client.exists():
                existing_bytes = blob_client.download_blob().readall()
                existing_json = json.loads(existing_bytes.decode("utf-8"))
                
                # Helper to extract list of records
                def extract_records(d):
                    if isinstance(d, dict):
                        for k, v in d.items():
                            if isinstance(v, list):
                                return k, v
                        return None, list(d.values())
                    elif isinstance(d, list):
                        return None, d
                    return None, []

                existing_key, existing_list = extract_records(existing_json)
                new_key, new_list = extract_records(new_data)
                court_key = new_key or existing_key or "Supreme Court of India"

                # Merge by unique identifier (diary_number or cnr or case_number)
                merged_dict = {}
                for item in existing_list:
                    if isinstance(item, dict):
                        # check nested case dict or top level
                        d_no = item.get("diary_number") or item.get("case", {}).get("cnr") or item.get("cnr") or item.get("case_number")
                        if d_no:
                            merged_dict[str(d_no)] = item

                for item in new_list:
                    if isinstance(item, dict):
                        d_no = item.get("diary_number") or item.get("case", {}).get("cnr") or item.get("cnr") or item.get("case_number")
                        if d_no:
                            merged_dict[str(d_no)] = item
                        else:
                            # fallback key
                            merged_dict[f"item_{len(merged_dict)}"] = item

                merged_records = list(merged_dict.values())
                print(f"[AZURE MERGE] Merged {len(existing_list)} existing + {len(new_list)} new -> {len(merged_records)} total {layer} records.")
                final_payload = {court_key: merged_records} if (court_key or isinstance(new_data, dict)) else merged_records
        except Exception as merge_err:
            print(f"[AZURE MERGE NOTICE] Could not merge with existing blob, uploading as-is: {merge_err}")

    # Prepare payload bytes
    if final_payload is not None:
        payload_bytes = json.dumps(final_payload, ensure_ascii=False, indent=2).encode("utf-8")
    else:
        p = Path(data_or_path)
        payload_bytes = p.read_bytes() if (p.exists() and p.is_file()) else str(data_or_path).encode("utf-8")

    try:
        from azure.storage.blob import ContentSettings
        blob_client = client.get_blob_client(container=target_container, blob=blob_name)
        blob_client.upload_blob(
            payload_bytes,
            overwrite=True,
            content_settings=ContentSettings(content_type="application/json")
        )
        blob_url = blob_client.url
        print(f"[AZURE JSON] Uploaded {layer.upper()} archive to {target_container}/{blob_name}")
        return blob_url
    except Exception as e:
        print(f"[ERROR] Error uploading JSON to Azure Blob ({blob_name}): {e}")
        return None


def sync_court_pdfs_to_blob(pdf_dir: Union[str, Path], court_code: str = "SCIN") -> Dict[str, str]:
    """
    Batch uploads all PDF files in a court directory to Azure Blob Storage.
    Returns: Dict mapping {local_filename: azure_blob_url}
    """
    pdf_path = Path(pdf_dir)
    if not pdf_path.exists() or not pdf_path.is_dir():
        print(f"[WARNING] Directory not found: {pdf_dir}")
        return {}

    pdf_files = list(pdf_path.glob("*.pdf"))
    print(f"[SYNC] Starting Azure Blob sync for {len(pdf_files)} PDFs in {pdf_path.name}...")

    results = {}
    success = 0
    for file in pdf_files:
        url = upload_pdf_to_blob(file, court_code=court_code)
        if url:
            results[file.name] = url
            success += 1

    print(f"[SUCCESS] Sync complete: {success}/{len(pdf_files)} PDFs uploaded to Azure Blob Storage.")
    return results


# ============================================================
# CLI TEST UTILITY
# ============================================================

if __name__ == "__main__":
    import sys
    print("=" * 60)
    print("AZURE BLOB STORAGE DIAGNOSTICS")
    print("=" * 60)
    print(f"Account Name  : {ACCOUNT_NAME or 'Not Set'}")
    print(f"PDF Container : {PDF_CONTAINER}")
    print(f"RAW Container : {RAW_CONTAINER}")
    print(f"Configured    : {is_azure_blob_configured()}")
    print("-" * 60)

    client = get_blob_service_client()
    if client:
        try:
            print("[CONNECTING] Verifying Azure Storage connection & containers...")
            containers = [c.name for c in client.list_containers()]
            print(f"[SUCCESS] Connection successful! Found {len(containers)} container(s):")
            for c in containers:
                print(f"  - {c}")
        except Exception as e:
            print(f"[ERROR] Connection error: {e}")
    else:
        print("[WARNING] Azure Blob credentials missing in .env file.")

