"""
Canonical industry taxonomy for LLM-classified `cases.industries`.

Reused as reference data from the old scraper-backend's
SUPREME_COURT_OF_INDIA_SCRAPER/pdf_metadata_extractor.py's INDUSTRY_KEYWORDS
dict -- only the category NAMES survive here, not its keyword-matching
approach (that was a whole-document keyword scan, rejected as a regex
extraction method in adapters/supreme_court/extraction.py's module docstring: a
single incidental mention mistags an unrelated case). The names themselves
are still a reasonable closed vocabulary for an LLM classifier that reads
the actual text and reasons about what the case is centrally about, unlike
a keyword scan -- same "reused as data, not logic" treatment
normalization/acts.py already gives CANONICAL_STATUTES.

Passed to pipeline/llm_enrichment.py's prompt as the only allowed values,
so results stay consistent across documents (the same industry name
resolves to the same `industries` row) instead of the LLM inventing
near-duplicate free-text names ("Banking" vs "Banking Sector" vs "Banks").
"""

CANONICAL_INDUSTRIES = [
    "Agriculture and Agro Products",
    "Aquaculture and Fisheries",
    "Auto",
    "Aviation",
    "Banks",
    "Breweries and Distilleries",
    "Capital Goods/ Engineering",
    "Cement",
    "Chemicals",
    "Construction/ Building Products",
    "Consumer Durables",
    "Cooperative Societies",
    "Education",
    "EOU/ SEZ/ Exporters",
    "Explosives",
    "Fertilizers",
    "Finance",
    "FMCG",
    "Gems and Jewellery",
    "Healthcare",
    "Hospitality",
    "Importers",
    "Information Technology",
    "Infrastructure",
    "Insurance",
    "Media and Entertainment",
    "Metals",
    "Mines and Minerals",
    "Oil and Gas",
    "Paper and Packaging",
    "Pharmaceutical",
    "Power and Energy",
    "Publishing & Printing",
    "Real Estate",
    "Retail",
    "Rubber",
    "Services Sector",
    "Shipping",
    "Sugar",
    "Telecom",
    "Textile",
    "Tobacco",
    "Trading",
    "Transport and Logistics",
]

_CANONICAL_LOOKUP = {name.lower(): name for name in CANONICAL_INDUSTRIES}


def resolve_industry(raw_name: str) -> "str | None":
    """
    Case-insensitive exact match against CANONICAL_INDUSTRIES, or None if
    the LLM returned something outside the closed list despite the prompt
    instructing it not to -- dropped rather than stored as a fabricated-
    looking new category (same defensive treatment
    adapters/supreme_court/promotion.py's _validate_enum gives an LLM enum response).
    """
    if not raw_name:
        return None
    return _CANONICAL_LOOKUP.get(raw_name.strip().lower())
