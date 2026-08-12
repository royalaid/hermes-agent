"""Bridge quiescence and atomic Windows mutation containment."""

import logging
import math
import os
import secrets
import sys
import threading
import time as _time
from pathlib import Path
from typing import Callable, NoReturn

from hermes_cli.update_readiness import (
    _git_preflight_metadata,
    _print_update_readiness,
    _readiness_exit_code,
    _readiness_payload,
)
from hermes_cli.update_receipt import _IDENTIFIER_RE, _load_update_receipt
from hermes_cli.update_transaction import _UpdateTransaction


logger = logging.getLogger(__name__)

_DRAIN_CLEAR_INTERVAL_SECONDS = 0.5
_DRAIN_CLEAR_STABILITY_SECONDS = 1.5
_DRAIN_COOPERATIVE_WAIT_SECONDS = 1.0
_DEFAULT_DRAIN_TIMEOUT_SECONDS = 12.0
_DEFAULT_ATOMIC_DRAIN_TIMEOUT_SECONDS = 30.0


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
    wait_for_hard_processes: bool = False,
) -> dict[str, object]:
    """Drain actionable MCP records and prove a bounded stable-clear window."""
    from hermes_cli._scan_venv_blockers import (
        scan_venv_blockers,
        terminate_mcp_bridge,
    )
    from hermes_mcp_update_gate import marker_path, write_quiesce_lease

    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or not 0.1 <= timeout <= 120.0:
        raise ValueError("timeout_seconds must be between 0.1 and 120")
    deadline = _time.monotonic() + timeout
    clear_stability_seconds = (
        _DRAIN_CLEAR_STABILITY_SECONDS
        if wait_for_hard_processes
        else _DRAIN_CLEAR_INTERVAL_SECONDS
    )
    actions: list[dict] = []
    clear_since: float | None = None
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

    def _drain_timeout_payload() -> dict[str, object]:
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

    while True:
        if _time.monotonic() >= deadline:
            return _drain_timeout_payload()
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
        scan_completed_at = _time.monotonic()
        if scan_completed_at >= deadline:
            return _drain_timeout_payload()

        bridges = list(last_scan.get("mcp_bridges", []))
        hard_processes = list(last_scan.get("processes", []))
        unactionable = [entry for entry in bridges if not bool(entry.get("actionable"))]
        if unactionable and not allow_hard_processes:
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
                reason="mcp-owner-unverified",
            )
        if hard_processes and not allow_hard_processes:
            if not wait_for_hard_processes:
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
                    reason="venv-blocked",
                )
            # A scheduled status/presence helper can start after Desktop's
            # outer preflight and briefly hold this venv. It is never safe to
            # terminate or exempt: wait only for an observed natural exit,
            # then restart the full stable-clear proof. The shared drain
            # deadline keeps persistent or repeated holders fail-closed.
            clear_since = None
            actions = [
                action for action in actions if action.get("type") != "clear-scan"
            ]
            remaining = deadline - _time.monotonic()
            if remaining > 0:
                _time.sleep(min(_DRAIN_CLEAR_INTERVAL_SECONDS, remaining))
            continue

        actionable = [entry for entry in bridges if bool(entry.get("actionable"))]
        if actionable:
            clear_since = None
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
                if _time.monotonic() >= deadline:
                    return _drain_timeout_payload()
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

        clear_now = scan_completed_at
        if clear_since is None:
            clear_since = clear_now
            actions.append({"type": "clear-scan", "sequence": 1})
        elif clear_now - clear_since >= clear_stability_seconds:
            actions.append({"type": "clear-scan", "sequence": 2})
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


def _cmd_update_drain(args, *, root: Path) -> NoReturn:
    """Create a bounded standalone pause, never a later update handoff.

    A successful command intentionally leaves its owner-bound lease active
    only for the dead-owner handoff grace.  The public response omits the raw
    capability, so an independent update must refuse until that grace expires
    instead of claiming an unauthenticated transition is race-free.
    """
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


def _prepare_atomic_windows_update(
    args,
    *,
    root: Path,
    transaction: _UpdateTransaction,
) -> None:
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
        requested_timeout = float(
            getattr(args, "timeout_seconds", _DEFAULT_DRAIN_TIMEOUT_SECONDS)
        )
        # --timeout-seconds is the public standalone-drain bound. argparse
        # also materializes it on normal updates, where the atomic handoff
        # needs enough time for the scheduled 20-second gateway-status helper
        # plus stable-clear proof. Keep the standalone behavior unchanged and
        # enforce a 30-second floor only on this atomic path. Preserve invalid
        # programmatic values so the drain validator still rejects them.
        atomic_timeout = (
            max(requested_timeout, _DEFAULT_ATOMIC_DRAIN_TIMEOUT_SECONDS)
            if math.isfinite(requested_timeout)
            and 0.1 <= requested_timeout <= 120.0
            else requested_timeout
        )
        payload = _drain_under_update_lease(
            root,
            lease,
            branch=branch,
            timeout_seconds=atomic_timeout,
            allow_hard_processes=force_venv,
            wait_for_hard_processes=True,
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
        transaction.lease = lease
        transaction.invocation_id = invocation_id
        transaction.handoff_owner_pid = handoff_owner_pid
    except BaseException:
        _return_or_release_on_failure(lease)
        raise
