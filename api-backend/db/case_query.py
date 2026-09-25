"""
Shared `cr_case_search_view` WHERE-clause builder.

Used by both QUERY /api/cases (routers/search_router.py, the results list)
and QUERY /api/cases/filters (routers/filter_router.py, the facet counts) so
a filter's option counts are always scoped to the exact same search the
results list runs — not a second, hand-maintained copy of this logic that
could drift out of sync with it.
"""

from typing import Any, Dict, List, Optional, Tuple


def parse_search_request(req) -> Dict[str, Any]:
    """Pulls the text-query/filters/date fields off a QUERY body (SearchRequestModel
    or any other Pydantic model shaped the same way for `query`/`filters`/`date`)."""
    text_query = all_terms = any_terms = exact_phrase = none_terms = None
    if isinstance(req.query, str):
        text_query = req.query
    elif req.query is not None:
        text_query, all_terms, any_terms, exact_phrase, none_terms = (
            req.query.text, req.query.all, req.query.any, req.query.exact, req.query.none,
        )

    return {
        "text_query": text_query,
        "all_terms": all_terms,
        "any_terms": any_terms,
        "exact_phrase": exact_phrase,
        "none_terms": none_terms,
        "filters_dict": req.filters,
        "from_date": req.date.from_date if req.date else None,
        "to_date": req.date.to_date if req.date else None,
    }


def build_case_where(
    text_query: Optional[str] = None,
    all_terms: Optional[List[str]] = None,
    any_terms: Optional[List[str]] = None,
    exact_phrase: Optional[str] = None,
    none_terms: Optional[List[str]] = None,
    filters_dict: Optional[Dict[str, Any]] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    exclude_filter_keys: Optional[set] = None,
) -> Tuple[str, List[Any]]:
    """Builds a `WHERE ...` clause (or "") + its params against `cr_case_search_view v`.

    `exclude_filter_keys` skips those keys out of `filters_dict` while building
    the clause — used by filter_router.py so a facet's own selected values don't
    narrow its own option counts (selecting one court shouldn't hide every other
    court from the court filter; it should still narrow judge/act/year counts).
    """
    where_clauses: List[str] = []
    params: List[Any] = []
    exclude_filter_keys = exclude_filter_keys or set()

    # 1. Free text — tsvector for prose fields, ILIKE for structured identifiers.
    if text_query and text_query.strip():
        tsquery_str = text_query.strip()
        ilike_str = f"%{tsquery_str}%"
        where_clauses.append("""(
            v.search_vector @@ plainto_tsquery('english', %s)
            OR v.case_number ILIKE %s
            OR v.neutral_citation ILIKE %s
        )""")
        params.extend([tsquery_str, ilike_str, ilike_str])

    if exact_phrase and exact_phrase.strip():
        where_clauses.append("v.search_vector @@ phraseto_tsquery('english', %s)")
        params.append(exact_phrase.strip())

    if all_terms:
        terms = [t.strip() for t in all_terms if t and t.strip()]
        if terms:
            where_clauses.append("v.search_vector @@ plainto_tsquery('english', %s)")
            params.append(" ".join(terms))

    if any_terms:
        any_clauses = []
        for term in any_terms:
            if term and term.strip():
                any_clauses.append("v.search_vector @@ plainto_tsquery('english', %s)")
                params.append(term.strip())
        if any_clauses:
            where_clauses.append("(" + " OR ".join(any_clauses) + ")")

    if none_terms:
        for term in none_terms:
            if term and term.strip():
                where_clauses.append("NOT (v.search_vector @@ plainto_tsquery('english', %s))")
                params.append(term.strip())

    # 2. Filters
    if filters_dict:
        for f_key, f_val in filters_dict.items():
            if f_key in exclude_filter_keys:
                continue
            if f_val is None or f_val == "" or (isinstance(f_val, list) and len(f_val) == 0):
                continue
            val_list = f_val if isinstance(f_val, list) else [f_val]

            if f_key in ("court", "court_id"):
                int_ids = [int(v) for v in val_list if str(v).isdigit()]
                if int_ids:
                    where_clauses.append("v.court_id = ANY(%s)")
                    params.append(int_ids)

            elif f_key in ("judgment_year", "year"):
                int_years = [int(y) for y in val_list if str(y).isdigit()]
                if int_years:
                    where_clauses.append("EXTRACT(YEAR FROM v.judgment_date)::INT = ANY(%s)")
                    params.append(int_years)

            elif f_key in ("judge", "judges"):
                judge_likes = [f"%{str(j).strip()}%" for j in val_list if str(j).strip()]
                if judge_likes:
                    where_clauses.append("EXISTS (SELECT 1 FROM unnest(COALESCE(v.bench_names, ARRAY[]::text[])) bn WHERE bn ILIKE ANY(%s))")
                    params.append(judge_likes)

            elif f_key in ("act", "acts"):
                act_likes = [f"%{str(a).strip()}%" for a in val_list if str(a).strip()]
                if act_likes:
                    where_clauses.append("EXISTS (SELECT 1 FROM unnest(COALESCE(v.act_names, ARRAY[]::text[])) an WHERE an ILIKE ANY(%s))")
                    params.append(act_likes)

            elif f_key == "disposition":
                # v.disposition is disposition_category_enum, not text -- ANY()
                # against a plain text[] param fails with "operator does not
                # exist" without this cast (found via a real run).
                where_clauses.append("v.disposition::text = ANY(%s)")
                params.append(val_list)

            elif f_key == "favouring_party":
                # Same enum-cast reasoning as disposition above.
                where_clauses.append("v.favouring_party::text = ANY(%s)")
                params.append(val_list)

            elif f_key in ("industry", "industries"):
                industry_likes = [f"%{str(i).strip()}%" for i in val_list if str(i).strip()]
                if industry_likes:
                    where_clauses.append("EXISTS (SELECT 1 FROM unnest(COALESCE(v.industry_names, ARRAY[]::text[])) ind WHERE ind ILIKE ANY(%s))")
                    params.append(industry_likes)

            elif f_key in ("ministry", "ministries"):
                ministry_likes = [f"%{str(m).strip()}%" for m in val_list if str(m).strip()]
                if ministry_likes:
                    where_clauses.append("EXISTS (SELECT 1 FROM unnest(COALESCE(v.ministry_names, ARRAY[]::text[])) mn WHERE mn ILIKE ANY(%s))")
                    params.append(ministry_likes)

    # 3. Date range
    if from_date:
        where_clauses.append("v.judgment_date >= %s")
        params.append(from_date)
    if to_date:
        where_clauses.append("v.judgment_date <= %s")
        params.append(to_date)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    return where_sql, params
