"""
Manual interop end-to-end check — NOT a pytest suite, run directly.

Rewritten 2026-09-08 for the flattened `cases` schema (db/schema.sql's
rewrite note). The real point of this test is unchanged from the original
Phase 4 version: api-backend is a completely separate, standalone
codebase from scraper-backend (no shared code), yet both read/write the
same Postgres database in a shared-DB deployment. This proves they
genuinely interoperate — data promoted by scraper-backend's real
pipeline is queryable through api-backend's independently-written
routers, with zero shared code between the two services.

What changed from the original: the old version depended on
scraper-backend/tests/populate_for_phase4.py, which populated the old
documents/cases/parties/document_coram/citations schema (a synthetic
3-court, cross-citation "who overruled whom" scenario) — both that script
and this test's assertions are gone, since neither the schema nor the
citation-tracking feature they exercised exists anymore (see
routers/search_router.py's module docstring on what was permanently
dropped: citations/treatment_status, advocates, holdings, timeline, prior
appellate history). This version instead depends on
scraper-backend/tests/manual_promotion_e2e.py, which pushes 3 REAL
Supreme Court judgment PDFs (not synthetic fixtures) through the actual
OCR -> regex-extraction -> promotion pipeline — already verified standalone
when it was written; this test adds the api-backend read side on top of
the exact same known data, so its assertions are pinned to that script's
real, already-confirmed output (see its own docstring for the 3 case
fixtures) rather than to a synthetic scenario built for this test.

Run against a real Postgres with BOTH services' schemas applied to the same
database — scraper-backend owns `init`, api-backend adds its own
supplements on top:

    export DATABASE_TYPE=postgres DB_HOST=localhost DB_PORT=5432 \\
           DB_NAME=<db> DB_USER=<user> DB_PASSWORD=<password>

    # from scraper-backend/:
    python3 -m db.init_db init
    python3 tests/manual_promotion_e2e.py

    # from api-backend/:
    python3 -m db.init_db ensure-supplement
    python3 -m db.init_db ensure-filters
    python3 -m db.init_db ensure-view
    python3 -m db.seed_filters
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
        # manual_promotion_e2e.py seeds exactly one court (Supreme Court of
        # India) and promotes 3 real cases under it -- 5 distinct judges
        # across their combined bench+judgment_by (Hemant Gupta, S.
        # Ravindra Bhat, C.T. Ravikumar, J.B. Pardiwala, R. Mahadevan), 4
        # distinct acts (IPC, Telangana Tenancy Act, Punjab Land
        # Preservation Act, CrPC) -- all confirmed against the real run
        # this test's assertions are pinned to.
        assert r.json()["judgments"] == 3
        assert r.json()["courts"] == 1
        assert r.json()["judges"] == 5
        assert r.json()["acts"] == 4

        print("\n=== GET /api/cases/filters ===")
        r = client.get("/api/cases/filters")
        body = r.json()
        assert r.status_code == 200
        filters_by_key = {f["key"]: f for f in body["filters"]}
        # treatment_status is gone entirely -- no citations table to compute
        # it from anymore (see routers/filter_router.py's module docstring).
        assert "treatment_status" not in filters_by_key
        assert set(filters_by_key.keys()) == {"court", "judge", "act", "judgment_year"}

        court_options = {o["label"]: o["count"] for o in filters_by_key["court"]["options"]}
        print("court facet:", court_options)
        assert court_options.get("Supreme Court of India") == 3

        judge_options = {o["label"]: o["count"] for o in filters_by_key["judge"]["options"]}
        print("judge facet:", judge_options)
        assert judge_options.get("HEMANT GUPTA") == 1
        assert judge_options.get("C.T. RAVIKUMAR") == 1
        assert len(judge_options) == 5

        act_options = {o["label"]: o["count"] for o in filters_by_key["act"]["options"]}
        print("act facet:", act_options)
        assert act_options.get("Indian Penal Code, 1860") == 2  # the two criminal appeals both cite IPC sections
        assert act_options.get("Code of Criminal Procedure, 1973") == 1
        assert act_options.get("Telangana Tenancy Act") == 1
        assert act_options.get("Punjab Land Preservation Act") == 1

        year_options = {o["label"]: o["count"] for o in filters_by_key["judgment_year"]["options"]}
        print("judgment_year facet:", year_options)
        assert year_options.get("2025") == 2
        assert year_options.get("2015") == 1

        print("\n=== QUERY /api/cases (free text) ===")
        r = client.request("QUERY", "/api/cases", json={"query": "Bilaspur", "page": 1, "limit": 20})
        body = r.json()
        print(r.status_code, "total:", body.get("total"), "| cases:", [x["case_number"] for x in body["results"]])
        assert r.status_code == 200
        assert body["total"] == 1
        assert body["results"][0]["case_number"] == "Criminal Appeal No. 1730 of 2015"

        print("\n=== QUERY /api/cases (filter by act, ILIKE substring match) ===")
        r = client.request("QUERY", "/api/cases", json={"filters": {"act": ["Criminal Procedure"]}, "page": 1, "limit": 20})
        body = r.json()
        print(r.status_code, "total:", body.get("total"), "| case:", body["results"][0]["case_number"] if body.get("results") else None)
        assert body["total"] == 1
        assert body["results"][0]["case_number"] == "Criminal Appeal No. 11 of 2025"

        print("\n=== QUERY /api/cases (filter by judge) ===")
        r = client.request("QUERY", "/api/cases", json={"filters": {"judge": ["HEMANT GUPTA"]}, "page": 1, "limit": 20})
        body = r.json()
        print(r.status_code, "total:", body.get("total"))
        assert body["total"] == 1
        assert body["results"][0]["case_number"] == "Criminal Appeal No. 1730 of 2015"

        print("\n=== QUERY /api/cases (filter by disposition) ===")
        r = client.request("QUERY", "/api/cases", json={"filters": {"disposition": ["Quashed"]}, "page": 1, "limit": 20})
        body = r.json()
        print(r.status_code, "total:", body.get("total"))
        assert body["total"] == 1
        assert body["results"][0]["case_number"] == "Criminal Appeal No. 11 of 2025"

        print("\n=== GET /api/cases/{case_id} (Pardeshiram case: full detail) ===")
        r = client.request("QUERY", "/api/cases", json={"query": "Bilaspur", "page": 1, "limit": 1})
        pardeshiram_id = r.json()["results"][0]["id"]
        r = client.get(f"/api/cases/{pardeshiram_id}")
        detail = r.json()
        print(r.status_code, detail)
        assert r.status_code == 200
        assert detail["disposition"] == "Partly Allowed"
        assert detail["subject"] == "Criminal"
        assert detail["case_category"] == ["Criminal Appeal"]
        assert {p["name"] for p in detail["parties"]} == {"PARDESHIRAM", "STATE OF M.P. (NOW CHHATTISGARH)"}
        assert {j["name"] for j in detail["judges"]} == {"HEMANT GUPTA", "S. RAVINDRA BHAT"}
        section_numbers = {p["section"] for p in detail["provisions"] if p["act_name"] == "Indian Penal Code, 1860"}
        assert {"302", "300", "304"} <= section_numbers
        assert detail["ministries"] == []
        assert detail["needs_review"] is False
        assert detail["ocr_text"] and "PARDESHIRAM" in detail["ocr_text"]

        print("\n=== GET /api/cases/{case_id} (Ministry of Railways case: ministries resolved) ===")
        r = client.request("QUERY", "/api/cases", json={"query": "quashed", "page": 1, "limit": 1})
        railways_id = r.json()["results"][0]["id"]
        r = client.get(f"/api/cases/{railways_id}")
        detail = r.json()
        print(r.status_code, detail)
        assert detail["disposition"] == "Quashed"
        assert detail["ministries"] == ["Ministry of Railways"]
        assert detail["neutral_citation"] == "2025 INSC 11"

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
