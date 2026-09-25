# Supreme Court follow-ups

Work already done for Madhya Pradesh High Court that still has to be brought to the
Supreme Court (SCIN) pipeline. MP is the reference implementation for everything below —
read the MP file named in each item before starting it.

All paths are relative to `legal-db/scraper-backend/`.

Rule for this codebase: anything that can differ per court (stage names, messages,
helpers) goes in `adapters/supreme_court/`, not in `orchestrator/` or `pipeline/`.

---

## 1. Stage-based logging

### Already works for SC with no changes

These run in shared code, so SC batches already get them:

- [x] Stage + case tagging on every line from `orchestrator/batch_runner.py`
      (BATCH, INGEST, PROMOTE, PROVISIONS, ENRICH) and `pipeline/ocr.py` (OCR).
- [x] Structured SSE lines, 1,000-line cap, trimmed notice, read positions that
      survive trimming (`orchestrator/live_logs.py`, `routers/scraper_router.py`).
- [x] UI stage column, per-case divider and trimmed note (legal-ui
      `features/admin/components/scraping/scraper-batch-log-viewer.tsx`).
- [x] MP **Phase 2** wording that lives in shared code (OCR, PROVISIONS line,
      ENRICH "Filled / Kept existing" line, multi-line batch summary) already applies
      to SC.

### Needs SC-specific work

- [ ] **Create `adapters/supreme_court/stages.py`** with SC's own scraping stages.
      Suggested: `DISCOVER` (date window + captcha + results table + pagination)
      and `JUDGMENT` (PDF download). Reference: `adapters/high_courts/mp/stages.py`.

- [ ] **Move `adapters/supreme_court/adapter.py` from `logger.*` to `slog()`.**
      Today its 4 log lines only reach stdout, never the live panel:
  - [ ] `_scrape_one_batch` start → DISCOVER: e.g.
        `Searching sci.gov.in judgments 01-01-2025 → 30-01-2025 (window 1 of 12)`.
        Windows come from `_split_into_batches` (max 30 days each).
  - [ ] `_solve_and_submit_captcha` → return the attempt number instead of `bool`
        and log `Captcha solved (attempt N of 20)`. Today it returns `False` for both
        "captcha failed" and "No Records Found" — split these so the panel can say
        `No judgments in this window` vs `Could not solve captcha → window skipped`.
        Reference: MP `ilrs._solve_and_submit_captcha`.
  - [ ] `SCI results headers: [...]` → `debug` (server log only).
  - [ ] Per results page → DISCOVER: `Page N · M judgment rows`.
  - [ ] `PDF download failed` → JUDGMENT warning `PDF download failed → skipped`;
        add a success line `PDF downloaded (N KB)`.
        Reference: MP `adapter._download_pdf`.
  - [ ] Rows with no PDF link are currently skipped silently (`if not pdf_links:
        continue`) — log `No judgment PDF link in row → skipped`.

- [ ] **Case scope + position.** If SC is made resumable first (see "Resume a
      stopped batch" below), `batch_runner._run_items` already wraps every case in
      `log_context.case_scope` with its `[i/total]` position — nothing to do here.
      Otherwise, in `scrape()`: wrap each row's work in
      `log_context.case_scope(case_number, index, total)` and set
      `record.position = (index, total)` before `yield`, exiting the scope before
      the yield.
  - [ ] Decide how to handle `total`: SC streams rows page by page, so the total
        isn't known up front. Options:
        (a) show index only (`[12] Crl.A. 123/2024`) — needs `_case_prefix` in
        `orchestrator/log_context.py` and `caseHeading` in the UI viewer to accept
        `total = None`;
        (b) read every page's rows first, then download — gives a real total but
        changes when downloads start.
        Recommended: (a).
  - [ ] Case label: `data.get("Case Number")`, falling back to the PDF URL (same
        fallback `batch_runner` uses).

- [ ] **Rewrite `adapters/supreme_court/promotion.py` messages.** Its `slog` calls
      still use the old wording (`ingestion_id=%s: starting (case_number_raw=%r)`).
      Match whatever MP Phase 2 settles on, e.g. `Saved as case #1204 ·
      LIZNR/SCIN/0087/2025`. The three `assign_liznr_id_for_reviewed_case` lines run
      outside a batch (admin review) — keep them, wording only.

- [ ] **Skip-reason counts for the batch summary.** MP Phase 2 adds a found /
      skipped-by-reason breakdown. SC needs its own counters: rows found per window,
      no PDF link, download failed, captcha failed windows.

- [ ] **Optional — skip already-promoted cases.** MP skips cases already in
      `cr_cases` before downloading (`scrape_jobs.get_promoted_case_numbers`). SC
      currently re-downloads them and relies on the checksum duplicate check /
      `ON CONFLICT` at promotion. Adding the same skip saves downloads on re-runs;
      log it as `N already in database → skipped`.

- [ ] **Verify** with a real SC batch: every stage appears in the panel, dividers
      show per case, debug dumps stay out of the panel.

### Resume a stopped batch

MP batches can be resumed (same batch, `run_count` + 1, one history/log page).
Shared pieces already in place: `cr_batch_items` (db/migrations/0013),
`POST /api/scraper/batches/{id}/resume`, the resumable path in
`orchestrator/batch_runner.py` (`_run_items`), and the UI Resume / Retry-skipped
buttons. They only turn on for an adapter that has `discover()`.

- [ ] **Split `SupremeCourtAdapter.scrape()` into `discover()` / `session()` /
      `process_item()`** (`adapters.base.ResumableScraperAdapter`).
      Reference: `MPHighCourtAdapter` in `adapters/high_courts/mp/adapter.py`.
  - [ ] Decide the unit of work. SC searches 30-day windows with a captcha per
        window and paginated results, so either:
        (a) discover = every window + every results page up front, one item per
        judgment row (payload = the row's cells + PDF link) — exact resume, but
        discovery gets long for big ranges and SC PDF links may be session-bound; or
        (b) one item per 30-day window — `process_item` re-searches that window and
        handles all its rows; resume restarts from the first unfinished window.
        Check whether SC PDF links still work from a new browser session before
        choosing (a).
  - [ ] Return skip reasons from `process_item` (no PDF link, download failed)
        instead of only logging them, so they land in `cr_batch_items.reason`.
  - [ ] Captcha / "No Records Found": raise `SourceUnavailableError` when the
        captcha can't be solved (like MP's `ilrs.discover_candidates`), so the batch
        stops resumable instead of looking like "no judgments".
- [ ] `item.key`: SC "Case Number" (or diary number), unique within the batch.
- [ ] Remove `scrape()` once `discover()` exists — `batch_runner` prefers the
      resumable path whenever `discover` is present.
- [ ] Verify: stop an SC batch (cancel, kill the server, source error), resume, and
      check only the remaining work runs.

---

## 2. Legal NER for acts / sections

### How SC works today

- `adapters/supreme_court/promotion.py` inserts `sections = '{}'`, `acts = '{}'`
  on purpose (see its `promote_ingestion` docstring, 2026-09-09): regex act-name
  capture produced junk acts at production scale.
- Acts/sections are then filled by `pipeline/llm_enrichment.py`, from the
  provision paragraphs found by `adapters/supreme_court/extraction.find_provision_paragraphs`.
  Enrichment only writes provisions when the case has none yet
  (`existing_sections` guard).

### Target flow (same as MP)

`NER on OCR text → if it finds acts, write them at promotion → LLM enrichment
only fills provisions when NER found nothing.` Because of the `existing_sections`
guard, the LLM fallback should need no change — confirm this while implementing.

### Checklist

- [ ] **Measure NER quality on SC judgments first.** NER was checked against 13 MP
      High Court judgments only. Run `pipeline.legal_ner_extraction.extract_acts_sections`
      over a sample of real SC judgments (from `cr_cases.ocr_text` where
      `court_id` = SCIN and enrichment already filled sections) and compare with the
      LLM's acts/sections. Look for the same failure the regex had: fragments
      stored as act names. Go ahead only if NER is at least as clean as the LLM.

- [ ] **Measure NER time on long SC judgments.** SC judgments can be ~300,000 chars
      (the 142-page test case in `llm_enrichment.py`'s docstring). NER runs per
      sentence and is serialized by `_infer_lock` in `pipeline/legal_ner_extraction.py`
      — measure one long judgment. If it's slow, consider running NER on
      `find_provision_paragraphs` output instead of the full text.

- [ ] **Share the act/section resolver.** MP turns NER output into ids with
      `_resolve_acts(cur, act_entries)` in `adapters/high_courts/mp/promotion.py`.
      SC needs identical logic (`resolve_act` + `get_or_create_act` +
      `get_or_create_section`, deduped). Either move it to a shared module
      (e.g. `db/lookups.py`) and import it from both courts, or copy it into SC
      promotion. It doesn't vary per court, so moving it is preferred.

- [ ] **Call NER in `adapters/supreme_court/promotion.py`.** Inside the
      `with conn.cursor()` block, before the INSERT:
      `act_ids, section_ids = _resolve_acts(cur, extract_acts_sections(ocr_text))`,
      then insert `section_ids` / `act_ids` instead of the literal `'{}', '{}'`.

- [ ] **Log it at the NER stage** (counts only, never act names):
  - found: `Ran Legal NER on judgment text · found N act(s), M section(s)`
  - found nothing: `Ran Legal NER · nothing found → LLM enrichment will extract provisions`

- [ ] **Update the `promote_ingestion` docstring** — the "sections/acts are
      deliberately left empty" paragraph stops being true.

- [ ] **Check the enrichment fallback end to end:** a case where NER finds nothing
      still gets provisions from the LLM; a case where NER found acts keeps them and
      the LLM doesn't overwrite them.

- [ ] **Verify** with a real SC batch: NER lines appear per case, acts/sections are
      written at promotion, enrichment only fills provisions for NER misses.

### Known NER limitation (applies to both courts)

2 of the 13 MP test judgments hit upstream `IndexError`s inside
`pipeline/legal_ner_lib/postprocessing_utils.py` (`seperate_provision`,
`map_pro_statute_on_heuristics`). Those return no acts; the error is now logged
with a traceback. Fixing them in the vendored library would help both courts.
