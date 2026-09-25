-- 0012_drop_cr_cases_provision_split.sql
--
-- Simplifies cr_cases' provision tracking down to just sections+acts --
-- the rules/orders distinction, and the LLM-classified relevant/other
-- split on top of it, are no longer wanted. Drops:
--   cr_cases.rules, cr_cases.orders
--   cr_cases.sections_relevant, cr_cases.sections_other
--   cr_cases.rules_relevant, cr_cases.rules_other
--   cr_cases.orders_relevant, cr_cases.orders_other
-- and the now-orphaned cr_rules/cr_orders lookup tables -- nothing
-- references rule_id/order_id anywhere once the arrays above are gone
-- (confirmed: api-backend's cr_case_search_view/search_router.py and every
-- promotion/enrichment module in scraper-backend were updated in the same
-- change that added this migration).
--
-- The sections_relevant/_other/rules_relevant/_other/orders_relevant/_other
-- columns were already dead in api-backend as of its own 2026-09-13
-- rewrite (no live query read them) -- this just removes the columns
-- themselves. rules/orders (the unified, non-split arrays) WERE still live
-- (api-backend's get_case_detail joined cr_rules/cr_orders against them) --
-- see api-backend/routers/search_router.py's matching update.
--
-- DESTRUCTIVE: any rule/order provisions already stored in these columns
-- are lost. NOT run against any real database as part of writing this file.
--
-- cr_case_search_view (api-backend's own view) selects cr_cases.rules, so
-- it must be dropped BEFORE the ALTER TABLE below, same as 0006's
-- structured_content drop. Recreate it afterwards from api-backend:
--   python -m db.init_db ensure-view

DROP VIEW IF EXISTS cr_case_search_view;

ALTER TABLE cr_cases
    DROP COLUMN IF EXISTS rules,
    DROP COLUMN IF EXISTS orders,
    DROP COLUMN IF EXISTS sections_relevant,
    DROP COLUMN IF EXISTS sections_other,
    DROP COLUMN IF EXISTS rules_relevant,
    DROP COLUMN IF EXISTS rules_other,
    DROP COLUMN IF EXISTS orders_relevant,
    DROP COLUMN IF EXISTS orders_other;

DROP TABLE IF EXISTS cr_rules;
DROP TABLE IF EXISTS cr_orders;
