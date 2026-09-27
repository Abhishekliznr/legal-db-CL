# Hand-kept mirror of scraper-backend's orchestrator/registry.py (no shared code between the
# services): which cr_court_scrape_config.adapter values the worker can actually run. Add a
# court here in the same change that registers its adapter there.
SUPPORTED_ADAPTERS = {
    "supreme_court": {"resumable": True},
    "high_court_mp": {"resumable": True},
}


def is_supported(adapter: str) -> bool:
    return adapter in SUPPORTED_ADAPTERS


def is_resumable(adapter: str) -> bool:
    return SUPPORTED_ADAPTERS.get(adapter, {}).get("resumable", False)
