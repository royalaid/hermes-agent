import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from hermes_cli import desktop_update_activation as activation


INVOCATION = "invocation-0123456789abcdef"
LEASE = "lease-0123456789abcdef"
ATTEMPT = "attempt-0123456789abcdef"
OLD_HEAD = "a" * 40
NEW_HEAD = "b" * 40


def _journal_lease(
    root: Path,
    *,
    lease_id: str,
    owner_pid: int,
    created_at: int,
) -> dict:
    return {
        "schema_version": 1,
        "lease_id": lease_id,
        "owner_pid": owner_pid,
        "created_at": created_at,
        "expires_at": created_at + 120,
        "handoff_grace_until": created_at + 60,
        "install_root": str(root.resolve()),
    }


def _write_lease_marker(path: Path, lease: dict) -> None:
    path.write_text(json.dumps(lease, sort_keys=True), encoding="utf-8")


def _scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from hermes_cli import update_cmd

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
    monkeypatch.setenv("HERMES_INTERNAL_DESKTOP_UPDATE_ATTEMPT", ATTEMPT)
    monkeypatch.setattr(
        update_cmd,
        "_node_dependencies_healthy_read_only",
        lambda: True,
    )
    monkeypatch.setattr(activation, "_git_branch", lambda _root: "main")
    return root, home, live, candidate


def _write_manifest(
    root: Path,
    home: Path,
    candidate: Path,
    *,
    provisioned_generation: Path | None = None,
):
    return activation.write_activation_manifest(
        root,
        home=home,
        invocation_id=INVOCATION,
        lease_id=LEASE,
        candidate=candidate,
        provisioned_generation=provisioned_generation,
        pre_update_head=OLD_HEAD,
        pre_update_branch="main",
        selected_pre_head=OLD_HEAD,
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


def _write_correlated_staging_journal(root: Path, home: Path) -> None:
    lease = _journal_lease(
        root,
        lease_id=LEASE,
        owner_pid=os.getpid(),
        created_at=100,
    )
    activation.write_staging_journal(
        root,
        home=home,
        invocation_id=INVOCATION,
        lease=lease,
        pre_update_head=OLD_HEAD,
        pre_update_branch="main",
        branch="main",
        selected_pre_head=OLD_HEAD,
        target_head=NEW_HEAD,
    )


def _write_desktop_health(root: Path, home: Path, *, branch: str = "main") -> Path:
    path = home / activation._DESKTOP_HEALTH_NAME
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "invocation_id": INVOCATION,
                "lease_id": LEASE,
                "root": str(root.resolve()),
                "target_head": NEW_HEAD,
                "branch": branch,
                "build_exit_code": 0,
                "node_dependencies": True,
                "desktop_rebuild": True,
                "created_at": int(time.time()),
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return path


def _write_commit_proposal(root: Path, home: Path) -> tuple[Path, Path]:
    manifest_path = home / activation._MANIFEST_NAME
    state_path = home / activation._STATE_NAME
    receipt_path = home / activation._RECEIPT_NAME
    ack_path = home / f".hermes-update-ack-{ATTEMPT}.json"
    plan_path = home / f".hermes-gateway-resume-{INVOCATION}.prepared"
    runtime_path = home / (
        f".hermes-gateway-resume-{INVOCATION}.prepared-runtime.json"
    )
    result_path = home / ".hermes-update-result.json"
    desktop_executable = (
        root / "apps" / "desktop" / "release" / "win-unpacked" / "Hermes.exe"
    )
    desktop_executable.parent.mkdir(parents=True, exist_ok=True)
    desktop_executable.write_bytes(b"MZ")
    acknowledged_at = int(time.time())
    desktop_identity = {
        "pid": os.getpid(),
        "process_started_at": acknowledged_at - 1,
        "root": str(root.resolve()),
        "executable": str(desktop_executable.resolve()),
        "build_id": NEW_HEAD,
        "acknowledged_at": acknowledged_at,
    }
    ack = {
        "schema_version": 1,
        "attempt_id": ATTEMPT,
        "invocation_id": INVOCATION,
        "lease_id": LEASE,
        **desktop_identity,
        "build_source": "install-stamp",
        "backend_ready": True,
        "backend_mode": "local",
        "error": None,
    }
    ack_path.write_bytes(activation._json_bytes(ack))

    created_at = int(time.time())
    plan_unsigned = {
        "schema_version": 1,
        "invocation_id": INVOCATION,
        "lease_fingerprint": activation.hashlib.sha256(LEASE.encode()).hexdigest(),
        "install_root": str(root.resolve()),
        "created_at": created_at,
        "expires_at": created_at + 3600,
        "profiles": [],
        "cold_start_if_installed": False,
    }
    plan = {
        **plan_unsigned,
        "auth": activation._commit_document_auth(plan_unsigned, LEASE),
    }
    plan_path.write_bytes(activation._json_bytes(plan))
    runtime_unsigned = {
        "schema_version": 1,
        "invocation_id": INVOCATION,
        "install_root": str(root.resolve()),
        "plan_sha256": activation._digest(plan_path.read_bytes()),
        "runtimes": [],
    }
    runtime = {
        **runtime_unsigned,
        "auth": activation._commit_document_auth(runtime_unsigned, LEASE),
    }
    runtime_path.write_bytes(activation._json_bytes(runtime))
    pending = {
        "schema_version": 2,
        "attempt_id": ATTEMPT,
        "state": "pending",
        "ok": False,
        "exit_code": None,
        "branch": "main",
        "invocation_id": INVOCATION,
        "lease_id": LEASE,
        "root": str(root.resolve()),
    }
    result_path.write_bytes(activation._json_bytes(pending))

    unsigned = {
        "schema_version": 1,
        "revision": 1,
        "decision": "commit",
        "attempt_id": ATTEMPT,
        "invocation_id": INVOCATION,
        "lease_id": LEASE,
        "root": str(root.resolve()),
        "target_head": NEW_HEAD,
        "desktop_ack_sha256": activation._digest(ack_path.read_bytes()),
        "desktop_identity": desktop_identity,
        "activation_manifest_sha256": activation._digest(manifest_path.read_bytes()),
        "activation_state_sha256": activation._digest(state_path.read_bytes()),
        "update_receipt_sha256": activation._digest(receipt_path.read_bytes()),
        "gateway_plan_sha256": activation._digest(plan_path.read_bytes()),
        "gateway_runtime_manifest_sha256": activation._digest(runtime_path.read_bytes()),
        "gateway_runtimes": [],
        "pending_result_sha256": activation._digest(result_path.read_bytes()),
        "created_at": int(time.time()),
    }
    proposal = {
        **unsigned,
        "auth": activation._commit_document_auth(unsigned, LEASE),
    }
    proposal_path = home / activation._COMMIT_PROPOSAL_NAME
    proposal_path.write_bytes(activation._json_bytes(proposal))
    return proposal_path, home / activation._COMMIT_COORDINATOR_NAME


def test_commit_decision_is_irreversible_and_binds_exact_artifacts(
    tmp_path, monkeypatch
):
    root, home, _live, candidate = _scope(tmp_path, monkeypatch)
    _write_manifest(root, home, candidate)
    monkeypatch.setattr(activation, "_git_head", lambda _root: NEW_HEAD)
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: True)
    activation.activate()
    _write_desktop_health(root, home)
    activation.publish_receipt()
    proposal_path, coordinator_path = _write_commit_proposal(root, home)

    activation.commit_decided()

    assert not proposal_path.exists()
    coordinator_raw = coordinator_path.read_bytes()
    assert coordinator_raw
    with pytest.raises(activation.ActivationError, match="rollback is forbidden"):
        activation.rollback()
    activation.commit()
    assert coordinator_path.read_bytes() == coordinator_raw


def test_commit_decision_replays_after_crash_between_publish_and_proposal_consume(
    tmp_path, monkeypatch
):
    root, home, _live, candidate = _scope(tmp_path, monkeypatch)
    _write_manifest(root, home, candidate)
    monkeypatch.setattr(activation, "_git_head", lambda _root: NEW_HEAD)
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: True)
    activation.activate()
    _write_desktop_health(root, home)
    activation.publish_receipt()
    proposal_path, coordinator_path = _write_commit_proposal(root, home)
    real_unlink = activation._unlink_exact
    failed = False

    def crash_after_publish(path, raw):
        nonlocal failed
        if Path(path) == proposal_path and not failed:
            failed = True
            raise OSError("injected decision crash")
        return real_unlink(path, raw)

    monkeypatch.setattr(activation, "_unlink_exact", crash_after_publish)
    with pytest.raises(OSError, match="injected decision crash"):
        activation.commit_decided()
    coordinator_raw = coordinator_path.read_bytes()
    publishing_path = activation._commit_coordinator_publishing_path(home, ATTEMPT)
    publishing_path.write_bytes(coordinator_raw)

    activation.commit_decided()

    assert coordinator_path.read_bytes() == coordinator_raw
    assert not proposal_path.exists()
    assert not publishing_path.exists()


@pytest.mark.parametrize("payload", [b"{", b"{}"])
def test_any_existing_commit_coordinator_forbids_rollback(
    tmp_path, monkeypatch, payload
):
    root, home, _live, candidate = _scope(tmp_path, monkeypatch)
    _write_manifest(root, home, candidate)
    coordinator_path = home / activation._COMMIT_COORDINATOR_NAME
    coordinator_path.write_bytes(payload)

    with pytest.raises(activation.ActivationError, match="rollback is forbidden"):
        activation.rollback()
    with pytest.raises(activation.ActivationError, match="rollback is forbidden"):
        activation.rollback_source_only()

    assert candidate.exists()
    assert coordinator_path.read_bytes() == payload


def test_commit_finishes_after_receipt_without_external_coordinator(
    tmp_path, monkeypatch
):
    root, home, _live, candidate = _scope(tmp_path, monkeypatch)
    _write_manifest(root, home, candidate)
    monkeypatch.setattr(activation, "_git_head", lambda _root: NEW_HEAD)
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: True)
    activation.activate()
    _write_desktop_health(root, home)
    activation.publish_receipt()
    state = json.loads((home / activation._STATE_NAME).read_text(encoding="utf-8"))
    backup = root / state["backup_rel"]

    activation.commit()

    assert not backup.exists()
    assert (root / "venv" / "new.txt").is_file()
    assert not (home / activation._MANIFEST_NAME).exists()
    assert not (home / activation._STATE_NAME).exists()


def test_commit_decided_is_not_a_public_activation_action(capsys):
    assert activation.main(["commit-decided"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "FAILED\n"


def test_commit_decision_rejects_authenticated_noninitial_revision(
    tmp_path, monkeypatch
):
    root, home, _live, candidate = _scope(tmp_path, monkeypatch)
    _write_manifest(root, home, candidate)
    monkeypatch.setattr(activation, "_git_head", lambda _root: NEW_HEAD)
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: True)
    activation.activate()
    _write_desktop_health(root, home)
    activation.publish_receipt()
    proposal_path, coordinator_path = _write_commit_proposal(root, home)
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    proposal["revision"] = 2
    unsigned = {key: value for key, value in proposal.items() if key != "auth"}
    proposal["auth"] = activation._commit_document_auth(unsigned, LEASE)
    proposal_path.write_bytes(activation._json_bytes(proposal))

    with pytest.raises(activation.ActivationError, match="identity is invalid"):
        activation.commit_decided()

    assert not coordinator_path.exists()


@pytest.mark.parametrize(
    "artifact",
    ["ack", "manifest", "state", "receipt", "plan", "runtime", "result"],
)
def test_commit_decision_rejects_changed_exact_artifact_before_publication(
    tmp_path, monkeypatch, artifact
):
    root, home, _live, candidate = _scope(tmp_path, monkeypatch)
    _write_manifest(root, home, candidate)
    monkeypatch.setattr(activation, "_git_head", lambda _root: NEW_HEAD)
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: True)
    activation.activate()
    _write_desktop_health(root, home)
    activation.publish_receipt()
    proposal_path, coordinator_path = _write_commit_proposal(root, home)
    paths = {
        "ack": home / f".hermes-update-ack-{ATTEMPT}.json",
        "manifest": home / activation._MANIFEST_NAME,
        "state": home / activation._STATE_NAME,
        "receipt": home / activation._RECEIPT_NAME,
        "plan": home / f".hermes-gateway-resume-{INVOCATION}.prepared",
        "runtime": home
        / f".hermes-gateway-resume-{INVOCATION}.prepared-runtime.json",
        "result": home / ".hermes-update-result.json",
    }
    paths[artifact].write_bytes(paths[artifact].read_bytes() + b" ")

    with pytest.raises(activation.ActivationError):
        activation.commit_decided()

    assert proposal_path.exists()
    assert not coordinator_path.exists()


def test_manifest_preserves_valid_slash_branch(tmp_path, monkeypatch):
    root, home, _live, candidate = _scope(tmp_path, monkeypatch)
    activation.write_activation_manifest(
        root,
        home=home,
        invocation_id=INVOCATION,
        lease_id=LEASE,
        candidate=candidate,
        provisioned_generation=None,
        pre_update_head=OLD_HEAD,
        pre_update_branch="main",
        selected_pre_head=None,
        branch="codex/disposable-fork-integration",
        remote="origin",
        target_ref="refs/remotes/origin/codex/disposable-fork-integration",
        target_sha=NEW_HEAD,
        python_health={
            "critical_syntax": True,
            "critical_imports": True,
            "dependencies": True,
        },
    )

    manifest, _raw = activation._validated_manifest(
        root, home, INVOCATION, LEASE
    )
    assert manifest["branch"] == "codex/disposable-fork-integration"


@pytest.mark.parametrize(
    "branch",
    [
        "../escape",
        "codex//double",
        "codex/../escape",
        "codex/@{stale}",
        "codex/bad.lock/next",
        "codex/trailing.",
        "codex\\windows",
        "-dash",
        "@",
    ],
)
def test_manifest_rejects_malformed_git_branch_before_write(
    tmp_path, monkeypatch, branch
):
    root, home, _live, candidate = _scope(tmp_path, monkeypatch)

    with pytest.raises(activation.ActivationError, match="target identity"):
        activation.write_activation_manifest(
            root,
            home=home,
            invocation_id=INVOCATION,
            lease_id=LEASE,
            candidate=candidate,
            provisioned_generation=None,
            pre_update_head=OLD_HEAD,
            pre_update_branch="main",
            selected_pre_head=OLD_HEAD,
            branch=branch,
            remote="origin",
            target_ref=f"refs/remotes/origin/{branch}",
            target_sha=NEW_HEAD,
            python_health={
                "critical_syntax": True,
                "critical_imports": True,
                "dependencies": True,
            },
        )

    assert not (home / ".hermes-update-activation.json").exists()


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

    _write_desktop_health(root, home)
    activation.publish_receipt()
    receipt = json.loads((home / activation._RECEIPT_NAME).read_text(encoding="utf-8"))
    assert receipt["resulting_head"] == NEW_HEAD
    assert all(receipt["health"].values())
    assert backup.exists(), "receipt publication must retain rollback material"

    _write_commit_proposal(root, home)
    activation.commit_decided()
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
    monkeypatch.setattr(activation, "_restore_source", lambda _root, **_claims: None)
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
    monkeypatch.setattr(activation, "_restore_source", lambda _root, **_claims: None)
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

    monkeypatch.setattr(activation, "_restore_source", lambda _root, **_claims: None)
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
        lambda _root, **claims: restored.append(claims["pre_update_head"]),
    )

    activation.activate()
    _write_desktop_health(root, home)
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
        lambda _root, **claims: restored.append(claims["pre_update_head"]),
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
    _write_desktop_health(root, home)
    activation.publish_receipt()
    state = json.loads((home / activation._STATE_NAME).read_text(encoding="utf-8"))
    backup = root / state["backup_rel"]
    _write_commit_proposal(root, home)
    activation.commit_decided()
    monkeypatch.setattr(activation, "_git_clean", lambda _root: False)

    with pytest.raises(activation.ActivationError):
        activation.commit()

    assert backup.exists()
    assert (home / activation._MANIFEST_NAME).exists()
    assert (home / activation._STATE_NAME).exists()


@pytest.mark.parametrize(
    "failure_edge",
    ["backup", "desktop-backup", "health", "staging", "manifest", "state"],
)
def test_commit_cleanup_resumes_exactly_after_each_failure_edge(
    tmp_path, monkeypatch, failure_edge
):
    root, home, live, candidate = _scope(tmp_path, monkeypatch)
    _write_correlated_staging_journal(root, home)
    _write_manifest(root, home, candidate)
    monkeypatch.setattr(activation, "_git_head", lambda _root: NEW_HEAD)
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: True)
    activation.activate()
    _write_desktop_health(root, home)
    activation.publish_receipt()
    _write_commit_proposal(root, home)
    activation.commit_decided()
    state = json.loads((home / activation._STATE_NAME).read_text(encoding="utf-8"))
    backup = root / state["backup_rel"]
    failed = False
    real_remove_tree = activation._remove_tree_exact
    real_remove_health = activation._remove_desktop_health
    real_unlink = activation._unlink_exact

    def fail_tree(path, **kwargs):
        nonlocal failed
        edge = (
            "backup"
            if Path(path).name.startswith("venv-rollback-")
            else "desktop-backup"
        )
        if not failed and failure_edge == edge:
            failed = True
            raise OSError("injected commit cleanup failure")
        return real_remove_tree(path, **kwargs)

    def fail_health(*args, **kwargs):
        nonlocal failed
        if not failed and failure_edge == "health":
            failed = True
            raise OSError("injected commit health cleanup failure")
        return real_remove_health(*args, **kwargs)

    def fail_unlink(path, raw):
        nonlocal failed
        edge = {
            activation._STAGING_NAME: "staging",
            activation._MANIFEST_NAME: "manifest",
            activation._STATE_NAME: "state",
        }.get(Path(path).name)
        if not failed and failure_edge == edge:
            failed = True
            raise OSError("injected commit protocol cleanup failure")
        return real_unlink(path, raw)

    monkeypatch.setattr(activation, "_remove_tree_exact", fail_tree)
    monkeypatch.setattr(activation, "_remove_desktop_health", fail_health)
    monkeypatch.setattr(activation, "_unlink_exact", fail_unlink)

    with pytest.raises(OSError, match="injected commit"):
        activation.commit()

    persisted = json.loads((home / activation._STATE_NAME).read_text(encoding="utf-8"))
    assert persisted["phase"] == "commit-cleaning"
    activation.commit()
    assert not backup.exists()
    assert (live / "new.txt").is_file()
    assert not (home / activation._MANIFEST_NAME).exists()
    assert not (home / activation._STATE_NAME).exists()
    assert not (home / activation._STAGING_NAME).exists()


@pytest.mark.parametrize(
    "failure_edge",
    [
        "desktop-restore",
        "source-restore",
        "generation",
        "health",
        "staging",
        "manifest",
        "state",
    ],
)
def test_rollback_cleanup_resumes_exactly_after_each_failure_edge(
    tmp_path, monkeypatch, failure_edge
):
    root, home, live, candidate = _scope(tmp_path, monkeypatch)
    generation = root / ".hermes-runtime" / "python" / "generation-12345678"
    generation.mkdir(parents=True)
    _write_correlated_staging_journal(root, home)
    _write_manifest(
        root,
        home,
        candidate,
        provisioned_generation=generation,
    )
    source_head = [NEW_HEAD]
    monkeypatch.setattr(activation, "_git_head", lambda _root: source_head[0])
    monkeypatch.setattr(activation, "_git_clean", lambda _root: True)
    monkeypatch.setattr(activation, "_smoke_live", lambda _venv, _root: True)
    activation.activate()
    _write_desktop_health(root, home)
    activation.publish_receipt()
    failed = False
    real_restore_desktop = activation._restore_desktop_backup
    real_remove_generation = activation._remove_generation_claim
    real_remove_health = activation._remove_desktop_health
    real_unlink = activation._unlink_exact

    def fail_desktop(*args, **kwargs):
        nonlocal failed
        if not failed and failure_edge == "desktop-restore":
            failed = True
            raise OSError("injected rollback Desktop restore failure")
        return real_restore_desktop(*args, **kwargs)

    def restore_source(*_args, **_kwargs):
        nonlocal failed
        if not failed and failure_edge == "source-restore":
            failed = True
            raise OSError("injected rollback source restore failure")
        source_head[0] = OLD_HEAD

    def fail_generation(*args, **kwargs):
        nonlocal failed
        if not failed and failure_edge == "generation":
            failed = True
            raise OSError("injected rollback generation cleanup failure")
        return real_remove_generation(*args, **kwargs)

    def fail_health(*args, **kwargs):
        nonlocal failed
        if not failed and failure_edge == "health":
            failed = True
            raise OSError("injected rollback health cleanup failure")
        return real_remove_health(*args, **kwargs)

    def fail_unlink(path, raw):
        nonlocal failed
        edge = {
            activation._STAGING_NAME: "staging",
            activation._MANIFEST_NAME: "manifest",
            activation._STATE_NAME: "state",
        }.get(Path(path).name)
        if not failed and failure_edge == edge:
            failed = True
            raise OSError("injected rollback protocol cleanup failure")
        return real_unlink(path, raw)

    monkeypatch.setattr(activation, "_restore_desktop_backup", fail_desktop)
    monkeypatch.setattr(activation, "_restore_source", restore_source)
    monkeypatch.setattr(activation, "_remove_generation_claim", fail_generation)
    monkeypatch.setattr(activation, "_remove_desktop_health", fail_health)
    monkeypatch.setattr(activation, "_unlink_exact", fail_unlink)

    with pytest.raises(OSError, match="injected rollback"):
        activation.rollback()

    if failure_edge in {"desktop-restore", "source-restore", "generation"}:
        assert generation.exists(), "rollback authority must survive pre-cleanup failure"
    if (home / activation._STATE_NAME).exists():
        persisted = json.loads(
            (home / activation._STATE_NAME).read_text(encoding="utf-8")
        )
        if failure_edge in {"desktop-restore", "source-restore"}:
            assert persisted["phase"] == "receipt-published"
        else:
            assert persisted["phase"].startswith("rollback-cleaning-")
    activation.rollback()
    assert (live / "old.txt").is_file()
    assert not generation.exists()
    assert not (home / activation._MANIFEST_NAME).exists()
    assert not (home / activation._STATE_NAME).exists()
    assert not (home / activation._STAGING_NAME).exists()


@pytest.mark.parametrize(
    ("field", "kind", "relative"),
    [
        ("candidate", "candidate", ".hermes-runtime/venv-candidate-absent12"),
        (
            "provisioned_generation",
            "generation",
            ".hermes-runtime/python/generation-absent12",
        ),
    ],
)
def test_absent_staging_artifact_rejects_malformed_identity_before_recovery(
    tmp_path, monkeypatch, field, kind, relative
):
    root, home, _live, _candidate = _scope(tmp_path, monkeypatch)
    lease = _journal_lease(
        root,
        lease_id=LEASE,
        owner_pid=os.getpid(),
        created_at=100,
    )
    activation.write_staging_journal(
        root,
        home=home,
        invocation_id=INVOCATION,
        lease=lease,
        pre_update_head=OLD_HEAD,
        pre_update_branch="main",
        branch="main",
        selected_pre_head=OLD_HEAD,
        target_head=NEW_HEAD,
    )
    journal_path = home / activation._STAGING_NAME
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    claim = {"rel": relative, "identity_sha256": "malformed"}
    journal[field] = claim
    journal_path.write_bytes(activation._json_bytes(journal))
    restored = []
    monkeypatch.setattr(
        activation,
        "_restore_source",
        lambda *_args, **_kwargs: restored.append(True),
    )

    with pytest.raises(activation.ActivationError, match="claim is invalid"):
        activation.recover_staging_journal(root, home, INVOCATION, LEASE)
    with pytest.raises(activation.ActivationError, match="claim is invalid"):
        activation._remove_staging_artifact(root, claim, kind, INVOCATION, LEASE)

    assert restored == []
    assert journal_path.exists()


@pytest.mark.parametrize("identity", [None, "a" * 64])
@pytest.mark.parametrize(
    ("kind", "relative"),
    [
        ("candidate", ".hermes-runtime/venv-candidate-absent12"),
        ("generation", ".hermes-runtime/python/generation-absent12"),
    ],
)
def test_absent_staging_artifact_accepts_none_or_valid_digest(
    tmp_path, kind, relative, identity
):
    root = tmp_path / "hermes-agent"
    root.mkdir()
    claim = {"rel": relative, "identity_sha256": identity}

    path = activation._validated_staging_artifact(
        root, claim, kind, INVOCATION, LEASE
    )

    assert path == root / relative


def test_coordinator_only_stale_recovery_refuses_without_parsing_or_deleting(
    tmp_path,
):
    root = tmp_path / "hermes-agent"
    home = tmp_path / "hermes"
    root.mkdir()
    home.mkdir()
    coordinator = home / activation._COMMIT_COORDINATOR_NAME
    opaque = b"opaque-not-json\x00coordinator"
    coordinator.write_bytes(opaque)

    with pytest.raises(activation.ActivationError, match="commit coordinator"):
        activation.recover_stale_transaction_journals(
            root,
            home=home,
            current_lease={},
        )

    assert coordinator.read_bytes() == opaque
    assert sorted(path.name for path in home.iterdir()) == [coordinator.name]


@pytest.mark.parametrize("publisher", ["acquisition", "staging", "manifest"])
def test_commit_coordinator_blocks_every_transaction_publisher_before_write(
    tmp_path, monkeypatch, publisher
):
    root, home, _live, candidate = _scope(tmp_path, monkeypatch)
    coordinator = home / activation._COMMIT_COORDINATOR_NAME
    opaque = b"opaque-coordinator-authority"
    coordinator.write_bytes(opaque)
    lease = _journal_lease(
        root,
        lease_id=LEASE,
        owner_pid=os.getpid(),
        created_at=100,
    )

    with pytest.raises(activation.ActivationError, match="commit coordinator"):
        if publisher == "acquisition":
            (home / "tmp").mkdir()
            activation.write_acquisition_journal(
                root,
                home=home,
                invocation_id=INVOCATION,
                lease=lease,
                workspace=home / "tmp" / ("update-acquisition-" + "a" * 24),
            )
        elif publisher == "staging":
            activation.write_staging_journal(
                root,
                home=home,
                invocation_id=INVOCATION,
                lease=lease,
                pre_update_head=OLD_HEAD,
                pre_update_branch="main",
                branch="main",
                selected_pre_head=OLD_HEAD,
                target_head=NEW_HEAD,
            )
        else:
            _write_manifest(root, home, candidate)

    assert coordinator.read_bytes() == opaque
    assert not (home / activation._ACQUISITION_NAME).exists()
    assert not (home / activation._STAGING_NAME).exists()
    assert not (home / activation._MANIFEST_NAME).exists()


@pytest.mark.windows_only
def test_restart_recovers_dead_acquisition_authority_and_stays_absent(
    tmp_path, monkeypatch
):
    import hermes_mcp_update_gate as gate

    root = tmp_path / "hermes-agent"
    home = tmp_path / "hermes"
    root.mkdir()
    (home / "tmp").mkdir(parents=True)
    old = _journal_lease(
        root,
        lease_id="lease-old-acquisition-123456",
        owner_pid=71001,
        created_at=100,
    )
    workspace = home / "tmp" / ("update-acquisition-" + "a" * 24)
    activation.write_acquisition_journal(
        root,
        home=home,
        invocation_id="invocation-old-acquisition-123456",
        lease=old,
        workspace=workspace,
    )
    workspace.mkdir()
    activation.bind_acquisition_workspace(
        root,
        home=home,
        invocation_id="invocation-old-acquisition-123456",
        lease_id=old["lease_id"],
    )
    (workspace / "evidence.txt").write_text("owned", encoding="utf-8")

    current = _journal_lease(
        root,
        lease_id="lease-current-acquisition-123456",
        owner_pid=os.getpid(),
        created_at=300,
    )
    marker = home / ".quiesce-lease.json"
    _write_lease_marker(marker, current)
    monkeypatch.setattr(gate, "marker_path", lambda *_args, **_kwargs: marker)

    activation.recover_stale_transaction_journals(
        root,
        home=home,
        current_lease=current,
        now=320,
        pid_alive=lambda pid: pid == os.getpid(),
        pid_create_time=lambda pid: 299.0 if pid == os.getpid() else None,
    )

    assert not workspace.exists()
    assert not (home / activation._ACQUISITION_NAME).exists()
    time.sleep(7.5)
    assert not workspace.exists()
    assert not (home / activation._ACQUISITION_NAME).exists()


@pytest.mark.windows_only
def test_restart_recovers_dead_staging_authority_with_exact_source_and_artifacts(
    tmp_path, monkeypatch
):
    import hermes_mcp_update_gate as gate

    root = tmp_path / "hermes-agent"
    home = tmp_path / "hermes"
    home.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Hermes Test"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    (root / ".gitignore").write_text(".hermes-runtime/\n", encoding="utf-8")
    (root / "source.txt").write_text("exact", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "source"], check=True)
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    candidate = root / ".hermes-runtime" / "venv-candidate-12345678"
    generation = root / ".hermes-runtime" / "python" / "generation-12345678"
    candidate.mkdir(parents=True)
    generation.mkdir(parents=True)
    old = _journal_lease(
        root,
        lease_id="lease-old-staging-123456",
        owner_pid=71002,
        created_at=100,
    )
    activation.write_staging_journal(
        root,
        home=home,
        invocation_id="invocation-old-staging-123456",
        lease=old,
        pre_update_head=head,
        pre_update_branch="main",
        branch="main",
        selected_pre_head=head,
        target_head=head,
    )
    activation.update_staging_journal(
        root,
        home=home,
        invocation_id="invocation-old-staging-123456",
        lease_id=old["lease_id"],
        phase="candidate-staging",
        candidate=candidate,
        provisioned_generation=generation,
    )
    current = _journal_lease(
        root,
        lease_id="lease-current-staging-123456",
        owner_pid=os.getpid(),
        created_at=300,
    )
    marker = home / ".quiesce-lease.json"
    _write_lease_marker(marker, current)
    monkeypatch.setattr(gate, "marker_path", lambda *_args, **_kwargs: marker)

    activation.recover_stale_transaction_journals(
        root,
        home=home,
        current_lease=current,
        now=320,
        pid_alive=lambda pid: pid == os.getpid(),
        pid_create_time=lambda pid: 299.0 if pid == os.getpid() else None,
    )

    assert not candidate.exists()
    assert not generation.exists()
    assert not (home / activation._STAGING_NAME).exists()
    assert subprocess.run(
        ["git", "-C", str(root), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "main"
    assert subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == head


@pytest.mark.windows_only
def test_restart_refuses_timestamp_only_recovery_while_old_owner_is_live(
    tmp_path, monkeypatch
):
    import hermes_mcp_update_gate as gate

    root = tmp_path / "hermes-agent"
    home = tmp_path / "hermes"
    root.mkdir()
    (home / "tmp").mkdir(parents=True)
    old = _journal_lease(
        root,
        lease_id="lease-live-old-authority-123456",
        owner_pid=71003,
        created_at=100,
    )
    workspace = home / "tmp" / ("update-acquisition-" + "b" * 24)
    activation.write_acquisition_journal(
        root,
        home=home,
        invocation_id="invocation-live-old-authority-123456",
        lease=old,
        workspace=workspace,
    )
    workspace.mkdir()
    activation.bind_acquisition_workspace(
        root,
        home=home,
        invocation_id="invocation-live-old-authority-123456",
        lease_id=old["lease_id"],
    )
    current = _journal_lease(
        root,
        lease_id="lease-current-live-check-123456",
        owner_pid=os.getpid(),
        created_at=300,
    )
    marker = home / ".quiesce-lease.json"
    _write_lease_marker(marker, current)
    monkeypatch.setattr(gate, "marker_path", lambda *_args, **_kwargs: marker)

    with pytest.raises(activation.ActivationError, match="still live"):
        activation.recover_stale_transaction_journals(
            root,
            home=home,
            current_lease=current,
            now=10_000,
            pid_alive=lambda _pid: True,
            pid_create_time=lambda pid: 50.0 if pid == old["owner_pid"] else 299.0,
        )

    assert workspace.exists()
    assert (home / activation._ACQUISITION_NAME).exists()


@pytest.mark.windows_only
def test_restart_refuses_foreign_current_lease_and_replaced_artifact(
    tmp_path, monkeypatch
):
    import hermes_mcp_update_gate as gate

    root = tmp_path / "hermes-agent"
    home = tmp_path / "hermes"
    root.mkdir()
    (home / "tmp").mkdir(parents=True)
    old = _journal_lease(
        root,
        lease_id="lease-old-foreign-check-123456",
        owner_pid=71004,
        created_at=100,
    )
    workspace = home / "tmp" / ("update-acquisition-" + "c" * 24)
    activation.write_acquisition_journal(
        root,
        home=home,
        invocation_id="invocation-old-foreign-check-123456",
        lease=old,
        workspace=workspace,
    )
    workspace.mkdir()
    activation.bind_acquisition_workspace(
        root,
        home=home,
        invocation_id="invocation-old-foreign-check-123456",
        lease_id=old["lease_id"],
    )
    workspace.rename(home / "tmp" / "parked-owned-workspace")
    workspace.mkdir()
    (workspace / "foreign.txt").write_text("foreign", encoding="utf-8")
    current = _journal_lease(
        root,
        lease_id="lease-current-foreign-check-123456",
        owner_pid=os.getpid(),
        created_at=300,
    )
    marker = home / ".quiesce-lease.json"
    _write_lease_marker(marker, {**current, "lease_id": "lease-marker-foreign-123456"})
    monkeypatch.setattr(gate, "marker_path", lambda *_args, **_kwargs: marker)

    with pytest.raises(activation.ActivationError, match="current recovery lease"):
        activation.recover_stale_transaction_journals(
            root,
            home=home,
            current_lease=current,
            now=320,
            pid_alive=lambda pid: pid == os.getpid(),
            pid_create_time=lambda pid: 299.0 if pid == os.getpid() else None,
        )
    assert (workspace / "foreign.txt").is_file()

    _write_lease_marker(marker, current)
    with pytest.raises(activation.ActivationError, match="identity changed"):
        activation.recover_stale_transaction_journals(
            root,
            home=home,
            current_lease=current,
            now=320,
            pid_alive=lambda pid: pid == os.getpid(),
            pid_create_time=lambda pid: 299.0 if pid == os.getpid() else None,
        )
    assert (workspace / "foreign.txt").is_file()
