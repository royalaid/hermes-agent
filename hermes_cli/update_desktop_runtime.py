"""Windows Desktop plugin lifecycle support for ``hermes update``.

Only a launcher whose argv names a file inside ``HERMES_HOME/desktop-plugins``
is update-managed. A Python process merely using Hermes' venv stays an
external blocker. This distinction is critical on Windows because an in-place
dependency update under an external holder can corrupt native extensions.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)


def _argv_references_plugin(argv: list[str], plugin_root: Path) -> bool:
    """Return true only for argv values resolving below the plugin root."""
    for arg in argv:
        try:
            Path(arg).resolve().relative_to(plugin_root)
            return True
        except (OSError, ValueError):
            continue
    return False


def is_update_managed_desktop_plugin_command_line(command_line: str, hermes_home: Path) -> bool:
    """Return whether a command launches a managed Desktop plugin.

    The Electron preflight sees a flattened command line while the updater gets
    argv arrays from psutil. Normalize the former here so both flows trust the
    same narrow ownership rule: only launchers that reference a file below
    ``HERMES_HOME/desktop-plugins`` are update-managed.
    """
    if not isinstance(command_line, str) or not command_line.strip():
        return False
    try:
        argv = [part.strip('"') for part in shlex.split(command_line, posix=False)]
    except ValueError:
        return False
    return _argv_references_plugin(argv, hermes_home / "desktop-plugins")


def find_desktop_plugin_launchers(
    records: Iterable[dict[str, Any]], plugin_root: Path
) -> list[dict[str, Any]]:
    """Find top-level plugin launcher processes from a process-table snapshot."""
    try:
        root = plugin_root.resolve()
    except OSError:
        return []
    processes = {int(row["pid"]): row for row in records if row.get("pid")}
    managed = {
        pid
        for pid, row in processes.items()
        if _argv_references_plugin([str(a) for a in row.get("argv") or []], root)
    }
    return [
        processes[pid]
        for pid in sorted(managed)
        if int(processes[pid].get("ppid") or 0) not in managed
    ]


def pause_desktop_plugins_for_update(
    hermes_home: Path,
    *,
    process_iter: Callable[[], Iterable[Any]],
    terminate_tree: Callable[[int], None],
) -> dict[str, Any] | None:
    """Stop and snapshot managed plugin launcher trees for a Windows update."""
    plugin_root = hermes_home / "desktop-plugins"
    if not plugin_root.is_dir():
        return None

    records: list[dict[str, Any]] = []
    for proc in process_iter():
        try:
            info = proc.info
            records.append(
                {
                    "pid": int(info["pid"]),
                    "ppid": int(info.get("ppid") or 0),
                    "argv": [str(a) for a in info.get("cmdline") or []],
                    "cwd": str(info.get("cwd") or ""),
                }
            )
        except Exception:
            continue

    launches = []
    for record in find_desktop_plugin_launchers(records, plugin_root):
        argv = record["argv"]
        if not argv:
            continue
        try:
            terminate_tree(int(record["pid"]))
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.warning("Could not stop managed desktop plugin PID %s: %s", record["pid"], exc)
            continue
        launches.append({"pid": record["pid"], "argv": argv, "cwd": record["cwd"]})

    return {"resume_needed": True, "launches": launches} if launches else None


def resume_desktop_plugins_after_update(
    token: dict[str, Any] | None,
    *,
    popen: Callable[..., Any] = subprocess.Popen,
    popen_kwargs: dict[str, Any] | None = None,
) -> int:
    """Restart only plugin launcher commands captured by this update."""
    if not token or not token.get("resume_needed"):
        return 0
    token["resume_needed"] = False
    restarted = 0
    for launch in token.get("launches") or []:
        argv = list(launch.get("argv") or [])
        if not argv:
            continue
        try:
            popen(
                argv,
                cwd=launch.get("cwd") or None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **(popen_kwargs or {}),
            )
            restarted += 1
        except (OSError, ValueError) as exc:
            logger.warning("Could not restart managed desktop plugin %r: %s", argv[0], exc)
    return restarted
