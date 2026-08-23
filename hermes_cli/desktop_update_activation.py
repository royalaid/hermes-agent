"""Crash-recoverable side-by-side activation for Windows Desktop updates.

This module deliberately uses only the Python standard library.  The outer
PowerShell handoff invokes it through Hermes' private base interpreter after
the updater child has exited, so the live venv directory can be renamed
without mutating an environment that is still executing code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
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
_GENERATION_RE = re.compile(r"^generation-[A-Za-z0-9._-]{8,96}$")
_MANIFEST_NAME = ".hermes-update-activation.json"
_STATE_NAME = ".hermes-update-activation-state.json"
_RECEIPT_NAME = ".hermes-update-receipt.json"
_STAGING_NAME = ".hermes-update-staging.json"
_ACQUISITION_NAME = ".hermes-update-acquisition.json"
_DESKTOP_HEALTH_NAME = ".hermes-update-desktop-health.json"
_COMMIT_PROPOSAL_NAME = ".hermes-update-commit-proposal.json"
_COMMIT_COORDINATOR_NAME = ".hermes-update-commit-coordinator.json"
_MAX_PROTOCOL_BYTES = 256 * 1024
_MAX_DESKTOP_HEALTH_BYTES = 64 * 1024
_MAX_RECEIPT_BYTES = 64 * 1024
_MAX_PATH_CHARS = 32_767
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_INVALID_REF_CHAR_RE = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_MAX_BRANCH_CHARS = 255
_MAX_COMMIT_RUNTIMES = 64
_MAX_GATEWAY_PLAN_SECONDS = 66 * 60
_MOVE_RETRY_SECONDS = 10.0
_MOVE_RETRY_INTERVAL_SECONDS = 0.1
_MAX_MOVE_ATTEMPTS = 128
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
logger = logging.getLogger(__name__)


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


def _commit_document_auth(value: dict, lease: str) -> str:
    """Authenticate one canonical transaction-coordinator document."""
    return hmac.new(
        lease.encode("utf-8"),
        _json_bytes(value),
        hashlib.sha256,
    ).hexdigest()


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


def _validate_remote_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or _REMOTE_NAME_RE.fullmatch(value) is None
        or value.endswith((".", ".lock"))
        or ".." in value
        or "@{" in value
    ):
        raise ActivationError("update target identity is invalid")
    return value


def _validate_branch_name(value: object) -> str:
    """Validate the bounded Git branch grammar without running Git hooks/config."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_BRANCH_CHARS
        or value == "@"
        or value.startswith(("-", "/"))
        or value.endswith(("/", "."))
        or "//" in value
        or ".." in value
        or "@{" in value
        or _INVALID_REF_CHAR_RE.search(value) is not None
    ):
        raise ActivationError("update target identity is invalid")
    components = value.split("/")
    if any(
        not component
        or component.startswith(".")
        or component.endswith((".", ".lock"))
        for component in components
    ):
        raise ActivationError("update target identity is invalid")
    return value


def _validate_update_target(branch: object, remote: object, target_ref: object) -> tuple[str, str, str]:
    branch_value = _validate_branch_name(branch)
    remote_value = _validate_remote_name(remote)
    expected_ref = f"refs/remotes/{remote_value}/{branch_value}"
    if not isinstance(target_ref, str) or target_ref != expected_ref:
        raise ActivationError("update target identity is invalid")
    return branch_value, remote_value, target_ref


def _manifest_paths(home: Path) -> tuple[Path, Path, Path]:
    return home / _MANIFEST_NAME, home / _STATE_NAME, home / _RECEIPT_NAME


def _staging_path(home: Path) -> Path:
    return home / _STAGING_NAME


def _desktop_health_path(home: Path) -> Path:
    return home / _DESKTOP_HEALTH_NAME


def _acquisition_path(home: Path) -> Path:
    return home / _ACQUISITION_NAME


def _commit_proposal_path(home: Path) -> Path:
    return home / _COMMIT_PROPOSAL_NAME


def _commit_coordinator_path(home: Path) -> Path:
    return home / _COMMIT_COORDINATOR_NAME


def _commit_coordinator_publishing_path(home: Path, attempt: str) -> Path:
    return _commit_coordinator_path(home).with_name(
        f"{_COMMIT_COORDINATOR_NAME}.{attempt}.publishing"
    )


def _refuse_existing_commit_coordinator(home: Path) -> None:
    """Never parse, adopt, or retire native commit authority from Python."""
    if os.path.lexists(_commit_coordinator_path(home)):
        raise ActivationError("a commit coordinator requires native retirement")


def _acquisition_workspace_relative(home: Path, workspace: Path) -> str:
    home = home.resolve()
    try:
        relative = workspace.absolute().relative_to(home).as_posix()
    except (OSError, ValueError) as exc:
        raise ActivationError("acquisition workspace is outside Hermes home") from exc
    if re.fullmatch(r"tmp/update-acquisition-[0-9a-f]{24}", relative) is None:
        raise ActivationError("acquisition workspace identity is invalid")
    return relative


def _validated_lease_authority(
    root: Path,
    lease: object,
    *,
    expected_lease_id: str | None = None,
) -> dict:
    """Validate one complete schema-v1 quiesce lease without deciding liveness."""
    from hermes_mcp_update_gate import (
        MAX_HANDOFF_GRACE_SECONDS,
        MAX_LEASE_SECONDS,
    )

    expected_keys = {
        "schema_version",
        "lease_id",
        "owner_pid",
        "created_at",
        "expires_at",
        "handoff_grace_until",
        "install_root",
    }
    if not isinstance(lease, dict) or set(lease) != expected_keys:
        raise ActivationError("transaction lease authority is invalid")
    lease_id = lease.get("lease_id")
    owner_pid = lease.get("owner_pid")
    created_at = lease.get("created_at")
    expires_at = lease.get("expires_at")
    handoff_until = lease.get("handoff_grace_until")
    if (
        lease.get("schema_version") != 1
        or not isinstance(lease_id, str)
        or _IDENTIFIER_RE.fullmatch(lease_id) is None
        or (expected_lease_id is not None and lease_id != expected_lease_id)
        or type(owner_pid) is not int
        or owner_pid <= 0
        or type(created_at) is not int
        or type(expires_at) is not int
        or type(handoff_until) is not int
        or created_at <= 0
        or not (created_at <= handoff_until <= expires_at)
        or expires_at - created_at > MAX_LEASE_SECONDS
        or handoff_until - created_at > MAX_HANDOFF_GRACE_SECONDS
        or _canonical(str(lease.get("install_root", ""))) != _canonical(root)
    ):
        raise ActivationError("transaction lease authority is invalid")
    return dict(lease)


def _acquisition_identity_digest(
    relative: str,
    identity: tuple[int, int],
    invocation: str,
    lease: str,
) -> str:
    return hashlib.sha256(
        (
            f"hermes-acquisition-v1\0{relative}\0{identity[0]}\0{identity[1]}\0"
            f"{invocation}\0{lease}"
        ).encode("utf-8")
    ).hexdigest()


def write_acquisition_journal(
    root: Path,
    *,
    home: Path,
    invocation_id: str,
    lease: dict,
    workspace: Path,
) -> None:
    root = root.resolve()
    home = home.resolve()
    _refuse_existing_commit_coordinator(home)
    invocation_id = _validate_identity(invocation_id, "invocation id")
    lease_authority = _validated_lease_authority(root, lease)
    lease_id = _validate_identity(lease_authority["lease_id"], "lease id")
    relative = _acquisition_workspace_relative(home, workspace)
    path = _acquisition_path(home)
    if path.exists() or os.path.lexists(workspace):
        raise ActivationError("an earlier acquisition transaction is unresolved")
    _atomic_write(
        path,
        _json_bytes(
            {
                "schema_version": 2,
                "invocation_id": invocation_id,
                "lease_id": lease_id,
                "lease_authority": lease_authority,
                "root": _canonical(root),
                "workspace_rel": relative,
                "workspace_identity_sha256": None,
                "created_at": int(time.time()),
            }
        ),
    )


def _validated_acquisition_journal(
    root: Path,
    home: Path,
    invocation: str,
    lease: str,
) -> tuple[dict, bytes, Path]:
    value, raw = _read_json(_acquisition_path(home))
    if set(value) != {
        "schema_version",
        "invocation_id",
        "lease_id",
        "lease_authority",
        "root",
        "workspace_rel",
        "workspace_identity_sha256",
        "created_at",
    } or value.get("schema_version") != 2:
        raise ActivationError("acquisition journal schema is invalid")
    if value.get("invocation_id") != invocation or value.get("lease_id") != lease:
        raise ActivationError("acquisition journal identity is stale")
    if _canonical(str(value.get("root", ""))) != _canonical(root):
        raise ActivationError("acquisition journal root is stale")
    _validated_lease_authority(
        root,
        value.get("lease_authority"),
        expected_lease_id=lease,
    )
    workspace = home / str(value.get("workspace_rel", ""))
    relative = _acquisition_workspace_relative(home, workspace)
    if relative != value.get("workspace_rel"):
        raise ActivationError("acquisition workspace claim is non-canonical")
    digest = value.get("workspace_identity_sha256")
    if os.path.lexists(workspace):
        identity = _directory_identity(workspace)
        if digest is None:
            try:
                if any(workspace.iterdir()):
                    raise ActivationError("unidentified acquisition workspace is not empty")
            except OSError as exc:
                raise ActivationError("acquisition workspace is unreadable") from exc
        elif (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not hmac.compare_digest(
                digest,
                _acquisition_identity_digest(relative, identity, invocation, lease),
            )
        ):
            raise ActivationError("acquisition workspace identity changed")
    elif digest is not None and (
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ActivationError("acquisition workspace identity is invalid")
    created_at = value.get("created_at")
    if type(created_at) is not int or created_at <= 0:
        raise ActivationError("acquisition journal timestamp is invalid")
    return value, raw, workspace


def bind_acquisition_workspace(
    root: Path,
    *,
    home: Path,
    invocation_id: str,
    lease_id: str,
) -> None:
    value, _, workspace = _validated_acquisition_journal(
        root, home, invocation_id, lease_id
    )
    relative = str(value["workspace_rel"])
    value["workspace_identity_sha256"] = _acquisition_identity_digest(
        relative,
        _directory_identity(workspace),
        invocation_id,
        lease_id,
    )
    _atomic_write(_acquisition_path(home), _json_bytes(value))


def recover_acquisition_journal(
    root: Path,
    home: Path,
    invocation: str,
    lease: str,
) -> None:
    _, raw, workspace = _validated_acquisition_journal(
        root, home, invocation, lease
    )
    if os.path.lexists(workspace):
        def make_writable(function, path, _error) -> None:
            os.chmod(path, 0o700)
            function(path)

        shutil.rmtree(workspace, onerror=make_writable)
    _unlink_exact(_acquisition_path(home), raw)


def _artifact_identity_digest(
    kind: str,
    relative: str,
    identity: tuple[int, int],
    invocation: str,
    lease: str,
) -> str:
    raw = (
        f"hermes-staging-{kind}-v1\0{relative}\0{identity[0]}\0{identity[1]}\0"
        f"{invocation}\0{lease}"
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _planned_artifact_relative(root: Path, path: Path, kind: str) -> str:
    root = root.resolve()
    if kind == "candidate":
        parent = root / ".hermes-runtime"
        pattern = _CANDIDATE_RE
    elif kind == "generation":
        parent = root / ".hermes-runtime" / "python"
        pattern = _GENERATION_RE
    else:  # pragma: no cover - private caller invariant
        raise ActivationError("unknown staging artifact kind")
    if (
        path.is_absolute()
        and path.parent.resolve() != parent.resolve()
    ) or pattern.fullmatch(path.name) is None:
        raise ActivationError("staging artifact path is invalid")
    absolute = path if path.is_absolute() else root / path
    if absolute.parent.resolve() != parent.resolve():
        raise ActivationError("staging artifact path is invalid")
    return absolute.relative_to(root).as_posix()


def _staging_artifact_claim(
    root: Path,
    path: Path | None,
    kind: str,
    invocation: str,
    lease: str,
) -> dict | None:
    if path is None:
        return None
    relative = _planned_artifact_relative(root, path, kind)
    absolute = root / relative
    identity_digest = None
    if os.path.lexists(absolute):
        identity = _directory_identity(absolute)
        identity_digest = _artifact_identity_digest(
            kind,
            relative,
            identity,
            invocation,
            lease,
        )
    return {"rel": relative, "identity_sha256": identity_digest}


def _validated_staging_artifact(
    root: Path,
    claim: object,
    kind: str,
    invocation: str,
    lease: str,
    *,
    allow_unidentified_nonempty: bool = False,
) -> Path | None:
    if claim is None:
        return None
    if not isinstance(claim, dict) or set(claim) != {"rel", "identity_sha256"}:
        raise ActivationError("staging artifact claim is invalid")
    relative = claim.get("rel")
    digest = claim.get("identity_sha256")
    if not isinstance(relative, str):
        raise ActivationError("staging artifact claim is invalid")
    if digest is not None and (
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ActivationError("staging artifact claim is invalid")
    path = root / Path(relative)
    if _planned_artifact_relative(root, path, kind) != relative:
        raise ActivationError("staging artifact claim is non-canonical")
    if not os.path.lexists(path):
        return path
    identity = _directory_identity(path)
    if digest is None:
        try:
            if any(path.iterdir()) and not allow_unidentified_nonempty:
                raise ActivationError("unidentified staging artifact is not empty")
        except OSError as exc:
            raise ActivationError("staging artifact is unreadable") from exc
        return path
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or not hmac.compare_digest(
            digest,
            _artifact_identity_digest(kind, relative, identity, invocation, lease),
        )
    ):
        raise ActivationError("staging artifact identity changed")
    return path


def write_staging_journal(
    root: Path,
    *,
    home: Path,
    invocation_id: str,
    lease: dict,
    pre_update_head: str,
    pre_update_branch: str,
    branch: str,
    selected_pre_head: str | None,
    target_head: str,
) -> Path:
    root = root.resolve()
    home = home.resolve()
    _refuse_existing_commit_coordinator(home)
    invocation_id = _validate_identity(invocation_id, "invocation id")
    lease_authority = _validated_lease_authority(root, lease)
    lease_id = _validate_identity(lease_authority["lease_id"], "lease id")
    pre_update_head = _validate_sha(pre_update_head, "pre-update head")
    target_head = _validate_sha(target_head, "target head")
    pre_update_branch = _validate_branch_name(pre_update_branch)
    branch = _validate_branch_name(branch)
    if selected_pre_head is not None:
        selected_pre_head = _validate_sha(selected_pre_head, "selected pre-update head")
    path = _staging_path(home)
    manifest_path, state_path, _ = _manifest_paths(home)
    if path.exists() or manifest_path.exists() or state_path.exists():
        raise ActivationError("an earlier update transaction is unresolved")
    now = int(time.time())
    payload = {
        "schema_version": 3,
        "invocation_id": invocation_id,
        "lease_id": lease_id,
        "lease_authority": lease_authority,
        "root": _canonical(root),
        "phase": "source-prepared",
        "pre_update_head": pre_update_head,
        "pre_update_branch": pre_update_branch,
        "branch": branch,
        "selected_pre_head": selected_pre_head,
        "target_head": target_head,
        "candidate": None,
        "provisioned_generation": None,
        "created_at": now,
        "updated_at": now,
    }
    _atomic_write(path, _json_bytes(payload))
    return path


def _validated_staging_journal(
    root: Path,
    home: Path,
    invocation: str,
    lease: str,
    *,
    finalizing_candidate: Path | None = None,
) -> tuple[dict, bytes]:
    value, raw = _read_json(_staging_path(home))
    expected = {
        "schema_version",
        "invocation_id",
        "lease_id",
        "lease_authority",
        "root",
        "phase",
        "pre_update_head",
        "pre_update_branch",
        "branch",
        "selected_pre_head",
        "target_head",
        "candidate",
        "provisioned_generation",
        "created_at",
        "updated_at",
    }
    if set(value) != expected or value.get("schema_version") != 3:
        raise ActivationError("staging journal schema is invalid")
    if value.get("invocation_id") != invocation or value.get("lease_id") != lease:
        raise ActivationError("staging journal identity is stale")
    if _canonical(str(value.get("root", ""))) != _canonical(root):
        raise ActivationError("staging journal root is stale")
    _validated_lease_authority(
        root,
        value.get("lease_authority"),
        expected_lease_id=lease,
    )
    if value.get("phase") not in {
        "source-prepared",
        "source-mutating",
        "source-selecting",
        "source-selected",
        "source-fast-forwarding",
        "source-active",
        "candidate-staging",
    }:
        raise ActivationError("staging journal phase is invalid")
    _validate_sha(value.get("pre_update_head"), "pre-update head")
    _validate_sha(value.get("target_head"), "target head")
    _validate_branch_name(value.get("pre_update_branch"))
    _validate_branch_name(value.get("branch"))
    selected_pre_head = value.get("selected_pre_head")
    if selected_pre_head is not None:
        _validate_sha(selected_pre_head, "selected pre-update head")
    candidate_claim = value.get("candidate")
    allow_candidate_finalization = False
    if finalizing_candidate is not None and value.get("phase") == "candidate-staging":
        final_relative = _planned_artifact_relative(
            root, finalizing_candidate, "candidate"
        )
        allow_candidate_finalization = (
            isinstance(candidate_claim, dict)
            and candidate_claim.get("rel") == final_relative
            and candidate_claim.get("identity_sha256") is None
        )
    _validated_staging_artifact(
        root,
        candidate_claim,
        "candidate",
        invocation,
        lease,
        allow_unidentified_nonempty=allow_candidate_finalization,
    )
    _validated_staging_artifact(
        root,
        value.get("provisioned_generation"),
        "generation",
        invocation,
        lease,
    )
    created_at = value.get("created_at")
    updated_at = value.get("updated_at")
    if (
        type(created_at) is not int
        or type(updated_at) is not int
        or created_at <= 0
        or updated_at < created_at
    ):
        raise ActivationError("staging journal timestamp is invalid")
    return value, raw


def update_staging_journal(
    root: Path,
    *,
    home: Path,
    invocation_id: str,
    lease_id: str,
    phase: str,
    candidate: Path | None = None,
    provisioned_generation: Path | None = None,
) -> None:
    value, _ = _validated_staging_journal(
        root,
        home,
        invocation_id,
        lease_id,
        finalizing_candidate=(
            candidate if phase == "candidate-staging" else None
        ),
    )
    if phase not in {
        "source-mutating",
        "source-selecting",
        "source-selected",
        "source-fast-forwarding",
        "source-active",
        "candidate-staging",
    }:
        raise ActivationError("staging journal phase transition is invalid")
    value["phase"] = phase
    if candidate is not None:
        value["candidate"] = _staging_artifact_claim(
            root, candidate, "candidate", invocation_id, lease_id
        )
    if provisioned_generation is not None:
        value["provisioned_generation"] = _staging_artifact_claim(
            root,
            provisioned_generation,
            "generation",
            invocation_id,
            lease_id,
        )
    value["updated_at"] = int(time.time())
    _atomic_write(_staging_path(home), _json_bytes(value))


def retire_staging_journal(
    root: Path,
    *,
    home: Path,
    invocation_id: str,
    lease_id: str,
) -> None:
    _, raw = _validated_staging_journal(root, home, invocation_id, lease_id)
    _unlink_exact(_staging_path(home), raw)


def _candidate_relative(root: Path, candidate: Path) -> str:
    runtime_root = (root / ".hermes-runtime").resolve()
    resolved = candidate.resolve()
    if candidate.is_symlink() or not _is_within(resolved, runtime_root):
        raise ActivationError("candidate environment is outside the runtime root")
    if resolved.parent != runtime_root or _CANDIDATE_RE.fullmatch(resolved.name) is None:
        raise ActivationError("candidate environment name is invalid")
    return resolved.relative_to(root.resolve()).as_posix()


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ActivationError("activation directory identity is unreadable") from exc
    attributes = int(getattr(value, "st_file_attributes", 0))
    if not stat.S_ISDIR(value.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise ActivationError("activation directory identity is unsafe")
    return int(value.st_dev), int(value.st_ino)


def _generation_identity_digest(
    relative: str,
    identity: tuple[int, int],
    invocation: str,
    lease: str,
) -> str:
    raw = (
        f"hermes-generation-v1\0{relative}\0{identity[0]}\0{identity[1]}\0"
        f"{invocation}\0{lease}"
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _generation_relative(root: Path, generation: Path) -> str:
    root = root.resolve()
    runtime_root = root / ".hermes-runtime"
    python_root = runtime_root / "python"
    resolved = generation.resolve()
    if (
        generation.is_symlink()
        or resolved.parent != python_root.resolve()
        or _GENERATION_RE.fullmatch(resolved.name) is None
        or not _is_within(resolved, root)
    ):
        raise ActivationError("provisioned generation path is invalid")
    _directory_identity(runtime_root)
    _directory_identity(python_root)
    _directory_identity(resolved)
    return resolved.relative_to(root).as_posix()


def _generation_claim(
    root: Path,
    generation: Path | None,
    invocation: str,
    lease: str,
) -> dict | None:
    if generation is None:
        return None
    relative = _generation_relative(root, generation)
    identity = _directory_identity(generation)
    return {
        "rel": relative,
        "identity_sha256": _generation_identity_digest(
            relative,
            identity,
            invocation,
            lease,
        ),
    }


def _validate_generation_claim(
    root: Path,
    claim: object,
    invocation: str,
    lease: str,
    *,
    missing_ok: bool = False,
) -> Path | None:
    if claim is None:
        return None
    if not isinstance(claim, dict) or set(claim) != {"rel", "identity_sha256"}:
        raise ActivationError("provisioned generation claim is invalid")
    relative = claim.get("rel")
    digest = claim.get("identity_sha256")
    if (
        not isinstance(relative, str)
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ActivationError("provisioned generation claim is invalid")
    path = root / Path(relative)
    if not os.path.lexists(path):
        if missing_ok:
            return path
        raise ActivationError("provisioned generation is missing")
    canonical = _generation_relative(root, path)
    identity = _directory_identity(path)
    if canonical != relative or not hmac.compare_digest(
        digest,
        _generation_identity_digest(relative, identity, invocation, lease),
    ):
        raise ActivationError("provisioned generation identity changed")
    return path


def write_activation_manifest(
    root: Path,
    *,
    home: Path,
    invocation_id: str,
    lease_id: str,
    candidate: Path,
    provisioned_generation: Path | None,
    pre_update_head: str,
    pre_update_branch: str,
    selected_pre_head: str | None,
    branch: str,
    remote: str,
    target_ref: str,
    target_sha: str,
    python_health: dict[str, bool],
) -> dict:
    """Publish a candidate claim without changing the live venv or receipt."""
    root = root.resolve()
    home = home.resolve()
    _refuse_existing_commit_coordinator(home)
    invocation_id = _validate_identity(invocation_id, "invocation id")
    lease_id = _validate_identity(lease_id, "lease id")
    pre_update_head = _validate_sha(pre_update_head, "pre-update head")
    target_sha = _validate_sha(target_sha, "target head")
    pre_update_branch = _validate_branch_name(pre_update_branch)
    if selected_pre_head is not None:
        selected_pre_head = _validate_sha(selected_pre_head, "selected pre-update head")
    branch, remote, target_ref = _validate_update_target(branch, remote, target_ref)
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
        "schema_version": 2,
        "invocation_id": invocation_id,
        "lease_id": lease_id,
        "root": _canonical(root),
        "candidate_rel": _candidate_relative(root, candidate),
        "provisioned_generation": _generation_claim(
            root,
            provisioned_generation,
            invocation_id,
            lease_id,
        ),
        "pre_update_head": pre_update_head,
        "pre_update_branch": pre_update_branch,
        "selected_pre_head": selected_pre_head,
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


def _expected_attempt() -> str:
    return _validate_identity(
        os.environ.get("HERMES_INTERNAL_DESKTOP_UPDATE_ATTEMPT"),
        "attempt id",
    )


def _read_regular_json(
    path: Path,
    maximum: int = _MAX_PROTOCOL_BYTES,
) -> tuple[dict, bytes]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ActivationError("required protocol artifact is unavailable") from exc
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    if (
        not stat.S_ISREG(metadata.st_mode)
        or attributes & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ActivationError("protocol artifact type is invalid")
    return _read_json(path, maximum)


def _publish_exclusive(path: Path, payload: bytes, *, attempt: str) -> None:
    """Publish complete bytes at a previously absent fixed path."""
    if not payload or len(payload) > _MAX_PROTOCOL_BYTES:
        raise ActivationError("refusing an unbounded protocol artifact")
    temporary = path.with_name(f"{path.name}.{attempt}.publishing")
    try:
        if os.path.lexists(temporary):
            existing = _read_bytes(temporary, _MAX_PROTOCOL_BYTES)
            if existing != payload:
                raise ActivationError("commit decision publication is unresolved")
        else:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
            try:
                offset = 0
                while offset < len(payload):
                    written = os.write(descriptor, payload[offset:])
                    if written <= 0:
                        raise OSError("short commit decision write")
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.link(temporary, path)
    except FileExistsError:
        raise ActivationError("commit decision already exists") from None
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            from hermes_cli.update_diagnostics import log_update_failure

            log_update_failure(
                logger,
                code="HDA201",
                stage="publication-cleanup",
                kind="io",
                level=logging.WARNING,
            )


def _validate_commit_path(value: object, *, root: Path, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_PATH_CHARS
    ):
        raise ActivationError(f"{label} is invalid")
    candidate = Path(value)
    canonical = _canonical(candidate)
    if not candidate.is_absolute() or not _is_within(candidate, root):
        raise ActivationError(f"{label} is outside the install root")
    return canonical


def _validate_gateway_runtime_claims(
    runtimes: object,
    *,
    root: Path,
    home: Path,
) -> list[dict]:
    if not isinstance(runtimes, list) or len(runtimes) > _MAX_COMMIT_RUNTIMES:
        raise ActivationError("gateway runtime identities are invalid")
    seen_profiles: set[str] = set()
    for runtime in runtimes:
        expected = {
            "pid",
            "creation_file_time",
            "executable_path",
            "profile",
            "profile_home",
        }
        if not isinstance(runtime, dict) or set(runtime) != expected:
            raise ActivationError("gateway runtime identity is invalid")
        profile = runtime.get("profile")
        creation = runtime.get("creation_file_time")
        if (
            not isinstance(profile, str)
            or _PROFILE_NAME_RE.fullmatch(profile) is None
            or profile in seen_profiles
            or type(runtime.get("pid")) is not int
            or runtime["pid"] <= 0
            or not isinstance(creation, str)
            or re.fullmatch(r"[1-9][0-9]{0,19}", creation) is None
            or int(creation) > (1 << 64) - 1
        ):
            raise ActivationError("gateway runtime identity is invalid")
        _validate_commit_path(
            runtime.get("executable_path"),
            root=root,
            label="gateway executable",
        )
        expected_home = home if profile == "default" else home / "profiles" / profile
        if _canonical(str(runtime.get("profile_home", ""))) != _canonical(expected_home):
            raise ActivationError("gateway profile home identity is invalid")
        seen_profiles.add(profile)
    if runtimes != sorted(runtimes, key=lambda item: (item["profile"], item["pid"])):
        raise ActivationError("gateway runtime identities are not canonical")
    return runtimes


def _validated_commit_document(
    value: object,
    *,
    root: Path,
    home: Path,
    attempt: str,
    invocation: str,
    lease: str,
) -> dict:
    expected = {
        "schema_version",
        "revision",
        "decision",
        "attempt_id",
        "invocation_id",
        "lease_id",
        "root",
        "target_head",
        "desktop_ack_sha256",
        "desktop_identity",
        "activation_manifest_sha256",
        "activation_state_sha256",
        "update_receipt_sha256",
        "gateway_plan_sha256",
        "gateway_runtime_manifest_sha256",
        "gateway_runtimes",
        "pending_result_sha256",
        "created_at",
        "auth",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ActivationError("commit decision schema is invalid")
    revision = value.get("revision")
    if (
        value.get("schema_version") != 1
        or type(revision) is not int
        or revision != 1
        or value.get("decision") != "commit"
        or value.get("attempt_id") != attempt
        or value.get("invocation_id") != invocation
        or value.get("lease_id") != lease
        or _canonical(str(value.get("root", ""))) != _canonical(root)
    ):
        raise ActivationError("commit decision identity is invalid")
    _validate_identity(attempt, "attempt id")
    _validate_sha(value.get("target_head"), "commit target head")
    for field in (
        "desktop_ack_sha256",
        "activation_manifest_sha256",
        "activation_state_sha256",
        "update_receipt_sha256",
        "gateway_plan_sha256",
        "gateway_runtime_manifest_sha256",
        "pending_result_sha256",
    ):
        digest = value.get(field)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ActivationError("commit decision digest is invalid")

    desktop = value.get("desktop_identity")
    desktop_keys = {
        "pid",
        "process_started_at",
        "root",
        "executable",
        "build_id",
        "acknowledged_at",
    }
    if not isinstance(desktop, dict) or set(desktop) != desktop_keys:
        raise ActivationError("Desktop commit identity is invalid")
    if (
        type(desktop.get("pid")) is not int
        or desktop["pid"] <= 0
        or type(desktop.get("process_started_at")) is not int
        or desktop["process_started_at"] <= 0
        or type(desktop.get("acknowledged_at")) is not int
        or desktop["acknowledged_at"] <= 0
        or _canonical(str(desktop.get("root", ""))) != _canonical(root)
        or _validate_sha(desktop.get("build_id"), "Desktop build id")
        != value["target_head"]
    ):
        raise ActivationError("Desktop commit identity is invalid")
    _validate_commit_path(
        desktop.get("executable"), root=root, label="Desktop executable"
    )

    _validate_gateway_runtime_claims(
        value.get("gateway_runtimes"), root=root, home=home
    )
    created_at = value.get("created_at")
    if type(created_at) is not int or created_at <= 0 or created_at > int(time.time()) + 30:
        raise ActivationError("commit decision timestamp is invalid")
    unsigned = {key: value[key] for key in expected if key != "auth"}
    auth = value.get("auth")
    if not isinstance(auth, str) or not hmac.compare_digest(
        auth, _commit_document_auth(unsigned, lease)
    ):
        raise ActivationError("commit decision authentication failed")
    return {**unsigned, "auth": auth}


def _validated_gateway_plan(
    root: Path,
    invocation: str,
    lease: str,
    value: object,
) -> None:
    expected = {
        "schema_version",
        "invocation_id",
        "lease_fingerprint",
        "install_root",
        "created_at",
        "expires_at",
        "profiles",
        "cold_start_if_installed",
        "auth",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ActivationError("gateway plan schema is invalid")
    created_at = value.get("created_at")
    expires_at = value.get("expires_at")
    if (
        value.get("schema_version") != 1
        or value.get("invocation_id") != invocation
        or value.get("lease_fingerprint")
        != hashlib.sha256(lease.encode("utf-8")).hexdigest()
        or _canonical(str(value.get("install_root", ""))) != _canonical(root)
        or type(created_at) is not int
        or type(expires_at) is not int
        or created_at <= 0
        or not created_at <= int(time.time()) <= expires_at
        or expires_at - created_at > _MAX_GATEWAY_PLAN_SECONDS
        or type(value.get("cold_start_if_installed")) is not bool
    ):
        raise ActivationError("gateway plan identity is invalid")
    profiles = value.get("profiles")
    if not isinstance(profiles, list) or len(profiles) > _MAX_COMMIT_RUNTIMES:
        raise ActivationError("gateway plan profiles are invalid")
    seen: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, dict) or set(profile) != {
            "name",
            "old_pid",
            "created_at",
        }:
            raise ActivationError("gateway plan profile is invalid")
        name = profile.get("name")
        process_created_at = profile.get("created_at")
        if (
            not isinstance(name, str)
            or _PROFILE_NAME_RE.fullmatch(name) is None
            or name in seen
            or type(profile.get("old_pid")) is not int
            or profile["old_pid"] <= 0
            or isinstance(process_created_at, bool)
            or not isinstance(process_created_at, (int, float))
            or not math.isfinite(float(process_created_at))
            or process_created_at <= 0
        ):
            raise ActivationError("gateway plan profile is invalid")
        seen.add(name)
    unsigned = {key: value[key] for key in expected if key != "auth"}
    if not isinstance(value.get("auth"), str) or not hmac.compare_digest(
        value["auth"], _commit_document_auth(unsigned, lease)
    ):
        raise ActivationError("gateway plan authentication failed")


def _validated_gateway_runtime_manifest(
    root: Path,
    home: Path,
    invocation: str,
    lease: str,
    value: object,
    plan_digest: str,
    claimed_runtimes: list[dict],
) -> None:
    expected = {
        "schema_version",
        "invocation_id",
        "install_root",
        "plan_sha256",
        "runtimes",
        "auth",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ActivationError("gateway runtime manifest schema is invalid")
    if (
        value.get("schema_version") != 1
        or value.get("invocation_id") != invocation
        or _canonical(str(value.get("install_root", ""))) != _canonical(root)
        or value.get("plan_sha256") != plan_digest
    ):
        raise ActivationError("gateway runtime manifest identity is invalid")
    runtimes = value.get("runtimes")
    if not isinstance(runtimes, list) or len(runtimes) > _MAX_COMMIT_RUNTIMES:
        raise ActivationError("gateway runtime manifest is invalid")
    projected: list[dict] = []
    for runtime in runtimes:
        if not isinstance(runtime, dict) or set(runtime) != {
            "profile",
            "pid",
            "created_at",
            "creation_file_time",
            "executable_path",
            "profile_home",
        }:
            raise ActivationError("gateway runtime manifest entry is invalid")
        created_at = runtime.get("created_at")
        if (
            isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or not math.isfinite(float(created_at))
            or created_at <= 0
        ):
            raise ActivationError("gateway runtime timestamp is invalid")
        projected.append(
            {
                key: runtime[key]
                for key in (
                    "pid",
                    "creation_file_time",
                    "executable_path",
                    "profile",
                    "profile_home",
                )
            }
        )
        if (
            not Path(str(runtime["executable_path"])).is_file()
            or not Path(str(runtime["profile_home"])).is_dir()
        ):
            raise ActivationError("gateway runtime path identity is unavailable")
    _validate_gateway_runtime_claims(projected, root=root, home=home)
    if projected != claimed_runtimes:
        raise ActivationError("gateway runtime identities changed before commit")
    unsigned = {key: value[key] for key in expected if key != "auth"}
    if not isinstance(value.get("auth"), str) or not hmac.compare_digest(
        value["auth"], _commit_document_auth(unsigned, lease)
    ):
        raise ActivationError("gateway runtime manifest authentication failed")


def _validated_manifest(
    root: Path,
    home: Path,
    invocation: str,
    lease: str,
    *,
    generation_missing_ok: bool = False,
) -> tuple[dict, bytes]:
    manifest_path, _, _ = _manifest_paths(home)
    manifest, raw = _read_json(manifest_path)
    expected_keys = {
        "schema_version",
        "invocation_id",
        "lease_id",
        "root",
        "candidate_rel",
        "provisioned_generation",
        "pre_update_head",
        "pre_update_branch",
        "selected_pre_head",
        "target_head",
        "branch",
        "remote",
        "target_ref",
        "prior_receipt_sha256",
        "python_health",
        "created_at",
    }
    if set(manifest) != expected_keys or manifest.get("schema_version") != 2:
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
    _validate_branch_name(manifest.get("pre_update_branch"))
    selected_pre_head = manifest.get("selected_pre_head")
    if selected_pre_head is not None:
        _validate_sha(selected_pre_head, "selected pre-update head")
    try:
        _validate_update_target(
            manifest.get("branch"),
            manifest.get("remote"),
            manifest.get("target_ref"),
        )
    except ActivationError as exc:
        raise ActivationError("activation manifest target is invalid") from exc
    _validate_generation_claim(
        root,
        manifest.get("provisioned_generation"),
        invocation,
        lease,
        missing_ok=generation_missing_ok,
    )
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


def _git_branch(root: Path) -> str | None:
    result = _git_output(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if result.returncode == 1:
        return None
    branch = result.stdout.strip()
    if result.returncode != 0:
        raise ActivationError("installed Git branch identity is unreadable")
    return _validate_branch_name(branch)


def _git_local_branch_head(root: Path, branch: str) -> str | None:
    branch = _validate_branch_name(branch)
    result = _git_output(root, "show-ref", "--verify", "--hash", f"refs/heads/{branch}")
    if result.returncode == 1:
        return None
    head = result.stdout.strip().lower()
    if result.returncode != 0 or _SHA_RE.fullmatch(head) is None:
        raise ActivationError("local Git branch identity is unreadable")
    return head


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
        "schema_version": 3,
        "invocation_id": invocation,
        "lease_id": lease,
        "root": manifest["root"],
        "phase": phase,
        "candidate_rel": manifest["candidate_rel"],
        "provisioned_generation": manifest["provisioned_generation"],
        "backup_rel": backup_rel,
        "rejected_rel": rejected_rel,
        "pre_update_head": manifest["pre_update_head"],
        "pre_update_branch": manifest["pre_update_branch"],
        "branch": manifest["branch"],
        "selected_pre_head": manifest["selected_pre_head"],
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
        "desktop_health_sha256": None,
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
    _, state_path, _ = _manifest_paths(home)
    state, state_raw = _read_json(state_path)
    cleanup_phases = {
        "commit-cleaning",
        "rollback-cleaning-published",
        "rollback-cleaning-unpublished",
    }
    manifest, manifest_raw = _validated_manifest(
        root,
        home,
        invocation,
        lease,
        generation_missing_ok=state.get("phase") in cleanup_phases,
    )
    expected_keys = {
        "schema_version",
        "invocation_id",
        "lease_id",
        "root",
        "phase",
        "candidate_rel",
        "provisioned_generation",
        "backup_rel",
        "rejected_rel",
        "pre_update_head",
        "pre_update_branch",
        "branch",
        "selected_pre_head",
        "target_head",
        "manifest_sha256",
        "prior_receipt_sha256",
        "prior_receipt_b64",
        "candidate_receipt_b64",
        "published_receipt_sha256",
        "desktop_health_sha256",
        "move_error",
        "created_at",
        "updated_at",
    }
    if set(state) != expected_keys or state.get("schema_version") != 3:
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
        or state.get("provisioned_generation")
        != manifest.get("provisioned_generation")
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
        or state.get("pre_update_branch") != manifest.get("pre_update_branch")
        or state.get("branch") != manifest.get("branch")
        or state.get("selected_pre_head") != manifest.get("selected_pre_head")
        or state.get("target_head") != manifest.get("target_head")
        or state.get("prior_receipt_sha256") != manifest.get("prior_receipt_sha256")
    ):
        raise ActivationError("activation state generation changed")
    _validate_generation_claim(
        root,
        state.get("provisioned_generation"),
        invocation,
        lease,
        missing_ok=state.get("phase") in cleanup_phases,
    )
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
        "commit-cleaning",
        "rollback-cleaning-published",
        "rollback-cleaning-unpublished",
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
    desktop_health_digest = state.get("desktop_health_sha256")
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
        "rollback-cleaning-unpublished",
    }:
        if (
            candidate_b64 is not None
            or published_digest is not None
            or desktop_health_digest is not None
        ):
            raise ActivationError("activation state published receipt is premature")
    else:
        if (
            not isinstance(candidate_b64, str)
            or not isinstance(published_digest, str)
            or not isinstance(desktop_health_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", desktop_health_digest) is None
        ):
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


def _load_terminal_cleanup_state(
    root: Path,
    home: Path,
    invocation: str,
    lease: str,
    *,
    action: str,
) -> tuple[dict, bytes]:
    """Validate the final one-file state after the manifest was retired."""
    _, state_path, receipt_path = _manifest_paths(home)
    state, raw = _read_json(state_path)
    expected_keys = {
        "schema_version",
        "invocation_id",
        "lease_id",
        "root",
        "phase",
        "candidate_rel",
        "provisioned_generation",
        "backup_rel",
        "rejected_rel",
        "pre_update_head",
        "pre_update_branch",
        "branch",
        "selected_pre_head",
        "target_head",
        "manifest_sha256",
        "prior_receipt_sha256",
        "prior_receipt_b64",
        "candidate_receipt_b64",
        "published_receipt_sha256",
        "desktop_health_sha256",
        "move_error",
        "created_at",
        "updated_at",
    }
    allowed_phases = (
        {"commit-cleaning"}
        if action == "commit"
        else {"rollback-cleaning-published", "rollback-cleaning-unpublished"}
    )
    if (
        set(state) != expected_keys
        or state.get("schema_version") != 3
        or state.get("phase") not in allowed_phases
        or state.get("invocation_id") != invocation
        or state.get("lease_id") != lease
        or _canonical(str(state.get("root", ""))) != _canonical(root)
        or state.get("move_error") is not None
    ):
        raise ActivationError("terminal activation cleanup state is invalid")
    _validate_sha(state.get("pre_update_head"), "pre-update head")
    _validate_sha(state.get("target_head"), "target head")
    _validate_branch_name(state.get("pre_update_branch"))
    _validate_branch_name(state.get("branch"))
    selected_pre_head = state.get("selected_pre_head")
    if selected_pre_head is not None:
        _validate_sha(selected_pre_head, "selected pre-update head")
    for field in ("candidate_rel", "backup_rel", "rejected_rel"):
        relative = state.get(field)
        if (
            not isinstance(relative, str)
            or len(relative) > _MAX_PATH_CHARS
            or not _is_within(root / relative, root / ".hermes-runtime")
        ):
            raise ActivationError("terminal cleanup path is invalid")
    _validate_generation_claim(
        root,
        state.get("provisioned_generation"),
        invocation,
        lease,
        missing_ok=True,
    )
    for field in ("manifest_sha256", "desktop_health_sha256"):
        value = state.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ActivationError("terminal cleanup digest is invalid")
    created_at = state.get("created_at")
    updated_at = state.get("updated_at")
    if (
        type(created_at) is not int
        or type(updated_at) is not int
        or created_at <= 0
        or updated_at < created_at
    ):
        raise ActivationError("terminal cleanup timestamp is invalid")

    receipt = _read_bytes(receipt_path, _MAX_RECEIPT_BYTES, missing_ok=True)
    expected_receipt = (
        state.get("published_receipt_sha256")
        if action == "commit"
        else state.get("prior_receipt_sha256")
    )
    if _digest(receipt) != expected_receipt:
        raise ActivationError("terminal cleanup receipt identity changed")
    if action == "commit":
        if (
            _git_head(root) != state["target_head"]
            or _git_branch(root) != state["branch"]
            or not _git_clean(root)
            or not _smoke_live(root / "venv", root)
        ):
            raise ActivationError("committed installation lost its final health proof")
    elif (
        _git_head(root) != state["pre_update_head"]
        or _git_branch(root) != state["pre_update_branch"]
        or not _git_clean(root)
        or not _venv_python(root / "venv").is_file()
    ):
        raise ActivationError("rolled back installation lost its final recovery proof")
    return state, raw


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


def _remove_generation_claim(
    root: Path,
    claim: object,
    invocation: str,
    lease: str,
) -> None:
    path = _validate_generation_claim(
        root,
        claim,
        invocation,
        lease,
        missing_ok=True,
    )
    if path is None or not os.path.lexists(path):
        return
    if (
        path.parent.resolve() != (root / ".hermes-runtime" / "python").resolve()
        or _GENERATION_RE.fullmatch(path.name) is None
        or path.is_symlink()
    ):
        raise ActivationError("refusing to remove an out-of-scope generation")
    _directory_identity(path)
    shutil.rmtree(path)


def activate() -> None:
    root, home, invocation, lease = _expected_context()
    manifest, manifest_raw = _validated_manifest(root, home, invocation, lease)
    _, state_path, receipt_path = _manifest_paths(home)
    if state_path.exists():
        raise ActivationError("activation state already exists")
    if (
        _git_head(root) != manifest["target_head"]
        or _git_branch(root) != manifest["branch"]
        or not _git_clean(root)
    ):
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


def _validated_desktop_health(
    root: Path,
    home: Path,
    manifest: dict,
    *,
    require_current_node: bool = True,
) -> tuple[dict, bytes]:
    value, raw = _read_json(
        _desktop_health_path(home),
        _MAX_DESKTOP_HEALTH_BYTES,
    )
    if set(value) != {
        "schema_version",
        "invocation_id",
        "lease_id",
        "root",
        "target_head",
        "branch",
        "build_exit_code",
        "node_dependencies",
        "desktop_rebuild",
        "created_at",
    }:
        raise ActivationError("Desktop health proof schema is invalid")
    if (
        value.get("schema_version") != 1
        or value.get("invocation_id") != manifest["invocation_id"]
        or value.get("lease_id") != manifest["lease_id"]
        or _canonical(str(value.get("root", ""))) != _canonical(root)
        or value.get("target_head") != manifest["target_head"]
        or value.get("branch") != manifest["branch"]
        or type(value.get("build_exit_code")) is not int
        or value.get("build_exit_code") != 0
        or value.get("node_dependencies") is not True
        or value.get("desktop_rebuild") is not True
    ):
        raise ActivationError("Desktop health proof identity is stale")
    created_at = value.get("created_at")
    now = int(time.time())
    if (
        type(created_at) is not int
        or created_at < manifest["created_at"]
        or created_at > now + 300
    ):
        raise ActivationError("Desktop health proof timestamp is invalid")
    if require_current_node:
        try:
            from hermes_cli.update_cmd import _node_dependencies_healthy_read_only

            node_healthy = _node_dependencies_healthy_read_only()
        except Exception as exc:
            raise ActivationError(
                "current Node dependency health could not be verified"
            ) from exc
        if node_healthy is not True:
            raise ActivationError(
                "current Node dependency health could not be verified"
            )
    return value, raw


def _remove_desktop_health(
    root: Path,
    home: Path,
    manifest: dict,
    *,
    expected_digest: str | None,
) -> None:
    path = _desktop_health_path(home)
    if not path.exists():
        return
    _, raw = _validated_desktop_health(
        root,
        home,
        manifest,
        require_current_node=False,
    )
    if expected_digest is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(),
        expected_digest,
    ):
        raise ActivationError("Desktop health proof changed before cleanup")
    _unlink_exact(path, raw)


def _build_receipt(manifest: dict, desktop_health: dict) -> dict:
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
            "node_dependencies": desktop_health["node_dependencies"],
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
    if (
        _git_head(root) != manifest["target_head"]
        or _git_branch(root) != manifest["branch"]
        or not _git_clean(root)
    ):
        raise ActivationError("source identity changed before receipt publication")
    if not _smoke_live(root / "venv", root):
        raise ActivationError("the active environment lost its health proof")
    current_receipt = _read_bytes(receipt_path, _MAX_RECEIPT_BYTES, missing_ok=True)
    if _digest(current_receipt) != state["prior_receipt_sha256"]:
        raise ActivationError("the prior update receipt changed during activation")

    desktop_health, desktop_health_raw = _validated_desktop_health(
        root, home, manifest
    )
    receipt_raw = _json_bytes(_build_receipt(manifest, desktop_health))
    state["phase"] = "publishing"
    state["candidate_receipt_b64"] = base64.b64encode(receipt_raw).decode("ascii")
    state["published_receipt_sha256"] = _digest(receipt_raw)
    state["desktop_health_sha256"] = hashlib.sha256(
        desktop_health_raw
    ).hexdigest()
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


def _restore_source(
    root: Path,
    *,
    pre_update_head: str,
    pre_update_branch: str,
    branch: str,
    selected_pre_head: str | None,
    target_head: str,
    source_phase: str = "source-active",
) -> None:
    pre_update_head = _validate_sha(pre_update_head, "pre-update head")
    target_head = _validate_sha(target_head, "target head")
    pre_update_branch = _validate_branch_name(pre_update_branch)
    branch = _validate_branch_name(branch)
    if selected_pre_head is not None:
        selected_pre_head = _validate_sha(selected_pre_head, "selected pre-update head")
    if source_phase not in {
        "source-prepared",
        "source-mutating",
        "source-selecting",
        "source-selected",
        "source-fast-forwarding",
        "source-active",
        "candidate-staging",
    }:
        raise ActivationError("source rollback phase is invalid")
    if not _git_clean(root):
        raise ActivationError("source rollback refused because tracked files changed")

    current_head = _git_head(root)
    current_branch = _git_branch(root)
    selected_head = _git_local_branch_head(root, branch)
    if current_head == pre_update_head and current_branch == pre_update_branch:
        if pre_update_branch != branch and selected_head != selected_pre_head:
            raise ActivationError("selected branch identity changed during rollback")
        return

    if pre_update_branch == branch:
        if (
            current_branch != branch
            or current_head != target_head
            or selected_head != target_head
        ):
            raise ActivationError("source rollback target identity is stale")
        result = _git_output(root, "reset", "--hard", pre_update_head)
        if (
            result.returncode != 0
            or _git_head(root) != pre_update_head
            or _git_branch(root) != pre_update_branch
            or not _git_clean(root)
        ):
            raise ActivationError("source rollback failed")
        return

    pre_branch_head = _git_local_branch_head(root, pre_update_branch)
    if pre_branch_head != pre_update_head:
        raise ActivationError("original branch identity changed during rollback")

    # Recover any exact partial phase left by a crash in the detach/CAS/checkout
    # sequence. No custom branch is ever reset.
    if current_branch == branch:
        allowed_selected_heads: set[str] = set()
        if source_phase in {"source-mutating", "source-selecting", "source-selected"}:
            allowed_selected_heads.add(selected_pre_head or target_head)
        if source_phase in {"source-mutating", "source-fast-forwarding"}:
            allowed_selected_heads.add(selected_pre_head or target_head)
            allowed_selected_heads.add(target_head)
        if source_phase in {"source-active", "candidate-staging"}:
            allowed_selected_heads.add(target_head)
        if (
            current_head not in allowed_selected_heads
            or selected_head != current_head
        ):
            raise ActivationError("source rollback target identity is stale")
        detached = _git_output(root, "checkout", "--detach", pre_update_head)
        if detached.returncode != 0:
            raise ActivationError("source rollback could not detach safely")
        current_branch = _git_branch(root)
        current_head = _git_head(root)
    if current_branch is None and current_head == pre_update_head:
        selected_head = _git_local_branch_head(root, branch)
        if selected_head == target_head:
            if selected_pre_head is None:
                restored = _git_output(
                    root,
                    "update-ref",
                    "--no-deref",
                    "-d",
                    f"refs/heads/{branch}",
                    target_head,
                )
            else:
                restored = _git_output(
                    root,
                    "update-ref",
                    "--no-deref",
                    f"refs/heads/{branch}",
                    selected_pre_head,
                    target_head,
                )
            if restored.returncode != 0:
                raise ActivationError("selected branch rollback CAS failed")
        elif selected_head != selected_pre_head:
            raise ActivationError("selected branch identity changed during rollback")
        checkout = _git_output(root, "checkout", pre_update_branch)
        if checkout.returncode != 0:
            raise ActivationError("original branch could not be restored")

    if (
        _git_head(root) != pre_update_head
        or _git_branch(root) != pre_update_branch
        or _git_local_branch_head(root, branch) != selected_pre_head
        or not _git_clean(root)
    ):
        raise ActivationError("source rollback failed")


def _unlink_exact(path: Path, expected_raw: bytes) -> None:
    current = _read_bytes(path, _MAX_PROTOCOL_BYTES)
    if current != expected_raw:
        raise ActivationError("protocol artifact changed before cleanup")
    path.unlink()


def _remove_correlated_staging_journal(
    root: Path,
    home: Path,
    invocation: str,
    lease: str,
    manifest: dict,
) -> None:
    staging_path = _staging_path(home)
    if not staging_path.exists():
        return
    journal, staging_raw = _validated_staging_journal(
        root, home, invocation, lease
    )
    if (
        journal["pre_update_head"] != manifest["pre_update_head"]
        or journal["pre_update_branch"] != manifest["pre_update_branch"]
        or journal["branch"] != manifest["branch"]
        or journal["selected_pre_head"] != manifest["selected_pre_head"]
        or journal["target_head"] != manifest["target_head"]
    ):
        raise ActivationError("coexisting staging journal identity changed")
    _unlink_exact(staging_path, staging_raw)


def _validated_commit_coordinator(
    root: Path,
    home: Path,
    invocation: str,
    lease: str,
    *,
    manifest_raw: bytes | None,
    state: dict,
    state_raw: bytes,
    receipt_raw: bytes,
) -> tuple[dict, bytes]:
    attempt = _expected_attempt()
    coordinator, raw = _read_regular_json(_commit_coordinator_path(home))
    document = _validated_commit_document(
        coordinator,
        root=root,
        home=home,
        attempt=attempt,
        invocation=invocation,
        lease=lease,
    )
    if (
        document["target_head"] != state["target_head"]
        or document["update_receipt_sha256"] != _digest(receipt_raw)
        or (
            manifest_raw is not None
            and document["activation_manifest_sha256"] != _digest(manifest_raw)
        )
        or (
            manifest_raw is None
            and document["activation_manifest_sha256"]
            != state["manifest_sha256"]
        )
    ):
        raise ActivationError("commit coordinator lost activation correlation")
    if (
        state.get("phase") == "receipt-published"
        and document["activation_state_sha256"] != _digest(state_raw)
    ):
        raise ActivationError("commit coordinator lost decision-state correlation")
    if state.get("phase") == "commit-cleaning":
        decision_state = dict(state)
        decision_state["phase"] = "receipt-published"
        if document["activation_state_sha256"] != _digest(
            _json_bytes(decision_state)
        ):
            raise ActivationError("commit cleanup state is not coordinator-derived")
    return document, raw


def _validate_desktop_commit_ack(
    value: object,
    *,
    root: Path,
    attempt: str,
    invocation: str,
    lease: str,
    target_head: str,
    expected_identity: dict,
) -> None:
    expected = {
        "schema_version",
        "attempt_id",
        "invocation_id",
        "lease_id",
        "pid",
        "process_started_at",
        "root",
        "executable",
        "build_id",
        "build_source",
        "backend_ready",
        "backend_mode",
        "acknowledged_at",
        "error",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ActivationError("Desktop acknowledgement schema is invalid")
    identity = {
        key: value[key]
        for key in (
            "pid",
            "process_started_at",
            "root",
            "executable",
            "build_id",
            "acknowledged_at",
        )
    }
    executable = Path(str(value.get("executable", "")))
    if (
        value.get("schema_version") != 1
        or value.get("attempt_id") != attempt
        or value.get("invocation_id") != invocation
        or value.get("lease_id") != lease
        or identity != expected_identity
        or value.get("build_id") != target_head
        or value.get("build_source") != "install-stamp"
        or value.get("backend_ready") is not True
        or value.get("backend_mode") not in {"local", "remote"}
        or value.get("error") is not None
        or not executable.is_file()
    ):
        raise ActivationError("Desktop acknowledgement identity is invalid")


def _validate_pending_result(
    value: object,
    *,
    root: Path,
    attempt: str,
    invocation: str,
    lease: str,
    branch: str,
) -> None:
    if not isinstance(value, dict):
        raise ActivationError("pending update result is invalid")
    if (
        value.get("schema_version") != 2
        or value.get("attempt_id") != attempt
        or value.get("state") != "pending"
        or value.get("ok") is not False
        or value.get("exit_code") is not None
        or value.get("branch") != branch
        or value.get("invocation_id") != invocation
        or value.get("lease_id") != lease
        or _canonical(str(value.get("root", ""))) != _canonical(root)
    ):
        raise ActivationError("pending update result identity is invalid")


def commit_decided() -> None:
    """Cross the irreversible commit boundary after exact external proofs."""
    root, home, invocation, lease = _expected_context()
    attempt = _expected_attempt()
    proposal_path = _commit_proposal_path(home)
    proposal, proposal_raw = _read_regular_json(proposal_path)
    document = _validated_commit_document(
        proposal,
        root=root,
        home=home,
        attempt=attempt,
        invocation=invocation,
        lease=lease,
    )
    manifest, manifest_raw = _validated_manifest(root, home, invocation, lease)
    state, _, state_raw = _load_state(root, home, invocation, lease)
    if state.get("phase") != "receipt-published":
        raise ActivationError("activation is not ready for commit decision")
    receipt_path = home / _RECEIPT_NAME
    receipt_raw = _read_bytes(receipt_path, _MAX_RECEIPT_BYTES)
    assert receipt_raw is not None
    if (
        document["target_head"] != manifest["target_head"]
        or document["activation_manifest_sha256"] != _digest(manifest_raw)
        or document["activation_state_sha256"] != _digest(state_raw)
        or document["update_receipt_sha256"] != _digest(receipt_raw)
        or state.get("published_receipt_sha256") != _digest(receipt_raw)
    ):
        raise ActivationError("commit proposal lost activation correlation")

    ack_path = home / f".hermes-update-ack-{attempt}.json"
    ack, ack_raw = _read_regular_json(ack_path, _MAX_RECEIPT_BYTES)
    if document["desktop_ack_sha256"] != _digest(ack_raw):
        raise ActivationError("Desktop acknowledgement changed before decision")
    _validate_desktop_commit_ack(
        ack,
        root=root,
        attempt=attempt,
        invocation=invocation,
        lease=lease,
        target_head=manifest["target_head"],
        expected_identity=document["desktop_identity"],
    )

    plan_path = home / f".hermes-gateway-resume-{invocation}.prepared"
    plan, plan_raw = _read_regular_json(plan_path)
    if document["gateway_plan_sha256"] != _digest(plan_raw):
        raise ActivationError("gateway plan changed before commit decision")
    _validated_gateway_plan(root, invocation, lease, plan)
    runtime_path = home / (
        f".hermes-gateway-resume-{invocation}.prepared-runtime.json"
    )
    runtime, runtime_raw = _read_regular_json(runtime_path)
    if document["gateway_runtime_manifest_sha256"] != _digest(runtime_raw):
        raise ActivationError("gateway runtime manifest changed before decision")
    _validated_gateway_runtime_manifest(
        root,
        home,
        invocation,
        lease,
        runtime,
        _digest(plan_raw) or "",
        document["gateway_runtimes"],
    )
    result, result_raw = _read_regular_json(home / ".hermes-update-result.json")
    if document["pending_result_sha256"] != _digest(result_raw):
        raise ActivationError("pending update result changed before decision")
    _validate_pending_result(
        result,
        root=root,
        attempt=attempt,
        invocation=invocation,
        lease=lease,
        branch=manifest["branch"],
    )

    coordinator_path = _commit_coordinator_path(home)
    coordinator_raw = _json_bytes(document)
    if os.path.lexists(coordinator_path):
        existing, existing_raw = _read_regular_json(coordinator_path)
        existing_document = _validated_commit_document(
            existing,
            root=root,
            home=home,
            attempt=attempt,
            invocation=invocation,
            lease=lease,
        )
        if existing_document != document or existing_raw != coordinator_raw:
            raise ActivationError("commit decision conflict requires manual recovery")
        publishing_path = _commit_coordinator_publishing_path(home, attempt)
        if os.path.lexists(publishing_path):
            _unlink_exact(publishing_path, coordinator_raw)
    else:
        _publish_exclusive(coordinator_path, coordinator_raw, attempt=attempt)
        published = _read_bytes(coordinator_path, _MAX_PROTOCOL_BYTES)
        if published != coordinator_raw:
            raise ActivationError("commit decision publication could not be proven")
    _unlink_exact(proposal_path, proposal_raw)


def rollback() -> None:
    root, home, invocation, lease = _expected_context()
    if os.path.lexists(_commit_coordinator_path(home)):
        raise ActivationError("commit is already decided; rollback is forbidden")
    manifest_path, state_path, receipt_path = _manifest_paths(home)
    if not manifest_path.exists():
        _, state_raw = _load_terminal_cleanup_state(
            root,
            home,
            invocation,
            lease,
            action="rollback",
        )
        _unlink_exact(state_path, state_raw)
        return

    state_hint = None
    if state_path.exists():
        state_hint, _ = _read_json(state_path)
    cleanup_phase = str((state_hint or {}).get("phase", "")).startswith(
        "rollback-cleaning-"
    )
    manifest, manifest_raw = _validated_manifest(
        root,
        home,
        invocation,
        lease,
        generation_missing_ok=cleanup_phase,
    )
    desktop_health_digest: str | None
    if state_path.exists():
        state, _, state_raw = _load_state(root, home, invocation, lease)
        desktop_health_digest = state.get("desktop_health_sha256")
        phase = str(state.get("phase", ""))
        if phase == "commit-cleaning":
            raise ActivationError("commit is already decided; rollback is forbidden")
        if phase.startswith("rollback-cleaning-"):
            current_receipt = _read_bytes(
                receipt_path, _MAX_RECEIPT_BYTES, missing_ok=True
            )
            if (
                _digest(current_receipt) != state["prior_receipt_sha256"]
                or _git_head(root) != state["pre_update_head"]
                or _git_branch(root) != state["pre_update_branch"]
                or not _git_clean(root)
                or not _venv_python(root / "venv").is_file()
            ):
                raise ActivationError("rollback cleanup lost its recovery proof")
        else:
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
            _restore_desktop_backup(
                root, hashlib.sha256(manifest_raw).hexdigest()[:20]
            )
            _restore_source(
                root,
                pre_update_head=str(state["pre_update_head"]),
                pre_update_branch=str(state["pre_update_branch"]),
                branch=str(manifest["branch"]),
                selected_pre_head=state["selected_pre_head"],
                target_head=str(state["target_head"]),
            )
            state["phase"] = (
                "rollback-cleaning-published"
                if state.get("published_receipt_sha256") is not None
                else "rollback-cleaning-unpublished"
            )
            state["move_error"] = None
            _write_state(state_path, state)
            state, _, state_raw = _load_state(root, home, invocation, lease)
    else:
        desktop_health_digest = None
        _restore_source(
            root,
            pre_update_head=str(manifest["pre_update_head"]),
            pre_update_branch=str(manifest["pre_update_branch"]),
            branch=str(manifest["branch"]),
            selected_pre_head=manifest["selected_pre_head"],
            target_head=str(manifest["target_head"]),
        )
        _remove_tree_exact(
            root / str(manifest["candidate_rel"]),
            parent=root / ".hermes-runtime",
            prefix="venv-candidate-",
        )
        current_receipt = _read_bytes(
            receipt_path, _MAX_RECEIPT_BYTES, missing_ok=True
        )
        if _digest(current_receipt) != manifest["prior_receipt_sha256"]:
            raise ActivationError("the prior receipt changed before rollback cleanup")
        state = _state_payload(
            manifest,
            manifest_raw,
            phase="rollback-cleaning-unpublished",
            prior_receipt=current_receipt,
        )
        _write_state(state_path, state)
        state, _, state_raw = _load_state(root, home, invocation, lease)

    _remove_generation_claim(
        root,
        state["provisioned_generation"],
        invocation,
        lease,
    )
    _remove_desktop_health(
        root,
        home,
        manifest,
        expected_digest=desktop_health_digest,
    )
    _remove_correlated_staging_journal(
        root, home, invocation, lease, manifest
    )
    _unlink_exact(manifest_path, manifest_raw)
    _unlink_exact(state_path, state_raw)


def _remove_staging_artifact(
    root: Path,
    claim: object,
    kind: str,
    invocation: str,
    lease: str,
) -> None:
    path = _validated_staging_artifact(root, claim, kind, invocation, lease)
    if path is None or not os.path.lexists(path):
        return
    _directory_identity(path)
    shutil.rmtree(path)


def recover_staging_journal(
    root: Path,
    home: Path,
    invocation: str,
    lease: str,
) -> None:
    journal, raw = _validated_staging_journal(
        root, home, invocation, lease
    )
    _restore_source(
        root,
        pre_update_head=str(journal["pre_update_head"]),
        pre_update_branch=str(journal["pre_update_branch"]),
        branch=str(journal["branch"]),
        selected_pre_head=journal["selected_pre_head"],
        target_head=str(journal["target_head"]),
        source_phase=str(journal["phase"]),
    )
    _remove_staging_artifact(
        root, journal["candidate"], "candidate", invocation, lease
    )
    _remove_staging_artifact(
        root,
        journal["provisioned_generation"],
        "generation",
        invocation,
        lease,
    )
    _unlink_exact(_staging_path(home), raw)


def recover_stale_transaction_journals(
    root: Path,
    *,
    home: Path,
    current_lease: dict,
    now: float | None = None,
    pid_alive: Callable[[int], bool] | None = None,
    pid_create_time: Callable[[int], float | None] | None = None,
) -> None:
    """Recover one crashed transaction under a distinct live updater lease.

    A timestamp never proves abandonment. The current process must own the
    exact live marker, and the old journal owner must be definitively absent
    or a different process generation before any source or artifact mutation.
    """
    import hermes_mcp_update_gate as gate

    root = root.resolve()
    home = home.resolve()
    _refuse_existing_commit_coordinator(home)
    acquisition_path = _acquisition_path(home)
    staging_path = _staging_path(home)
    acquisition_exists = os.path.lexists(acquisition_path)
    staging_exists = os.path.lexists(staging_path)
    if not acquisition_exists and not staging_exists:
        return
    if acquisition_exists and staging_exists:
        raise ActivationError("multiple stale update journals require manual review")

    current = _validated_lease_authority(root, current_lease)
    if current.get("owner_pid") != os.getpid():
        raise ActivationError("current recovery lease is not owned by this updater")
    live_kwargs: dict[str, object] = {
        "install_root": root,
        "now": time.time() if now is None else now,
    }
    if pid_alive is not None:
        live_kwargs["pid_alive"] = pid_alive
    if pid_create_time is not None:
        live_kwargs["pid_create_time"] = pid_create_time
    try:
        live = gate.live_quiesce_lease(gate.marker_path(), **live_kwargs)
    except Exception as exc:
        from hermes_cli.update_diagnostics import diagnostic_error

        raise diagnostic_error(
            exc,
            code="HDA101",
            stage="current-lease",
        ) from None
    if live != current:
        raise ActivationError("current recovery lease is not exact and live")

    if acquisition_exists:
        value, _ = _read_json(acquisition_path)
        invocation = _validate_identity(value.get("invocation_id"), "invocation id")
        lease_id = _validate_identity(value.get("lease_id"), "lease id")
        journal, _, _ = _validated_acquisition_journal(
            root, home, invocation, lease_id
        )
    else:
        value, _ = _read_json(staging_path)
        invocation = _validate_identity(value.get("invocation_id"), "invocation id")
        lease_id = _validate_identity(value.get("lease_id"), "lease id")
        journal, _ = _validated_staging_journal(root, home, invocation, lease_id)

    old = _validated_lease_authority(
        root,
        journal.get("lease_authority"),
        expected_lease_id=lease_id,
    )
    if old == current:
        raise ActivationError("stale journal still belongs to the current lease")
    owner_alive = pid_alive if pid_alive is not None else gate._pid_alive
    owner_create_time = (
        pid_create_time if pid_create_time is not None else gate._pid_create_time
    )
    try:
        old_owner_is_live = gate._lease_owner_is_live(
            int(old["owner_pid"]),
            float(old["created_at"]),
            pid_alive=owner_alive,
            pid_create_time=owner_create_time,
        )
    except Exception as exc:
        from hermes_cli.update_diagnostics import diagnostic_error

        raise diagnostic_error(
            exc,
            code="HDA102",
            stage="stale-owner",
        ) from None
    if old_owner_is_live:
        raise ActivationError("stale journal owner is still live")

    if acquisition_exists:
        recover_acquisition_journal(root, home, invocation, lease_id)
    else:
        recover_staging_journal(root, home, invocation, lease_id)


def rollback_source_only() -> None:
    root, home, invocation, lease = _expected_context()
    if os.path.lexists(_commit_coordinator_path(home)):
        raise ActivationError("commit is already decided; rollback is forbidden")
    acquisition_path = _acquisition_path(home)
    acquisition_recovered = False
    if acquisition_path.exists():
        recover_acquisition_journal(root, home, invocation, lease)
        acquisition_recovered = True
    journal_path = _staging_path(home)
    if journal_path.exists():
        recover_staging_journal(root, home, invocation, lease)
        return
    if acquisition_recovered:
        return
    # Backward-compatible no-journal recovery may prove an already-restored
    # clean source, but cannot mutate a branch without the missing ref claims.
    pre_update_head = _validate_sha(
        os.environ.get("HERMES_INTERNAL_DESKTOP_UPDATE_PRE_HEAD"),
        "pre-update head",
    )
    if _git_head(root) != pre_update_head or not _git_clean(root):
        raise ActivationError("source rollback claims are unavailable")


def commit() -> None:
    root, home, invocation, lease = _expected_context()
    manifest_path, state_path, receipt_path = _manifest_paths(home)
    if not manifest_path.exists():
        state, state_raw = _load_terminal_cleanup_state(
            root,
            home,
            invocation,
            lease,
            action="commit",
        )
        receipt_raw = _read_bytes(receipt_path, _MAX_RECEIPT_BYTES)
        assert receipt_raw is not None
        _unlink_exact(state_path, state_raw)
        return

    manifest, manifest_raw = _validated_manifest(root, home, invocation, lease)
    state, _, state_raw = _load_state(root, home, invocation, lease)
    phase = state.get("phase")
    if phase not in {"receipt-published", "commit-cleaning"}:
        raise ActivationError("activation is not ready to commit")
    current_receipt = _read_bytes(receipt_path, _MAX_RECEIPT_BYTES)
    assert current_receipt is not None
    if _digest(current_receipt) != state.get("published_receipt_sha256"):
        raise ActivationError("the committed receipt changed before cleanup")
    if (
        _git_head(root) != manifest["target_head"]
        or _git_branch(root) != manifest["branch"]
        or not _git_clean(root)
        or not _smoke_live(root / "venv", root)
    ):
        raise ActivationError("the installed generation lost its final health proof")

    if phase == "receipt-published":
        health_path = _desktop_health_path(home)
        if health_path.exists():
            _, health_raw = _validated_desktop_health(
                root,
                home,
                manifest,
                require_current_node=False,
            )
            if not hmac.compare_digest(
                hashlib.sha256(health_raw).hexdigest(),
                state["desktop_health_sha256"],
            ):
                raise ActivationError("Desktop health proof changed before commit")
        staging_path = _staging_path(home)
        if staging_path.exists():
            journal, _ = _validated_staging_journal(
                root, home, invocation, lease
            )
            if (
                journal["pre_update_head"] != manifest["pre_update_head"]
                or journal["pre_update_branch"] != manifest["pre_update_branch"]
                or journal["branch"] != manifest["branch"]
                or journal["selected_pre_head"] != manifest["selected_pre_head"]
                or journal["target_head"] != manifest["target_head"]
            ):
                raise ActivationError("coexisting staging journal identity changed")
        state["phase"] = "commit-cleaning"
        # This durable phase is the irreversible boundary. Retries finish the
        # same cleanup; rollback rejects commit-cleaning state.
        _atomic_write(state_path, _json_bytes(state))
        state, _, state_raw = _load_state(root, home, invocation, lease)

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
    _remove_desktop_health(
        root,
        home,
        manifest,
        expected_digest=state["desktop_health_sha256"],
    )
    _remove_correlated_staging_journal(
        root, home, invocation, lease, manifest
    )
    _unlink_exact(manifest_path, manifest_raw)
    _unlink_exact(state_path, state_raw)


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
