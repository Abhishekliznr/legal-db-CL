-- =====================================================================
-- API-BACKEND-V2-ONLY SUPPLEMENT: cr_case_search_view
--
-- Split out (rather than only living in schema.sql, where it started) so
-- it can be applied on its own via `python -m db.init_db ensure-view` —
-- the shared-DB deployment shape, where scraper-backend owns `init` but
-- has no equivalent view (it never reads its own writes). CREATE OR
-- REPLACE VIEW is naturally idempotent, so this file is safe to re-run
-- with no IF NOT EXISTS needed.
--
-- Kept in sync with the identical view definition inside schema.sql (used
-- by the standalone `init` path) — a change to one belongs in both, same
-- as every other "kept in sync by hand" pair in this codebase.
-- =====================================================================

CREATE OR REPLACE VIEW cr_case_search_view AS
SELECT
    c.case_id,
    c.liznr_id,
    c.case_number,
    c.petitioner,
    c.respondent,
    c.court_id,
    crt.court_name,
    c.judgment_date,
    c.language,
    c.neutral_citation,
    c.disposition,
    c.document_type,
    c.case_note,
    c.conclusion,
    c.judgement,
    c.ocr_text,
    -- blob_pdf_id and source_pdf_url are exposed separately, NOT merged
    -- into one "pdf_url" (that COALESCE existed here briefly and was wrong
    -- as soon as blob_pdf_id stopped being a full URL, 2026-09-08): the
    -- two aren't the same shape of value anymore -- blob_pdf_id is a bare
    -- path within the blob container (e.g. "SCIN/<checksum>.pdf"), and
    -- constructing a working link from it (account + container base URL +
    -- this path) is now deliberately a frontend concern, not something
    -- this service computes or stores. source_pdf_url is already a
    -- complete, directly-usable URL (the court's own site).
    c.blob_pdf_id,
    c.source_pdf_url,
    c.needs_review,
    c.search_vector,
    jb.full_name AS judgment_by_name,
    subj.subject_name,
    (SELECT array_agg(j.full_name ORDER BY j.full_name)
       FROM cr_judges j WHERE j.judge_id = ANY(c.bench)) AS bench_names,
    (SELECT array_agg(DISTINCT a.act_name)
       FROM cr_acts a WHERE a.act_id = ANY(c.acts)) AS act_names,
    (SELECT array_agg(DISTINCT cc.category_name)
       FROM cr_case_categories cc WHERE cc.category_id = ANY(c.case_category)) AS category_names,
    (SELECT array_agg(DISTINCT m.ministry_name)
       FROM cr_ministries m WHERE m.ministry_id = ANY(c.ministries)) AS ministry_names
FROM cr_cases c
JOIN cr_courts crt ON crt.court_id = c.court_id
LEFT JOIN cr_judges jb ON jb.judge_id = c.judgment_by
LEFT JOIN cr_subjects subj ON subj.subject_id = c.subject;
