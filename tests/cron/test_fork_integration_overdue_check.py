"""Out-of-process dead-man's switch checker (U4/R18, KTD14).

Exercises scripts/fork_integration/overdue_check.py both as a pure Python
module (compute_verdict) and as a real subprocess CLI invocation, always
via --hermes-home pointed at a pytest tmp_path fixture -- this script must
never touch a real HERMES_HOME as a side effect of being tested.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "fork_integration" / "overdue_check.py"
)

_SPEC = importlib.util.spec_from_file_location("overdue_check", SCRIPT_PATH)
assert _SPEC and _SPEC.loader
overdue_check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(overdue_check)


def _write_jobs(home: Path, jobs: list) -> None:
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")


def _write_heartbeat(home: Path, age_seconds: float) -> None:
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "ticker_heartbeat").write_text(str(time.time() - age_seconds), encoding="utf-8")


class TestResolveHermesHome:
    def test_override_wins_over_everything(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "env-home"))
        assert overdue_check.resolve_hermes_home(str(tmp_path / "override")) == tmp_path / "override"

    def test_env_var_used_when_no_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "env-home"))
        assert overdue_check.resolve_hermes_home(None) == tmp_path / "env-home"

    def test_platform_default_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("HERMES_HOME", raising=False)
        home = overdue_check.resolve_hermes_home(None)
        assert home == overdue_check._platform_default_hermes_home()


class TestComputeVerdict:
    def test_healthy_job_on_schedule(self, tmp_path):
        now = time.time()
        _write_jobs(tmp_path, [{
            "id": "1ab4c7013fef", "enabled": True,
            "next_run_at": _iso(now + 3600),
        }])
        _write_heartbeat(tmp_path, age_seconds=30)

        verdict = overdue_check.compute_verdict(tmp_path, "1ab4c7013fef", 90.0, now=now)

        assert verdict["verdict"] == "healthy"
        assert verdict["alarm"] is False
        assert verdict["overdue"] is False
        assert verdict["heartbeat_stale"] is False

    def test_overdue_with_stale_heartbeat_is_alarm(self, tmp_path):
        now = time.time()
        _write_jobs(tmp_path, [{
            "id": "1ab4c7013fef", "enabled": True,
            "next_run_at": _iso(now - 3 * 3600),  # 3h late
        }])
        _write_heartbeat(tmp_path, age_seconds=3 * 3600)  # scheduler also silent 3h

        verdict = overdue_check.compute_verdict(tmp_path, "1ab4c7013fef", 90.0, now=now)

        assert verdict["overdue"] is True
        assert verdict["heartbeat_stale"] is True
        assert verdict["verdict"] == "overdue"
        assert verdict["alarm"] is True

    def test_overdue_but_healthy_ticker_is_not_an_alarm(self, tmp_path):
        """A long-running previous job can push next_run_at lateness past
        grace while the scheduler is still very much alive -- must not
        alarm (see U4 test scenario: 'does not fire during a healthy long
        run')."""
        now = time.time()
        _write_jobs(tmp_path, [{
            "id": "1ab4c7013fef", "enabled": True,
            "next_run_at": _iso(now - 3 * 3600),
        }])
        _write_heartbeat(tmp_path, age_seconds=10)  # ticker looped 10s ago

        verdict = overdue_check.compute_verdict(tmp_path, "1ab4c7013fef", 90.0, now=now)

        assert verdict["overdue"] is True
        assert verdict["heartbeat_stale"] is False
        assert verdict["verdict"] == "healthy"
        assert verdict["alarm"] is False

    def test_missing_heartbeat_file_counts_as_stale(self, tmp_path):
        now = time.time()
        _write_jobs(tmp_path, [{
            "id": "1ab4c7013fef", "enabled": True,
            "next_run_at": _iso(now - 3 * 3600),
        }])
        # No heartbeat file written at all.

        verdict = overdue_check.compute_verdict(tmp_path, "1ab4c7013fef", 90.0, now=now)

        assert verdict["heartbeat_age_seconds"] is None
        assert verdict["heartbeat_stale"] is True
        assert verdict["alarm"] is True

    def test_job_not_found_is_indeterminate_not_healthy(self, tmp_path):
        _write_jobs(tmp_path, [{"id": "some-other-job", "enabled": True}])
        _write_heartbeat(tmp_path, age_seconds=10)

        verdict = overdue_check.compute_verdict(tmp_path, "1ab4c7013fef", 90.0)

        assert verdict["job_found"] is False
        assert verdict["verdict"] == "indeterminate"
        assert verdict["alarm"] is False

    def test_missing_jobs_file_is_indeterminate(self, tmp_path):
        verdict = overdue_check.compute_verdict(tmp_path, "1ab4c7013fef", 90.0)

        assert verdict["job_found"] is False
        assert verdict["verdict"] == "indeterminate"

    def test_disabled_job_is_healthy_even_if_stale(self, tmp_path):
        now = time.time()
        _write_jobs(tmp_path, [{
            "id": "1ab4c7013fef", "enabled": False,
            "next_run_at": _iso(now - 30 * 3600),
        }])
        # Heartbeat also stale/missing -- still must not alarm a disabled job.

        verdict = overdue_check.compute_verdict(tmp_path, "1ab4c7013fef", 90.0, now=now)

        assert verdict["verdict"] == "healthy"
        assert verdict["alarm"] is False

    def test_missing_next_run_at_is_indeterminate(self, tmp_path):
        _write_jobs(tmp_path, [{"id": "1ab4c7013fef", "enabled": True}])
        _write_heartbeat(tmp_path, age_seconds=10)

        verdict = overdue_check.compute_verdict(tmp_path, "1ab4c7013fef", 90.0)

        assert verdict["verdict"] == "indeterminate"
        assert verdict["alarm"] is False

    def test_grace_window_boundary(self, tmp_path):
        now = time.time()
        _write_jobs(tmp_path, [{
            "id": "1ab4c7013fef", "enabled": True,
            "next_run_at": _iso(now - 89 * 60),  # 89 minutes late, grace=90
        }])
        _write_heartbeat(tmp_path, age_seconds=89 * 60)

        verdict = overdue_check.compute_verdict(tmp_path, "1ab4c7013fef", 90.0, now=now)

        assert verdict["overdue"] is False, "89 minutes must stay under a 90-minute grace"


class TestAlertLogAndDryRun:
    def test_alarm_appends_alert_log_line(self, tmp_path):
        now = time.time()
        _write_jobs(tmp_path, [{
            "id": "1ab4c7013fef", "enabled": True,
            "next_run_at": _iso(now - 3 * 3600),
        }])
        # No heartbeat -> stale -> alarm.

        exit_code = overdue_check.main([
            "--hermes-home", str(tmp_path), "--job-id", "1ab4c7013fef",
        ])

        assert exit_code == 2
        log_path = tmp_path / "logs" / "cron-scripts" / "overdue-alerts.log"
        assert log_path.is_file()
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        logged = json.loads(lines[0])
        assert logged["alarm"] is True

    def test_dry_run_never_writes_alert_log(self, tmp_path):
        now = time.time()
        _write_jobs(tmp_path, [{
            "id": "1ab4c7013fef", "enabled": True,
            "next_run_at": _iso(now - 3 * 3600),
        }])

        exit_code = overdue_check.main([
            "--hermes-home", str(tmp_path), "--job-id", "1ab4c7013fef", "--dry-run",
        ])

        assert exit_code == 2, "dry-run still reports the real verdict/exit code"
        assert not (tmp_path / "logs").exists(), "dry-run must not write under hermes-home"

    def test_healthy_run_never_writes_alert_log(self, tmp_path):
        now = time.time()
        _write_jobs(tmp_path, [{
            "id": "1ab4c7013fef", "enabled": True,
            "next_run_at": _iso(now + 3600),
        }])
        _write_heartbeat(tmp_path, age_seconds=5)

        exit_code = overdue_check.main([
            "--hermes-home", str(tmp_path), "--job-id", "1ab4c7013fef",
        ])

        assert exit_code == 0
        assert not (tmp_path / "logs").exists()


class TestCliSubprocess:
    def test_subprocess_healthy_exit_code_and_json(self, tmp_path):
        now = time.time()
        _write_jobs(tmp_path, [{
            "id": "1ab4c7013fef", "enabled": True,
            "next_run_at": _iso(now + 3600),
        }])
        _write_heartbeat(tmp_path, age_seconds=5)

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--hermes-home", str(tmp_path)],
            capture_output=True, text=True, check=False,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["verdict"] == "healthy"

    def test_subprocess_alarm_exit_code(self, tmp_path):
        now = time.time()
        _write_jobs(tmp_path, [{
            "id": "1ab4c7013fef", "enabled": True,
            "next_run_at": _iso(now - 3 * 3600),
        }])

        result = subprocess.run(
            [
                sys.executable, str(SCRIPT_PATH),
                "--hermes-home", str(tmp_path), "--dry-run",
            ],
            capture_output=True, text=True, check=False,
        )

        assert result.returncode == 2, result.stderr
        payload = json.loads(result.stdout)
        assert payload["alarm"] is True

    def test_subprocess_indeterminate_exit_code(self, tmp_path):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--hermes-home", str(tmp_path)],
            capture_output=True, text=True, check=False,
        )

        assert result.returncode == 1, result.stderr
        payload = json.loads(result.stdout)
        assert payload["verdict"] == "indeterminate"

    def test_default_job_id_matches_documented_default(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--help"],
            capture_output=True, text=True, check=True,
        )
        assert overdue_check.DEFAULT_JOB_ID in result.stdout


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
