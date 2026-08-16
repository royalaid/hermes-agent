"""U3 (lock-identity half, KTD9): the release script's exclusive lock gains
holder identity (holder, pid, started_at) and a stale-reclaim rule.

Test scenarios:
  - Busy refusal names the holder identity.
  - A corrupt/empty lock file refuses safely, reporting "unknown holder"
    rather than raising an unhandled exception.
  - A stale lock (dead pid, started_at older than the 6h grace window) is
    reclaimed, and the reclaim is logged.
  - A live-pid lock is never reclaimed, no matter its age.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts.fork_integration.release import mod as release

# A pid this large will not exist on any real Windows/POSIX host; psutil
# reports it as not-alive without raising.
_DEAD_PID = 999_999_999


def _write_lock(path: Path, *, holder: str, pid: int, started_at: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"holder": holder, "pid": pid, "started_at": started_at}), encoding="utf-8")


def _install_fail_spy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route fail() through a catchable exception carrying its message,
    instead of SystemExit + a real log()/print() write."""
    monkeypatch.setattr(release, "log", lambda message: None)

    def spy_fail(message: str, *, code: int = 1) -> None:
        raise RuntimeError(message)

    monkeypatch.setattr(release, "fail", spy_fail)


def test_busy_refusal_names_the_holder_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = tmp_path / "locks" / "hermes-integration-release.lock"
    started_at = datetime.now(timezone.utc).isoformat()
    _write_lock(lock_path, holder="investigator-session-42", pid=os.getpid(), started_at=started_at)
    monkeypatch.setattr(release, "LOCK_PATH", lock_path)
    _install_fail_spy(monkeypatch)

    with pytest.raises(RuntimeError) as excinfo:
        with release.exclusive_lock("scheduler"):
            pass  # pragma: no cover - must not be entered

    message = str(excinfo.value)
    assert "busy, held by investigator-session-42" in message
    assert f"pid {os.getpid()}" in message
    assert started_at in message
    # The held lock was not stolen or mutated by the refused attempt.
    assert json.loads(lock_path.read_text(encoding="utf-8"))["holder"] == "investigator-session-42"


def test_corrupt_lock_json_refuses_safely_as_unknown_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "locks" / "hermes-integration-release.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(release, "LOCK_PATH", lock_path)
    _install_fail_spy(monkeypatch)

    with pytest.raises(RuntimeError, match="unknown holder"):
        with release.exclusive_lock("scheduler"):
            pass  # pragma: no cover - must not be entered


def test_empty_lock_file_refuses_safely_as_unknown_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "locks" / "hermes-integration-release.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(release, "LOCK_PATH", lock_path)
    _install_fail_spy(monkeypatch)

    with pytest.raises(RuntimeError, match="unknown holder"):
        with release.exclusive_lock("scheduler"):
            pass  # pragma: no cover - must not be entered


def test_stale_dead_pid_lock_is_reclaimed_and_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "locks" / "hermes-integration-release.lock"
    old_started_at = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    _write_lock(lock_path, holder="orphaned-run", pid=_DEAD_PID, started_at=old_started_at)
    monkeypatch.setattr(release, "LOCK_PATH", lock_path)

    log_lines: list[str] = []
    monkeypatch.setattr(release, "log", lambda message: log_lines.append(message))

    def unexpected_fail(message: str, *, code: int = 1) -> None:
        raise AssertionError(f"fail() must not be called on a reclaimable stale lock: {message}")

    monkeypatch.setattr(release, "fail", unexpected_fail)

    with release.exclusive_lock("new-holder"):
        held = json.loads(lock_path.read_text(encoding="utf-8"))
        assert held["holder"] == "new-holder"
        assert held["pid"] == os.getpid()

    assert not lock_path.exists()  # released on exit, same as today
    assert any(
        "reclaiming stale lock held by orphaned-run" in line
        and f"pid {_DEAD_PID}" in line
        and old_started_at in line
        for line in log_lines
    ), log_lines


def test_live_pid_lock_is_never_reclaimed_regardless_of_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "locks" / "hermes-integration-release.lock"
    very_old_started_at = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
    _write_lock(lock_path, holder="long-running-holder", pid=os.getpid(), started_at=very_old_started_at)
    monkeypatch.setattr(release, "LOCK_PATH", lock_path)

    log_lines: list[str] = []
    monkeypatch.setattr(release, "log", lambda message: log_lines.append(message))
    _install_fail_spy_with_log(monkeypatch, log_lines)

    with pytest.raises(RuntimeError) as excinfo:
        with release.exclusive_lock("scheduler"):
            pass  # pragma: no cover - must not be entered

    assert "busy, held by long-running-holder" in str(excinfo.value)
    assert not any("reclaiming" in line for line in log_lines)
    # The 100h-old lock is untouched -- still owned by the live holder.
    assert json.loads(lock_path.read_text(encoding="utf-8"))["holder"] == "long-running-holder"


def _install_fail_spy_with_log(monkeypatch: pytest.MonkeyPatch, log_lines: list[str]) -> None:
    def spy_fail(message: str, *, code: int = 1) -> None:
        raise RuntimeError(message)

    monkeypatch.setattr(release, "fail", spy_fail)


def test_acquired_lock_writes_holder_pid_and_started_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh (uncontended) acquire stamps the full identity payload."""
    lock_path = tmp_path / "locks" / "hermes-integration-release.lock"
    monkeypatch.setattr(release, "LOCK_PATH", lock_path)

    before = datetime.now(timezone.utc)
    with release.exclusive_lock("manual-operator"):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))

    assert payload["holder"] == "manual-operator"
    assert payload["pid"] == os.getpid()
    started = datetime.fromisoformat(payload["started_at"])
    assert started.tzinfo is not None
    assert before <= started <= datetime.now(timezone.utc)


def test_exclusive_lock_default_holder_parameter_is_scheduler() -> None:
    import inspect

    signature = inspect.signature(release.exclusive_lock)
    assert signature.parameters["holder"].default == "scheduler"


def test_cli_default_holder_flows_from_main_into_exclusive_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No --holder on the command line -> main() acquires the lock as
    "scheduler" (the nightly run's identity)."""
    import sys
    from contextlib import contextmanager

    captured: dict[str, str] = {}

    @contextmanager
    def spy_lock(holder: str = "scheduler"):
        captured["holder"] = holder
        yield

    monkeypatch.setattr(release, "exclusive_lock", spy_lock)
    monkeypatch.setattr(
        release, "ensure_clean_identity",
        lambda: (_ for _ in ()).throw(RuntimeError("stop-after-lock-acquire")),
    )
    monkeypatch.setattr(sys, "argv", ["hermes-integration-release-windows.py"])
    _install_fail_spy(monkeypatch)
    monkeypatch.setattr(release, "launch_failure_investigator", lambda **kwargs: None)
    monkeypatch.setattr(release, "emit_fleet_receipt", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="stop-after-lock-acquire"):
        release.main()

    assert captured["holder"] == "scheduler"


def test_cli_explicit_holder_flows_into_exclusive_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """--holder <label> overrides the default for investigator/manual callers."""
    import sys
    from contextlib import contextmanager

    captured: dict[str, str] = {}

    @contextmanager
    def spy_lock(holder: str = "scheduler"):
        captured["holder"] = holder
        yield

    monkeypatch.setattr(release, "exclusive_lock", spy_lock)
    monkeypatch.setattr(
        release, "ensure_clean_identity",
        lambda: (_ for _ in ()).throw(RuntimeError("stop-after-lock-acquire")),
    )
    monkeypatch.setattr(sys, "argv", ["hermes-integration-release-windows.py", "--holder", "investigator-session-99"])
    _install_fail_spy(monkeypatch)
    monkeypatch.setattr(release, "launch_failure_investigator", lambda **kwargs: None)
    monkeypatch.setattr(release, "emit_fleet_receipt", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="stop-after-lock-acquire"):
        release.main()

    assert captured["holder"] == "investigator-session-99"
