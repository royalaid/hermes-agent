"""Stdlib-only update lease gate for the Hermes-tools MCP bridge.

This module deliberately lives outside :mod:`agent`.  ``python -m
agent.transports.hermes_tools_mcp_server`` imports ``agent.__init__`` before
the target module, and that package preloads a native extension.  The gate
must therefore be usable before any mutable-venv import occurs.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

from hermes_constants import get_default_hermes_root

MARKER_NAME = ".hermes-venv-quiesce"
MCP_MAIN_MODULE = "agent.transports.hermes_tools_mcp_server"
SCHEMA_VERSION = 1
MAX_LEASE_SECONDS = 20 * 60
MAX_HANDOFF_GRACE_SECONDS = 90
EMERGENCY_LEASE_SECONDS = 2 * 60
DEFAULT_HANDOFF_GRACE_SECONDS = 90
_CLOCK_SKEW_SECONDS = 5
_OWNER_START_TOLERANCE_SECONDS = 1.0
_LEASE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{16,128}$")
_V1_LEASE_KEYS = frozenset(
    {
        "schema_version",
        "lease_id",
        "owner_pid",
        "created_at",
        "expires_at",
        "handoff_grace_until",
        "install_root",
    }
)


def _windows_open_process_error_is_definitive_exit(error: int) -> bool:
    """Return whether ``OpenProcess`` proved that a PID does not exist.

    ``ERROR_INVALID_PARAMETER`` is the documented result for an invalid or
    already-exited process identifier. Access denial and every unfamiliar API
    failure are ownership-unknown, so updater coordination treats them live.
    """
    return int(error) == 87  # ERROR_INVALID_PARAMETER


def _canonical(path: str | Path) -> str:
    """Return a case-normalized, symlink-resolved absolute path."""
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _pid_alive(pid: int) -> bool:
    """Return whether *pid* is live, treating access denial as live."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
        open_process.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_bool
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        get_exit_code.restype = ctypes.c_bool

        process = open_process(0x1000, False, int(pid))
        if not process:
            return not _windows_open_process_error_is_definitive_exit(
                ctypes.get_last_error()
            )
        try:
            exit_code = ctypes.c_ulong()
            if not get_exit_code(process, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            close_handle(process)
    except Exception:
        # A platform/API failure is not a definitive process exit. Lease
        # ownership must fail closed for every positive PID we cannot inspect.
        return True


def _pid_create_time(pid: int) -> float | None:
    """Return a Windows process creation epoch, or ``None`` if unprovable.

    The lease format intentionally stays cross-runtime schema v1.  Comparing
    the live process creation time with the already-recorded ``created_at``
    prevents a recycled numeric PID from inheriting an old updater's lease
    without adding a Python-only marker field.  Unknown/API-denied results are
    not proof of reuse and are therefore handled fail-closed by the caller.
    """
    if pid <= 0 or os.name != "nt":
        return None

    class _FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
        open_process.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_bool
        get_process_times = kernel32.GetProcessTimes
        get_process_times.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
        ]
        get_process_times.restype = ctypes.c_bool

        process = open_process(0x1000, False, int(pid))
        if not process:
            return None
        try:
            created = _FileTime()
            exited = _FileTime()
            kernel = _FileTime()
            user = _FileTime()
            if not get_process_times(
                process,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            ticks = (int(created.high) << 32) | int(created.low)
            # FILETIME is 100-nanosecond ticks since 1601-01-01 UTC.
            return (ticks / 10_000_000.0) - 11_644_473_600.0
        finally:
            close_handle(process)
    except Exception:
        return None

def marker_path(hermes_home: str | Path | None = None) -> Path:
    """Return the install-global bridge lease path.

    ``hermes_constants`` is deliberately stdlib-only and is the canonical
    owner of Hermes-home resolution.  Reusing it here keeps the pre-import
    MCP gate aligned with the updater lock/receipt paths, including custom
    and profile-scoped homes, without importing the ``agent`` package.
    """
    return get_default_hermes_root(hermes_home) / MARKER_NAME


def infer_install_root() -> Path | None:
    """Infer the owning checkout from the venv prefix or managed runtime."""
    prefix = Path(sys.prefix)
    if prefix.name.lower() in {"venv", ".venv"}:
        return prefix.parent

    executable = Path(sys.executable)
    parts = [part.lower() for part in executable.parts]
    try:
        marker_index = parts.index(".hermes-runtime")
    except ValueError:
        return None
    if marker_index <= 0:
        return None
    return Path(*executable.parts[:marker_index])


def is_exact_mcp_module_argv(argv: Sequence[str]) -> bool:
    """True only for an argument-free ``-m <MCP_MAIN_MODULE>`` launch."""
    parts = [str(part) for part in argv]
    indexes = [index for index, token in enumerate(parts) if token == "-m"]
    if not (
        parts
        and len(indexes) == 1
        and indexes[0] + 2 == len(parts)
        and parts[indexes[0] + 1] == MCP_MAIN_MODULE
    ):
        return False

    # ``-m`` must be the interpreter's operative action, not text after a
    # script name.  Accept Python's ordinary pre-module runtime switches, but
    # reject ``-c``, ``--``, unknown switches, and non-option operands.  This
    # is intentionally smaller than the complete Python CLI grammar: failure
    # to recognize an exotic launch only makes an updater refuse to kill it.
    before_module = parts[1 : indexes[0]]
    argument_options = {"-W", "-X", "--check-hash-based-pycs"}
    long_flags = {
        "--bytes-warning",
        "--debug",
        "--dont-write-bytecode",
        "--help-env",
        "--help-xoptions",
        "--ignore-environment",
        "--inspect",
        "--isolated",
        "--no-site",
        "--no-user-site",
        "--safe-path",
        "--unbuffered",
        "--verbose",
    }
    short_flag_chars = frozenset("bBdEIiOPqRsStuUvx")
    index = 0
    while index < len(before_module):
        token = before_module[index]
        if token in argument_options:
            index += 1
            if index >= len(before_module) or before_module[index].startswith("-"):
                return False
        elif token.startswith("-W") and token != "-W":
            pass
        elif token.startswith("-X") and token != "-X":
            pass
        elif token.startswith("--check-hash-based-pycs="):
            pass
        elif token in long_flags:
            pass
        elif token.startswith("-") and not token.startswith("--"):
            if not token[1:] or any(char not in short_flag_chars for char in token[1:]):
                return False
        else:
            return False
        index += 1
    return True


def _parse_marker(raw: str) -> dict | None:
    """Parse a marker snapshot, including the legacy two-line format."""
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        value = None
    if isinstance(value, dict):
        return value

    try:
        pid_line, created_line, *_ = raw.splitlines()
        return {
            "schema_version": 0,
            "owner_pid": int(pid_line.strip()),
            "created_at": float(created_line.strip()),
        }
    except (TypeError, ValueError):
        return None


def _read_marker_snapshot(path: Path) -> tuple[str, dict | None] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"bridge lease marker is unreadable: {path}") from exc
    return raw, _parse_marker(raw)


def _read_marker(path: Path) -> dict | None:
    snapshot = _read_marker_snapshot(path)
    return snapshot[1] if snapshot is not None else None


def read_quiesce_lease(marker: str | Path | None = None) -> dict | None:
    """Read one complete known lease document without deciding liveness.

    Raw marker parsing is intentionally private.  A caller that acts on a
    returned document must never accidentally authorize a future schema by
    selecting only the fields it currently understands.
    """
    path = marker_path() if marker is None else Path(marker)
    lease = _read_marker(path)
    if lease is not None and not _lease_document_is_structurally_valid(lease):
        raise RuntimeError(f"bridge lease marker has an unknown or invalid schema: {path}")
    return lease


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _lease_document_is_structurally_valid(lease: object) -> bool:
    """Return whether a parsed marker is a complete known lease document."""
    if not isinstance(lease, dict):
        return False
    owner_value = lease.get("owner_pid")
    if type(owner_value) is not int or owner_value <= 0:
        return False

    schema = lease.get("schema_version")
    if schema == 0:
        created_at = _finite_number(lease.get("created_at"))
        return bool(
            set(lease) == {"schema_version", "owner_pid", "created_at"}
            and created_at is not None
            and created_at > 0
        )
    if schema != SCHEMA_VERSION or set(lease) != _V1_LEASE_KEYS:
        return False

    lease_id = lease.get("lease_id")
    install_value = lease.get("install_root")
    created_at = lease.get("created_at")
    expires_at = lease.get("expires_at")
    handoff_until = lease.get("handoff_grace_until")
    return bool(
        isinstance(lease_id, str)
        and _LEASE_ID_RE.fullmatch(lease_id) is not None
        and isinstance(install_value, str)
        and install_value
        and type(created_at) is int
        and created_at > 0
        and type(expires_at) is int
        and type(handoff_until) is int
        and created_at <= handoff_until <= expires_at
        and expires_at - created_at <= MAX_LEASE_SECONDS
        and handoff_until - created_at <= MAX_HANDOFF_GRACE_SECONDS
    )


def _lease_owner_is_live(
    owner_pid: int,
    created_at: float,
    *,
    pid_alive: Callable[[int], bool],
    pid_create_time: Callable[[int], float | None] | None,
) -> bool:
    """Bind a live numeric PID to the process that could create the lease."""
    try:
        if not pid_alive(owner_pid):
            return False
    except Exception:
        # An inspection failure is not proof that the owner exited.
        return True

    create_time_probe = pid_create_time
    if create_time_probe is None:
        # Existing callers inject ``pid_alive`` for deterministic tests and
        # legacy compatibility. Production defaults add native PID-reuse
        # validation; injected liveness remains the complete test double unless
        # the caller also injects a creation-time probe.
        if pid_alive is not _pid_alive:
            return True
        create_time_probe = _pid_create_time
    try:
        started_at = _finite_number(create_time_probe(owner_pid))
    except Exception:
        return True
    if started_at is None or started_at <= 0:
        return True
    # ``created_at`` is serialized to whole seconds. A process starting at or
    # beyond the next full second definitely did not write this lease.
    return started_at < created_at + _OWNER_START_TOLERANCE_SECONDS


def live_quiesce_lease(
    marker: str | Path | None = None,
    *,
    install_root: str | Path | None = None,
    now: float | None = None,
    pid_alive: Callable[[int], bool] = _pid_alive,
    pid_create_time: Callable[[int], float | None] | None = None,
) -> dict | None:
    """Return a validated active lease, otherwise ``None``.

    A schema-v1 lease is scoped to one canonical install root and bounded to
    twenty minutes.  It remains active while its owner lives, or briefly after
    owner exit during the explicit handoff grace window.  The grace closes the
    drain-to-updater race without turning a crashed updater into a permanent
    bridge outage.
    """
    path = marker_path() if marker is None else Path(marker)
    root = Path(install_root) if install_root is not None else infer_install_root()
    if root is None:
        return None
    current_time = _finite_number(time.time() if now is None else now)
    if current_time is None or current_time <= 0:
        return None

    # Renewal publishes an active shadow before moving the primary. Read a
    # stable primary+shadow generation: a reader that misses the old primary
    # and then loses its listed shadow to cleanup must re-read the new primary,
    # never report a false clear. Continuous churn is an explicit fail-closed
    # probe error rather than permission to import native modules.
    for _attempt in range(4):
        before = _read_marker_snapshot(path)
        if before is not None:
            active = _validate_active_lease(
                before[1] or {},
                marker=path,
                root=root,
                current_time=current_time,
                pid_alive=pid_alive,
                pid_create_time=pid_create_time,
            )
            if active is not None:
                return active
        recovery_before = _lease_recovery_paths(path)
        for candidate in recovery_before:
            snapshot = _read_marker_snapshot(candidate)
            if snapshot is None:
                continue
            if not _lease_document_is_structurally_valid(snapshot[1]):
                raise RuntimeError("bridge lease recovery artifact is malformed")
            active = _validate_active_lease(
                snapshot[1],
                marker=path,
                root=root,
                current_time=current_time,
                pid_alive=pid_alive,
                pid_create_time=pid_create_time,
                force_until_expiry=candidate.name.startswith(
                    f"{path.name}.cas-emergency-"
                ),
            )
            if active is not None:
                return active
            candidate_root = snapshot[1].get("install_root")
            candidate_expiry = _finite_number(snapshot[1].get("expires_at"))
            if (
                isinstance(candidate_root, str)
                and _canonical(candidate_root) == _canonical(root)
                and candidate_expiry is not None
                and current_time <= candidate_expiry
            ):
                raise RuntimeError("bridge lease recovery is pending")
        after = _read_marker_snapshot(path)
        if after is not None:
            active = _validate_active_lease(
                after[1] or {},
                marker=path,
                root=root,
                current_time=current_time,
                pid_alive=pid_alive,
                pid_create_time=pid_create_time,
            )
            if active is not None:
                return active
        recovery_after = _lease_recovery_paths(path)
        before_raw = before[0] if before is not None else None
        after_raw = after[0] if after is not None else None
        if before_raw == after_raw and {
            str(candidate) for candidate in recovery_before
        } == {str(candidate) for candidate in recovery_after}:
            if after is not None and not _lease_document_is_structurally_valid(
                after[1]
            ):
                if not _malformed_marker_is_bounded_stale(
                    path, now=current_time
                ):
                    raise RuntimeError(
                        "bridge lease marker is malformed and not stale"
                    )
            return None
    raise RuntimeError("bridge lease marker changed continuously during validation")


def _validate_active_lease(
    lease: dict,
    *,
    marker: Path,
    root: Path,
    current_time: float,
    pid_alive: Callable[[int], bool],
    pid_create_time: Callable[[int], float | None] | None,
    force_until_expiry: bool = False,
) -> dict | None:
    """Validate one already-read primary or recovery lease snapshot."""

    # Ownership/action decisions are permitted only for the exact legacy or
    # schema-v1 shape.  Checking selected fields is insufficient: a future or
    # foreign writer may add semantics that this runtime does not understand.
    if not _lease_document_is_structurally_valid(lease):
        return None

    try:
        owner_pid = int(lease["owner_pid"])
    except (KeyError, TypeError, ValueError):
        return None
    created_at = _finite_number(lease.get("created_at"))
    if owner_pid <= 0 or created_at is None or created_at <= 0:
        return None

    if lease.get("schema_version") == 0:
        # The old format carried no root. It is safe only for the historical
        # default layout where the marker's home owns ``hermes-agent``.
        legacy_root = marker.parent / "hermes-agent"
        age = current_time - created_at
        if _canonical(root) != _canonical(legacy_root):
            return None
        if age < -_CLOCK_SKEW_SECONDS or age > MAX_LEASE_SECONDS:
            return None
        return (
            lease
            if _lease_owner_is_live(
                owner_pid,
                created_at,
                pid_alive=pid_alive,
                pid_create_time=pid_create_time,
            )
            else None
        )

    if lease.get("schema_version") != SCHEMA_VERSION:
        return None
    lease_id = lease.get("lease_id")
    install_value = lease.get("install_root")
    expires_at = _finite_number(lease.get("expires_at"))
    handoff_until = _finite_number(lease.get("handoff_grace_until"))
    if not isinstance(lease_id, str) or _LEASE_ID_RE.fullmatch(lease_id) is None:
        return None
    if not isinstance(install_value, str) or not install_value:
        return None
    if expires_at is None or handoff_until is None:
        return None
    if _canonical(install_value) != _canonical(root):
        return None
    if created_at > current_time + _CLOCK_SKEW_SECONDS:
        return None
    if not (created_at <= handoff_until <= expires_at):
        return None
    if expires_at - created_at > MAX_LEASE_SECONDS:
        return None
    if handoff_until - created_at > MAX_HANDOFF_GRACE_SECONDS:
        return None
    if current_time > expires_at:
        return None
    if force_until_expiry:
        if expires_at - created_at > EMERGENCY_LEASE_SECONDS:
            return None
        # Emergency shadows are recovery gates, not adoptable ownership.
        # They remain active for their full bounded expiry even after the
        # failed updater dies, giving contained children time to unwind.
        return lease
    owner_live = _lease_owner_is_live(
        owner_pid,
        created_at,
        pid_alive=pid_alive,
        pid_create_time=pid_create_time,
    )
    if not owner_live and current_time > handoff_until:
        return None
    return lease


def live_quiesce_marker(
    marker: str | Path | None = None,
    *,
    install_root: str | Path | None = None,
) -> bool:
    """Compatibility boolean wrapper for older bridge tests/callers."""
    return live_quiesce_lease(marker, install_root=install_root) is not None


def _lease_recovery_paths(path: Path) -> list[Path]:
    try:
        return list(path.parent.glob(f"{path.name}.cas-*"))
    except OSError as exc:
        raise RuntimeError("bridge lease recovery directory is unreadable") from exc


def _pending_lease_recovery(path: Path, *, now: float) -> bool:
    """Block while a recent interrupted compare-and-swap needs recovery."""
    pending = False
    for candidate in _lease_recovery_paths(path):
        if candidate == path:
            return True
        try:
            snapshot = _read_marker_snapshot(candidate)
        except RuntimeError:
            return True
        if snapshot is None:
            continue
        raw, lease = snapshot
        if (
            isinstance(lease, dict)
            and lease.get("schema_version") == SCHEMA_VERSION
            and _lease_document_is_structurally_valid(lease)
        ):
            created = _finite_number(lease.get("created_at"))
            expires = _finite_number(lease.get("expires_at"))
            handoff = _finite_number(lease.get("handoff_grace_until"))
            try:
                owner_pid = int(lease.get("owner_pid", 0))
            except (TypeError, ValueError):
                owner_pid = 0
            well_formed = (
                created is not None
                and expires is not None
                and handoff is not None
                and created > 0
                and created <= handoff <= expires
                and expires - created <= MAX_LEASE_SECONDS
                and handoff - created <= MAX_HANDOFF_GRACE_SECONDS
                and owner_pid > 0
                and isinstance(lease.get("lease_id"), str)
                and _LEASE_ID_RE.fullmatch(lease["lease_id"]) is not None
                and isinstance(lease.get("install_root"), str)
                and bool(lease["install_root"])
            )
            emergency = candidate.name.startswith(f"{path.name}.cas-emergency-")
            retired = bool(
                well_formed
                and (
                    now > expires
                    or (
                        not emergency
                        and now > handoff
                        and not _pid_alive(owner_pid)
                    )
                )
            )
            if retired:
                tombstone = _move_lease_if_unchanged(candidate, raw)
                if tombstone is not None:
                    try:
                        tombstone.unlink()
                    except OSError:
                        pending = True
                    continue
                pending = True
                continue
        try:
            age = now - candidate.stat().st_mtime
        except OSError:
            return True
        if math.isfinite(age) and age > MAX_LEASE_SECONDS:
            try:
                candidate.unlink()
            except OSError:
                pending = True
        else:
            pending = True
    return pending


def _write_unpublished_exclusive(path: Path, raw: str) -> None:
    """Write and fsync a unique path that no lease reader enumerates."""
    descriptor = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        encoded = raw.encode("utf-8")
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("short write while claiming bridge lease")
            offset += written
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_exclusive(path: Path, raw: str) -> None:
    """Atomically publish complete bytes without overwriting existing state."""
    temporary = path.parent / f".hermes-lease-pending-{secrets.token_hex(16)}"
    _write_unpublished_exclusive(temporary, raw)
    try:
        os.link(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _publish_exclusive_atomic(path: Path, raw: str) -> None:
    """Compatibility name for atomic exclusive publication."""
    _write_exclusive(path, raw)


def _restore_lease_tombstone(tombstone: Path, path: Path) -> bool:
    """Restore exact tombstoned bytes without overwriting a newer marker."""
    try:
        os.link(tombstone, path)
    except FileExistsError:
        return False
    except OSError:
        try:
            raw = tombstone.read_text(encoding="utf-8")
            _write_exclusive(path, raw)
        except (FileExistsError, OSError):
            return False
    try:
        tombstone.unlink()
    except OSError:
        return False
    return True


def _move_lease_if_unchanged(path: Path, expected_raw: str) -> Path | None:
    tombstone = path.with_name(
        f"{path.name}.cas-previous-{os.getpid()}-{secrets.token_hex(8)}"
    )
    try:
        os.replace(path, tombstone)
        moved_raw = tombstone.read_text(encoding="utf-8")
    except OSError:
        return None
    if moved_raw == expected_raw:
        return tombstone
    _restore_lease_tombstone(tombstone, path)
    return None


def _replace_lease_if_unchanged(path: Path, expected_raw: str, new_raw: str) -> bool:
    # Publish the next valid state before moving the primary.  MCP launches
    # scan these same-directory shadows, which removes the false-clear window
    # inherent in rename-then-create compare-and-swap.
    shadow = path.with_name(
        f"{path.name}.cas-shadow-{os.getpid()}-{secrets.token_hex(8)}"
    )
    try:
        _write_exclusive(shadow, new_raw)
    except (FileExistsError, OSError):
        return False
    tombstone = _move_lease_if_unchanged(path, expected_raw)
    if tombstone is None:
        try:
            shadow.unlink()
        except OSError:
            # A retained active shadow is fail-closed and bounded by its own
            # expiry.  A future writer will refuse while recovery is pending.
            pass
        return False
    try:
        _write_exclusive(path, new_raw)
    except (FileExistsError, OSError):
        # Keep the tombstone if a concurrent claimant filled the path or the
        # durable write failed.  Writers scan it and fail closed; no foreign
        # evidence is silently discarded.
        return False
    try:
        tombstone.unlink()
    except OSError:
        # The new primary marker is valid, but retain/report the tombstone as
        # evidence of incomplete cleanup; a future writer will fail closed.
        return False
    try:
        shadow.unlink()
    except OSError:
        # The primary is valid, but renewal is not considered healthy until
        # all recovery evidence has been durably retired.
        return False
    return True


def write_emergency_quiesce_shadow(
    install_root: str | Path,
    *,
    lease_id: str,
    owner_pid: int | None = None,
    now: float | None = None,
) -> Path:
    """Publish a bounded fail-stop shadow without touching foreign primary bytes.

    This is used only when an updater loses its mutation lease.  The updater
    exits immediately, but a child installer may still be unwinding.  A
    two-minute handoff-grace shadow keeps new MCP bridges gated during that
    bounded fail-stop interval even if the primary was replaced by malformed
    or otherwise untrusted bytes.
    """
    current = _finite_number(time.time() if now is None else now)
    pid = os.getpid() if owner_pid is None else int(owner_pid)
    if current is None or current <= 0 or pid <= 0:
        raise ValueError("emergency lease identity and time must be valid")
    if not isinstance(lease_id, str) or _LEASE_ID_RE.fullmatch(lease_id) is None:
        raise ValueError("invalid emergency lease_id")
    created_at = int(current)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "lease_id": lease_id,
        "owner_pid": pid,
        "created_at": created_at,
        "expires_at": created_at + EMERGENCY_LEASE_SECONDS,
        "handoff_grace_until": created_at + MAX_HANDOFF_GRACE_SECONDS,
        "install_root": _canonical(install_root),
    }
    primary = marker_path()
    shadow = primary.with_name(
        f"{primary.name}.cas-emergency-{pid}-{secrets.token_hex(8)}"
    )
    primary.parent.mkdir(parents=True, exist_ok=True)
    _publish_exclusive_atomic(shadow, json.dumps(payload, sort_keys=True))
    return shadow


def _malformed_marker_is_bounded_stale(path: Path, *, now: float) -> bool:
    try:
        age = now - path.stat().st_mtime
    except OSError:
        return False
    return math.isfinite(age) and age > MAX_LEASE_SECONDS


def write_quiesce_lease(
    install_root: str | Path,
    *,
    marker: str | Path | None = None,
    lease_id: str | None = None,
    owner_pid: int | None = None,
    now: float | None = None,
    lifetime_seconds: float = MAX_LEASE_SECONDS,
    handoff_grace_seconds: float = DEFAULT_HANDOFF_GRACE_SECONDS,
    expected_owner_pid: int | None = None,
) -> dict:
    """Exclusively create, renew, or reclaim a bounded schema-v1 lease.

    Existing state is replaced only through an exact-byte compare-and-swap.
    A live, foreign, or recently malformed claim is never overwritten.
    ``expected_owner_pid`` is used only by explicit handoff adoption.
    """
    lifetime_value = _finite_number(lifetime_seconds)
    grace_value = _finite_number(handoff_grace_seconds)
    created_value = _finite_number(time.time() if now is None else now)
    if lifetime_value is None or grace_value is None or created_value is None:
        raise ValueError("lease times must be finite")
    if created_value <= 0:
        raise ValueError("created_at must be positive")
    lifetime = min(max(lifetime_value, 1.0), float(MAX_LEASE_SECONDS))
    grace = min(
        max(grace_value, 0.0),
        float(MAX_HANDOFF_GRACE_SECONDS),
        lifetime,
    )
    created_at = int(created_value)
    pid = os.getpid() if owner_pid is None else int(owner_pid)
    if pid <= 0:
        raise ValueError("owner_pid must be positive")
    identifier = lease_id or secrets.token_urlsafe(24)
    if not isinstance(identifier, str) or _LEASE_ID_RE.fullmatch(identifier) is None:
        raise ValueError(
            "lease_id must match [A-Za-z0-9._-]{16,128}"
        )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "lease_id": identifier,
        "owner_pid": pid,
        "created_at": created_at,
        "expires_at": created_at + int(lifetime),
        "handoff_grace_until": created_at + int(grace),
        "install_root": _canonical(install_root),
    }
    path = marker_path() if marker is None else Path(marker)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw_payload = json.dumps(payload, sort_keys=True)
    if _pending_lease_recovery(path, now=created_value):
        raise RuntimeError("bridge lease recovery is pending")

    snapshot = _read_marker_snapshot(path)
    if snapshot is None:
        if lease_id is not None:
            raise RuntimeError("bridge lease identity disappeared before renewal")
        try:
            _write_exclusive(path, raw_payload)
        except FileExistsError as exc:
            raise RuntimeError("bridge lease was concurrently claimed") from exc
        return payload

    expected_raw, current = snapshot
    if lease_id is not None:
        expected_owner = pid if expected_owner_pid is None else int(expected_owner_pid)
        if not (
            isinstance(current, dict)
            and _lease_document_is_structurally_valid(current)
            and current.get("schema_version") == SCHEMA_VERSION
            and current.get("lease_id") == identifier
            and current.get("owner_pid") == expected_owner
            and _canonical(str(current.get("install_root", "")))
            == _canonical(install_root)
        ):
            raise RuntimeError("bridge lease identity changed before renewal")
    else:
        # A well-formed claim is stale only after its own bounded liveness
        # check fails.  Malformed/unknown bytes are retained for one full max
        # lease lifetime before exact-byte recovery is allowed.
        active_root = (
            current.get("install_root")
            if isinstance(current, dict)
            and current.get("schema_version") == SCHEMA_VERSION
            and isinstance(current.get("install_root"), str)
            else path.parent / "hermes-agent"
        )
        if isinstance(current, dict) and current.get("schema_version") in {0, 1}:
            if live_quiesce_lease(path, install_root=active_root, now=created_value) is not None:
                raise RuntimeError("another updater owns the bridge quiesce lease")
        elif not _malformed_marker_is_bounded_stale(path, now=created_value):
            raise RuntimeError("bridge lease marker is malformed and not stale")

    if not _replace_lease_if_unchanged(path, expected_raw, raw_payload):
        raise RuntimeError("bridge lease changed during compare-and-swap")
    return payload


def adopt_quiesce_lease(
    install_root: str | Path,
    *,
    marker: str | Path | None = None,
    lease_id: str | None = None,
    owner_pid: int | None = None,
    now: float | None = None,
) -> dict | None:
    """Adopt a still-active handoff lease without changing its identity."""
    path = marker_path() if marker is None else Path(marker)
    active = live_quiesce_lease(path, install_root=install_root, now=now)
    if active is None or active.get("schema_version") != SCHEMA_VERSION:
        return None
    if lease_id is not None and active.get("lease_id") != lease_id:
        return None
    return write_quiesce_lease(
        install_root,
        marker=path,
        lease_id=str(active["lease_id"]),
        owner_pid=owner_pid,
        now=now,
        handoff_grace_seconds=0,
        expected_owner_pid=int(active["owner_pid"]),
    )


def clear_quiesce_lease(
    lease_id: str,
    *,
    owner_pid: int | None = None,
    marker: str | Path | None = None,
    install_root: str | Path,
) -> bool:
    """Remove a schema-v1 lease only while *lease_id* and owner still match."""
    path = marker_path() if marker is None else Path(marker)
    snapshot = _read_marker_snapshot(path)
    pid = os.getpid() if owner_pid is None else int(owner_pid)
    if snapshot is None:
        return False
    raw, lease = snapshot
    if (
        not isinstance(lease, dict)
        or not _lease_document_is_structurally_valid(lease)
        or lease.get("schema_version") != SCHEMA_VERSION
    ):
        return False
    if lease.get("lease_id") != lease_id or lease.get("owner_pid") != pid:
        return False
    if _canonical(str(lease.get("install_root", ""))) != _canonical(install_root):
        return False
    tombstone = _move_lease_if_unchanged(path, raw)
    if tombstone is None:
        return False
    try:
        tombstone.unlink()
    except OSError:
        return False
    return True


def should_quiesce_mcp_bridge(
    *,
    argv: Sequence[str] | None = None,
    marker: str | Path | None = None,
    install_root: str | Path | None = None,
    now: float | None = None,
    pid_alive: Callable[[int], bool] = _pid_alive,
) -> bool:
    """True only for the exact MCP module under a validated active lease."""
    original_argv = getattr(sys, "orig_argv", sys.argv) if argv is None else argv
    if not is_exact_mcp_module_argv(original_argv):
        return False
    return live_quiesce_lease(
        marker,
        install_root=install_root,
        now=now,
        pid_alive=pid_alive,
    ) is not None
