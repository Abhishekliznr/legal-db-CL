-- 0002_dedupe_acts_nulls_not_distinct.sql
--
-- Fixes Bug 2: cr_acts.act_name/act_year is UNIQUE (act_name, act_year), and
-- Postgres treats NULLs as distinct in a plain UNIQUE constraint by default.
-- A year-less act (e.g. "Constitution of India", act_year IS NULL) therefore
-- never actually conflicts on re-insertion -- pipeline/promotion.py's
-- _get_or_create_act() ON CONFLICT silently never fires for these, its
-- fallback SELECT is never reached, and a fresh duplicate cr_acts row is
-- created every time instead of reusing the existing one.
--
-- No Python change is required alongside this migration: _get_or_create_act()
-- already issues `ON CONFLICT (act_name, act_year) DO NOTHING RETURNING
-- act_id`, and Postgres's ON CONFLICT arbiter matches a unique
-- constraint/index by its columns regardless of its NULLS [NOT] DISTINCT
-- setting -- the fallback SELECT's `(act_year = %s OR (act_year IS NULL AND
-- %s IS NULL))` was already NULL-safe. Fixing the constraint alone closes
-- the bug.
--
-- This migration:
--   1. Merges any duplicate cr_acts rows that already accumulated this way
--      (grouped by act_name + act_year, treating NULL act_year as one
--      group -- the same grouping Postgres will enforce going forward),
--      repointing cr_sections/cr_rules/cr_orders.act_id and cr_cases.acts
--      onto a single surviving act_id per group.
--   2. That repoint can itself create duplicate cr_sections/cr_rules/
--      cr_orders rows -- two rows for the same section/rule/order number
--      that used to point at two different act_id's for what is now the
--      "same" act. Merges those too, repointing every cr_cases array
--      column that references section_id/rule_id/order_id.
--   3. Recreates cr_acts's unique constraint as UNIQUE NULLS NOT DISTINCT
--      (Postgres 15+, matching db/schema.sql line 3's stated target) so
--      the bug can't recur. Guarded so this is a no-op once already
--      applied.
--
-- NOTE -- related, NOT fixed here (out of scope for this migration; flagged
-- for a separate decision): cr_rules.act_id and cr_orders.act_id are
-- nullable (a standalone rule/order not resolved to a named Act), and their
-- own UNIQUE (act_id, rule_number)/(act_id, order_number) constraints have
-- the identical NULLs-are-distinct exposure for act_id IS NULL rows. Step 2
-- below merges any such duplicates it finds (window-function PARTITION BY
-- already treats NULL act_id as one group), but does not change those two
-- tables' constraints, since only cr_acts's constraint was in scope here.
--
-- Idempotent: safe to run more than once. On a database with no existing
-- duplicates and the constraint already fixed, every step below is a no-op.
--
-- Apply with:
--   psql "$YOUR_CONN_STRING" -f db/migrations/0002_dedupe_acts_nulls_not_distinct.sql
-- or, using this repo's discrete env vars:
--   python -c "from db.connection import get_connection; \
--       conn = get_connection(); \
--       conn.cursor().execute(open('db/migrations/0002_dedupe_acts_nulls_not_distinct.sql').read()); \
--       conn.commit()"
--
-- NOT run against any real database as part of writing this file.

BEGIN;

-- ---------------------------------------------------------------------
-- 1. Merge duplicate cr_acts rows.
-- ---------------------------------------------------------------------

CREATE TEMP TABLE _act_id_map ON COMMIT DROP AS
SELECT
    a.act_id AS duplicate_id,
    MIN(a.act_id) OVER (PARTITION BY a.act_name, a.act_year) AS survivor_id
FROM cr_acts a;

DELETE FROM _act_id_map WHERE duplicate_id = survivor_id;

UPDATE cr_sections s SET act_id = m.survivor_id
FROM _act_id_map m WHERE s.act_id = m.duplicate_id;

UPDATE cr_rules r SET act_id = m.survivor_id
FROM _act_id_map m WHERE r.act_id = m.duplicate_id;

UPDATE cr_orders o SET act_id = m.survivor_id
FROM _act_id_map m WHERE o.act_id = m.duplicate_id;

UPDATE cr_cases c
SET acts = (
    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
    FROM unnest(c.acts) AS elem
    LEFT JOIN _act_id_map m ON m.duplicate_id = elem
)
WHERE c.acts && (SELECT COALESCE(array_agg(duplicate_id), '{}') FROM _act_id_map);

DELETE FROM cr_acts a USING _act_id_map m WHERE a.act_id = m.duplicate_id;

-- ---------------------------------------------------------------------
-- 2. Merge duplicate cr_sections/cr_rules/cr_orders rows created by the
--    act_id repoint above (two rows now sharing the same act_id + number).
-- ---------------------------------------------------------------------

-- -- sections --
CREATE TEMP TABLE _section_id_map ON COMMIT DROP AS
SELECT
    s.section_id AS duplicate_id,
    MIN(s.section_id) OVER (PARTITION BY s.act_id, s.section_number) AS survivor_id
FROM cr_sections s;
DELETE FROM _section_id_map WHERE duplicate_id = survivor_id;

UPDATE cr_cases c SET sections = (
    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
    FROM unnest(c.sections) AS elem LEFT JOIN _section_id_map m ON m.duplicate_id = elem
) WHERE c.sections && (SELECT COALESCE(array_agg(duplicate_id), '{}') FROM _section_id_map);

UPDATE cr_cases c SET sections_relevant = (
    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
    FROM unnest(c.sections_relevant) AS elem LEFT JOIN _section_id_map m ON m.duplicate_id = elem
) WHERE c.sections_relevant && (SELECT COALESCE(array_agg(duplicate_id), '{}') FROM _section_id_map);

UPDATE cr_cases c SET sections_other = (
    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
    FROM unnest(c.sections_other) AS elem LEFT JOIN _section_id_map m ON m.duplicate_id = elem
) WHERE c.sections_other && (SELECT COALESCE(array_agg(duplicate_id), '{}') FROM _section_id_map);

DELETE FROM cr_sections s USING _section_id_map m WHERE s.section_id = m.duplicate_id;

-- -- rules --
CREATE TEMP TABLE _rule_id_map ON COMMIT DROP AS
SELECT
    r.rule_id AS duplicate_id,
    MIN(r.rule_id) OVER (PARTITION BY r.act_id, r.rule_number) AS survivor_id
FROM cr_rules r;
DELETE FROM _rule_id_map WHERE duplicate_id = survivor_id;

UPDATE cr_cases c SET rules = (
    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
    FROM unnest(c.rules) AS elem LEFT JOIN _rule_id_map m ON m.duplicate_id = elem
) WHERE c.rules && (SELECT COALESCE(array_agg(duplicate_id), '{}') FROM _rule_id_map);

UPDATE cr_cases c SET rules_relevant = (
    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
    FROM unnest(c.rules_relevant) AS elem LEFT JOIN _rule_id_map m ON m.duplicate_id = elem
) WHERE c.rules_relevant && (SELECT COALESCE(array_agg(duplicate_id), '{}') FROM _rule_id_map);

UPDATE cr_cases c SET rules_other = (
    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
    FROM unnest(c.rules_other) AS elem LEFT JOIN _rule_id_map m ON m.duplicate_id = elem
) WHERE c.rules_other && (SELECT COALESCE(array_agg(duplicate_id), '{}') FROM _rule_id_map);

DELETE FROM cr_rules r USING _rule_id_map m WHERE r.rule_id = m.duplicate_id;

-- -- orders --
CREATE TEMP TABLE _order_id_map ON COMMIT DROP AS
SELECT
    o.order_id AS duplicate_id,
    MIN(o.order_id) OVER (PARTITION BY o.act_id, o.order_number) AS survivor_id
FROM cr_orders o;
DELETE FROM _order_id_map WHERE duplicate_id = survivor_id;

UPDATE cr_cases c SET orders = (
    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
    FROM unnest(c.orders) AS elem LEFT JOIN _order_id_map m ON m.duplicate_id = elem
) WHERE c.orders && (SELECT COALESCE(array_agg(duplicate_id), '{}') FROM _order_id_map);

UPDATE cr_cases c SET orders_relevant = (
    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
    FROM unnest(c.orders_relevant) AS elem LEFT JOIN _order_id_map m ON m.duplicate_id = elem
) WHERE c.orders_relevant && (SELECT COALESCE(array_agg(duplicate_id), '{}') FROM _order_id_map);

UPDATE cr_cases c SET orders_other = (
    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
    FROM unnest(c.orders_other) AS elem LEFT JOIN _order_id_map m ON m.duplicate_id = elem
) WHERE c.orders_other && (SELECT COALESCE(array_agg(duplicate_id), '{}') FROM _order_id_map);

DELETE FROM cr_orders o USING _order_id_map m WHERE o.order_id = m.duplicate_id;

-- ---------------------------------------------------------------------
-- 3. Recreate cr_acts's unique constraint as NULLS NOT DISTINCT.
-- ---------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint con
        JOIN pg_index idx ON idx.indexrelid = con.conindid
        WHERE con.conname = 'uq_cr_acts_name_year' AND idx.indnullsnotdistinct
    ) THEN
        ALTER TABLE cr_acts DROP CONSTRAINT IF EXISTS uq_cr_acts_name_year;
        ALTER TABLE cr_acts ADD CONSTRAINT uq_cr_acts_name_year UNIQUE NULLS NOT DISTINCT (act_name, act_year);
    END IF;
END $$;

COMMIT;
