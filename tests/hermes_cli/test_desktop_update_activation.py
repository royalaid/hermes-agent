import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from hermes_cli import desktop_update_activation as activation


INVOCATION = "invocation-0123456789abcdef"
LEASE = "lease-0123456789abcdef"
OLD_HEAD = "a" * 40
NEW_HEAD = "b" * 40


def _scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "hermes-agent"
    home = tmp_path / "hermes"
    runtime = root / ".hermes-runtime"
    live = root / "venv"
    candidate = runtime / "venv-candidate-12345678"
    live.mkdir(parents=True)
    candidate.mkdir(parents=True)
    (live / "Scripts").mkdir()
    (candidate / "Scripts").mkdir()
    (live / "Scripts" / "python.exe").write_bytes(b"old-python")
    (candidate / "Scripts" / "python.exe").write_bytes(b"new-python")
    home.mkdir()
    (live / "old.txt").write_text("old", encoding="utf-8")
    (candidate / "new.txt").write_text("new", encoding="utf-8")
    monkeypatch.setenv("HERMES_INTERNAL_DESKTOP_UPDATE_ROOT", str(root))
    monkeypatch.setenv("HERMES_INTERNAL_DESKTOP_UPDATE_HOME", str(home))
    monkeypatch.setenv("HERMES_INTERNAL_DESKTOP_UPDATE_INVOCATION", INVOCATION)
    monkeypatch.setenv("HERMES_INTERNAL_DESKTOP_UPDATE_LEASE", LEASE)
    return root, home, live, candidate


def _write_manifest(root: Path, home: Path, candidate: Path):
    return activation.write_activation_manifest(
        root,
        home=home,
        invocation_id=INVOCATION,
        lease_id=LEASE,
        candidate=candidate,
        pre_update_head=OLD_HEAD,
        branch="main",
        remote="origin",
        target_ref="refs/remotes/origin/main",
        target_sha=NEW_HEAD,
        python_health={
            "critical_syntax": True,
            "critical_imports": True,
            "dependencies": True,
        },
    )


def _install_fake_retry_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    current = [0.0]
    monkeypatch.setattr(activation.time, "monotonic", lambda: current[0])

    def advance(delay: float) -> None:
        current[0] = round(current[0] + delay, 6)

    monkeypatch.setattr(activation.time, "sleep", advance)
    return current


def _spawn_no_delete_share_holder(path: Path, seconds: float) -> subprocess.Popen[str]:
    helper = r"""
import ctypes
import sys
import time

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
create_file = kernel32.CreateFileW
create_file.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
create_file.restype = ctypes.c_void_p
close_handle = kernel32.CloseHandle
close_handle.argtypes = [ctypes.c_void_p]
close_handle.restype = ctypes.c_int
handle = create_file(sys.argv[1], 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
if handle == ctypes.c_void_p(-1).value:
    raise OSError(ctypes.get_last_error(), "CreateFileW failed")
print("READY", flush=True)
try:
    time.sleep(float(sys.argv[2]))
finally:
    close_handle(handle)
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", helper, str(path), str(seconds)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "READY"
    return holder


def _drain_holder(holder: subprocess.Popen[str]) -> None:
    if holder.poll() is not None:
        return
    try:
        holder.wait(timeout=3)
    except subprocess.TimeoutExpired:
        holder.terminate()
        holder.wait(timeout=3)


def test_activation_keeps_old_venv_until_commit(tmp_path, monkeypatch):
    root, home, live, candidate = _scope(tmp_path, monkeypatch)
    _write_manifest(root, home, candidate)
    monkeypatch.setattr(activation, "_git_head", lambda _root: NEW_HEAD)
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: True)

    activation.activate()

    state = json.loads((home / activation._STATE_NAME).read_text(encoding="utf-8"))
    backup = root / state["backup_rel"]
    assert (live / "new.txt").read_text(encoding="utf-8") == "new"
    assert (backup / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (home / activation._RECEIPT_NAME).exists()

    activation.publish_receipt()
    receipt = json.loads((home / activation._RECEIPT_NAME).read_text(encoding="utf-8"))
    assert receipt["resulting_head"] == NEW_HEAD
    assert all(receipt["health"].values())
    assert backup.exists(), "receipt publication must retain rollback material"

    activation.commit()
    assert not backup.exists()
    assert not (home / activation._MANIFEST_NAME).exists()
    assert not (home / activation._STATE_NAME).exists()
    assert (live / "new.txt").exists()


def test_failed_activation_restores_old_venv_without_receipt(tmp_path, monkeypatch):
    root, home, live, candidate = _scope(tmp_path, monkeypatch)
    _write_manifest(root, home, candidate)
    monkeypatch.setattr(activation, "_git_head", lambda _root: NEW_HEAD)
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: False)

    with pytest.raises(activation.ActivationError):
        activation.activate()

    assert (live / "old.txt").exists()
    assert not (home / activation._RECEIPT_NAME).exists()
    monkeypatch.setattr(activation, "_restore_source", lambda _root, _head: None)
    activation.rollback()
    assert not (home / activation._MANIFEST_NAME).exists()
    assert not (home / activation._STATE_NAME).exists()
    assert not list((root / ".hermes-runtime").glob("venv-rejected-*"))


def test_failed_candidate_swap_records_the_exact_durable_stage(tmp_path, monkeypatch):
    root, home, live, candidate = _scope(tmp_path, monkeypatch)
    _write_manifest(root, home, candidate)
    monkeypatch.setattr(activation, "_git_head", lambda _root: NEW_HEAD)
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: True)
    _install_fake_retry_clock(monkeypatch)
    real_replace = activation.os.replace

    def fail_candidate_swap(source, destination):
        if Path(source) == candidate and Path(destination) == live:
            raise PermissionError(13, "synthetic access denial")
        return real_replace(source, destination)

    monkeypatch.setattr(activation.os, "replace", fail_candidate_swap)

    with pytest.raises(PermissionError):
        activation.activate()

    state = json.loads((home / activation._STATE_NAME).read_text(encoding="utf-8"))
    assert state["phase"] == "candidate-move-failed-access"
    assert (live / "old.txt").exists()
    assert (candidate / "new.txt").exists()
    assert not list((root / ".hermes-runtime").glob("venv-rollback-*"))
    monkeypatch.setattr(activation, "_restore_source", lambda _root, _head: None)
    activation.rollback()
    assert not candidate.exists()
    assert not (home / activation._MANIFEST_NAME).exists()
    assert not (home / activation._STATE_NAME).exists()


def test_failed_live_swap_records_access_failure_without_mutation(tmp_path, monkeypatch):
    root, home, live, candidate = _scope(tmp_path, monkeypatch)
    _write_manifest(root, home, candidate)
    monkeypatch.setattr(activation, "_git_head", lambda _root: NEW_HEAD)
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: True)
    _install_fake_retry_clock(monkeypatch)
    real_replace = activation.os.replace

    def fail_live_swap(source, destination):
        if Path(source) == live:
            raise PermissionError(13, "synthetic access denial")
        return real_replace(source, destination)

    monkeypatch.setattr(activation.os, "replace", fail_live_swap)

    with pytest.raises(PermissionError):
        activation.activate()

    state = json.loads((home / activation._STATE_NAME).read_text(encoding="utf-8"))
    assert state["phase"] == "live-move-failed-access"
    assert state["move_error"] == {
        "attempts": 101,
        "elapsed_ms": 10_000,
        "reason": "access",
        "stage": "live",
        "system_errno": 13,
        "win32_error": None,
    }
    assert (live / "old.txt").exists()
    assert (candidate / "new.txt").exists()
    assert not list((root / ".hermes-runtime").glob("venv-rollback-*"))


def test_transient_candidate_swap_is_retried_without_losing_rollback(tmp_path, monkeypatch):
    root, home, live, candidate = _scope(tmp_path, monkeypatch)
    _write_manifest(root, home, candidate)
    monkeypatch.setattr(activation, "_git_head", lambda _root: NEW_HEAD)
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: True)
    _install_fake_retry_clock(monkeypatch)
    real_replace = activation.os.replace
    candidate_attempts = 0

    def fail_candidate_swap_once(source, destination):
        nonlocal candidate_attempts
        if Path(source) == candidate and Path(destination) == live:
            candidate_attempts += 1
            if candidate_attempts == 1:
                raise PermissionError("synthetic sharing violation")
        return real_replace(source, destination)

    monkeypatch.setattr(activation.os, "replace", fail_candidate_swap_once)

    activation.activate()

    state = json.loads((home / activation._STATE_NAME).read_text(encoding="utf-8"))
    backup = root / state["backup_rel"]
    assert state["phase"] == "active"
    assert candidate_attempts == 2
    assert (live / "new.txt").exists()
    assert (backup / "old.txt").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows delete-sharing semantics")
def test_live_swap_waits_for_external_no_delete_share_holder(tmp_path, monkeypatch):
    root, home, live, candidate = _scope(tmp_path, monkeypatch)
    _write_manifest(root, home, candidate)
    monkeypatch.setattr(activation, "_git_head", lambda _root: NEW_HEAD)
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: True)

    holder = _spawn_no_delete_share_holder(live / "old.txt", 6.25)
    try:
        started = time.monotonic()
        activation.activate()
        elapsed = time.monotonic() - started
        assert elapsed >= 6.0
        assert elapsed < 15.5
        state = json.loads((home / activation._STATE_NAME).read_text(encoding="utf-8"))
        backup = root / state["backup_rel"]
        assert state["phase"] == "active"
        assert (live / "new.txt").exists()
        assert (backup / "old.txt").exists()
    finally:
        _drain_holder(holder)


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows delete-sharing semantics")
def test_live_swap_holder_beyond_deadline_records_winerror_and_rolls_back(tmp_path, monkeypatch):
    root, home, live, candidate = _scope(tmp_path, monkeypatch)
    _write_manifest(root, home, candidate)
    monkeypatch.setattr(activation, "_git_head", lambda _root: NEW_HEAD)
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: True)

    holder = _spawn_no_delete_share_holder(live / "old.txt", 12.0)
    try:
        started = time.monotonic()
        with pytest.raises(PermissionError):
            activation.activate()
        elapsed = time.monotonic() - started
        assert 10.0 <= elapsed < 11.0
        state = json.loads((home / activation._STATE_NAME).read_text(encoding="utf-8"))
        diagnostics = state["move_error"]
        assert state["phase"] == "live-move-failed-access"
        assert diagnostics["stage"] == "live"
        assert diagnostics["reason"] == "access"
        assert diagnostics["win32_error"] == 5
        assert diagnostics["system_errno"] == 13
        assert 2 <= diagnostics["attempts"] <= activation._MAX_MOVE_ATTEMPTS
        assert 10_000 <= diagnostics["elapsed_ms"] <= 11_000
        assert (live / "old.txt").exists()
        assert (candidate / "new.txt").exists()
    finally:
        _drain_holder(holder)

    monkeypatch.setattr(activation, "_restore_source", lambda _root, _head: None)
    activation.rollback()
    assert (live / "old.txt").exists()
    assert not candidate.exists()
    assert not (home / activation._MANIFEST_NAME).exists()
    assert not (home / activation._STATE_NAME).exists()
    assert not list((root / ".hermes-runtime").glob("venv-rollback-*"))
    assert not list((root / ".hermes-runtime").glob("venv-rejected-*"))


def test_rollback_restores_prior_receipt_source_and_venv(tmp_path, monkeypatch):
    root, home, live, candidate = _scope(tmp_path, monkeypatch)
    prior = b'{"schema_version":0,"historical":true}'
    receipt_path = home / activation._RECEIPT_NAME
    receipt_path.write_bytes(prior)
    _write_manifest(root, home, candidate)
    monkeypatch.setattr(activation, "_git_head", lambda _root: NEW_HEAD)
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: True)
    restored = []
    monkeypatch.setattr(
        activation,
        "_restore_source",
        lambda _root, head: restored.append(head),
    )

    activation.activate()
    activation.publish_receipt()
    assert receipt_path.read_bytes() != prior
    activation.rollback()

    assert receipt_path.read_bytes() == prior
    assert (live / "old.txt").exists()
    assert restored == [OLD_HEAD]
    assert not (home / activation._MANIFEST_NAME).exists()
    assert not (home / activation._STATE_NAME).exists()


def test_publish_fails_closed_if_prior_receipt_changes(tmp_path, monkeypatch):
    root, home, _live, candidate = _scope(tmp_path, monkeypatch)
    receipt_path = home / activation._RECEIPT_NAME
    receipt_path.write_text('{"old":true}', encoding="utf-8")
    _write_manifest(root, home, candidate)
    monkeypatch.setattr(activation, "_git_head", lambda _root: NEW_HEAD)
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: True)
    activation.activate()
    receipt_path.write_text('{"changed":true}', encoding="utf-8")

    with pytest.raises(activation.ActivationError):
        activation.publish_receipt()

    assert json.loads(receipt_path.read_text(encoding="utf-8")) == {"changed": True}


def test_manifest_refuses_candidate_outside_runtime_root(tmp_path, monkeypatch):
    root, home, _live, _candidate = _scope(tmp_path, monkeypatch)
    outside = tmp_path / "outside" / "venv-candidate-12345678"
    outside.mkdir(parents=True)

    with pytest.raises(activation.ActivationError):
        _write_manifest(root, home, outside)


def test_rollback_before_activation_removes_exact_candidate(tmp_path, monkeypatch):
    root, home, _live, candidate = _scope(tmp_path, monkeypatch)
    _write_manifest(root, home, candidate)
    restored = []
    monkeypatch.setattr(
        activation,
        "_restore_source",
        lambda _root, head: restored.append(head),
    )

    activation.rollback()

    assert restored == [OLD_HEAD]
    assert not candidate.exists()
    assert not (home / activation._MANIFEST_NAME).exists()


def test_tampered_state_path_fails_before_rollback_mutation(tmp_path, monkeypatch):
    root, home, live, candidate = _scope(tmp_path, monkeypatch)
    _write_manifest(root, home, candidate)
    monkeypatch.setattr(activation, "_git_head", lambda _root: NEW_HEAD)
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: True)
    activation.activate()

    state_path = home / activation._STATE_NAME
    state = json.loads(state_path.read_text(encoding="utf-8"))
    real_backup = root / state["backup_rel"]
    state["backup_rel"] = ".hermes-runtime/unrelated-but-in-scope"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(activation.ActivationError):
        activation.rollback()

    assert (live / "new.txt").exists()
    assert (real_backup / "old.txt").exists()


def test_commit_refuses_dirty_source_and_retains_rollback(tmp_path, monkeypatch):
    root, home, _live, candidate = _scope(tmp_path, monkeypatch)
    _write_manifest(root, home, candidate)
    monkeypatch.setattr(activation, "_git_head", lambda _root: NEW_HEAD)
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: True)
    activation.activate()
    activation.publish_receipt()
    state = json.loads((home / activation._STATE_NAME).read_text(encoding="utf-8"))
    backup = root / state["backup_rel"]
    monkeypatch.setattr(activation, "_git_clean", lambda _root: False)

    with pytest.raises(activation.ActivationError):
        activation.commit()

    assert backup.exists()
    assert (home / activation._MANIFEST_NAME).exists()
    assert (home / activation._STATE_NAME).exists()
