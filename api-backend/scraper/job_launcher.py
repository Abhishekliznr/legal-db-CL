"""
Starts one scraper-backend worker run for a QUEUED batch.

SCRAPER_LAUNCHER=trigger (deployed): POSTs to the trigger service, which creates the
K8s Job from infra-deployments/services/dev/trigger/app/jobs/case-scraper.yaml.
SCRAPER_LAUNCHER=subprocess (local dev only): runs SCRAPER_WORKER_CMD in SCRAPER_WORKER_DIR.
"""

import logging
import os
import shlex
import subprocess
import threading

import requests

logger = logging.getLogger("api_backend_v2.scraper_launcher")

_TRIGGER_TIMEOUT_SECONDS = 30

# Only these reach a local worker process: it must load its own .env (DB, Azure, OpenAI),
# and load_dotenv() never overrides a variable that's already set.
_PASSTHROUGH_ENV = ("PATH", "HOME", "LANG", "TMPDIR", "DISPLAY", "PLAYWRIGHT_BROWSERS_PATH")


class LaunchError(Exception):
    pass


def launch_batch(batch_id: int, run_number: int, headless: bool = True) -> str:
    """Returns the launched job's name. Raises LaunchError with the real reason on failure."""
    mode = os.environ.get("SCRAPER_LAUNCHER", "trigger").strip().lower()
    if mode == "trigger":
        return _launch_via_trigger(batch_id, run_number, headless)
    if mode == "subprocess":
        return _launch_subprocess(batch_id, headless)
    raise LaunchError(f"Unknown SCRAPER_LAUNCHER={mode!r} — expected 'trigger' or 'subprocess'")


def _launch_via_trigger(batch_id: int, run_number: int, headless: bool) -> str:
    url = os.environ.get("SCRAPER_TRIGGER_URL", "").strip()
    if not url:
        raise LaunchError("SCRAPER_TRIGGER_URL is not set — point it at the trigger service's /job/case-scraper/ endpoint")

    payload = {"batch_id": batch_id, "run_number": run_number, "headless": "true" if headless else "false"}
    try:
        response = requests.post(url, json=payload, timeout=_TRIGGER_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise LaunchError(f"Could not reach the trigger service at {url}: {exc}") from exc

    if not response.ok:
        raise LaunchError(f"Trigger service returned {response.status_code}: {response.text[:500]}")
    try:
        body = response.json()
    except ValueError as exc:
        raise LaunchError(f"Trigger service returned a non-JSON response: {response.text[:500]}") from exc

    job_name = body.get("job_name")
    if not job_name:
        raise LaunchError(f"Trigger service response has no job_name: {body}")
    if body.get("status") != "Job triggered":
        logger.warning("Trigger service answered %r for batch %s run %s (job %s)", body.get("status"), batch_id, run_number, job_name)
    return job_name


def _launch_subprocess(batch_id: int, headless: bool) -> str:
    command = os.environ.get("SCRAPER_WORKER_CMD", "").strip()
    workdir = os.environ.get("SCRAPER_WORKER_DIR", "").strip() or None
    if not command:
        raise LaunchError("SCRAPER_LAUNCHER=subprocess needs SCRAPER_WORKER_CMD, e.g. 'venv/bin/python -m worker'")

    env = {name: os.environ[name] for name in _PASSTHROUGH_ENV if name in os.environ}
    env["HEADLESS"] = "true" if headless else "false"
    try:
        process = subprocess.Popen(
            [*shlex.split(command), "--batch-id", str(batch_id)],
            cwd=workdir,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise LaunchError(f"Could not start the local worker ({command!r} in {workdir or os.getcwd()}): {exc}") from exc
    threading.Thread(target=_reap, args=(process, batch_id), daemon=True).start()
    return f"local-pid-{process.pid}"


def _reap(process: subprocess.Popen, batch_id: int) -> None:
    code = process.wait()
    log = logger.info if code == 0 else logger.error
    log("Local scraper worker for batch %s (pid %s) exited with code %s", batch_id, process.pid, code)
