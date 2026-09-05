"""
Post-LLM normalization helpers — act alias resolution (acts.py), judge name
cleaning (judges.py), party name cleaning (parties.py). Used by
pipeline/promotion.py after pipeline/extraction.py's LLM call has already
identified who/what is in a judgment; this package only cleans up the
strings before they become rows in judges/parties/statutes.
"""
