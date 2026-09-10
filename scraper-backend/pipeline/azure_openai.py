"""
Azure OpenAI config/auth helpers, shared by any pipeline stage that calls
Azure OpenAI (currently pipeline/llm_enrichment.py only).

Split out of the old pipeline/extraction.py (the Phase 3 LLM-extraction
stage, superseded 2026-09-08 by regex_extraction.py + llm_enrichment.py and
removed) so this reusable config/auth logic didn't get deleted along with
the stage-specific code that used to live around it.
"""

import os

# Read fresh from os.environ on every call rather than cached as
# module-level constants at import time — matching db/connection.py's
# convention elsewhere in this service, and specifically so tests can
# toggle these by setting/unsetting the env var mid-run without needing to
# reload this module.


def azure_config() -> dict:
    return {
        "endpoint": os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip().rstrip("/"),
        "api_key": os.environ.get("AZURE_OPENAI_API_KEY", "").strip(),
        # A *deployment name* (what you named the model deployment in Azure
        # AI Foundry / the Azure OpenAI resource), not a base model id like
        # "gpt-4o" — Azure routes by deployment name in the URL path, unlike
        # Groq/OpenAI where the model id goes in the request body.
        "deployment": os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip(),
        # Any Azure OpenAI API version that supports response_format=json_object
        # for your deployment's base model (gpt-4o/gpt-4o-mini/gpt-35-turbo-1106+
        # all support it from 2023-12-01-preview onward).
        "api_version": os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21").strip(),
    }


def is_configured(config: dict) -> bool:
    return bool(config["endpoint"] and config["api_key"] and config["deployment"])
