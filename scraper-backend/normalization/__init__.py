"""
Normalization helpers — act alias resolution (acts.py), judge name cleaning
(judges.py), party name cleaning (parties.py). Used by pipeline/promotion.py
after the adapter's raw table-cell fields (RawJudgmentRecord.judge_raw/
party_name_raw) have already been split up by pipeline/regex_extraction.py;
this package only cleans up the strings before they become rows in
judges/parties/statutes. acts.py is also used by pipeline/llm_enrichment.py
for the provisions it resolves.
"""
