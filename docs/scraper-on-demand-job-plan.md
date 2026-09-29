# Plan: scraper-backend → on-demand Trigger Job, control plane → api-backend

Status (2026-09-27): **Phases 0-4 built and verified locally. Phase 5 (cutover) not done.**

Decisions made: logs are stored in a DB table and pruned after 7 days. Parallel batches for the same court are allowed only when their date ranges don't overlap (inclusive) with an active (QUEUED or RUNNING) batch; the check is serialized per court with a Postgres advisory lock. In-place refactor. Spot nodes stay. api-backend auth on the scraper routes is deferred.

What differs from the text below: `cr_scrape_batches.queued_at` was added, so a resumed run's QUEUED timeout doesn't use the old `requested_at`. The api-backend companion migration is `db/migrations/0002_on_demand_worker.sql`. The sweeper runs lazily at most once a minute on `GET /batches` and `GET /batches/{id}`.

Verified locally (throwaway Postgres; real api-backend and worker; fake court adapter, so no court sites or Azure were touched):
- start → QUEUED → RUNNING → COMPLETED, with the SSE stream following the logs live
- a start whose date range overlaps an active batch of the same court → 409
- cancel while RUNNING → CANCELLED → resume starts a new process (run 2), and the logs of both runs replay on one stream
- SIGTERM → FAILED with the reason, and the batch can be resumed
- `kill -9` → the heartbeat sweeper marks it FAILED with an INTERRUPTED event
- cancel while QUEUED → CANCELLED, and the worker exits 0 without scraping
- a QUEUED run that's never claimed → FAILED "never started"
- trigger unreachable → 502, the batch is FAILED with the real error, and the court isn't left locked
- the trigger's `main.py` with the real `case-scraper.yaml` (K8s stubbed) → Job `case-scraper-b42-r3` with `BATCH_ID` injected, and the manual `COURT` path still works

Not verified: a real K8s Job run, a real SC or MP scrape through the worker, pod memory, the legal-ui screens in a browser.

**Remaining cutover steps (manual):**
1. Push infra-deployments. Rebuild and redeploy **trigger**: its image bakes in `app/jobs/*.yaml`, so the new `case-scraper.yaml` only ships with a trigger rebuild. Apply `cluster-role-binding.yaml`.
2. Add `SCRAPER_LAUNCHER=trigger` and `SCRAPER_TRIGGER_URL=http://<trigger svc>.dev.svc.cluster.local/job/case-scraper/` to the `case-research-backend/.env` blob. Confirm the service name with `kubectl get svc -n dev`.
3. Deploy api-backend. It applies 0002 and seeds courts.
4. Run the case-scraper Jenkins job. It now pushes `:latest` and no longer deploys with Helm.
5. Deploy legal-ui. `CASE_RESEARCH_SCRAPER_SERVER_URL` is no longer read and can be deleted from its env.
6. `helm uninstall case-scraper -n dev`, then immediately `kubectl apply -f services/dev/case-scraper/serviceaccount.yaml`.
7. Run a short real SC range from the admin UI and follow §7's dev-cluster checks.

## 1. Goal

- **scraper-backend** stops being a standing FastAPI Deployment. It becomes a run-once worker image: a pod starts, runs **one batch**, writes everything to Postgres/Blob, and exits. It is launched only through the Trigger service (`infra-deployments/services/dev/trigger`) using `jobs/case-scraper.yaml`.
- **api-backend** takes over every HTTP concern the scraper serves today: start/resume/cancel, batch list/detail/records, pipeline status, court config, and the live-log SSE stream.
- **legal-ui** talks only to api-backend. `CASE_RESEARCH_SCRAPER_SERVER_URL` and `configs/scraper-axios.config.ts` go away.

```
legal-ui ──server actions / SSE proxy──▶ api-backend ──POST /job/case-scraper/──▶ trigger ──▶ K8s Job (scraper worker)
                                              │                                                        │
                                              └───────────────── Postgres (cr_scrape_batches, cr_batch_logs, …) ◀──┘
```

The database is the only channel between api-backend and a running worker. api-backend never calls the pod, and the pod never calls api-backend.

## 2. What breaks if we only move the endpoints (why this isn't a copy-paste)

These are the current behaviours that assume one long-lived scraper process. Each one needs a replacement:

| # | Today | Why it breaks as a Job | Replacement |
|---|---|---|---|
| 1 | **Live logs are in-memory** (`orchestrator/live_logs.py`), and the SSE endpoint reads that buffer | api-backend is a different process and can't see the pod's memory. The pod has no Service and is deleted after the TTL | New `cr_batch_logs` table. The worker writes to it in batches, and api-backend's SSE endpoint tails it (§4.3) |
| 2 | **`fail_orphaned_batches()` at startup** marks *every* RUNNING batch FAILED (`api.py:173-184`) | Once several jobs run at once, each new pod would kill every other running batch | Remove it. Use a heartbeat column plus a stale-batch sweeper in api-backend (§4.4) |
| 3 | `batch_id` is created inside the scraper's `/start` and returned to the UI, which then navigates to `/admin/scraping/history/{id}` | The Trigger response returns only a k8s job name | api-backend creates the batch row (`QUEUED`) **before** it triggers the job and passes `BATCH_ID` to the pod |
| 4 | The adapter registry (`_ADAPTER_REGISTRY` in `routers/scraper_router.py`) is also used to answer "is this batch resumable?" (`hasattr(adapter_class, "discover")`) | api-backend can't import adapters (no shared code, no Playwright) | A small static court-capability map in api-backend. The worker still owns the real registry (§4.2) |
| 5 | Courts are seeded at scraper startup (`ensure_seeded()`) | The scraper no longer starts until someone triggers it, and `/sc/start` needs `SCIN` to exist first | Move `seed_courts` to api-backend startup |
| 6 | Schema and migrations are applied at scraper startup | The new columns and tables must exist before api-backend writes `QUEUED` batches | api-backend applies the operational migrations. The worker checks the schema and fails fast (§4.5) |
| 7 | Trigger names jobs `case-scraper-{int(time.time())}` (`trigger/app/main.py:67`) | Two starts in the same second collide: the second gets "Already running" and its batch never runs | Name the job after the batch: `case-scraper-b{batch_id}-r{run}` |
| 8 | Jenkins pushes only `case-scraper-dev:${BUILD_NUMBER}-dev` | `case-scraper.yaml` pulls `:latest`, which no pipeline pushes, so it's stale or missing | Jenkins also tags and pushes `:latest` |
| 9 | The Job borrows `case-scraper-sa` from the Helm release | Uninstalling the release deletes the ServiceAccount, and blob `.env` loading breaks | A standalone ServiceAccount manifest with the same name, so the federated credential still matches |
| 10 | Trigger's ClusterRole allows only `create/get/list` on jobs, but `main.py` also deletes finished jobs with the same name | That call returns 403 | Unique names (fix 7) mean the delete branch is never reached. Add `delete` anyway so the code and the RBAC agree |

## 3. Target batch lifecycle

```
            api-backend                          worker pod
start ─▶ INSERT batch status=QUEUED ─▶ trigger ─▶ claim QUEUED→RUNNING ─▶ run ─▶ COMPLETED / FAILED / CANCELLED / SOURCE_*
resume ─▶ claim stopped→QUEUED (run_count+1) ─▶ trigger ─▶ (same as above)
cancel ─▶ QUEUED   → CANCELLED directly (worker's claim then no-ops, exits 0)
          RUNNING  → cancel_requested=TRUE (worker polls between records — unchanged)
sweeper ─▶ QUEUED  with no claim after 15 min  → FAILED "job never started"
           RUNNING with heartbeat older than 5 min → FAILED/CANCELLED "worker stopped responding"
```

- `QUEUED` is new. `status` is `TEXT`, so no enum migration is needed. The UI shows it as "Starting…", because pulling the heavy image onto a fresh spot node can take minutes.
- If the trigger call fails, api-backend marks the batch `FAILED` with `error_message = "Could not launch scraper job: …"` in the same request and returns a 502. It doesn't leave a QUEUED row behind.

## 4. Design details

### 4.1 Worker entrypoint (scraper-backend)

New `worker.py` (run as `python -m worker`):

```
python -m worker --batch-id 123            # normal path (api-backend created the batch)
python -m worker --court sc --from 2026-09-01 --to 2026-09-07   # manual / CronJob path
python -m worker --court mp --year 2024
```

Sequence:
1. Logging setup (moved from `api.py`), including the noisy-Azure-logger quieting.
2. Fail-fast startup: DB pool + `SELECT 1`, schema check (§4.5), `load_models()` for NER. Each one is an `exit 1` with the real reason logged, not a swallowed error.
3. If `--court` is given, create the batch itself as `QUEUED` (same resolution as today's `/sc/start` and `/mp/start`). This keeps the current `COURT/FROM_DATE/TO_DATE/YEAR` trigger usage working for manual runs and scheduled CronJobs.
4. Atomic claim: `UPDATE cr_scrape_batches SET status='RUNNING', heartbeat_at=now(), job_name=%s WHERE batch_id=%s AND status='QUEUED' RETURNING court_id, date_from, date_to, run_count`. If no row comes back, log why (already running or cancelled) and exit 0.
5. Resolve the court's `AdapterSpec` from the registry, now in `orchestrator/registry.py` and moved out of the router, then call `batch_runner.run_batch(...)` with `run_number=run_count`.
6. Start a heartbeat thread that updates `heartbeat_at` every 30s until the run finishes.
7. SIGTERM handler for spot eviction or `kubectl delete`: set the cancel flag in process, so the runner stops after the current record and finishes as `FAILED` with `error_message="Worker pod terminated (…)"`. The batch stays resumable because it's item-based. Set `terminationGracePeriodSeconds: 90` in the Job so one record can finish.
8. Exit code: `0` for COMPLETED or CANCELLED, `1` otherwise, so `kubectl get jobs` means something.

Removed from scraper-backend: `api.py`, `routers/`, `static/`, `fastapi`/`uvicorn` from `requirements.txt`, the Dockerfile's `EXPOSE`/`CMD uvicorn` (the CMD becomes `python -m worker`), `fail_orphaned_batches()`, and `live_logs.py`'s in-memory buffer. Kept unchanged: adapters, pipeline, normalization, storage, and the write-side functions in `db/scrape_jobs.py`.

### 4.2 api-backend control plane

New or moved files (a copy with ownership transferred; both services still share no code):

| api-backend file | Contents | Source |
|---|---|---|
| `routers/scraper_router.py` | Same paths as today (`/api/scraper/start`, `/sc/start`, `/mp/start`, `/batches`, `/batches/{id}`, `/records`, `/cancel`, `/resume`, `/logs/stream`, `/status`) so the legal-ui change is only a base-URL swap | scraper-backend `routers/scraper_router.py` minus BackgroundTasks |
| `routers/court_config_router.py` | `GET /api/courts`, `PUT /api/courts/{id}/config` | moved as-is |
| `db/scrape_jobs.py` | `create_batch` (now `QUEUED`), `claim_batch_resume` (now → `QUEUED`), `request_cancel` (handles QUEUED), `get_batch`, `list_batches`, `list_ingestions_by_batch`, `_derived_events`, `fail_stale_batches` | scraper-backend `db/scrape_jobs.py` read/control half |
| `db/court_config.py`, `db/seed_courts.py` | court lookups + seeding at startup | moved |
| `db/batch_logs.py` | `get_logs_since(batch_id, after_id, limit)` | new |
| `scraper/job_launcher.py` | `launch_batch(batch_id, run_number, headless)`: POST `{batch_id, headless}` to `SCRAPER_TRIGGER_URL`, record `job_name` on the batch, and raise with the real response body on any non-2xx | new |
| `scraper/courts.py` | `SUPPORTED_ADAPTERS = {"supreme_court": {"resumable": True}, "high_court_mp": {"resumable": True}}`, used for `404/400` on start and for the `resumable` flag. Mirrors the worker's registry by hand, the same way `schema.sql` is mirrored | new |

- The launcher has two modes, set by `SCRAPER_LAUNCHER=trigger|subprocess`. `subprocess` is for local development only: it runs `SCRAPER_WORKER_CMD` (for example `cd ../scraper-backend && venv/bin/python -m worker`) with `--batch-id`, detached. This replaces the "just run uvicorn locally" workflow.
- Stale sweeper: `fail_stale_batches()` runs lazily at the top of `GET /batches` and `GET /batches/{id}`. It's one cheap UPDATE, and no background scheduler is needed in a single-replica service. It records an `INTERRUPTED` event with the reason.
- New dependency: `httpx` (or `requests`) in `api-backend/requirements.txt`.
- New env vars: `SCRAPER_TRIGGER_URL` (for example `http://<trigger-svc>.dev.svc.cluster.local/job/case-scraper/`; confirm the service name with `kubectl get svc -n dev`), `SCRAPER_LAUNCHER`, and `SCRAPER_WORKER_CMD` (local only).

### 4.3 Persistent logs (`cr_batch_logs`)

```sql
CREATE TABLE cr_batch_logs (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  batch_id    BIGINT NOT NULL REFERENCES cr_scrape_batches(batch_id) ON DELETE CASCADE,
  run_number  INT NOT NULL,
  ts          TIMESTAMPTZ NOT NULL,
  level       TEXT NOT NULL,
  stage       TEXT, case_ref TEXT, item_index INT, item_total INT,
  message     TEXT NOT NULL
);
CREATE INDEX ix_cr_batch_logs_batch_id ON cr_batch_logs(batch_id, id);
```

- **Worker side:** `live_logs.log()` keeps its signature, so `log_context.slog()` is untouched. It now appends to a thread-safe queue. A flusher thread inserts lines with `execute_values` every 1s or every 100 lines, and does a final flush in `finish_batch`/SIGTERM. A DB write failure is logged to stdout with the reason and never fails the batch.
- **api-backend side:** SSE polls `WHERE batch_id=%s AND id > %s ORDER BY id LIMIT 500` every 1s. It closes when the batch is terminal and there are no new rows. It replays full history on connect, which fixes "logs lost after restart" for free. The `trimmed` event and the 1,000-line cap go away. A resumed batch naturally shows every run on one page.
- **Retention:** delete rows older than 60 days in the same lazy sweeper. A full-year MP run is about 10 lines per case, which is small for Postgres.
- The payload shape (`ts, level, stage, case, index, total, message`) stays the same, so `scraper-batch-log-viewer.tsx` needs only copy changes.

### 4.4 Heartbeat and orphan handling

- New columns on `cr_scrape_batches`: `heartbeat_at TIMESTAMPTZ`, `job_name TEXT`, `claimed_at TIMESTAMPTZ`.
- The sweeper's rules are the thresholds in §3. It uses the same `CASE WHEN cancel_requested THEN 'CANCELLED' ELSE 'FAILED'` logic as today's `fail_orphaned_batches`, so existing UI states still work.
- A batch the sweeper failed can be resumed like any other, since both adapters are item-based now.

### 4.5 Schema ownership

Today scraper-backend applies `schema.sql` + `db/migrations/*` at startup, and api-backend keeps a hand-mirrored `schema.sql`. After the move, api-backend is the only always-on service and the first writer of the new columns, so:

- **Recommended:** new migration `0015_on_demand_worker.sql` (heartbeat/job_name/claimed_at columns + `cr_batch_logs`) lives in scraper-backend's `db/migrations/` as usual **and** is mirrored into api-backend's `db/schema.sql` + `db/migrations/`. api-backend's startup applies it, and the worker's startup also runs `ensure_schema()` (idempotent, already fail-fast). Whichever starts first applies it, and the second is a no-op. This keeps the existing "identical §0-§5" contract.
- Deploy order matters (§6): api-backend deploys first.

## 5. Infra changes (`infra-deployments`)

**`services/dev/trigger/app/jobs/case-scraper.yaml`** (rewrite):
- `metadata.labels: {app: case-scraper-job}`.
- `env`: `BATCH_ID`, `HEADLESS` (default `"true"`), and keep `COURT`, `FROM_DATE`, `TO_DATE`, `YEAR` for the manual path. Trigger only overrides env names already declared in the YAML (`main.py:74-90`), so every one must be listed.
- `args`: `["/bin/sh","-c","if [ -n \"$BATCH_ID\" ]; then exec python -m worker --batch-id \"$BATCH_ID\"; else …--court path…; fi"]`. The uvicorn/curl/poll script is gone, and `docker-entrypoint.sh` still starts Xvfb.
- `backoffLimit: 0`. A retried pod would find the batch already RUNNING or FAILED anyway, and a restart is the user's Resume.
- `terminationGracePeriodSeconds: 90`, `activeDeadlineSeconds: 43200` (12h safety cap; confirm against the longest real MP year run), `ttlSecondsAfterFinished: 7200` (unchanged).
- Resources: raise the memory limit to about **2Gi**. Chromium, spaCy NER and Tesseract in one pod can exceed 1Gi. Check against a real run with `kubectl top pod`.
- Spot node selector stays. Eviction is survivable through SIGTERM → FAILED → Resume.

**`services/dev/trigger/app/main.py`**: when the request has `batch_id`, set `k8s_job_name = f"{job_name}-b{batch_id}-r{run_number or 1}"`. Return that name. Add a `batch-id` label to the Job for `kubectl get jobs -l`.

**`services/dev/trigger/cluster-role-binding.yaml`**: add `delete` to the verbs.

**`services/dev/case-scraper/`**:
- New `serviceaccount.yaml`: `case-scraper-sa` in `dev`, with the same `azure.workload.identity/client-id` annotation the Helm chart sets. The name doesn't change, so the federated credential in `terra_liznr/terraform.tfvars` doesn't change either.
- `jenkins/jenkins.groovy`: build, push `:${BUILD_NUMBER}-dev` **and** `:latest`, and remove the `helm upgrade` stage.
- `values.yaml`: delete it (or keep it as reference) once the release is uninstalled.

**`services/dev/case-research-backend/`** (api-backend): no chart change. Add `SCRAPER_TRIGGER_URL`/`SCRAPER_LAUNCHER=trigger` to the `case-research-backend/.env` blob. The scraper's Azure OpenAI, NER and Playwright settings stay only in `case-scraper/.env`.

## 6. Phases and order

**Phase 0: Infra prep (no behaviour change)**
1. Jenkins pushes `:latest` for case-scraper.
2. Trigger: job naming from `batch_id`, `delete` verb. Redeploy trigger.
3. Create the standalone `case-scraper-sa` manifest. Don't apply it yet (the Helm release still owns the name).

**Phase 1: Schema**
4. Migration `0015` in scraper-backend and mirrored into api-backend's schema and migrations.

**Phase 2: api-backend control plane**
5. Move the court config, seeding, batch read and control code. Add `job_launcher`, `batch_logs`, the stale sweeper and the SSE endpoint.
6. `/start`, `/resume` and `/cancel` use the QUEUED lifecycle and the launcher.

**Phase 3: scraper-backend → worker**
7. `worker.py`, `orchestrator/registry.py`, heartbeat thread, SIGTERM handler, DB-backed `live_logs`.
8. Remove the FastAPI surface, `fail_orphaned_batches`, and the fastapi/uvicorn deps. Change the Dockerfile CMD.
9. Update `docker-compose.yml`: the scraper becomes a `profiles: [worker]` run-once service, and api-backend uses `SCRAPER_LAUNCHER=subprocess` locally.

**Phase 4: legal-ui**
10. `features/admin/api/scraping.actions.ts` switches from `scraperApi` to the case-research axios client. The paths stay the same.
11. `app/next-api/admin/scraper/batches/[id]/logs/route.ts`: `baseUrl` becomes `CASE_RESEARCH_SERVER_URL`.
12. `features/admin/api/types.ts`: add `QUEUED` to the batch status union. Header card, batch row and stepper get a "Starting…" state. Resume and cancel buttons handle QUEUED.
13. Log viewer: remove the "logs are in-memory only" copy and the `trimmed` handling.
14. Delete `configs/scraper-axios.config.ts` and the `CASE_RESEARCH_SCRAPER_SERVER_URL` env entries.

**Phase 5: Cutover (dev)**
15. Deploy api-backend (applies 0015) → push the worker image `:latest` → deploy legal-ui.
16. `helm uninstall case-scraper -n dev` → immediately `kubectl apply` `case-scraper-sa`.
    - During any overlap, **don't restart the old scraper Deployment**. Its startup `fail_orphaned_batches()` would kill batches the new Jobs are running.

## 7. Verification

These must be run for real. Import checks and passing builds are not enough.

**Local (throwaway Postgres, `SCRAPER_LAUNCHER=subprocess`)**
- Start SC for a 1-2 day range: the batch is QUEUED → RUNNING → COMPLETED, logs stream through api-backend SSE, and the records and status endpoints match.
- Cancel while QUEUED ends as CANCELLED and the worker exits 0 without scraping. Cancel while RUNNING stops after the current record.
- `kill -9` the worker mid-run: after 5 min the sweeper marks it FAILED, and Resume continues the remaining items with `run_count=2` and the logs of both runs on one page.
- `kill -TERM`: ends FAILED with "pod terminated" and can be resumed.
- Two starts in quick succession produce two independent batches and two workers.

**Dev cluster**
- A real SC short range and a real MP year through the admin UI. Check the Job name `case-scraper-b{id}-r1`, that `.env` came from blob, pod memory under `kubectl top`, and that the Job completes with exit 0.
- `kubectl delete pod` mid-run → FAILED → Resume from the UI → the new Job is `-r2`.
- Make the trigger unreachable (scale it to 0): the start endpoint returns 502 and the batch shows FAILED with the reason.
- Browser check of the QUEUED state, the log viewer, and the resume and cancel buttons.

## 8. Risks and open questions

1. **No auth on the new write endpoints.** Neither backend verifies the bearer token legal-ui sends today. Admin gating lives only in legal-ui's `proxy.ts`, and api-backend is ClusterIP-only. The new endpoints launch pods, so consider verifying the admin JWT in api-backend's scraper router. Recommended, but can be a follow-up.
2. **One batch per court at a time?** Nothing prevents two concurrent batches for the same court today. Parallel pods make this more likely, with duplicate discovery and double promotion attempts. A cheap guard would reject `/start` with 409 when a QUEUED or RUNNING batch exists for that court. Recommended.
3. **Spot eviction on long MP year runs.** Resume covers it, but a 10h run could be evicted several times. Option: a non-spot node pool for `case-scraper` Jobs only.
4. **Trigger is unauthenticated.** That's fine only while its ingress stays disabled (`trigger/values.yaml`). Keep it that way.
5. **Refactor in place, not a new directory.** This restructures scraper-backend in place instead of creating a sibling `-v2` rebuild. The adapters and pipeline don't change, so a parallel directory would only duplicate them. Say if you want the isolated-directory approach instead.
6. **Scheduled scrapes.** The `--court` path makes a K8s `CronJob` (for example weekly SC for the last 7 days) a small follow-up, with no api-backend involvement.
