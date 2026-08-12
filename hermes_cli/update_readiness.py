"""Read-only update target resolution and readiness reporting."""

import functools
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from hermes_cli.update_receipt import (
    _IDENTIFIER_RE,
    _load_update_receipt,
    _sanitize_update_receipt,
)


_READINESS_SCHEMA_VERSION = 1
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_EXECUTABLE_GIT_CONFIG_RE = (
    r"^(filter\..*\.(clean|smudge|process)|merge\..*\.driver|"
    r"core\.(sshcommand|gitproxy)|credential(\..*)?\.helper|"
    r"url\..*\.(insteadof|pushinsteadof))$"
)
_READINESS_KEYS = {
    "schema_version",
    "mode",
    "ok",
    "ready",
    "blocked",
    "reason",
    "root",
    "venv",
    "processes",
    "mcp_bridges",
    "pausable_gateways",
    "pausable_gateway_processes",
    "git",
    "last_update_receipt",
    "lease",
    "actions",
    "error",
}


@dataclass(frozen=True)
class _UpdateTarget:
    branch: str
    remote: str
    tracking_ref: str
    refspec: str


def _is_safe_remote_name(remote: str) -> bool:
    return bool(
        _REMOTE_NAME_RE.fullmatch(remote)
        and ".." not in remote
        and "//" not in remote
        and "@{" not in remote
        and not remote.endswith(("/", ".", ".lock"))
    )


def _git_cmd() -> list[str]:
    # Every update Git subprocess shares this prefix.  Do not let repository
    # configuration turn read/update operations into arbitrary process
    # launches: hooks and fsmonitor are disabled and global/system attribute
    # files are ignored.  Local filter/merge drivers need a separate explicit
    # refusal because Git has no wildcard `-c filter.*=off` override.
    command = [
        "git",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.attributesFile={os.devnull}",
        "-c",
        "protocol.ext.allow=never",
    ]
    if sys.platform == "win32":
        command.extend(["-c", "windows.appendAtomically=false"])
    return command


def _sanitized_git_env(*, read_only: bool = False) -> dict[str, str]:
    """Return the ambient environment without inherited Git control knobs."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    # A privileged updater must not execute helpers or filters injected by a
    # user's system/global Git configuration. Repository-local remote
    # configuration remains available; executable local selectors are rejected
    # by `_assert_safe_git_configuration` before status/mutation.
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
        }
    )
    if read_only:
        env.update(
            {
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
    return env


class _GitRoutingEnvironmentGuard:
    """Temporarily remove ambient variables that can redirect Git mutation."""

    def __init__(self) -> None:
        self._saved: dict[str, str] = {}

    @staticmethod
    def _is_routing_key(key: str) -> bool:
        return key.upper().startswith("GIT_")

    def __enter__(self) -> "_GitRoutingEnvironmentGuard":
        self._saved = {
            key: value
            for key, value in os.environ.items()
            if self._is_routing_key(key)
        }
        for key in self._saved:
            os.environ.pop(key, None)
        os.environ.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_ATTR_NOSYSTEM": "1",
            }
        )
        return self

    def __exit__(self, *_exc) -> None:
        # Remove any routing key introduced by update code, then restore the
        # caller's exact environment. All direct and helper subprocess calls
        # inside the decorated mutation pipeline inherit the safe view.
        for key in list(os.environ):
            if self._is_routing_key(key):
                os.environ.pop(key, None)
        os.environ.update(self._saved)


def _with_sanitized_git_routing(function):
    @functools.wraps(function)
    def guarded(*args, **kwargs):
        with _GitRoutingEnvironmentGuard():
            return function(*args, **kwargs)

    return guarded


def _assert_safe_git_configuration(
    git_cmd: list[str], cwd: Path, *, env: dict[str, str] | None = None
) -> None:
    """Refuse repository-local configuration that can execute a command.

    Git's `status`, `stash`, `checkout`, `reset`, `restore`, and `merge` can
    invoke filter/merge drivers without an obvious subprocess at this layer.
    Fetch also honors repository-local ``url.*.insteadOf`` selectors, which
    can redirect a literal official URL to an arbitrary transport/helper.
    System/global config is excluded by the sanitized environment; inspect
    the remaining local scope (including files it includes) before the first
    worktree read or mutation.  Failure to inspect is itself a refusal.
    """
    command_env = _sanitized_git_env() if env is None else env
    result = subprocess.run(
        git_cmd
        + [
            "config",
            "--includes",
            "--show-origin",
            "--show-scope",
            "--name-only",
            "--get-regexp",
            _EXECUTABLE_GIT_CONFIG_RE,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=command_env,
    )
    if result.returncode == 1 and not result.stdout.strip():
        return
    if result.returncode != 0:
        raise RuntimeError("could not prove repository Git configuration is safe")
    selectors = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if selectors:
        raise RuntimeError(
            "repository Git configuration contains executable filter/merge "
            "drivers or URL rewrite selectors: "
            + ", ".join(selectors)
        )


def _resolve_update_target(
    git_cmd: list[str], cwd: Path, branch: str, *, env: dict[str, str] | None = None
) -> _UpdateTarget:
    """Resolve the upstream updater's fixed origin branch contract."""
    command_env = _sanitized_git_env() if env is None else env
    check = subprocess.run(
        git_cmd + ["check-ref-format", "--branch", branch],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=command_env,
    )
    if check.returncode != 0:
        raise ValueError(f"invalid update branch: {branch}")
    remote = "origin"
    tracking_ref = f"refs/remotes/{remote}/{branch}"
    remote_probe = subprocess.run(
        git_cmd + ["remote", "get-url", "--", remote],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=command_env,
    )
    ref_probe = subprocess.run(
        git_cmd + ["check-ref-format", tracking_ref],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=command_env,
    )
    if remote_probe.returncode != 0 or ref_probe.returncode != 0:
        raise ValueError(f"invalid or missing update remote: {remote}")
    return _UpdateTarget(
        branch=branch,
        remote=remote,
        tracking_ref=tracking_ref,
        refspec=f"+refs/heads/{branch}:{tracking_ref}",
    )


def _git_preflight_metadata(root: Path, branch: str) -> dict | None:
    if not (root / ".git").exists():
        return None
    git_cmd = _git_cmd()
    read_only_env = _sanitized_git_env(read_only=True)
    target = _resolve_update_target(git_cmd, root, branch, env=read_only_env)
    _assert_safe_git_configuration(git_cmd, root, env=read_only_env)

    def _read(*args: str, required: bool = True) -> str | None:
        result = subprocess.run(
            git_cmd + list(args),
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=read_only_env,
        )
        if result.returncode != 0:
            if required:
                raise RuntimeError(f"git {' '.join(args)} failed")
            return None
        return result.stdout.strip()

    head = _read("rev-parse", "HEAD")
    current_branch = _read("symbolic-ref", "--quiet", "--short", "HEAD", required=False)
    dirty = bool(_read("status", "--porcelain"))
    target_sha = _read("rev-parse", "--verify", "--quiet", target.tracking_ref, required=False)
    return {
        "head": head,
        "branch": current_branch or "HEAD",
        "dirty": dirty,
        "tracking_remote": target.remote,
        "target_branch": target.branch,
        "target_ref": target.tracking_ref,
        "target_sha": target_sha,
    }


def _public_quiesce_lease(lease: dict | None) -> dict | None:
    """Return readiness-safe lease metadata without its adoption capability."""
    if lease is None:
        return None
    if "lease_fingerprint" in lease and "lease_id" not in lease:
        return dict(lease)
    lease_id = lease.get("lease_id")
    if not isinstance(lease_id, str) or _IDENTIFIER_RE.fullmatch(lease_id) is None:
        return None
    return {
        "schema_version": lease.get("schema_version"),
        "lease_fingerprint": hashlib.sha256(lease_id.encode("utf-8")).hexdigest(),
        "owner_pid": lease.get("owner_pid"),
        "created_at": lease.get("created_at"),
        "expires_at": lease.get("expires_at"),
        "handoff_grace_until": lease.get("handoff_grace_until"),
        "install_root": lease.get("install_root"),
    }


def _readiness_payload(
    *,
    mode: str,
    root: Path,
    scan: dict[str, object] | None = None,
    git: dict | None = None,
    receipt: dict | None = None,
    lease: dict | None = None,
    actions: list[dict] | None = None,
    ok: bool = False,
    ready: bool = False,
    reason: str | None = None,
    error: dict[str, str] | None = None,
) -> dict[str, object]:
    venv = root / "venv"
    if not venv.exists() and (root / ".venv").exists():
        venv = root / ".venv"
    scan = scan or {}
    return {
        "schema_version": _READINESS_SCHEMA_VERSION,
        "mode": mode,
        "ok": bool(ok),
        "ready": bool(ready),
        "blocked": not bool(ready),
        "reason": reason,
        "root": os.path.normcase(os.path.realpath(root)),
        "venv": os.path.normcase(os.path.realpath(venv)),
        "processes": list(scan.get("processes", [])),
        "mcp_bridges": list(scan.get("mcp_bridges", [])),
        "pausable_gateways": int(scan.get("pausable_gateways", 0)),
        "pausable_gateway_processes": list(
            scan.get("pausable_gateway_processes", [])
        ),
        "git": git,
        "last_update_receipt": receipt,
        "lease": _public_quiesce_lease(lease),
        "actions": list(actions or []),
        "error": error,
    }


def validate_update_readiness(payload: object) -> dict[str, object]:
    """Validate the cross-language v1 readiness contract and invariants.

    The versioned JSON Schema is the structural public contract.  These
    relational checks are deliberately in production code as well: JSON
    Schema cannot prove that a terminate action identifies an actionable
    bridge in the same document or that a lease belongs to this root.
    """

    def reject(message: str) -> NoReturn:
        raise ValueError(f"invalid update readiness document: {message}")

    if not isinstance(payload, dict) or set(payload) != _READINESS_KEYS:
        reject("top-level keys do not match schema v1")
    if payload["schema_version"] != 1 or payload["mode"] not in {
        "preflight",
        "drain",
    }:
        reject("unsupported schema version or mode")
    for key in ("ok", "ready", "blocked"):
        if type(payload[key]) is not bool:
            reject(f"{key} must be boolean")
    if payload["blocked"] is not (not payload["ready"]):
        reject("blocked must be the inverse of ready")
    if payload["ready"] and (
        payload["reason"] is not None or payload["error"] is not None
    ):
        reject("ready output cannot have a reason or error")
    if not payload["ready"] and not isinstance(payload["reason"], str):
        reject("not-ready output requires a stable reason")
    if payload["ok"] != (payload["error"] is None):
        reject("ok and error are inconsistent")
    if not isinstance(payload["root"], str) or not isinstance(payload["venv"], str):
        reject("root and venv must be strings")
    for key in (
        "processes",
        "mcp_bridges",
        "pausable_gateway_processes",
        "actions",
    ):
        if not isinstance(payload[key], list):
            reject(f"{key} must be an array")
    if type(payload["pausable_gateways"]) is not int or payload["pausable_gateways"] < 0:
        reject("pausable_gateways must be a non-negative integer")
    if payload["pausable_gateways"] != len(payload["pausable_gateway_processes"]):
        reject("pausable gateway count does not match its process array")

    allowed_owners = {"codex", "claude", "desktop", "gateway", "unknown"}
    allowed_mcp_roles = {"mcp_bridge_wrapper", "mcp_bridge_worker"}
    bridge_by_identity: dict[tuple[int, float], dict] = {}
    for record in payload["mcp_bridges"]:
        if not isinstance(record, dict):
            reject("mcp bridge entries must be objects")
        try:
            pid = record["pid"]
            created_at = float(record["created_at"])
            owner = record["owner"]
            role = record["role"]
            actionable = record["actionable"]
        except (KeyError, TypeError, ValueError):
            reject("mcp bridge identity is incomplete")
        if (
            type(pid) is not int
            or pid <= 0
            or not math.isfinite(created_at)
            or created_at <= 0
            or owner not in allowed_owners
            or role not in allowed_mcp_roles
            or type(actionable) is not bool
        ):
            reject("mcp bridge identity is invalid")
        if actionable:
            if owner not in {"codex", "claude"} or (
                record.get("actionability") != "exact_mcp_bridge"
                or record.get("action") != "terminate_exact_mcp"
            ):
                reject("actionable bridge lacks exact owner/action contract")
        elif record.get("actionability") != "hard_block" or record.get("action") != "refuse":
            reject("unactionable bridge must be a hard refusal")
        bridge_by_identity[(pid, created_at)] = record

    for record in payload["processes"]:
        if not isinstance(record, dict) or record.get("actionable") is not False:
            reject("ordinary processes are never directly actionable")
        if record.get("owner") not in allowed_owners:
            reject("process owner is invalid")
        if record.get("role") not in {
            "other",
            "desktop_backend",
            "update_lock_holder",
        }:
            reject("process role is invalid")
        if record.get("actionability") != "hard_block" or record.get("action") != "refuse":
            reject("ordinary process must be a hard refusal")

    for record in payload["pausable_gateway_processes"]:
        if not isinstance(record, dict) or not (
            record.get("owner") == "gateway"
            and record.get("role") == "gateway_run"
            and record.get("actionable") is False
            and record.get("actionability") == "downstream_drainable"
            and record.get("action") == "pause_downstream"
        ):
            reject("gateway record is incoherent")

    lease = payload["lease"]
    if lease is not None:
        expected_lease_keys = {
            "schema_version",
            "lease_fingerprint",
            "owner_pid",
            "created_at",
            "expires_at",
            "handoff_grace_until",
            "install_root",
        }
        if not isinstance(lease, dict) or set(lease) != expected_lease_keys:
            reject("lease keys do not match schema v1")
        times = [lease.get(key) for key in ("created_at", "expires_at", "handoff_grace_until")]
        if (
            lease.get("schema_version") != 1
            or not isinstance(lease.get("lease_fingerprint"), str)
            or re.fullmatch(r"[0-9a-f]{64}", lease["lease_fingerprint"]) is None
            or type(lease.get("owner_pid")) is not int
            or lease["owner_pid"] <= 0
            or any(type(value) is not int or value <= 0 for value in times)
            or lease["created_at"] > lease["handoff_grace_until"]
            or lease["handoff_grace_until"] > lease["expires_at"]
            or os.path.normcase(os.path.realpath(str(lease.get("install_root", ""))))
            != os.path.normcase(os.path.realpath(payload["root"]))
        ):
            reject("lease identity, ordering, or root is invalid")

    receipt = payload["last_update_receipt"]
    if receipt is not None:
        sanitized_receipt = _sanitize_update_receipt(
            receipt, Path(str(payload["root"]))
        )
        if sanitized_receipt is None or sanitized_receipt != receipt:
            reject("last update receipt is invalid or lacks health proof")

    for action in payload["actions"]:
        if not isinstance(action, dict):
            reject("actions must be objects")
        if action.get("type") == "clear-scan":
            if payload["mode"] != "drain":
                reject("preflight cannot contain drain clear-scan actions")
            if type(action.get("sequence")) is not int or action["sequence"] not in {1, 2}:
                reject("clear-scan sequence is invalid")
            continue
        if action.get("type") != "terminate-mcp-bridge":
            reject("unknown action type")
        try:
            identity = (action["pid"], float(action["created_at"]))
        except (KeyError, TypeError, ValueError):
            reject("terminate action identity is invalid")
        if action.get("owner") not in {"codex", "claude"} or action.get(
            "role"
        ) not in allowed_mcp_roles:
            reject("terminate action owner or role is invalid")
        if payload["mode"] == "preflight" and "terminated" in action:
            reject("preflight terminate action cannot claim an outcome")
        if payload["mode"] == "drain" and type(action.get("terminated")) is not bool:
            reject("drain terminate action requires its outcome")
        bridge = bridge_by_identity.get(identity)
        if bridge is not None and (
            bridge.get("actionable") is not True
            or bridge.get("owner") != action.get("owner")
            or bridge.get("role") != action.get("role")
        ):
            reject("terminate action does not match an actionable bridge")
        if bridge is None and type(action.get("terminated")) is not bool:
            reject("historical terminate action requires its outcome")

    if payload["ready"] and (
        payload["processes"]
        or payload["mcp_bridges"]
        or (payload["mode"] == "preflight" and payload["lease"] is not None)
    ):
        reject("ready output still contains blockers")
    if payload["mode"] == "drain" and payload["ready"]:
        if payload["lease"] is None:
            reject("successful drain requires a live handoff lease")
        clear_proof = [
            action.get("sequence")
            for action in payload["actions"]
            if action.get("type") == "clear-scan"
        ]
        if clear_proof != [1, 2]:
            reject("successful drain requires two final clear scans")
        if any(
            action.get("type") != "clear-scan" for action in payload["actions"][-2:]
        ):
            reject("successful drain clear proof must be the final two actions")
    return payload


def _read_update_holder_read_only() -> object | None:
    """Read the shared update marker without stale-marker cleanup mutation."""
    from hermes_cli.update_lock import (
        UpdateHolder,
        _pid_matches_update_owner,
        update_marker_path,
    )

    path = update_marker_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"update marker is unreadable: {exc}") from exc
    try:
        lines = raw.splitlines()
        pid = int(lines[0].strip())
        started_at = float(lines[1].strip())
    except (IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("update marker is malformed") from exc
    age = _time.time() - started_at
    if not math.isfinite(age) or age < -5:
        return None
    if not _pid_matches_update_owner(pid, started_at):
        return None
    return UpdateHolder(pid=pid, age_seconds=age, started_at=started_at, raw=raw)


def _build_update_preflight(
    root: Path,
    branch: str,
    *,
    expected_lease_id: str | None = None,
) -> dict[str, object]:
    """Build the exact 17-key readiness document without network or mutation."""
    from hermes_cli._scan_venv_blockers import scan_venv_blockers
    from hermes_mcp_update_gate import live_quiesce_lease, marker_path

    canonical_root = Path(os.path.realpath(root))
    receipt = _load_update_receipt(canonical_root)
    try:
        scan = scan_venv_blockers(canonical_root)
        git = _git_preflight_metadata(canonical_root, branch)
        lease = live_quiesce_lease(marker_path(), install_root=canonical_root)
        if lease is not None and lease.get("schema_version") != 1:
            lease = None
        if (
            receipt is not None
            and lease is not None
            and receipt.get("lease_id") == lease.get("lease_id")
        ):
            # A deferred receipt is historical only after the live capability
            # is cleared.  While the same lease is active, returning its raw
            # ID through public readiness JSON would disclose adoption power.
            receipt = None
        update_holder = _read_update_holder_read_only()
    except Exception as exc:
        return _readiness_payload(
            mode="preflight",
            root=canonical_root,
            receipt=receipt,
            reason="probe-failed",
            error={"code": "probe-failed", "message": str(exc)},
        )

    processes = list(scan.get("processes", []))
    foreign_update = False
    if update_holder is not None:
        from hermes_cli.update_lock import _handoff_pid, _is_ancestor_pid

        holder_pid = int(update_holder.pid)
        if holder_pid not in {os.getpid(), _handoff_pid()} and not _is_ancestor_pid(
            holder_pid
        ):
            foreign_update = True
            processes.append(
                {
                    "pid": holder_pid,
                    "name": "hermes-update",
                    "cmdline": "<redacted>",
                    "owner": "unknown",
                    "role": "update_lock_holder",
                    "actionable": False,
                    "actionability": "hard_block",
                    "action": "refuse",
                }
            )
            scan = {**scan, "processes": processes}
    lease_authorized = False
    lease_reason: str | None = None
    if expected_lease_id is not None:
        from hermes_cli.update_lock import _handoff_pid, _is_ancestor_pid

        if _IDENTIFIER_RE.fullmatch(expected_lease_id) is None:
            lease_reason = "lease-capability-invalid"
        elif lease is None:
            lease_reason = "lease-capability-missing"
        elif lease.get("lease_id") != expected_lease_id:
            lease_reason = "lease-capability-mismatch"
        else:
            owner_pid = int(lease.get("owner_pid", 0))
            lease_authorized = owner_pid == os.getpid() or (
                owner_pid > 0 and _is_ancestor_pid(owner_pid)
            )
            if not lease_authorized:
                lease_reason = "lease-capability-owner-mismatch"
    elif lease is not None:
        lease_reason = "quiesce-lease-active"

    bridges = list(scan.get("mcp_bridges", []))
    ready = not processes and not bridges and lease_reason is None
    if foreign_update:
        reason = "update-running"
    elif processes:
        reason = "venv-blocked"
    elif any(not bool(entry.get("actionable")) for entry in bridges):
        reason = "mcp-owner-unverified"
    elif bridges:
        reason = "mcp-bridges-running"
    elif lease_reason is not None:
        reason = lease_reason
    else:
        reason = None
    actions = [
        {
            "type": "terminate-mcp-bridge",
            "pid": int(entry["pid"]),
            "created_at": float(entry["created_at"]),
            "owner": str(entry["owner"]),
            "role": str(entry["role"]),
        }
        for entry in bridges
        if bool(entry.get("actionable"))
    ]
    return _readiness_payload(
        mode="preflight",
        root=canonical_root,
        scan=scan,
        git=git,
        receipt=receipt,
        # A capability-authorized preflight is ready under the caller's
        # already-owned lease, but the public document never returns that
        # reusable capability (or even a non-null active lease on ready
        # preflight). Unauthorized observations expose only a hash fingerprint.
        lease=None if lease_authorized else lease,
        actions=actions,
        ok=True,
        ready=ready,
        reason=reason,
    )


def _print_update_readiness(payload: dict[str, object], *, json_mode: bool) -> None:
    validate_update_readiness(payload)
    if json_mode:
        print(json.dumps(payload, separators=(",", ":")))
        return
    if not payload["ok"]:
        print(f"✗ Update safety probe failed: {payload['reason']}")
    elif payload["ready"]:
        print("✓ This Hermes install is ready to update.")
    else:
        print(f"✗ This Hermes install is not ready to update: {payload['reason']}")
        for process in list(payload.get("processes", [])) + list(
            payload.get("mcp_bridges", [])
        ):
            print(
                f"  PID {process.get('pid')}  {process.get('role')}  "
                f"{process.get('cmdline')}"
            )


def _readiness_exit_code(payload: dict[str, object]) -> int:
    if not bool(payload.get("ok")):
        return 1
    return 0 if bool(payload.get("ready")) else 2


def _cmd_update_preflight(args, *, root: Path) -> NoReturn:
    branch = (getattr(args, "branch", None) or "main").strip() or "main"
    payload = _build_update_preflight(
        root,
        branch,
        expected_lease_id=getattr(args, "bridge_lease_id", None),
    )
    _print_update_readiness(payload, json_mode=bool(getattr(args, "json", False)))
    raise SystemExit(_readiness_exit_code(payload))
