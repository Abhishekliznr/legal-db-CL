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
--   1. Computes a plan for merging any duplicate cr_acts rows that already
--      accumulated this way (grouped by act_name + act_year, treating NULL
--      act_year as one group -- the same grouping Postgres will enforce
--      going forward).
--   2. For sections/rules/orders in turn: computes which of THEIR rows
--      would collide once their act_id is repointed per the plan above
--      (two rows for the same section/rule/order number that used to point
--      at two different act_id's for what is about to become the "same"
--      act), repoints every cr_cases array column referencing the
--      about-to-be-removed duplicate row ids onto the surviving id, deletes
--      the now-redundant duplicate rows, and ONLY THEN repoints the
--      surviving rows' act_id -- see the 2026-09-19 FIX note below for why
--      this order is load-bearing, not arbitrary.
--   3. Repoints cr_cases.acts and deletes the duplicate cr_acts rows
--      (safe now: nothing still points a section/rule/order at one of
--      them).
--   4. Recreates cr_acts's unique constraint as UNIQUE NULLS NOT DISTINCT
--      (Postgres 15+, matching db/schema.sql line 3's stated target) so
--      the bug can't recur. Guarded so this is a no-op once already
--      applied.
--
-- NOTE -- related, NOT fixed here (out of scope for this migration; flagged
-- for a separate decision): cr_rules.act_id and cr_orders.act_id are
-- nullable (a standalone rule/order not resolved to a named Act), and their
-- own UNIQUE (act_id, rule_number)/(act_id, order_number) constraints have
-- the identical NULLs-are-distinct exposure for act_id IS NULL rows. The
-- merge below handles any such duplicates it finds the same way (window
-- function PARTITION BY already treats NULL act_id as one group), but does
-- not change those two tables' constraints, since only cr_acts's constraint
-- was in scope here.
--
-- 2026-09-19 FIX (found by actually running this migration against a
-- database seeded with real duplicate-act data, not just inspection): the
-- original version repointed cr_sections/cr_rules/cr_orders.act_id onto the
-- surviving act_id FIRST, then deduped the resulting collisions in a
-- separate later step. That ordering is wrong -- UNIQUE constraints are
-- checked per-statement (not deferred) by default, so the very first
-- repointing UPDATE that creates a collision (e.g. two "Section 226" rows,
-- one under each of two now-merged "Constitution of India" act rows) fails
-- immediately with `duplicate key value violates unique constraint
-- uq_cr_sections_act_number`, aborting the whole transaction before the
-- later dedup step ever runs. Fixed by computing each table's merge plan
-- using the PROSPECTIVE post-repoint act_id (COALESCE'd against the act
-- plan) up front, applying it to cr_cases' arrays and deleting the
-- redundant duplicate rows FIRST, and only THEN repointing the single
-- surviving row per group's act_id -- by construction there is no longer
-- a second row to collide with once repointing actually happens.
--
-- 2026-09-19 UPDATE (db/migrations/0012_drop_cr_cases_provision_split.sql):
-- cr_rules/cr_orders and cr_cases.rules/orders/sections_relevant/
-- sections_other/rules_relevant/rules_other/orders_relevant/orders_other
-- were dropped entirely -- rules/orders as a distinct provision kind are no
-- longer tracked. This migration runs BEFORE 0012 in filename order, so on
-- a database that's carrying real history through both, those tables/
-- columns still exist when this file runs and the merge below is still
-- correct (0012 drops them right after). On a genuinely fresh database
-- (schema.sql already excludes them, so they never existed at all), every
-- step below that touches cr_rules/cr_orders or the _relevant/_other
-- columns is guarded to a no-op instead of erroring on a table/column that
-- was never created -- there's nothing to dedupe there in either case.
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
-- Verified 2026-09-19 against a real (throwaway, local) Postgres seeded
-- with duplicate cr_acts/cr_sections/cr_rules/cr_orders rows and cr_cases
-- rows referencing both sides of each duplicate: confirmed the merge
-- completes without error and collapses each duplicate group down to one
-- surviving row, with cr_cases' array columns correctly repointed. Also
-- verified against a fresh database that never had cr_rules/cr_orders/the
-- _relevant/_other columns at all (this file running as part of a full
-- schema.sql + all-migrations bootstrap) -- every guarded block is a
-- correct no-op there.

BEGIN;

-- ---------------------------------------------------------------------
-- 1. Plan which cr_acts rows will merge (no changes yet).
-- ---------------------------------------------------------------------

CREATE TEMP TABLE _act_id_map ON COMMIT DROP AS
SELECT
    a.act_id AS duplicate_id,
    MIN(a.act_id) OVER (PARTITION BY a.act_name, a.act_year) AS survivor_id
FROM cr_acts a;

DELETE FROM _act_id_map WHERE duplicate_id = survivor_id;

-- ---------------------------------------------------------------------
-- 2. Sections: merge-then-repoint (see 2026-09-19 FIX note above for why
--    this order, not the reverse, is required).
-- ---------------------------------------------------------------------

CREATE TEMP TABLE _section_id_map ON COMMIT DROP AS
SELECT
    s.section_id AS duplicate_id,
    MIN(s.section_id) OVER (
        PARTITION BY COALESCE(am.survivor_id, s.act_id), s.section_number
    ) AS survivor_id
FROM cr_sections s
LEFT JOIN _act_id_map am ON am.duplicate_id = s.act_id;

DELETE FROM _section_id_map WHERE duplicate_id = survivor_id;

UPDATE cr_cases c SET sections = (
    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
    FROM unnest(c.sections) AS elem LEFT JOIN _section_id_map m ON m.duplicate_id = elem
) WHERE c.sections && (SELECT COALESCE(array_agg(duplicate_id), '{}') FROM _section_id_map);

-- sections_relevant/sections_other: dropped 2026-09-19 (0012) -- guarded,
-- see the UPDATE note above.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'cr_cases' AND column_name = 'sections_relevant') THEN
        EXECUTE '
            UPDATE cr_cases c SET sections_relevant = (
                SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
                FROM unnest(c.sections_relevant) AS elem LEFT JOIN _section_id_map m ON m.duplicate_id = elem
            ) WHERE c.sections_relevant && (SELECT COALESCE(array_agg(duplicate_id), ''{}'') FROM _section_id_map)';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'cr_cases' AND column_name = 'sections_other') THEN
        EXECUTE '
            UPDATE cr_cases c SET sections_other = (
                SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
                FROM unnest(c.sections_other) AS elem LEFT JOIN _section_id_map m ON m.duplicate_id = elem
            ) WHERE c.sections_other && (SELECT COALESCE(array_agg(duplicate_id), ''{}'') FROM _section_id_map)';
    END IF;
END $$;

-- Delete the now-redundant duplicate rows BEFORE repointing act_id on the
-- one survivor per group -- by the time the repoint below runs, each
-- surviving row is the only remaining row for its prospective
-- (final act_id, section_number) pair, so it can never collide.
DELETE FROM cr_sections s USING _section_id_map m WHERE s.section_id = m.duplicate_id;

UPDATE cr_sections s SET act_id = m.survivor_id
FROM _act_id_map m WHERE s.act_id = m.duplicate_id;

-- ---------------------------------------------------------------------
-- 3. Rules/Orders: same merge-then-repoint pattern, guarded -- dropped
--    entirely 2026-09-19 (0012), see the UPDATE note above.
-- ---------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('public.cr_rules') IS NOT NULL THEN
        CREATE TEMP TABLE _rule_id_map ON COMMIT DROP AS
        SELECT
            r.rule_id AS duplicate_id,
            MIN(r.rule_id) OVER (
                PARTITION BY COALESCE(am.survivor_id, r.act_id), r.rule_number
            ) AS survivor_id
        FROM cr_rules r
        LEFT JOIN _act_id_map am ON am.duplicate_id = r.act_id;
        DELETE FROM _rule_id_map WHERE duplicate_id = survivor_id;

        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'cr_cases' AND column_name = 'rules') THEN
            EXECUTE '
                UPDATE cr_cases c SET rules = (
                    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
                    FROM unnest(c.rules) AS elem LEFT JOIN _rule_id_map m ON m.duplicate_id = elem
                ) WHERE c.rules && (SELECT COALESCE(array_agg(duplicate_id), ''{}'') FROM _rule_id_map)';
        END IF;
        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'cr_cases' AND column_name = 'rules_relevant') THEN
            EXECUTE '
                UPDATE cr_cases c SET rules_relevant = (
                    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
                    FROM unnest(c.rules_relevant) AS elem LEFT JOIN _rule_id_map m ON m.duplicate_id = elem
                ) WHERE c.rules_relevant && (SELECT COALESCE(array_agg(duplicate_id), ''{}'') FROM _rule_id_map)';
        END IF;
        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'cr_cases' AND column_name = 'rules_other') THEN
            EXECUTE '
                UPDATE cr_cases c SET rules_other = (
                    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
                    FROM unnest(c.rules_other) AS elem LEFT JOIN _rule_id_map m ON m.duplicate_id = elem
                ) WHERE c.rules_other && (SELECT COALESCE(array_agg(duplicate_id), ''{}'') FROM _rule_id_map)';
        END IF;

        DELETE FROM cr_rules r USING _rule_id_map m WHERE r.rule_id = m.duplicate_id;
        UPDATE cr_rules r SET act_id = m.survivor_id FROM _act_id_map m WHERE r.act_id = m.duplicate_id;

        DROP TABLE _rule_id_map;
    END IF;

    IF to_regclass('public.cr_orders') IS NOT NULL THEN
        CREATE TEMP TABLE _order_id_map ON COMMIT DROP AS
        SELECT
            o.order_id AS duplicate_id,
            MIN(o.order_id) OVER (
                PARTITION BY COALESCE(am.survivor_id, o.act_id), o.order_number
            ) AS survivor_id
        FROM cr_orders o
        LEFT JOIN _act_id_map am ON am.duplicate_id = o.act_id;
        DELETE FROM _order_id_map WHERE duplicate_id = survivor_id;

        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'cr_cases' AND column_name = 'orders') THEN
            EXECUTE '
                UPDATE cr_cases c SET orders = (
                    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
                    FROM unnest(c.orders) AS elem LEFT JOIN _order_id_map m ON m.duplicate_id = elem
                ) WHERE c.orders && (SELECT COALESCE(array_agg(duplicate_id), ''{}'') FROM _order_id_map)';
        END IF;
        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'cr_cases' AND column_name = 'orders_relevant') THEN
            EXECUTE '
                UPDATE cr_cases c SET orders_relevant = (
                    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
                    FROM unnest(c.orders_relevant) AS elem LEFT JOIN _order_id_map m ON m.duplicate_id = elem
                ) WHERE c.orders_relevant && (SELECT COALESCE(array_agg(duplicate_id), ''{}'') FROM _order_id_map)';
        END IF;
        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'cr_cases' AND column_name = 'orders_other') THEN
            EXECUTE '
                UPDATE cr_cases c SET orders_other = (
                    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
                    FROM unnest(c.orders_other) AS elem LEFT JOIN _order_id_map m ON m.duplicate_id = elem
                ) WHERE c.orders_other && (SELECT COALESCE(array_agg(duplicate_id), ''{}'') FROM _order_id_map)';
        END IF;

        DELETE FROM cr_orders o USING _order_id_map m WHERE o.order_id = m.duplicate_id;
        UPDATE cr_orders o SET act_id = m.survivor_id FROM _act_id_map m WHERE o.act_id = m.duplicate_id;

        DROP TABLE _order_id_map;
    END IF;
END $$;

-- ---------------------------------------------------------------------
-- 4. Now safe to repoint cr_cases.acts and delete the duplicate cr_acts
--    rows -- nothing still points a section/rule/order at one of them.
-- ---------------------------------------------------------------------

UPDATE cr_cases c
SET acts = (
    SELECT array_agg(DISTINCT COALESCE(m.survivor_id, elem))
    FROM unnest(c.acts) AS elem
    LEFT JOIN _act_id_map m ON m.duplicate_id = elem
)
WHERE c.acts && (SELECT COALESCE(array_agg(duplicate_id), '{}') FROM _act_id_map);

DELETE FROM cr_acts a USING _act_id_map m WHERE a.act_id = m.duplicate_id;

-- ---------------------------------------------------------------------
-- 5. Recreate cr_acts's unique constraint as NULLS NOT DISTINCT.
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
