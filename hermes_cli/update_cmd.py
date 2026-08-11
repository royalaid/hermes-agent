"""Hermes update pipeline — extracted from ``hermes_cli/main.py``.

Mechanical move (main.py decomposition): ``_cmd_update_impl``, ``_cmd_update_check``
and every module-level helper used only by the update path, plus the update-only
constants they read. Function bodies are lifted verbatim; the only mechanical
change is that references to helpers/constants that STAY in ``hermes_cli.main``
(and to moved-but-test-patched siblings) are routed through ``_m()`` — a lazy
``hermes_cli.main`` reference — so existing call sites and test monkeypatches
that target ``hermes_cli.main.<name>`` (``PROJECT_ROOT``, ``_is_windows``,
``_run_pre_update_backup``, ...) keep working unchanged. ``main.py`` re-imports
every public-ish name from here (``# noqa: F401``) so the argparse wiring and
the test-patch surface still resolve on ``hermes_cli.main``.

Three self-contained closures nested inside ``_cmd_update_impl``
(``_print_items``, ``_wait_for_service_active``, ``_service_restart_sec``) were
hoisted to module level; they capture no enclosing state (verified via
``symtable``). ``_restart_one_systemd_gateway_unit``, ``_resolve_manage_cmd``
and ``_on_unit_timeout`` DO capture enclosing locals and stay nested,
byte-identical.

Imports are one-way: ``hermes_cli.main`` imports this module, never the reverse
at import time (``_m()`` resolves lazily at call time, when main.py is fully
loaded, so there is no import cycle).
"""

import hashlib
import hmac
import functools
import json
import logging
import math
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, NoReturn, Optional

from hermes_cli.config import get_hermes_home
from hermes_constants import venv_bin_dir, venv_python_path

logger = logging.getLogger(__name__)

_UPDATE_RECEIPT_NAME = ".hermes-update-receipt.json"
_DEFERRED_GATEWAY_PLAN_PREFIX = ".hermes-gateway-resume-"
# Native wrappers bound source/dependency mutation at 60 minutes and the
# outside-Job fleet-resume phase at 5 minutes. Keep one additional minute for
# descendant-drain/clock scheduling while still bounding abandoned recovery.
_DEFERRED_GATEWAY_PLAN_TTL_SECONDS = 66 * 60
_READINESS_SCHEMA_VERSION = 1
_DRAIN_CLEAR_INTERVAL_SECONDS = 0.5
_DRAIN_COOPERATIVE_WAIT_SECONDS = 1.0
_DEFAULT_DRAIN_TIMEOUT_SECONDS = 12.0
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]{16,128}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
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


@dataclass(frozen=True)
class _VerifiedProcessIdentity:
    """One live process identity authorized for a narrowly scoped stop."""

    pid: int
    created_at: float
    kind: str
    argv: tuple[str, ...] = ()
    executable: str = ""
    install_root: str = ""
    role: str = ""
    working_directory: str = ""


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
    # user's system/global Git configuration.  Repository-local remote and
    # branch configuration remains available; executable local selectors are
    # rejected by `_assert_safe_git_configuration` before status/mutation.
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


def _receipt_path(root: Path) -> Path:
    del root
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / _UPDATE_RECEIPT_NAME


def _deferred_gateway_plan_path(
    root: Path, invocation_id: str, *, completed: bool = False
) -> Path:
    """Return the install-global private fleet-plan path."""
    del root
    from hermes_constants import get_default_hermes_root

    suffix = ".completed" if completed else ".json"
    return get_default_hermes_root() / (
        f"{_DEFERRED_GATEWAY_PLAN_PREFIX}{invocation_id}{suffix}"
    )


def _write_private_exclusive(path: Path, raw: str) -> None:
    """Publish fully-written private bytes without overwriting a claim."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".hermes-gateway-plan-{secrets.token_hex(16)}"
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        encoded = raw.encode("utf-8")
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("short write while publishing gateway resume plan")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except OSError:
            pass


def _gateway_plan_auth(payload: dict, lease_id: str) -> str:
    authenticated = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hmac.new(lease_id.encode("utf-8"), authenticated, hashlib.sha256).hexdigest()


def _sanitize_deferred_gateway_plan(
    value: object,
    *,
    root: Path,
    invocation_id: str,
    lease_id: str,
    now: float | None = None,
) -> dict | None:
    """Validate a private no-argv gateway fleet plan and its capability MAC."""
    if not isinstance(value, dict):
        return None
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
    if (
        set(value) != expected
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
    ):
        return None
    if value.get("invocation_id") != invocation_id:
        return None
    if not _IDENTIFIER_RE.fullmatch(invocation_id):
        return None
    if not _IDENTIFIER_RE.fullmatch(lease_id):
        return None
    expected_fingerprint = hashlib.sha256(lease_id.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(
        str(value.get("lease_fingerprint", "")), expected_fingerprint
    ):
        return None
    if os.path.normcase(os.path.realpath(str(value.get("install_root", "")))) != os.path.normcase(
        os.path.realpath(root)
    ):
        return None
    if type(value.get("created_at")) is not int or type(value.get("expires_at")) is not int:
        return None
    created_at = value["created_at"]
    expires_at = value["expires_at"]
    current = _time.time() if now is None else float(now)
    if not math.isfinite(current) or not (
        created_at > 0
        and created_at <= expires_at
        and expires_at - created_at <= _DEFERRED_GATEWAY_PLAN_TTL_SECONDS
        and created_at <= current + 5
        and current <= expires_at
    ):
        return None
    if type(value.get("cold_start_if_installed")) is not bool:
        return None
    raw_profiles = value.get("profiles")
    if not isinstance(raw_profiles, list):
        return None
    profiles: list[dict] = []
    seen: set[str] = set()
    for entry in raw_profiles:
        if not isinstance(entry, dict) or set(entry) != {
            "name",
            "old_pid",
            "created_at",
        }:
            return None
        name = entry.get("name")
        if (
            not isinstance(name, str)
            or _PROFILE_NAME_RE.fullmatch(name) is None
            or name in {".", ".."}
            or name in seen
        ):
            return None
        if type(entry.get("old_pid")) is not int or isinstance(
            entry.get("created_at"), bool
        ) or not isinstance(entry.get("created_at"), (int, float)):
            return None
        old_pid = entry["old_pid"]
        process_created_at = float(entry["created_at"])
        if (
            isinstance(entry.get("old_pid"), bool)
            or old_pid <= 0
            or not math.isfinite(process_created_at)
            or process_created_at <= 0
        ):
            return None
        seen.add(name)
        profiles.append(
            {"name": name, "old_pid": old_pid, "created_at": process_created_at}
        )
    unsigned = {key: value[key] for key in expected if key != "auth"}
    auth = value.get("auth")
    if not isinstance(auth, str) or not hmac.compare_digest(
        auth, _gateway_plan_auth(unsigned, lease_id)
    ):
        return None
    return {
        **unsigned,
        "profiles": profiles,
        "auth": auth,
    }


def _write_deferred_gateway_plan(args, root: Path) -> Path:
    invocation_id = getattr(args, "_update_invocation_id", None)
    lease = getattr(args, "_update_quiesce_lease", None)
    token = getattr(args, "_windows_gateway_resume_plan", None) or {}
    if not isinstance(invocation_id, str) or not isinstance(lease, dict):
        raise RuntimeError("deferred gateway plan lacks update correlation")
    lease_id = lease.get("lease_id")
    if not isinstance(lease_id, str):
        raise RuntimeError("deferred gateway plan lacks lease correlation")
    existing_path = getattr(args, "_deferred_gateway_plan_written", None)
    if isinstance(existing_path, Path):
        try:
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("deferred gateway plan became unreadable") from exc
        if _sanitize_deferred_gateway_plan(
            existing,
            root=root,
            invocation_id=invocation_id,
            lease_id=lease_id,
        ) is None:
            raise RuntimeError("deferred gateway plan correlation changed")
        return existing_path
    if token.get("unmapped") or token.get("unmapped_pids"):
        raise RuntimeError("unmapped gateways cannot be deferred safely")
    identities = token.get("profile_identities") or {}
    profiles = []
    for name, old_pid in sorted((token.get("profiles") or {}).items()):
        identity = identities.get(name)
        if not isinstance(identity, dict):
            raise RuntimeError(f"gateway profile {name!r} has no process identity")
        if (
            type(old_pid) is not int
            or type(identity.get("pid")) is not int
            or identity.get("pid") != old_pid
        ):
            raise RuntimeError(
                f"gateway profile {name!r} process identity does not match its PID"
            )
        profiles.append(
            {
                "name": str(name),
                "old_pid": int(old_pid),
                "created_at": float(identity["created_at"]),
            }
        )
    created_at = int(_time.time())
    unsigned = {
        "schema_version": 1,
        "invocation_id": invocation_id,
        "lease_fingerprint": hashlib.sha256(lease_id.encode("utf-8")).hexdigest(),
        "install_root": os.path.normcase(os.path.realpath(root)),
        "created_at": created_at,
        "expires_at": created_at + _DEFERRED_GATEWAY_PLAN_TTL_SECONDS,
        "profiles": profiles,
        "cold_start_if_installed": bool(token.get("cold_start_if_installed")),
    }
    payload = {**unsigned, "auth": _gateway_plan_auth(unsigned, lease_id)}
    sanitized = _sanitize_deferred_gateway_plan(
        payload,
        root=root,
        invocation_id=invocation_id,
        lease_id=lease_id,
        now=created_at,
    )
    if sanitized is None:
        raise RuntimeError("refusing invalid deferred gateway plan")
    path = _deferred_gateway_plan_path(root, invocation_id)
    _write_private_exclusive(
        path, json.dumps(sanitized, sort_keys=True, separators=(",", ":"))
    )
    setattr(args, "_deferred_gateway_plan_written", path)
    return path


def _load_deferred_gateway_plan(
    path: Path,
    *,
    root: Path,
    invocation_id: str,
    lease_id: str,
) -> tuple[str, dict] | None:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except FileNotFoundError:
        # A crash can occur after consume moves the pending name but before it
        # publishes the completed record. Recover only authenticated,
        # byte-identical consume tombstones; malformed or divergent evidence
        # remains fail-closed for manual recovery.
        candidates = sorted(path.parent.glob(f"{path.name}.consume-*"))
        if not candidates:
            return None
        recovered_raw: str | None = None
        recovered_value: dict | None = None
        for candidate in candidates:
            try:
                candidate_raw = candidate.read_text(encoding="utf-8")
                candidate_value = json.loads(candidate_raw)
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "deferred gateway plan recovery is unreadable"
                ) from exc
            candidate_plan = _sanitize_deferred_gateway_plan(
                candidate_value,
                root=root,
                invocation_id=invocation_id,
                lease_id=lease_id,
            )
            if candidate_plan is None:
                raise RuntimeError("deferred gateway plan recovery is invalid")
            if recovered_raw is not None and candidate_raw != recovered_raw:
                raise RuntimeError("deferred gateway plan recoveries diverged")
            recovered_raw = candidate_raw
            recovered_value = candidate_plan
        assert recovered_raw is not None and recovered_value is not None
        try:
            os.link(candidates[0], path)
        except FileExistsError:
            try:
                if path.read_text(encoding="utf-8") != recovered_raw:
                    raise RuntimeError("deferred gateway plan changed during recovery")
            except OSError as exc:
                raise RuntimeError(
                    "deferred gateway plan recovery could not be proven"
                ) from exc
        except OSError as exc:
            raise RuntimeError("deferred gateway plan could not be restored") from exc
        return recovered_raw, recovered_value
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("deferred gateway plan is unreadable") from exc
    sanitized = _sanitize_deferred_gateway_plan(
        value,
        root=root,
        invocation_id=invocation_id,
        lease_id=lease_id,
    )
    if sanitized is None:
        raise RuntimeError("deferred gateway plan is invalid or expired")
    return raw, sanitized


def _consume_deferred_gateway_plan(path: Path, expected_raw: str) -> bool:
    """Consume exact pending bytes into an idempotent completed record."""
    completed = path.with_suffix(".completed")
    tombstone = path.with_name(
        f"{path.name}.consume-{os.getpid()}-{secrets.token_hex(8)}"
    )
    try:
        os.replace(path, tombstone)
    except OSError:
        return False
    try:
        moved_raw = tombstone.read_text(encoding="utf-8")
    except OSError:
        # The rename already consumed the only discoverable pending name.
        # Restore the exact inode without overwriting anything so a transient
        # read/sharing failure cannot strand a stopped fleet with no retry
        # path. Retain the tombstone as fail-closed evidence if restoration
        # itself cannot be proven.
        try:
            os.link(tombstone, path)
        except OSError:
            pass
        return False
    if moved_raw != expected_raw:
        try:
            os.link(tombstone, path)
        except OSError:
            pass
        return False
    try:
        os.link(tombstone, completed)
    except (FileExistsError, OSError):
        try:
            os.link(tombstone, path)
        except OSError:
            pass
        return False
    try:
        tombstone.unlink()
    except OSError:
        # A completed record is terminal authority only after the pending
        # tombstone is retired. Roll it back and restore the exact pending
        # bytes so replay cannot skip lease cleanup after a partial consume.
        try:
            completed.unlink()
        except OSError:
            pass
        try:
            os.link(tombstone, path)
        except OSError:
            pass
        return False
    return True


def _validate_deferred_update_request(args) -> None:
    invocation_id = getattr(args, "invocation_id", None)
    lease_id = getattr(args, "bridge_lease_id", None)
    if invocation_id is not None and (
        not isinstance(invocation_id, str)
        or _IDENTIFIER_RE.fullmatch(invocation_id) is None
    ):
        raise ValueError("invalid --invocation-id")
    if lease_id is not None and (
        not isinstance(lease_id, str)
        or _IDENTIFIER_RE.fullmatch(lease_id) is None
    ):
        raise ValueError("invalid --bridge-lease-id")
    if not bool(getattr(args, "defer_gateway_resume", False)):
        return
    incompatible = [
        flag
        for flag in ("check", "preflight", "drain", "resume_deferred_gateway")
        if bool(getattr(args, flag, False))
    ]
    if incompatible:
        raise ValueError(
            "--defer-gateway-resume cannot be combined with --"
            + incompatible[0].replace("_", "-")
        )
    if not bool(getattr(args, "gateway", False)):
        raise ValueError("--defer-gateway-resume requires --gateway")
    if invocation_id is None:
        raise ValueError("--defer-gateway-resume requires a valid --invocation-id")
    if lease_id is None:
        raise ValueError("--defer-gateway-resume requires a valid --bridge-lease-id")


def _profile_process_still_matches(old_pid: int, created_at: float) -> bool:
    """Fail closed when the pre-update process identity cannot be disproved."""
    try:
        import psutil  # type: ignore
    except ImportError as exc:
        raise RuntimeError("psutil is required to verify gateway identity") from exc
    try:
        process = psutil.Process(int(old_pid))
        live_created = float(process.create_time())
    except psutil.NoSuchProcess:
        return False
    except Exception as exc:
        raise RuntimeError("could not revalidate prior gateway process identity") from exc
    if not math.isfinite(live_created):
        raise RuntimeError("prior gateway process creation time is invalid")
    return abs(live_created - float(created_at)) <= 0.001


def _running_gateway_profiles() -> dict[str, int]:
    from hermes_cli.gateway import find_profile_gateway_processes

    return {
        str(process.profile): int(process.pid)
        for process in find_profile_gateway_processes()
    }


def _spawn_deferred_gateway_profile(profile: str) -> int:
    """Start one derived Hermes profile without accepting caller argv."""
    from hermes_constants import get_default_hermes_root
    from hermes_cli import gateway_windows

    default_root = get_default_hermes_root()
    profile_home = default_root if profile == "default" else default_root / "profiles" / profile
    if profile != "default" and not profile_home.is_dir():
        raise RuntimeError(f"gateway profile {profile!r} no longer exists")
    previous = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(profile_home)
    try:
        return int(gateway_windows._spawn_detached())
    finally:
        if previous is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous


def _wait_for_deferred_gateway_profile(profile: str, *, timeout: float = 20.0) -> bool:
    deadline = _time.monotonic() + max(0.1, float(timeout))
    while _time.monotonic() < deadline:
        try:
            if profile in _running_gateway_profiles():
                return True
        except Exception:
            pass
        _time.sleep(0.2)
    return False


def _resume_deferred_gateway_fleet(plan: dict) -> None:
    """Resume only the authenticated structured fleet, idempotently."""
    running = _running_gateway_profiles()
    for entry in plan["profiles"]:
        profile = str(entry["name"])
        running_pid = running.get(profile)
        if running_pid is not None:
            if int(running_pid) == int(entry["old_pid"]) and _profile_process_still_matches(
                int(entry["old_pid"]), float(entry["created_at"])
            ):
                raise RuntimeError(
                    f"prior gateway profile {profile!r} is still running"
                )
            # A different verified profile PID (or a recycled numeric PID
            # whose creation identity does not match) is a prior successful
            # partial-resume result. Do not start a duplicate.
            continue
        if _profile_process_still_matches(
            int(entry["old_pid"]), float(entry["created_at"])
        ):
            raise RuntimeError(
                f"prior gateway profile {profile!r} is still running"
            )
        if _spawn_deferred_gateway_profile(profile) <= 0:
            raise RuntimeError(f"gateway profile {profile!r} did not start")
        if not _wait_for_deferred_gateway_profile(profile):
            raise RuntimeError(f"gateway profile {profile!r} did not become ready")
        running = _running_gateway_profiles()

    if plan["cold_start_if_installed"] and not plan["profiles"]:
        if "default" not in running:
            from hermes_cli import gateway_windows

            if gateway_windows.is_installed():
                if _spawn_deferred_gateway_profile("default") <= 0:
                    raise RuntimeError("default gateway did not start")
                if not _wait_for_deferred_gateway_profile("default"):
                    raise RuntimeError("default gateway did not become ready")


def _cmd_update_resume_deferred_gateway(args, *, root: Path) -> NoReturn:
    """Consume one authenticated deferred fleet plan outside mutation Jobs."""
    invocation_id = getattr(args, "invocation_id", None)
    lease_id = getattr(args, "bridge_lease_id", None)
    requested_root = getattr(args, "resume_root", None)
    if (
        not isinstance(invocation_id, str)
        or _IDENTIFIER_RE.fullmatch(invocation_id) is None
        or not isinstance(lease_id, str)
        or _IDENTIFIER_RE.fullmatch(lease_id) is None
        or not isinstance(requested_root, str)
        or os.path.normcase(os.path.realpath(requested_root))
        != os.path.normcase(os.path.realpath(root))
    ):
        print("✗ Invalid deferred gateway resume request.")
        raise SystemExit(1)

    pending_path = _deferred_gateway_plan_path(root, invocation_id)
    completed_path = _deferred_gateway_plan_path(root, invocation_id, completed=True)
    completed = _load_deferred_gateway_plan(
        completed_path,
        root=root,
        invocation_id=invocation_id,
        lease_id=lease_id,
    )

    from hermes_cli.update_lock import UpdateLock
    from hermes_mcp_update_gate import marker_path, read_quiesce_lease

    if completed is not None and read_quiesce_lease(marker_path()) is None:
        print("✓ Deferred gateway fleet was already resumed.")
        raise SystemExit(0)

    update_lock = UpdateLock()
    lease: dict | None = None
    prior_owner_pid: int | None = None
    success = False
    try:
        if not update_lock.acquire() or not update_lock.prove_claim():
            raise RuntimeError("update handoff lock is not owned by this transaction")
        prior = read_quiesce_lease(marker_path())
        if not (
            isinstance(prior, dict)
            and prior.get("schema_version") == 1
            and prior.get("lease_id") == lease_id
        ):
            raise RuntimeError("deferred gateway lease is missing or changed")
        prior_owner_pid = int(prior.get("owner_pid", 0))
        lease = _claim_update_quiesce_lease(root, expected_lease_id=lease_id)
        # The native parent must prove that its exact spawned child held the
        # capability even when a fast no-op resume adopts and clears the lease
        # between marker polls.  Emit no capability bytes: this frame is only
        # an identity-bound observation, and terminal success still requires
        # the correlated receipt/plan plus exact lease cleanup.
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "event": "deferred-gateway-lease-adopted",
                    "invocation_id": invocation_id,
                    "owner_pid": os.getpid(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
        receipt = _load_update_receipt(root)
        if receipt is not None and receipt.get("invocation_id") == invocation_id:
            if not (
                receipt.get("lease_id") == lease_id
                and receipt.get("gateway_resume_deferred") is True
            ):
                raise RuntimeError("deferred gateway receipt correlation failed")
        if completed is None:
            loaded = _load_deferred_gateway_plan(
                pending_path,
                root=root,
                invocation_id=invocation_id,
                lease_id=lease_id,
            )
            if loaded is None:
                raise RuntimeError("deferred gateway plan is missing")
            raw, plan = loaded
            _resume_deferred_gateway_fleet(plan)
            if not _consume_deferred_gateway_plan(pending_path, raw):
                raise RuntimeError("deferred gateway plan changed before consume")
        success = True
    except Exception as exc:
        print(f"✗ Deferred gateway resume failed: {exc}")
    finally:
        try:
            if lease is not None:
                if success:
                    try:
                        released = _release_update_quiesce_lease(root, lease)
                    except Exception:
                        released = False
                    if not released:
                        success = False
                        print("✗ Deferred gateway lease cleanup could not be proven.")
                if not success and prior_owner_pid is not None and prior_owner_pid > 0:
                    try:
                        _transfer_update_quiesce_lease(
                            root, lease, new_owner_pid=prior_owner_pid
                        )
                    except Exception:
                        # Retain the child-owned/foreign marker as fail-closed
                        # evidence. The native parent will reject terminal
                        # success and can run its bounded recovery flow.
                        pass
        finally:
            update_lock.release()
    if success:
        print(
            "✓ Deferred gateway fleet was already resumed."
            if completed is not None
            else "✓ Deferred gateway fleet resumed."
        )
    raise SystemExit(0 if success else 1)


def _sanitize_update_receipt(value: object, root: Path) -> dict | None:
    if not isinstance(value, dict):
        return None
    expected_receipt_keys = {
        "schema_version",
        "invocation_id",
        "lease_id",
        "mode",
        "root",
        "remote",
        "branch",
        "target_ref",
        "target_sha",
        "resulting_head",
        "archive_sha",
        "timestamp",
        "success",
        "gateway_resume_deferred",
        "health",
    }
    if set(value) != expected_receipt_keys:
        return None
    try:
        timestamp = int(value["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    if value.get("schema_version") != 1 or value.get("success") is not True:
        return None
    if type(value.get("gateway_resume_deferred")) is not bool:
        return None
    if value.get("mode") not in {"git", "archive"} or timestamp <= 0:
        return None
    if os.path.normcase(os.path.realpath(str(value.get("root", "")))) != os.path.normcase(
        os.path.realpath(root)
    ):
        return None
    invocation_id = value.get("invocation_id")
    lease_id = value.get("lease_id")
    if not isinstance(invocation_id, str) or _IDENTIFIER_RE.fullmatch(invocation_id) is None:
        return None
    if not isinstance(lease_id, str) or _IDENTIFIER_RE.fullmatch(lease_id) is None:
        return None
    branch = value.get("branch")
    if not isinstance(branch, str) or not branch:
        return None
    remote = value.get("remote")
    target_ref = value.get("target_ref")
    if remote is not None and not isinstance(remote, str):
        return None
    if target_ref is not None and not isinstance(target_ref, str):
        return None
    shas: dict[str, str | None] = {}
    for field in ("target_sha", "resulting_head", "archive_sha"):
        candidate = value.get(field)
        if candidate is not None and (
            not isinstance(candidate, str) or _SHA_RE.fullmatch(candidate) is None
        ):
            return None
        shas[field] = candidate.lower() if candidate else None
    if value["mode"] == "git":
        if (
            not remote
            or not target_ref
            or shas["target_sha"] is None
            or shas["resulting_head"] is None
            or shas["target_sha"] != shas["resulting_head"]
            or shas["archive_sha"] is not None
        ):
            return None
    elif (
        remote is not None
        or target_ref is not None
        or shas["target_sha"] is not None
        or shas["resulting_head"] is not None
        or shas["archive_sha"] is None
        or len(shas["archive_sha"]) != 64
    ):
        return None
    health = value.get("health")
    expected_health = {
        "critical_syntax",
        "critical_imports",
        "dependencies",
        "node_dependencies",
    }
    if not isinstance(health, dict) or set(health) != expected_health:
        return None
    if any(type(health[field]) is not bool for field in expected_health) or not all(
        health[field] for field in expected_health
    ):
        return None
    return {
        "schema_version": 1,
        "invocation_id": invocation_id,
        "lease_id": lease_id,
        "mode": value["mode"],
        "root": os.path.normcase(os.path.realpath(root)),
        "remote": remote,
        "branch": branch,
        "target_ref": target_ref,
        **shas,
        "timestamp": timestamp,
        "success": True,
        "gateway_resume_deferred": bool(value["gateway_resume_deferred"]),
        "health": {field: bool(health[field]) for field in sorted(expected_health)},
    }


def _load_update_receipt(root: Path) -> dict | None:
    try:
        value = json.loads(_receipt_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return _sanitize_update_receipt(value, root)


def _write_update_receipt(
    root: Path,
    *,
    invocation_id: str,
    lease_id: str,
    mode: str,
    branch: str,
    remote: str | None,
    target_ref: str | None,
    target_sha: str | None,
    resulting_head: str | None,
    archive_sha: str | None,
    gateway_resume_deferred: bool,
    health: dict[str, bool],
) -> dict:
    value = {
        "schema_version": 1,
        "invocation_id": invocation_id,
        "lease_id": lease_id,
        "mode": mode,
        "root": os.path.normcase(os.path.realpath(root)),
        "remote": remote,
        "branch": branch,
        "target_ref": target_ref,
        "target_sha": target_sha,
        "resulting_head": resulting_head,
        "archive_sha": archive_sha,
        "timestamp": int(_time.time()),
        "success": True,
        "gateway_resume_deferred": bool(gateway_resume_deferred),
        "health": health,
    }
    sanitized = _sanitize_update_receipt(value, root)
    if sanitized is None:
        raise ValueError("refusing to write an invalid update receipt")
    path = _receipt_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(sanitized, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)
    return sanitized


def _record_update_success(
    args,
    *,
    mode: str,
    branch: str,
    remote: str | None,
    target_ref: str | None,
    target_sha: str | None,
    resulting_head: str | None,
    archive_sha: str | None,
    health: dict[str, bool],
) -> dict | None:
    """Write a receipt only for an atomic invocation that owns a lease."""
    invocation_id = getattr(args, "_update_invocation_id", None)
    lease = getattr(args, "_update_quiesce_lease", None)
    lease_id = lease.get("lease_id") if isinstance(lease, dict) else None
    if not isinstance(invocation_id, str) or not isinstance(lease_id, str):
        return None
    expected_health = {
        "critical_syntax",
        "critical_imports",
        "dependencies",
        "node_dependencies",
    }
    if (
        set(health) != expected_health
        or any(type(health[key]) is not bool for key in expected_health)
        or not all(health.values())
    ):
        raise RuntimeError("refusing success receipt without complete health proof")

    # The cached lease object is not authority: a heartbeat may have lost the
    # marker or another process may have replaced it during mutation.  Re-read
    # the live capability immediately before the receipt becomes durable.
    from hermes_mcp_update_gate import live_quiesce_lease, marker_path

    root = Path(_m().PROJECT_ROOT)
    live = live_quiesce_lease(marker_path(), install_root=root)
    if not (
        isinstance(live, dict)
        and live.get("schema_version") == 1
        and live.get("lease_id") == lease_id
        and live.get("owner_pid") == os.getpid()
    ):
        raise RuntimeError("update quiesce lease ownership was lost before receipt")
    deferred = bool(getattr(args, "defer_gateway_resume", False))
    if deferred:
        # Publish the authenticated, no-argv fleet state first.  The receipt
        # is the terminal mutation proof and must never claim a resumable
        # update when the private plan was not durably published.
        _write_deferred_gateway_plan(args, root)
    receipt = _write_update_receipt(
        root,
        invocation_id=invocation_id,
        lease_id=lease_id,
        mode=mode,
        branch=branch,
        remote=remote,
        target_ref=target_ref,
        target_sha=target_sha,
        resulting_head=resulting_head,
        archive_sha=archive_sha,
        gateway_resume_deferred=deferred,
        health=health,
    )
    setattr(args, "_update_receipt_written", True)
    return receipt


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


def _claim_update_quiesce_lease(
    root: Path,
    *,
    expected_lease_id: str | None = None,
) -> dict:
    """Create a lease or adopt the exact capability supplied by a handoff."""
    from hermes_mcp_update_gate import (
        adopt_quiesce_lease,
        live_quiesce_lease,
        marker_path,
        write_quiesce_lease,
    )

    path = marker_path()
    active = live_quiesce_lease(path, install_root=root)
    if expected_lease_id is not None and _IDENTIFIER_RE.fullmatch(expected_lease_id) is None:
        raise RuntimeError("invalid bridge lease capability")
    if active is not None:
        if active.get("schema_version") != 1:
            raise RuntimeError("a legacy bridge lease is already active")
        active_id = str(active.get("lease_id", ""))
        if int(active.get("owner_pid", 0)) == os.getpid():
            if expected_lease_id is not None and active_id != expected_lease_id:
                raise RuntimeError("bridge lease capability does not match")
            return write_quiesce_lease(
                root,
                marker=path,
                lease_id=active_id,
                owner_pid=os.getpid(),
                handoff_grace_seconds=0,
            )
        if expected_lease_id is None or active_id != expected_lease_id:
            raise RuntimeError("another updater owns the bridge quiesce lease")
        from hermes_cli.update_lock import _is_ancestor_pid

        owner_pid = int(active.get("owner_pid", 0))
        if owner_pid != os.getpid() and not _is_ancestor_pid(owner_pid):
            raise RuntimeError(
                "bridge lease owner is not this process or its live ancestor"
            )
        adopted = adopt_quiesce_lease(
            root,
            marker=path,
            lease_id=expected_lease_id,
            owner_pid=os.getpid(),
        )
        if adopted is None:
            raise RuntimeError("bridge quiesce lease adoption failed")
        return adopted
    if expected_lease_id is not None:
        raise RuntimeError("expected bridge quiesce lease is missing or stale")

    # Malformed, expired, or dead-owner state is bounded stale state. It does
    # not authorize a kill, but it also must not wedge updates indefinitely;
    # atomically replace it with this invocation's fresh capability.
    return write_quiesce_lease(
        root,
        marker=path,
        owner_pid=os.getpid(),
        handoff_grace_seconds=0,
    )


def _release_update_quiesce_lease(root: Path, lease: dict | None) -> bool:
    if not lease:
        return False
    from hermes_mcp_update_gate import clear_quiesce_lease, marker_path

    return clear_quiesce_lease(
        str(lease.get("lease_id", "")),
        owner_pid=os.getpid(),
        marker=marker_path(),
        install_root=root,
    )


def _transfer_update_quiesce_lease(
    root: Path, lease: dict, *, new_owner_pid: int
) -> dict:
    """Atomically return/adopt a lease across one verified parent handoff."""
    from hermes_cli.update_lock import _is_ancestor_pid
    from hermes_mcp_update_gate import marker_path, write_quiesce_lease

    owner_pid = int(new_owner_pid)
    if owner_pid <= 0 or not _is_ancestor_pid(owner_pid):
        raise RuntimeError("bridge lease handoff owner is not a live ancestor")
    return write_quiesce_lease(
        root,
        marker=marker_path(),
        lease_id=str(lease.get("lease_id", "")),
        owner_pid=owner_pid,
        expected_owner_pid=os.getpid(),
        lifetime_seconds=1200,
        handoff_grace_seconds=90,
    )


def _drain_under_update_lease(
    root: Path,
    lease: dict,
    *,
    branch: str,
    timeout_seconds: float,
    allow_hard_processes: bool = False,
) -> dict[str, object]:
    """Drain only actionable MCP records and prove two bounded clear scans."""
    from hermes_cli._scan_venv_blockers import (
        scan_venv_blockers,
        terminate_mcp_bridge,
    )
    from hermes_mcp_update_gate import marker_path, write_quiesce_lease

    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or not 0.1 <= timeout <= 120.0:
        raise ValueError("timeout_seconds must be between 0.1 and 120")
    deadline = _time.monotonic() + timeout
    actions: list[dict] = []
    clear_scans = 0
    cooperative_wait_done = False
    last_scan: dict[str, object] = {}
    try:
        git_metadata = _git_preflight_metadata(root, branch)
        receipt = _load_update_receipt(root)
    except Exception as exc:
        return _readiness_payload(
            mode="drain",
            root=root,
            lease=lease,
            reason="probe-failed",
            error={"code": "probe-failed", "message": str(exc)},
        )
    while True:
        if _time.monotonic() > deadline:
            return _readiness_payload(
                mode="drain",
                root=root,
                scan=last_scan,
                git=git_metadata,
                receipt=receipt,
                lease=lease,
                actions=actions,
                ok=True,
                ready=False,
                reason="drain-timeout",
            )
        try:
            last_scan = scan_venv_blockers(root)
        except Exception as exc:
            return _readiness_payload(
                mode="drain",
                root=root,
                lease=lease,
                actions=actions,
                reason="probe-failed",
                error={"code": "probe-failed", "message": str(exc)},
            )

        bridges = list(last_scan.get("mcp_bridges", []))
        hard_processes = list(last_scan.get("processes", []))
        unactionable = [entry for entry in bridges if not bool(entry.get("actionable"))]
        if (hard_processes or unactionable) and not allow_hard_processes:
            reason = "venv-blocked" if hard_processes else "mcp-owner-unverified"
            return _readiness_payload(
                mode="drain",
                root=root,
                scan=last_scan,
                git=git_metadata,
                receipt=receipt,
                lease=lease,
                actions=actions,
                ok=True,
                ready=False,
                reason=reason,
            )

        actionable = [entry for entry in bridges if bool(entry.get("actionable"))]
        if actionable:
            clear_scans = 0
            actions = [
                action for action in actions if action.get("type") != "clear-scan"
            ]
            if not cooperative_wait_done:
                cooperative_wait_done = True
                remaining = deadline - _time.monotonic()
                if remaining > 0:
                    _time.sleep(min(_DRAIN_COOPERATIVE_WAIT_SECONDS, remaining))
                continue
            # The scanner supplies worker-first ordering. Preserve it so an
            # external base worker's live wrapper ancestry remains provable.
            for entry in actionable:
                terminated = terminate_mcp_bridge(
                    root,
                    pid=int(entry["pid"]),
                    created_at=float(entry["created_at"]),
                )
                actions.append(
                    {
                        "type": "terminate-mcp-bridge",
                        "pid": int(entry["pid"]),
                        "created_at": float(entry["created_at"]),
                        "owner": str(entry["owner"]),
                        "role": str(entry["role"]),
                        "terminated": bool(terminated),
                    }
                )
            _time.sleep(min(0.1, max(0.0, deadline - _time.monotonic())))
            continue

        clear_scans += 1
        actions.append({"type": "clear-scan", "sequence": clear_scans})
        if clear_scans >= 2:
            # Renew from success time so the caller has a full, bounded handoff
            # window. The token stays stable for explicit updater adoption.
            lease = write_quiesce_lease(
                root,
                marker=marker_path(),
                lease_id=str(lease["lease_id"]),
                owner_pid=os.getpid(),
                lifetime_seconds=120,
                handoff_grace_seconds=90,
            )
            return _readiness_payload(
                mode="drain",
                root=root,
                scan=last_scan,
                git=git_metadata,
                receipt=receipt,
                lease=lease,
                actions=actions,
                ok=True,
                ready=True,
            )
        remaining = deadline - _time.monotonic()
        if remaining > 0:
            _time.sleep(min(_DRAIN_CLEAR_INTERVAL_SECONDS, remaining))


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


def _cmd_update_drain(args, *, root: Path) -> NoReturn:
    if not bool(getattr(args, "yes", False)):
        payload = _readiness_payload(
            mode="drain",
            root=root,
            reason="consent-required",
            error=None,
            ok=True,
            ready=False,
        )
        _print_update_readiness(payload, json_mode=bool(getattr(args, "json", False)))
        raise SystemExit(2)
    from hermes_cli.update_lock import UpdateLock

    update_lock = UpdateLock()
    lease: dict | None = None
    success = False
    try:
        claimed = update_lock.acquire()
        if not claimed:
            if update_lock.holder is not None:
                payload = _readiness_payload(
                    mode="drain",
                    root=root,
                    reason="update-running",
                    ok=True,
                    ready=False,
                )
            else:
                payload = _readiness_payload(
                    mode="drain",
                    root=root,
                    reason="lock-failed",
                    error={
                        "code": str(update_lock.failure_reason or "lock-failed"),
                        "message": "could not acquire the update safety lock",
                    },
                )
        else:
            # An accepted parent handoff does not rewrite the parent's marker;
            # prove it is still live before acquiring the bridge lease.
            lock_proven = update_lock.prove_claim()
            if not lock_proven:
                payload = _readiness_payload(
                    mode="drain",
                    root=root,
                    reason="lock-failed",
                    error={
                        "code": "lock-lost",
                        "message": "update safety lock disappeared before drain",
                    },
                )
            else:
                lease = _claim_update_quiesce_lease(root)
                branch = (getattr(args, "branch", None) or "main").strip() or "main"
                payload = _drain_under_update_lease(
                    root,
                    lease,
                    branch=branch,
                    timeout_seconds=float(
                        getattr(args, "timeout_seconds", _DEFAULT_DRAIN_TIMEOUT_SECONDS)
                    ),
                )
                success = bool(payload.get("ok") and payload.get("ready"))
    except Exception as exc:
        payload = _readiness_payload(
            mode="drain",
            root=root,
            lease=lease,
            reason="lease-failed",
            error={"code": "lease-failed", "message": str(exc)},
        )
    finally:
        try:
            if lease is not None and not success:
                try:
                    released = _release_update_quiesce_lease(root, lease)
                except Exception as exc:
                    released = False
                    cleanup_message = str(exc)
                else:
                    cleanup_message = "bridge lease cleanup could not be proven"
                if not released:
                    payload = _readiness_payload(
                        mode="drain",
                        root=root,
                        lease=lease,
                        reason="lease-failed",
                        error={
                            "code": "lease-cleanup-failed",
                            "message": cleanup_message,
                        },
                    )
        finally:
            update_lock.release()
    _print_update_readiness(payload, json_mode=bool(getattr(args, "json", False)))
    raise SystemExit(_readiness_exit_code(payload))


class _UpdateLeaseHeartbeat:
    """Renew an owner-bound mutation lease during a long dependency rebuild."""

    def __init__(
        self,
        root: Path,
        lease: dict,
        interval_seconds: float = 30.0,
        *,
        fail_stop: Callable[[str], object] | None = None,
    ):
        self.root = root
        self.lease = lease
        self.interval_seconds = interval_seconds
        self.lost = False
        self.loss_reason: str | None = None
        self._fail_stop = fail_stop or self._exit_process
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="hermes-update-lease-heartbeat",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():
            self._lose("update lease heartbeat did not stop cleanly")
            raise RuntimeError("update lease heartbeat did not stop cleanly")

    @staticmethod
    def _exit_process(_reason: str) -> NoReturn:
        # A cooperative exception in this daemon thread cannot interrupt a
        # dependency/git subprocess running on the main thread.  Losing the
        # bridge gate while source mutation continues is unsafe, so terminate
        # the updater process immediately.  The emergency shadow below keeps
        # bridges gated while any already-started child process unwinds.
        os._exit(1)

    def _lose(self, reason: str) -> None:
        if self.lost:
            return
        self.lost = True
        self.loss_reason = reason
        try:
            from hermes_mcp_update_gate import write_emergency_quiesce_shadow

            write_emergency_quiesce_shadow(
                self.root,
                lease_id=str(self.lease.get("lease_id", "")),
                owner_pid=os.getpid(),
            )
        except Exception as exc:
            logger.critical(
                "Update lease was lost and the emergency bridge gate failed: %s",
                exc,
            )
        self._fail_stop(reason)

    def _renew_once(self) -> bool:
        from hermes_mcp_update_gate import (
            marker_path,
            read_quiesce_lease,
            write_quiesce_lease,
        )

        try:
            current = read_quiesce_lease(marker_path())
        except Exception as exc:
            self._lose(f"bridge quiesce lease probe failed: {exc}")
            return False
        if not isinstance(current, dict) or (
            current.get("schema_version") != 1
            or current.get("lease_id") != self.lease.get("lease_id")
            or current.get("owner_pid") != os.getpid()
            or os.path.normcase(os.path.realpath(str(current.get("install_root", ""))))
            != os.path.normcase(os.path.realpath(self.root))
        ):
            self._lose("bridge quiesce lease identity changed")
            return False
        try:
            self.lease = write_quiesce_lease(
                self.root,
                marker=marker_path(),
                lease_id=str(self.lease["lease_id"]),
                owner_pid=os.getpid(),
                lifetime_seconds=1200,
                handoff_grace_seconds=0,
            )
        except Exception as exc:
            self._lose(f"bridge quiesce lease renewal failed: {exc}")
            return False
        return True

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            if not self._renew_once():
                return


class _WindowsMutationJob:
    """Contain this updater and every descendant in a kill-on-close job."""

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows mutation containment is Windows-only")
        import ctypes
        from ctypes import wintypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class _BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ctypes = ctypes
        self._info_type = _ExtendedLimitInformation
        self._accounting_type = _BasicAccountingInformation
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL
        self._set_information = kernel32.SetInformationJobObject
        self._set_information.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._set_information.restype = wintypes.BOOL
        self._query_information = kernel32.QueryInformationJobObject
        self._query_information.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._query_information.restype = wintypes.BOOL
        create_job = kernel32.CreateJobObjectW
        create_job.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        create_job.restype = wintypes.HANDLE
        assign = kernel32.AssignProcessToJobObject
        assign.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        assign.restype = wintypes.BOOL
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.argtypes = []
        get_current_process.restype = wintypes.HANDLE

        handle = create_job(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        self.handle = handle
        try:
            self._configure(kill_on_close=True)
            if not assign(handle, get_current_process()):
                raise OSError(
                    ctypes.get_last_error(),
                    "could not contain the updater in a Windows Job",
                )
        except BaseException:
            self._close_handle(handle)
            self.handle = None
            raise

    def _configure(
        self, *, kill_on_close: bool, allow_breakaway: bool = False
    ) -> None:
        info = self._info_type()
        flags = 0
        if kill_on_close:
            flags |= self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if allow_breakaway:
            flags |= self._JOB_OBJECT_LIMIT_BREAKAWAY_OK
        info.BasicLimitInformation.LimitFlags = flags
        if not self._set_information(
            self.handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            self._ctypes.byref(info),
            self._ctypes.sizeof(info),
        ):
            raise OSError(
                self._ctypes.get_last_error(),
                "SetInformationJobObject failed",
            )

    def abort(self, _reason: str) -> NoReturn:
        """Atomically terminate the updater and its already-spawned mutators."""
        handle, self.handle = self.handle, None
        if handle:
            self._close_handle(handle)
        # Closing the last kill-on-close handle terminates this process too;
        # retain a fail-stop fallback for an unexpected platform/API failure.
        os._exit(1)

    def _active_processes(self) -> int:
        accounting = self._accounting_type()
        returned = self._ctypes.c_ulong()
        if not self._query_information(
            self.handle,
            1,  # JobObjectBasicAccountingInformation
            self._ctypes.byref(accounting),
            self._ctypes.sizeof(accounting),
            self._ctypes.byref(returned),
        ):
            raise OSError(
                self._ctypes.get_last_error(),
                "QueryInformationJobObject failed",
            )
        return int(accounting.ActiveProcesses)

    def disarm(self, *, timeout_seconds: float = 5.0) -> None:
        """Release containment only after every update descendant has exited."""
        if not self.handle:
            return
        deadline = _time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            try:
                active = self._active_processes()
            except OSError as exc:
                self.abort(f"could not prove update descendants exited: {exc}")
            if active <= 1:
                break
            if _time.monotonic() >= deadline:
                self.abort(
                    f"{active - 1} update descendant(s) survived mutation completion"
                )
            _time.sleep(0.05)
        try:
            # The updater remains associated with the Job until this handle is
            # closed, so first remove KILL_ON_CLOSE after proving it is the
            # only member. Never enable BREAKAWAY_OK: every mutator was born
            # under a no-breakaway boundary, and persistent gateway resume is
            # either after this Job is destroyed (direct CLI) or in the
            # separate parent-owned resume child (Desktop/bootstrap).
            self._configure(kill_on_close=False, allow_breakaway=False)
        except OSError as exc:
            self.abort(f"could not disarm update descendant containment: {exc}")
        handle, self.handle = self.handle, None
        self._close_handle(handle)


def _prepare_atomic_windows_update(args, *, root: Path) -> tuple[dict, str]:
    """Acquire consent+lease, then drain before any update mutation occurs."""
    handoff_id = getattr(args, "bridge_lease_id", None)
    handoff_owner_pid: int | None = None
    if handoff_id:
        from hermes_mcp_update_gate import marker_path, read_quiesce_lease

        prior = read_quiesce_lease(marker_path())
        if (
            isinstance(prior, dict)
            and prior.get("schema_version") == 1
            and prior.get("lease_id") == handoff_id
        ):
            try:
                handoff_owner_pid = int(prior.get("owner_pid", 0))
            except (TypeError, ValueError):
                handoff_owner_pid = None
    assume_yes = bool(getattr(args, "yes", False))
    if not assume_yes and not handoff_id:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("✗ Safe Windows update requires explicit consent; re-run with --yes.")
            raise SystemExit(2)
        try:
            response = input(
                "This update may pause verified Codex or Claude Hermes MCP bridges. "
                "Continue? [y/N]: "
            )
        except (EOFError, KeyboardInterrupt, UnicodeDecodeError):
            response = ""
        if response.strip().lower() not in {"y", "yes"}:
            print("Update cancelled.")
            raise SystemExit(2)

    lease = _claim_update_quiesce_lease(root, expected_lease_id=handoff_id)

    def _return_or_release_on_failure(current: dict) -> None:
        if handoff_id and handoff_owner_pid is not None and handoff_owner_pid > 0:
            try:
                _transfer_update_quiesce_lease(
                    root, current, new_owner_pid=handoff_owner_pid
                )
                return
            except Exception as transfer_error:
                # The adopting child must not disappear with a zero-grace
                # capability if the parent handoff cannot be restored.
                try:
                    from hermes_mcp_update_gate import write_emergency_quiesce_shadow

                    write_emergency_quiesce_shadow(
                        root,
                        lease_id=str(current.get("lease_id", "")),
                        owner_pid=os.getpid(),
                    )
                except Exception:
                    pass
                raise RuntimeError(
                    "could not return the bridge lease to the update parent"
                ) from transfer_error
        _release_update_quiesce_lease(root, current)

    try:
        branch = (getattr(args, "branch", None) or "main").strip() or "main"
        force_venv = bool(getattr(args, "force_venv", False))
        if force_venv:
            print(
                "⚠ --force-venv: unverified venv holders will not block mutation; "
                "native extensions may remain locked and the install may be damaged."
            )
        payload = _drain_under_update_lease(
            root,
            lease,
            branch=branch,
            timeout_seconds=float(
                getattr(args, "timeout_seconds", _DEFAULT_DRAIN_TIMEOUT_SECONDS)
            ),
            allow_hard_processes=force_venv,
        )
        if not payload.get("ok") or not payload.get("ready"):
            _print_update_readiness(payload, json_mode=False)
            raise SystemExit(_readiness_exit_code(payload))

        from hermes_mcp_update_gate import marker_path, write_quiesce_lease

        lease = write_quiesce_lease(
            root,
            marker=marker_path(),
            lease_id=str(lease["lease_id"]),
            owner_pid=os.getpid(),
            lifetime_seconds=1200,
            handoff_grace_seconds=0,
        )
        requested_invocation = getattr(args, "invocation_id", None)
        if requested_invocation is not None and (
            not isinstance(requested_invocation, str)
            or _IDENTIFIER_RE.fullmatch(requested_invocation) is None
        ):
            raise RuntimeError("invalid update invocation identity")
        invocation_id = requested_invocation or secrets.token_urlsafe(24)
        setattr(args, "_update_quiesce_lease", lease)
        setattr(args, "_update_invocation_id", invocation_id)
        setattr(args, "_update_handoff_owner_pid", handoff_owner_pid)
        return lease, invocation_id
    except BaseException:
        _return_or_release_on_failure(lease)
        raise


def _m():
    """Lazy ``hermes_cli.main`` reference.

    Lets callers keep patching ``hermes_cli.main.<helper>`` (the historical
    test surface) and have those patches reach this code path, and defers the
    import so ``hermes_cli.main`` -> ``hermes_cli.update_cmd`` stays one-way
    at import time.
    """
    from hermes_cli import main

    return main


_UPDATE_RUNTIME_RELOAD_MODULES = (
    "hermes_constants",
    "tools.environments.local",
    "tools.lazy_deps",
)

def _reload_updated_runtime_modules() -> None:
    """Reload update-sensitive modules after the checkout changes in-place.

    ``hermes update`` keeps running in the pre-pull Python process. After a
    large update, modules already present in ``sys.modules`` can still expose
    old symbols even though their source files on disk are new. Refresh the
    small module set used by lazy-backend refresh before that step imports
    newly-updated code paths.
    """
    try:
        import importlib

        importlib.invalidate_caches()
        for module_name in _UPDATE_RUNTIME_RELOAD_MODULES:
            module = _m().sys.modules.get(module_name)
            if module is None:
                continue
            try:
                importlib.reload(module)
            except Exception as exc:
                logger.debug("Could not reload updated module %s: %s", module_name, exc)
    except Exception as exc:
        logger.debug("Could not refresh update runtime modules: %s", exc)


def _reload_config_modules() -> None:
    """Force-reload config modules from disk after git pull.

    ``hermes update`` runs in the PRE-pull Python process. After ``git pull``
    updates the source files on disk, modules already in ``sys.modules``
    still hold the OLD code. Function-level imports return the cached module,
    so ``DEFAULT_CONFIG["_config_version"]`` is the OLD value and
    ``check_config_version()`` reports ``(33, 33)`` — "up to date" — even
    though the freshly-pulled code has v34 with a migration to run.

    This function force-reloads ``hermes_cli.config_defaults``,
    ``hermes_cli.config``, and ``hermes_cli.config_migrations`` from disk
    so subsequent imports read the UPDATED code.
    """
    import importlib

    importlib.invalidate_caches()
    for mod_name in ("hermes_cli.config_defaults", "hermes_cli.config", "hermes_cli.config_migrations"):
        mod = sys.modules.get(mod_name)
        if mod is not None:
            try:
                importlib.reload(mod)
            except Exception as exc:
                logger.debug("Could not reload %s for fresh config check: %s", mod_name, exc)


def _run_config_check_fresh() -> tuple:
    """Check config version using freshly-reloaded modules.

    See ``_reload_config_modules`` for why this is necessary.
    Returns ``(current_ver, latest_ver)``.
    """
    _reload_config_modules()
    from hermes_cli.config import check_config_version

    return check_config_version()


def _run_migrate_config_fresh(*, interactive: bool = False, quiet: bool = False) -> dict:
    """Run config migration using freshly-reloaded modules.

    See ``_reload_config_modules`` for why this is necessary.
    Returns the migration results dict.
    """
    _reload_config_modules()
    from hermes_cli.config import migrate_config

    return migrate_config(interactive=interactive, quiet=quiet)


# Critical files that Hermes must be able to import immediately after an
# update/install. Most are imported on every CLI startup; ``web_server.py``
# is the desktop/dashboard backend path that a fresh Windows install launches
# right away. If any of these fail to parse after a pull, the user can be
# left with a bricked CLI or desktop backend. The post-pull syntax guard
# validates these and auto-rolls-back on failure.
_UPDATE_CRITICAL_FILES = (
    "hermes_cli/main.py",
    "hermes_cli/config.py",
    "hermes_cli/__init__.py",
    "hermes_cli/web_server.py",
    "cli.py",
    "run_agent.py",
    "model_tools.py",
    "toolsets.py",
    "hermes_constants.py",
)

def _capture_head_sha(git_cmd, cwd) -> str | None:
    """Return the current HEAD SHA, or None if it can't be resolved."""
    try:
        result = subprocess.run(
            git_cmd + ["rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            check=True,
            env=_sanitized_git_env(),
        )
        return result.stdout.strip() or None
    except (subprocess.CalledProcessError, OSError):
        return None


def _refresh_update_target_sha(
    git_cmd: list[str], cwd: Path, target: _UpdateTarget, *, env: dict[str, str]
) -> str | None:
    """Refresh and resolve the exact selected remote-tracking ref.

    Fork synchronization may advance local HEAD and then fail to push.  A
    receipt must describe the selected remote target, not merely whatever
    commit is currently checked out, so fetch the same explicit refspec again
    and resolve that same ref before success can be recorded.
    """
    try:
        fetched = subprocess.run(
            git_cmd + ["fetch", "--", target.remote, target.refspec],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if fetched.returncode != 0:
            return None
        resolved = subprocess.run(
            git_cmd + ["rev-parse", "--verify", target.tracking_ref],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if resolved.returncode != 0:
            return None
        candidate = resolved.stdout.strip()
        return candidate if _SHA_RE.fullmatch(candidate) else None
    except OSError:
        return None

def _validate_critical_files_syntax(root) -> tuple[bool, str | None, str | None]:
    """Compile each file in ``_UPDATE_CRITICAL_FILES`` to catch SyntaxErrors.

    These are the files imported on every ``hermes`` startup; if any of them
    has a syntax error (orphan merge-conflict markers, bad ref to a name
    that no longer exists, etc.) the CLI can't bootstrap at all. We validate
    them after a successful ``git pull`` so we can auto-roll-back instead of
    leaving the user with a bricked install.

    The compiled ``.pyc`` is written to a temp directory rather than the
    source tree's ``__pycache__/`` so we don't race with concurrent test
    workers that walk the same dir, and so we don't leave a stale pyc
    behind in production if the next interpreter run picks a different
    Python version. The pyc is discarded on function return either way —
    we only care about the compile-or-not signal.

    Returns ``(ok, failing_path, error_message)``. ``ok=True`` means every
    file parsed cleanly.
    """
    import py_compile
    import tempfile

    root = Path(root)
    with tempfile.TemporaryDirectory(prefix="hermes-syntax-check-") as tmpdir:
        for relpath in _UPDATE_CRITICAL_FILES:
            path = root / relpath
            if not path.exists():
                # Missing file is suspicious but not necessarily fatal — a future
                # refactor may legitimately remove one of these. Skip and move on.
                continue
            # Mirror the relative path under the tmpdir so two different
            # files with the same basename don't collide on the cfile name.
            cfile = Path(tmpdir) / (relpath.replace("/", "__") + "c")
            try:
                py_compile.compile(str(path), cfile=str(cfile), doraise=True)
            except py_compile.PyCompileError as exc:
                return False, str(path), str(exc)
            except OSError as exc:
                return False, str(path), f"could not read: {exc}"
    return True, None, None


# Modules imported on every agent startup. Unlike _UPDATE_CRITICAL_FILES (which
# is only parsed), these are actually *imported* so that cross-module breakage
# is caught — a file can be syntactically perfect and still fail to import
# because a name it pulls from a sibling module no longer exists.
_UPDATE_CRITICAL_MODULES = (
    "hermes_cli.main",
    "run_agent",
    "model_tools",
    "toolsets",
)


def _validate_critical_modules_import(root) -> tuple[bool, str | None, str | None]:
    """Import each module in ``_UPDATE_CRITICAL_MODULES`` in a subprocess.

    ``_validate_critical_files_syntax`` only *parses* files, so it cannot see
    cross-module breakage: a partially-updated tree where ``agent/`` is new but
    ``tools/`` is old parses perfectly and still dies at startup with
    ``ImportError: cannot import name 'TODO_INJECTION_HEADER' from
    'tools.todo_tool'``. Every file is valid Python; the *combination* is not.

    That skew is reachable on the Windows ZIP-update path, whose copy loop
    walks top-level entries in ``os.listdir`` order and replaces each one
    independently — ``agent/`` lands long before ``tools/``, so a failure or
    interruption between them leaves exactly that mismatch on disk.

    Runs in a subprocess because importing these modules into the running
    updater would pollute ``sys.modules`` and execute import-time side effects
    against the half-updated tree. Costs ~0.4s.

    Uses the project venv's interpreter when there is one (matching
    ``_venv_core_imports_healthy``): ``hermes update`` can be driven by a
    different Python than the install's own, and probing the wrong
    interpreter would test a tree the user never runs.

    Returns ``(ok, failing_module, error_message)``.
    """
    from hermes_constants import FIRST_PARTY_MODULE_ROOTS

    probe = (
        "import importlib, sys\n"
        "for name in %r:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except ModuleNotFoundError as exc:\n"
        # A missing *third-party* module means dependencies aren't installed
        # yet, not a skewed checkout. Only our own packages count as breakage.
        # The root set is injected from hermes_constants so this can't drift
        # from the hint the user is shown (they disagreed once already).
        "        missing = (getattr(exc, 'name', '') or '').split('.')[0]\n"
        "        if missing in %r or missing.startswith('hermes_'):\n"
        "            sys.stdout.write(name + '\\n' + str(exc))\n"
        "            raise SystemExit(3)\n"
        "    except ImportError as exc:\n"
        "        sys.stdout.write(name + '\\n' + str(exc))\n"
        "        raise SystemExit(3)\n"
        "    except Exception:\n"
        "        pass\n"  # non-import errors (config/env) aren't update breakage
        "raise SystemExit(0)\n"
        % (_UPDATE_CRITICAL_MODULES, tuple(sorted(FIRST_PARTY_MODULE_ROOTS)))
    )
    try:
        interpreter = sys.executable
        try:
            venv_python = venv_python_path(
                Path(root) / "venv", windows=_m()._is_windows()
            )
            if venv_python.exists():
                interpreter = str(venv_python)
        except Exception:
            pass  # fall back to the running interpreter
        result = subprocess.run(
            [interpreter, "-c", probe],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        # Can't run the probe — don't block the update on our own tooling.
        return True, None, None
    if result.returncode == 3:
        parts = (result.stdout or "").split("\n", 1)
        module = parts[0].strip() or "unknown"
        detail = parts[1].strip() if len(parts) > 1 else ""
        return False, module, detail
    return True, None, None

def _gateway_prompt(prompt_text: str, default: str = "", timeout: float = 300.0) -> str:
    """File-based IPC prompt for gateway mode.

    Writes a prompt marker file so the gateway can forward the question to the
    user, then polls for a response file.  Falls back to *default* on timeout.

    Used by ``hermes update --gateway`` so interactive prompts (stash restore,
    config migration) are forwarded to the messenger instead of being silently
    skipped.
    """
    import json as _json
    import uuid as _uuid
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    prompt_path = home / ".update_prompt.json"
    response_path = home / ".update_response"

    # Clean any stale response file
    response_path.unlink(missing_ok=True)

    payload = {
        "prompt": prompt_text,
        "default": default,
        "id": str(_uuid.uuid4()),
    }
    tmp = prompt_path.with_suffix(".tmp")
    tmp.write_text(_json.dumps(payload), encoding="utf-8")
    tmp.replace(prompt_path)

    # Poll for response
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if response_path.exists():
            try:
                answer = response_path.read_text(encoding="utf-8").strip()
                response_path.unlink(missing_ok=True)
                prompt_path.unlink(missing_ok=True)
                return answer if answer else default
            except (OSError, ValueError):
                pass
        _time.sleep(0.5)

    # Timeout — clean up and use default
    prompt_path.unlink(missing_ok=True)
    response_path.unlink(missing_ok=True)
    print(f"  (no response after {int(timeout)}s, using default: {default!r})")
    return default

def _npm_bin_exists(bin_dir: Path, name: str) -> bool:
    """True when an npm bin shim for *name* exists (POSIX or Windows)."""
    return any(
        (bin_dir / candidate).exists()
        for candidate in (name, f"{name}.cmd", f"{name}.ps1", f"{name}.exe")
    )

def _web_build_toolchain_ready(*roots: Path) -> bool:
    """True when ``tsc`` and ``vite`` shims are reachable from any of *roots*.

    Callers must pass every root the build would search; checking only one
    reports a healthy tree as broken.
    """
    bin_dirs = [
        bin_dir
        for bin_dir in (root / "node_modules" / ".bin" for root in roots)
        if bin_dir.is_dir()
    ]
    return bool(bin_dirs) and all(
        any(_npm_bin_exists(bin_dir, tool) for bin_dir in bin_dirs)
        for tool in ("tsc", "vite")
    )

def _web_toolchain_roots(web_dir: Path) -> tuple[Path, ...]:
    """Roots whose ``node_modules/.bin`` can satisfy the web build.

    ``npm run build`` prepends ``node_modules/.bin`` for the package and each
    of its ancestors, so shims hoisted to the workspace root and shims nested
    under a package that owns its lockfile (#42973) are equally valid.
    """
    return (web_dir, web_dir.parent)

def _print_curator_first_run_notice() -> None:
    """Print a short heads-up about the skill curator after `hermes update`.

    Only fires when the curator is enabled AND has no recorded run yet, which
    is exactly the window where the gateway ticker used to fire Curator
    against a fresh skill library immediately after an update. We defer the
    first real pass by one ``interval_hours``; this notice tells the user how
    to preview or disable before then. Silent on steady state.
    """
    try:
        from agent import curator
    except Exception:
        return
    try:
        if not curator.is_enabled():
            return
        state = curator.load_state()
    except Exception:
        return
    if state.get("last_run_at"):
        # Curator has run before (real or already seeded) — no notice needed.
        return
    try:
        hours = curator.get_interval_hours()
    except Exception:
        hours = 24 * 7
    days = max(1, hours // 24)
    print()
    print("ℹ Skill curator")
    print(
        f"  Background skill maintenance is enabled. First pass is deferred "
        f"~{days}d after installation; only agent-created skills are in "
        f"scope and nothing is ever auto-deleted (archive is recoverable)."
    )
    print("  Preview now:  hermes curator run --dry-run")
    print("  Pause it:     hermes curator pause")
    print(
        "  Docs:         https://hermes-agent.nousresearch.com/docs/user-guide/features/curator"
    )

def _print_fts_optimize_available_notice() -> None:
    """Advertise the opt-in v23 search-index optimization after `hermes update`.

    Only fires when the current profile's state.db is still on the legacy
    (pre-v23) inline FTS layout. Leads with the reclaimable-space figure and
    points at the exact command. Honors ``sessions.fts_optimize_notice``:
    ``advise`` (default) prints an advisory notice, ``require`` prints a
    firmer required-upgrade notice, ``off`` suppresses it. Silent for
    fresh/already-optimized installs.
    """
    mode = "advise"
    try:
        from hermes_cli.config import load_config

        mode = str(
            ((load_config() or {}).get("sessions") or {}).get(
                "fts_optimize_notice", "advise"
            )
        ).strip().lower()
    except Exception:
        mode = "advise"
    if mode == "off":
        return

    try:
        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB
    except Exception:
        return
    db_path = get_hermes_home() / "state.db"
    if not db_path.exists():
        return
    try:
        size_gb = db_path.stat().st_size / (1024 ** 3)
    except OSError:
        return
    # Skip the notice for trivially small DBs — the win isn't worth the nag.
    if size_gb < 0.5:
        return
    db = None
    interrupted = False
    try:
        db = SessionDB(db_path=db_path, read_only=True)
        # read_only opens skip schema init, so probe the layout directly.
        row = db._conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'messages_fts'"
        ).fetchone()
        # An interrupted `optimize-storage` run: the table is already the
        # v23 shape, but backfill markers / demoted trash tables remain.
        # Offer the command again — re-running resumes and finishes it.
        interrupted = bool(
            db._conn.execute(
                "SELECT 1 FROM state_meta "
                "WHERE key = 'fts_rebuild_high_water' LIMIT 1"
            ).fetchone()
            or db._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE 'fts\\_v22\\_trash\\_%' ESCAPE '\\' LIMIT 1"
            ).fetchone()
            or db._conn.execute(
                "SELECT 1 FROM state_meta WHERE key IN "
                "('fts_cjk_rebuild_high_water', 'fts_cjk_stale') LIMIT 1"
            ).fetchone()
        )
    except Exception:
        return
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    sql = (row[0] if row else "") or ""
    if not sql or ("tool_name" in sql and not interrupted):
        # v23 layout already present (fresh/optimized) — nothing to offer.
        return

    if interrupted:
        print()
        print("◆ Session database optimization incomplete")
        print(
            "  A previous `hermes sessions optimize-storage` run was "
            "interrupted. Search still works; re-run the command to resume "
            "and finish reclaiming disk:"
        )
        print("    hermes sessions optimize-storage")
        return

    # Concrete size framing — lead with the savings the user cares about.
    est_reclaim = size_gb * 0.6
    print()
    if mode == "require":
        print("◆ Session database upgrade required")
        print(
            f"  Your search index uses the OLD storage layout and should be "
            f"upgraded. The new layout typically frees ~60% of state.db "
            f"(≈{est_reclaim:.1f} GB of your current {size_gb:.1f} GB) and is "
            f"required for continued optimal operation."
        )
    else:
        print("◆ Reclaim ~60% of your session database disk")
        print(
            f"  Your search index uses the old storage layout. Upgrading it "
            f"typically frees ~60% of state.db — about {est_reclaim:.1f} GB "
            f"of your current {size_gb:.1f} GB."
        )
    print("  Run when convenient:  hermes sessions optimize-storage")
    print(
        "  It runs in the foreground with a progress bar, is safe to "
        "interrupt/re-run, and never changes your conversations."
    )

def _print_curator_recent_run_notice() -> None:
    """Print the most recent curator run summary, exactly once.

    The curator runs in the background (gateway tick + CLI session start),
    so users learn about skill consolidations only by stumbling into a
    rename. ``hermes update`` is a high-attention surface — surface the
    most recent run's rename map here, once.

    Show-once: state stamps ``last_run_summary_shown_at`` after printing.
    Subsequent ``hermes update`` invocations skip the block until a newer
    curator run lands. Silent when the curator has never run, when the
    most recent summary has already been shown, or when the summary has
    no rename information to display (no archives).
    """
    try:
        from agent import curator
    except Exception:
        return
    try:
        state = curator.load_state()
    except Exception:
        return

    last_run_at = state.get("last_run_at")
    if not last_run_at:
        return  # no curator run yet — first-run notice handles this case

    if state.get("last_run_summary_shown_at") == last_run_at:
        return  # already shown for this run

    summary = state.get("last_run_summary") or ""
    if not summary:
        return

    # Only print when there's something interesting to show — i.e. the
    # rename map block was appended (multi-line summary). A bare "auto:
    # no changes; llm: no change" doesn't warrant interrupting the
    # update flow.
    if "\n" not in summary:
        # Still stamp it shown so we don't reconsider it on every update.
        try:
            state["last_run_summary_shown_at"] = last_run_at
            curator.save_state(state)
        except Exception:
            pass
        return

    # Format the timestamp as "Xh ago" for readability.
    when = _format_time_ago(last_run_at)
    print()
    print(f"ℹ Skill curator — last run {when}")
    for line in summary.splitlines():
        print(f"  {line}")
    print(
        "  (This message shows once per curator run. "
        "View anytime: hermes curator status)"
    )

    # Stamp shown so we don't repeat on the next update.
    try:
        state["last_run_summary_shown_at"] = last_run_at
        curator.save_state(state)
    except Exception:
        pass

def _format_time_ago(iso_ts: str) -> str:
    """Render an ISO timestamp as `Xh ago` / `Xd ago` / `Xm ago`. Best effort."""
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        secs = int(delta.total_seconds())
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return "recently"

def _finish_dashboard_update_cleanup(node_failures: list[str]) -> None:
    """Refresh managed dashboards or stop stale manual ones after an update."""
    if node_failures:
        print()
        print("  ℹ Leaving running dashboard process(es) untouched because the")
        print("    Node.js dependency refresh did not complete.")
        return

    stop_result = _m()._kill_stale_dashboard_processes(restart_managed=True)
    if not stop_result.get("unrecovered"):
        return

    print()
    print(
        "⚠ A web dashboard/serve process was stopped during update and could "
        "not be auto-restarted."
    )
    print("  Re-launch it when you want the web UI back:")
    print("    hermes dashboard --port <port>")

def _atomic_replace_dir(src: str, dst: str) -> None:
    """Replace directory *dst* with *src* without leaving *dst* half-deleted.

    The naive ``rmtree(dst); copytree(src, dst)`` has a destructive window: if
    the copy fails partway (common on the Windows ZIP-update path, which only
    runs because file I/O is already flaky on that machine), the old directory
    is already gone and nothing replaced it — the install is left with a
    deleted tree (issue #49145, where ``ui-tui/`` vanished and broke the TUI).

    Now a thin single-entry alias over the two-phase helpers below, which
    generalise the same stage-then-swap discipline across every entry the ZIP
    update touches (#76104). Retained because it is part of the mechanical
    ``hermes_cli.main`` re-export surface and guards the #49145 regression.
    """
    _commit_staged_replacements([(_stage_replacement(src, dst), dst)])


def _stage_replacement(src: str, dst: str) -> str:
    """Copy *src* to a sibling staging path for *dst*; return the staging path.

    Phase 1 of the two-phase replace. Handles both directories and plain
    files. Touches nothing live, so a failure here leaves the whole install
    untouched.
    """
    staging = f"{dst}.hermes-update-staging"
    backup = f"{dst}.hermes-update-old"
    # A previous run may have died between "move dst aside" and "move staging
    # in" — leaving dst missing and the backup as the ONLY copy of that entry.
    # Restore it before clearing leftovers: deleting the backup first and then
    # failing to stage (disk exhaustion is likely right after writing a full
    # staging copy) would leave a hole in the install with nothing to roll
    # back to. The restore is a same-filesystem rename — instant and safe.
    if not os.path.exists(dst) and os.path.exists(backup):
        os.rename(backup, dst)
    for leftover in (staging, backup):
        if os.path.isdir(leftover):
            shutil.rmtree(leftover, ignore_errors=True)
        elif os.path.exists(leftover):
            os.remove(leftover)
    if os.path.isdir(src):
        shutil.copytree(src, staging)
    else:
        shutil.copy2(src, staging)
    return staging


def _discard_staged(staged) -> None:
    """Remove staging paths for entries that were never committed.

    Without this a phase-1 failure (typically disk exhaustion) orphans one
    staging copy per entry already processed — up to a full second copy of
    the tree. The user then follows the "re-run `hermes update`" advice with
    *less* free space than before and the retry fails harder than the
    original attempt.
    """
    for staging, _dst in staged:
        try:
            if os.path.isdir(staging):
                shutil.rmtree(staging, ignore_errors=True)
            elif os.path.exists(staging):
                os.remove(staging)
        except OSError as exc:  # best-effort cleanup, never fatal
            logger.warning("could not remove staging path %s: %s", staging, exc)


def _commit_staged_replacements(staged) -> None:
    """Phase 2: swap every staged entry into place, rolling back all on failure.

    ``_atomic_replace_dir`` makes each *individual* directory swap safe, but
    the ZIP update replaces ~90 top-level entries in a loop, and nothing made
    the loop atomic *as a whole*. A failure partway left some entries at the
    new version and the rest at the old one — every file valid Python, the
    combination unbootable (issue #76104; the ``ImportError`` in #76091 and
    the field report in #63717 are both this).

    This covers plain files as well as directories: the repo root holds 20
    first-party modules (``run_agent.py``, ``cli.py``, ``hermes_constants.py``
    …), so a files-only failure reproduces exactly the bug class we are
    closing. Every swap is an ``os.rename`` onto a path that was just moved
    aside — a same-filesystem rename is atomic on POSIX and NTFS alike, so a
    file swap can never leave a half-written module the way ``copy2`` onto a
    live path can.

    Splitting stage-all-then-swap-all shrinks the failure window from "the
    duration of a full tree copy" to "the duration of N renames", and makes
    the remaining window recoverable: if a swap fails we restore every entry
    already swapped, so the tree lands wholly new or wholly old.
    """
    swapped: list[tuple[str, str]] = []  # (dst, backup) in swap order; "" = absent
    try:
        for staging, dst in staged:
            backup = f"{dst}.hermes-update-old"
            if os.path.exists(dst):
                os.rename(dst, backup)
                swapped.append((dst, backup))
            else:
                swapped.append((dst, ""))
            os.rename(staging, dst)
    except OSError:
        # Undo every swap already made so the install stays self-consistent.
        for dst, backup in reversed(swapped):
            try:
                if os.path.isdir(dst):
                    shutil.rmtree(dst, ignore_errors=True)
                elif os.path.exists(dst):
                    os.remove(dst)
                if backup and os.path.exists(backup):
                    os.rename(backup, dst)
            except OSError as exc:
                # Keep restoring the rest — a silent failure here is the one
                # thing that turns a recoverable rollback into a mixed tree,
                # so say so rather than swallowing it.
                logger.warning("rollback failed for %s: %s", dst, exc)
        raise
    # All swaps succeeded — drop the backups (best-effort, never fatal).
    for _dst, backup in swapped:
        if backup and os.path.isdir(backup):
            shutil.rmtree(backup, ignore_errors=True)
        elif backup and os.path.exists(backup):
            try:
                os.remove(backup)
            except OSError:
                pass


def _print_update_completion(message: str) -> None:
    """Print an update outcome plus, when the dashboard launched this run
    with an action id, a terminal receipt line the Desktop can match after
    the dashboard restarts (see #47359 / #58764)."""
    print(message)
    action_id = os.environ.get("HERMES_ACTION_ID", "")
    if len(action_id) == 32 and all(char in "0123456789abcdef" for char in action_id):
        print(f"=== hermes-update completed {action_id} ===")


def _update_via_zip(args):
    """Update Hermes Agent by downloading a ZIP archive.

    Used on Windows when git file I/O is broken (antivirus, NTFS filter
    drivers causing 'Invalid argument' errors on file creation).
    """
    import tempfile
    import zipfile
    from urllib.request import urlretrieve

    # The ZIP fallback exists for Windows git-file-I/O breakage. It pulls a
    # static archive from GitHub, which is fine for the default "main"
    # channel but would silently ignore --branch and update from main even
    # if the user asked for something else — exactly the silent-divergence
    # bug --branch was added to prevent. Refuse to proceed in that case
    # rather than lie.
    branch = _m()._resolve_update_branch(args)
    if branch != "main":
        print(
            f"✗ --branch={branch} is not supported on the Windows ZIP-fallback "
            "update path."
        )
        print(
            "  This path runs when git file I/O is broken on the system. "
            "Either resolve the git-side breakage (typically an antivirus "
            "or NTFS filter holding files open) and rerun `hermes update "
            f"--branch {branch}`, or update against main with `hermes update`."
        )
        _m().sys.exit(1)
    zip_url = (
        f"https://github.com/NousResearch/hermes-agent/archive/refs/heads/{branch}.zip"
    )

    print("→ Downloading latest version...")
    tmp_dir = tempfile.mkdtemp(prefix="hermes-update-")
    archive_sha: str | None = None
    try:
        zip_path = os.path.join(tmp_dir, f"hermes-agent-{branch}.zip")
        urlretrieve(zip_url, zip_path)
        archive_digest = hashlib.sha256()
        with open(zip_path, "rb") as archive_file:
            for chunk in iter(lambda: archive_file.read(1024 * 1024), b""):
                archive_digest.update(chunk)
        archive_sha = archive_digest.hexdigest()

        print("→ Extracting...")
        import stat as _stat
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Validate paths to prevent zip-slip (path traversal) AND reject
            # symlink members. A GitHub source ZIP for hermes-agent itself
            # should never contain symlinks — they'd point outside the
            # extracted tree and let an attacker who can compromise the
            # update mirror plant arbitrary files via the update path.
            tmp_dir_real = os.path.realpath(tmp_dir)
            for member in zf.infolist():
                member_path = os.path.realpath(os.path.join(tmp_dir, member.filename))
                if (
                    not member_path.startswith(tmp_dir_real + os.sep)
                    and member_path != tmp_dir_real
                ):
                    raise ValueError(
                        f"Zip-slip detected: {member.filename} escapes extraction directory"
                    )
                # Unix mode lives in the upper 16 bits of external_attr;
                # mask to the file-type bits.
                mode = (member.external_attr >> 16) & 0o170000
                if _stat.S_ISLNK(mode):
                    raise ValueError(
                        f"ZIP contains unsupported symlink member: {member.filename}"
                    )
            zf.extractall(tmp_dir)

        # GitHub ZIPs extract to hermes-agent-<branch>/
        extracted = os.path.join(tmp_dir, f"hermes-agent-{branch}")
        if not os.path.isdir(extracted):
            # Try to find it
            for d in os.listdir(tmp_dir):
                candidate = os.path.join(tmp_dir, d)
                if os.path.isdir(candidate) and d != "__MACOSX":
                    extracted = candidate
                    break

        # Copy updated files over existing installation, preserving venv/node_modules/.git
        preserve = {"venv", "node_modules", ".git", ".env"}
        entries = [i for i in os.listdir(extracted) if i not in preserve]

        # Two-phase replace (#76104). Phase 1 copies every entry — directories
        # AND top-level files — to a sibling staging path without touching
        # anything live; phase 2 swaps them all in with same-filesystem
        # renames and rolls back every swap if any one fails. Replacing
        # entries one-at-a-time (the previous shape) meant an interruption
        # partway left `agent/` new and `tools/` stale — all files valid, the
        # tree unbootable. Files matter as much as directories here: the repo
        # root holds 20 first-party modules (run_agent.py, cli.py,
        # hermes_constants.py, ...).
        #
        # Staging costs one extra copy of the tree on disk. Check up front so
        # we fail with a clear message instead of running out mid-copy.
        need = sum(
            os.path.getsize(os.path.join(dirpath, f))
            for entry in entries
            for dirpath, _dirs, files in os.walk(os.path.join(extracted, entry))
            for f in files
        ) + sum(
            os.path.getsize(os.path.join(extracted, e))
            for e in entries
            if os.path.isfile(os.path.join(extracted, e))
        )
        # Only the staging copy is new — the live tree already occupies its
        # space and the swaps are renames, not copies. Ask for the staging
        # copy plus 20% headroom rather than a full 2x, which would block
        # updates that would have succeeded on exactly the space-constrained
        # machines most likely to hit this path.
        required = int(need * 1.2)
        free = shutil.disk_usage(str(_m().PROJECT_ROOT)).free
        if free < required:
            raise RuntimeError(
                f"not enough free disk space to stage the update safely "
                f"(need ~{required // (1024 * 1024)} MB, have "
                f"{free // (1024 * 1024)} MB)"
            )

        staged: list[tuple[str, str]] = []
        try:
            for item in entries:
                src = os.path.join(extracted, item)
                dst = os.path.join(str(_m().PROJECT_ROOT), item)
                staged.append((_stage_replacement(src, dst), dst))
        except Exception:
            # Nothing is live yet; drop the partial staging copies so a retry
            # starts from the same free space this attempt did.
            _discard_staged(staged)
            raise

        try:
            _commit_staged_replacements(staged)
        except Exception:
            # The rollback already restored every swapped entry, but staging
            # copies for the not-yet-swapped entries (potentially most of a
            # full tree) are still on disk. Drop them, or the retry's
            # up-front free-space check — which runs BEFORE the lazy
            # per-entry leftover cleanup — fails on litter this attempt
            # left behind: the exact "retry fails harder" failure mode
            # _discard_staged exists to prevent. Safe post-rollback: swapped
            # entries' staging paths were renamed away, and _discard_staged
            # skips paths that no longer exist.
            _discard_staged(staged)
            raise
        update_count = len(staged)

        print(f"✓ Updated {update_count} items from ZIP")

    except Exception as e:
        print(f"✗ ZIP update failed: {e}")
        # The two-phase replace either commits every entry or rolls them all
        # back, so a failure here does not leave a mixed-version tree — don't
        # scare the user toward a reinstall they don't need.
        print("  Your existing install was left in place.")
        print(
            "  Re-run `hermes update` to retry; if the agent won't start, "
            "reinstall from https://hermes-agent.nousresearch.com"
        )
        _m().sys.exit(1)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Clear stale bytecode after ZIP extraction
    removed = _m()._clear_bytecode_cache(_m().PROJECT_ROOT)
    if removed:
        print(
            f"  ✓ Cleared {removed} stale __pycache__ director{'y' if removed == 1 else 'ies'}"
        )
    _m()._record_bytecode_fingerprint()
    _m()._refresh_bootstrap_cache_scripts(branch)

    # Reinstall Python dependencies. Prefer .[all], but if one optional extra
    # breaks on this machine, keep base deps and reinstall the remaining extras
    # individually so update does not silently strip working capabilities.
    print("→ Updating Python dependencies...")
    dependencies_ok = False

    from hermes_cli.managed_uv import ensure_uv, update_managed_uv

    # Keep managed uv current — runs `uv self update` if we already have one.
    update_managed_uv()

    uv_bin = ensure_uv()

    pip_cmd = [_m().sys.executable, "-m", "pip"]
    if not uv_bin:
        uv_bin = _ensure_uv_for_termux(pip_cmd)
    if uv_bin:
        uv_env = {**os.environ, "VIRTUAL_ENV": str(_m().PROJECT_ROOT / "venv")}
        if _m()._is_termux_env(uv_env):
            uv_env.pop("PYTHONPATH", None)
            uv_env.pop("PYTHONHOME", None)
        _m()._install_python_dependencies_with_optional_fallback([uv_bin, "pip"], env=uv_env)
    else:
        # Use sys.executable to explicitly call the venv's pip module,
        # avoiding PEP 668 'externally-managed-environment' errors on Debian/Ubuntu.
        # Some environments lose pip inside the venv; bootstrap it back with
        # ensurepip before trying the editable install.
        try:
            subprocess.run(
                pip_cmd + ["--version"],
                cwd=_m().PROJECT_ROOT,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            subprocess.run(
                [_m().sys.executable, "-m", "ensurepip", "--upgrade", "--default-pip"],
                cwd=_m().PROJECT_ROOT,
                check=True,
            )
        _m()._install_python_dependencies_with_optional_fallback(pip_cmd)
    dependencies_ok = True

    # ZIP path parity: heal the active memory provider's bridge packages
    # after the dependency reinstall, same as the git-pull path (#53272,
    # #70636).
    _m()._refresh_active_memory_provider_dependencies()

    # Now that dependencies are installed, verify the tree actually imports.
    # The copy loop above replaces top-level entries one at a time in
    # os.listdir order, so an interruption between (say) `agent/` and `tools/`
    # leaves a tree whose files all parse but cannot be imported together —
    # the ImportError-on-startup class this guard exists to catch. Deliberately
    # placed *after* the dependency reinstall so a genuinely-new third-party
    # requirement isn't misreported as a partial copy. There is no SHA to roll
    # back to here, so surface it with a concrete recovery step rather than
    # reporting a successful update over a bricked install.
    syntax_ok, failing_path, syntax_error = _validate_critical_files_syntax(
        _m().PROJECT_ROOT
    )
    if not syntax_ok:
        print()
        print(f"✗ Updated checkout failed syntax validation: {failing_path}")
        if syntax_error:
            print(f"  {syntax_error}")
        _m().sys.exit(1)

    import_ok, failing_module, import_error = _validate_critical_modules_import(
        _m().PROJECT_ROOT
    )
    if not import_ok:
        print()
        print("✗ Update left the install in an unimportable state:")
        print(f"  {failing_module}: {import_error}")
        print()
        print("  This usually means the copy was interrupted partway through.")
        print("  Re-run `hermes update` to complete it.")
        _m().sys.exit(1)

    node_failures = _update_node_dependencies()
    _m()._build_web_ui(_m().PROJECT_ROOT / "web")

    # Sync skills
    try:
        from tools.skills_sync import sync_skills

        print("→ Syncing bundled skills...")
        result = sync_skills(quiet=True)
        if result["copied"]:
            print(f"  + {len(result['copied'])} new: {', '.join(result['copied'])}")
        if result.get("updated"):
            print(
                f"  ↑ {len(result['updated'])} updated: {', '.join(result['updated'])}"
            )
        if result.get("user_modified"):
            print(f"  ~ {len(result['user_modified'])} user-modified (kept)")
            print(
                "    → see them: hermes skills list-modified  "
                "(diff/reset to resume updates)"
            )
        if result.get("cleaned"):
            print(f"  − {len(result['cleaned'])} removed from manifest")
        if result.get("relocated"):
            print(
                f"  → {len(result['relocated'])} moved to new upstream paths: "
                f"{', '.join(result['relocated'])}"
            )
        if not result["copied"] and not result.get("updated"):
            print("  ✓ Skills are up to date")
    except Exception:
        pass

    # Seed the model-catalog disk cache from the freshly-unpacked checkout
    # (same rationale as the git-pull path in _cmd_update_impl). Non-fatal.
    try:
        from hermes_cli.model_catalog import seed_cache_from_checkout

        if seed_cache_from_checkout(_m().PROJECT_ROOT):
            print("  ✓ Model catalog cache refreshed from checkout")
    except Exception as e:
        logger.debug("Model catalog seed during zip update failed: %s", e)

    # ── Post-update state.db integrity guard (#68474) ─────────────────
    # Same as the git-pull path: verify state.db survived the ZIP update
    # and auto-restore from the most recent pre-update snapshot if needed.
    try:
        from hermes_cli.backup import _quick_snapshot_root, verify_sqlite_integrity

        _state_path = get_hermes_home() / "state.db"
        if _state_path.exists():
            _state_ok = verify_sqlite_integrity(
                _state_path, check_header=True, run_pragma=True
            )
            if not _state_ok.get("valid"):
                print()
                print(
                    "⚠ state.db is corrupted after update: "
                    + _state_ok.get("message", "unknown error")
                )
                _snap_root = _quick_snapshot_root(get_hermes_home())
                if _snap_root.exists():
                    _snap_dirs = sorted(
                        (d for d in _snap_root.iterdir() if d.is_dir()),
                        reverse=True,
                    )
                    for _snap_dir in _snap_dirs:
                        _snap_state = _snap_dir / "state.db"
                        if _snap_state.exists():
                            _snap_ok = verify_sqlite_integrity(
                                _snap_state, check_header=True, run_pragma=True
                            )
                            if _snap_ok.get("valid"):
                                try:
                                    import shutil as _shutil

                                    _shutil.copy2(_snap_state, _state_path)
                                    _restored_ok = verify_sqlite_integrity(
                                        _state_path,
                                        check_header=True,
                                        run_pragma=True,
                                    )
                                    if _restored_ok.get("valid"):
                                        print(
                                            "  ✓ Auto-restored from snapshot "
                                            f"{_snap_dir.name}"
                                        )
                                    else:
                                        print(
                                            "  ✗ Auto-restore FAILED — restored "
                                            "copy also failed integrity"
                                        )
                                    break
                                except OSError as _exc:
                                    print(
                                        f"  ✗ Auto-restore file copy failed: {_exc}"
                                    )
                                    break
    except Exception as exc:
        logger.debug(
            "Post-update state.db integrity check (zip path) failed: %s", exc
        )

    print()
    if node_failures:
        print(
            "⚠ Update partially complete — Node.js dependencies for "
            f"{', '.join(node_failures)} did not refresh."
        )
        print("  Code and Python deps are updated, but the dashboard/TUI may")
        print("  be in a mixed state until the Node deps are rebuilt.")
    else:
        _record_update_success(
            args,
            mode="archive",
            branch=branch,
            remote=None,
            target_ref=None,
            target_sha=None,
            resulting_head=None,
            archive_sha=archive_sha,
            health={
                "critical_syntax": syntax_ok,
                "critical_imports": import_ok,
                "dependencies": dependencies_ok,
                "node_dependencies": not bool(node_failures),
            },
        )
        _print_update_completion("✓ Update complete!")
    try:
        _print_curator_first_run_notice()
    except Exception as e:
        logger.debug("Curator first-run notice failed: %s", e)
    try:
        _print_curator_recent_run_notice()
    except Exception as e:
        logger.debug("Curator recent-run notice failed: %s", e)
    # Don't stop a working dashboard when the Node refresh failed — see the
    # git-update path for rationale (#30271).
    _finish_dashboard_update_cleanup(node_failures)

def _stash_local_changes_if_needed(git_cmd: list[str], cwd: Path) -> Optional[str]:
    status = subprocess.run(
        git_cmd + ["status", "--porcelain"],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=True,
    )
    if not status.stdout.strip():
        return None

    # If the index has unmerged entries (e.g. from an interrupted merge/rebase),
    # git stash will fail with "needs merge / could not write index".  Clear the
    # conflict state with `git reset` so the stash can proceed.  Working-tree
    # changes are preserved; only the index conflict markers are dropped.
    unmerged = subprocess.run(
        git_cmd + ["ls-files", "--unmerged"],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if unmerged.stdout.strip():
        print("→ Clearing unmerged index entries from a previous conflict...")
        subprocess.run(git_cmd + ["reset"], cwd=cwd, capture_output=True)

    from datetime import datetime, timezone

    stash_name = datetime.now(timezone.utc).strftime(
        "hermes-update-autostash-%Y%m%d-%H%M%S"
    )
    print("→ Local changes detected — stashing before update...")
    prev_stash = subprocess.run(
        git_cmd + ["rev-parse", "--verify", "refs/stash"],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    ).stdout.strip()
    push = subprocess.run(
        git_cmd + ["stash", "push", "--include-untracked", "-m", stash_name],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if push.stdout.strip():
        print(push.stdout.strip())
    stash_probe = subprocess.run(
        git_cmd + ["rev-parse", "--verify", "refs/stash"],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    stash_ref = stash_probe.stdout.strip()
    stash_created = (
        stash_probe.returncode == 0 and bool(stash_ref) and stash_ref != prev_stash
    )

    if push.returncode != 0:
        if stash_created:
            # git stash push exits non-zero when it saved everything but could
            # not delete some swept untracked files from the working tree
            # (e.g. a root-owned directory: "warning: failed to remove ...:
            # Permission denied").  The stash entry is complete — the changes
            # are safe — so this is not a failure.  Leave the undeletable
            # files in place and continue the update.
            if push.stderr.strip():
                print(push.stderr.strip())
            print(
                "  ⚠ Some untracked files could not be removed from the "
                "working tree (permission denied)."
            )
            print(
                "    They were still saved to the stash and were left in "
                "place — the update will continue."
            )
            # A partially-failed stash push also aborts its working-tree
            # cleanup for TRACKED modifications — they are saved in the stash
            # but still dirty the tree, which would break the checkout/pull
            # that follows. Safe to reset: everything is in the stash entry.
            subprocess.run(
                git_cmd + ["reset", "--hard", "HEAD"],
                cwd=cwd,
                capture_output=True,
            )
        else:
            # No stash entry was created: the changes were NOT saved.  This
            # is a real failure — bail out before the update touches HEAD.
            print("✗ Could not stash local changes — update aborted.")
            if push.stderr.strip():
                print(f"  {push.stderr.strip().splitlines()[0]}")
            print(
                "  Commit, stash, or clean up your local changes manually, "
                "then re-run `hermes update`."
            )
            raise subprocess.CalledProcessError(
                push.returncode, push.args, output=push.stdout, stderr=push.stderr
            )

    return stash_ref

def _resolve_stash_selector(
    git_cmd: list[str], cwd: Path, stash_ref: str
) -> Optional[str]:
    stash_list = subprocess.run(
        git_cmd + ["stash", "list", "--format=%gd %H"],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=True,
    )
    for line in stash_list.stdout.splitlines():
        selector, _, commit = line.partition(" ")
        if commit.strip() == stash_ref:
            return selector.strip()
    return None

def _print_stash_cleanup_guidance(
    stash_ref: str, stash_selector: Optional[str] = None
) -> None:
    print(
        "  Check `git status` first so you don't accidentally reapply the same change twice."
    )
    print("  Find the saved entry with: git stash list --format='%gd %H %s'")
    if stash_selector:
        print(f"  Remove it with: git stash drop {stash_selector}")
    else:
        print(
            f"  Look for commit {stash_ref}, then drop its selector with: git stash drop stash@{{N}}"
        )

def _stash_apply_failed_only_on_existing_untracked(stderr: str) -> bool:
    """True when a ``git stash apply`` failure is ONLY about untracked files
    that already exist in the working tree.

    This is the tail end of the permission-denied autostash class: ``git stash
    push --include-untracked`` swept undeletable files (e.g. a root-owned
    ``packaging/`` directory) into the stash but could not remove them from
    disk.  On restore, git applies all tracked changes, then refuses to
    overwrite those still-present files (``already exists, no checkout`` /
    ``could not restore untracked files from stash``) and exits non-zero even
    though nothing was lost.  Any other error line (e.g. ``would be
    overwritten by merge`` / ``Aborting``) means the tracked apply itself
    failed and this returns False.
    """
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    if not lines:
        return False
    saw_untracked_error = False
    for ln in lines:
        if "already exists, no checkout" in ln:
            saw_untracked_error = True
        elif "could not restore untracked files from stash" in ln:
            saw_untracked_error = True
        elif ln.startswith(("warning:", "hint:")):
            continue
        else:
            return False
    return saw_untracked_error

def _restore_stashed_changes(
    git_cmd: list[str],
    cwd: Path,
    stash_ref: str,
    prompt_user: bool = False,
    input_fn=None,
) -> bool:
    if prompt_user:
        print()
        print("⚠ Local changes were stashed before updating.")
        print(
            "  Restoring them may reapply local customizations onto the updated codebase."
        )
        print("  Review the result afterward if Hermes behaves unexpectedly.")
        print("Restore local changes now? [Y/n]")
        if input_fn is not None:
            response = input_fn("Restore local changes now? [Y/n]", "y")
        else:
            try:
                response = input().strip().lower()
            except (EOFError, UnicodeDecodeError):
                # Mirror the config-migration prompt's fix: don't let a
                # terminal-encoding issue or a closed stdin crash the
                # update mid-restore. Falls through to the existing
                # skip-restore path below, which already explains how to
                # restore manually from git stash.
                response = "n"
        if response not in {"", "y", "yes"}:
            print("Skipped restoring local changes.")
            print("Your changes are still preserved in git stash.")
            print(f"Restore manually with: git stash apply {stash_ref}")
            return False

    print("→ Restoring local changes...")
    restore = subprocess.run(
        git_cmd + ["stash", "apply", stash_ref],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )

    # Check for unmerged (conflicted) files — can happen even when returncode is 0
    unmerged = subprocess.run(
        git_cmd + ["diff", "--name-only", "--diff-filter=U"],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    probe_failed = unmerged.returncode != 0
    has_conflicts = bool(unmerged.stdout.strip())

    if restore.returncode != 0 and not has_conflicts and (
        _stash_apply_failed_only_on_existing_untracked(restore.stderr)
    ):
        # Permission-denied autostash tail end: the tracked changes applied
        # cleanly; the only "failure" is untracked files that never left the
        # working tree (git could not delete them at stash time, so it now
        # refuses to overwrite them). Their content was never touched —
        # nothing is lost. Treat as restored.
        print(
            "  ⚠ Some stashed untracked files already exist in the working "
            "tree and were kept as-is."
        )
    elif restore.returncode != 0 or has_conflicts or probe_failed:
        print("✗ Update pulled new code, but restoring local changes hit conflicts.")
        if restore.stdout.strip():
            print(restore.stdout.strip())
        if restore.stderr.strip():
            print(restore.stderr.strip())

        # Show which files conflicted
        conflicted_files = unmerged.stdout.strip()
        if conflicted_files:
            print("\nConflicted files:")
            for f in conflicted_files.splitlines():
                print(f"  • {f}")

        print("\nYour stashed changes are preserved — nothing is lost.")
        print(f"  Stash ref: {stash_ref}")

        # Always reset to clean state — leaving conflict markers in source
        # files makes hermes completely unrunnable (SyntaxError on import).
        # The user's changes are safe in the stash for manual recovery.
        reset = subprocess.run(
            git_cmd + ["reset", "--hard", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_sanitized_git_env(),
        )
        verify_unmerged = subprocess.run(
            git_cmd + ["diff", "--name-only", "--diff-filter=U"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_sanitized_git_env(),
        )
        cleanup_ok = bool(
            reset.returncode == 0
            and verify_unmerged.returncode == 0
            and not verify_unmerged.stdout.strip()
        )
        if cleanup_ok:
            print("Working tree reset to clean state.")
        else:
            print("✗ Could not prove the conflicted working tree was cleaned.")
            if reset.stderr.strip():
                print(f"  {reset.stderr.strip().splitlines()[0]}")
        print(f"Restore your changes later with: git stash apply {stash_ref}")
        # Don't sys.exit — the code update itself succeeded, only the stash
        # restore had conflicts.  Let cmd_update continue with pip install,
        # skill sync, and gateway restart.
        return False

    stash_selector = _resolve_stash_selector(git_cmd, cwd, stash_ref)
    if stash_selector is None:
        print(
            "⚠ Local changes were restored, but Hermes couldn't find the stash entry to drop."
        )
        print(
            "  The stash was left in place. You can remove it manually after checking the result."
        )
        _print_stash_cleanup_guidance(stash_ref)
    else:
        drop = subprocess.run(
            git_cmd + ["stash", "drop", stash_selector],
            cwd=cwd,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            env=_sanitized_git_env(),
        )
        if drop.returncode != 0:
            print(
                "⚠ Local changes were restored, but Hermes couldn't drop the saved stash entry."
            )
            if drop.stdout.strip():
                print(drop.stdout.strip())
            if drop.stderr.strip():
                print(drop.stderr.strip())
            print(
                "  The stash was left in place. You can remove it manually after checking the result."
            )
            _print_stash_cleanup_guidance(stash_ref, stash_selector)

    print("⚠ Local changes were restored on top of the updated codebase.")
    print("  Review `git diff` / `git status` if Hermes behaves unexpectedly.")
    return True

def _discard_stashed_changes(
    git_cmd: list[str],
    cwd: Path,
    stash_ref: str,
) -> bool:
    """Throw away a stash created before an update, without applying it.

    Used only on a NON-interactive update when the user has set
    ``updates.non_interactive_local_changes: discard`` — i.e. they've opted out
    of keeping local source edits on this machine. Drops the stash entry
    instead of re-applying it, so the working tree stays clean at the freshly
    pulled HEAD. Unlike ``git reset --hard`` + ``git clean -fd``, this only
    affects what was stashed (tracked changes + the untracked files we
    explicitly captured) — ignored paths like node_modules/venv/build outputs
    are never touched, since they were never stashed.

    Returns True if the stash was dropped, False on a git failure (in which
    case the stash is left in place for safety).
    """
    stash_selector = _resolve_stash_selector(git_cmd, cwd, stash_ref)
    if stash_selector is None:
        print(
            "⚠ Configured to discard local changes on non-interactive update, "
            "but Hermes couldn't find the stash entry to drop."
        )
        _print_stash_cleanup_guidance(stash_ref)
        return False

    drop = subprocess.run(
        git_cmd + ["stash", "drop", stash_selector],
        cwd=cwd,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if drop.returncode != 0:
        print(
            "⚠ Configured to discard local changes, but Hermes couldn't drop "
            "the saved stash entry."
        )
        if drop.stderr.strip():
            print(f"  {drop.stderr.strip().splitlines()[0]}")
        _print_stash_cleanup_guidance(stash_ref, stash_selector)
        return False

    print("→ Discarded local source changes (updates.non_interactive_local_changes=discard).")
    return True

OFFICIAL_REPO_URLS = {
    "https://github.com/NousResearch/hermes-agent.git",
    "git@github.com:NousResearch/hermes-agent.git",
    "https://github.com/NousResearch/hermes-agent",
    "git@github.com:NousResearch/hermes-agent",
}

OFFICIAL_REPO_URL = "https://github.com/NousResearch/hermes-agent.git"

SKIP_UPSTREAM_PROMPT_FILE = ".skip_upstream_prompt"

def _get_remote_url(
    git_cmd: list[str], cwd: Path, remote: str
) -> Optional[str]:
    """Get one configured remote URL, or ``None`` when unavailable."""
    if not _is_safe_remote_name(remote):
        return None
    try:
        result = subprocess.run(
            git_cmd + ["remote", "get-url", "--", remote],
            cwd=cwd,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            env=_sanitized_git_env(),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None

def _get_origin_url(git_cmd: list[str], cwd: Path) -> Optional[str]:
    """Backwards-compatible origin URL wrapper."""
    return _get_remote_url(git_cmd, cwd, "origin")


def _canonical_repo_url(value: str | None) -> str | None:
    """Canonicalize the two supported GitHub URL forms for exact comparison."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().rstrip("/")
    if not normalized or any(ch in normalized for ch in "\r\n\x00"):
        return None
    if normalized.casefold().endswith(".git"):
        normalized = normalized[:-4].rstrip("/")
    if re.fullmatch(
        r"https://github\.com/[^/?#]+/[^/?#]+", normalized, re.IGNORECASE
    ):
        return normalized.casefold()
    if re.fullmatch(
        r"git@github\.com:[^/?#]+/[^/?#]+", normalized, re.IGNORECASE
    ):
        return normalized.casefold()
    return None


def _is_official_repo_url(value: str | None) -> bool:
    canonical = _canonical_repo_url(value)
    return canonical is not None and canonical in {
        _canonical_repo_url(official) for official in OFFICIAL_REPO_URLS
    }


def _is_fork(origin_url: Optional[str]) -> bool:
    """Check if the origin remote points to a fork (not the official repo)."""
    if not origin_url:
        return False
    return not _is_official_repo_url(origin_url)

def _has_upstream_remote(git_cmd: list[str], cwd: Path) -> bool:
    """Return true only for an upstream remote expanded to the official repo."""
    return _is_official_repo_url(_get_remote_url(git_cmd, cwd, "upstream"))

def _add_upstream_remote(git_cmd: list[str], cwd: Path) -> bool:
    """Add the official repo as the 'upstream' remote. Returns True on success."""
    try:
        result = subprocess.run(
            git_cmd + ["remote", "add", "upstream", OFFICIAL_REPO_URL],
            cwd=cwd,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            env=_sanitized_git_env(),
        )
        return result.returncode == 0
    except Exception:
        return False

def _count_commits_between(git_cmd: list[str], cwd: Path, base: str, head: str) -> int:
    """Count commits on `head` that are not on `base`. Returns -1 on error."""
    try:
        result = subprocess.run(
            git_cmd + ["rev-list", "--count", f"{base}..{head}"],
            cwd=cwd,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return -1

def _should_skip_upstream_prompt() -> bool:
    """Check if user previously declined to add upstream."""
    from hermes_constants import get_hermes_home

    return (get_hermes_home() / SKIP_UPSTREAM_PROMPT_FILE).exists()

def _mark_skip_upstream_prompt():
    """Create marker file to skip future upstream prompts."""
    try:
        from hermes_constants import get_hermes_home

        (get_hermes_home() / SKIP_UPSTREAM_PROMPT_FILE).touch()
    except Exception:
        pass

def _sync_fork_with_upstream(
    git_cmd: list[str], cwd: Path, fork_remote: str = "origin"
) -> bool:
    """Attempt to push updated main to the selected fork remote.

    Returns True if push succeeded, False otherwise.
    """
    try:
        result = subprocess.run(
            git_cmd + ["push", fork_remote, "main", "--force-with-lease"],
            cwd=cwd,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        return result.returncode == 0
    except Exception:
        return False

def _sync_with_upstream_if_needed(
    git_cmd: list[str], cwd: Path, fork_remote: str = "origin"
) -> None:
    """Check if fork is behind upstream and sync if safe.

    This implements the fork upstream sync logic:
    - If upstream remote doesn't exist, ask user if they want to add it
    - Compare origin/main with upstream/main
    - If origin/main is strictly behind upstream/main, pull from upstream
    - Try to sync fork back to origin if possible
    """
    try:
        _assert_safe_git_configuration(git_cmd, cwd)
    except RuntimeError as exc:
        print()
        print(f"✗ Refusing upstream sync: {exc}")
        return

    upstream_url = _get_remote_url(git_cmd, cwd, "upstream")
    if upstream_url is not None and not _is_official_repo_url(upstream_url):
        print()
        print("✗ Refusing upstream sync: the 'upstream' remote is not the official Hermes repository.")
        print(f"  Expected: {OFFICIAL_REPO_URL}")
        return
    has_upstream = upstream_url is not None

    if not has_upstream:
        # Check if user previously declined
        if _should_skip_upstream_prompt():
            return

        # Ask user if they want to add upstream
        print()
        print("ℹ Your fork is not tracking the official Hermes repository.")
        print("  This means you may miss updates from NousResearch/hermes-agent.")
        print()
        try:
            response = (
                input("Add official repo as 'upstream' remote? [Y/n]: ").strip().lower()
            )
        except (EOFError, KeyboardInterrupt, UnicodeDecodeError):
            print()
            response = "n"

        if response in {"", "y", "yes"}:
            print("→ Adding upstream remote...")
            if _add_upstream_remote(git_cmd, cwd):
                # Re-read Git's expanded URL after the mutation. A local URL
                # rewrite or a concurrent config change must not let the
                # no-upstream prompt path turn into a redirected fetch.
                upstream_url = _get_remote_url(git_cmd, cwd, "upstream")
                if not _is_official_repo_url(upstream_url):
                    print(
                        "  ✗ The added upstream does not expand to the official "
                        "Hermes repository. Refusing upstream sync."
                    )
                    return
                print(f"  ✓ Added upstream: {OFFICIAL_REPO_URL}")
                has_upstream = True
            else:
                print("  ✗ Failed to add upstream remote. Skipping upstream sync.")
                return
        else:
            print(
                "  Skipped. Run 'git remote add upstream https://github.com/NousResearch/hermes-agent.git' to add later."
            )
            _mark_skip_upstream_prompt()
            return

    # Fetch upstream main from the immutable official URL into the exact ref
    # this sync reads. Even if repository configuration changes the named
    # remote after the check above, it cannot redirect this operation. A bare
    # fetch drags in thousands of auto-generated branches.
    print()
    print("→ Fetching upstream...")
    try:
        # Re-prove immediately before transport selection. This closes the
        # prompt/add window and protects direct helper callers as well as the
        # ordinary updater's earlier repository-config check.
        _assert_safe_git_configuration(git_cmd, cwd)
    except RuntimeError as exc:
        print(f"  ✗ Refusing upstream sync: {exc}")
        return
    try:
        subprocess.run(
            git_cmd
            + [
                "fetch",
                OFFICIAL_REPO_URL,
                "+refs/heads/main:refs/remotes/upstream/main",
                "--quiet",
            ],
            cwd=cwd,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        print("  ✗ Failed to fetch upstream. Skipping upstream sync.")
        return

    fork_ref = f"refs/remotes/{fork_remote}/main"
    upstream_ref = "refs/remotes/upstream/main"
    # Compare the selected fork target with upstream/main.
    origin_ahead = _count_commits_between(git_cmd, cwd, upstream_ref, fork_ref)
    upstream_ahead = _count_commits_between(
        git_cmd, cwd, fork_ref, upstream_ref
    )

    if origin_ahead < 0 or upstream_ahead < 0:
        print("  ✗ Could not compare branches. Skipping upstream sync.")
        return

    # If origin/main has commits not on upstream, don't trample
    if origin_ahead > 0:
        print()
        print(f"ℹ Your fork has {origin_ahead} commit(s) not on upstream.")
        print("  Skipping upstream sync to preserve your changes.")
        print("  If you want to merge upstream changes, run:")
        print("    git pull upstream main")
        return

    # If upstream is not ahead, fork is up to date
    if upstream_ahead == 0:
        print("  ✓ Fork is up to date with upstream")
        return

    # origin/main is strictly behind upstream/main (can fast-forward)
    print()
    print(f"→ Fork is {upstream_ahead} commit(s) behind upstream")
    print("→ Pulling from upstream...")

    try:
        subprocess.run(
            git_cmd + ["merge", "--ff-only", upstream_ref],
            cwd=cwd,
            check=True,
        )
    except subprocess.CalledProcessError:
        print(
            "  ✗ Failed to pull from upstream. You may need to resolve conflicts manually."
        )
        return

    print("  ✓ Updated from upstream")

    # Try to sync fork back to origin
    print("→ Syncing fork...")
    if _sync_fork_with_upstream(git_cmd, cwd, fork_remote=fork_remote):
        print("  ✓ Fork synced with upstream")
    else:
        print(
            "  ℹ Got updates from upstream but couldn't push to fork (no write access?)"
        )
        print("    Your local repo is updated, but your fork on GitHub may be behind.")

def _invalidate_update_cache():
    """Delete the update-check cache for ALL profiles so no banner
    reports a stale "commits behind" count after a successful update.

    The git repo is shared across profiles — when one profile runs
    ``hermes update``, every profile is now current.
    """
    homes = []
    # Default profile home (Docker-aware — uses /opt/data in Docker)
    from hermes_constants import get_default_hermes_root

    default_home = get_default_hermes_root()
    homes.append(default_home)
    # Named profiles under <root>/profiles/
    profiles_root = default_home / "profiles"
    if profiles_root.is_dir():
        for entry in profiles_root.iterdir():
            if entry.is_dir():
                homes.append(entry)
    for home in homes:
        try:
            cache_file = home / ".update_check"
            if cache_file.exists():
                cache_file.unlink()
        except Exception:
            pass

def _write_marker_file(path: Path, *, label: str) -> None:
    """Drop an update-recovery breadcrumb. Never raises."""
    if _m()._pytest_owns_live_checkout(path.parent):
        logger.debug("Skipping %s marker under pytest (live checkout)", label)
        return
    try:
        path.write_text(
            f"started={_time.time()}\npid={os.getpid()}\n", encoding="utf-8"
        )
    except OSError as exc:
        logger.debug("Could not write %s marker: %s", label, exc)

def _write_update_incomplete_marker() -> None:
    """Drop the interrupted core-install breadcrumb. Never raises."""
    _write_marker_file(_m()._update_marker_path(), label="update-incomplete")

def _write_lazy_refresh_incomplete_marker() -> None:
    """Drop the interrupted lazy-refresh breadcrumb. Never raises."""
    _write_marker_file(_m()._lazy_refresh_marker_path(), label="lazy-refresh-incomplete")

def _format_concurrent_instances_message(
    matches: list[tuple[int, str]], scripts_dir: Path
) -> str:
    """Build a human-readable explanation + remediation hint for the user."""
    shim = scripts_dir / "hermes.exe"
    lines = ["✗ Another hermes.exe is running:"]
    for pid, name in matches:
        lines.append(f"    PID {pid}  {name}")
    lines.append("")
    lines.append(f"  Updating now would fail to overwrite {shim} because")
    lines.append("  Windows blocks REPLACE on a running executable.")
    lines.append("")
    lines.append("  Close Hermes Desktop, exit any open `hermes` REPLs, and")
    lines.append("  stop the gateway (`hermes gateway stop`) before retrying.")
    lines.append("")
    if matches:
        pid_args = " ".join(f"/PID {pid}" for pid, _ in matches)
        lines.append("  If you've already closed everything and these PIDs are")
        lines.append("  stale, terminate them directly, then retry the update:")
        lines.append(f"      taskkill {pid_args} /F")
        lines.append("")
    lines.append("  Override with `hermes update --force` if you've already")
    lines.append("  confirmed those processes will not write to the venv.")
    return "\n".join(lines)

def _upgrade_pip_before_lazy_refresh(
    install_cmd_prefix: list[str],
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Upgrade pip before lazy-backend refreshes.

    Older pip (e.g. 24.0 on Python 3.11) can fail setuptools-backed source
    builds during lazy installs and leave a partially-written venv (#57828).
    Never raises.
    """
    try:
        _m()._run_package_only_install(
            install_cmd_prefix + ["install", "--upgrade", "pip"],
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        logger.debug("pip upgrade before lazy refresh failed: %s", exc)

def _refresh_active_lazy_features(
    install_cmd_prefix: list[str] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> bool:
    """Refresh lazy-installed backends after a code update.

    When pyproject.toml's ``[all]`` extra was slimmed down (May 2026), most
    optional backends moved to ``tools/lazy_deps.py`` and only install on
    first use. ``hermes update`` runs ``uv pip install -e .[all]`` which
    leaves those packages untouched — so if we bump a pin in
    :data:`LAZY_DEPS` (CVE response, transitive bug fix), users who already
    activated the backend keep the stale version forever.

    This function asks lazy_deps which features the user has previously
    activated and reinstalls them under the current pins. Features the
    user never enabled stay quiet — no churn for cold backends.

    Returns True when the venv is safe to use (refresh succeeded, or no
    active lazy backends, or post-failure import repair succeeded). Returns
    False when a failed lazy install left broken core imports that automatic
    repair could not fix (#57828).

    Never raises. A failure here must not block the rest of the update.
    """
    try:
        from tools import lazy_deps
    except Exception as exc:
        logger.debug("Lazy refresh skipped (import failed): %s", exc)
        return True

    try:
        active = lazy_deps.active_features()
    except Exception as exc:
        logger.debug("Lazy refresh skipped (active_features failed): %s", exc)
        return True

    if not active:
        return True

    print()
    print(f"→ Refreshing {len(active)} active lazy backend(s)...")

    unexpected_failure = False
    try:
        results = lazy_deps.refresh_active_features(prompt=False)
    except Exception as exc:
        # refresh_active_features is documented as never-raise, but defend
        # the update flow against future regressions.
        print(f"  ⚠ Lazy refresh failed unexpectedly: {exc}")
        results = {}
        unexpected_failure = True

    refreshed = [f for f, s in results.items() if s == "refreshed"]
    current = [f for f, s in results.items() if s == "current"]
    failed = [(f, s) for f, s in results.items() if s.startswith("failed:")]
    skipped = [(f, s) for f, s in results.items() if s.startswith("skipped:")]

    if refreshed:
        print(f"  ↑ {len(refreshed)} refreshed: {', '.join(refreshed)}")
    if current:
        print(f"  ✓ {len(current)} already current")
    if skipped:
        # Most common reason: security.allow_lazy_installs=false. Show one
        # line so the user knows why; not an error.
        names = ", ".join(f for f, _ in skipped)
        reason = skipped[0][1].split(": ", 1)[-1]
        print(f"  · {len(skipped)} skipped ({reason}): {names}")

    if not failed and not unexpected_failure:
        return True

    for feature, status in failed:
        reason = status.split(": ", 1)[-1]
        # Clip noisy pip stderr to keep update output legible.
        if len(reason) > 200:
            reason = reason[:200] + "..."
        print(f"  ⚠ {feature} failed to refresh: {reason}")

    if install_cmd_prefix is None:
        print("  ⚠ Lazy refresh failed; rerun `hermes update` once resolved.")
        return False

    # Immediate import-based recovery — metadata-only verifiers miss the case
    # where DISTRIBUTION-INFO remains but import files were wiped (#57828).
    # Unavailable probes are indeterminate, not healthy — keep the lazy marker.
    status = _m()._repair_venv_via_import_probes(install_cmd_prefix, env=env)
    if status == "repaired":
        print(
            "  Lazy backend(s) keep their previous version until refresh succeeds."
        )
        return True
    if status == "healthy":
        print(
            "  Lazy backend(s) keep their previous version; probed packages look intact."
        )
        print("  Rerun `hermes update` once the upstream issue is resolved.")
        return True
    if status == "indeterminate":
        print(
            "  ⚠ Leaving `.lazy-refresh-incomplete` until import probes can confirm health."
        )
    return False

def _refresh_active_memory_provider_dependencies() -> None:
    """Refresh pip dependencies for the configured external memory provider.

    Memory-provider bridge packages are declared in each provider's
    ``plugin.yaml`` (plus mode-dependent extras like Hindsight's
    ``hindsight-all``), NOT in Hermes' editable-install extras or
    ``LAZY_DEPS`` alone — so the core dependency reinstall above can strip
    or downgrade them (#53272 mem0ai, #70636 hindsight-embed). Re-run the
    provider's declared install for the ACTIVE provider only, after the
    core install and lazy refresh, so the last write to any shared package
    is the one the active provider needs.

    Never raises. A failure here must not block the rest of the update.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
    except Exception as exc:
        logger.debug("Memory provider refresh skipped (config load failed): %s", exc)
        return

    provider = ""
    if isinstance(cfg, dict):
        memory_cfg = cfg.get("memory")
        if isinstance(memory_cfg, dict):
            if memory_cfg.get("enabled") is False:
                return
            provider = str(memory_cfg.get("provider") or "").strip()

    # "default" / empty is the built-in file-backed store — no pip deps.
    if not provider or provider in {"default", "builtin", "none"}:
        return

    try:
        from hermes_cli.memory_setup import _install_dependencies
    except Exception as exc:
        logger.debug("Memory provider refresh skipped (import failed): %s", exc)
        return

    print()
    print(f"→ Refreshing active memory provider dependencies ({provider})...")

    try:
        _install_dependencies(provider, force=True)
    except Exception as exc:
        print(f"  ⚠ {provider} dependencies failed to refresh: {exc}")

def _is_android_python() -> bool:
    return _m().sys.platform == "android"

def _install_psutil_android_compat(
    install_cmd_prefix: list[str],
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Install psutil on Android by patching upstream platform detection.

    psutil's setup currently gates Linux sources behind
    ``sys.platform.startswith('linux')``. On Termux Python reports
    ``sys.platform == 'android'``, so setup aborts with
    "platform android is not supported" despite compiling fine when using the
    Linux source path.

    We patch only the extracted build tree used for this install attempt;
    nothing is persisted in the repository.

    Stopgap: remove this once https://github.com/giampaolo/psutil/pull/2762
    merges and ships in a release. The standalone installer script uses the
    same shared helper and should be removed together.
    """
    import tempfile
    import urllib.request
    from hermes_cli.psutil_android import PSUTIL_URL, prepare_patched_psutil_sdist

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "psutil.tar.gz"
        urllib.request.urlretrieve(PSUTIL_URL, archive)
        src_root = prepare_patched_psutil_sdist(archive, tmp_path)

        _m()._run_install_with_heartbeat(
            install_cmd_prefix + ["install", "--no-build-isolation", str(src_root)],
            env=env,
        )

def _ensure_uv_for_termux(pip_cmd: list[str]) -> str | None:
    """Best-effort uv bootstrap on Termux for faster update installs.

    The normal path (``ensure_uv()`` in managed_uv) installs the managed
    standalone uv into ``$HERMES_HOME/bin/uv``, but on Termux the official
    installer may not work (glibc vs bionic).  Prefer a uv already on PATH
    (e.g. ``pkg install uv``); only if there is none do we fall back to a
    wheel-only ``pip install uv`` so we never source-build the Rust crate.
    """
    from hermes_cli.managed_uv import resolve_uv

    existing = resolve_uv()
    if existing:
        return existing
    if not _m()._is_termux_env():
        return None
    # A Termux-packaged uv lands on PATH but not in the managed bin dir, so
    # resolve_uv() misses it. Use it before pip, which has no Android wheel and
    # would otherwise build uv from source on a low-memory device.
    system_uv = shutil.which("uv")
    if system_uv:
        return system_uv
    try:
        print("  → Termux detected: trying to install uv for faster dependency updates...")
        result = subprocess.run(
            pip_cmd + ["install", "uv", "--only-binary", ":all:"],
            cwd=_m().PROJECT_ROOT,
            check=False,
        )
        if result.returncode != 0:
            return None
    except Exception:
        pass
    # After pip install, check managed path first, then PATH
    return resolve_uv() or shutil.which("uv")

def _npm_manifest_paths() -> tuple[Path, ...]:
    """Manifests whose changes must defeat the update-skip.

    The lockfile alone is NOT a sufficient key: on a local checkout a dev
    can edit package.json (root or a workspace) without running npm — the
    lockfile is then unchanged but `hermes update` is exactly the step
    expected to sync node_modules (via the `npm install` fallback in
    _run_npm_install_deterministic).

    The workspace list is pulled from the root package.json's `workspaces`
    globs (npm's own source of truth) rather than hardcoded, so adding a
    workspace can never silently escape the skip key. Every workspace
    manifest belongs in the key — desktop included, even though the
    install only names ui-tui and web — because the single lockfile spans
    the whole workspace graph, so any manifest edit can put the lockfile
    out of sync and change what the install must do. Falls back to hashing
    just root manifests if package.json is unreadable (never skips more
    than main would have installed).
    """
    root_pkg = _m().PROJECT_ROOT / "package.json"
    paths = [_m().PROJECT_ROOT / "package-lock.json", root_pkg]
    try:
        workspaces = json.loads(root_pkg.read_text(encoding="utf-8")).get(
            "workspaces", []
        )
        if isinstance(workspaces, dict):  # legacy {"packages": [...]} form
            workspaces = workspaces.get("packages", [])
        for pattern in workspaces:
            for match in sorted(_m().PROJECT_ROOT.glob(str(pattern))):
                manifest = match / "package.json"
                if manifest.is_file():
                    paths.append(manifest)
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return tuple(paths)

def _npm_manifests_digest() -> str | None:
    """Combined sha256 over the lockfile + all workspace package.json files.

    Returns None when the lockfile is missing (never skip then).
    """
    if not (_m().PROJECT_ROOT / "package-lock.json").exists():
        return None
    h = hashlib.sha256()
    for p in _npm_manifest_paths():
        h.update(str(p.relative_to(_m().PROJECT_ROOT)).encode())
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()

def _npm_lockfile_changed(hermes_root: Path) -> bool:
    current = _npm_manifests_digest()
    if current is None:
        return True
    # Also check that node_modules exists; a matching hash with missing
    # node_modules means the cache was recorded by another checkout.
    if not (_m().PROJECT_ROOT / "node_modules").is_dir():
        return True
    # A matching lockfile hash over a tree whose web build toolchain never
    # landed must NOT skip the reinstall — otherwise every later `hermes
    # update` keeps rebuilding against a half-installed tree and serving a
    # stale dist.
    web_dir = _m().PROJECT_ROOT / "web"
    if (web_dir / "package.json").is_file() and not _web_build_toolchain_ready(
        *_web_toolchain_roots(web_dir)
    ):
        return True
    try:
        # Key the cache by PROJECT_ROOT so parallel worktrees don't collide.
        cache_key = hashlib.sha256(str(_m().PROJECT_ROOT).encode()).hexdigest()[:12]
        cache_file = hermes_root / f".npm_lock_hash_{cache_key}"
        if not cache_file.exists():
            return True
        return cache_file.read_text(encoding="utf-8").strip() != current
    except OSError:
        return True


def _node_dependencies_healthy_read_only() -> bool:
    """Prove Node dependency state without installing or rewriting caches."""
    if not (_m().PROJECT_ROOT / "package.json").is_file():
        return True
    try:
        from hermes_constants import get_default_hermes_root

        return not _npm_lockfile_changed(get_default_hermes_root())
    except Exception:
        return False

def _record_npm_lockfile_hash(hermes_root: Path) -> None:
    digest = _npm_manifests_digest()
    if digest is None:
        return
    try:
        cache_key = hashlib.sha256(str(_m().PROJECT_ROOT).encode()).hexdigest()[:12]
        cache_file = hermes_root / f".npm_lock_hash_{cache_key}"
        cache_file.write_text(digest, encoding="utf-8")
    except OSError:
        logger.debug("Could not write npm lockfile hash cache")

def _repair_node_deps_on_current_checkout(print_completion) -> None:
    """Repair Node deps on the ``commit_count == 0`` path (#77211).

    A current checkout does not imply healthy Node deps: a previous npm
    install may have failed (EBADENGINE from a node/npm mismatch, network
    timeout, interrupted install) and its error message says to "re-run
    hermes update" — but the early return never reached the Node refresh,
    so that repair advice could never work. ``_update_node_dependencies``
    self-gates on the lockfile hash, which is only recorded after a
    SUCCESSFUL npm install (and re-trips when node_modules is missing or
    the web toolchain never landed), so this is a cheap no-op on healthy
    installs and a real repair after a failed one.
    """
    node_failures = _update_node_dependencies()
    if node_failures:
        print(f"  ⚠ Node.js refresh failed for: {', '.join(node_failures)}")
        print("    Fix npm and re-run `hermes update`.")
        print_completion(
            "⚠ Checkout is current, but Node.js dependencies could not be repaired."
        )
        return
    # Pair the refresh with the web build like every other
    # _update_node_dependencies call site; it staleness-checks internally,
    # so this is a no-op when nothing changed.
    _m()._build_web_ui(_m().PROJECT_ROOT / "web")
    print_completion("✓ Already up to date!")


def _update_node_dependencies() -> list[str]:
    """Refresh Node deps for the ui-tui and web workspaces.

    Returns the list of labels whose npm install failed (empty on success),
    so the caller can treat a Node refresh failure as a partial update rather
    than silently reporting ``Update complete!`` (#30271).
    """
    if not (_m().PROJECT_ROOT / "package.json").exists():
        return []

    npm = _m()._resolve_node_runtime_npm()
    if not npm:
        # If the only npm reachable inside this WSL shell is the Windows one,
        # flag it loudly: silently skipping leaves ui-tui deps stale while the
        # rest of the update proceeds, and running it would corrupt the tree.
        from hermes_constants import is_wsl

        path_npm = shutil.which("npm")
        if is_wsl() and path_npm and _m()._is_windows_npm_path(path_npm):
            print("→ Updating Node.js dependencies...")
            print("  ⚠ Skipped: only a Windows npm is reachable from this WSL shell.")
            print("    Install Node.js inside the WSL distro (nvm, or your distro's")
            print("    package manager), then re-run `hermes update`.")
            failed = []
            if any(
                (_m().PROJECT_ROOT / workspace / "package.json").exists()
                for workspace in ("ui-tui", "web")
            ):
                failed.append("ui-tui, web workspaces")
            return failed
        return []

    from hermes_constants import get_default_hermes_root

    # This cache describes PROJECT_ROOT/node_modules, which is shared by every
    # Hermes profile using this checkout. Keep one per-checkout cache under the
    # shared Hermes root rather than rerunning npm once per named profile.
    shared_hermes_root = get_default_hermes_root()

    # Best-effort: warm npx's cache for agent-browser (#43564). Runs before
    # the lockfile-unchanged early return below since that's the common
    # `hermes update` case. Synchronous and can block ~11s on a true cold
    # cache (~0.4s once warm) — print first so that doesn't look like a hang.
    print("→ Warming npx cache for agent-browser...")
    try:
        from tools.browser_tool import warm_agent_browser_npx_cache
        warm_agent_browser_npx_cache()
    except Exception:
        pass

    if not _m()._npm_lockfile_changed(shared_hermes_root):
        logger.info("npm lockfile unchanged, skipping npm install")
        return []

    # Root package.json has no dependencies of its own (agent-browser and
    # @streamdown/math were moved out — see #43564): agent-browser resolves
    # at runtime via `npx agent-browser` (tools/browser_tool.py), and
    # @streamdown/math is a desktop-only import now declared in
    # apps/desktop/package.json. That means a plain workspace-scoped install
    # can never prune anything root-only, so we only need to name the
    # workspaces the CLI/TUI/web build actually requires. apps/desktop pulls
    # in Electron as a devDependency with a ~200MB postinstall download, so
    # it's deliberately never named here — desktop deps install on demand
    # (see _desktop_build_needed).
    print("→ Updating Node.js dependencies...")

    def _partial_update_failure(*labels: str) -> list[str]:
        print()
        print("  ⚠ Node.js dependency refresh did not complete cleanly; the")
        print("    installation may be in a mixed state (updated code, stale Node")
        print("    deps). Fix npm and re-run `hermes update`.")
        return list(labels)

    install_args = [
        "--no-fund", "--no-audit", "--prefer-offline", "--progress=false",
        "--workspace", "ui-tui", "--workspace", "web",
        # Root package.json's own devDependencies (the shared ESLint flat
        # config every workspace's eslint.config.mjs imports) are otherwise
        # pruned by this scoped install, same as agent-browser/@streamdown
        # math used to be before they moved out of root entirely (#43564).
        # Unlike those, root's devDependencies have nowhere else to live —
        # this flag still excludes apps/desktop, which is never named above.
        "--include-workspace-root",
    ]

    from hermes_constants import with_hermes_node_path

    nixos_env = with_hermes_node_path(_m()._nixos_build_env())

    # NOTE: capture_output=False here is deliberate (#18840) — optional
    # postinstall scripts print download progress, and capturing it makes a
    # long download look hung. The chatty npm-deprecation noise during
    # `hermes update` comes from the *desktop* build, not this step; that
    # one is captured to update.log.
    result = _m()._run_npm_install_deterministic(
        npm,
        _m().PROJECT_ROOT,
        extra_args=tuple(install_args),
        capture_output=False,
        env=nixos_env,
    )
    if result.returncode == 0:
        _record_npm_lockfile_hash(shared_hermes_root)
        print("  ✓ ui-tui, web workspaces installed (desktop skipped)")
        failures: list[str] = []
    else:
        print("  ⚠ npm install failed")
        stderr = (result.stderr or "").strip() if result.stderr else ""
        if stderr:
            print(f"    {stderr.splitlines()[-1]}")
        failures = _partial_update_failure("ui-tui, web workspaces")

    return failures

def _log_only_write(text: str) -> None:
    """Write ``text`` to ``~/.hermes/logs/update.log`` only, never the terminal.

    During ``hermes update`` ``sys.stdout`` is an ``_UpdateOutputStream`` that
    mirrors to both the terminal and ``update.log``. Loud, low-signal
    subprocess output (npm installs, the Electron/vite build, the cua-driver
    installer's "Next steps" wall) should be captured and tucked into the log
    so failures stay debuggable, without flooding the user's terminal. This
    reaches past the mirroring stream straight to the underlying log handle.
    """
    if not text:
        return
    stream = _m().sys.stdout
    log_file = getattr(stream, "_log", None)
    if log_file is None:
        return
    try:
        log_file.write(text if text.endswith("\n") else text + "\n")
        log_file.flush()
    except Exception:
        pass

def _run_logged_subprocess(cmd, *, cwd=None, env=None):
    """Run ``cmd`` capturing combined output into update.log (not the terminal).

    Returns the ``CompletedProcess`` (with ``stdout`` populated) so the caller
    can decide whether to surface the captured output on failure.
    """
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    _log_only_write(result.stdout or "")
    return result

@_with_sanitized_git_routing
def _cmd_update_check(branch: str = "main", *, branch_explicit: bool = False):
    """Implement ``hermes update --check``: fetch and report without installing.

    ``branch`` selects which branch the check compares against. Default is
    "main"; callers can pass another branch to ask "are there new commits
    on origin/<branch>?" without performing the update.

    ``branch_explicit`` is True iff the caller passed --branch on the CLI.
    Installs that can't honor non-default branches (e.g. Docker) surface a
    one-line notice instead of silently dropping the flag.
    """
    from hermes_cli.config import detect_install_method, recommended_update_command_for_method
    method = detect_install_method(_m().PROJECT_ROOT)
    if method == "docker":
        # Docker can't ``git fetch`` from within the container.  Surface the
        # same long-form ``docker pull`` guidance ``hermes update`` (apply
        # path) uses — telling the user to "reinstall via curl" or that
        # ".git is missing" would point them at the wrong remediation.
        from hermes_cli.config import format_docker_update_message
        print(format_docker_update_message())
        sys.exit(1)

    if method in {"nix", "nixos"}:
        print(recommended_update_command_for_method(method))
        sys.exit(1)

    git_dir = _m().PROJECT_ROOT / ".git"
    if not git_dir.exists():
        print("✗ Not a git repository — cannot check for updates.")
        sys.exit(1)

    git_cmd = _git_cmd()
    git_env = _sanitized_git_env()

    # Fetch only the branch we compare against; prefer upstream as the canonical
    # reference. A bare `git fetch <remote>` pulls every ref, and this repo has
    # thousands of auto-generated branches, so scope the fetch to <branch>.
    # Note: upstream/<branch> may not exist for non-main branches (a fork's
    # bb/gui has no upstream counterpart), so when the caller picks a
    # non-default branch we skip the upstream probe and use origin directly.
    # Installer checkouts are shallow (`git clone --depth 1`). A plain
    # `git fetch` would unshallow the repo (dragging in the whole history —
    # the exact cost the shallow clone avoided) and the rev-list count below
    # would then report a huge bogus "behind" number. Detect shallow up front:
    # fetch with --depth 1 to preserve the boundary and report presence-only.
    is_shallow = (
        subprocess.run(
            git_cmd + ["rev-parse", "--is-shallow-repository"],
            cwd=_m().PROJECT_ROOT,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            env=git_env,
        ).stdout.strip()
        == "true"
    )
    depth_args = ["--depth", "1"] if is_shallow else []
    try:
        _assert_safe_git_configuration(
            git_cmd, _m().PROJECT_ROOT, env=git_env
        )
    except RuntimeError as exc:
        print(f"✗ Unsafe Git configuration: {exc}")
        sys.exit(1)

    if branch == "main":
        # Probe locally (~6 ms) whether an 'upstream' remote exists at all
        # before spending a network fetch on it. Non-fork installs have no
        # 'upstream' remote, and the old flow burned a failed network attempt
        # (~0.3-1 s) on every --check before falling back to origin.
        has_upstream_remote = (
            subprocess.run(
                git_cmd + ["remote", "get-url", "upstream"],
                cwd=_m().PROJECT_ROOT,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                env=git_env,
            ).returncode
            == 0
        )
        fetch_result = None
        if has_upstream_remote:
            print("→ Fetching from upstream...")
            fetch_result = subprocess.run(
                git_cmd + ["fetch"] + depth_args + ["upstream", branch],
                cwd=_m().PROJECT_ROOT,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                env=git_env,
            )
        if fetch_result is not None and fetch_result.returncode == 0:
            upstream_exists = True
            compare_branch = f"upstream/{branch}"
        else:
            # No upstream remote, or the upstream fetch failed — use origin.
            print("→ Fetching from origin...")
            fetch_result = subprocess.run(
                git_cmd + ["fetch"] + depth_args + ["origin", branch],
                cwd=_m().PROJECT_ROOT,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                env=git_env,
            )
            upstream_exists = False
            compare_branch = f"origin/{branch}"
    else:
        # Non-default branch: compare against origin/<branch> directly.
        print("→ Fetching from origin...")
        fetch_result = subprocess.run(
            git_cmd + ["fetch"] + depth_args + ["origin", branch],
            cwd=_m().PROJECT_ROOT,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            env=git_env,
        )
        upstream_exists = False
        compare_branch = f"origin/{branch}"

    if fetch_result.returncode != 0:
        stderr = fetch_result.stderr.strip()
        if "Could not resolve host" in stderr or "unable to access" in stderr:
            print("✗ Network error — cannot reach the remote repository.")
        elif "Authentication failed" in stderr or "could not read Username" in stderr:
            print("✗ Authentication failed — check your git credentials or SSH key.")
        else:
            print("✗ Failed to fetch.")
            if stderr:
                print(f"  {stderr.splitlines()[0]}")
        sys.exit(1)

    # Verify the compare ref actually exists before asking rev-list about it.
    # Without this, `git rev-list HEAD..origin/<bogus> --count` exits 128 and
    # (with check=True) raises CalledProcessError, surfacing a Python
    # traceback. Friendlier to detect-and-report.
    verify_result = subprocess.run(
        git_cmd + ["rev-parse", "--verify", "--quiet", compare_branch],
        cwd=_m().PROJECT_ROOT,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        env=git_env,
    )
    if verify_result.returncode != 0:
        print(f"✗ Branch '{branch}' not found on {compare_branch.split('/', 1)[0]}.")
        sys.exit(1)

    if is_shallow:
        # No history to count across the shallow boundary. Compare tip SHAs and
        # report presence-only (mirrors the banner's _check_via_local_git).
        head_sha = subprocess.run(
            git_cmd + ["rev-parse", "HEAD"],
            cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=git_env,
        ).stdout.strip()
        target_sha = subprocess.run(
            git_cmd + ["rev-parse", compare_branch],
            cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=git_env,
        ).stdout.strip()
        if head_sha and target_sha and head_sha == target_sha:
            print("✓ Already up to date.")
        else:
            print(f"⚕ Update available (behind {compare_branch}).")
            from hermes_cli.config import recommended_update_command

            print(f"  Run '{recommended_update_command()}' to install.")
        return

    rev_result = subprocess.run(
        git_cmd + ["rev-list", f"HEAD..{compare_branch}", "--count"],
        cwd=_m().PROJECT_ROOT,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=True,
        env=git_env,
    )
    behind = int(rev_result.stdout.strip())

    if behind == 0:
        print("✓ Already up to date.")
    else:
        commits_word = "commit" if behind == 1 else "commits"
        print(f"⚕ Update available: {behind} {commits_word} behind {compare_branch}.")
        from hermes_cli.config import recommended_update_command

        print(f"  Run '{recommended_update_command()}' to install.")

def _ensure_fhs_path_guard() -> None:
    """Ensure /usr/local/bin is on PATH for RHEL-family root non-login shells.

    Mirrors the post-symlink probe added to ``scripts/install.sh`` so that
    existing FHS-layout root installs on RHEL/CentOS/Rocky/Alma 8+ get
    repaired on ``hermes update`` without requiring a reinstall.  The
    installer's assumption that ``/usr/local/bin`` is on PATH for every
    standard shell breaks on those distros in non-login interactive shells
    (su, sudo -s, tmux panes, some web terminals): /etc/bashrc doesn't
    add /usr/local/bin and /root/.bash_profile doesn't either.  Symptom:
    ``hermes`` prints ``command not found`` even though the symlink lives
    at /usr/local/bin/hermes.

    Silent no-op on: non-Linux, non-root, non-FHS installs, and any system
    where ``bash -i -c 'command -v hermes'`` already resolves.  Idempotent.
    """
    if _m().sys.platform != "linux":
        return
    try:
        if os.geteuid() != 0:  # windows-footgun: ok — Linux FHS helper, guarded by sys.platform == "linux" above + AttributeError catch
            return
    except AttributeError:
        return
    # Only act when this is actually an FHS-layout install (command link at
    # /usr/local/bin/hermes, code at /usr/local/lib/hermes-agent).
    fhs_link = Path("/usr/local/bin/hermes")
    if not fhs_link.is_symlink() and not fhs_link.exists():
        return

    # Probe a fresh non-login interactive bash the way the user will use it.
    # ``bash -i -c`` sources ~/.bashrc but NOT ~/.bash_profile or /etc/profile,
    # which is the exact scenario where RHEL root loses /usr/local/bin.
    home = os.environ.get("HOME") or "/root"
    try:
        probe = subprocess.run(
            [
                "env",
                "-i",
                f"HOME={home}",
                f"TERM={os.environ.get('TERM', 'dumb')}",
                "bash",
                "-i",
                "-c",
                "command -v hermes",
            ],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return  # no bash or probe hung — don't block update on this
    if probe.returncode == 0:
        return  # already on PATH, nothing to do

    path_line = 'export PATH="/usr/local/bin:$PATH"'
    path_comment = (
        "# Hermes Agent — ensure /usr/local/bin is on PATH " "(RHEL non-login shells)"
    )
    wrote_any = False
    for candidate in (".bashrc", ".bash_profile"):
        cfg = Path(home) / candidate
        if not cfg.is_file():
            continue
        try:
            existing = cfg.read_text(errors="replace", encoding="utf-8")
        except OSError:
            continue
        # Idempotency: skip if any uncommented PATH= line already references
        # /usr/local/bin.  Mirrors the grep pattern used by install.sh.
        already_guarded = any(
            "/usr/local/bin" in line
            and "PATH" in line
            and not line.lstrip().startswith("#")
            for line in existing.splitlines()
        )
        if already_guarded:
            continue
        try:
            with cfg.open("a", encoding="utf-8") as f:
                f.write("\n" + path_comment + "\n" + path_line + "\n")
        except OSError as e:
            print(f"  ⚠ Could not update {cfg}: {e}")
            continue
        print(f"  ✓ Added /usr/local/bin to PATH in {cfg}")
        wrote_any = True
    if wrote_any:
        print("    (reload your shell or run 'source ~/.bashrc' to pick it up)")

def _ensure_acp_launcher() -> None:
    """Self-heal: install a ``hermes-acp`` launcher next to the ``hermes`` one.

    Mirrors the launcher block in ``scripts/install.sh`` so existing installs
    gain the ACP command on ``hermes update`` without a reinstall.  ACP hosts
    (Zed, JetBrains, Buzz Desktop) spawn the agent by resolving the
    ``hermes-acp`` command name against the login-shell PATH; the console
    script of that name lives inside the install's venv, which is not on that
    PATH, so those hosts report Hermes as not installed even when it is.

    The shim simply delegates to the sibling ``hermes`` launcher with the
    ``acp`` subcommand, which makes it correct for every install layout
    (venv wrapper, FHS symlink, pipx/pip console script) without having to
    reconstruct interpreter/entrypoint paths.

    No-op on Windows (install.ps1 copies ``hermes.exe`` + ``hermes-acp.exe``
    into ``$InstallDir\bin`` and puts THAT on the user PATH — never the whole
    ``venv\Scripts`` dir, which would shadow the user's ``python`` (#83797) —
    so ``hermes-acp.exe`` already resolves) and wherever a ``hermes-acp`` is
    already present next to the ``hermes`` command.  Unwritable directories
    (e.g. ``/usr/local/bin`` as non-root) are skipped silently.  Idempotent.
    """
    if _m().sys.platform == "win32":
        return
    for bin_dir in (Path.home() / ".local" / "bin", Path("/usr/local/bin")):
        hermes_cmd = bin_dir / "hermes"
        acp_cmd = bin_dir / "hermes-acp"
        try:
            if not (hermes_cmd.is_file() or hermes_cmd.is_symlink()):
                continue
            # Already present — a console script (pip/pipx install), an
            # earlier shim, or a symlink. is_symlink() catches broken
            # symlinks that exists() would miss; never follow-and-overwrite
            # (the #21454 failure mode).
            if acp_cmd.exists() or acp_cmd.is_symlink():
                continue
            shim = (
                "#!/usr/bin/env bash\n"
                "# Hermes Agent — ACP launcher (written by `hermes update`).\n"
                "# ACP hosts (Zed, JetBrains, Buzz) resolve the agent by this\n"
                "# command name on the login-shell PATH.\n"
                f'exec "{hermes_cmd}" acp "$@"\n'
            )
            acp_cmd.write_text(shim, encoding="utf-8")
            acp_cmd.chmod(acp_cmd.stat().st_mode | 0o755)
        except OSError:
            continue
        print(f"  ✓ Installed hermes-acp launcher → {acp_cmd}")

_PRE_UPDATE_SNAPSHOT_KEEP = 1

# Per-file size cap for the pre-update quick snapshot. Anything larger is
# skipped with a warning: the snapshot exists to protect small, hard-to-
# regenerate state (pairing JSONs, cron jobs, config, auth) — not to copy a
# multi-GB state.db on every update (observed: a 24 GB state.db added ~60s
# of wall time and silently ate 24 GB of disk per update).
_PRE_UPDATE_SNAPSHOT_MAX_FILE_SIZE = 1 << 30  # 1 GiB

def _resolve_pre_update_backup_mode(args) -> str:
    """Resolve the pre-update backup mode: ``"off"``, ``"quick"``, or ``"full"``.

    CLI flags win over config; ``--no-backup`` beats ``--backup`` when both
    are set. Config accepts the mode strings plus legacy booleans:
    ``true`` → ``full`` (the old zip behavior), ``false`` → ``off``
    (an explicit opt-out now disables the quick snapshot too — previously
    it ran unconditionally, ignoring the user's setting). A missing key
    defaults to ``quick``.
    """
    if getattr(args, "no_backup", False):
        return "off"
    if getattr(args, "backup", False):
        return "full"

    try:
        from hermes_cli.config import load_config

        cfg = load_config()
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "Could not load config for pre-update backup: %s", exc
        )
        cfg = {}

    updates_cfg = cfg.get("updates", {}) if isinstance(cfg, dict) else {}
    raw = updates_cfg.get("pre_update_backup", "quick")

    if raw is True:
        return "full"
    if raw is False:
        return "off"
    mode = str(raw).strip().lower()
    if mode in ("off", "false", "none", "disabled"):
        return "off"
    if mode in ("full", "zip", "true"):
        return "full"
    if mode == "quick":
        return "quick"
    logging.getLogger(__name__).warning(
        "Unknown updates.pre_update_backup value %r — using 'quick'", raw
    )
    return "quick"

def _run_pre_update_backup(args) -> Optional[str]:
    """Run the pre-update safety backup and return the quick-snapshot id.

    Single consolidated mechanism gated on ``updates.pre_update_backup``:

    - ``off``   — nothing runs. Explicit user opt-out is honored fully.
    - ``quick`` (default) — a state snapshot of critical small files
      (pairing JSONs, cron jobs, config, auth; see ``_QUICK_STATE_FILES``)
      under ``state-snapshots/``. Files over 1 GiB are skipped with a
      warning so a bloated state.db can never stall the update
      (issues #15733, #34600 are the reason this safety net exists).
    - ``full``  — the quick snapshot PLUS a full zip of HERMES_HOME under
      ``backups/`` (restorable via ``hermes import``; the #48200 wrong-path
      wipe is the reason this level exists).

    ``--backup`` forces ``full`` for one run; ``--no-backup`` forces ``off``.
    Never raises — a backup failure should not block the update itself.

    Returns the quick-snapshot id (used by the post-update cron-jobs
    restore safety net), or ``None`` when mode is ``off`` or the snapshot
    failed.
    """
    mode = _resolve_pre_update_backup_mode(args)

    if mode == "off":
        if getattr(args, "no_backup", False):
            print("◆ Pre-update backup: skipped (--no-backup)")
            print()
        # Config-level off is silent — the user opted out; don't spam them
        # on every update.
        return None

    snapshot_id = None
    try:
        from hermes_cli.backup import (
            _quick_snapshot_root,
            create_quick_snapshot,
            verify_sqlite_integrity,
        )

        # NOTE: this function later does `from hermes_constants import
        # get_hermes_home`, which makes the name function-local — the
        # module-level import is shadowed and unbound here. Alias explicitly.
        from hermes_cli.config import get_hermes_home as _get_home

        snapshot_id = create_quick_snapshot(
            label="pre-update",
            keep=_PRE_UPDATE_SNAPSHOT_KEEP,
            max_file_size=_PRE_UPDATE_SNAPSHOT_MAX_FILE_SIZE,
        )

        # After the snapshot, verify the source state.db is still intact.
        # The snapshot was taken via _safe_copy_db (read-only SQLite backup
        # API), but a concurrent process (antivirus, force-killed gateway
        # releasing file handles, Windows filter driver) can corrupt the live
        # file at any point. A silent zeroing at this point would proceed with
        # the update and exit code 0 — exactly the #68474 symptom.
        if snapshot_id:
            _src_path = _get_home() / "state.db"
            if _src_path.exists():
                _integrity = verify_sqlite_integrity(
                    _src_path,
                    check_header=True,
                    run_pragma=True,
                    max_bytes=_PRE_UPDATE_SNAPSHOT_MAX_FILE_SIZE,
                )
                if not _integrity.get("valid"):
                    _msg = _integrity.get("message", "unknown error")
                    print(
                        f"  ⚠ state.db integrity check FAILED after snapshot: {_msg}"
                    )
                    # Check if the snapshot itself is valid.
                    _snap_root = _quick_snapshot_root(_get_home())
                    _snap_state = _snap_root / snapshot_id / "state.db"
                    if _snap_state.exists():
                        _snap_ok = verify_sqlite_integrity(
                            _snap_state, check_header=True, run_pragma=True
                        )
                        if _snap_ok.get("valid"):
                            print(
                                "  ✓ Snapshot copy is valid — continuing update."
                            )
                            print(
                                "    If state.db is lost after update it will be auto-restored."
                            )
                        else:
                            print(
                                "  ✗ Snapshot copy ALSO failed integrity — "
                                "the source was already corrupted before the backup."
                            )
                    else:
                        print(
                            "  ⚠ Snapshot does not contain state.db (was skipped or too large)."
                        )
                    print()
        if snapshot_id:
            print(f"◆ Pre-update snapshot: {snapshot_id}")
    except Exception as exc:
        # Never let a snapshot failure block an update.
        logging.getLogger(__name__).debug("Pre-update snapshot failed: %s", exc)

    if mode != "full":
        if snapshot_id:
            print()
        return snapshot_id

    try:
        from hermes_cli.backup import create_pre_update_backup
    except Exception as exc:
        print(
            f"⚠ Pre-update backup: could not load backup module ({exc}); continuing update."
        )
        print()
        return snapshot_id

    try:
        from hermes_cli.config import load_config

        _keep = (load_config() or {}).get("updates", {}).get("backup_keep", 5)
    except Exception:
        _keep = 5

    print("◆ Creating pre-update backup...")
    t0 = _time.monotonic()
    try:
        out_path = create_pre_update_backup(keep=int(_keep))
    except Exception as exc:  # defensive — helper already swallows, but just in case
        print(f"  ⚠ Backup failed: {exc}")
        print("  Continuing with update.")
        print()
        return snapshot_id

    elapsed = _time.monotonic() - t0

    if out_path is None:
        print("  ⚠ Backup skipped (no files found or write failed); continuing update.")
        print()
        return snapshot_id

    try:
        size_bytes = out_path.stat().st_size
    except OSError:
        size_bytes = 0

    # Human-readable size
    from hermes_cli.sizefmt import format_bytes

    size_str = format_bytes(size_bytes)

    # Render path using display_hermes_home so the user sees ~/.hermes/...
    try:
        from hermes_constants import get_hermes_home, display_hermes_home

        home = get_hermes_home()
        try:
            display_path = f"{display_hermes_home()}/{out_path.relative_to(home)}"
        except ValueError:
            display_path = str(out_path)
    except Exception:
        display_path = str(out_path)

    print(f"  Saved:    {display_path} ({size_str}, {elapsed:.1f}s)")
    print(f"  Restore:  hermes import {out_path}")
    print("  Disable:  set updates.pre_update_backup: quick (or off) in config.yaml")
    print()
    return snapshot_id

def _write_update_planned_stop_marker(profile_path: Path, pid: int) -> bool:
    """Write a planned-stop marker into a specific profile home."""
    try:
        from datetime import timezone

        from gateway.status import _get_process_start_time
        from utils import atomic_json_write

        record = {
            "target_pid": pid,
            "target_start_time": _get_process_start_time(pid),
            "stopper_pid": os.getpid(),
            "written_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_json_write(
            Path(profile_path) / ".gateway-planned-stop.json",
            record,
            indent=None,
            separators=(",", ":"),
        )
        return True
    except (OSError, PermissionError):
        return False

def _wait_for_windows_update_gateway_exit(
    pids: list[int], *, timeout: float
) -> set[int]:
    """Wait for the given gateway PIDs to exit, returning survivors."""
    if not pids:
        return set()

    from gateway.status import _pid_exists

    remaining = set(pids)
    deadline = _time.monotonic() + max(timeout, 0.0)
    while remaining and _time.monotonic() < deadline:
        for pid in list(remaining):
            try:
                if not _pid_exists(pid):
                    remaining.discard(pid)
            except Exception:
                remaining.discard(pid)
        if remaining:
            _time.sleep(0.25)

    survivors: set[int] = set()
    for pid in remaining:
        try:
            if _pid_exists(pid):
                survivors.add(pid)
        except Exception:
            pass
    return survivors

def _venv_core_imports_healthy() -> tuple[bool, str]:
    """Probe the project venv for the core imports the backend needs to boot.

    Runs a tiny import check inside the venv interpreter (NOT this process —
    ``hermes update`` may be driven by a different Python). Catches the
    half-updated-venv state: git checkout current but a dependency sync that
    failed or was killed partway (e.g. Windows access-denied on a loaded
    .pyd), leaving imports like ``fastapi``'s new transitive deps missing.
    Without this probe, ``hermes update`` on a current checkout prints
    "Already up to date!" and returns without ever re-syncing dependencies —
    the user's install stays broken no matter how many times they update
    (ryanc's incident, July 2026).

    Returns ``(healthy, detail)``. Never raises; unknown states report
    healthy so a probe failure can't force needless reinstalls.
    """
    venv_dir = _m().PROJECT_ROOT / "venv"
    venv_python = venv_python_path(venv_dir, windows=_m()._is_windows())
    if not venv_python.exists():
        # No venv interpreter at all. In a dev checkout that's normal (the
        # dev may run hermes from any interpreter), so report healthy to
        # avoid forcing reinstalls. But on a MANAGED install (the Windows
        # installer / desktop bootstrap stamps `.hermes-bootstrap-complete`,
        # and an interrupted update leaves `.update-incomplete`), the venv
        # IS the install — its absence means a repair got interrupted after
        # the old venv was moved aside, and "Already up to date!" would
        # gaslight the user while nothing can run.
        managed_markers = (
            _m().PROJECT_ROOT / ".hermes-bootstrap-complete",
            _m()._update_marker_path(),
        )
        if any(m.exists() for m in managed_markers):
            return False, f"venv python missing ({venv_python})"
        return True, ""

    # Core web/serve imports plus their newest transitive deps. Import (not
    # just metadata) — a package can have intact dist-info but a missing
    # module after an interrupted uninstall/install cycle.
    check = (
        "import importlib\n"
        "mods = ['fastapi', 'uvicorn', 'pydantic', 'openai', 'yaml']\n"
        "missing = []\n"
        "for m in mods:\n"
        "    try: importlib.import_module(m)\n"
        "    except Exception as e: missing.append(f'{m}: {e}')\n"
        "print('\\n'.join(missing))\n"
    )
    try:
        result = subprocess.run(
            [str(venv_python), "-c", check],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=60,
            cwd=_m().PROJECT_ROOT,
        )
    except Exception as exc:
        logger.debug("venv health probe failed to run: %s", exc)
        return True, ""

    missing = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if result.returncode != 0 and not missing:
        # Interpreter itself is broken (e.g. deleted stdlib) — that IS unhealthy.
        detail = (result.stderr or "").strip().splitlines()
        return False, detail[0] if detail else "venv python failed to run"
    if missing:
        return False, "; ".join(missing[:4])
    return True, ""

_UPDATE_SHIM_FLAG_OPTIONS = frozenset(
    {
        "--accept-hooks",
        "--cli",
        "--dev",
        "--ignore-rules",
        "--ignore-user-config",
        "--no-restore-cwd",
        "--pass-session-id",
        "--safe-mode",
        "--tui",
        "--worktree",
        "--yolo",
        "-w",
    }
)
_UPDATE_SHIM_VALUE_OPTIONS = frozenset(
    {
        "--in",
        "--model",
        "--oneshot",
        "--profile",
        "--provider",
        "--reasoning",
        "--resume",
        "--skills",
        "--toolsets",
        "--usage-file",
        "-m",
        "-p",
        "-r",
        "-s",
        "-t",
        "-z",
    }
)


def _is_current_update_shim_argv(argv: list[str]) -> bool:
    """Prove that a target-venv console shim is this update invocation.

    Only recognized top-level options may precede the operative ``update``
    token. Options with optional values (notably ``--continue``) are omitted
    because their following ``update`` text is ambiguous by construction.
    """
    index = 1
    while index < len(argv):
        token = str(argv[index])
        if token.casefold() == "update":
            return True
        if token == "--":
            # argparse accepts one top-level end-of-options marker before the
            # subcommand.  Keep this proof deliberately narrower than the
            # parser: the very next token must be the operative update
            # command, so ``-- -- update`` and ``-- value update`` cannot
            # turn arbitrary shim ancestors into self exclusions.
            return (
                index + 1 < len(argv)
                and str(argv[index + 1]).casefold() == "update"
            )
        if token in _UPDATE_SHIM_FLAG_OPTIONS:
            index += 1
            continue
        if token in _UPDATE_SHIM_VALUE_OPTIONS:
            if index + 1 >= len(argv):
                return False
            index += 2
            continue
        if token.startswith("--") and "=" in token:
            option, value = token.split("=", 1)
            if option in _UPDATE_SHIM_VALUE_OPTIONS and value:
                index += 1
                continue
        return False
    return False


def _detect_venv_python_processes(
    *,
    exclude_pids: set[int] | None = None,
    root: Path | str | None = None,
    strict: bool = False,
) -> list[tuple[int, str, str]]:
    """Find live processes running from the project venv's interpreter.

    The hermes.exe shim guard misses the biggest lock-holder class on
    Windows: the Desktop app's backend (``python.exe -m hermes_cli.main
    serve``) and anything else running straight off ``venv\\Scripts\\python
    (w).exe``. Those processes keep native ``.pyd`` extensions mapped, so a
    dependency sync mid-update dies with access-denied and strands the venv
    half-updated (ryanc's brotlicffi/_sodium.pyd incidents, July 2026).

    Killing them from here is pointless — the Desktop app supervises its
    backend and respawns it within seconds — so the caller should refuse and
    tell the user to close the app instead. Returns ``(pid, name, cmdline)``
    tuples; empty off-Windows / without psutil / when nothing matches. The
    The exact calling process is excluded (a CLI ``hermes update`` itself runs
    from the venv python). Its ancestors are not: a venv launcher ancestor can
    keep native modules mapped and must remain a hard blocker. Never raises in
    compatibility mode; ``strict=True`` turns unprovable enumeration into a
    probe failure.
    """
    if not (os.name == "nt" if root is not None else _m()._is_windows()):
        return []
    try:
        import psutil
        from hermes_mcp_update_gate import is_exact_mcp_module_argv
    except Exception as exc:
        if strict:
            raise RuntimeError(f"psutil is not available: {exc}") from exc
        return []

    target_root = Path(root) if root is not None else _m().PROJECT_ROOT
    venv_dir = target_root / "venv"
    if not venv_dir.exists() and (target_root / ".venv").exists():
        venv_dir = target_root / ".venv"
    try:
        venv_prefix = str(venv_dir.resolve()).lower().rstrip(os.sep) + os.sep
    except OSError:
        venv_prefix = str(venv_dir).lower().rstrip(os.sep) + os.sep
    managed_dir = target_root / ".hermes-runtime" / "python"
    try:
        managed_prefix = str(managed_dir.resolve()).lower().rstrip(os.sep) + os.sep
    except OSError:
        managed_prefix = str(managed_dir).lower().rstrip(os.sep) + os.sep
    try:
        root_prefix = str(target_root.resolve()).lower().rstrip(os.sep) + os.sep
    except OSError:
        root_prefix = str(target_root).lower().rstrip(os.sep) + os.sep

    skip: set[int] = set(exclude_pids or set())
    skip.add(os.getpid())
    # A native console-script launch has one load-bearing venv shim directly
    # above this updater process. Exclude only that exact current-invocation
    # ``hermes.exe ... update`` parent. Higher venv ancestors may be agents or
    # other native-module holders and remain fail-closed blockers.
    try:
        parent = psutil.Process(os.getpid()).parent()
        expected_shim = venv_bin_dir(venv_dir, windows=True) / "hermes.exe"
        parent_argv = [str(value) for value in (parent.cmdline() or [])]
        parent_exe = str(parent.exe() or "")
        same_exe = os.path.normcase(os.path.realpath(parent_exe)) == os.path.normcase(
            os.path.realpath(expected_shim)
        )
        same_argv0 = bool(parent_argv) and os.path.normcase(
            os.path.realpath(parent_argv[0])
        ) == os.path.normcase(os.path.realpath(expected_shim))
        if (
            same_exe
            and same_argv0
            and _is_current_update_shim_argv(parent_argv)
        ):
            skip.add(int(parent.pid))
    except Exception:
        pass

    matches: list[tuple[int, str, str]] = []
    try:
        proc_iter = psutil.process_iter(["pid", "exe", "name", "cmdline", "cwd"])
    except Exception as exc:
        if strict:
            raise RuntimeError(f"process enumeration failed: {exc}") from exc
        return []
    for proc in proc_iter:
        try:
            info = proc.info
        except Exception as exc:
            if strict:
                raise RuntimeError("process identity enumeration was unreadable") from exc
            continue
        pid = info.get("pid")
        exe = info.get("exe")
        if pid is None:
            if strict:
                raise RuntimeError("process enumeration returned no PID")
            continue
        try:
            numeric_pid = int(pid)
        except (TypeError, ValueError) as exc:
            if strict:
                raise RuntimeError("process enumeration returned an invalid PID") from exc
            continue
        if numeric_pid in skip:
            continue
        exe_norm = ""
        if exe:
            try:
                exe_norm = str(Path(exe).resolve()).lower()
            except (OSError, ValueError):
                exe_norm = str(exe).lower()
        cmdline_values = [str(value) for value in (info.get("cmdline") or [])]
        cmdline_raw = " ".join(cmdline_values)
        cmdline_low = cmdline_raw.lower()
        cwd_low = str(info.get("cwd") or "").lower().rstrip(os.sep) + os.sep
        name_value = str(info.get("name") or "")

        # ``psutil.process_iter(attrs=...)`` substitutes ``None`` for each
        # AccessDenied attribute instead of raising.  With no exe/argv/cwd we
        # cannot prove whether an elevated Python/Hermes process belongs to
        # this target venv. A strict readiness probe must fail closed instead
        # of silently dropping the potential native-module holder.
        if (
            strict
            and exe is None
            and info.get("cmdline") is None
            and info.get("cwd") is None
            and Path(name_value).name.casefold()
            in {"python.exe", "pythonw.exe", "hermes.exe"}
        ):
            raise RuntimeError(
                f"process {numeric_pid} ({name_value}) identity metadata was unreadable"
            )

        # Primary match: the executable itself lives under this venv
        # (venv\Scripts\python(w).exe — the desktop backend / gateway case).
        is_holder = exe_norm.startswith(venv_prefix) or exe_norm.startswith(
            managed_prefix
        )
        # Fallback: uv/base-interpreter trampolines run a python whose exe is
        # OUTSIDE the venv but which still imports from it and holds its .pyd
        # files. Catch those by what they're running: a cmdline that references
        # this venv's path, or a `-m hermes_cli.main ...` invocation tied to
        # this install (install root in the cmdline or as the working dir).
        if not is_holder and venv_prefix in cmdline_low:
            is_holder = True
        if not is_holder and "hermes_cli.main" in cmdline_low:
            if root_prefix in cmdline_low or cwd_low.startswith(root_prefix):
                is_holder = True
        if (
            not is_holder
            and cwd_low.startswith(root_prefix)
            and is_exact_mcp_module_argv(cmdline_values)
        ):
            # An AccessDenied/exe-less exact MCP launch rooted in this install
            # is still a potential native-module holder. The strict scanner
            # must report it as an unreadable hard blocker, never skip it.
            is_holder = True
        if not is_holder:
            continue
        name = name_value or (Path(exe).name if exe else "unreadable-process")
        # Return the FULL cmdline: callers match against it (the Desktop
        # preflight's pausable-gateway exemption parses for `gateway run`).
        # Truncating here cut long managed-runtime interpreter paths before
        # the `-m hermes_cli.main gateway run` argv, so autostarted gateways
        # were misreported as blockers and the update dead-ended. Truncate
        # only at display time.
        matches.append((numeric_pid, str(name), cmdline_raw))
    return matches

def _format_venv_python_holders_message(matches: list[tuple[int, str, str]]) -> str:
    """Explain which venv processes block the update and how to clear them."""
    lines = [
        "✗ Other Hermes processes are running from this install's venv:",
    ]
    try:
        from hermes_cli._scan_venv_blockers import (  # noqa: PLC0415
            _hermes_cli_command,
            _is_pausable_gateway,
        )
    except Exception:
        _hermes_cli_command = lambda _cmdline: None
        _is_pausable_gateway = lambda _cmdline: False
    for pid, name, cmdline in matches[:6]:
        hint = ""
        command = _hermes_cli_command(cmdline)
        if command in {"serve", "dashboard"}:
            hint = "  ← Hermes Desktop backend (close the desktop app)"
        elif _is_pausable_gateway(cmdline):
            hint = "  ← gateway"
        lines.append(f"  PID {pid}  {name}  {cmdline[:120]}{hint}")
    if len(matches) > 6:
        lines.append(f"  ... and {len(matches) - 6} more")
    lines.append("")
    lines.append(
        "  On Windows these keep native extension files (.pyd) locked, so the"
    )
    lines.append(
        "  dependency update would fail partway and leave a broken install."
    )
    lines.append(
        "  Close the Hermes desktop app / other Hermes terminals, then re-run:"
    )
    lines.append("    hermes update")
    lines.append("  (or use `hermes update --force-venv` to proceed anyway at your own risk)")
    return "\n".join(lines)

_GATEWAY_STOP_ROLES = frozenset(
    {
        "gateway_worker",
        "gateway_unmapped",
        "gateway_launcher",
        "gateway_leftover",
    }
)


def _canonical_process_path(value: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(value)))


def _process_path_is_within(value: str, parent: str) -> bool:
    try:
        return os.path.commonpath([value, parent]) == parent
    except (OSError, ValueError):
        return False


def _capture_gateway_stop_identity(
    pid: int,
    *,
    role: str,
    root: Path | str | None = None,
) -> _VerifiedProcessIdentity | None:
    """Capture one exact, root-bound gateway identity before any stop.

    ``None`` means the PID definitively exited. Unreadable or weakly
    classified state raises so callers refuse the update instead of turning a
    discovery-time numeric PID into tree-kill authority.
    """
    if role not in _GATEWAY_STOP_ROLES:
        raise RuntimeError(f"unsupported gateway stop role: {role}")
    try:
        import psutil  # type: ignore
        from hermes_cli._scan_venv_blockers import _is_pausable_gateway
    except Exception as exc:
        raise RuntimeError("gateway process identity support is unavailable") from exc

    numeric_pid = int(pid)
    try:
        process = psutil.Process(numeric_pid)
        created_at = float(process.create_time())
        argv = tuple(str(value) for value in (process.cmdline() or []))
        executable_raw = str(process.exe() or "")
        cwd_raw = str(process.cwd() or "")
    except psutil.NoSuchProcess:
        return None
    except Exception as exc:
        raise RuntimeError(
            f"gateway process {numeric_pid} identity is unreadable"
        ) from exc

    if not math.isfinite(created_at) or created_at <= 0:
        raise RuntimeError(f"gateway process {numeric_pid} creation time is invalid")
    if not argv or not _is_pausable_gateway(list(argv)):
        raise RuntimeError(
            f"gateway process {numeric_pid} no longer has exact gateway argv"
        )
    if not executable_raw or not os.path.isabs(executable_raw) or not cwd_raw:
        raise RuntimeError(
            f"gateway process {numeric_pid} executable/root is unreadable"
        )

    install_root = _canonical_process_path(
        Path(root) if root is not None else _m().PROJECT_ROOT
    )
    executable = _canonical_process_path(executable_raw)
    working_directory = _canonical_process_path(cwd_raw)
    if not _process_path_is_within(working_directory, install_root):
        raise RuntimeError(
            f"gateway process {numeric_pid} is not rooted in this install"
        )
    if role == "gateway_launcher":
        launcher_roots = (
            _canonical_process_path(Path(install_root) / "venv"),
            _canonical_process_path(Path(install_root) / ".venv"),
        )
        if not any(
            _process_path_is_within(executable, launcher_root)
            for launcher_root in launcher_roots
        ):
            raise RuntimeError(
                f"gateway launcher {numeric_pid} is outside this install's venv"
            )

    return _VerifiedProcessIdentity(
        pid=numeric_pid,
        created_at=created_at,
        kind="pausable_gateway",
        argv=argv,
        executable=executable,
        install_root=install_root,
        role=role,
        working_directory=working_directory,
    )


def _gateway_stop_identity_state(identity: _VerifiedProcessIdentity) -> str:
    """Return ``match``, ``exited``, or fail-closed ``refuse``."""
    if not (
        isinstance(identity, _VerifiedProcessIdentity)
        and identity.kind == "pausable_gateway"
        and identity.role in _GATEWAY_STOP_ROLES
        and identity.argv
        and identity.executable
        and identity.install_root
        and identity.working_directory
    ):
        return "refuse"
    try:
        current = _capture_gateway_stop_identity(
            identity.pid,
            role=identity.role,
            root=identity.install_root,
        )
    except Exception:
        return "refuse"
    if current is None:
        return "exited"
    return "match" if current == identity else "refuse"


def _venv_launcher_ancestors(
    workers: list[_VerifiedProcessIdentity],
) -> list[_VerifiedProcessIdentity]:
    """Return exact venv-launcher identities for mapped gateway workers.

    On Windows a gateway started through the venv shim is a **two-process
    chain**: ``venv\\Scripts\\python.exe`` (the launcher, which keeps native
    ``.pyd`` files from the venv mapped) spawns the actual interpreter from
    uv's managed CPython directory (``AppData\\Roaming\\uv\\python\\...``).
    The gateway writes its PID file from the *child*, so
    ``find_gateway_pids()`` — and therefore this module's pause set — only
    ever sees the uv-side worker.

    ``_detect_venv_python_processes()`` matches on the venv path prefix, so
    the guard downstream of the pause sees the *launcher* instead. The two
    sets are disjoint, which meant a paused gateway still tripped the
    venv-holder guard and aborted the update every time (the Desktop
    "venv-blocked: N process(es) hold the install" dead-end, where the
    reported holder is a gateway the updater believes it already stopped).

    Walking one hop up from each mapped gateway PID and keeping ancestors
    that live under the project venv closes the gap. Only the venv-side
    parent is returned — unrelated ancestors (the Scheduled Task's
    ``cmd.exe``, an operator's shell) are ignored so we never widen the
    blast radius beyond the gateway's own launcher. Unreadable or changed
    target identities raise so the caller refuses before any force-stop.
    """
    if not _m()._is_windows() or not workers:
        return []
    try:
        import psutil
    except Exception:
        return []

    # Never return ourselves or our own ancestry: a CLI ``hermes update``
    # runs from the venv python and would otherwise nominate itself.
    skip: set[int] = {os.getpid()}
    try:
        for anc in psutil.Process().parents():
            skip.add(int(anc.pid))
    except Exception:
        pass

    found: list[_VerifiedProcessIdentity] = []
    found_pids: set[int] = set()
    worker_pids = {int(identity.pid) for identity in workers}
    for worker in workers:
        if _gateway_stop_identity_state(worker) != "match":
            raise RuntimeError(
                f"gateway worker {worker.pid} changed before launcher capture"
            )
        try:
            parent = psutil.Process(int(worker.pid)).parent()
        except psutil.NoSuchProcess:
            continue
        except Exception as exc:
            raise RuntimeError(
                f"gateway worker {worker.pid} parent is unreadable"
            ) from exc
        if parent is None:
            continue
        ppid = int(parent.pid)
        if ppid in skip or ppid in found_pids or ppid in worker_pids:
            continue
        try:
            identity = _capture_gateway_stop_identity(
                ppid,
                role="gateway_launcher",
                root=worker.install_root,
            )
        except RuntimeError as exc:
            # An unrelated shell/cmd parent is not a launcher candidate. A
            # target-venv executable whose remaining identity is unreadable is
            # a hard refusal, not permission to kill it by PID.
            try:
                parent_exe = _canonical_process_path(parent.exe() or "")
                venv_roots = (
                    _canonical_process_path(Path(worker.install_root) / "venv"),
                    _canonical_process_path(Path(worker.install_root) / ".venv"),
                )
            except Exception as probe_exc:
                raise RuntimeError(
                    f"gateway launcher candidate {ppid} is unreadable"
                ) from probe_exc
            if any(
                _process_path_is_within(parent_exe, venv_root)
                for venv_root in venv_roots
            ):
                raise exc
            continue
        if identity is not None:
            found.append(identity)
            found_pids.add(ppid)
    return found


def _leftover_pausable_gateway_pids(
    matches: list[tuple[int, str, str]],
) -> list[_VerifiedProcessIdentity] | None:
    """PIDs from *matches* when every remaining venv holder is a pausable gateway.

    ``_pause_windows_gateways_for_update()`` stops every gateway its discovery
    finds, but the venv-holder guard downstream sees the process table as it
    is *now*: a gateway respawned by its supervisor (Scheduled Task, login
    watchdog) inside the pause→guard window, or one started through a spawn
    path the discovery does not map, still holds venv ``.pyd`` files and
    would dead-end the update — an abort pointed at exactly the kind of
    process the pause machinery exists to stop.

    Holders are classified with the same matcher the Desktop preflight uses
    to exempt them (``_is_pausable_gateway``), so the preflight's exemption
    and this guard's tolerance cannot drift apart — matcher drift between
    two views of the same process table is what produced the launcher/worker
    dead-end fixed above. Authorization uses only a freshly read argv sequence
    plus process creation time. Captured/flattened scan text is display data,
    never authority for a force-stop.

    Returns ``None`` when any holder is not a pausable gateway — an operator
    REPL, a stray script, or the Desktop backend has no pause machinery
    downstream, and the guard must keep refusing exactly as before.
    """
    identities: list[_VerifiedProcessIdentity] = []
    for pid, _name, _cmdline in matches:
        try:
            identity = _capture_gateway_stop_identity(
                int(pid),
                role="gateway_leftover",
                root=_m().PROJECT_ROOT,
            )
        except Exception:
            return None
        if identity is not None:
            identities.append(identity)
    return identities


def _revalidate_pausable_gateway_identity(
    identity: _VerifiedProcessIdentity,
) -> bool:
    """Re-prove the exact frozen identity immediately before force-stop."""
    return _gateway_stop_identity_state(identity) == "match"


def _orphaned_desktop_backend_pids(
    matches: list[tuple[int, str, str]],
) -> list[_VerifiedProcessIdentity] | None:
    """PIDs from *matches* when every remaining holder is an ORPHANED backend.

    The venv-holder guard refuses on the Desktop app's ``serve`` backend by
    design: while the Desktop is open, killing its backend is futile (the app
    supervises and respawns it within seconds), so the user must close the
    app. But in the GUI-updater handoff path the Desktop has *already
    exited* — by contract it tree-kills its backends and waits for the venv
    shim before spawning hermes-setup, and the update-in-progress marker
    parks any relaunched Desktop from spawning a fresh backend (#50238). A
    ``serve`` backend still holding the venv at that point is a straggler
    whose supervisor is gone: SIGTERM raced its spawn, or it belongs to a
    crashed window. Nothing will respawn it, and refusing on it dead-ends
    the update with "Hermes is still running" while the user stares at zero
    open windows (ryanc's 2026-08-09 01:59/02:17 failures).

    A holder qualifies only when BOTH hold:

    - its cmdline is a Hermes backend (``hermes_cli.main`` + ``serve`` /
      ``dashboard``), and
    - its supervising parent is demonstrably gone: the parent PID no longer
      exists, or the PID was reused (parent created *after* the child).

    Tree-aware: the scanner can return an orphaned backend AND one of its
    managed-runtime descendants (the ``.hermes-runtime`` interpreter child)
    in the same holder set. That descendant has a live parent — the orphaned
    backend itself — and isn't a ``serve`` cmdline, so per-process rules
    would refuse a set that is entirely safe to reap. Holders that sit
    inside an accepted orphan root's tree are therefore folded into that
    root (only roots are returned; ``taskkill /T`` reaps the descendants).

    Any other live-parent backend (the Desktop is still open), non-backend
    holder outside an orphan tree, or unprovable case disqualifies the whole
    set — the guard must keep refusing exactly as before. Returns ``None``
    in that case, or when psutil is unavailable (can't prove orphanhood →
    refuse). Never raises.
    """
    try:
        import psutil  # type: ignore
    except Exception:
        return None

    from hermes_cli._scan_venv_blockers import _hermes_cli_command

    def _is_backend(argv: list[str]) -> bool:
        return _hermes_cli_command(argv) in {"serve", "dashboard"}

    # Pass 1: find orphaned backend ROOTS among the holders.
    roots: list[_VerifiedProcessIdentity] = []
    remaining: list[int] = []
    for pid, _name, _cmdline in matches:
        try:
            proc = psutil.Process(int(pid))
            argv = [str(value) for value in proc.cmdline()]
            created_at = float(proc.create_time())
        except psutil.NoSuchProcess:
            # Holder exited between scan and classification — nothing to
            # reap, nothing blocking. Skip it.
            continue
        except Exception:
            return None
        if not argv or not math.isfinite(created_at) or created_at <= 0:
            return None
        if not _is_backend(argv):
            remaining.append(int(pid))
            continue
        try:
            ppid = proc.ppid()
            parent = psutil.Process(ppid) if ppid else None
            if parent is not None and parent.is_running():
                # PID-reuse check: a "parent" created after its child is a
                # recycled PID, not the real (dead) supervisor.
                if parent.create_time() <= proc.create_time():
                    # Live parent — NOT a root. But it may still be a
                    # descendant of an orphan root: the venv python.exe is
                    # a trampoline that re-execs the uv-managed interpreter
                    # with the SAME backend argv, so the worker half of the
                    # two-process chain lands here. Defer to pass 2 instead
                    # of refusing outright.
                    remaining.append(int(pid))
                    continue
        except psutil.NoSuchProcess:
            pass  # parent gone → orphan
        except Exception:
            return None
        roots.append(
            _VerifiedProcessIdentity(
                pid=int(pid), created_at=created_at, kind="orphan_backend"
            )
        )

    # Pass 2: every non-backend holder must be a descendant of an accepted
    # orphan root — then it dies with the root's tree reap. Anything else
    # (operator REPL, stray script) keeps the refusal.
    root_set = {identity.pid for identity in roots}
    for pid in remaining:
        if not root_set:
            return None
        try:
            ancestors = {int(a.pid) for a in psutil.Process(pid).parents()}
        except psutil.NoSuchProcess:
            continue  # exited already
        except Exception:
            return None
        if not (ancestors & root_set):
            return None
    return roots


def _revalidate_orphan_backend_identity(
    identity: _VerifiedProcessIdentity,
) -> bool:
    """Re-prove one orphan backend and its creation time immediately before kill."""
    if identity.kind != "orphan_backend":
        return False
    try:
        import psutil  # type: ignore
        from hermes_cli._scan_venv_blockers import _hermes_cli_command

        process = psutil.Process(int(identity.pid))
        created_at = float(process.create_time())
        argv = [str(value) for value in process.cmdline()]
        if (
            not math.isfinite(created_at)
            or abs(created_at - identity.created_at) > 0.001
            or _hermes_cli_command(argv) not in {"serve", "dashboard"}
        ):
            return False
        ppid = int(process.ppid())
        if not ppid:
            return True
        try:
            parent = psutil.Process(ppid)
            if not parent.is_running():
                return True
            return float(parent.create_time()) > created_at
        except psutil.NoSuchProcess:
            return True
    except Exception:
        return False


def _stop_process_trees(identities: list[_VerifiedProcessIdentity]) -> None:
    """Force-stop each PID with its full child tree (Windows).

    ``taskkill /T /F`` mirrors the Desktop's ``forceKillProcessTree`` and
    install.ps1's venv sweep: stopping only the parent can leave a managed
    ``.hermes-runtime`` interpreter child alive and holding the install open
    (#70026). Best effort; never raises.
    """
    for identity in identities:
        if not isinstance(identity, _VerifiedProcessIdentity):
            logger.warning("Refusing bare-PID process-tree stop without identity proof")
            continue
        if not _revalidate_orphan_backend_identity(identity):
            logger.warning(
                "Refusing process-tree stop for PID %s after identity changed",
                identity.pid,
            )
            continue
        try:
            subprocess.run(
                ["taskkill", "/PID", str(int(identity.pid)), "/T", "/F"],
                check=False,
                capture_output=True,
            )
        except Exception as exc:
            logger.debug("Could not stop process tree %s: %s", identity.pid, exc)


def _pause_windows_gateways_for_update(
    *,
    require_structured_resume: bool = False,
    before_stop: Callable[[dict], object] | None = None,
) -> dict | None:
    """Stop running Windows gateways before mutating the checkout or venv.

    Windows scheduled/startup gateways run through pythonw.exe, so the generic
    hermes.exe concurrent-instance guard does not see them. They still import
    from the checkout and can keep files locked while ``git`` or ``uv`` updates
    the install. Stop only PIDs that the gateway discovery code identifies.
    """
    if not _m()._is_windows():
        return None

    try:
        from gateway.status import terminate_pid
        from hermes_cli.gateway import (
            _get_restart_drain_timeout,
            find_gateway_pids,
            find_profile_gateway_processes,
        )
    except Exception as exc:
        if require_structured_resume:
            raise RuntimeError(
                "could not load verified gateway discovery for deferred resume"
            ) from exc
        logger.debug("Could not prepare Windows gateway pause for update: %s", exc)
        return None

    try:
        running_pids = list(dict.fromkeys(find_gateway_pids(all_profiles=True)))
    except Exception as exc:
        if require_structured_resume:
            raise RuntimeError(
                "could not discover the gateway fleet for deferred resume"
            ) from exc
        logger.debug("Could not discover Windows gateway PIDs before update: %s", exc)
        return None
    if not running_pids:
        # No gateway is running right now, but the user may have installed an
        # autostart entry (Scheduled Task or Startup-folder login item) — that
        # is an explicit "I want a gateway" signal. A gateway that died between
        # updates (e.g. the spawning terminal/TUI closed, taking its child with
        # it) would otherwise never come back: the autostart entry only fires on
        # the next login, and the update flow's resume path only relaunched
        # gateways that were running when the update began. Cold-start one after
        # the update so an installed gateway is actually up post-update. Users
        # who run gateway-less (no autostart entry) get nothing forced on them.
        try:
            from hermes_cli import gateway_windows

            installed = bool(gateway_windows.is_installed())
        except Exception as exc:
            if require_structured_resume:
                raise RuntimeError(
                    "could not prove gateway autostart state for deferred resume"
                ) from exc
            logger.debug(
                "Could not check Windows gateway autostart state before update: %s",
                exc,
            )
            installed = False
        if installed or require_structured_resume:
            token = {
                "resume_needed": bool(installed),
                "profiles": {},
                "profile_identities": {},
                "unmapped_pids": [],
                "unmapped": [],
                "cold_start_if_installed": bool(installed),
            }
            if before_stop is not None:
                # Even an explicit empty fleet needs a durable authenticated
                # recovery plan so a failed update can return and clear the
                # continuously-held lease through the hidden resume seam.
                before_stop(token)
            return token
        return None

    profile_processes = {}
    try:
        profile_processes = {
            proc.pid: proc for proc in find_profile_gateway_processes()
        }
    except Exception as exc:
        if require_structured_resume:
            raise RuntimeError(
                "could not map the gateway fleet to verified profiles"
            ) from exc
        logger.debug("Could not map Windows gateway PIDs to profiles: %s", exc)

    # Convert discovery PIDs into immutable, root-bound process identities
    # before the first wait or stop. A PID that exits during the drain can be
    # reused immediately; the numeric value and a post-wait argv read are not
    # authority to tree-kill the replacement.
    verified_by_pid: dict[int, _VerifiedProcessIdentity] = {}
    for pid in running_pids:
        proc = profile_processes.get(pid)
        role = "gateway_worker" if proc is not None else "gateway_unmapped"
        try:
            identity = _capture_gateway_stop_identity(
                int(pid), role=role, root=_m().PROJECT_ROOT
            )
        except RuntimeError:
            raise
        if identity is None:
            if require_structured_resume:
                raise RuntimeError(
                    f"gateway process {pid} exited before its fleet identity was frozen"
                )
            continue
        verified_by_pid[int(pid)] = identity

    running_pids = list(verified_by_pid)
    profiles: dict[str, int] = {}
    profile_identities: dict[str, dict[str, float | int]] = {}
    mapped_pids: list[int] = []
    for pid in running_pids:
        proc = profile_processes.get(pid)
        if proc is None:
            continue
        identity = verified_by_pid[pid]
        profiles[str(proc.profile)] = int(pid)
        mapped_pids.append(int(pid))
        profile_identities[str(proc.profile)] = {
            "pid": int(pid),
            "created_at": identity.created_at,
        }

    unmapped_pids = [pid for pid in running_pids if pid not in profile_processes]
    if require_structured_resume and unmapped_pids:
        raise RuntimeError(
            "deferred gateway resume requires every running gateway to map "
            "to a verified Hermes profile"
        )

    resume_token = {
        "resume_needed": True,
        "profiles": profiles,
        "profile_identities": profile_identities,
        "unmapped_pids": unmapped_pids,
        "unmapped": [],
        "cold_start_if_installed": False,
    }
    if before_stop is not None:
        # The authenticated recovery plan must be durable before the first
        # gateway stop. A publication failure therefore leaves the entire
        # prior fleet untouched and aborts before source mutation.
        before_stop(resume_token)

    for pid in mapped_pids:
        proc = profile_processes[pid]
        _write_update_planned_stop_marker(Path(proc.path), int(pid))

    # Resolve each mapped worker's venv-side launcher BEFORE draining: the
    # drain stops tracking a PID exactly when it dies, so a gracefully
    # drained worker is gone by the time the wait returns — and a dead pid's
    # parent cannot be recovered (psutil raises NoSuchProcess). The snapshot
    # is stopped after the drain alongside the survivors.
    #
    # Why launchers matter: the drain targets the PID that wrote the PID
    # file (the uv-side worker). On Windows that worker's parent is usually
    # the venv-side ``python.exe`` launcher, which keeps venv ``.pyd`` files
    # mapped and is what ``_detect_venv_python_processes()`` reports
    # downstream. Left alive, it trips the venv-holder guard and aborts the
    # update even though the gateway itself is stopped.
    mapped_identities = [verified_by_pid[pid] for pid in mapped_pids]
    launcher_identities = _m()._venv_launcher_ancestors(mapped_identities)

    print("→ Stopping Windows gateway process(es) before updating Hermes...")
    try:
        drain_timeout = max(float(_get_restart_drain_timeout()), 1.0)
    except Exception:
        drain_timeout = 10.0
    survivors = _m()._wait_for_windows_update_gateway_exit(
        mapped_pids,
        timeout=drain_timeout,
    )
    # Resume argv comes from the same pre-wait immutable identity that
    # authorizes the stop. Never re-read argv after a drain interval: at that
    # point the numeric PID may belong to an unrelated process.
    unmapped = [
        {"pid": int(pid), "argv": list(verified_by_pid[pid].argv)}
        for pid in unmapped_pids
    ]
    resume_token["unmapped"] = unmapped

    # Select candidates by PID, but authorize each stop only with its frozen
    # identity. Re-read PID/create-time/argv/exe/cwd/root immediately before
    # the tree kill. A definite exit is harmless; every unreadable or changed
    # identity aborts the update rather than widening the kill.
    candidates = {
        identity.pid: identity
        for identity in launcher_identities
    }
    for pid in set(survivors).union(unmapped_pids):
        identity = verified_by_pid.get(int(pid))
        if identity is None:
            raise RuntimeError(
                f"gateway process {pid} has no frozen stop identity"
            )
        candidates[int(pid)] = identity

    force_killed: list[int] = []
    for pid in sorted(candidates):
        identity = candidates[pid]
        state = _gateway_stop_identity_state(identity)
        if state == "exited":
            continue
        if state != "match":
            raise RuntimeError(
                f"gateway process {pid} changed before force-stop; refusing update"
            )
        try:
            terminate_pid(int(pid), force=True)
            force_killed.append(int(pid))
        except ProcessLookupError:
            continue
        except (PermissionError, OSError) as exc:
            raise RuntimeError(
                f"gateway process {pid} could not be safely stopped"
            ) from exc

    if profiles:
        print(f"  ✓ Paused gateway profile(s): {', '.join(sorted(profiles))}")
    if force_killed:
        print(f"  → Force-stopped {len(force_killed)} gateway process(es)")

    if unmapped_pids:
        print(
            f"  → Stopped {len(unmapped_pids)} gateway process(es) without profile mapping"
        )

    return resume_token

def _cold_start_windows_gateway_after_update() -> None:
    """Start a fresh detached gateway after update when one is installed but down.

    Invoked from ``_resume_windows_gateways_after_update`` for the
    ``cold_start_if_installed`` case: no gateway was running when the update
    began, but an autostart entry (Scheduled Task / Startup-folder login item)
    is installed, signalling the user wants a gateway. Unlike the relaunch
    paths — which watch an old PID and respawn once it exits — this is a direct
    fresh spawn via the same hidden-console + breakaway path that
    ``hermes gateway start`` uses (``gateway_windows._spawn_detached``).

    Best-effort and idempotent: re-checks that nothing is running first so a
    concurrent start (e.g. the autostart entry firing) can't produce a
    duplicate gateway.
    """
    if not _m()._is_windows():
        return
    try:
        from hermes_cli import gateway_windows
        from hermes_cli.gateway import find_gateway_pids
    except Exception as exc:
        logger.debug("Could not load Windows gateway cold-start helpers: %s", exc)
        return

    # Re-check liveness right before spawning — between pause and resume the
    # autostart entry may have already brought a gateway up, or a leftover
    # process may have re-registered. Don't double-start.
    try:
        if list(find_gateway_pids(all_profiles=True)):
            return
    except Exception as exc:
        logger.debug("Could not re-check gateway liveness before cold-start: %s", exc)
        return

    try:
        pid = gateway_windows._spawn_detached()
    except Exception as exc:
        logger.debug("Could not cold-start Windows gateway after update: %s", exc)
        return

    if pid:
        print()
        print(f"  ✓ Starting Windows gateway after update (PID {pid})")

def _for_each_systemd_gateway_unit(
    list_units_stdout: str,
    *,
    process_unit,
    on_unit_timeout,
) -> None:
    """Process each ``hermes-gateway*.service`` from ``systemctl list-units``.

    ``subprocess.TimeoutExpired`` raised by ``process_unit`` is isolated to
    that unit via ``on_unit_timeout`` so one wedged systemctl call cannot
    abort the rest of the fleet (#68523).
    """
    for line in (list_units_stdout or "").strip().splitlines():
        parts = line.split()
        if not parts:
            continue
        unit = parts[0]
        if not unit.endswith(".service"):
            continue
        # list-units is already pattern-filtered, but keep the name gate so a
        # stray non-gateway line cannot enter the restart path.
        if not unit.startswith("hermes-gateway"):
            continue
        svc_name = unit.removesuffix(".service")
        try:
            process_unit(svc_name)
        except subprocess.TimeoutExpired as exc:
            on_unit_timeout(svc_name, exc)

def _warn_incomplete_gateway_fleet_restart(failed_units: list) -> None:
    """Print an explicit incomplete-update warning for unrestarted units."""
    if not failed_units:
        return
    # Preserve discovery order while de-duplicating.
    seen = set()
    ordered = []
    for name in failed_units:
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    print()
    print("⚠ Update incomplete — some gateway units were not restarted:")
    for name in ordered:
        print(f"    - {name}")
    print("  Skipped units may still be running pre-update code (mixed")
    print("  sys.modules). Restart them manually, then verify:")
    print("    hermes gateway status")
    print("    systemctl --user restart <unit>   # user-scope")
    print("    sudo systemctl restart <unit>     # system-scope")

def _refresh_windows_gateway_launchers() -> None:
    """Regenerate installed Windows gateway launcher scripts after update.

    The Scheduled Task / Startup-folder launchers (``gateway.cmd`` +
    ``gateway.vbs``) are persistence artifacts written once at install time —
    ``hermes update`` never touched them, so installs created before the
    hidden-console rework (aa2ae36c3f) kept launching the gateway through
    ``pythonw.exe`` forever: every descendant spawn flashed a conhost
    (#54220/#56747) and, since #70344, the console-less gateway died at
    startup with ``RuntimeError: sys.stderr is None`` (#71671).

    The task's /TR points at a stable script path, so rewriting the files in
    place retargets the task without any schtasks call (no UAC needed).
    ``_write_task_script`` is idempotent and renders from current code, so
    this is a no-op for modern installs. Best-effort: a failed refresh must
    never fail the update.
    """
    if not _m()._is_windows():
        return
    try:
        from hermes_cli import gateway_windows

        if not gateway_windows.is_installed():
            return
        gateway_windows._write_task_script()
        print("  ✓ Refreshed Windows gateway launcher scripts")
    except Exception as exc:
        logger.debug("Could not refresh Windows gateway launchers after update: %s", exc)

def _refresh_bootstrap_cache_scripts(branch: str = "main") -> None:
    """Sync the installer's bootstrap-cache scripts from the fresh checkout.

    The Desktop GUI updater (``hermes-setup.exe``) executes
    ``$HERMES_HOME/bootstrap-cache/install-<ref>.ps1`` (or ``.sh``) for its
    repair/bootstrap stages. Installer binaries built before the #67193
    cache-refresh fix (June 2026 and earlier) NEVER re-download a cached
    branch-ref script — ``install-main.ps1`` cached at install time is
    reused forever, executing months-stale code with long-fixed bugs (the
    2026-08-09 incident: a June 4 cached script's venv stage lacked the
    #81327 process-tree sweep and died on ``Access denied``). The binary
    has no self-update path, so the poisoned cache outlives every
    ``hermes update``.

    Overwriting the cached script for *branch* with the freshly pulled
    ``scripts/install.ps1`` / ``scripts/install.sh`` on every update turns
    the stale binary's unconditional reuse into a feature: it "reuses" a
    file this function keeps permanently current. Post-#67193 installers
    re-download on each run anyway, so for them this is a harmless
    pre-seed of the same bytes.

    Scope guards, mirroring ``install_script.rs``:

    - Only the cache key for the update-target *branch* is rewritten
      (``sanitize_ref``: non ``[A-Za-z0-9._-]`` chars become ``_``, so
      ``bb/gui`` → ``install-bb_gui.ps1``). Sibling mutable refs cache
      DIFFERENT branches' scripts — updating main must not clobber
      ``install-bb_gui.ps1`` with main's script.
    - Commit-SHA pins are immutable by design and never touched. The
      installer's ``is_valid_commit()`` accepts **7–40** hex chars, so an
      abbreviated pin like ``install-4ce1994.ps1`` is just as immutable as
      a full 40-hex one; the sanitized *branch* is additionally required
      to not itself look like a commit pin (defense in depth against a
      caller passing a SHA as the branch).

    The .ps1 copy gets a UTF-8 BOM to match the installer's cache format
    (#67193 encoding fix). Best-effort: a failed refresh must never fail
    the update.
    """
    try:
        import re as _re

        cache_dir = Path(_m().get_hermes_home()) / "bootstrap-cache"
        if not cache_dir.is_dir():
            return
        # Mirror install_script.rs::sanitize_ref().
        safe_ref = _re.sub(r"[^A-Za-z0-9._-]", "_", str(branch or "main"))
        # Mirror install_script.rs::is_valid_commit(): 7-40 hex chars is an
        # immutable commit pin — abbreviated SHAs included. Never rewrite.
        if _re.fullmatch(r"[0-9a-fA-F]{7,40}", safe_ref):
            return
        refreshed = []
        for kind, src_name in (("ps1", "install.ps1"), ("sh", "install.sh")):
            src = _m().PROJECT_ROOT / "scripts" / src_name
            if not src.is_file():
                continue
            cached = cache_dir / f"install-{safe_ref}.{kind}"
            if not cached.is_file():
                continue  # this ref was never bootstrap-cached — nothing to heal
            data = src.read_bytes()
            if kind == "ps1" and not data.startswith(b"\xef\xbb\xbf"):
                # Match the installer's cache format: PowerShell needs the
                # UTF-8 BOM or localized/em-dash text mis-decodes (#67193).
                data = b"\xef\xbb\xbf" + data
            if cached.read_bytes() == data:
                continue  # already current
            tmp = cached.with_suffix(cached.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, cached)
            refreshed.append(cached.name)
        if refreshed:
            print(
                "  ✓ Refreshed installer bootstrap-cache script(s): "
                + ", ".join(sorted(refreshed))
            )
    except Exception as exc:
        logger.debug("Could not refresh bootstrap-cache scripts after update: %s", exc)

def _resume_windows_gateways_after_update(token: dict | None) -> None:
    """Restart Windows profile gateways previously paused for update."""
    if not token or not token.get("resume_needed"):
        return
    token["resume_needed"] = False
    if not _m()._is_windows():
        return

    # Regenerate the persisted launcher scripts before respawning anything,
    # so a legacy pythonw-era Scheduled Task / Startup entry comes back on
    # the current hidden-console design at the next login too.
    _m()._refresh_windows_gateway_launchers()

    profiles = token.get("profiles") or {}
    unmapped = token.get("unmapped") or []
    cold_start = bool(token.get("cold_start_if_installed"))
    if not profiles and not any(u.get("argv") for u in unmapped):
        if cold_start:
            _m()._cold_start_windows_gateway_after_update()
        return

    try:
        from hermes_cli.gateway import (
            launch_detached_gateway_restart_by_cmdline,
            launch_detached_profile_gateway_restart,
        )
    except Exception as exc:
        logger.debug("Could not load Windows gateway restart helper: %s", exc)
        return

    relaunched = []
    for profile, old_pid in sorted(profiles.items()):
        try:
            if launch_detached_profile_gateway_restart(str(profile), int(old_pid)):
                relaunched.append(str(profile))
        except Exception as exc:
            logger.debug(
                "Could not restart Windows gateway profile %s after update: %s",
                profile,
                exc,
            )

    # Respawn unmapped gateways (no profile→PID-file mapping, e.g. a Scheduled
    # Task) by replaying the argv we snapshotted before force-killing them.
    unmapped_relaunched = 0
    for entry in unmapped:
        argv = entry.get("argv")
        old_pid = entry.get("pid")
        if not argv or not old_pid:
            continue
        try:
            if launch_detached_gateway_restart_by_cmdline(int(old_pid), list(argv)):
                unmapped_relaunched += 1
        except Exception as exc:
            logger.debug(
                "Could not restart unmapped Windows gateway (pid %s) after update: %s",
                old_pid,
                exc,
            )

    if relaunched:
        print()
        print(f"  ✓ Restarting Windows gateway profile(s): {', '.join(relaunched)}")
    if unmapped_relaunched:
        if not relaunched:
            print()
        print(
            f"  ✓ Restarting {unmapped_relaunched} unmapped Windows gateway process(es)"
        )

def _discard_lockfile_churn(git_cmd, repo_root):
    """Restore tracked ``package-lock.json`` files that npm dirtied locally.

    npm rewrites lockfiles non-deterministically at install/build time. On a
    managed install those diffs are never intentional, so we discard them so
    ``hermes update`` sees a clean tree instead of autostashing every run.
    Best-effort; only ever touches files named ``package-lock.json``.
    """
    try:
        diff = subprocess.run(
            git_cmd + ["diff", "--name-only"],
            cwd=repo_root,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        if diff.returncode != 0:
            return
        dirty_package_dirs = {
            Path(line.strip()).parent
            for line in diff.stdout.splitlines()
            if line.strip().endswith("package.json")
        }
        dirty = [
            line.strip()
            for line in diff.stdout.splitlines()
            if line.strip().endswith("package-lock.json")
            and Path(line.strip()).parent not in dirty_package_dirs
        ]
        if not dirty:
            return
        subprocess.run(
            git_cmd + ["checkout", "--", *dirty],
            cwd=repo_root,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            check=False,
        )
        print(f"→ Discarded npm lockfile churn ({len(dirty)} file(s))")
    except Exception:
        # Never let lockfile cleanup block an update.
        pass

def _normalize_managed_eol(git_cmd, repo_root):
    """Take a managed checkout off ``core.autocrlf=true`` without leaving it dirty.

    Git for Windows ships ``core.autocrlf=true`` in its system config, which
    renormalizes this repo's LF text files to CRLF in the working tree. That
    breaks ``git checkout`` on update with "Your local changes would be
    overwritten", so ``install.ps1`` pins ``core.autocrlf=false`` on the managed
    clone (#67730). Checkouts created before that landed never got the pin and
    cannot receive it — the bootstrap installer reuses its build-pinned
    ``install.ps1`` forever — so ``hermes update``, which ships with the checkout
    itself, is the only path left that can fix them.

    The pin and the cleanup are one operation. Under ``autocrlf=true`` git
    compares normalized content, so a CRLF working tree reads clean; pinning
    alone would expose every text file as modified and hand the update an
    autostash of the whole tree. So the pin is written only after the tree is
    verified clean under it, and a checkout we cannot fully normalize is left
    exactly as it was. Best-effort: never blocks an update.
    """
    # -c, not config: evaluate the tree as it WOULD look pinned, without
    # persisting anything we might not be able to follow through on.
    probe = git_cmd + ["-c", "core.autocrlf=false"]

    def _dirty(*extra):
        out = subprocess.run(
            probe + ["diff", "-z", "--name-only", *extra],
            cwd=repo_root,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        if out.returncode != 0:
            return None
        return {p for p in out.stdout.split("\0") if p}

    def _real_dirty():
        # Files with a *content* change once CRLF differences are ignored.
        # NOTE: ``diff --name-only --ignore-cr-at-eol`` still LISTS CR-only
        # files (the name list is computed from blob/stat differences before
        # the CR filter is applied), so it cannot be used to isolate real
        # edits. ``--numstat`` does honor the filter: a CR-only file produces
        # no numstat record, while a genuinely-edited file does. Parse the
        # paths out of numstat instead.
        out = subprocess.run(
            probe + ["-c", "core.quotepath=false",
                     "diff", "--numstat", "--ignore-cr-at-eol"],
            cwd=repo_root,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        if out.returncode != 0:
            return None
        paths = set()
        for line in out.stdout.splitlines():
            if not line.strip():
                continue
            # Format: "<added>\t<deleted>\t<path>". Rename detection is off in
            # plain diff, so there is exactly one path field per record.
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[2]:
                paths.add(parts[2])
        return paths

    def _eol_only():
        all_dirty, real_dirty = _dirty(), _real_dirty()
        if all_dirty is None or real_dirty is None:
            return None
        return all_dirty - real_dirty

    try:
        effective = subprocess.run(
            git_cmd + ["config", "--get", "core.autocrlf"],
            cwd=repo_root,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        # Only "true" rewrites LF to CRLF on checkout. Unset, false, and input
        # all leave the working tree alone, so there is nothing to repair.
        if effective.stdout.strip().lower() != "true":
            return

        eol_only = _eol_only()
        if eol_only is None:
            return
        if eol_only:
            # Pathspec over stdin, not argv: a fully renormalized checkout is
            # thousands of paths, well past the Windows command-line limit.
            subprocess.run(
                probe
                + ["checkout", "--pathspec-from-file=-", "--pathspec-file-nul", "--"],
                cwd=repo_root,
                input="\0".join(sorted(eol_only)),
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                check=False,
            )
            if _eol_only():
                # Still dirty — persisting the pin here would only surface churn
                # we failed to clear. Leave the checkout as we found it.
                return
            print(f"→ Normalized line-ending churn ({len(eol_only)} file(s))")

        subprocess.run(
            git_cmd + ["config", "core.autocrlf", "false"],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
    except Exception:
        # Never let line-ending cleanup block an update.
        pass

@_with_sanitized_git_routing
def _cmd_update_impl(args, gateway_mode: bool):
    """Body of ``cmd_update`` — kept separate so the wrapper can always
    restore stdio even on ``sys.exit``."""
    # In gateway mode, use file-based IPC for prompts instead of stdin
    gw_input_fn = (
        (lambda prompt, default="": _gateway_prompt(prompt, default))
        if gateway_mode
        else None
    )
    assume_yes = bool(getattr(args, "yes", False))

    # Whether this update is running without a human at the keyboard.
    # Interactive terminal updates always stash-and-ask (unchanged behavior);
    # only non-interactive updates (desktop/chat app, gateway, `--yes`) consult
    # the `updates.non_interactive_local_changes` config setting to decide
    # whether to auto-restore stashed local source changes or throw them away.
    _non_interactive_update = (
        gateway_mode
        or assume_yes
        or not (sys.stdin.isatty() and sys.stdout.isatty())
    )
    discard_local_changes = False
    if _non_interactive_update:
        try:
            from hermes_cli.config import load_config

            _update_cfg = (load_config() or {}).get("updates", {})
            if isinstance(_update_cfg, dict):
                _mode = str(_update_cfg.get("non_interactive_local_changes", "stash")).lower()
                discard_local_changes = _mode == "discard"
        except Exception as exc:
            # Never let a config read failure change the safe default.
            logger.debug("Could not read updates.non_interactive_local_changes: %s", exc)
            discard_local_changes = False

    print("⚕ Updating Hermes Agent...")
    print()

    # On Windows, abort early if another hermes.exe is holding the venv shim
    # open. Continuing would result in a string of WinError 32 warnings and
    # then either a deferred-rename leftover or a failed git-pull fast path
    # that silently falls back to the slower ZIP route. See issue #26670.
    if _m()._is_windows() and not getattr(args, "force", False):
        scripts_dir = _m()._venv_scripts_dir()
        if scripts_dir is not None:
            concurrent = _m()._detect_concurrent_hermes_instances(scripts_dir)
            if concurrent:
                print(_format_concurrent_instances_message(concurrent, scripts_dir))
                sys.exit(2)

    deferred_gateway_resume = bool(
        getattr(args, "defer_gateway_resume", False)
    )
    if deferred_gateway_resume:
        # Prove the prior fleet is losslessly representable before the first
        # backup/source mutation.  Unmapped argv is never persisted or passed
        # across the privileged handoff boundary.
        def _publish_resume_plan_before_stop(token: dict) -> None:
            setattr(args, "_windows_gateway_resume_plan", token)
            _write_deferred_gateway_plan(args, Path(_m().PROJECT_ROOT))

        _windows_gateway_resume = _m()._pause_windows_gateways_for_update(
            require_structured_resume=True,
            before_stop=_publish_resume_plan_before_stop,
        )
    else:
        _windows_gateway_resume = None
    # The outer command owns Windows Job containment. It resumes this trusted
    # gateway plan only after the Job proves every mutating descendant exited
    # and disarms kill-on-close, while both update coordination markers remain
    # held. Starting it here would make the gateway inherit the mutation Job.
    setattr(args, "_windows_gateway_resume_plan", _windows_gateway_resume)

    # Pre-update backup — runs before any git/file mutation so users can
    # always roll back to the exact state they had before this update.
    # Returns the quick-snapshot id (or None when disabled/failed); the
    # post-update cron-jobs safety net uses it to detect job loss.
    pre_update_snapshot_id = _m()._run_pre_update_backup(args)
    if not deferred_gateway_resume:
        _windows_gateway_resume = _m()._pause_windows_gateways_for_update()
        setattr(args, "_windows_gateway_resume_plan", _windows_gateway_resume)

    # With gateways paused, anything still running from the venv interpreter
    # (most commonly the Desktop app's `hermes serve` backend) will keep .pyd
    # files locked and corrupt the dependency sync below. Refuse rather than
    # race: killing the desktop backend is futile (the app supervises and
    # respawns it), so the user must close the app. Deliberately NOT bypassed
    # by plain --force: the desktop bootstrap updater passes --force to skip
    # the hermes.exe shim guard above, but its lock probe only checks the shim
    # and app.asar — a non-desktop venv python holding a .pyd would sail
    # through and corrupt the sync (the exact failure this guard exists for).
    # --force-venv is the explicit escape hatch.
    if _m()._is_windows() and not getattr(args, "force_venv", False):
        _venv_holders = _m()._detect_venv_python_processes()
        if _venv_holders:
            _gateway_holders = _m()._leftover_pausable_gateway_pids(_venv_holders)
            if _gateway_holders is not None and deferred_gateway_resume:
                planned_pids = {
                    int(pid)
                    for pid in (
                        (_windows_gateway_resume or {}).get("profiles") or {}
                    ).values()
                }
                if any(
                    int(identity.pid) not in planned_pids
                    for identity in _gateway_holders
                ):
                    # The authenticated resume plan is frozen before any stop.
                    # A later gateway is not represented in that plan, so
                    # killing it would make the post-Job resume fleet lossy.
                    # Leave it alive and let the ordinary hard-holder refusal
                    # abort before source mutation.
                    _gateway_holders = None
            if _gateway_holders is not None:
                # Every remaining holder is a gateway the pause machinery
                # already owns — respawned by its supervisor inside the
                # pause→guard window, or up through a spawn path discovery
                # does not map. Stop them and re-check instead of
                # dead-ending; the post-update resume (and the supervisor
                # that respawned them) brings gateways back afterwards.
                from gateway.status import terminate_pid

                print(
                    f"  ⚠ {len(_gateway_holders)} gateway process(es) still "
                    "hold the venv after the pause; stopping them"
                )
                for _identity in _gateway_holders:
                    try:
                        if not _revalidate_pausable_gateway_identity(_identity):
                            raise RuntimeError(
                                f"gateway process {_identity.pid} changed before force-stop"
                            )
                        terminate_pid(int(_identity.pid), force=True)
                    except ProcessLookupError:
                        continue
                    except Exception as exc:
                        raise RuntimeError(
                            f"leftover gateway {_identity.pid} could not be safely stopped"
                        ) from exc
                _time.sleep(1.0)
                _venv_holders = _m()._detect_venv_python_processes()
        if _venv_holders:
            _orphan_backends = _m()._orphaned_desktop_backend_pids(_venv_holders)
            if _orphan_backends:
                # Every remaining holder is a Desktop `serve` backend whose
                # supervising app is GONE — the GUI-updater handoff race:
                # Electron's teardown lost the SIGTERM race, exited, and left
                # its backend (and any .hermes-runtime child) holding the
                # venv. Nothing will respawn an orphan, so reap the tree and
                # re-check instead of dead-ending with "Hermes is still
                # running" while no window is open. Backends whose Desktop
                # is still alive never reach here (_orphaned_desktop_
                # backend_pids returns None for them) — that path keeps the
                # refusal, because the app would just respawn what we kill.
                print(
                    f"  ⚠ {len(_orphan_backends)} orphaned Desktop backend "
                    "process(es) still hold the venv; stopping their trees"
                )
                _m()._stop_process_trees(_orphan_backends)
                _time.sleep(1.0)
                _venv_holders = _m()._detect_venv_python_processes()
        if _venv_holders:
            print(_format_venv_python_holders_message(_venv_holders))
            sys.exit(2)

    # Try git-based update first, fall back to ZIP download on Windows
    # when git file I/O is broken (antivirus, NTFS filter drivers, etc.)
    use_zip_update = False
    git_dir = _m().PROJECT_ROOT / ".git"

    if not git_dir.exists():
        if sys.platform == "win32":
            use_zip_update = True
        else:
            print("✗ Not a git repository. Please reinstall:")
            print(
                "  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
            )
            sys.exit(1)

    # On Windows, git can fail with "unable to write loose object file: Invalid argument"
    # due to filesystem atomicity issues. Set the recommended workaround.
    if sys.platform == "win32" and git_dir.exists():
        subprocess.run(
            _git_cmd() + ["config", "windows.appendAtomically", "false"],
            cwd=_m().PROJECT_ROOT,
            check=False,
            capture_output=True,
            env=_sanitized_git_env(),
        )

    # Build git command once — reused for fork detection and the update itself.
    git_cmd = _git_cmd()
    git_env = _sanitized_git_env()

    branch = _m()._resolve_update_branch(args)
    update_target = None
    if not use_zip_update:
        try:
            update_target = _resolve_update_target(
                git_cmd, _m().PROJECT_ROOT, branch, env=git_env
            )
            _assert_safe_git_configuration(
                git_cmd, _m().PROJECT_ROOT, env=git_env
            )
        except ValueError as exc:
            print(f"✗ {exc}")
            sys.exit(1)
        except RuntimeError as exc:
            print(f"✗ Unsafe Git configuration: {exc}")
            print("  Remove executable filter/merge drivers from this checkout before updating.")
            sys.exit(1)

    # Discard npm lockfile churn before any stash/branch logic. npm rewrites
    # tracked package-lock.json files non-deterministically at install/build
    # time (platform-specific optional deps, ideallyInert annotations, etc.),
    # which is never an intentional edit on a managed install but leaves the
    # tree dirty — forcing an autostash on every update and making branch
    # switches fragile. Restoring them first lets the common case (only
    # lockfile churn) update with a clean tree.
    _discard_lockfile_churn(git_cmd, _m().PROJECT_ROOT)
    # Same rationale, different generator: line-ending churn is machine-made
    # dirt on a managed checkout, so clear it (and stop generating it) before
    # the stash/branch logic rather than autostashing the entire tree.
    _normalize_managed_eol(git_cmd, _m().PROJECT_ROOT)

    # Detect if we're updating from a fork (before any branch logic)
    origin_url = _m()._get_origin_url(git_cmd, _m().PROJECT_ROOT)
    is_fork = _is_fork(origin_url)

    if is_fork:
        print("⚠ Updating from fork:")
        print(f"  {origin_url}")
        print()

    if use_zip_update:
        # ZIP-based update for Windows when git is broken
        _update_via_zip(args)
        return

    # Fetch and pull
    try:

        # Resolve the target branch up front so the fetch can be scoped to it.
        # A bare `git fetch origin` pulls every ref, and this repo carries
        # thousands of auto-generated branches — an unscoped fetch can stall for
        # minutes on a non-single-branch checkout. Fetch only what we update
        # against.
        assert update_target is not None

        print("→ Fetching updates...")
        fetch_result = subprocess.run(
            git_cmd
            + ["fetch", "--", update_target.remote, update_target.refspec],
            cwd=_m().PROJECT_ROOT,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            env=git_env,
        )
        if fetch_result.returncode != 0:
            stderr = fetch_result.stderr.strip()
            if "Could not resolve host" in stderr or "unable to access" in stderr:
                print("✗ Network error — cannot reach the remote repository.")
                print(f"  {stderr.splitlines()[0]}" if stderr else "")
            elif (
                "Authentication failed" in stderr or "could not read Username" in stderr
            ):
                print(
                    "✗ Authentication failed — check your git credentials or SSH key."
                )
            else:
                print("✗ Failed to fetch updates from origin.")
                if stderr:
                    print(f"  {stderr.splitlines()[0]}")
            sys.exit(1)

        target_sha_result = subprocess.run(
            git_cmd + ["rev-parse", "--verify", update_target.tracking_ref],
            cwd=_m().PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            env=git_env,
        )
        target_sha = target_sha_result.stdout.strip()

        # Get current branch (returns literal "HEAD" when detached)
        result = subprocess.run(
            git_cmd + ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=_m().PROJECT_ROOT,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            check=True,
            env=git_env,
        )
        current_branch = result.stdout.strip()

        # If user is on a different branch than the update target, switch
        # to the target. When the target is "main" this is the historical
        # "always update against main" behavior; for any other target it's
        # the same thing — get HEAD onto the requested branch first, then
        # fast-forward.
        if current_branch != branch:
            label = (
                "detached HEAD"
                if current_branch == "HEAD"
                else f"branch '{current_branch}'"
            )
            print(f"  ⚠ Currently on {label} — switching to {branch} for update...")
            # Stash before checkout so uncommitted work isn't lost
            auto_stash_ref = _m()._stash_local_changes_if_needed(git_cmd, _m().PROJECT_ROOT)
            checkout_result = subprocess.run(
                git_cmd + ["checkout", branch],
                cwd=_m().PROJECT_ROOT,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                env=git_env,
            )
            if checkout_result.returncode != 0:
                # Local checkout doesn't have this branch yet. Try to set
                # it up as a tracking branch of origin/<branch>. This is
                # the common case when the requested branch exists upstream
                # but was never checked out locally.
                track_result = subprocess.run(
                    git_cmd
                    + ["checkout", "-B", branch, update_target.tracking_ref],
                    cwd=_m().PROJECT_ROOT,
                    capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                    env=git_env,
                )
                if track_result.returncode != 0:
                    # Restore the user's prior branch + stash before bailing
                    # so we don't leave them stranded in a weird state.
                    if auto_stash_ref is not None:
                        _m()._restore_stashed_changes(
                            git_cmd,
                            _m().PROJECT_ROOT,
                            auto_stash_ref,
                            prompt_user=False,
                            input_fn=gw_input_fn,
                        )
                    print(f"✗ Branch '{branch}' does not exist locally or on origin.")
                    if track_result.stderr.strip():
                        print(f"  {track_result.stderr.strip().splitlines()[0]}")
                    sys.exit(1)
        else:
            auto_stash_ref = _m()._stash_local_changes_if_needed(git_cmd, _m().PROJECT_ROOT)

        prompt_for_restore = (
            auto_stash_ref is not None
            and not assume_yes
            and (gateway_mode or (sys.stdin.isatty() and sys.stdout.isatty()))
        )

        # Check if there are updates
        result = subprocess.run(
            git_cmd
            + ["rev-list", f"HEAD..{update_target.tracking_ref}", "--count"],
            cwd=_m().PROJECT_ROOT,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            check=True,
            env=git_env,
        )
        commit_count = int(result.stdout.strip())

        if commit_count == 0:
            _invalidate_update_cache()
            local_state_restored = True

            # Even if origin is up to date, the fork may be behind upstream.
            if is_fork and branch == "main":
                _m()._sync_with_upstream_if_needed(
                    git_cmd,
                    _m().PROJECT_ROOT,
                    fork_remote=update_target.remote,
                )

            # Upstream fork synchronization can advance the checked-out
            # target even when the selected fork ref had no new commits. Keep
            # the installed target identity before restoring the caller's
            # original branch; a receipt must never describe that other HEAD.
            installed_target_head = _capture_head_sha(
                git_cmd, _m().PROJECT_ROOT
            )
            if (
                is_fork
                and branch == "main"
                and update_target.remote == "origin"
            ):
                # A failed fork push leaves local HEAD ahead of the selected
                # remote target. Refresh the exact refspec and keep the two
                # identities distinct so receipt validation fails closed.
                target_sha = _refresh_update_target_sha(
                    git_cmd,
                    _m().PROJECT_ROOT,
                    update_target,
                    env=git_env,
                )

            # Return to the branch that owned the user's local changes before
            # reapplying its stash. Applying while still on the update target
            # can create conflicts that do not exist on the original branch.
            if current_branch not in {branch, "HEAD"}:
                checkout_original = subprocess.run(
                    git_cmd + ["checkout", current_branch],
                    cwd=_m().PROJECT_ROOT,
                    capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                    check=False,
                    env=git_env,
                )
                symbolic_original = subprocess.run(
                    git_cmd + ["symbolic-ref", "--quiet", "--short", "HEAD"],
                    cwd=_m().PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    env=git_env,
                )
                if not (
                    checkout_original.returncode == 0
                    and symbolic_original.returncode == 0
                    and symbolic_original.stdout.strip() == current_branch
                ):
                    print(
                        f"✗ Update target was checked, but branch '{current_branch}' "
                        "could not be restored."
                    )
                    print("  No success receipt was written.")
                    sys.exit(1)
            if auto_stash_ref is not None:
                local_state_restored = bool(_m()._restore_stashed_changes(
                    git_cmd,
                    _m().PROJECT_ROOT,
                    auto_stash_ref,
                    prompt_user=prompt_for_restore,
                    input_fn=gw_input_fn,
                ))

            # "No new commits" does not mean the managed interpreter is safe.
            # uv can retain the same CPython patch while python-build-standalone
            # refreshes the embedded SQLite underneath it. Keep the existing
            # update-boundary hook active on this retry path too.
            from hermes_cli.managed_uv import ensure_uv, update_managed_uv

            runtime_repairs = []
            update_managed_uv(repair_observer=runtime_repairs.append)
            ensure_uv(repair_observer=runtime_repairs.append)
            runtime_repaired = next(
                (result for result in runtime_repairs if result.repaired),
                None,
            )

            # A current checkout does NOT imply a healthy install: a previous
            # dependency sync may have failed partway (classic on Windows,
            # where a running gateway/desktop backend keeps .pyd files locked
            # and uv/pip dies with access-denied, stranding the venv between
            # versions). Probe the venv's core imports and repair if broken —
            # otherwise "Already up to date!" gaslights the user while their
            # install stays bricked.
            healthy, detail = _venv_core_imports_healthy()
            dependencies_ok = healthy
            completion_message: str | None = None
            if not healthy:
                print("⚠ Checkout is current, but the venv is unhealthy:")
                print(f"  {detail}")
                print("→ Repairing Python dependencies...")
                _write_update_incomplete_marker()
                from hermes_cli.managed_uv import ensure_uv

                repair_uv = ensure_uv()
                # A managed install whose venv is gone entirely (interrupted
                # repair after the old venv was moved aside) needs the venv
                # recreated before dependencies can be installed into it.
                venv_python_missing = not (
                    venv_python_path(
                        _m().PROJECT_ROOT / "venv", windows=_m()._is_windows()
                    )
                ).exists()
                if venv_python_missing and repair_uv:
                    print("→ Recreating virtual environment...")
                    subprocess.run(
                        [repair_uv, "venv", "venv"],
                        cwd=_m().PROJECT_ROOT,
                        check=False,
                    )
                if repair_uv:
                    repair_env = {**os.environ, "VIRTUAL_ENV": str(_m().PROJECT_ROOT / "venv")}
                    _m()._install_python_dependencies_with_optional_fallback(
                        [repair_uv, "pip"], env=repair_env, group="all"
                    )
                else:
                    _m()._install_python_dependencies_with_optional_fallback(
                        [sys.executable, "-m", "pip"], group="all"
                    )
                _m()._clear_update_incomplete_marker()
                healthy_after, detail_after = _venv_core_imports_healthy()
                dependencies_ok = healthy_after
                if healthy_after:
                    print("✓ Dependencies repaired!")
                    completion_message = "✓ Update complete!"
                else:
                    print(f"⚠ Venv still unhealthy after repair: {detail_after}")
                    print("  Close all Hermes windows/gateways and re-run: hermes update")
            else:
                completion_message = "✓ Already up to date!"
                _repair_node_deps_on_current_checkout(_print_update_completion)
            if runtime_repaired is not None and not _m()._is_windows():
                print()
                print(
                    "⚠ Restart required to finish the managed Python runtime repair."
                )
                print(
                    "  Any running Hermes gateways, Desktop backends, or other "
                    "long-lived processes still use the previous runtime."
                )
                print("  Restart each of them to pick up the repaired runtime.")
            if completion_message is not None:
                if not local_state_restored:
                    print(
                        "⚠ Local changes were not restored cleanly; "
                        "no success receipt was written."
                    )
                    sys.exit(1)
                syntax_ok, failing_path, syntax_error = _validate_critical_files_syntax(
                    _m().PROJECT_ROOT
                )
                if not syntax_ok:
                    print(f"✗ Current checkout failed syntax validation: {failing_path}")
                    if syntax_error:
                        print(f"  {syntax_error}")
                    sys.exit(1)
                node_dependencies_ok = _node_dependencies_healthy_read_only()
                if not node_dependencies_ok:
                    print(
                        "⚠ Checkout is current, but Node dependency health could "
                        "not be proven; no success receipt was written."
                    )
                    return
                resulting_head = installed_target_head
                if resulting_head is None or target_sha != resulting_head:
                    print(
                        "⚠ Installed Git identity could not be proven; "
                        "no success receipt was written."
                    )
                else:
                    _record_update_success(
                        args,
                        mode="git",
                        branch=branch,
                        remote=update_target.remote,
                        target_ref=update_target.tracking_ref,
                        target_sha=target_sha,
                        resulting_head=resulting_head,
                        archive_sha=None,
                        health={
                            "critical_syntax": syntax_ok,
                            "critical_imports": dependencies_ok,
                            "dependencies": dependencies_ok,
                            "node_dependencies": node_dependencies_ok,
                        },
                    )
                    _print_update_completion(completion_message)
            return

        print(f"→ Found {commit_count} new commit(s)")

        print("→ Pulling updates...")
        update_succeeded = False
        local_state_restored = True
        # Capture the pre-pull SHA so we can auto-roll-back if the new code
        # has a syntax error in a critical-path file (PR #28452 incident:
        # orphan merge-conflict markers in hermes_cli/config.py bricked
        # every user who ran ``hermes update`` for the 7 minutes between
        # the bad commit and the fix landing).
        pre_pull_sha = _capture_head_sha(git_cmd, _m().PROJECT_ROOT)
        try:
            # Merge the ref we already fetched above (→ Fetching updates...)
            # instead of `git pull`, which performs a SECOND network fetch of
            # the same branch (~0.5-1.5 s of redundant round-trip per update).
            # `merge --ff-only origin/<branch>` is byte-identical in effect to
            # `pull --ff-only origin <branch>` given the fresh tracking ref;
            # the divergence fallback below is unchanged.
            pull_result = subprocess.run(
                git_cmd + ["merge", "--ff-only", update_target.tracking_ref],
                cwd=_m().PROJECT_ROOT,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                env=git_env,
            )
            if pull_result.returncode != 0:
                # ff-only failed — local and remote have diverged (e.g. upstream
                # force-pushed or rebase).  Since local changes are already
                # stashed, reset to match the remote exactly.
                print(
                    "  ⚠ Fast-forward not possible (history diverged), resetting to match remote..."
                )
                reset_result = subprocess.run(
                    git_cmd + ["reset", "--hard", update_target.tracking_ref],
                    cwd=_m().PROJECT_ROOT,
                    capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                    env=git_env,
                )
                if reset_result.returncode != 0:
                    print(f"✗ Failed to reset to {update_target.tracking_ref}.")
                    if reset_result.stderr.strip():
                        print(f"  {reset_result.stderr.strip()}")
                    print(
                        f"  Try manually: git fetch {update_target.remote} "
                        f"'{update_target.refspec}' && git reset --hard "
                        f"{update_target.tracking_ref}"
                    )
                    sys.exit(1)

            # Post-pull syntax guard: validate critical-path files actually
            # parse before declaring the update successful. If a bad commit
            # made it through CI (e.g. admin-merge bypass of a failing
            # ruff check), this catches it on the user side and rolls back
            # so the CLI stays bootable. The user can then retry ``hermes
            # update`` later once a fix lands upstream.
            syntax_ok, failing_path, syntax_error = _validate_critical_files_syntax(
                _m().PROJECT_ROOT
            )
            if not syntax_ok:
                print()
                print("✗ Pulled code has a syntax error in a critical file:")
                print(f"  {failing_path}")
                if syntax_error:
                    # py_compile errors can be multi-line; show the first
                    # ~6 lines so the user sees the actual SyntaxError text.
                    for line in str(syntax_error).splitlines()[:6]:
                        print(f"    {line}")
                if pre_pull_sha:
                    print()
                    print(f"→ Rolling back to {pre_pull_sha[:10]}...")
                    rollback_result = subprocess.run(
                        git_cmd + ["reset", "--hard", pre_pull_sha],
                        cwd=_m().PROJECT_ROOT,
                        capture_output=True,
                        text=True, encoding="utf-8", errors="replace",
                        env=git_env,
                    )
                    if rollback_result.returncode == 0:
                        print("  ✓ Rollback complete — your install is unchanged.")
                        print("  Try ``hermes update`` again later once a fix lands.")
                    else:
                        print("  ✗ Rollback failed. Recover manually with:")
                        print(f"    cd {_m().PROJECT_ROOT} && git reset --hard {pre_pull_sha}")
                        if rollback_result.stderr.strip():
                            print(f"    ({rollback_result.stderr.strip().splitlines()[0]})")
                else:
                    print()
                    print("  Could not capture pre-pull SHA — recover manually with:")
                    print(f"    cd {_m().PROJECT_ROOT} && git reflog && git reset --hard <prev-sha>")
                sys.exit(1)

            update_succeeded = True
        finally:
            if auto_stash_ref is not None:
                # Don't attempt stash restore if the code update itself failed —
                # working tree is in an unknown state.
                if not update_succeeded:
                    print(
                        f"  ℹ️  Local changes preserved in stash (ref: {auto_stash_ref})"
                    )
                    print("  Restore manually with: git stash apply")
                elif discard_local_changes:
                    # Non-interactive update + user opted into discarding local
                    # source edits (updates.non_interactive_local_changes:
                    # discard). Throw the stash away instead of re-applying it.
                    _m()._discard_stashed_changes(
                        git_cmd,
                        _m().PROJECT_ROOT,
                        auto_stash_ref,
                    )
                else:
                    local_state_restored = bool(_m()._restore_stashed_changes(
                        git_cmd,
                        _m().PROJECT_ROOT,
                        auto_stash_ref,
                        prompt_user=prompt_for_restore,
                        input_fn=gw_input_fn,
                    ))

        if not local_state_restored:
            print(
                "✗ Local changes were not restored cleanly; refusing to "
                "continue or write a success receipt."
            )
            sys.exit(1)

        _invalidate_update_cache()

        # Clear stale .pyc bytecode cache — prevents ImportError on gateway
        # restart when updated source references names that didn't exist in
        # the old bytecode (e.g. get_hermes_home added to hermes_constants).
        removed = _m()._clear_bytecode_cache(_m().PROJECT_ROOT)
        if removed:
            print(
                f"  ✓ Cleared {removed} stale __pycache__ director{'y' if removed == 1 else 'ies'}"
            )
        _m()._record_bytecode_fingerprint()
        _m()._refresh_bootstrap_cache_scripts(branch)

        # Fork upstream sync logic (only for main branch on forks).
        if is_fork and branch == "main":
            _m()._sync_with_upstream_if_needed(
                git_cmd,
                _m().PROJECT_ROOT,
                fork_remote=update_target.remote,
            )
            target_sha = _refresh_update_target_sha(
                git_cmd,
                _m().PROJECT_ROOT,
                update_target,
                env=git_env,
            )

        # Reinstall Python dependencies. Prefer .[all], but if one optional extra
        # breaks on this machine, keep base deps and reinstall the remaining extras
        # individually so update does not silently strip working capabilities.
        #
        # Drop the core-install breadcrumb BEFORE touching the venv. If the
        # install is killed mid-flight (Ctrl-C, terminal close, WSL OOM), the
        # marker survives and the next ``hermes`` launch finishes the install
        # via ``_recover_from_interrupted_install``. Cleared after the core
        # ``.[all]`` install completes — lazy refresh uses a separate marker.
        dependencies_ok = False
        _write_update_incomplete_marker()
        print("→ Updating Python dependencies...")
        from hermes_cli.managed_uv import ensure_uv, update_managed_uv

        # Keep managed uv current — runs `uv self update` if we already have one.
        update_managed_uv()

        uv_bin = ensure_uv()

        pip_cmd = [sys.executable, "-m", "pip"]
        if not uv_bin:
            uv_bin = _ensure_uv_for_termux(pip_cmd)
        install_group = "all"

        if uv_bin:
            uv_env = {**os.environ, "VIRTUAL_ENV": str(_m().PROJECT_ROOT / "venv")}
            if _m()._is_termux_env(uv_env):
                uv_env.pop("PYTHONPATH", None)
                uv_env.pop("PYTHONHOME", None)
                install_group = "termux-all"
                print("  → Termux detected: using uv + curated termux-all optional profile...")
            if _m()._is_termux_env(uv_env) and _is_android_python():
                print("  → Termux/Android detected: prebuilding psutil with Linux source path compatibility...")
                _install_psutil_android_compat([uv_bin, "pip"], env=uv_env)
            _m()._install_python_dependencies_with_optional_fallback(
                [uv_bin, "pip"], env=uv_env, group=install_group
            )
        else:
            # Use sys.executable to explicitly call the venv's pip module,
            # avoiding PEP 668 'externally-managed-environment' errors on Debian/Ubuntu.
            # Some environments lose pip inside the venv; bootstrap it back with
            # ensurepip before trying the editable install.
            pip_cmd = [sys.executable, "-m", "pip"]
            try:
                subprocess.run(
                    pip_cmd + ["--version"],
                    cwd=_m().PROJECT_ROOT,
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError:
                subprocess.run(
                    [sys.executable, "-m", "ensurepip", "--upgrade", "--default-pip"],
                    cwd=_m().PROJECT_ROOT,
                    check=True,
                )
            if _m()._is_termux_env():
                install_group = "termux-all"
                print("  → Termux detected: using curated termux-all optional profile...")
            if _m()._is_termux_env() and _is_android_python():
                print("  → Termux/Android detected: prebuilding psutil with Linux source path compatibility...")
                _install_psutil_android_compat(pip_cmd)
            _m()._install_python_dependencies_with_optional_fallback(pip_cmd, group=install_group)
        dependencies_ok = True

        install_prefix = [uv_bin, "pip"] if uv_bin else pip_cmd
        lazy_env = uv_env if uv_bin else None

        # Core ``.[all]`` install finished. Clear the generic core breadcrumb
        # before the lazy-refresh phase — that phase uses its own marker so a
        # later lazy failure cannot be "healed" by clearing the core marker
        # based on a narrow 7-package import probe (#58004 review).
        _m()._clear_update_incomplete_marker()

        # The update process is still the old Python interpreter process. Run
        # one final cache/module refresh immediately before lazy backend
        # refresh, which imports newly-pulled modules that may depend on fresh
        # symbols in hermes_constants or lazy_deps. The dependency install
        # above may also have regenerated bytecode from build-cache copies —
        # this second sweep catches those stragglers (#60242, #65240).
        removed = _m()._clear_bytecode_cache(_m().PROJECT_ROOT)
        if removed:
            print(
                f"  ✓ Cleared {removed} stale __pycache__ director{'y' if removed == 1 else 'ies'}"
            )
        _m()._record_bytecode_fingerprint()
        _m()._refresh_bootstrap_cache_scripts(branch)
        _m()._reload_updated_runtime_modules()

        # Upgrade pip before lazy refreshes — stale pip can fail source builds
        # and leave partially-written packages (#57828).
        _write_lazy_refresh_incomplete_marker()
        _m()._upgrade_pip_before_lazy_refresh(install_prefix, env=lazy_env)

        # Lazy refresh can corrupt the venv when a backend install fails.
        # Clear the lazy marker only when refresh/repair is confirmed healthy.
        lazy_ok = _m()._refresh_active_lazy_features(install_prefix, env=lazy_env)
        if lazy_ok:
            _m()._clear_lazy_refresh_incomplete_marker()
        else:
            dependencies_ok = False
            print(
                "  ⚠ Lazy-refresh recovery incomplete — run `hermes` again "
                "to finish import-based venv repair."
            )

        # Heal the active memory provider's bridge packages last — the core
        # reinstall + lazy refresh above may have stripped or downgraded
        # plugin.yaml-declared deps that aren't in extras (#53272, #70636).
        _m()._refresh_active_memory_provider_dependencies()

        # Everything that can legitimately produce a transient ImportError has
        # now run (bytecode sweep, dependency reinstall, lazy refresh), so a
        # module that still won't import is real breakage. Warn only — never
        # roll back here: `cannot import name X` is also the signature of the
        # stale-bytecode class (#6207, #60242), and the launch-time sweep in
        # _sweep_stale_bytecode_if_checkout_changed() self-heals that on the
        # next run. A destructive reset would undo a good update over a state
        # that fixes itself.
        import_ok, failing_module, import_error = _validate_critical_modules_import(
            _m().PROJECT_ROOT
        )
        if not import_ok:
            print()
            print(f"  ⚠ {failing_module} still fails to import after updating:")
            print(f"      {import_error}")
            print("    Run `hermes update` again — if it persists, reinstall:")
            print("    https://hermes-agent.nousresearch.com")

        node_failures = _update_node_dependencies()
        _m()._build_web_ui(_m().PROJECT_ROOT / "web")

        # Rebuild the desktop app if the source tree changed since the last
        # build.  ``hermes desktop --build-only`` uses the content-hash stamp
        # internally, so this is effectively a no-op when nothing changed.
        # Only bother if the user has a desktop app installed (indicated by
        # an existing packaged executable or desktop dist); people who have
        # never run ``hermes desktop`` shouldn't be forced into a full
        # Electron build by ``hermes update``.
        desktop_dir = _m().PROJECT_ROOT / "apps" / "desktop"
        has_desktop_app = _m()._desktop_packaged_executable(desktop_dir) is not None or _m()._desktop_dist_exists(desktop_dir)
        if (desktop_dir / "package.json").exists() and _m()._resolve_node_runtime_npm() and has_desktop_app:
            print("→ Checking if desktop app needs rebuilding...")
            # Consult the content-hash stamp IN-PROCESS first. The spawned
            # `hermes desktop --build-only` subprocess re-imports the whole
            # CLI stack (~1-3 s) just to reach the same _m()._desktop_build_needed
            # check; when the stamp already says "up to date" we can skip the
            # spawn entirely. The update path never passes --source, so the
            # subprocess would run with source_mode=False — mirror that here.
            # Any error in the pre-check falls through to the subprocess.
            _skip_desktop_build = False
            try:
                _skip_desktop_build = not _m()._desktop_build_needed(
                    desktop_dir, _m().PROJECT_ROOT, source_mode=False
                )
            except Exception:
                _skip_desktop_build = False
            if _skip_desktop_build:
                print("  ✓ Desktop app up to date")
            else:
                _desktop_build_cmd = [sys.executable, "-m", "hermes_cli.main", "desktop", "--build-only"]
                # Capture the (very loud) Electron/vite build output into
                # update.log instead of streaming it to the terminal. On the rare
                # nonzero exit, retry once after waiting again for the venv — this
                # covers a still-settling rebuild window the first wait didn't fully
                # catch — then surface the captured tail so the failure is
                # debuggable.
                #
                # Start the build subprocess with the Hermes-managed Node on PATH:
                # when `hermes update` runs inside the desktop updater chain
                # (Desktop → hermes-setup → hermes update), the shell PATH
                # customizations are lost, so a bare-PATH child would fail with
                # `node: not found` before cmd_gui can self-heal.
                from hermes_constants import with_hermes_node_path

                _build_env = with_hermes_node_path()
                build_result = _m()._run_logged_subprocess(_desktop_build_cmd, cwd=_m().PROJECT_ROOT, env=_build_env)
                if build_result.returncode != 0:
                    build_result = _m()._run_logged_subprocess(_desktop_build_cmd, cwd=_m().PROJECT_ROOT, env=_build_env)
                if build_result.returncode != 0:
                    print("  ⚠ Desktop build failed (non-fatal; run `hermes desktop` to retry)")
                    tail = "\n".join((build_result.stdout or "").strip().splitlines()[-15:])
                    if tail:
                        print(tail)
                    from hermes_constants import display_hermes_home as _dhh
                    print(f"  Full build log: {_dhh()}/logs/update.log")
                else:
                    print("  ✓ Desktop app up to date")

        print()
        print("✓ Code updated!")

        # ── Post-update state.db integrity guard (#68474) ─────────────────
        # Verify that state.db survived the update intact.  If the live file
        # is now corrupted (zeroed, missing header, integrity failure),
        # automatically restore from the pre-update snapshot rather than
        # letting the user discover silently that their sessions are gone.
        try:
            from hermes_cli.backup import _quick_snapshot_root, verify_sqlite_integrity

            _state_path = get_hermes_home() / "state.db"
            if _state_path.exists():
                _state_ok = verify_sqlite_integrity(
                    _state_path,
                    check_header=True,
                    run_pragma=True,
                )
                if _state_ok.get("valid"):
                    logger.debug(
                        "Post-update state.db integrity check: %s",
                        _state_ok.get("message"),
                    )
                else:
                    print()
                    print(
                        "⚠ state.db is corrupted after update: "
                        + _state_ok.get("message", "unknown error")
                    )
                    _pre_snap_id = pre_update_snapshot_id
                    if _pre_snap_id:
                        _snap_state = (
                            _quick_snapshot_root(get_hermes_home())
                            / _pre_snap_id
                            / "state.db"
                        )
                        if _snap_state.exists():
                            _snap_ok = verify_sqlite_integrity(
                                _snap_state, check_header=True, run_pragma=True
                            )
                            if _snap_ok.get("valid"):
                                try:
                                    import shutil as _shutil

                                    _shutil.copy2(_snap_state, _state_path)
                                    _restored_ok = verify_sqlite_integrity(
                                        _state_path,
                                        check_header=True,
                                        run_pragma=True,
                                    )
                                    if _restored_ok.get("valid"):
                                        print(
                                            "  ✓ Auto-restored from pre-update "
                                            f"snapshot ({_pre_snap_id})"
                                        )
                                    else:
                                        print(
                                            "  ✗ Auto-restore FAILED — restored "
                                            "copy also failed integrity"
                                        )
                                except OSError as _exc:
                                    print(
                                        f"  ✗ Auto-restore file copy failed: {_exc}"
                                    )
                            else:
                                print(
                                    "  ✗ Pre-update snapshot also failed integrity"
                                )
                        else:
                            print(
                                "  ⚠ Pre-update snapshot does not contain state.db"
                            )
                    else:
                        print("  ⚠ No pre-update snapshot was taken")
                    print()
        except Exception as exc:
            logger.debug("Post-update state.db integrity check failed: %s", exc)

        # Seed the model-catalog disk cache from the freshly-pulled checkout.
        # The repo ships the canonical catalog at
        # website/static/api/model-catalog.json, and `git pull` just made it
        # current — so copy it straight over ~/.hermes/cache/model_catalog.json
        # instead of waiting on a network fetch (which can be bot-gated or hit a
        # Portal hiccup). Keeps the model picker's curated/free lists in sync
        # with the version the user just installed. Non-fatal on failure: the
        # normal network refresh still applies on the next picker open.
        try:
            from hermes_cli.model_catalog import seed_cache_from_checkout

            if seed_cache_from_checkout(_m().PROJECT_ROOT):
                print("  ✓ Model catalog cache refreshed from checkout")
        except Exception as e:
            logger.debug("Model catalog seed during update failed: %s", e)

        # Sync bundled skills (copies new, updates changed, respects user deletions)
        try:
            from tools.skills_sync import sync_skills

            print()
            print("→ Syncing bundled skills...")
            result = sync_skills(quiet=True)
            if result["copied"]:
                print(f"  + {len(result['copied'])} new: {', '.join(result['copied'])}")
            if result.get("updated"):
                print(
                    f"  ↑ {len(result['updated'])} updated: {', '.join(result['updated'])}"
                )
            if result.get("user_modified"):
                print(f"  ~ {len(result['user_modified'])} user-modified (kept)")
                print(
                    "    → see them: hermes skills list-modified  "
                    "(diff/reset to resume updates)"
                )
            if result.get("cleaned"):
                print(f"  − {len(result['cleaned'])} removed from manifest")
            if result.get("relocated"):
                print(
                    f"  → {len(result['relocated'])} moved to new upstream paths: "
                    f"{', '.join(result['relocated'])}"
                )
            if not result["copied"] and not result.get("updated"):
                print("  ✓ Skills are up to date")
        except Exception as e:
            logger.debug("Skills sync during update failed: %s", e)

        # Sync bundled skills to all profiles (including the active one).
        # seed_profile_skills() uses subprocess with an explicit HERMES_HOME so
        # it is not affected by sync_skills()'s module-level HERMES_HOME cache,
        # which means the active profile is reliably synced regardless of whether
        # the caller's HERMES_HOME env var points at the default or a named profile.
        try:
            from hermes_cli.profiles import (
                list_profiles,
                seed_profile_skills,
            )

            all_profiles = list_profiles()
            if all_profiles:
                print()
                print("→ Syncing bundled skills to all profiles...")
                for p in all_profiles:
                    try:
                        r = seed_profile_skills(p.path, quiet=True)
                        if r and r.get("skipped_opt_out"):
                            status = "opted out (--no-skills)"
                        elif r:
                            copied = len(r.get("copied", []))
                            updated = len(r.get("updated", []))
                            modified = len(r.get("user_modified", []))
                            parts = []
                            if copied:
                                parts.append(f"+{copied} new")
                            if updated:
                                parts.append(f"↑{updated} updated")
                            if modified:
                                parts.append(f"~{modified} user-modified")
                            status = ", ".join(parts) if parts else "up to date"
                        else:
                            status = "sync failed"
                        print(f"  {p.name}: {status}")
                    except Exception as pe:
                        print(f"  {p.name}: error ({pe})")
        except Exception:
            pass  # profiles module not available or no profiles

        # Backfill per-profile .env files for profiles created before the
        # .env-seeding fix (#44792). Copies the default install's .env so
        # those profiles keep the credentials they were effectively using.
        try:
            from hermes_cli.profiles import backfill_profile_envs

            backfilled = backfill_profile_envs(quiet=True)
            if backfilled:
                print()
                print(
                    f"→ Seeded .env for {len(backfilled)} profile(s) "
                    f"(copied from default): {', '.join(backfilled)}"
                )
        except Exception:
            pass  # profiles module not available or no profiles

        # Sync Honcho host blocks to all profiles
        try:
            from plugins.memory.honcho.cli import sync_honcho_profiles_quiet

            synced = sync_honcho_profiles_quiet()
            if synced:
                print(f"\n-> Honcho: synced {synced} profile(s)")
        except Exception:
            pass  # honcho plugin not installed or not configured

        # Check for config migrations.
        #
        # CRITICAL: check_config_version and migrate_config must use
        # freshly-reloaded modules, not the sys.modules cache. The
        # ``hermes update`` process is the PRE-pull Python process — its
        # ``sys.modules`` cache holds the OLD ``hermes_cli.config`` and
        # ``hermes_cli.config_migrations`` from before ``git pull`` updated
        # the source files. A function-level ``from hermes_cli.config import
        # check_config_version`` returns the cached module, so
        # ``DEFAULT_CONFIG["_config_version"]`` is the OLD value and
        # ``check_config_version()`` reports ``(33, 33)`` — "up to date" —
        # even though the freshly-pulled code has v34 with a migration to
        # run. The personality reset migration (#81946) was silently skipped
        # this way, leaving ``display.personality: kawaii`` active after
        # updates that should have reset it.
        print()
        print("→ Checking configuration for new options...")

        # Reload config modules BEFORE any config reads so get_missing_*,
        # check_config_version, and migrate_config all use the updated code.
        _reload_config_modules()

        from hermes_cli.config import (
            get_missing_env_vars,
            get_missing_config_fields,
        )

        missing_env = get_missing_env_vars(required_only=True)
        missing_config = get_missing_config_fields()
        current_ver, latest_ver = _run_config_check_fresh()

        has_new_options = bool(missing_env or missing_config)
        version_bump_only = (
            not has_new_options and current_ver < latest_ver
        )
        needs_migration = has_new_options or current_ver < latest_ver

        if version_bump_only:
            # Nothing for the user to fill in — only the config format version
            # changed (new defaults already merge in transparently). Asking
            # "configure new options now?" here is misleading: saying yes just
            # bumps the version and looks like a no-op (issue: ScottFive /
            # Tt2021). Apply it silently and say what actually happened.
            print()
            print(
                f"  ℹ Updating config format (v{current_ver} → v{latest_ver})…"
            )
            try:
                _run_migrate_config_fresh(interactive=False, quiet=True)
                print("  ✓ Config format updated (no new settings to configure)")
            except Exception as _mig_err:
                print(f"  ⚠️  Config format update failed: {_mig_err}")
                print("     Run 'hermes config migrate' to retry.")
        elif needs_migration:
            print()
            # Show WHAT changed, not just a count, so the user can make an
            # informed yes/no decision (previously the prompt named nothing).
            def _print_items(items, label, key, fallback_key=None):
                if not items:
                    return
                print(f"  {label}:")
                shown = items[:8]
                for it in shown:
                    if isinstance(it, dict):
                        name = it.get(key) or (fallback_key and it.get(fallback_key)) or "?"
                        desc = (it.get("description") or "").strip()
                    else:
                        # Defensive: some callers/mocks pass bare name strings.
                        name = str(it)
                        desc = ""
                    if desc:
                        print(f"      • {name} — {desc}")
                    else:
                        print(f"      • {name}")
                extra = len(items) - len(shown)
                if extra > 0:
                    print(f"      … and {extra} more")

            if missing_env:
                print(
                    f"  ⚠️  {len(missing_env)} new required setting(s) need configuration"
                )
                _print_items(missing_env, "New settings", "name")
            if missing_config:
                print(f"  ℹ️  {len(missing_config)} new config option(s) available")
                _print_items(missing_config, "New options", "key")

            print()
            if assume_yes:
                print(
                    "  ℹ --yes: auto-applying config migration (skipping API-key prompts)."
                )
                response = "y"
            elif gateway_mode:
                response = (
                    _gateway_prompt(
                        "Would you like to configure new options now? [Y/n]", "n"
                    )
                    .strip()
                    .lower()
                )
            elif not (sys.stdin.isatty() and sys.stdout.isatty()):
                print("  ℹ Non-interactive session — applying safe config migrations.")
                response = "auto"
            else:
                try:
                    response = (
                        input("Would you like to configure them now? [Y/n]: ")
                        .strip()
                        .lower()
                    )
                except EOFError:
                    response = "n"
                except UnicodeDecodeError:
                    # input() can raise this when the terminal encoding can't
                    # decode the byte sequence (e.g. a non-UTF-8 locale, or an
                    # embedded terminal). Without this, the exception escapes
                    # here and crashes the update at this prompt.
                    print(
                        "  ⚠ Could not read input (encoding issue). Skipping. "
                        "Run 'hermes config migrate' manually to configure."
                    )
                    response = "n"

            if response in {"", "y", "yes", "auto"}:
                print()
                # Gateway mode, --yes, and non-interactive update contexts
                # (dashboard / web server actions) cannot prompt for API keys.
                # Still run the non-interactive migration pass before restarting
                # so new default config fields and version bumps are written
                # before the freshly updated gateway validates config at startup.
                interactive_migration = not (
                    gateway_mode or assume_yes or response == "auto"
                )
                results = _run_migrate_config_fresh(interactive=interactive_migration, quiet=False)

                if results["env_added"] or results["config_added"]:
                    print()
                    print("✓ Configuration updated!")
                if (gateway_mode or assume_yes or response == "auto") and missing_env:
                    print("  ℹ API keys require manual entry: hermes config migrate")
            else:
                print()
                print("Skipped. Run 'hermes config migrate' later to configure.")
        else:
            print("  ✓ Configuration is up to date")

        # Safety net: config-version migrations have been observed to leave
        # cron/jobs.json valid-but-empty, silently dropping every scheduled
        # job (issue #34600). The desktop scheduler can also overwrite with
        # its own small set, causing partial loss (issue #52144). If the
        # live file now has fewer jobs than the pre-update snapshot, restore
        # it and warn loudly.
        try:
            from hermes_cli.backup import restore_cron_jobs_if_emptied

            cron_restore = restore_cron_jobs_if_emptied(pre_update_snapshot_id)
            if cron_restore:
                print()
                print(
                    "  ⚠️  cron/jobs.json lost jobs during this update — "
                    f"restored {cron_restore['job_count']} job(s) from "
                    f"pre-update snapshot {cron_restore['snapshot_id']}."
                )
        except Exception as exc:
            # Never let the cron safety net break an otherwise-good update.
            logger.debug("Cron jobs auto-restore check failed: %s", exc)

        print()
        if node_failures:
            print(
                "⚠ Update partially complete — Node.js dependencies for "
                f"{', '.join(node_failures)} did not refresh."
            )
            print("  Code and Python deps are updated, but the dashboard/TUI may")
            print("  be in a mixed state until the Node deps are rebuilt.")
        elif not import_ok or not dependencies_ok:
            print(
                "⚠ Update did not pass the final runtime health proof; "
                "no success receipt was written."
            )
        else:
            resulting_head = _capture_head_sha(git_cmd, _m().PROJECT_ROOT)
            if resulting_head is None or target_sha != resulting_head:
                print(
                    "⚠ Installed Git identity could not be proven; "
                    "no success receipt was written."
                )
            else:
                _record_update_success(
                    args,
                    mode="git",
                    branch=branch,
                    remote=update_target.remote,
                    target_ref=update_target.tracking_ref,
                    target_sha=target_sha,
                    resulting_head=resulting_head,
                    archive_sha=None,
                    health={
                        "critical_syntax": syntax_ok,
                        "critical_imports": import_ok,
                        "dependencies": dependencies_ok,
                        "node_dependencies": not bool(node_failures),
                    },
                )
                _print_update_completion("✓ Update complete!")

        # Search-index optimization notice (v23). Existing installs keep their
        # working search index untouched on update; the compact v23 layout —
        # which reclaims a large fraction of state.db on heavy users — is
        # opt-in. Surface it here (the moment the user is already thinking
        # about their install) with the exact command and the concrete size
        # win. Show-once-ish: only when a legacy index is actually present.
        try:
            _print_fts_optimize_available_notice()
        except Exception as e:
            logger.debug("FTS optimize notice failed: %s", e)

        # Curator first-run heads-up. Only prints when curator is enabled AND
        # has never run — i.e. the window where the ticker would otherwise
        # have fired against a fresh skill library. Kept silent on steady
        # state so we don't nag.
        try:
            _print_curator_first_run_notice()
        except Exception as e:
            logger.debug("Curator first-run notice failed: %s", e)

        # Most-recent curator run notice — show-once per run. Surfaces the
        # rename map (`old-name → umbrella`) on the high-attention update
        # surface so users learn about consolidations without having to
        # check `hermes curator status`. Self-stamps after printing so it
        # never repeats for the same run.
        try:
            _print_curator_recent_run_notice()
        except Exception as e:
            logger.debug("Curator recent-run notice failed: %s", e)

        # Repair RHEL-family root installs where /usr/local/bin isn't on PATH
        # for non-login interactive shells.  No-op on every other platform.
        try:
            _ensure_fhs_path_guard()
        except Exception as e:
            logger.debug("FHS PATH guard check failed: %s", e)

        # Self-heal the hermes-acp launcher for installs that predate it, so
        # ACP hosts (Zed, JetBrains, Buzz) can resolve Hermes on PATH without
        # a reinstall.  No-op on Windows and when already present.
        try:
            _ensure_acp_launcher()
        except Exception as e:
            logger.debug("hermes-acp launcher self-heal failed: %s", e)

        # Refresh the cua-driver binary used by the Computer Use toolset.
        # The upstream installer is gated on supported platforms and on the
        # binary already being on PATH, so this is a no-op for users who
        # don't have it. Tying the refresh to ``hermes update`` gives users a
        # predictable cadence (matches when they pull new agent code) without
        # adding startup latency or a per-launch GitHub API call.
        try:
            refresh_cua_driver = True
            try:
                from hermes_cli.config import load_config

                _update_cfg = (load_config() or {}).get("updates", {})
                if isinstance(_update_cfg, dict):
                    refresh_cua_driver = bool(
                        _update_cfg.get("refresh_cua_driver", True)
                    )
            except Exception as cfg_exc:
                logger.debug("Could not read updates.refresh_cua_driver: %s", cfg_exc)

            if (
                refresh_cua_driver
                and sys.platform in ("darwin", "win32", "linux")
                and shutil.which("cua-driver")
            ):
                from hermes_cli.tools_config import install_cua_driver

                print()
                print("→ Refreshing cua-driver (Computer Use)...")
                # require_confirmed_update: only run the (multi-minute,
                # silent) upstream installer when the driver's native
                # check-update verb positively reports a newer release.
                # An indeterminate check (offline, rate-limited, old
                # driver) keeps the installed version — `hermes update`
                # must stay fast; `hermes computer-use install --upgrade`
                # remains the force path.
                install_cua_driver(
                    upgrade=True,
                    require_confirmed_update=True,
                    show_installer_progress=False,
                )
        except Exception as e:
            logger.debug("cua-driver refresh failed: %s", e)

        # Write exit code *before* the gateway restart attempt.
        # When running as ``hermes update --gateway`` (spawned by the gateway's
        # /update command), this process lives inside the gateway's systemd
        # cgroup.  A graceful SIGUSR1 restart keeps the drain loop alive long
        # enough for the exit-code marker to be written below, but the
        # fallback ``systemctl restart`` path (see below) kills everything in
        # the cgroup (KillMode=mixed → SIGKILL to remaining processes),
        # including us and the wrapping bash shell.  The shell never reaches
        # its ``printf $status > .update_exit_code`` epilogue, so the
        # exit-code marker file would never be created.  The new gateway's
        # update watcher would then poll for 30 minutes and send a spurious
        # timeout message.
        #
        # Writing the marker here — after git pull + pip install succeed but
        # before we attempt the restart — ensures the new gateway sees it
        # regardless of how we die.
        if gateway_mode:
            _exit_code_path = get_hermes_home() / ".update_exit_code"
            try:
                _exit_code_path.write_text("0", encoding="utf-8")
            except OSError:
                pass

        gateway_fleet_restart_incomplete = False

        # Auto-restart ALL gateways after update.
        # The code update (git pull) is shared across all profiles, so every
        # running gateway needs restarting to pick up the new code.
        try:
            from hermes_cli.gateway import (
                is_macos,
                supports_systemd_services,
                _ensure_user_systemd_env,
                find_gateway_pids,
                find_profile_gateway_processes,
                _prepare_profile_gateway_update_restart,
                _get_service_pids,
                _graceful_restart_via_sigusr1,
                _wait_for_gateway_exit,
            )
            import signal as _signal

            def _wait_for_service_active(
                scope_cmd_: list,
                svc_name_: str,
                timeout: float = 10.0,
            ) -> bool:
                """Poll ``systemctl is-active`` until the unit reports active.

                systemd's Stopped -> Started transition after a graceful exit
                (or a hard restart) is not instantaneous; a one-shot check
                races that window and falsely reports the unit as down.
                Poll every 0.5s up to ``timeout`` seconds before giving up.
                """
                deadline = _time.monotonic() + max(timeout, 0.5)
                while True:
                    try:
                        _verify = subprocess.run(
                            scope_cmd_ + ["is-active", svc_name_],
                            capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=5,
                        )
                        if _verify.stdout.strip() == "active":
                            return True
                    except (FileNotFoundError, subprocess.TimeoutExpired):
                        pass
                    if _time.monotonic() >= deadline:
                        return False
                    _time.sleep(0.5)

            def _service_restart_sec(
                scope_cmd_: list,
                svc_name_: str,
                default: float = 0.0,
            ) -> float:
                """Read the unit's ``RestartUSec`` (RestartSec) in seconds.

                After a graceful exit-75, systemd waits ``RestartSec`` before
                respawning the unit.  Callers that poll for ``is-active``
                must use a timeout >= ``RestartSec`` + transition slack, or
                they'll give up *during* the cooldown window and wrongly
                conclude the unit didn't relaunch.
                """
                try:
                    _show = subprocess.run(
                        scope_cmd_
                        + [
                            "show",
                            svc_name_,
                            "--property=RestartUSec",
                            "--value",
                        ],
                        capture_output=True,
                        text=True, encoding="utf-8", errors="replace",
                        timeout=5,
                    )
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    return default
                raw = (_show.stdout or "").strip()
                # systemd emits values like "30s", "100ms", "1min 30s", or
                # "infinity".  Parse conservatively; on any miss return default.
                if not raw or raw == "infinity":
                    return default
                total = 0.0
                matched = False
                for part in raw.split():
                    for _suf, _mult in (
                        ("ms", 0.001),
                        ("us", 0.000001),
                        ("min", 60.0),
                        ("s", 1.0),
                    ):
                        if part.endswith(_suf):
                            try:
                                total += float(part[: -len(_suf)]) * _mult
                                matched = True
                            except ValueError:
                                pass
                            break
                return total if matched else default

            _manage_cmd_cache: dict = {}

            def _resolve_manage_cmd(scope_: str, scope_cmd_: list, svc_name_: str):
                """Resolve the command prefix for manage-units operations.

                Read-only systemctl calls (``is-active``, ``show``,
                ``list-units``) work unprivileged, but manage-units verbs
                (``reset-failed``, ``start``, ``restart``) on a *system*
                service trigger a polkit ``org.freedesktop.systemd1.manage-units``
                authentication prompt when run as a non-root user.  That
                interactive prompt runs inside our captured subprocess with a
                10-15s timeout — the user sees the prompt flash and "exit
                directly" before they can answer, and the resulting
                TimeoutExpired used to be swallowed silently.

                Strategy: if root, plain systemctl.  If not root, try
                non-interactive sudo (``sudo -n``) — first a blanket probe,
                then a targeted ``systemctl reset-failed`` probe so a
                least-privilege sudoers entry scoped to
                ``systemctl ... hermes-gateway*`` also qualifies
                (``reset-failed`` is an idempotent no-op we run before every
                privileged restart anyway).  If neither works, return None —
                the caller must SKIP the restart (without draining the
                gateway first!) and tell the user how to restart manually.
                ``--no-ask-password`` guarantees polkit can never hang a
                captured subprocess on this path.
                """
                if scope_ in _manage_cmd_cache:
                    return _manage_cmd_cache[scope_]
                cmd = scope_cmd_ + ["--no-ask-password"]
                if (
                    scope_ == "system"
                    and hasattr(os, "geteuid")
                    and os.geteuid() != 0  # windows-footgun: ok — systemd path, Linux-only
                ):
                    sudo_cmd = ["sudo", "-n"] + scope_cmd_ + ["--no-ask-password"]
                    sudo_ok = False
                    try:
                        _probe = subprocess.run(
                            ["sudo", "-n", "true"],
                            capture_output=True,
                            timeout=5,
                        )
                        sudo_ok = _probe.returncode == 0
                        if not sudo_ok:
                            # Blanket sudo refused — a targeted sudoers entry
                            # (NOPASSWD for systemctl ... hermes-gateway*)
                            # may still allow the exact commands we need.
                            _probe = subprocess.run(
                                sudo_cmd + ["reset-failed", svc_name_],
                                capture_output=True,
                                timeout=5,
                            )
                            sudo_ok = _probe.returncode == 0
                    except (FileNotFoundError, subprocess.TimeoutExpired):
                        sudo_ok = False
                    cmd = sudo_cmd if sudo_ok else None
                _manage_cmd_cache[scope_] = cmd
                return cmd

            # Wait budget for graceful SIGUSR1 restarts.  In-band restart
            # may defer stop() until active turns finish
            # (``restart_after_turn_timeout``, #77184) and then spend up to
            # ``restart_drain_timeout`` inside stop(). Cover both phases so
            # we don't fall back to a hard kill while the gateway is still
            # patiently waiting for the requesting turn. On older systemd
            # units without SIGUSR1 wiring this wait just times out and we
            # fall back to ``systemctl restart`` (the old behaviour).
            try:
                from hermes_cli.gateway import _get_restart_exit_wait_budget

                _drain_budget = max(float(_get_restart_exit_wait_budget()), 45.0)
            except Exception:
                _drain_budget = 45.0

            restarted_services = []
            failed_or_stale_units = []
            killed_pids = set()
            relaunched_profiles = []
            externally_supervised_profiles = []

            # --- Systemd services (Linux) ---
            # Discover all hermes-gateway* units (default + profiles)
            if supports_systemd_services():
                try:
                    _ensure_user_systemd_env()
                except Exception:
                    pass

                for scope, scope_cmd in [
                    ("user", ["systemctl", "--user"]),
                    ("system", ["systemctl"]),
                ]:
                    try:
                        result = subprocess.run(
                            scope_cmd
                            + [
                                "list-units",
                                "hermes-gateway*",
                                "--plain",
                                "--no-legend",
                                "--no-pager",
                            ],
                            capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=10,
                        )
                    except FileNotFoundError:
                        continue
                    except subprocess.TimeoutExpired as exc:
                        # Discovery timeout — skip this scope, keep the other.
                        print(
                            f"  ⚠ systemctl timed out listing {scope}-scope "
                            f"gateway units ({exc.cmd if exc.cmd else 'unknown command'}). "
                            f"Check the gateway with: hermes gateway status"
                        )
                        continue

                    def _restart_one_systemd_gateway_unit(svc_name: str) -> None:
                        # Check if active
                        check = subprocess.run(
                            scope_cmd + ["is-active", svc_name],
                            capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=5,
                        )
                        if check.stdout.strip() != "active":
                            return

                        # Resolve how we may run manage-units verbs
                        # (reset-failed/start/restart) for this scope.
                        # None ⇒ no non-interactive privilege path; we
                        # must avoid those verbs entirely or polkit will
                        # throw an interactive auth prompt inside our
                        # captured 10-15s subprocess (the user sees it
                        # flash and "exit directly" — reported June 2026).
                        _manage_cmd = _resolve_manage_cmd(
                            scope, scope_cmd, svc_name
                        )

                        # Prefer a graceful SIGUSR1 restart so in-flight
                        # agent runs drain instead of being SIGKILLed.
                        # The gateway's SIGUSR1 handler calls
                        # request_restart(via_service=True) → drain →
                        # exit; systemd's Restart=always respawns the unit.
                        _main_pid = 0
                        try:
                            _show = subprocess.run(
                                scope_cmd
                                + [
                                    "show",
                                    svc_name,
                                    "--property=MainPID",
                                    "--value",
                                ],
                                capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=5,
                            )
                            _main_pid = int((_show.stdout or "").strip() or 0)
                        except (
                            ValueError,
                            subprocess.TimeoutExpired,
                            FileNotFoundError,
                        ):
                            _main_pid = 0

                        _graceful_ok = False
                        if _main_pid > 0:
                            print(
                                f"  → {svc_name}: draining (up to {int(_drain_budget)}s)..."
                            )
                            _graceful_ok = _graceful_restart_via_sigusr1(
                                _main_pid,
                                drain_timeout=_drain_budget,
                            )

                        if _graceful_ok:
                            # Gateway exited after a planned restart.
                            # ``Restart=always`` means systemd WILL respawn
                            # the unit — but only after
                            # ``RestartSec`` (default 60s on our unit
                            # file). That 60s wait is a crash-loop guard,
                            # and is the right default when the gateway
                            # dies unexpectedly. For a voluntary restart
                            # on update, it's dead time the user watches.
                            #
                            # Shortcut it: ``reset-failed`` + ``start``
                            # skips RestartSec entirely (we're manually
                            # initiating the unit, not waiting for
                            # systemd's auto-restart logic). Takes about
                            # as long as the process takes to come up
                            # (~1-3s on a warm box).
                            #
                            # If the unit is already active because
                            # RestartSec elapsed while we were draining,
                            # ``start`` is a no-op and we fall through to
                            # the poll below. Either way we collapse the
                            # 60s+ delay to a ~5s one.
                            #
                            # The shortcut needs manage-units privileges.
                            # Without them (system service, non-root, no
                            # passwordless sudo) skip it — systemd's own
                            # auto-restart still relaunches the unit after
                            # RestartSec, no privileges required.
                            if _manage_cmd is not None:
                                subprocess.run(
                                    _manage_cmd + ["reset-failed", svc_name],
                                    capture_output=True,
                                    text=True, encoding="utf-8", errors="replace",
                                    timeout=10,
                                )
                                subprocess.run(
                                    _manage_cmd + ["start", svc_name],
                                    capture_output=True,
                                    text=True, encoding="utf-8", errors="replace",
                                    timeout=15,
                                )
                                # Short poll: the gateway should be up
                                # within a few seconds now that we
                                # bypassed RestartSec.
                                if _wait_for_service_active(
                                    scope_cmd,
                                    svc_name,
                                    timeout=10.0,
                                ):
                                    restarted_services.append(svc_name)
                                    return
                            # Passive poll: systemd's auto-restart fires
                            # after RestartSec regardless of privileges.
                            # This is the primary path when _manage_cmd is
                            # None, and the fallback when the explicit
                            # start didn't take.
                            _restart_sec = _service_restart_sec(
                                scope_cmd,
                                svc_name,
                                default=0.0,
                            )
                            _post_drain_timeout = max(
                                10.0,
                                _restart_sec + 10.0,
                            )
                            if _manage_cmd is None and _restart_sec > 5.0:
                                print(
                                    f"  → {svc_name}: waiting for systemd "
                                    f"auto-restart (~{int(_restart_sec)}s; "
                                    "no root for an immediate restart)..."
                                )
                            if _wait_for_service_active(
                                scope_cmd,
                                svc_name,
                                timeout=_post_drain_timeout,
                            ):
                                restarted_services.append(svc_name)
                                return
                            # Process exited but wasn't respawned (older
                            # unit without Restart=on-failure or
                            # RestartForceExitStatus=75).  Fall through
                            # to systemctl start/restart.
                            print(
                                f"  ⚠ {svc_name} drained but didn't relaunch — forcing restart"
                            )

                        # Forcing a restart requires manage-units
                        # privileges.  Without a non-interactive path,
                        # running systemctl here would spawn a polkit
                        # auth prompt inside a captured 10-15s subprocess
                        # — it flashes and dies before the user can
                        # answer.  Skip with clear instructions instead.
                        if _manage_cmd is None:
                            failed_or_stale_units.append(svc_name)
                            print(
                                f"  ⚠ {svc_name} is a system service and restarting it needs root.\n"
                                f"    Restart it manually to load the new version:\n"
                                f"      sudo systemctl restart {svc_name}\n"
                                f"    To let `hermes update` restart it automatically, allow\n"
                                f"    passwordless sudo for systemctl, or run updates with sudo."
                            )
                            return

                        # Fallback: blunt systemctl restart.  This is
                        # what the old code always did; we get here only
                        # when the graceful path failed (unit missing
                        # SIGUSR1 wiring, drain exceeded the budget,
                        # restart-policy mismatch).
                        #
                        # Always `reset-failed` first.  If systemd's own
                        # auto-restart attempts already parked the unit
                        # in a failed state (transient CHDIR / OOM /
                        # filesystem race after our drain + exit-75),
                        # a plain `systemctl restart` can wedge against
                        # the RestartSec backoff and leave the unit
                        # dead.  Clearing the failed state first makes
                        # the restart idempotent.  Mirrors the recovery
                        # path in `hermes gateway restart`
                        # (`systemd_restart()`) as of PR #20949.
                        subprocess.run(
                            _manage_cmd + ["reset-failed", svc_name],
                            capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=10,
                        )
                        restart = subprocess.run(
                            _manage_cmd + ["restart", svc_name],
                            capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=15,
                        )
                        if restart.returncode == 0:
                            # Verify the service actually survived the
                            # restart.  systemctl restart returns 0 even
                            # if the new process crashes immediately.
                            if _wait_for_service_active(
                                scope_cmd,
                                svc_name,
                                timeout=10.0,
                            ):
                                restarted_services.append(svc_name)
                            else:
                                # Retry once — transient startup failures
                                # (stale module cache, import race) often
                                # resolve on the second attempt.  Again
                                # clear any failed state first so the
                                # retry isn't blocked by the previous
                                # crash.
                                print(
                                    f"  ⚠ {svc_name} died after restart, retrying..."
                                )
                                subprocess.run(
                                    _manage_cmd + ["reset-failed", svc_name],
                                    capture_output=True,
                                    text=True, encoding="utf-8", errors="replace",
                                    timeout=10,
                                )
                                subprocess.run(
                                    _manage_cmd + ["restart", svc_name],
                                    capture_output=True,
                                    text=True, encoding="utf-8", errors="replace",
                                    timeout=15,
                                )
                                if _wait_for_service_active(
                                    scope_cmd,
                                    svc_name,
                                    timeout=10.0,
                                ):
                                    restarted_services.append(svc_name)
                                    print(f"  ✓ {svc_name} recovered on retry")
                                else:
                                    failed_or_stale_units.append(svc_name)
                                    _scope_flag = "--user " if scope == "user" else ""
                                    _sudo_hint = "sudo " if scope == "system" else ""
                                    print(
                                        f"  ✗ {svc_name} failed to stay running after restart.\n"
                                        f"    Check logs: {_sudo_hint}journalctl {_scope_flag}-u {svc_name} --since '2 min ago'\n"
                                        f"    Recover manually:\n"
                                        f"      {_sudo_hint}systemctl {_scope_flag}reset-failed {svc_name}\n"
                                        f"      {_sudo_hint}systemctl {_scope_flag}restart {svc_name}"
                                    )
                        else:
                            failed_or_stale_units.append(svc_name)
                            print(
                                f"  ⚠ Failed to restart {svc_name}: {restart.stderr.strip()}"
                            )

                    def _on_unit_timeout(svc_name: str, exc: subprocess.TimeoutExpired) -> None:
                        # Isolate the timeout to this unit and keep going
                        # (#68523). A scope-wide handler used to abort every
                        # later gateway and leave the fleet on mixed code.
                        failed_or_stale_units.append(svc_name)
                        print(
                            f"  ⚠ systemctl timed out restarting {svc_name} "
                            f"({exc.cmd if exc.cmd else 'unknown command'}); "
                            f"continuing with remaining gateways"
                        )

                    _for_each_systemd_gateway_unit(
                        result.stdout,
                        process_unit=_restart_one_systemd_gateway_unit,
                        on_unit_timeout=_on_unit_timeout,
                    )

            # --- Launchd services (macOS) ---
            if is_macos():
                try:
                    from hermes_cli.gateway import (
                        launchd_restart,
                        get_launchd_label,
                        get_launchd_plist_path,
                    )

                    plist_path = get_launchd_plist_path()
                    if plist_path.exists():
                        check = subprocess.run(
                            ["launchctl", "list", get_launchd_label()],
                            capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=5,
                        )
                        if check.returncode == 0:
                            try:
                                launchd_restart()
                                restarted_services.append(get_launchd_label())
                            except subprocess.CalledProcessError as e:
                                stderr = (getattr(e, "stderr", "") or "").strip()
                                print(f"  ⚠ Gateway restart failed: {stderr}")
                except (FileNotFoundError, subprocess.TimeoutExpired, ImportError):
                    pass

            # --- Manual (non-service) gateways ---
            # Kill any remaining gateway processes not managed by a service.
            # Exclude PIDs that belong to just-restarted services so we don't
            # immediately kill the process that systemd/launchd just spawned.
            service_pids = _get_service_pids()
            manual_pids = find_gateway_pids(
                exclude_pids=service_pids, all_profiles=True
            )
            profile_processes = {
                proc.pid: proc
                for proc in find_profile_gateway_processes(exclude_pids=service_pids)
                if proc.pid in manual_pids
            }
            for pid, proc in profile_processes.items():
                restart_mode = _prepare_profile_gateway_update_restart(
                    proc.profile, pid
                )
                if restart_mode is None:
                    continue
                # Prefer a graceful SIGUSR1 drain so in-flight agent runs
                # finish before the watcher respawns the gateway.  If the
                # gateway doesn't support SIGUSR1 or doesn't exit within
                # the drain budget, fall back to SIGTERM — the watcher
                # still sees the exit and relaunches either way.
                # Announce the drain first: this wait can hold for the full
                # budget per gateway with no other output, and on surfaces
                # that stream update progress (the desktop updater most of
                # all) the silence reads as a hung update (#44515).
                print(
                    f"  → {proc.profile}: draining gateway PID {pid} "
                    f"(up to {int(_drain_budget)}s)..."
                )
                drained = _graceful_restart_via_sigusr1(
                    pid,
                    drain_timeout=_drain_budget,
                )
                if not drained:
                    try:
                        os.kill(pid, _signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        pass
                # Wait for the old process to fully exit before the watcher
                # spawns the new gateway.  Telegram holds the previous
                # getUpdates long-poll session open on its servers for up to
                # ~30s after the client disconnects.  If the new gateway
                # connects before that window expires it receives a 409
                # Conflict, which _handle_polling_conflict() recovers from
                # via back-off retries — but a brief wait here reduces the
                # chance of hitting that path at all, especially on fast
                # machines where the watcher loop restarts in < 1s.
                # We wait up to 5s for the process to exit (the OS-level
                # close, not the Telegram server-side expiry), then let the
                # watcher take over.  The Telegram adapter's retry logic
                # handles any remaining 409s if the server session is still
                # live when the new gateway polls.
                _wait_for_gateway_exit(timeout=5.0, force_after=None)
                killed_pids.add(pid)
                if restart_mode == "external-supervisor":
                    externally_supervised_profiles.append(proc.profile)
                else:
                    relaunched_profiles.append(proc.profile)

            for pid in manual_pids:
                if pid in profile_processes:
                    continue
                try:
                    os.kill(pid, _signal.SIGTERM)
                    killed_pids.add(pid)
                except (ProcessLookupError, PermissionError):
                    pass

            if restarted_services or killed_pids:
                print()
                for svc in restarted_services:
                    print(f"  ✓ Restarted {svc}")
                if relaunched_profiles:
                    names = ", ".join(relaunched_profiles)
                    print(f"  ✓ Restarting manual gateway profile(s): {names}")
                if externally_supervised_profiles:
                    names = ", ".join(externally_supervised_profiles)
                    print(
                        "  ✓ Handed gateway profile(s) back to their external "
                        f"supervisor: {names}"
                    )
                unmapped_count = (
                    len(killed_pids)
                    - len(relaunched_profiles)
                    - len(externally_supervised_profiles)
                )
                if unmapped_count:
                    print(f"  → Stopped {unmapped_count} manual gateway process(es)")
                    print("    Restart manually: hermes gateway run")
                    if unmapped_count > 1:
                        print(
                            "    (or: hermes -p <profile> gateway run  for each profile)"
                        )

            if failed_or_stale_units:
                gateway_fleet_restart_incomplete = True
                if gateway_mode:
                    _exit_code_path = get_hermes_home() / ".update_exit_code"
                    try:
                        _exit_code_path.write_text("1", encoding="utf-8")
                    except OSError:
                        pass
            _warn_incomplete_gateway_fleet_restart(failed_or_stale_units)

            if not restarted_services and not killed_pids:
                # No gateways were running — nothing to do
                pass

            # --- Post-restart survivor sweep -----------------------------
            # Issue #17648: some gateways ignore SIGTERM (stuck drain,
            # blocked I/O, PID dead but zombie).  The detached profile
            # watchers wait 120s for the old PID to exit — if it never
            # does, no respawn happens and the user keeps hitting
            # ImportError against a stale sys.modules.  Give the
            # graceful paths a brief window to complete, then SIGKILL
            # any remaining pre-update PIDs so the watcher / service
            # manager can relaunch with fresh code.
            try:
                _time.sleep(3.0)
                _service_pids_after = _get_service_pids()
                _surviving = find_gateway_pids(
                    exclude_pids=_service_pids_after,
                    all_profiles=True,
                )
                # Scope to PIDs we already tried to kill during this
                # update (killed_pids).  Anything new is a gateway that
                # started AFTER our restart attempt — respecting user
                # intent, we don't kill those.
                _stuck = [pid for pid in _surviving if pid in killed_pids]
                if _stuck:
                    print()
                    print(
                        f"  ⚠ {len(_stuck)} gateway process(es) ignored SIGTERM — force-killing"
                    )
                    from gateway.status import terminate_pid as _terminate_pid
                    for pid in _stuck:
                        try:
                            # Routes through taskkill /T /F on Windows,
                            # SIGKILL on POSIX — _signal.SIGKILL doesn't
                            # exist on Windows so the old raw os.kill call
                            # used to crash the entire update path.
                            _terminate_pid(pid, force=True)
                        except (ProcessLookupError, PermissionError, OSError):
                            pass
                    # Give the OS a beat to reap the processes so the
                    # watchers see them exit and respawn.
                    _time.sleep(1.5)
            except Exception as _sweep_exc:
                logger.debug("Post-restart survivor sweep failed: %s", _sweep_exc)

        except Exception as e:
            logger.debug("Gateway restart during update failed: %s", e)

        # Warn if legacy Hermes gateway unit files are still installed.
        # When both hermes.service (from a pre-rename install) and the
        # current hermes-gateway.service are enabled, they SIGTERM-fight
        # for the same bot token (see PR #11909). Flagging here means
        # every `hermes update` surfaces the issue until the user migrates.
        try:
            from hermes_cli.gateway import (
                has_legacy_hermes_units,
                _find_legacy_hermes_units,
                supports_systemd_services,
            )

            if supports_systemd_services() and has_legacy_hermes_units():
                print()
                print("⚠ Legacy Hermes gateway unit(s) detected:")
                for name, path, is_sys in _find_legacy_hermes_units():
                    scope = "system" if is_sys else "user"
                    print(f"    {path}  ({scope} scope)")
                print()
                print("  These pre-rename units (hermes.service) fight the current")
                print("  hermes-gateway.service for the bot token and cause SIGTERM")
                print("  flap loops. Remove them with:")
                print()
                print("    hermes gateway migrate-legacy")
                print()
                print("  (add `sudo` if any are in system scope)")
        except Exception as e:
            logger.debug("Legacy unit check during update failed: %s", e)

        # Restart a managed dashboard through systemd, or stop stale manual
        # dashboard processes. Raw-killing a systemd-owned dashboard PID makes
        # systemd treat it as a clean stop, leaving the Cloudflare origin dead.
        # Preserve the safety rule above: a failed Node refresh leaves the
        # currently running dashboard untouched.
        _finish_dashboard_update_cleanup(node_failures)

        print()
        print("Tip: You can now select a provider and model:")
        print("  hermes model              # Select provider and model")

        if gateway_fleet_restart_incomplete:
            # Code update itself succeeded, but at least one gateway still
            # runs pre-update modules — surface that as a failed update so
            # automation / operators do not treat the fleet as healthy.
            sys.exit(1)

    except subprocess.CalledProcessError as e:
        if sys.platform == "win32":
            print(f"⚠ Git update failed: {e}")
            print("→ Falling back to ZIP download...")
            print()
            _update_via_zip(args)
        else:
            print(f"✗ Update failed: {e}")
            sys.exit(1)

# --- Hoisted from the body of _cmd_update_impl (self-contained, no closure state) ---

def _print_items(items, label, key, fallback_key=None):
    if not items:
        return
    print(f"  {label}:")
    shown = items[:8]
    for it in shown:
        if isinstance(it, dict):
            name = it.get(key) or (fallback_key and it.get(fallback_key)) or "?"
            desc = (it.get("description") or "").strip()
        else:
            # Defensive: some callers/mocks pass bare name strings.
            name = str(it)
            desc = ""
        if desc:
            print(f"      • {name} — {desc}")
        else:
            print(f"      • {name}")
    extra = len(items) - len(shown)
    if extra > 0:
        print(f"      … and {extra} more")

def _wait_for_service_active(
    scope_cmd_: list,
    svc_name_: str,
    timeout: float = 10.0,
) -> bool:
    """Poll ``systemctl is-active`` until the unit reports active.

    systemd's Stopped -> Started transition after a graceful exit
    (or a hard restart) is not instantaneous; a one-shot check
    races that window and falsely reports the unit as down.
    Poll every 0.5s up to ``timeout`` seconds before giving up.
    """
    deadline = _time.monotonic() + max(timeout, 0.5)
    while True:
        try:
            _verify = subprocess.run(
                scope_cmd_ + ["is-active", svc_name_],
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=5,
            )
            if _verify.stdout.strip() == "active":
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        if _time.monotonic() >= deadline:
            return False
        _time.sleep(0.5)

def _service_restart_sec(
    scope_cmd_: list,
    svc_name_: str,
    default: float = 0.0,
) -> float:
    """Read the unit's ``RestartUSec`` (RestartSec) in seconds.

    After a graceful exit-75, systemd waits ``RestartSec`` before
    respawning the unit.  Callers that poll for ``is-active``
    must use a timeout >= ``RestartSec`` + transition slack, or
    they'll give up *during* the cooldown window and wrongly
    conclude the unit didn't relaunch.
    """
    try:
        _show = subprocess.run(
            scope_cmd_
            + [
                "show",
                svc_name_,
                "--property=RestartUSec",
                "--value",
            ],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return default
    raw = (_show.stdout or "").strip()
    # systemd emits values like "30s", "100ms", "1min 30s", or
    # "infinity".  Parse conservatively; on any miss return default.
    if not raw or raw == "infinity":
        return default
    total = 0.0
    matched = False
    for part in raw.split():
        for _suf, _mult in (
            ("ms", 0.001),
            ("us", 0.000001),
            ("min", 60.0),
            ("s", 1.0),
        ):
            if part.endswith(_suf):
                try:
                    total += float(part[: -len(_suf)]) * _mult
                    matched = True
                except ValueError:
                    pass
                break
    return total if matched else default
