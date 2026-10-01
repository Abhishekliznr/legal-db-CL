"""
scraper-backend worker: runs exactly one scrape batch, then exits.

    python -m worker --batch-id 123                                # a batch api-backend queued
    python -m worker --court sc [--from 2026-09-01] [--to 2026-09-07]  # manual run, creates its own batch
    python -m worker --court mp --year 2024

Deployed as one K8s Job per batch run (infra-deployments/services/dev/trigger/app/jobs/
case-scraper.yaml). There's no HTTP surface: api-backend queues the batch (status QUEUED),
this process claims it (-> RUNNING), heartbeats every 30s while it works, writes its live
log lines to cr_batch_logs, and finishes it. Exit code 0 = COMPLETED/CANCELLED (or nothing
to do), 1 = anything else.
"""

import argparse
import logging
import os
import signal
import sys
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)-40s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# The Azure SDK logs every blob request with full headers at INFO once the root logger is
# at INFO — noise, and its auth headers ride along. storage/azure_blob.py logs real failures itself.
for _noisy_logger_name in ("azure", "azure.core.pipeline.policies.http_logging_policy", "urllib3", "urllib3.connectionpool"):
    logging.getLogger(_noisy_logger_name).setLevel(logging.WARNING)

from db import connection, court_config, scrape_jobs  # noqa: E402
from orchestrator import batch_runner, shutdown  # noqa: E402

logger = logging.getLogger("scraper_backend_v2.worker")

# Bump on each deploy you need to confirm; the startup log line shows which build a pod is running.
BUILD_MARKER = "2026-10-01 pdf-download-fix"

_HEARTBEAT_SECONDS = 30
_COURT_CODES = {"sc": "SCIN", "mp": "MPHC"}


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="python -m worker", description="Run one scraper batch and exit.")
    parser.add_argument("--batch-id", type=int, help="A QUEUED cr_scrape_batches row to claim and run")
    parser.add_argument("--court", choices=sorted(_COURT_CODES), help="Manual run: create a batch for this court first")
    parser.add_argument("--from", dest="from_date", help="--court sc: YYYY-MM-DD, default 7 days ago")
    parser.add_argument("--to", dest="to_date", help="--court sc: YYYY-MM-DD, default today")
    parser.add_argument("--year", type=int, help="--court mp: ILR year")
    args = parser.parse_args(argv)
    if (args.batch_id is None) == (args.court is None):
        parser.error("pass exactly one of --batch-id or --court")
    if args.court == "mp" and args.year is None:
        parser.error("--court mp needs --year")
    return args


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value not in ("false", "0", "no", "off")


def _startup() -> None:
    connection.init_connection_pool()
    with connection.get_pooled_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")

    # Idempotent; also how a worker image with a newer migration brings the DB up to date
    # (api-backend applies its own companion migrations at its startup).
    from db.init_db import ensure_schema
    ensure_schema()

    # A missing model would otherwise make every promotion's NER fallback return [], indistinguishable from "no acts found".
    from pipeline.legal_ner_extraction import load_models
    load_models()


def _create_manual_batch(args) -> int:
    court_code = _COURT_CODES[args.court]
    court_id = court_config.get_court_id_by_code(court_code)
    if court_id is None:
        raise RuntimeError(f"No court with court_code='{court_code}' — start api-backend once (it seeds courts) or run python -m db.seed_courts")
    if args.court == "mp":
        date_from, date_to = date(args.year, 1, 1), date(args.year, 12, 31)
    else:
        date_from = date.fromisoformat(args.from_date) if args.from_date else date.today() - timedelta(days=7)
        date_to = date.fromisoformat(args.to_date) if args.to_date else date.today()
    batch_id = scrape_jobs.create_batch(court_id, date_from, date_to)
    logger.info("Created batch #%s for %s %s -> %s", batch_id, court_code, date_from, date_to)
    return batch_id


def _heartbeat_loop(batch_id: int, stop: threading.Event) -> None:
    while not stop.wait(_HEARTBEAT_SECONDS):
        try:
            scrape_jobs.heartbeat(batch_id)
        except Exception:
            logger.exception("Heartbeat for batch %s failed — api-backend fails the batch after 5 min without one", batch_id)


def _install_signal_handlers(job_name: Optional[str]) -> None:
    def on_signal(signum, _frame):
        name = signal.Signals(signum).name
        logger.warning("Received %s — stopping after the current record", name)
        shutdown.request(f"Worker {job_name or 'process'} received {name} (pod evicted, deleted or timed out)")

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)


def main(argv=None) -> int:
    args = _parse_args(argv)
    logger.info("Scraper worker build: %s", BUILD_MARKER)
    headless = _env_bool("HEADLESS", True)
    job_name = os.environ.get("HOSTNAME")  # the pod name inside K8s
    _install_signal_handlers(job_name)

    try:
        _startup()
    except Exception as exc:
        logger.exception("Worker startup failed")
        if args.batch_id is not None:
            _fail_unclaimed(args.batch_id, f"Scraper worker could not start: {type(exc).__name__}: {exc}")
        return 1

    try:
        batch_id = args.batch_id if args.batch_id is not None else _create_manual_batch(args)
    except Exception:
        logger.exception("Could not create the batch for this manual run")
        return 1

    claim = scrape_jobs.claim_queued_batch(batch_id, job_name)
    if claim is None:
        status = scrape_jobs.get_batch_status(batch_id)
        if status is None:
            logger.error("Batch %s does not exist", batch_id)
            return 1
        logger.warning("Batch %s is %s, not QUEUED — nothing for this worker to do", batch_id, status)
        return 0

    from orchestrator.registry import ADAPTER_REGISTRY, adapter_kwargs

    config = court_config.get_court_scrape_config(claim["court_id"])
    spec = ADAPTER_REGISTRY.get(config["adapter"]) if config else None
    if spec is None:
        reason = f"No scraper adapter registered for court_id={claim['court_id']} (adapter={config and config['adapter']!r})"
        logger.error("%s — failing batch %s", reason, batch_id)
        scrape_jobs.finish_batch(batch_id, "FAILED", 0, 0, error_message=reason)
        return 1

    logger.info("Claimed batch #%s run %s (%s %s -> %s) as %s", batch_id, claim["run_count"], claim["court_code"],
                claim["date_from"], claim["date_to"], job_name or "local process")

    stop_heartbeat = threading.Event()
    threading.Thread(target=_heartbeat_loop, args=(batch_id, stop_heartbeat), name="heartbeat", daemon=True).start()
    try:
        result = batch_runner.run_batch(
            spec.adapter_class(), spec.promote_fn, batch_id, claim["court_id"], claim["court_code"],
            claim["date_from"].isoformat(), claim["date_to"].isoformat(), spec.data_source,
            find_provisions_fn=spec.find_provisions_fn, run_enrichment=spec.run_enrichment,
            run_number=claim["run_count"], save_without_judgment_fn=spec.save_without_judgment_fn,
            **adapter_kwargs(config, headless),
        )
    except Exception:
        # run_batch has already recorded the batch as FAILED with the reason before re-raising.
        logger.exception("Batch %s failed", batch_id)
        return 1
    finally:
        stop_heartbeat.set()

    logger.info("Batch %s finished: %s", batch_id, result["status"])
    return 0 if result["status"] in ("COMPLETED", "CANCELLED") else 1


def _fail_unclaimed(batch_id: int, reason: str) -> None:
    try:
        scrape_jobs.fail_queued_batch(batch_id, reason)
    except Exception:
        logger.exception("Could not record the startup failure on batch %s either — api-backend will fail it once it goes stale", batch_id)


if __name__ == "__main__":
    code = main()
    connection.close_connection_pool()
    sys.exit(code)
