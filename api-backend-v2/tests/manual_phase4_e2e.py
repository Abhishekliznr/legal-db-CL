"""
Manual Phase 4 end-to-end check — NOT a pytest suite, run directly.

The real point of this test: api-backend-v2 was built as a completely
separate, standalone codebase from scraper-backend-v2 (spec's "no shared
code" decision), yet both read/write the same Postgres schema. This proves
they actually interoperate — data promoted by scraper-backend-v2's real
pipeline (tests/populate_for_phase4.py, run first) is genuinely queryable
through api-backend-v2's independently-written routers: filters compute
correct live counts, full-text search finds the right documents, case
detail assembles provisions/citations/coram correctly, and the computed
treatment_status (Overruled via the citations graph) reads back correctly
with zero shared code between the two services.

Run after `python3 -m tests.populate_for_phase4` (from scraper-backend-v2)
and `python3 -m db.seed_filters` (from here) against the same DB:

    export DATABASE_TYPE=postgres DB_HOST=localhost DB_PORT=5432 \\
           DB_NAME=liznrlegal DB_USER=postgres DB_PASSWORD=testpass
    python3 -m tests.manual_phase4_e2e
"""

from fastapi.testclient import TestClient

import api


def main():
    with TestClient(api.app) as client:
        print("=== /health ===")
        r = client.get("/health")
        print(r.status_code, r.json())
        assert r.status_code == 200 and r.json()["status"] == "healthy"

        print("\n=== GET /api/cases/stats ===")
        r = client.get("/api/cases/stats")
        print(r.status_code, r.json())
        assert r.status_code == 200
        assert r.json()["judgments"] == 3
        assert r.json()["courts"] == 3
        assert r.json()["acts"] == 3  # Arbitration Act, CrPC, CPC

        print("\n=== GET /api/cases/filters ===")
        r = client.get("/api/cases/filters")
        body = r.json()
        assert r.status_code == 200
        filters_by_key = {f["key"]: f for f in body["filters"]}

        court_options = {o["label"]: o["count"] for o in filters_by_key["court"]["options"]}
        print("court facet:", court_options)
        assert court_options.get("Supreme Court of India") == 1
        assert court_options.get("Delhi High Court") == 1
        assert court_options.get("Bombay High Court") == 1

        judge_options = {o["label"]: o["count"] for o in filters_by_key["judge"]["options"]}
        print("judge facet:", judge_options)
        assert judge_options.get("B.V. NAGARATHNA") == 2  # authored SC case, sat on Delhi HC bench

        act_options = {o["label"] for o in filters_by_key["act"]["options"]}
        print("act facet:", act_options)
        assert "Arbitration and Conciliation Act, 1996" in act_options  # resolved to canonical name, not raw "Arbitration and Conciliation Act"
        assert "Code of Criminal Procedure, 1973" in act_options

        treatment_options = {o["label"]: o["count"] for o in filters_by_key["treatment_status"]["options"]}
        print("treatment_status facet:", treatment_options)
        assert treatment_options.get("Overruled") == 1  # the Bombay HC case cited "Prior Partition Case" with treatment=Overruled
        assert treatment_options.get("Good Law (Affirmed)") == 2

        print("\n=== QUERY /api/cases (free text) ===")
        # Matches 2: the Supreme Court case's own note, AND the Delhi HC case
        # note, which mentions "the Supreme Court's arbitration ruling" as
        # part of setting up the overrule scenario above — both genuinely
        # contain the word, this isn't a false positive.
        r = client.request("QUERY", "/api/cases", json={"query": "arbitration", "page": 1, "limit": 20})
        body = r.json()
        print(r.status_code, "total:", body.get("total"), "| courts:", [x["court_name"] for x in body["results"]])
        assert r.status_code == 200
        assert body["total"] == 2
        assert {r["court_name"] for r in body["results"]} == {"Supreme Court of India", "Delhi High Court"}

        print("\n=== QUERY /api/cases (filter by act) ===")
        r = client.request("QUERY", "/api/cases", json={"filters": {"act": ["Criminal Procedure"]}, "page": 1, "limit": 20})
        body = r.json()
        print(r.status_code, "total:", body.get("total"), "| court:", body["results"][0]["court_name"] if body.get("results") else None)
        assert body["total"] == 1
        assert body["results"][0]["court_name"] == "Delhi High Court"

        print("\n=== QUERY /api/cases (filter by judge, matches 2 courts) ===")
        r = client.request("QUERY", "/api/cases", json={"filters": {"judge": ["Nagarathna"]}, "page": 1, "limit": 20})
        body = r.json()
        print(r.status_code, "total:", body.get("total"))
        assert body["total"] == 2

        print("\n=== QUERY /api/cases (filter by treatment_status=OVERRULED) ===")
        # The Supreme Court case is OVERRULED here — the Delhi HC case cites
        # it (by neutral_citation) with treatment=Overruled, and
        # reconcile_citations() resolved that edge to this in-corpus
        # document (tests/populate_for_phase4.py's overrule scenario).
        r = client.request("QUERY", "/api/cases", json={"filters": {"treatment_status": ["OVERRULED"]}, "page": 1, "limit": 20})
        body = r.json()
        print(r.status_code, "total:", body.get("total"), "| case:", body["results"][0]["case_number"] if body.get("results") else None)
        assert body["total"] == 1
        assert body["results"][0]["case_number"] == "C.A. No.-100 - 2026"

        print("\n=== GET /api/cases/{case_id} (Supreme Court case: own detail + who overruled it) ===")
        sc_case_id = body["results"][0]["id"]
        r = client.get(f"/api/cases/{sc_case_id}")
        detail = r.json()
        print(r.status_code, detail)
        assert r.status_code == 200
        assert detail["disposition_category"] == "Dismissed"
        assert detail["treatment_status"] == "OVERRULED"
        assert len(detail["provisions"]) == 1 and detail["provisions"][0]["section"] == "34"
        assert len(detail["parties"]) == 2
        # cited_by: the resolved reverse edge — Delhi HC's case shows up as
        # having overruled this one, with a real document_id (not NULL),
        # proving reconcile_citations() actually linked the two documents.
        assert len(detail["cited_by"]) == 1
        assert detail["cited_by"][0]["treatment"] == "Overruled"
        assert detail["cited_by"][0]["document_id"] is not None
        assert detail["cited_by"][0]["case_number"] == "Crl.A. No.-200 - 2026"

        print("\n=== GET /api/cases/{case_id} (Bombay case: citation to an out-of-corpus case) ===")
        r = client.request("QUERY", "/api/cases", json={"query": "partition", "page": 1, "limit": 20})
        bombay_case_id = r.json()["results"][0]["id"]
        r = client.get(f"/api/cases/{bombay_case_id}")
        detail = r.json()
        print(r.status_code, detail)
        assert detail["disposition_category"] == "Set Aside"
        assert detail["treatment_status"] == "GOOD_LAW"  # this case's own status, not what it did to "Prior Partition Case"
        assert len(detail["provisions"]) == 1 and detail["provisions"][0]["section"] == "96"
        # citations_made: cites a case that was never itself promoted into
        # this corpus, so cited_document_id correctly stays unresolved (None) —
        # not a bug, reconcile_citations() only links to documents that exist.
        assert len(detail["citations_made"]) == 1 and detail["citations_made"][0]["treatment"] == "Overruled"
        assert detail["citations_made"][0]["cited_document_id"] is None
        assert len(detail["parties"]) == 2

        print("\n=== POST + GET /api/cases/history ===")
        r = client.post("/api/cases/history", json={"user_id": "test-user-1", "query": "arbitration act"})
        print("POST:", r.status_code)
        assert r.status_code == 204
        r = client.get("/api/cases/history", params={"user_id": "test-user-1"})
        print("GET:", r.status_code, r.json())
        assert r.status_code == 200
        assert r.json()["items"][0]["query"] == "arbitration act"

        print("\n=== GET /api/cases/searches ===")
        r = client.get("/api/cases/searches")
        print(r.status_code, [f["key"] for f in r.json()["fields"]])
        assert r.status_code == 200
        assert len(r.json()["fields"]) == 5

    print("\nALL PHASE 4 INTEROP CHECKS PASSED")


if __name__ == "__main__":
    main()
