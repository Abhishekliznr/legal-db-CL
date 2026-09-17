"""
get-or-create lookup helpers over cr_cases' array-column lookup tables
(judges, acts, sections, rules, orders, subjects, ministries, industries,
case categories) — schema-wide, not specific to any one court's promotion
pipeline. Shared by every court's own promotion.py and by
pipeline/llm_enrichment.py (both need the same lookup tables).

Array columns on `cr_cases` (bench, sections, acts, rules, orders,
ministries, case_category) mean Postgres can't FK-constrain membership the
way normalized junction tables would — these helpers are where that
integrity actually gets enforced instead.

Each does INSERT ... ON CONFLICT (<natural key>) DO NOTHING RETURNING <id>,
falling back to a SELECT only when nothing came back (another concurrent
promotion won the race). A plain SELECT-then-INSERT lets two concurrent
promotions both pass the SELECT before either INSERTs, so the loser's
INSERT throws an unhandled UniqueViolation; INSERT ... ON CONFLICT takes a
row lock on the conflicting key, so the loser blocks until the winner
commits and then safely reads back the winner's row instead.
"""

from typing import Any, Dict, List, Optional, Tuple

from normalization import case_numbers


def get_or_create_judge(cur, cleaned_name: str) -> int:
    cur.execute(
        "INSERT INTO cr_judges (full_name, normalized_name) VALUES (%s, %s) ON CONFLICT (normalized_name) DO NOTHING RETURNING judge_id;",
        (cleaned_name, cleaned_name),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT judge_id FROM cr_judges WHERE normalized_name = %s;", (cleaned_name,))
    return cur.fetchone()[0]


def get_or_create_act(cur, act_name: str, short_code: Optional[str], act_year: Optional[int]) -> int:
    cur.execute(
        "INSERT INTO cr_acts (act_name, act_year, short_code) VALUES (%s, %s, %s) ON CONFLICT (act_name, act_year) DO NOTHING RETURNING act_id;",
        (act_name, act_year, short_code),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "SELECT act_id FROM cr_acts WHERE act_name = %s AND (act_year = %s OR (act_year IS NULL AND %s IS NULL));",
        (act_name, act_year, act_year),
    )
    return cur.fetchone()[0]


def get_or_create_section(cur, act_id: int, section_number: str) -> int:
    cur.execute(
        "INSERT INTO cr_sections (act_id, section_number) VALUES (%s, %s) ON CONFLICT (act_id, section_number) DO NOTHING RETURNING section_id;",
        (act_id, section_number),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT section_id FROM cr_sections WHERE act_id = %s AND section_number = %s;", (act_id, section_number))
    return cur.fetchone()[0]


def get_or_create_rule(cur, act_id: int, rule_number: str) -> int:
    cur.execute(
        "INSERT INTO cr_rules (act_id, rule_number) VALUES (%s, %s) ON CONFLICT (act_id, rule_number) DO NOTHING RETURNING rule_id;",
        (act_id, rule_number),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT rule_id FROM cr_rules WHERE act_id = %s AND rule_number = %s;", (act_id, rule_number))
    return cur.fetchone()[0]


def get_or_create_order(cur, act_id: int, order_number: str) -> int:
    cur.execute(
        "INSERT INTO cr_orders (act_id, order_number) VALUES (%s, %s) ON CONFLICT (act_id, order_number) DO NOTHING RETURNING order_id;",
        (act_id, order_number),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT order_id FROM cr_orders WHERE act_id = %s AND order_number = %s;", (act_id, order_number))
    return cur.fetchone()[0]


def get_or_create_subject(cur, subject_name: str) -> int:
    cur.execute(
        "INSERT INTO cr_subjects (subject_name) VALUES (%s) ON CONFLICT (subject_name) DO NOTHING RETURNING subject_id;",
        (subject_name,),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT subject_id FROM cr_subjects WHERE subject_name = %s;", (subject_name,))
    return cur.fetchone()[0]


def get_or_create_ministry(cur, ministry_name: str) -> int:
    cur.execute(
        "INSERT INTO cr_ministries (ministry_name) VALUES (%s) ON CONFLICT (ministry_name) DO NOTHING RETURNING ministry_id;",
        (ministry_name,),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT ministry_id FROM cr_ministries WHERE ministry_name = %s;", (ministry_name,))
    return cur.fetchone()[0]


def get_or_create_advocate(cur, name: str, enrollment_no: str, enrollment_year: Optional[int]) -> int:
    """
    Keyed on enrollment_no (a real, stable identity a source like Madhya
    Pradesh's case-status page provides), not name -- two rows spelling the
    same advocate's name slightly differently but sharing an enrollment_no
    are the same advocate; two different advocates never share one.
    """
    cur.execute(
        "INSERT INTO cr_advocates (advocate_name, enrollment_no, enrollment_year) VALUES (%s, %s, %s) "
        "ON CONFLICT (enrollment_no) DO NOTHING RETURNING advocate_id;",
        (name, enrollment_no, enrollment_year),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT advocate_id FROM cr_advocates WHERE enrollment_no = %s;", (enrollment_no,))
    return cur.fetchone()[0]


def get_or_create_industry(cur, industry_name: str) -> int:
    cur.execute(
        "INSERT INTO cr_industries (industry_name) VALUES (%s) ON CONFLICT (industry_name) DO NOTHING RETURNING industry_id;",
        (industry_name,),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT industry_id FROM cr_industries WHERE industry_name = %s;", (industry_name,))
    return cur.fetchone()[0]


def get_or_create_category(cur, case_number: Optional[str]) -> Optional[int]:
    classified = case_numbers.classify_category(case_number)
    if not classified:
        return None
    code, name = classified
    cur.execute(
        "INSERT INTO cr_case_categories (category_code, category_name) VALUES (%s, %s) ON CONFLICT (category_code) DO NOTHING RETURNING category_id;",
        (code, name),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT category_id FROM cr_case_categories WHERE category_code = %s;", (code,))
    return cur.fetchone()[0]


def resolve_provisions(cur, provisions: List[Dict[str, Any]]) -> Tuple[List[int], List[int], List[int], List[int]]:
    """Returns (act_ids, section_ids, rule_ids, order_ids), each de-duplicated, act_ids covering every act referenced by any of the other three."""
    act_ids: List[int] = []
    section_ids: List[int] = []
    rule_ids: List[int] = []
    order_ids: List[int] = []
    seen_acts = set()

    for provision in provisions:
        act_id = get_or_create_act(cur, provision["statute_name"], provision["short_code"], provision["statute_year"])
        if act_id not in seen_acts:
            seen_acts.add(act_id)
            act_ids.append(act_id)

        number = provision["section_number"]
        provision_type = provision["provision_type"]
        if provision_type == "rule":
            rule_ids.append(get_or_create_rule(cur, act_id, number))
        elif provision_type == "order":
            order_ids.append(get_or_create_order(cur, act_id, number))
        else:
            section_ids.append(get_or_create_section(cur, act_id, number))

    return act_ids, section_ids, rule_ids, order_ids


def resolve_bench(cur, judge_names: List[str]) -> List[int]:
    judge_ids = []
    seen = set()
    for name in judge_names:
        if name in seen:
            continue
        seen.add(name)
        judge_ids.append(get_or_create_judge(cur, name))
    return judge_ids
