"""Live Windows host ``PATH`` overlay for dispatcher-spawned workers.

A Windows process inherits the environment block of whatever started it. A
gateway launched from a GUI or updater chain (Desktop hand-off, Explorer, an
installer) therefore carries that chain's snapshot — which can predate ``PATH``
edits made after login, or omit the ``HKCU\\Environment`` ``Path`` block
entirely. Every worker the kanban dispatcher spawns copies ``os.environ``
verbatim, so a CLI that resolves in a fresh shell (``claude``, ``codex``, npm
shims under ``%USERPROFILE%``) is ``command not found`` inside the worker.

The fix mirrors the Desktop backend's live refresh (NousResearch/hermes-agent
#79726): read the current Machine and User ``Path`` values from the registry
and merge them in Windows precedence order, keeping Hermes-managed entries at
the front and the inherited snapshot as the tail.

Precedence and fallbacks::

    managed -> live Machine -> live User -> inherited (case-insensitive dedupe)
    Machine read fails  -> inherited PATH returned unchanged
    User read fails     -> managed -> live Machine -> inherited

Everything here is best-effort: no registry access ever raises out of
:func:`overlay_windows_host_path`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterable

_IS_WINDOWS = sys.platform == "win32"

_MACHINE_ENV_SUBKEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
_USER_ENV_SUBKEY = "Environment"


def _read_registry_path(hive: int, subkey: str) -> str | None:
    """Return the ``Path`` value under *hive*\\*subkey*, or ``None`` on any failure.

    ``REG_EXPAND_SZ`` references are expanded against the *current* process
    environment — the same caveat as the Desktop counterpart: a variable this
    process has never seen stays literal.
    """
    try:
        import winreg  # type: ignore[import-not-found]  # Windows-only stdlib module
    except ImportError:
        return None
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_QUERY_VALUE) as key:
            value, value_type = winreg.QueryValueEx(key, "Path")
    except OSError:
        return None
    if not isinstance(value, str):
        return None
    if value_type == winreg.REG_EXPAND_SZ:
        try:
            value = winreg.ExpandEnvironmentStrings(value)
        except OSError:
            pass
    value = value.strip()
    return value or None


def read_windows_host_path() -> tuple[str | None, str | None]:
    """Return ``(machine_path, user_path)`` from the live registry.

    Either element is ``None`` when its read fails or the value is empty.
    Off Windows both are ``None`` without touching the registry.
    """
    if not _IS_WINDOWS:
        return None, None
    import winreg  # type: ignore[import-not-found]

    machine = _read_registry_path(winreg.HKEY_LOCAL_MACHINE, _MACHINE_ENV_SUBKEY)
    user = _read_registry_path(winreg.HKEY_CURRENT_USER, _USER_ENV_SUBKEY)
    return machine, user


def _split_path(value: str | None) -> list[str]:
    return [entry for entry in (value or "").split(";") if entry]


def merge_windows_host_path(
    inherited: str | None,
    machine: str | None,
    user: str | None,
    managed: Iterable[str] = (),
) -> str:
    """Merge live registry ``PATH`` values over an inherited snapshot.

    Pure function so the precedence contract is testable on any host. Entries
    are compared case-insensitively; the first occurrence wins and empty
    entries are dropped. A failed Machine read (``None``) returns *inherited*
    verbatim rather than risk placing User entries ahead of Machine ones.
    """
    if machine is None:
        return inherited or ""

    ordered: list[str] = []
    seen: set[str] = set()
    for value in (";".join(managed), machine, user, inherited):
        for entry in _split_path(value):
            key = entry.casefold()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(entry)
    return ";".join(ordered)


def _path_key(env: dict) -> str | None:
    for key in env:
        if key.upper() == "PATH":
            return key
    return None


def _managed_roots(env: dict) -> list[str]:
    """Roots whose inherited ``PATH`` entries keep precedence over the live host PATH.

    The dispatcher's own interpreter prefix (the managed venv), ``VIRTUAL_ENV``,
    and every ``HERMES_HOME`` in play (the dispatcher's root and the worker's
    profile-scoped home) — the Windows analogue of the Desktop backend keeping
    its Node and virtualenv entries ahead of the host ``PATH``.
    """
    candidates = [
        sys.prefix,
        env.get("VIRTUAL_ENV"),
        env.get("HERMES_HOME"),
        os.environ.get("HERMES_HOME"),
    ]
    roots: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        normalized = os.path.normcase(os.path.normpath(candidate)).rstrip("\\/")
        if normalized and normalized not in roots:
            roots.append(normalized)
    return roots


def managed_path_entries(inherited: str | None, roots: Iterable[str]) -> list[str]:
    """Return the inherited entries that live under any of *roots*, in order."""
    normalized_roots = [
        os.path.normcase(os.path.normpath(root)).rstrip("\\/") for root in roots if root
    ]
    managed: list[str] = []
    for entry in _split_path(inherited):
        candidate = os.path.normcase(os.path.normpath(entry))
        for root in normalized_roots:
            if candidate == root or candidate.startswith(root + os.sep):
                managed.append(entry)
                break
    return managed


def overlay_windows_host_path(
    env: dict,
    *,
    read_host_path: Callable[[], tuple[str | None, str | None]] | None = None,
    is_windows: bool = _IS_WINDOWS,
) -> dict:
    """Overlay the live registry ``PATH`` onto *env* in place (Windows only).

    Returns *env* for convenience. The existing ``PATH`` key spelling is kept
    so a caller that spelled it ``Path`` keeps that key. Never raises: any
    failure leaves *env* untouched. *read_host_path* defaults to
    :func:`read_windows_host_path`, resolved at call time.
    """
    if not is_windows:
        return env
    try:
        key = _path_key(env) or "PATH"
        inherited = env.get(key, "")
        reader = read_host_path or read_windows_host_path
        machine, user = reader()
        managed = managed_path_entries(inherited, _managed_roots(env))
        env[key] = merge_windows_host_path(inherited, machine, user, managed)
    except Exception:
        pass
    return env
