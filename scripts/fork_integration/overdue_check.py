"""Out-of-process dead-man's switch for a cron job (U4/R18, KTD14).

Standalone by design: this is meant to run from a Windows Scheduled Task
independent of the Hermes Python environment/venv, so it must keep working
even when that environment is broken -- exactly the failure mode it exists
to catch (a scheduler that silently stopped ticking). It imports NOTHING
from the ``hermes-agent`` package -- stdlib only.

Because it cannot import ``hermes_constants``/``cron.jobs``, the HERMES_HOME
resolution and the ``cron/jobs.json`` / ``cron/ticker_heartbeat`` paths below
are a deliberate READ-ONLY mirror of:

  - ``hermes_constants.get_hermes_home()`` / ``_get_platform_default_hermes_home()``
    (env var -> platform-native default; the context-local override only
    exists inside a running Hermes process and has no meaning here)
  - ``cron.jobs.JOBS_FILE`` (``<home>/cron/jobs.json``)
  - ``cron.jobs.TICKER_HEARTBEAT_FILE`` (``<home>/cron/ticker_heartbeat`` --
    NOT ``.ticker_heartbeat``; verified against cron/jobs.py on this branch)

If those change, this mirror must be updated by hand.

Verdict (always printed to stdout as one JSON object):
  - exit 0  healthy: the job is not overdue, or it's overdue but the ticker
    heartbeat is still fresh (the scheduler is alive -- plausibly just a
    long-running previous job, not a dead-man's condition).
  - exit 2  ALARM: the job is overdue AND the ticker heartbeat is stale --
    the scheduler itself looks dead, not merely busy. Unless --dry-run, one
    line is appended to ``<hermes-home>/logs/cron-scripts/overdue-alerts.log``.
  - exit 1  indeterminate: jobs.json is missing/corrupt, the job id was not
    found, or the job has no computable ``next_run_at``. Never reported as
    "healthy" -- a checker that cannot see the job must not claim it's fine.

DELIVERY NOTE (v1 scope, deliberately limited): this script does not send a
live alert (Discord/webhook/etc). It only detects, prints a JSON verdict,
logs on ALARM, and exits nonzero, per the plan's KTD14 ("the dead-man's
overdue check is the only out-of-process sender") -- wiring an actual
out-of-band send is deferred; today the wrapping scheduled task's exit code
and the alert log are the signal an operator (or another watcher) consumes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

DEFAULT_JOB_ID = "1ab4c7013fef"
DEFAULT_GRACE_MINUTES = 90.0


def _platform_default_hermes_home() -> Path:
    """Mirror hermes_constants._get_platform_default_hermes_home()."""
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def resolve_hermes_home(override: Optional[str]) -> Path:
    """Resolve HERMES_HOME: --hermes-home override > HERMES_HOME env > platform default.

    ``override`` exists so tests (and any operator debugging a fixture) can
    point the checker at a tmp directory instead of the real profile home --
    this script must never write under a real HERMES_HOME as a side effect
    of being tested.
    """
    if override:
        return Path(override)
    env_value = os.environ.get("HERMES_HOME", "").strip()
    if env_value:
        return Path(env_value)
    return _platform_default_hermes_home()


def _parse_iso_to_epoch(raw: Any) -> Optional[float]:
    """Parse an ISO-8601 timestamp (as written by hermes_time.now().isoformat())
    to POSIX epoch seconds. Returns None for anything unparseable."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except (TypeError, ValueError):
        return None


def load_job(hermes_home: Path, job_id: str) -> tuple[Optional[dict], Optional[str]]:
    """Read ``<hermes_home>/cron/jobs.json`` read-only and find one job.

    Returns ``(job, error)``. ``error`` is set only for a read/parse failure
    (missing file, corrupt JSON) -- a clean read where the job id simply
    isn't present returns ``(None, "job id ... not found ...")`` too, since
    either way the caller cannot compute a verdict for that job.
    """
    jobs_file = hermes_home / "cron" / "jobs.json"
    if not jobs_file.exists():
        return None, f"jobs.json not found at {jobs_file}"
    try:
        raw = jobs_file.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"failed to read jobs.json: {exc}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"failed to parse jobs.json: {exc}"

    if isinstance(data, dict):
        jobs = data.get("jobs", [])
    elif isinstance(data, list):
        jobs = data
    else:
        return None, f"jobs.json has unexpected shape: {type(data).__name__}"

    for job in jobs:
        if isinstance(job, dict) and str(job.get("id")) == str(job_id):
            return job, None
    return None, f"job id {job_id!r} not found in jobs.json ({len(jobs)} job(s) present)"


def heartbeat_age_seconds(hermes_home: Path, *, now: float) -> tuple[Optional[float], Path]:
    """Seconds since the ticker last touched ``cron/ticker_heartbeat``, or
    None if the file is missing/unreadable (treated as stale by the caller,
    same as cron.jobs.get_ticker_heartbeat_age's "unknown" contract)."""
    heartbeat_path = hermes_home / "cron" / "ticker_heartbeat"
    try:
        raw = heartbeat_path.read_text(encoding="utf-8").strip()
        return max(0.0, now - float(raw)), heartbeat_path
    except (OSError, ValueError):
        return None, heartbeat_path


def compute_verdict(
    hermes_home: Path,
    job_id: str,
    grace_minutes: float,
    *,
    now: Optional[float] = None,
) -> dict:
    """Compute the full verdict dict (pure given the filesystem state) --
    this is the unit tests exercise directly; ``main()`` just prints/exits it.
    """
    now = time.time() if now is None else now
    grace_seconds = max(0.0, grace_minutes) * 60.0
    job, job_error = load_job(hermes_home, job_id)
    heartbeat_age, heartbeat_path = heartbeat_age_seconds(hermes_home, now=now)
    heartbeat_stale = heartbeat_age is None or heartbeat_age > grace_seconds

    result: dict = {
        "job_id": job_id,
        "hermes_home": str(hermes_home),
        "now": now,
        "grace_minutes": grace_minutes,
        "grace_seconds": grace_seconds,
        "heartbeat_path": str(heartbeat_path),
        "heartbeat_age_seconds": heartbeat_age,
        "heartbeat_stale": heartbeat_stale,
    }

    if job is None:
        result.update(
            job_found=False,
            overdue=False,
            alarm=False,
            verdict="indeterminate",
            note=job_error or "job not found",
        )
        return result

    next_run_raw = job.get("next_run_at")
    next_run_epoch = _parse_iso_to_epoch(next_run_raw)
    enabled = bool(job.get("enabled", True))
    result.update(job_found=True, job_enabled=enabled, next_run_at=next_run_raw)

    if not enabled:
        result.update(
            lateness_seconds=None,
            overdue=False,
            alarm=False,
            verdict="healthy",
            note="job is disabled -- not a dead-man's candidate",
        )
        return result

    if next_run_epoch is None:
        result.update(
            lateness_seconds=None,
            overdue=False,
            alarm=False,
            verdict="indeterminate",
            note="job has no computable next_run_at",
        )
        return result

    lateness = now - next_run_epoch
    overdue = lateness > grace_seconds
    result["lateness_seconds"] = lateness
    result["overdue"] = overdue

    if overdue and heartbeat_stale:
        result["verdict"] = "overdue"
        result["alarm"] = True
        result["note"] = (
            "job is overdue and the ticker heartbeat is stale -- the scheduler "
            "itself looks dead, not just a long-running previous job. Live "
            "out-of-band delivery from this checker is deferred to wiring "
            "(see this module's docstring)."
        )
    else:
        result["verdict"] = "healthy"
        result["alarm"] = False
        result["note"] = (
            "overdue but the ticker heartbeat is fresh -- not raised as an "
            "alarm (a long run is plausible)"
            if overdue
            else "on schedule"
        )
    return result


def _append_alert_log(hermes_home: Path, verdict: dict) -> Path:
    log_path = hermes_home / "logs" / "cron-scripts" / "overdue-alerts.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"logged_at": time.time(), **verdict}, sort_keys=True, default=str)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return log_path


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Out-of-process dead-man's switch: alarms when a cron job "
        "is overdue AND the scheduler's own ticker heartbeat is stale."
    )
    parser.add_argument("--job-id", default=DEFAULT_JOB_ID, help=f"Job id to check (default: {DEFAULT_JOB_ID}).")
    parser.add_argument(
        "--grace-minutes", type=float, default=DEFAULT_GRACE_MINUTES,
        help=f"Minutes of lateness/heartbeat staleness tolerated before ALARM (default: {DEFAULT_GRACE_MINUTES}).",
    )
    parser.add_argument(
        "--hermes-home", default=None,
        help="Override HERMES_HOME resolution. Intended for tests/fixtures -- "
        "never pass a real profile home just to try this out; the real path "
        "is picked up automatically from the environment.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute and print the verdict without appending to the alert log.",
    )
    args = parser.parse_args(argv)

    hermes_home = resolve_hermes_home(args.hermes_home)
    verdict = compute_verdict(hermes_home, args.job_id, args.grace_minutes)

    if verdict.get("alarm") and not args.dry_run:
        try:
            verdict["alert_log_path"] = str(_append_alert_log(hermes_home, verdict))
        except OSError as exc:
            verdict["alert_log_error"] = str(exc)
    elif verdict.get("alarm") and args.dry_run:
        verdict["alert_log_path"] = None
        verdict["note"] += " (--dry-run: alert log NOT written)"

    print(json.dumps(verdict, indent=2, sort_keys=True, default=str))

    if verdict.get("verdict") == "indeterminate":
        return 1
    return 2 if verdict.get("alarm") else 0


if __name__ == "__main__":
    sys.exit(main())
