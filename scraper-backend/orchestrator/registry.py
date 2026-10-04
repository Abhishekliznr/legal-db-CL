"""
The one place a court's adapter + extraction/promotion pipeline gets registered, keyed by
cr_court_scrape_config.adapter. api-backend keeps a hand-mirrored list of these keys in
scraper/courts.py (it decides what can be started/resumed) — update both together.
"""

from dataclasses import dataclass
from typing import Optional

from adapters.high_courts.mp.adapter import MPHighCourtAdapter
from adapters.high_courts.mp.promotion import promote_ingestion as _mp_promote
from adapters.high_courts.mp.promotion import save_case_without_judgment as _mp_save_without_judgment
from adapters.high_courts.telangana.adapter import TelanganaHighCourtAdapter
from adapters.high_courts.telangana.promotion import promote_ingestion as _tg_promote
from adapters.high_courts.telangana.promotion import save_case_without_judgment as _tg_save_without_judgment
from adapters.supreme_court.adapter import SupremeCourtAdapter
from adapters.supreme_court.extraction import find_provision_paragraphs as _sc_find_provisions
from adapters.supreme_court.promotion import promote_ingestion as _sc_promote
from adapters.supreme_court.promotion import save_case_without_judgment as _sc_save_without_judgment
from orchestrator.batch_runner import FindProvisionsFn, PromoteFn, SaveWithoutJudgmentFn


@dataclass(frozen=True)
class AdapterSpec:
    adapter_class: type  # implements adapters.base.ScraperAdapter
    promote_fn: PromoteFn  # this court's own cr_cases promotion, e.g. adapters.supreme_court.promotion.promote_ingestion
    data_source: str  # data_source_enum value new records get tagged with — resolved here, never left to a DB column DEFAULT (a prior real bug: every source silently landed as 'ECOURTS')
    find_provisions_fn: Optional[FindProvisionsFn] = None  # optional: this court's OCR-text provision-paragraph finder, fed to pipeline.llm_enrichment.enrich_case
    run_enrichment: bool = True  # False for a court whose pipeline doesn't use LLM enrichment yet — see batch_runner.run_batch's own docstring
    save_without_judgment_fn: Optional[SaveWithoutJudgmentFn] = None  # saves a case's metadata when its PDF is missing; without one those cases are skipped


ADAPTER_REGISTRY = {
    "supreme_court": AdapterSpec(
        adapter_class=SupremeCourtAdapter,
        promote_fn=_sc_promote,
        save_without_judgment_fn=_sc_save_without_judgment,
        find_provisions_fn=_sc_find_provisions,
        data_source="SCI_WEBSITE",
    ),
    "high_court_mp": AdapterSpec(
        adapter_class=MPHighCourtAdapter,
        promote_fn=_mp_promote,
        save_without_judgment_fn=_mp_save_without_judgment,
        # MP's sections/acts come straight from case-status's own Act lines
        # at promotion time (adapters/high_courts/mp/promotion.py) when
        # present. find_provision_paragraphs is reused as-is from Supreme
        # Court's own extraction module (its paragraph-finding logic isn't
        # SC-specific) so pipeline/llm_enrichment.py's enrich_case() can
        # fall back to the same LLM-based provision extraction Supreme
        # Court uses -- but only actually WRITES sections/acts when MP's
        # own promotion left them empty (see enrich_case()'s own
        # existing_sections guard), never overwriting real case-status data.
        find_provisions_fn=_sc_find_provisions,
        data_source="MPHC_WEBSITE",
    ),
    "high_court_telangana": AdapterSpec(
        adapter_class=TelanganaHighCourtAdapter,
        promote_fn=_tg_promote,
        save_without_judgment_fn=_tg_save_without_judgment,
        find_provisions_fn=_sc_find_provisions,
        data_source="TSHC_WEBSITE",
    ),
}


def adapter_kwargs(config: dict, headless: bool) -> dict:
    # config["config"] is that court's own free-form settings (JSONB); each adapter reads only the keys it defined.
    return {**(config.get("config") or {}), "headless": headless}
