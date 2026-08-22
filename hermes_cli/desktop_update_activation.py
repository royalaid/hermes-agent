"""Crash-recoverable side-by-side activation for Windows Desktop updates.

This module deliberately uses only the Python standard library.  The outer
PowerShell handoff invokes it through Hermes' private base interpreter after
the updater child has exited, so the live venv directory can be renamed
without mutating an environment that is still executing code.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]{16,128}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_CANDIDATE_RE = re.compile(r"^venv-candidate-[A-Za-z0-9._-]{8,96}$")
_MANIFEST_NAME = ".hermes-update-activation.json"
_STATE_NAME = ".hermes-update-activation-state.json"
_RECEIPT_NAME = ".hermes-update-receipt.json"
_MAX_PROTOCOL_BYTES = 256 * 1024
_MAX_RECEIPT_BYTES = 64 * 1024
_MAX_PATH_CHARS = 32_767
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_MOVE_RETRY_SECONDS = 10.0
_MOVE_RETRY_INTERVAL_SECONDS = 0.1
_MAX_MOVE_ATTEMPTS = 128
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class ActivationError(RuntimeError):
    """A stable, fail-closed activation protocol error."""


def _canonical(path: Path | str) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _read_bytes(path: Path, maximum: int, *, missing_ok: bool = False) -> bytes | None:
    try:
        with path.open("rb") as handle:
            payload = handle.read(maximum + 1)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ActivationError("required protocol artifact is missing") from None
    except OSError as exc:
        raise ActivationError("protocol artifact is unreadable") from exc
    if not payload or len(payload) > maximum:
        raise ActivationError("protocol artifact size is outside its bounded contract")
    return payload


def _read_json(path: Path, maximum: int = _MAX_PROTOCOL_BYTES) -> tuple[dict, bytes]:
    raw = _read_bytes(path, maximum)
    assert raw is not None
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivationError("protocol artifact is malformed") from exc
    if not isinstance(value, dict):
        raise ActivationError("protocol artifact is not an object")
    return value, raw


def _atomic_write(path: Path, payload: bytes) -> None:
    if not payload or len(payload) > _MAX_PROTOCOL_BYTES:
        raise ActivationError("refusing an unbounded protocol artifact")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _validate_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ActivationError(f"{label} is invalid")
    return value


def _validate_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise ActivationError(f"{label} is invalid")
    return value.lower()


def _manifest_paths(home: Path) -> tuple[Path, Path, Path]:
    return home / _MANIFEST_NAME, home / _STATE_NAME, home / _RECEIPT_NAME


def _candidate_relative(root: Path, candidate: Path) -> str:
    runtime_root = (root / ".hermes-runtime").resolve()
    resolved = candidate.resolve()
    if candidate.is_symlink() or not _is_within(resolved, runtime_root):
        raise ActivationError("candidate environment is outside the runtime root")
    if resolved.parent != runtime_root or _CANDIDATE_RE.fullmatch(resolved.name) is None:
        raise ActivationError("candidate environment name is invalid")
    return resolved.relative_to(root.resolve()).as_posix()


def write_activation_manifest(
    root: Path,
    *,
    home: Path,
    invocation_id: str,
    lease_id: str,
    candidate: Path,
    pre_update_head: str,
    branch: str,
    remote: str,
    target_ref: str,
    target_sha: str,
    python_health: dict[str, bool],
) -> dict:
    """Publish a candidate claim without changing the live venv or receipt."""
    root = root.resolve()
    home = home.resolve()
    invocation_id = _validate_identity(invocation_id, "invocation id")
    lease_id = _validate_identity(lease_id, "lease id")
    pre_update_head = _validate_sha(pre_update_head, "pre-update head")
    target_sha = _validate_sha(target_sha, "target head")
    if not branch or not remote or target_ref != f"refs/remotes/{remote}/{branch}":
        raise ActivationError("update target identity is invalid")
    expected_python_health = {
        "critical_syntax",
        "critical_imports",
        "dependencies",
    }
    if (
        set(python_health) != expected_python_health
        or any(type(python_health[key]) is not bool for key in expected_python_health)
        or not all(python_health.values())
    ):
        raise ActivationError("candidate Python health is incomplete")

    manifest_path, state_path, receipt_path = _manifest_paths(home)
    if manifest_path.exists() or state_path.exists():
        raise ActivationError("an earlier activation transaction is unresolved")
    prior_receipt = _read_bytes(receipt_path, _MAX_RECEIPT_BYTES, missing_ok=True)
    if prior_receipt is not None:
        try:
            json.loads(prior_receipt.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ActivationError("the prior update receipt is malformed") from exc

    manifest = {
        "schema_version": 1,
        "invocation_id": invocation_id,
        "lease_id": lease_id,
        "root": _canonical(root),
        "candidate_rel": _candidate_relative(root, candidate),
        "pre_update_head": pre_update_head,
        "target_head": target_sha,
        "branch": branch,
        "remote": remote,
        "target_ref": target_ref,
        "prior_receipt_sha256": _digest(prior_receipt),
        "python_health": {key: True for key in sorted(expected_python_health)},
        "created_at": int(time.time()),
    }
    _atomic_write(manifest_path, _json_bytes(manifest))
    return manifest


def _expected_context() -> tuple[Path, Path, str, str]:
    root_raw = os.environ.get("HERMES_INTERNAL_DESKTOP_UPDATE_ROOT", "")
    home_raw = os.environ.get("HERMES_INTERNAL_DESKTOP_UPDATE_HOME", "")
    invocation = _validate_identity(
        os.environ.get("HERMES_INTERNAL_DESKTOP_UPDATE_INVOCATION"),
        "invocation id",
    )
    lease = _validate_identity(
        os.environ.get("HERMES_INTERNAL_DESKTOP_UPDATE_LEASE"),
        "lease id",
    )
    if not root_raw or not home_raw:
        raise ActivationError("activation scope is missing")
    root = Path(root_raw).resolve()
    home = Path(home_raw).resolve()
    if not root.is_dir() or not home.is_dir():
        raise ActivationError("activation scope is unavailable")
    return root, home, invocation, lease


def _validated_manifest(
    root: Path,
    home: Path,
    invocation: str,
    lease: str,
) -> tuple[dict, bytes]:
    manifest_path, _, _ = _manifest_paths(home)
    manifest, raw = _read_json(manifest_path)
    expected_keys = {
        "schema_version",
        "invocation_id",
        "lease_id",
        "root",
        "candidate_rel",
        "pre_update_head",
        "target_head",
        "branch",
        "remote",
        "target_ref",
        "prior_receipt_sha256",
        "python_health",
        "created_at",
    }
    if set(manifest) != expected_keys or manifest.get("schema_version") != 1:
        raise ActivationError("activation manifest schema is invalid")
    if manifest.get("invocation_id") != invocation or manifest.get("lease_id") != lease:
        raise ActivationError("activation manifest identity is stale")
    claimed_root = manifest.get("root")
    if (
        not isinstance(claimed_root, str)
        or not claimed_root
        or len(claimed_root) > _MAX_PATH_CHARS
        or _canonical(claimed_root) != _canonical(root)
    ):
        raise ActivationError("activation manifest root is stale")
    _validate_sha(manifest.get("pre_update_head"), "pre-update head")
    _validate_sha(manifest.get("target_head"), "target head")
    branch = manifest.get("branch")
    remote = manifest.get("remote")
    target_ref = manifest.get("target_ref")
    if (
        not isinstance(branch, str)
        or _NAME_RE.fullmatch(branch) is None
        or not isinstance(remote, str)
        or _NAME_RE.fullmatch(remote) is None
        or target_ref != f"refs/remotes/{remote}/{branch}"
    ):
        raise ActivationError("activation manifest target is invalid")
    health = manifest.get("python_health")
    if not isinstance(health, dict) or set(health) != {
        "critical_imports",
        "critical_syntax",
        "dependencies",
    } or not all(value is True for value in health.values()):
        raise ActivationError("activation manifest health is invalid")
    prior_digest = manifest.get("prior_receipt_sha256")
    if prior_digest is not None and (
        not isinstance(prior_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", prior_digest) is None
    ):
        raise ActivationError("prior receipt claim is invalid")
    candidate_rel = manifest.get("candidate_rel")
    if (
        not isinstance(candidate_rel, str)
        or not candidate_rel
        or len(candidate_rel) > _MAX_PATH_CHARS
    ):
        raise ActivationError("candidate path claim is invalid")
    candidate = root / Path(candidate_rel)
    if _candidate_relative(root, candidate) != candidate_rel.replace("\\", "/"):
        raise ActivationError("candidate path claim is non-canonical")
    created_at = manifest.get("created_at")
    if type(created_at) is not int or created_at <= 0:
        raise ActivationError("activation manifest timestamp is invalid")
    return manifest, raw


def _git_output(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    for key in list(env):
        if key.upper().startswith("GIT_") and key.upper() not in {"GIT_TERMINAL_PROMPT"}:
            env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", "-c", "core.hooksPath=NUL" if os.name == "nt" else "/dev/null", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )


def _git_head(root: Path) -> str:
    result = _git_output(root, "rev-parse", "HEAD")
    head = result.stdout.strip()
    if result.returncode != 0 or _SHA_RE.fullmatch(head) is None:
        raise ActivationError("installed Git identity is unreadable")
    return head.lower()


def _git_clean(root: Path) -> bool:
    result = _git_output(root, "status", "--porcelain")
    return result.returncode == 0 and not result.stdout.strip()


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _smoke_live(venv: Path, root: Path) -> bool:
    python = _venv_python(venv)
    if not python.is_file():
        return False
    script = (
        "import dotenv,fastapi,openai,prompt_toolkit,pydantic,rich,uvicorn,yaml\n"
        "import hermes_cli.main,hermes_state,model_tools,run_agent,toolsets\n"
    )
    env = dict(os.environ)
    for key in (
        "CONDA_DEFAULT_ENV",
        "CONDA_PREFIX",
        "PYTHONHOME",
        "PYTHONPATH",
        "UV_PROJECT_ENVIRONMENT",
        "UV_PYTHON",
        "VIRTUAL_ENV",
    ):
        env.pop(key, None)
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", script],
            cwd=root,
            env=env,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _backup_name(invocation: str, lease: str) -> str:
    token = hashlib.sha256(f"{invocation}\0{lease}".encode()).hexdigest()[:20]
    return f"venv-rollback-{token}"


def _state_payload(
    manifest: dict,
    manifest_raw: bytes,
    *,
    phase: str,
    prior_receipt: bytes | None,
) -> dict:
    invocation = str(manifest["invocation_id"])
    lease = str(manifest["lease_id"])
    backup_rel = f".hermes-runtime/{_backup_name(invocation, lease)}"
    rejected_rel = f".hermes-runtime/venv-rejected-{hashlib.sha256(manifest_raw).hexdigest()[:20]}"
    return {
        "schema_version": 1,
        "invocation_id": invocation,
        "lease_id": lease,
        "root": manifest["root"],
        "phase": phase,
        "candidate_rel": manifest["candidate_rel"],
        "backup_rel": backup_rel,
        "rejected_rel": rejected_rel,
        "pre_update_head": manifest["pre_update_head"],
        "target_head": manifest["target_head"],
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "prior_receipt_sha256": manifest["prior_receipt_sha256"],
        "prior_receipt_b64": (
            base64.b64encode(prior_receipt).decode("ascii")
            if prior_receipt is not None
            else None
        ),
        "candidate_receipt_b64": None,
        "published_receipt_sha256": None,
        "move_error": None,
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }


def _write_state(path: Path, state: dict) -> None:
    state["updated_at"] = int(time.time())
    _atomic_write(path, _json_bytes(state))


def _move_failure_reason(error: OSError) -> str:
    winerror = getattr(error, "winerror", None)
    error_number = getattr(error, "errno", None)
    if winerror == 32 or error_number == 32:
        return "sharing"
    if winerror == 5 or error_number in {5, 13} or isinstance(error, PermissionError):
        return "access"
    if winerror in {2, 3} or error_number == 2:
        return "missing"
    return "other"


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ActivationError("activation directory identity is unreadable") from exc
    attributes = int(getattr(value, "st_file_attributes", 0))
    if not stat.S_ISDIR(value.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise ActivationError("activation directory identity is unsafe")
    return int(value.st_dev), int(value.st_ino)


def _replace_directory_with_retry(
    source: Path,
    destination: Path,
    *,
    validate_protocol: Callable[[], None],
) -> None:
    """Retry only transient Windows access/sharing failures against one identity."""
    expected_identity = _directory_identity(source)
    started = time.monotonic()
    deadline = started + _MOVE_RETRY_SECONDS
    last_error: OSError | None = None
    attempts = 0
    while attempts < _MAX_MOVE_ATTEMPTS:
        validate_protocol()
        if _directory_identity(source) != expected_identity:
            raise ActivationError("activation directory identity changed during retry")
        if destination.exists() or destination.is_symlink():
            raise ActivationError("activation destination appeared during retry")
        attempts += 1
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            last_error = exc
            elapsed_ms = max(0, min(60_000, int((time.monotonic() - started) * 1000)))
            setattr(exc, "hermes_move_attempts", attempts)
            setattr(exc, "hermes_move_elapsed_ms", elapsed_ms)
            if _move_failure_reason(exc) not in {"access", "sharing"}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(_MOVE_RETRY_INTERVAL_SECONDS, remaining))
    assert last_error is not None
    raise last_error


def _move_failure_phase(stage: str, error: OSError) -> str:
    """Return one bounded diagnostic class without paths or exception text."""
    return f"{stage}-move-failed-{_move_failure_reason(error)}"


def _move_failure_diagnostics(stage: str, error: OSError) -> dict:
    winerror = getattr(error, "winerror", None)
    error_number = getattr(error, "errno", None)
    return {
        "stage": stage,
        "reason": _move_failure_reason(error),
        "win32_error": winerror if type(winerror) is int and 0 <= winerror <= 65_535 else None,
        "system_errno": (
            error_number
            if type(error_number) is int and -65_535 <= error_number <= 65_535
            else None
        ),
        "attempts": max(
            1,
            min(_MAX_MOVE_ATTEMPTS, int(getattr(error, "hermes_move_attempts", 1))),
        ),
        "elapsed_ms": max(
            0,
            min(60_000, int(getattr(error, "hermes_move_elapsed_ms", 0))),
        ),
    }


def _validate_move_protocol(
    root: Path,
    home: Path,
    invocation: str,
    lease: str,
    expected_phase: str,
) -> None:
    current, _, _ = _load_state(root, home, invocation, lease)
    if current.get("phase") != expected_phase or current.get("move_error") is not None:
        raise ActivationError("activation protocol changed during directory retry")


def _load_state(
    root: Path, home: Path, invocation: str, lease: str
) -> tuple[dict, bytes, bytes]:
    manifest, manifest_raw = _validated_manifest(root, home, invocation, lease)
    _, state_path, _ = _manifest_paths(home)
    state, state_raw = _read_json(state_path)
    expected_keys = {
        "schema_version",
        "invocation_id",
        "lease_id",
        "root",
        "phase",
        "candidate_rel",
        "backup_rel",
        "rejected_rel",
        "pre_update_head",
        "target_head",
        "manifest_sha256",
        "prior_receipt_sha256",
        "prior_receipt_b64",
        "candidate_receipt_b64",
        "published_receipt_sha256",
        "move_error",
        "created_at",
        "updated_at",
    }
    if set(state) != expected_keys or state.get("schema_version") != 1:
        raise ActivationError("activation state schema is invalid")
    if state.get("invocation_id") != invocation or state.get("lease_id") != lease:
        raise ActivationError("activation state identity is stale")
    if _canonical(str(state.get("root", ""))) != _canonical(root):
        raise ActivationError("activation state root is stale")
    if state.get("manifest_sha256") != hashlib.sha256(manifest_raw).hexdigest():
        raise ActivationError("activation manifest changed after preparation")
    expected_backup = f".hermes-runtime/{_backup_name(invocation, lease)}"
    expected_rejected = (
        f".hermes-runtime/venv-rejected-{hashlib.sha256(manifest_raw).hexdigest()[:20]}"
    )
    if (
        state.get("candidate_rel") != manifest.get("candidate_rel")
        or state.get("backup_rel") != expected_backup
        or state.get("rejected_rel") != expected_rejected
    ):
        raise ActivationError("activation candidate identity changed")
    for field in ("candidate_rel", "backup_rel", "rejected_rel"):
        rel = state[field]
        if (
            not isinstance(rel, str)
            or len(rel) > _MAX_PATH_CHARS
            or not _is_within(root / rel, root / ".hermes-runtime")
        ):
            raise ActivationError("activation state contains an out-of-scope path")
    if (
        state.get("pre_update_head") != manifest.get("pre_update_head")
        or state.get("target_head") != manifest.get("target_head")
        or state.get("prior_receipt_sha256") != manifest.get("prior_receipt_sha256")
    ):
        raise ActivationError("activation state generation changed")
    phase = state.get("phase")
    if phase not in {
        "prepared",
        "moving-live-to-backup",
        "moving-candidate-to-live",
        "live-move-failed-sharing",
        "live-move-failed-access",
        "live-move-failed-missing",
        "live-move-failed-other",
        "candidate-move-failed-sharing",
        "candidate-move-failed-access",
        "candidate-move-failed-missing",
        "candidate-move-failed-other",
        "smoke-testing-active",
        "active",
        "publishing",
        "receipt-published",
    }:
        raise ActivationError("activation state phase is invalid")
    failed_move_phases = {
        "live-move-failed-sharing",
        "live-move-failed-access",
        "live-move-failed-missing",
        "live-move-failed-other",
        "candidate-move-failed-sharing",
        "candidate-move-failed-access",
        "candidate-move-failed-missing",
        "candidate-move-failed-other",
    }
    move_error = state.get("move_error")
    if phase in failed_move_phases:
        if not isinstance(move_error, dict) or set(move_error) != {
            "stage",
            "reason",
            "win32_error",
            "system_errno",
            "attempts",
            "elapsed_ms",
        }:
            raise ActivationError("activation move diagnostics are invalid")
        stage = move_error.get("stage")
        reason = move_error.get("reason")
        winerror = move_error.get("win32_error")
        system_errno = move_error.get("system_errno")
        attempts = move_error.get("attempts")
        elapsed_ms = move_error.get("elapsed_ms")
        if (
            stage not in {"live", "candidate"}
            or reason not in {"sharing", "access", "missing", "other"}
            or phase != f"{stage}-move-failed-{reason}"
            or (winerror is not None and (type(winerror) is not int or not 0 <= winerror <= 65_535))
            or (
                system_errno is not None
                and (type(system_errno) is not int or not -65_535 <= system_errno <= 65_535)
            )
            or type(attempts) is not int
            or not 1 <= attempts <= _MAX_MOVE_ATTEMPTS
            or type(elapsed_ms) is not int
            or not 0 <= elapsed_ms <= 60_000
        ):
            raise ActivationError("activation move diagnostics are out of bounds")
    elif move_error is not None:
        raise ActivationError("activation move diagnostics are premature")
    created_at = state.get("created_at")
    updated_at = state.get("updated_at")
    if (
        type(created_at) is not int
        or type(updated_at) is not int
        or created_at <= 0
        or updated_at < created_at
    ):
        raise ActivationError("activation state timestamp is invalid")

    prior_b64 = state.get("prior_receipt_b64")
    prior_digest = state.get("prior_receipt_sha256")
    if prior_b64 is None:
        if prior_digest is not None:
            raise ActivationError("activation state lost prior receipt bytes")
    elif isinstance(prior_b64, str):
        try:
            prior_raw = base64.b64decode(prior_b64, validate=True)
        except ValueError as exc:
            raise ActivationError("activation state prior receipt is malformed") from exc
        if not prior_raw or len(prior_raw) > _MAX_RECEIPT_BYTES or _digest(prior_raw) != prior_digest:
            raise ActivationError("activation state prior receipt changed")
    else:
        raise ActivationError("activation state prior receipt is invalid")

    candidate_b64 = state.get("candidate_receipt_b64")
    published_digest = state.get("published_receipt_sha256")
    if phase in {
        "prepared",
        "moving-live-to-backup",
        "moving-candidate-to-live",
        "live-move-failed-sharing",
        "live-move-failed-access",
        "live-move-failed-missing",
        "live-move-failed-other",
        "candidate-move-failed-sharing",
        "candidate-move-failed-access",
        "candidate-move-failed-missing",
        "candidate-move-failed-other",
        "smoke-testing-active",
        "active",
    }:
        if candidate_b64 is not None or published_digest is not None:
            raise ActivationError("activation state published receipt is premature")
    else:
        if not isinstance(candidate_b64, str) or not isinstance(published_digest, str):
            raise ActivationError("activation state published receipt is missing")
        try:
            candidate_raw = base64.b64decode(candidate_b64, validate=True)
        except ValueError as exc:
            raise ActivationError("activation state published receipt is malformed") from exc
        if (
            not candidate_raw
            or len(candidate_raw) > _MAX_RECEIPT_BYTES
            or re.fullmatch(r"[0-9a-f]{64}", published_digest) is None
            or _digest(candidate_raw) != published_digest
        ):
            raise ActivationError("activation state published receipt changed")
    return state, manifest_raw, state_raw


def _remove_tree_exact(path: Path, *, parent: Path, prefix: str) -> None:
    """Remove one derived rollback artifact without following links."""
    if not path.exists():
        return
    if (
        path.is_symlink()
        or path.parent.resolve() != parent.resolve()
        or not path.name.startswith(prefix)
    ):
        raise ActivationError("refusing to remove an out-of-scope rollback artifact")
    shutil.rmtree(path)


def activate() -> None:
    root, home, invocation, lease = _expected_context()
    manifest, manifest_raw = _validated_manifest(root, home, invocation, lease)
    _, state_path, receipt_path = _manifest_paths(home)
    if state_path.exists():
        raise ActivationError("activation state already exists")
    if _git_head(root) != manifest["target_head"] or not _git_clean(root):
        raise ActivationError("source identity changed before activation")

    current_receipt = _read_bytes(receipt_path, _MAX_RECEIPT_BYTES, missing_ok=True)
    if _digest(current_receipt) != manifest["prior_receipt_sha256"]:
        raise ActivationError("the prior update receipt changed before activation")
    state = _state_payload(
        manifest,
        manifest_raw,
        phase="prepared",
        prior_receipt=current_receipt,
    )
    _write_state(state_path, state)

    live = root / "venv"
    candidate = root / state["candidate_rel"]
    backup = root / state["backup_rel"]
    rejected = root / state["rejected_rel"]
    if not live.is_dir() or not candidate.is_dir() or backup.exists() or rejected.exists():
        raise ActivationError("activation directories are not in the expected state")
    if live.is_symlink() or candidate.is_symlink():
        raise ActivationError("activation refuses a linked venv root")

    try:
        state["phase"] = "moving-live-to-backup"
        state["move_error"] = None
        _write_state(state_path, state)
        try:
            _replace_directory_with_retry(
                live,
                backup,
                validate_protocol=lambda: _validate_move_protocol(
                    root,
                    home,
                    invocation,
                    lease,
                    "moving-live-to-backup",
                ),
            )
        except OSError as exc:
            state["phase"] = _move_failure_phase("live", exc)
            state["move_error"] = _move_failure_diagnostics("live", exc)
            _write_state(state_path, state)
            raise
        state["phase"] = "moving-candidate-to-live"
        state["move_error"] = None
        _write_state(state_path, state)
        try:
            _replace_directory_with_retry(
                candidate,
                live,
                validate_protocol=lambda: _validate_move_protocol(
                    root,
                    home,
                    invocation,
                    lease,
                    "moving-candidate-to-live",
                ),
            )
        except OSError as exc:
            state["phase"] = _move_failure_phase("candidate", exc)
            state["move_error"] = _move_failure_diagnostics("candidate", exc)
            _write_state(state_path, state)
            raise
        state["phase"] = "smoke-testing-active"
        _write_state(state_path, state)
        if not _smoke_live(live, root):
            raise ActivationError("the activated environment failed its import smoke test")
    except BaseException:
        try:
            if backup.exists():
                if live.exists() and not rejected.exists():
                    os.replace(live, rejected)
                if not live.exists():
                    os.replace(backup, live)
        except OSError as rollback_error:
            raise ActivationError("environment activation and rollback both failed") from rollback_error
        raise
    state["phase"] = "active"
    _write_state(state_path, state)


def _build_receipt(manifest: dict) -> dict:
    value = {
        "schema_version": 1,
        "invocation_id": manifest["invocation_id"],
        "lease_id": manifest["lease_id"],
        "mode": "git",
        "root": manifest["root"],
        "remote": manifest["remote"],
        "branch": manifest["branch"],
        "target_ref": manifest["target_ref"],
        "target_sha": manifest["target_head"],
        "resulting_head": manifest["target_head"],
        "archive_sha": None,
        "timestamp": int(time.time()),
        "success": True,
        "gateway_resume_deferred": True,
        "health": {
            "critical_syntax": True,
            "critical_imports": True,
            "dependencies": True,
            "node_dependencies": True,
        },
    }
    from hermes_cli.update_receipt import _sanitize_update_receipt

    sanitized = _sanitize_update_receipt(value, Path(manifest["root"]))
    if sanitized is None:
        raise ActivationError("refusing to publish an invalid update receipt")
    return sanitized


def publish_receipt() -> None:
    root, home, invocation, lease = _expected_context()
    manifest, _ = _validated_manifest(root, home, invocation, lease)
    _, state_path, receipt_path = _manifest_paths(home)
    state, _, _ = _load_state(root, home, invocation, lease)
    if state.get("phase") != "active":
        raise ActivationError("the candidate environment is not active")
    if _git_head(root) != manifest["target_head"] or not _git_clean(root):
        raise ActivationError("source identity changed before receipt publication")
    if not _smoke_live(root / "venv", root):
        raise ActivationError("the active environment lost its health proof")
    current_receipt = _read_bytes(receipt_path, _MAX_RECEIPT_BYTES, missing_ok=True)
    if _digest(current_receipt) != state["prior_receipt_sha256"]:
        raise ActivationError("the prior update receipt changed during activation")

    receipt_raw = _json_bytes(_build_receipt(manifest))
    state["phase"] = "publishing"
    state["candidate_receipt_b64"] = base64.b64encode(receipt_raw).decode("ascii")
    state["published_receipt_sha256"] = _digest(receipt_raw)
    _write_state(state_path, state)
    _atomic_write(receipt_path, receipt_raw)
    state["phase"] = "receipt-published"
    _write_state(state_path, state)


def _restore_receipt(state: dict, receipt_path: Path) -> None:
    current = _read_bytes(receipt_path, _MAX_RECEIPT_BYTES, missing_ok=True)
    current_digest = _digest(current)
    prior_digest = state.get("prior_receipt_sha256")
    published_digest = state.get("published_receipt_sha256")
    if current_digest == prior_digest:
        return
    if published_digest is None or current_digest != published_digest:
        raise ActivationError("update receipt ownership changed before rollback")
    prior_b64 = state.get("prior_receipt_b64")
    if prior_b64 is None:
        try:
            receipt_path.unlink()
        except FileNotFoundError:
            pass
        return
    try:
        prior = base64.b64decode(prior_b64, validate=True)
    except (TypeError, ValueError) as exc:
        raise ActivationError("saved receipt rollback bytes are invalid") from exc
    if _digest(prior) != prior_digest:
        raise ActivationError("saved receipt rollback bytes changed")
    _atomic_write(receipt_path, prior)


def _restore_desktop_backup(root: Path, token: str) -> None:
    live = root / "apps" / "desktop" / "release" / "win-unpacked"
    backup = live.with_name(f"{live.name}.bak")
    rejected = live.with_name(f"{live.name}.rejected-{token}")
    backup_exe = backup / "Hermes.exe"
    if not backup_exe.is_file():
        if (live / "Hermes.exe").is_file():
            _remove_tree_exact(
                rejected,
                parent=live.parent,
                prefix="win-unpacked.rejected-",
            )
        return
    try:
        with backup_exe.open("rb") as handle:
            if handle.read(2) != b"MZ":
                raise ActivationError("the prior Desktop backup is not a valid PE image")
    except OSError as exc:
        raise ActivationError("the prior Desktop backup is unreadable") from exc
    if live.exists() and rejected.exists():
        raise ActivationError("a prior rejected Desktop build is unresolved")
    if live.exists():
        os.replace(live, rejected)
    os.replace(backup, live)
    _remove_tree_exact(
        rejected,
        parent=live.parent,
        prefix="win-unpacked.rejected-",
    )


def _restore_source(root: Path, pre_update_head: str) -> None:
    pre_update_head = _validate_sha(pre_update_head, "pre-update head")
    if _git_head(root) == pre_update_head:
        return
    if not _git_clean(root):
        raise ActivationError("source rollback refused because tracked files changed")
    result = _git_output(root, "reset", "--hard", pre_update_head)
    if result.returncode != 0 or _git_head(root) != pre_update_head:
        raise ActivationError("source rollback failed")


def _unlink_exact(path: Path, expected_raw: bytes) -> None:
    current = _read_bytes(path, _MAX_PROTOCOL_BYTES)
    if current != expected_raw:
        raise ActivationError("protocol artifact changed before cleanup")
    path.unlink()


def rollback() -> None:
    root, home, invocation, lease = _expected_context()
    manifest, manifest_raw = _validated_manifest(root, home, invocation, lease)
    manifest_path, state_path, receipt_path = _manifest_paths(home)
    if state_path.exists():
        state, _, state_raw = _load_state(root, home, invocation, lease)
        if state.get("phase") not in {
            "prepared",
            "moving-live-to-backup",
            "moving-candidate-to-live",
            "live-move-failed-sharing",
            "live-move-failed-access",
            "live-move-failed-missing",
            "live-move-failed-other",
            "candidate-move-failed-sharing",
            "candidate-move-failed-access",
            "candidate-move-failed-missing",
            "candidate-move-failed-other",
            "smoke-testing-active",
            "active",
            "publishing",
            "receipt-published",
        }:
            raise ActivationError("activation state cannot be rolled back")
        _restore_receipt(state, receipt_path)
        live = root / "venv"
        backup = root / state["backup_rel"]
        rejected = root / state["rejected_rel"]
        candidate = root / state["candidate_rel"]
        if backup.exists():
            if live.exists():
                if rejected.exists():
                    raise ActivationError("a rejected environment already exists")
                os.replace(live, rejected)
            os.replace(backup, live)
        if not _venv_python(live).is_file():
            raise ActivationError("the prior environment could not be restored")
        _remove_tree_exact(
            rejected,
            parent=root / ".hermes-runtime",
            prefix="venv-rejected-",
        )
        _remove_tree_exact(
            candidate,
            parent=root / ".hermes-runtime",
            prefix="venv-candidate-",
        )
        _restore_desktop_backup(root, hashlib.sha256(manifest_raw).hexdigest()[:20])
        _restore_source(root, str(state["pre_update_head"]))
        _unlink_exact(state_path, state_raw)
    else:
        _restore_source(root, str(manifest["pre_update_head"]))
        _remove_tree_exact(
            root / str(manifest["candidate_rel"]),
            parent=root / ".hermes-runtime",
            prefix="venv-candidate-",
        )
    _unlink_exact(manifest_path, manifest_raw)


def rollback_source_only() -> None:
    root, _, _, _ = _expected_context()
    pre_update_head = os.environ.get("HERMES_INTERNAL_DESKTOP_UPDATE_PRE_HEAD")
    _restore_source(root, _validate_sha(pre_update_head, "pre-update head"))


def commit() -> None:
    root, home, invocation, lease = _expected_context()
    manifest, manifest_raw = _validated_manifest(root, home, invocation, lease)
    manifest_path, state_path, receipt_path = _manifest_paths(home)
    state, _, state_raw = _load_state(root, home, invocation, lease)
    if state.get("phase") != "receipt-published":
        raise ActivationError("activation is not ready to commit")
    current_receipt = _read_bytes(receipt_path, _MAX_RECEIPT_BYTES)
    if _digest(current_receipt) != state.get("published_receipt_sha256"):
        raise ActivationError("the committed receipt changed before cleanup")
    if (
        _git_head(root) != manifest["target_head"]
        or not _git_clean(root)
        or not _smoke_live(root / "venv", root)
    ):
        raise ActivationError("the installed generation lost its final health proof")

    backup = root / state["backup_rel"]
    _remove_tree_exact(
        backup,
        parent=root / ".hermes-runtime",
        prefix="venv-rollback-",
    )
    desktop_backup = root / "apps" / "desktop" / "release" / "win-unpacked.bak"
    _remove_tree_exact(
        desktop_backup,
        parent=root / "apps" / "desktop" / "release",
        prefix="win-unpacked.bak",
    )
    _unlink_exact(state_path, state_raw)
    _unlink_exact(manifest_path, manifest_raw)


_ACTIONS = {
    "activate": activate,
    "publish-receipt": publish_receipt,
    "rollback": rollback,
    "rollback-source": rollback_source_only,
    "commit": commit,
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in _ACTIONS:
        print("FAILED", file=sys.stderr)
        return 2
    try:
        _ACTIONS[args[0]]()
    except BaseException:
        # Paths, receipt bytes, identifiers, and exception details are private
        # protocol data.  The outer handoff records only this stable outcome.
        print("FAILED", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
