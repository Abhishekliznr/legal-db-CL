"""
Manual check for pipeline/regex_extraction.py::find_provision_paragraphs()
(2026-09-09) — NOT a pytest suite, run directly, no DB needed.

Confirms:
1. A provision reference placed in the MIDDLE of a numbered document (where
   llm_enrichment.py's head+tail excerpt would NOT reach it) is still found.
2. A numbered paragraph with no provision reference is excluded.
3. The unnumbered-document fallback (fixed-context windows) works when
   split_into_paragraphs() can't find a reliable numbering run.
4. A document with no provision references anywhere returns "".
5. (2026-09-09) The \b word-boundary fix on _PROVISION_PATTERN's trigger
   group: paragraphs containing ONLY "Rs. 5000"-style rupee amounts or
   "...mental disorder 5 years..."-style prose are NOT flagged as
   provision-bearing (both previously false-matched trigger "s."/"order" as
   substrings of "Rs."/"disorder"), while real citations in the same
   paragraph shape still are.

Run:
    python3 tests/manual_provision_paragraphs_demo.py
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.regex_extraction import find_provision_paragraphs

_FAILURES = []


def _check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        _FAILURES.append(label)


# A synthetic numbered judgment, ~15 paragraphs, with the ONLY provision
# reference sitting at paragraph 8 (dead center) -- outside where
# llm_enrichment.py's ~1200-head/~2000-tail excerpt would ever look given
# this text is short enough that head+tail would actually cover the whole
# thing at this length, so we pad each paragraph to make head+tail's budget
# genuinely miss it.
_PAD = "This paragraph discusses unrelated factual background. " * 40  # ~2280 chars/para

_numbered_paragraphs = []
for i in range(1, 16):
    if i == 8:
        _numbered_paragraphs.append(
            f"{i}. The accused was charged u/s 302 of the Indian Penal Code, 1860 and the trial court framed charges accordingly. {_PAD}"
        )
    else:
        _numbered_paragraphs.append(f"{i}. {_PAD}")
_numbered_doc = "\n".join(_numbered_paragraphs)

result = find_provision_paragraphs(_numbered_doc)
_check("mid-document provision paragraph is found", "[Para 8]" in result)
_check("provision text itself is present in the returned block", "302" in result and "Indian Penal Code" in result)
_check("an unrelated paragraph (e.g. Para 1) is excluded", "[Para 1]" not in result)
_check("an unrelated paragraph (e.g. Para 15) is excluded", "[Para 15]" not in result)

# Unnumbered short "Order"-style text -- split_into_paragraphs() returns []
# for this (fewer than 3 numbered paragraphs), so the fixed-context-window
# fallback must kick in instead.
_unnumbered_doc = (
    "IN THE HIGH COURT OF DELHI\n\n"
    "ORDER\n\n"
    "Heard learned counsel. This application is filed under Order 21 Rule 32 "
    "of the Code of Civil Procedure, 1908 seeking execution of the decree. "
    "List on 14.03.2026 for further orders."
)
result_unnumbered = find_provision_paragraphs(_unnumbered_doc)
_check("unnumbered fallback finds the Order/Rule reference", "Order 21 Rule 32" in result_unnumbered)
_check("unnumbered fallback result is non-empty", bool(result_unnumbered))

# No provision reference anywhere -- cheap short-circuit to "".
_no_provision_doc = "1. The parties appeared through counsel.\n2. Matter adjourned.\n3. List after four weeks."
_check("no-provision document returns empty string", find_provision_paragraphs(_no_provision_doc) == "")

# Empty input.
_check("empty input returns empty string", find_provision_paragraphs("") == "")

# \b word-boundary regression checks (2026-09-09) -- these are real
# sentences that false-triggered before the fix (trigger "s." matched
# inside "Rs.", trigger "order" matched inside "disorder").
_false_positive_docs = [
    "1. The petitioner appeared through counsel.\n2. The compensation was fixed at Rs. 5000 payable within a month.\n3. Matter closed.",
    "1. The petitioner appeared through counsel.\n2. The petitioner suffers from a mental disorder 5 years after the incident.\n3. Matter closed.",
]
for doc in _false_positive_docs:
    _check(f"no false trigger on {doc.splitlines()[1]!r}", find_provision_paragraphs(doc) == "")

# Same shape, but with a real citation instead -- must still be found.
_true_positive_doc = "1. The petitioner appeared through counsel.\n2. The accused was charged u/s 302 of the Indian Penal Code, 1860.\n3. Matter closed."
_check("real citation in the same paragraph shape is still found", "[Para 2]" in find_provision_paragraphs(_true_positive_doc))

print()
if _FAILURES:
    print(f"{len(_FAILURES)} check(s) FAILED: {_FAILURES}")
    sys.exit(1)
print("All checks passed.")
