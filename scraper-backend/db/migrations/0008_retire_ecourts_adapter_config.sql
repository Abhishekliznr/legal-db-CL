-- 0008_retire_ecourts_adapter_config.sql
--
-- The generic eCourts adapter (one adapter for all High Courts, config-driven
-- by state_code/bench_code) is retired -- each High Court now gets its own
-- adapter + extraction/promotion pipeline under adapters/high_courts/<code>/.
-- See adapters/__init__.py and routers/scraper_router.py's _ADAPTER_REGISTRY.
--
-- cr_court_scrape_config.adapter no longer validates against a fixed CHECK
-- (the valid set now grows one court at a time, in code, via
-- _ADAPTER_REGISTRY) -- any existing row with adapter='ecourts' is left as
-- data (harmless: it simply won't resolve to anything in the registry until
-- that court's own adapter is built and registered, at which point its
-- config row's `adapter` value should be updated to match).
--
-- state_code/bench_code were eCourts-specific selectors, meaningless now;
-- replaced with a generic `config` JSONB column any adapter can use for its
-- own settings. Existing state_code/bench_code values are dropped, not
-- migrated into `config` -- they described a source (eCourts) no adapter
-- reads from anymore.
--
-- cr_raw_ingestions.data_source's DEFAULT moves off 'ECOURTS' (see
-- db/schema.sql's column comment) since every insert already sets it
-- explicitly; this only changes the column default for future inserts,
-- existing rows are untouched.
--
-- NOT run against any real database as part of writing this file.

ALTER TABLE cr_court_scrape_config
    DROP CONSTRAINT IF EXISTS ck_cr_court_scrape_config_adapter,
    DROP COLUMN IF EXISTS state_code,
    DROP COLUMN IF EXISTS bench_code,
    ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}';

ALTER TABLE cr_raw_ingestions
    ALTER COLUMN data_source SET DEFAULT 'OTHER';
