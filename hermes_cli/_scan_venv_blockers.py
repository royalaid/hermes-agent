"""Strict, target-root process scanner for safe native-Windows updates.

The external interface always writes exactly one JSON document to stdout.
Valid clear and blocked scans exit zero for backwards compatibility with the
Desktop probe. Invalid roots and probe failures exit one and are fail-closed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Mapping, NoReturn, Sequence

from hermes_mcp_update_gate import MCP_MAIN_MODULE, is_exact_mcp_module_argv

SCHEMA_VERSION = 1
_CREATE_TIME_TOLERANCE_SECONDS = 0.01

# Long CLI flags whose argument value must be redacted from the cmdline.
_SENSITIVE_LONG_FLAGS: list[str] = [
    "--token",
    "--api-key",
    "--password",
    "--secret",
    "--authorization",
    "--access-key",
    "--private-key",
    "--session-key",
]


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


@dataclass(frozen=True)
class _ProcessSnapshot:
    pid: int
    ppid: int
    name: str
    exe: str
    argv: tuple[str, ...]
    created_at: float
    process: Any


def _canonical(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _within(path: str | Path, directory: str | Path) -> bool:
    try:
        return os.path.commonpath((_canonical(path), _canonical(directory))) == _canonical(
            directory
        )
    except (OSError, ValueError):
        return False


def _venv_dir(root: Path) -> Path:
    primary = root / "venv"
    if primary.exists() or not (root / ".venv").exists():
        return primary
    return root / ".venv"


def _validated_root(value: str | Path) -> tuple[Path, Path]:
    """Validate both the requested install and this scanner's provenance."""
    supplied = Path(value)
    if not supplied.is_absolute():
        raise ValueError("--root must be an absolute path")
    root = Path(_canonical(supplied))
    if not root.is_dir() or not (root / "hermes_cli").is_dir():
        raise ValueError("--root is not a Hermes installation")
    venv = _venv_dir(root)
    if not venv.is_dir():
        raise ValueError("target root has no venv")
    code_root = Path(__file__).resolve().parents[1]
    if _canonical(code_root) != _canonical(root):
        raise ValueError("scanner was not imported from the target root")
    if _canonical(sys.prefix) != _canonical(venv):
        raise ValueError("scanner is not running from the target root venv")
    return root, venv


def _base_result(
    *,
    root: str = "",
    venv: str = "",
    ok: bool = False,
    blocked: bool = True,
    reason: str | None = None,
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "scan",
        "ok": ok,
        "ready": bool(ok and not blocked),
        "blocked": blocked,
        "reason": reason,
        "root": root,
        "venv": venv,
        "processes": [],
        "mcp_bridges": [],
        "pausable_gateways": 0,
        "pausable_gateway_processes": [],
        "error": error,
    }


def _probe_fail_json(
    *, root: str = "", venv: str = "", code: str = "probe_failed", message: str = ""
) -> str:
    """Return the stable fail-closed probe envelope."""
    return json.dumps(
        _base_result(
            root=root,
            venv=venv,
            reason=code,
            error={"code": code, "message": message},
        )
    )


def _emit_probe_fail(
    diagnostic: str,
    *,
    root: str = "",
    venv: str = "",
    code: str = "probe_failed",
) -> NoReturn:
    print(_probe_fail_json(root=root, venv=venv, code=code, message=diagnostic))
    print(diagnostic, file=sys.stderr)
    raise SystemExit(1)


def _find_flag(text: str, flag: str) -> int:
    """Return the index of *flag* when it starts the string or follows space."""
    low = text.lower()
    fl = flag.lower()
    pos = 0
    while True:
        idx = low.find(fl, pos)
        if idx == -1:
            return -1
        if idx == 0 or text[idx - 1] == " ":
            return idx
        pos = idx + 1


def _redact_sensitive_cmdline(cmdline: str) -> str:
    """Apply generic secret redaction then conservative flag redaction."""
    try:
        from agent.redact import redact_sensitive_text  # noqa: PLC0415

        cmdline = redact_sensitive_text(cmdline, force=True)
    except Exception:
        return "<redacted>"

    earliest = len(cmdline)
    for flag in _SENSITIVE_LONG_FLAGS:
        idx = _find_flag(cmdline, flag + "=")
        if idx != -1 and idx + len(flag) + 1 < earliest:
            earliest = idx + len(flag) + 1
        idx = _find_flag(cmdline, flag + " ")
        if idx != -1 and idx + len(flag) + 1 < earliest:
            earliest = idx + len(flag) + 1
    if earliest < len(cmdline):
        return cmdline[:earliest] + "<redacted>"
    return cmdline


def _classify_local_preview_args(args: object) -> dict[str, object]:
    """Return safe UI metadata for an exact ``python -m http.server`` argv.

    The general holder detector intentionally truncates its diagnostic command
    line. Reading argv separately preserves a useful directory label without
    exposing an unbounded command line to the renderer.
    """
    if not isinstance(args, (list, tuple)) or not all(isinstance(arg, str) for arg in args):
        return {}

    # For a real interpreter module invocation, ``-m`` is the first argument
    # after the executable. A later ``-m http.server`` can merely be data passed
    # to an unrelated script and must never authorize termination.
    if len(args) < 3 or args[1] != "-m" or args[2].lower() != "http.server":
        return {}
    module_index = 1

    port = 8000
    if module_index + 2 < len(args):
        candidate = args[module_index + 2]
        if candidate.isdigit() and 0 < int(candidate) <= 65535:
            port = int(candidate)

    label = ""
    try:
        directory_index = args.index("--directory")
        if directory_index + 1 < len(args):
            label = PureWindowsPath(args[directory_index + 1]).name
    except ValueError:
        pass

    metadata: dict[str, object] = {
        "kind": "local-preview",
        "safeToStop": True,
        "port": port,
    }
    if label:
        metadata["label"] = label
    return metadata


def _local_preview_metadata(pid: int, name: str) -> dict[str, object]:
    if name.lower() not in {"python.exe", "pythonw.exe", "python", "pythonw"}:
        return {}
    try:
        import psutil  # noqa: PLC0415

        process = psutil.Process(pid)
        metadata = _classify_local_preview_args(process.cmdline())
        if metadata:
            metadata["createTime"] = process.create_time()
        return metadata
    except Exception:
        return {}


def _terminate_safe_preview(
    pid: int,
    expected_create_time: float,
    *,
    psutil_module: object | None = None,
) -> tuple[bool, str | None]:
    """Terminate one verified local preview process tree.

    A fresh ``psutil.Process`` identity check and exact argv classification occur
    immediately before termination. psutil guards mutating Process methods
    against PID reuse, avoiding taskkill's stale-PID race.
    """
    try:
        if psutil_module is None:
            import psutil as psutil_module  # type: ignore[no-redef]  # noqa: PLC0415

        process = psutil_module.Process(pid)  # type: ignore[attr-defined]
        if abs(process.create_time() - expected_create_time) > 0.001:
            return False, "process identity changed"
        if not _classify_local_preview_args(process.cmdline()):
            return False, "process is no longer a local preview"

        children = process.children(recursive=True)
        targets = [*reversed(children), process]
        for target in targets:
            target.terminate()
        _gone, alive = psutil_module.wait_procs(targets, timeout=3)  # type: ignore[attr-defined]
        for target in alive:
            target.kill()
        if alive:
            psutil_module.wait_procs(alive, timeout=2)  # type: ignore[attr-defined]
        return True, None
    except Exception as exc:
        return False, f"termination failed: {type(exc).__name__}"


def _tokens(argv: str | Sequence[str]) -> list[str]:
    if not isinstance(argv, str):
        return [str(value).strip('"') for value in argv]
    try:
        return [value.strip('"') for value in shlex.split(argv, posix=False)]
    except ValueError:
        return []


def _hermes_cli_tail(argv: str | Sequence[str]) -> list[str] | None:
    """Return the operative ``hermes_cli.main`` argv without global options."""
    parts = _tokens(argv)
    module_indexes = [
        index
        for index in range(1, len(parts) - 1)
        if parts[index] == "-m" and parts[index + 1] == "hermes_cli.main"
    ]
    if len(module_indexes) != 1:
        return None
    module_index = module_indexes[0]
    # A non-option before ``-m`` is a script operand unless it is the value of
    # a recognized Python runtime switch. Keep this parser deliberately
    # conservative: an unrecognized launch is a hard blocker, never killable.
    prefix = parts[1:module_index]
    index = 0
    while index < len(prefix):
        token = prefix[index]
        if token in {"-X", "-W", "--check-hash-based-pycs"}:
            index += 1
            if index >= len(prefix) or prefix[index].startswith("-"):
                return None
        elif token.startswith(("-X", "-W")) and token not in {"-X", "-W"}:
            pass
        elif token.startswith("--check-hash-based-pycs="):
            pass
        elif token.startswith("-") and not token.startswith("--"):
            if not token[1:] or any(char not in "bBdEIiOPqRsStuUvx" for char in token[1:]):
                return None
        else:
            return None
        index += 1
    tail = parts[module_index + 2 :]
    index = 0
    while index < len(tail):
        token = tail[index]
        if token in {"--profile", "-p"}:
            if index + 1 >= len(tail) or tail[index + 1].startswith("-"):
                return None
            index += 2
            continue
        if token.startswith("--profile="):
            if token == "--profile=":
                return None
            index += 1
            continue
        break
    return tail[index:]


def _hermes_cli_command(argv: str | Sequence[str]) -> str | None:
    """Return the exact Hermes subcommand token, never a substring match."""
    tail = _hermes_cli_tail(argv)
    if not tail or tail[0].startswith("-"):
        return None
    return tail[0].lower()


def _is_pausable_gateway(argv: str | Sequence[str]) -> bool:
    """Return only when live token boundaries prove ``gateway run``.

    A running gateway shows up in the venv-holder scan as one or both halves
    of its launcher/worker chain (``venv\\Scripts\\python.exe -m
    hermes_cli.main gateway run`` and the uv-side interpreter re-running the
    same argv). Reporting those as blockers dead-ends the Desktop update:
    the preflight aborts with ``venv-blocked`` *before* spawning
    ``hermes-setup``, so the CLI updater's own
    ``_pause_windows_gateways_for_update()`` — which exists precisely to
    stop these processes (and is always active: ``hermes-setup`` invokes
    ``hermes update --yes --gateway``) — never gets the chance to run.

    Only gateway invocations are exempted. Anything else running from the
    venv (an operator's REPL, a stray script, a ``serve`` backend that
    survived the desktop's own teardown) has no pause machinery downstream
    and must keep blocking the handoff.

    The tail parser is profile-selector aware on purpose: a hand-rolled
    token scan regressed ``--profile gateway gateway run``, where the
    profile *value* shadowed the subcommand token.
    """
    tail = _hermes_cli_tail(argv)
    return bool(
        tail
        and len(tail) >= 2
        and tail[0].casefold() == "gateway"
        and tail[1].casefold() == "run"
    )


def _snapshot_for_pid(
    pid: int,
    *,
    parent_by_pid: Mapping[int, int] | None = None,
) -> _ProcessSnapshot | None:
    import psutil  # noqa: PLC0415

    try:
        process = psutil.Process(int(pid))
        argv_value = process.cmdline()
        exe = process.exe()
        created_at = float(process.create_time())
        ppid = int(
            process.ppid()
            if parent_by_pid is None
            else parent_by_pid.get(int(pid), 0)
        )
        name = str(process.name() or Path(str(exe)).name)
    except psutil.NoSuchProcess:
        return None
    if not isinstance(argv_value, (list, tuple)) or not isinstance(exe, str):
        raise RuntimeError(f"process {pid} returned invalid identity metadata")
    argv = tuple(str(value) for value in argv_value)
    if not exe or not argv or not math.isfinite(created_at) or created_at <= 0:
        raise RuntimeError(f"process {pid} returned incomplete identity metadata")
    return _ProcessSnapshot(
        pid=int(pid),
        ppid=ppid,
        name=name,
        exe=exe,
        argv=argv,
        created_at=created_at,
        process=process,
    )


def _mcp_role(
    snapshot: _ProcessSnapshot,
    root: Path,
    wrappers: set[int],
    snapshots: dict[int, _ProcessSnapshot],
) -> tuple[str, int | None] | None:
    if not is_exact_mcp_module_argv(snapshot.argv):
        return None
    venv = _venv_dir(root)
    managed = root / ".hermes-runtime" / "python"
    if _within(snapshot.exe, venv):
        return "mcp_bridge_wrapper", snapshot.pid
    if _within(snapshot.exe, managed):
        parent = snapshot.ppid if snapshot.ppid in wrappers else None
        return "mcp_bridge_worker", parent

    # A uv/base-Python trampoline is actionable only while its relationship
    # to a verified target-venv wrapper remains live and visible.
    ancestor = snapshot.ppid
    seen: set[int] = set()
    while ancestor and ancestor not in seen:
        if ancestor in wrappers:
            return "mcp_bridge_worker", ancestor
        seen.add(ancestor)
        parent = snapshots.get(ancestor)
        if parent is None:
            break
        ancestor = parent.ppid
    return None


def _mcp_record(
    snapshot: _ProcessSnapshot,
    *,
    role: str,
    wrapper_pid: int | None,
    owner: str | None = None,
    parent_by_pid: Mapping[int, int] | None = None,
) -> dict[str, Any]:
    if owner is None:
        owner = (
            _owner_from_ancestry(snapshot)
            if parent_by_pid is None
            else _owner_from_ancestry(
                snapshot,
                parent_by_pid=parent_by_pid,
            )
        )
    actionable = owner in {"codex", "claude"}
    record: dict[str, Any] = {
        "pid": snapshot.pid,
        "name": snapshot.name,
        "cmdline": _redact_sensitive_cmdline(" ".join(snapshot.argv))[:120],
        "created_at": snapshot.created_at,
        "owner": owner,
        "role": role,
        "actionable": actionable,
        "actionability": "exact_mcp_bridge" if actionable else "hard_block",
        "action": "terminate_exact_mcp" if actionable else "refuse",
    }
    if wrapper_pid is not None and wrapper_pid != snapshot.pid:
        record["wrapper_pid"] = wrapper_pid
    return record


def _owner_from_ancestry(
    snapshot: _ProcessSnapshot,
    *,
    parent_by_pid: Mapping[int, int] | None = None,
) -> str:
    """Attribute a bridge only when a live ancestor proves the owner."""
    if parent_by_pid is None:
        try:
            parents = snapshot.process.parents()
        except Exception:
            return "unknown"
    else:
        import psutil  # noqa: PLC0415

        parents = []
        ancestor_pid = snapshot.ppid
        seen: set[int] = set()
        while ancestor_pid > 0 and ancestor_pid not in seen:
            seen.add(ancestor_pid)
            try:
                parents.append(psutil.Process(ancestor_pid))
            except Exception:
                pass
            try:
                ancestor_pid = int(parent_by_pid.get(ancestor_pid, 0))
            except (TypeError, ValueError):
                break
    for parent in parents:
        try:
            name = str(parent.name() or "").lower()
            exe = str(parent.exe() or "").lower()
            argv = [str(value).lower() for value in (parent.cmdline() or [])]
        except Exception:
            continue
        basenames = {
            Path(name).stem.lower(),
            Path(exe).stem.lower(),
            *(Path(value.strip('"')).stem.lower() for value in argv[:1]),
        }
        if basenames & {"codex", "codex-cli"}:
            return "codex"
        if basenames & {"claude", "claude-code"}:
            return "claude"
        joined = " ".join(argv)
        normalized_argv = joined.replace("\\", "/")
        if "/@anthropic-ai/claude-code/cli.js" in normalized_argv:
            return "claude"
        if "apps\\desktop" in joined or "apps/desktop" in joined:
            return "desktop"
    return "unknown"


def _generic_record(
    pid: int,
    name: str,
    cmdline: str,
    snapshot: _ProcessSnapshot | None,
) -> dict[str, Any]:
    command = _hermes_cli_command(snapshot.argv if snapshot else cmdline)
    owner = "desktop" if command in {"serve", "dashboard"} else "unknown"
    role = "desktop_backend" if owner == "desktop" else "other"
    record: dict[str, Any] = {
        "pid": int(pid),
        "name": str(name),
        "cmdline": _redact_sensitive_cmdline(cmdline)[:120],
        "owner": owner,
        "role": role,
        "actionable": False,
        "actionability": "hard_block",
        "action": "refuse",
    }
    if snapshot is not None:
        record["created_at"] = snapshot.created_at
    # Safe-to-stop local previews (``python -m http.server``) still carry their
    # UI metadata. This is display/affordance data only — the record stays a
    # hard block for the updater; stopping one goes through the separate
    # ``--terminate-safe`` path, which re-verifies identity and argv itself.
    record.update(_local_preview_metadata(int(pid), str(name)))
    return record


def scan_venv_blockers(root: str | Path) -> dict[str, Any]:
    """Return a strict, typed blocker snapshot for a validated target root."""
    target_root, venv = _validated_root(root)
    try:
        import psutil  # noqa: PLC0415
        from hermes_cli.update_cmd import (  # noqa: PLC0415
            _detect_venv_python_processes,
        )

        parent_by_pid: dict[int, int] | None = None
        ppid_map_fn = getattr(psutil, "_ppid_map", None)
        if callable(ppid_map_fn):
            parent_by_pid = {
                int(pid): int(ppid) for pid, ppid in ppid_map_fn().items()
            }
        detector_kwargs: dict[str, Any] = {}
        if parent_by_pid is not None:
            detector_kwargs["_parent_by_pid"] = parent_by_pid
        matches = _detect_venv_python_processes(
            root=target_root,
            strict=True,
            **detector_kwargs,
        )
    except Exception as exc:
        raise RuntimeError(f"scan aborted: {exc}") from exc

    by_pid: dict[int, tuple[str, str]] = {
        int(pid): (str(name), str(cmdline)) for pid, name, cmdline in matches
    }
    snapshots: dict[int, _ProcessSnapshot] = {}
    unreadable: set[int] = set()
    for pid in by_pid:
        try:
            snapshot = (
                _snapshot_for_pid(pid)
                if parent_by_pid is None
                else _snapshot_for_pid(
                    pid,
                    parent_by_pid=parent_by_pid,
                )
            )
        except Exception:
            unreadable.add(pid)
            continue
        if snapshot is not None:
            snapshots[pid] = snapshot

    wrappers = {
        pid
        for pid, snapshot in snapshots.items()
        if is_exact_mcp_module_argv(snapshot.argv)
        and _within(snapshot.exe, venv)
    }
    mcp_bridges: list[dict[str, Any]] = []
    processes: list[dict[str, Any]] = []
    gateways: list[dict[str, Any]] = []
    owner_by_parent_pid: dict[int, str] = {}
    for pid, (scanned_name, scanned_cmdline) in by_pid.items():
        snapshot = snapshots.get(pid)
        if snapshot is None and pid not in unreadable:
            continue  # exited between enumeration and identity read
        if snapshot is not None:
            classified = _mcp_role(snapshot, target_root, wrappers, snapshots)
            if classified is not None:
                role, wrapper_pid = classified
                owner_anchor = (
                    snapshots.get(wrapper_pid, snapshot)
                    if wrapper_pid is not None
                    else snapshot
                )
                owner = owner_by_parent_pid.get(owner_anchor.ppid)
                if owner is None:
                    owner = (
                        _owner_from_ancestry(owner_anchor)
                        if parent_by_pid is None
                        else _owner_from_ancestry(
                            owner_anchor,
                            parent_by_pid=parent_by_pid,
                        )
                    )
                    owner_by_parent_pid[owner_anchor.ppid] = owner
                mcp_bridges.append(
                    _mcp_record(
                        snapshot,
                        role=role,
                        wrapper_pid=wrapper_pid,
                        owner=owner,
                        parent_by_pid=parent_by_pid,
                    )
                )
                continue
            live_argv = snapshot.argv
            live_cmdline = " ".join(live_argv)
        else:
            live_cmdline = scanned_cmdline
            # A target-root candidate whose live exe/argv/create-time cannot
            # be re-read is never downstream-actionable. In particular, do
            # not exempt a captured ``gateway run`` string after AccessDenied:
            # without live identity it is an unverified hard blocker.
            processes.append(
                _generic_record(pid, scanned_name, live_cmdline, snapshot=None)
            )
            continue
        if _is_pausable_gateway(live_argv):
            gateway = _generic_record(pid, scanned_name, live_cmdline, snapshot)
            gateway.update(
                {
                    "owner": "gateway",
                    "role": "gateway_run",
                    "actionable": False,
                    "actionability": "downstream_drainable",
                    "action": "pause_downstream",
                }
            )
            gateways.append(gateway)
            continue
        processes.append(_generic_record(pid, scanned_name, live_cmdline, snapshot))

    # Workers first: terminating a wrapper first can destroy the ancestry proof
    # required to authorize an external base-interpreter child.
    mcp_bridges.sort(
        key=lambda item: (item.get("role") != "mcp_bridge_worker", item["pid"])
    )
    processes.sort(key=lambda item: item["pid"])
    gateways.sort(key=lambda item: item["pid"])
    blocked = bool(processes or mcp_bridges)
    result = _base_result(
        root=str(target_root),
        venv=str(venv),
        ok=True,
        blocked=blocked,
        reason="processes_running" if blocked else None,
    )
    result.update(
        {
            "processes": processes,
            "mcp_bridges": mcp_bridges,
            "pausable_gateways": len(gateways),
            "pausable_gateway_processes": gateways,
        }
    )
    return result


def _live_mcp_bridge_process(
    root: Path,
    pid: int,
) -> tuple[Any, dict[str, Any]] | None:
    snapshot = _snapshot_for_pid(pid)
    if snapshot is None:
        return None
    snapshots = {snapshot.pid: snapshot}
    wrappers: set[int] = set()
    if is_exact_mcp_module_argv(snapshot.argv) and _within(snapshot.exe, _venv_dir(root)):
        wrappers.add(snapshot.pid)

    # Rebuild the live ancestry needed for an external uv/base worker. A
    # managed-runtime worker remains owned by the target root after its wrapper
    # exits, so no ancestry is needed for that case.
    try:
        parents = snapshot.process.parents()
    except Exception:
        parents = []
    for parent_process in parents:
        try:
            parent = _snapshot_for_pid(int(parent_process.pid))
        except Exception:
            continue
        if parent is None:
            continue
        snapshots[parent.pid] = parent
        if is_exact_mcp_module_argv(parent.argv) and _within(
            parent.exe, _venv_dir(root)
        ):
            wrappers.add(parent.pid)
    classified = _mcp_role(snapshot, root, wrappers, snapshots)
    if classified is None:
        return None
    role, wrapper_pid = classified
    return snapshot.process, _mcp_record(
        snapshot, role=role, wrapper_pid=wrapper_pid
    )


def terminate_mcp_bridge(
    root: str | Path,
    *,
    pid: int,
    created_at: float,
) -> bool:
    """Kill exactly one still-identical MCP bridge PID; never a process tree."""
    target_root, _venv = _validated_root(root)
    try:
        expected_created_at = float(created_at)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(expected_created_at) or expected_created_at <= 0:
        return False
    try:
        live = _live_mcp_bridge_process(target_root, int(pid))
    except Exception:
        return False
    if live is None:
        return False
    process, record = live
    if (
        record.get("owner") not in {"codex", "claude"}
        or record.get("role")
        not in {"mcp_bridge_wrapper", "mcp_bridge_worker"}
        or record.get("actionable") is not True
        or record.get("action") != "terminate_exact_mcp"
    ):
        return False
    if abs(float(record["created_at"]) - expected_created_at) > _CREATE_TIME_TOLERANCE_SECONDS:
        return False
    try:
        process.kill()
    except Exception:
        return False
    return True


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(add_help=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--terminate-mcp-bridge", type=int)
    parser.add_argument("--created-at", type=float)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Print one JSON document; exit 0 for valid scan/action, 1 on failure."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _build_parser().parse_args(raw_argv)
    except (ValueError, argparse.ArgumentError) as exc:
        _emit_probe_fail(str(exc), code="invalid_arguments")

    root_text = str(args.root)
    terminate_requested = args.terminate_mcp_bridge is not None
    created_requested = args.created_at is not None
    if terminate_requested != created_requested:
        _emit_probe_fail(
            "--terminate-mcp-bridge and --created-at must be supplied together",
            root=root_text,
            code="invalid_arguments",
        )

    if terminate_requested:
        try:
            target_root, venv = _validated_root(root_text)
            terminated = terminate_mcp_bridge(
                target_root,
                pid=args.terminate_mcp_bridge,
                created_at=args.created_at,
            )
        except Exception as exc:
            _emit_probe_fail(str(exc), root=root_text, code="probe_failed")
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "mode": "terminate_mcp_bridge",
                    "ok": True,
                    "terminated": terminated,
                    "pid": args.terminate_mcp_bridge,
                    "created_at": args.created_at,
                    "root": str(target_root),
                    "venv": str(venv),
                    "error": None,
                }
            )
        )
        raise SystemExit(0)

    try:
        data = scan_venv_blockers(root_text)
    except ValueError as exc:
        _emit_probe_fail(str(exc), root=root_text, code="invalid_root")
    except Exception as exc:
        _emit_probe_fail(str(exc), root=root_text, code="probe_failed")
    print(json.dumps(data))
    raise SystemExit(0)


def _terminate_safe_main(argv: list[str]) -> NoReturn:
    if len(argv) != 2:
        print(json.dumps({"ok": False, "error": "expected pid and create time"}))
        raise SystemExit(2)
    try:
        pid = int(argv[0])
        create_time = float(argv[1])
        if pid <= 0 or not math.isfinite(create_time) or create_time <= 0:
            raise ValueError
    except ValueError:
        print(json.dumps({"ok": False, "error": "invalid process identity"}))
        raise SystemExit(2)

    stopped, error = _terminate_safe_preview(pid, create_time)
    print(json.dumps({"ok": stopped, "pid": pid, "error": error}))
    raise SystemExit(0 if stopped else 1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--terminate-safe":
        _terminate_safe_main(sys.argv[2:])
    main()
