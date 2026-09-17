"""
Normalization helpers — act alias resolution (acts.py), judge name cleaning
(judges.py), party name cleaning (parties.py). Used by adapters/supreme_court/promotion.py
after the adapter's raw table-cell fields (RawJudgmentRecord.judge_raw/
party_name_raw) have already been split up by adapters/supreme_court/extraction.py;
this package only cleans up the strings before they become rows in
judges/parties/statutes. acts.py is also used by pipeline/llm_enrichment.py
for the provisions it resolves.
"""
