"""
Legal Citator & Treatment Recompute Engine
------------------------------------------
This module handles Steps 3 & 4 of the Enterprise Architecture:
1. Citation Extraction: Scans judgment text / case notes for citations to other cases
   (SCC, AIR, SCR, Neutral Citations, Criminal/Civil Appeals, SLPs).
2. Treatment Classification: Classifies the treatment applied to the cited case
   (OVERRULED, DOUBTED, DISTINGUISHED, FOLLOWED, REFERRED).
3. Treatment Recompute Job: Nightly / batch graph traversal that computes and updates
   the derived 'treatment_status' flag on every case in PostgreSQL.
"""

import re
from typing import List, Dict, Any, Optional, Tuple


# ============================================================
# CITATION PATTERNS (Indian Legal Reporters)
# ============================================================

CITATION_REGEXES = [
    # Neutral Citations: e.g. 2024 INSC 123, 2023:DHC:4567
    {
        "reporter": "NEUTRAL",
        "pattern": re.compile(r"\b([12][90]\d{2})\s*(?:INSC|:\s*[A-Z]{2,4}\s*:)\s*(\d+)\b", re.IGNORECASE)
    },
    # Standard Reporters: (2024) 1 SCC 234, AIR 2023 SC 567, 2022 (3) SCR 45
    {
        "reporter": "LAW_REPORTER",
        "pattern": re.compile(
            r"(?:\(?([12][90]\d{2})\)?\s*)?(?:(\d+)\s+)?(SCC|AIR|SCR|SCALE|ILR|DLT|Cri\s*LJ|CrLJ|SCC\s*OnLine\s*SC)\s+(?:([A-Z]{2,4})\s+)?(\d+)",
            re.IGNORECASE
        )
    },
    # Appeal / SLP Citations: Criminal Appeal No. 1234 of 2024, SLP(Crl) No. 5678 of 2023
    {
        "reporter": "CASE_NUMBER",
        "pattern": re.compile(
            r"\b(Criminal\s+Appeal|Civil\s+Appeal|SLP\s*\((?:Crl|C|Civil)\)|Writ\s+Petition\s*\((?:Crl|C)\))\s+(?:No[s\.]*|Number)\s*([0-9\-\/]+)\s+of\s+([12][90]\d{2})\b",
            re.IGNORECASE
        )
    }
]


# ============================================================
# TREATMENT CLASSIFICATION PATTERNS
# ============================================================

TREATMENT_PATTERNS = [
    (
        "OVERRULED",
        re.compile(
            r"\b(overruled?|overruling|set\s+aside|quashed|no\s+longer\s+good\s+law|per\s+incuriam|bad\s+in\s+law|stands\s+overruled|is\s+hereby\s+overruled|cannot\s+be\s+sustained)\b",
            re.IGNORECASE
        ),
        100
    ),
    (
        "DOUBTED",
        re.compile(
            r"\b(doubted|questioned|hesitate\s+to\s+follow|requires\s+reconsideration|referred\s+to\s+(?:a\s+)?larger\s+bench|correctness\s+of.*doubted)\b",
            re.IGNORECASE
        ),
        80
    ),
    (
        "DISTINGUISHED",
        re.compile(
            r"\b(distinguished|inapplicable|distinguishable|not\s+applicable\s+to\s+the\s+facts|rendered\s+in\s+different\s+context)\b",
            re.IGNORECASE
        ),
        60
    ),
    (
        "FOLLOWED",
        re.compile(
            r"\b(followed|affirmed|approved|relied\s+upon|reiterated|concurred\s+with|in\s+agreement\s+with|settled\s+law|upheld)\b",
            re.IGNORECASE
        ),
        40
    )
]


def extract_citations_from_text(text: str) -> List[Dict[str, Any]]:
    """
    Extracts all legal citations from judgment text or case notes and classifies treatment.
    """
    if not text:
        return []

    citations: List[Dict[str, Any]] = []
    seen_citations = set()

    for regex_def in CITATION_REGEXES:
        for match in regex_def["pattern"].finditer(text):
            full_raw = match.group(0).strip()
            if full_raw in seen_citations or len(full_raw) < 5:
                continue
            seen_citations.add(full_raw)

            # Get surrounding context (150 chars before and after) to classify treatment
            start_pos = max(0, match.start() - 150)
            end_pos = min(len(text), match.end() + 150)
            context_snippet = text[start_pos:end_pos].replace("\n", " ").strip()

            # Classify treatment sentiment in the context window
            treatment_type = "REFERRED"
            confidence = 50.0

            for t_label, t_regex, t_weight in TREATMENT_PATTERNS:
                if t_regex.search(context_snippet):
                    treatment_type = t_label
                    confidence = float(t_weight)
                    break

            citations.append({
                "raw_citation_text": full_raw,
                "reporter_type": regex_def["reporter"],
                "treatment_type": treatment_type,
                "confidence_score": confidence,
                "context_snippet": context_snippet
            })

    return citations


# ============================================================
# TREATMENT STATUS GRAPH RECOMPUTE
# ============================================================

def recompute_all_treatment_statuses(conn) -> Dict[str, int]:
    """
    Nightly / Batch job:
    Walks through all citations in the database and computes the derived 'treatment_status'
    for every case:
      - If cited with 'OVERRULED' by a subsequent case -> 'OVERRULED'
      - Else if cited with 'DOUBTED' -> 'DOUBTED'
      - Else if cited with 'DISTINGUISHED' -> 'DISTINGUISHED'
      - Default -> 'GOOD_LAW'
    """
    stats = {"GOOD_LAW": 0, "DOUBTED": 0, "OVERRULED": 0, "DISTINGUISHED": 0, "TOTAL_UPDATED": 0}
    
    with conn.cursor() as cur:
        # 1. Reset cases without incoming negative treatment to GOOD_LAW
        cur.execute("""
            UPDATE cases
            SET treatment_status = 'GOOD_LAW',
                overruled = FALSE
            WHERE id NOT IN (
                SELECT DISTINCT cited_case_id 
                FROM citations 
                WHERE cited_case_id IS NOT NULL 
                  AND treatment_type IN ('OVERRULED', 'DOUBTED', 'DISTINGUISHED')
            );
        """)

        # 2. Update OVERRULED cases
        cur.execute("""
            UPDATE cases
            SET treatment_status = 'OVERRULED',
                overruled = TRUE
            WHERE id IN (
                SELECT DISTINCT cited_case_id 
                FROM citations 
                WHERE cited_case_id IS NOT NULL 
                  AND treatment_type = 'OVERRULED'
            );
        """)
        stats["OVERRULED"] = cur.rowcount

        # 3. Update DOUBTED cases (that are not already overruled)
        cur.execute("""
            UPDATE cases
            SET treatment_status = 'DOUBTED'
            WHERE treatment_status != 'OVERRULED'
              AND id IN (
                SELECT DISTINCT cited_case_id 
                FROM citations 
                WHERE cited_case_id IS NOT NULL 
                  AND treatment_type = 'DOUBTED'
            );
        """)
        stats["DOUBTED"] = cur.rowcount

        # 4. Update DISTINGUISHED cases
        cur.execute("""
            UPDATE cases
            SET treatment_status = 'DISTINGUISHED'
            WHERE treatment_status NOT IN ('OVERRULED', 'DOUBTED')
              AND id IN (
                SELECT DISTINCT cited_case_id 
                FROM citations 
                WHERE cited_case_id IS NOT NULL 
                  AND treatment_type = 'DISTINGUISHED'
            );
        """)
        stats["DISTINGUISHED"] = cur.rowcount

        # Count Good Law cases
        cur.execute("SELECT COUNT(*) FROM cases WHERE treatment_status = 'GOOD_LAW';")
        stats["GOOD_LAW"] = cur.fetchone()[0]
        stats["TOTAL_UPDATED"] = stats["GOOD_LAW"] + stats["DOUBTED"] + stats["OVERRULED"] + stats["DISTINGUISHED"]

        conn.commit()

    return stats
