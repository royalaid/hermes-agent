"""Live Windows host PATH overlay for kanban workers.

A gateway launched from a GUI/updater chain inherits that chain's environment
snapshot. On the reproducing host the worker's PATH was ``<venv>;<HKLM Path>``
with the whole HKCU block (``%USERPROFILE%\\.local\\bin`` among it) missing, so
``claude`` was ``command not found`` (exit 127) inside every worker while a
fresh shell resolved it. Python counterpart of the Desktop fix in #79726.

The merge is a pure function that takes the platform-dependent inputs as
data, so the precedence contract runs on every host. The tests that touch the
real registry or the real spawn path are ``windows_only``.
"""

import os
import sys

import pytest

from hermes_cli import windows_host_path as whp


MANAGED = r"C:\hermes\hermes-agent\venv\Scripts"
MACHINE = r"C:\Windows\system32;C:\Windows;C:\Program Files\Git\cmd"
USER = r"C:\Users\me\.local\bin;C:\Users\me\AppData\Roaming\npm"


def _entries(value):
    return value.split(";")


def test_precedence_managed_machine_user_then_inherited():
    inherited = f"{MANAGED};{MACHINE};C:\\stale\\only-in-snapshot"

    merged = whp.merge_windows_host_path(inherited, MACHINE, USER, managed=[MANAGED])

    assert _entries(merged) == [
        MANAGED,
        *_entries(MACHINE),
        *_entries(USER),
        r"C:\stale\only-in-snapshot",
    ]


def test_live_user_block_is_added_when_snapshot_lacks_it():
    """The reproducing shape: inherited = venv + Machine, no HKCU block."""
    inherited = f"{MANAGED};{MACHINE}"

    merged = whp.merge_windows_host_path(inherited, MACHINE, USER, managed=[MANAGED])

    assert r"C:\Users\me\.local\bin" in _entries(merged)
    # Machine entries still precede User entries, as Windows composes them.
    assert _entries(merged).index(r"C:\Windows\system32") < _entries(merged).index(
        r"C:\Users\me\.local\bin"
    )


def test_dedupe_is_case_insensitive_and_first_occurrence_wins():
    inherited = r"c:\windows\SYSTEM32;C:\Users\me\.LOCAL\bin"

    merged = whp.merge_windows_host_path(inherited, MACHINE, USER)

    lowered = [entry.casefold() for entry in _entries(merged)]
    assert len(lowered) == len(set(lowered))
    # The live Machine spelling wins over the inherited one because it comes first.
    assert r"C:\Windows\system32" in _entries(merged)
    assert r"c:\windows\SYSTEM32" not in _entries(merged)


def test_empty_entries_are_dropped():
    merged = whp.merge_windows_host_path(";;C:\\a;", "C:\\b;;", ";C:\\c")

    assert _entries(merged) == ["C:\\b", "C:\\c", "C:\\a"]


def test_machine_read_failure_keeps_inherited_verbatim():
    inherited = f"{MANAGED};{MACHINE};C:\\keep\\me"

    assert whp.merge_windows_host_path(inherited, None, USER, managed=[MANAGED]) == inherited


def test_user_read_failure_yields_managed_machine_inherited():
    inherited = f"C:\\keep\\me;{MANAGED}"

    merged = whp.merge_windows_host_path(inherited, MACHINE, None, managed=[MANAGED])

    assert _entries(merged) == [MANAGED, *_entries(MACHINE), r"C:\keep\me"]


def test_managed_entries_are_the_inherited_ones_under_managed_roots():
    inherited = (
        r"C:\hermes\node;C:\hermes\hermes-agent\venv\Scripts;C:\Windows;C:\hermes-other\bin"
    )

    managed = whp.managed_path_entries(inherited, [r"C:\hermes", r"C:\hermes\hermes-agent\venv"])

    assert managed == [r"C:\hermes\node", r"C:\hermes\hermes-agent\venv\Scripts"]


def test_overlay_is_a_no_op_off_windows():
    env = {"PATH": "keep", "HERMES_HOME": "/tmp/h"}

    result = whp.overlay_windows_host_path(
        env, read_host_path=lambda: (MACHINE, USER), is_windows=False
    )

    assert result is env
    assert env["PATH"] == "keep"


def test_overlay_keeps_the_callers_path_key_spelling():
    env = {"Path": f"{MANAGED};{MACHINE}", "VIRTUAL_ENV": r"C:\hermes\hermes-agent\venv"}

    whp.overlay_windows_host_path(env, read_host_path=lambda: (MACHINE, USER), is_windows=True)

    assert set(env) == {"Path", "VIRTUAL_ENV"}
    assert _entries(env["Path"])[0] == MANAGED
    assert r"C:\Users\me\.local\bin" in _entries(env["Path"])


def test_overlay_swallows_reader_errors():
    def _boom():
        raise RuntimeError("registry unavailable")

    env = {"PATH": "keep"}

    whp.overlay_windows_host_path(env, read_host_path=_boom, is_windows=True)

    assert env["PATH"] == "keep"


@pytest.mark.windows_only
def test_live_registry_read_returns_machine_path():
    machine, user = whp.read_windows_host_path()

    assert isinstance(machine, str) and machine
    assert user is None or (isinstance(user, str) and user)
    # Every live entry ends up in the merged PATH even from an empty snapshot.
    merged = whp.merge_windows_host_path("", machine, user)
    for entry in (machine + (";" + user if user else "")).split(";"):
        if entry:
            assert entry.casefold() in {e.casefold() for e in merged.split(";")}


@pytest.mark.windows_only
def test_worker_spawn_overlays_live_user_path(monkeypatch, tmp_path):
    """End-to-end through ``_spawn_worker``: the Popen env carries the live User block."""
    from hermes_cli import kanban_db as kb

    captured = {}

    class _Proc:
        pid = 4321

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _Proc()

    venv_scripts = os.path.join(sys.prefix, "Scripts")
    stale_snapshot = f"{venv_scripts};{MACHINE}"
    monkeypatch.setenv("PATH", stale_snapshot)
    monkeypatch.setattr(whp, "read_windows_host_path", lambda: (MACHINE, USER))
    monkeypatch.setattr("subprocess.Popen", _fake_popen)
    monkeypatch.setattr(kb, "_retag_legacy_worker_sessions", lambda _root: None)
    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: tmp_path / "logs")

    task = kb.Task(
        id="t_b21733fb",
        title="probe",
        body=None,
        assignee="default",
        status="in_progress",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )
    workspace = str(tmp_path / "ws")
    os.makedirs(workspace, exist_ok=True)

    kb._default_spawn(task, workspace)

    entries = _entries(captured["env"]["PATH"])
    assert entries[0] == venv_scripts
    assert r"C:\Users\me\.local\bin" in entries
    assert entries.index(r"C:\Windows\system32") < entries.index(r"C:\Users\me\.local\bin")
