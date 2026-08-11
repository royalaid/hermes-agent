"""Tests for issue #26670 — concurrent hermes.exe detection and improved
quarantine retry / reboot-deferred fallback during `hermes update` on Windows.

These tests force ``_is_windows`` to return ``True`` via patching so the
Windows-specific code paths can be exercised on any host.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import main as cli_main
from hermes_cli import update_cmd


# Tests in this module either exercise the REAL _detect_concurrent_hermes_instances
# helper (and need the autouse stub in tests/hermes_cli/conftest.py disabled),
# or supply their own explicit return value via patch.object. Mark the whole
# module so the conftest fixture skips its default stub.
pytestmark = pytest.mark.real_concurrent_gate


# ---------------------------------------------------------------------------
# _detect_concurrent_hermes_instances
# ---------------------------------------------------------------------------


def _make_proc(pid: int, exe: str, name: str = "hermes.exe"):
    """Build a duck-typed psutil Process stand-in with the .info dict."""
    proc = MagicMock()
    proc.info = {"pid": pid, "exe": exe, "name": name}
    return proc




# ---------------------------------------------------------------------------
# Parent-chain exclusion (issue #30768 follow-up — the setuptools .exe
# launcher on Windows is a separate native process that spawns python.exe;
# excluding only ``os.getpid()`` flags the launcher as a concurrent instance.
# ---------------------------------------------------------------------------


def _fake_psutil_with_parent_chain(
    parent_chain: list[int],
    proc_iter_rows: list,
    *,
    ancestor_exe: str | None = None,
):
    """Build a psutil stand-in that has Process()/parents()/exe() AND process_iter().

    ``parent_chain`` is the ordered list of ancestor PIDs (closest first)
    returned by ``proc.parents()`` on the seed (``os.getpid()``).
    ``ancestor_exe`` is the executable path reported by each ancestor's
    ``.exe()``; when it matches one of our shim paths the ancestor is
    excluded (the launcher-shim case). Pass ``None`` to model an ancestor
    whose exe can't be read (psutil error) — it stays in the candidate set.
    """

    class _FakeProc:
        def __init__(self, pid: int, exe_path: str | None):
            self.pid = pid
            self._exe = exe_path

        def exe(self):
            if self._exe is None:
                raise OSError("exe unavailable")
            return self._exe

        def parents(self):
            return [_FakeProc(p, ancestor_exe) for p in parent_chain]

    class _NoSuchProcess(Exception):
        pass

    class _AccessDenied(Exception):
        pass

    def _process(pid=None):
        return _FakeProc(pid if pid is not None else os.getpid(), ancestor_exe)

    return types.SimpleNamespace(
        Process=_process,
        NoSuchProcess=_NoSuchProcess,
        AccessDenied=_AccessDenied,
        process_iter=lambda attrs: iter(proc_iter_rows),
    )


@patch.object(cli_main, "_is_windows", return_value=True)
def test_detect_concurrent_parents_call_robust_to_one_bad_hop(_winp, tmp_path):
    """The launcher shim is still excluded even when an ancestor exe is unreadable.

    Field regression (issues #29341, #34795): the old per-hop ``parent()``
    walk bailed on the FIRST psutil error, so an AccessDenied on any hop left
    the launcher shim in the candidate set and re-triggered the false
    positive. ``parents()`` returns the whole list at once; we evaluate each
    ancestor independently, so one unreadable hop never strands the launcher.
    """
    scripts_dir = tmp_path
    shim = scripts_dir / "hermes.exe"
    shim.write_bytes(b"")
    me = os.getpid()
    launcher_pid = me + 100

    rows = [
        _make_proc(me, str(shim), "python.exe"),
        _make_proc(launcher_pid, str(shim), "hermes.exe"),
    ]
    # ancestor_exe=None → every ancestor's .exe() raises OSError. The helper
    # must swallow it per-ancestor and not crash; the launcher won't be
    # excluded in this degenerate case, but a real run reads the shim exe.
    fake_psutil = _fake_psutil_with_parent_chain(
        parent_chain=[launcher_pid],
        proc_iter_rows=rows,
        ancestor_exe=None,
    )
    with patch.dict(sys.modules, {"psutil": fake_psutil}):
        result = cli_main._detect_concurrent_hermes_instances(scripts_dir)

    # No crash; helper completes. (Degenerate stub: launcher exe unreadable.)
    assert result == [(launcher_pid, "hermes.exe")]




# ---------------------------------------------------------------------------
# _format_concurrent_instances_message
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# _quarantine_running_hermes_exe — retry + reboot-deferred fallback
# ---------------------------------------------------------------------------


@patch.object(cli_main, "_is_windows", return_value=True)
def test_quarantine_succeeds_first_attempt(_winp, tmp_path):
    """When the rename works immediately, no warning, single rename pair returned."""
    shim = tmp_path / "hermes.exe"
    shim.write_bytes(b"old")

    pairs = cli_main._quarantine_running_hermes_exe(tmp_path)

    assert len(pairs) == 1
    orig, quarantine = pairs[0]
    assert orig == shim
    assert quarantine.name.startswith("hermes.exe.old.")
    assert quarantine.exists()
    assert not shim.exists()


@patch.object(cli_main, "_is_windows", return_value=True)
def test_quarantine_falls_back_to_reboot_schedule(_winp, tmp_path, capsys, monkeypatch):
    """When every retry fails, we schedule via MoveFileEx and warn helpfully."""
    shim = tmp_path / "hermes.exe"
    shim.write_bytes(b"locked")

    def always_fails(self, target):
        raise OSError(32, "The process cannot access the file (simulated lock)")

    scheduled_calls: list[tuple[Path, Path]] = []

    def fake_schedule(s: Path, q: Path) -> bool:
        scheduled_calls.append((s, q))
        return True

    monkeypatch.setattr(cli_main, "_hermes_exe_shims", lambda d: [shim])
    with patch.object(Path, "rename", always_fails), patch.object(
        cli_main, "_schedule_replace_on_reboot", fake_schedule
    ), patch("time.sleep", lambda *_a, **_k: None):
        pairs = cli_main._quarantine_running_hermes_exe(tmp_path)

    captured = capsys.readouterr().out

    # The reboot-deferred path was used.
    assert scheduled_calls and scheduled_calls[0][0] == shim
    # It is NOT added to the returned roll-back list (the issue calls this
    # out — don't undo a deferred operation).
    assert pairs == []
    # The user got a clear message, not raw [WinError 32].
    assert "scheduled" in captured.lower()
    assert "reboot" in captured.lower()




# ---------------------------------------------------------------------------
# Windows gateway pause/resume before update mutation
# ---------------------------------------------------------------------------


def _fake_gateway_psutil(
    records: dict[int, dict],
    *,
    parents: dict[int, int] | None = None,
    dead: set[int] | None = None,
    unreadable: set[int] | None = None,
):
    """psutil stand-in with exact argv/exe/cwd/create-time process state."""

    parent_map = parents or {}
    dead_pids = dead if dead is not None else set()
    unreadable_pids = unreadable if unreadable is not None else set()

    class NoSuchProcess(ProcessLookupError):
        pass

    class AccessDenied(PermissionError):
        pass

    class FakeProc:
        def __init__(self, pid=None):
            self.pid = os.getpid() if pid is None else int(pid)
            if self.pid in dead_pids:
                raise NoSuchProcess(self.pid)
            if self.pid != os.getpid() and self.pid not in records:
                raise NoSuchProcess(self.pid)

        def _record(self):
            if self.pid in unreadable_pids:
                raise AccessDenied(self.pid)
            return records[self.pid]

        def create_time(self):
            return self._record()["created_at"]

        def cmdline(self):
            return list(self._record()["argv"])

        def exe(self):
            return self._record()["exe"]

        def cwd(self):
            return self._record()["cwd"]

        def parent(self):
            parent_pid = parent_map.get(self.pid)
            return FakeProc(parent_pid) if parent_pid is not None else None

        def parents(self):
            return []

    return types.SimpleNamespace(
        Process=FakeProc,
        NoSuchProcess=NoSuchProcess,
        AccessDenied=AccessDenied,
    )


def _gateway_record(
    pid: int,
    *,
    exe: str | None = None,
    argv: list[str] | None = None,
    created_at: float | None = None,
    cwd: str | None = None,
) -> dict:
    executable = exe or str(
        cli_main.PROJECT_ROOT / ".hermes-runtime" / "python" / "python.exe"
    )
    return {
        "created_at": float(created_at if created_at is not None else 1000 + pid),
        "argv": argv
        or [
            executable,
            "-m",
            "hermes_cli.main",
            "gateway",
            "run",
        ],
        "exe": executable,
        "cwd": cwd or str(cli_main.PROJECT_ROOT),
    }


@patch.object(cli_main, "_is_windows", return_value=True)
def test_pause_windows_gateways_for_update_stops_profile_and_unmapped_pids(
    _winp,
    monkeypatch,
    tmp_path,
    capsys,
):
    import gateway.status as status_mod
    import hermes_cli.gateway as gateway_mod

    profile_home = tmp_path / "profiles" / "work"
    profile_home.mkdir(parents=True)
    profile_proc = SimpleNamespace(profile="work", path=profile_home, pid=101)

    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda **_k: [101, 202])
    monkeypatch.setattr(
        gateway_mod,
        "find_profile_gateway_processes",
        lambda **_k: [profile_proc],
    )
    monkeypatch.setattr(gateway_mod, "_get_restart_drain_timeout", lambda: 0.1)
    records = {101: _gateway_record(101), 202: _gateway_record(202)}
    monkeypatch.setitem(sys.modules, "psutil", _fake_gateway_psutil(records))
    waited_for = []

    def fake_wait(pids, *, timeout):
        waited_for.extend(pids)
        return set()

    monkeypatch.setattr(cli_main, "_wait_for_windows_update_gateway_exit", fake_wait)
    terminated = []
    monkeypatch.setattr(
        status_mod,
        "terminate_pid",
        lambda pid, force=False: terminated.append((pid, force)),
    )

    token = cli_main._pause_windows_gateways_for_update()

    assert token == {
        "resume_needed": True,
        "profiles": {"work": 101},
        "profile_identities": {
            "work": {"pid": 101, "created_at": records[101]["created_at"]}
        },
        "unmapped_pids": [202],
        "unmapped": [
            {
                "pid": 202,
                "argv": records[202]["argv"],
            }
        ],
        "cold_start_if_installed": False,
    }
    assert waited_for == [101]
    assert terminated == [(202, True)]

    marker = json.loads((profile_home / ".gateway-planned-stop.json").read_text())
    assert marker["target_pid"] == 101
    assert marker["stopper_pid"] == os.getpid()

    captured = capsys.readouterr().out
    assert "Paused gateway profile(s): work" in captured
    assert "without profile mapping" in captured
    # An unmapped PID whose argv we captured is respawnable, so we must NOT
    # tell the user to restart it manually.
    assert "Restart manually after update" not in captured


@patch.object(cli_main, "_is_windows", return_value=True)
def test_capture_gateway_identity_accepts_install_global_home_cwd(
    _winp,
    monkeypatch,
    tmp_path,
):
    """Managed gateways run from HERMES_HOME, one level above the checkout."""
    global_home = tmp_path / "home"
    install_root = global_home / "hermes-agent"
    global_home.mkdir()
    pid = 202
    records = {
        pid: _gateway_record(
            pid,
            exe=r"C:\Python311\python.exe",
            cwd=str(global_home),
        )
    }
    monkeypatch.setitem(sys.modules, "psutil", _fake_gateway_psutil(records))
    monkeypatch.setattr(update_cmd, "get_default_hermes_root", lambda: global_home)

    identity = update_cmd._capture_gateway_stop_identity(
        pid,
        role="gateway_worker",
        root=install_root,
    )

    assert identity is not None
    assert identity.working_directory == update_cmd._canonical_process_path(
        global_home
    )


@patch.object(cli_main, "_is_windows", return_value=True)
def test_pause_ignores_unmapped_gateway_from_another_install(
    _winp,
    monkeypatch,
    tmp_path,
):
    """Global discovery cannot make one Hermes install stop another's fleet."""
    import gateway.status as status_mod
    import hermes_cli.gateway as gateway_mod
    from hermes_cli import gateway_windows

    pid = 202
    target_home = tmp_path / "target-home"
    foreign_home = tmp_path / "foreign-home"
    records = {
        pid: _gateway_record(
            pid,
            exe=r"C:\Python311\python.exe",
            cwd=str(foreign_home),
        )
    }
    monkeypatch.setitem(sys.modules, "psutil", _fake_gateway_psutil(records))
    monkeypatch.setattr(update_cmd, "get_default_hermes_root", lambda: target_home)
    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda **_k: [pid])
    monkeypatch.setattr(gateway_mod, "find_profile_gateway_processes", lambda: [])
    monkeypatch.setattr(gateway_windows, "is_installed", lambda: False)
    terminated = []
    monkeypatch.setattr(
        status_mod,
        "terminate_pid",
        lambda value, force=False: terminated.append((value, force)),
    )
    captured = []

    token = cli_main._pause_windows_gateways_for_update(
        require_structured_resume=True,
        before_stop=lambda value: captured.append(value.copy()),
    )

    assert token == {
        "resume_needed": False,
        "profiles": {},
        "profile_identities": {},
        "unmapped_pids": [],
        "unmapped": [],
        "cold_start_if_installed": False,
    }
    assert captured == [token]
    assert terminated == []


@patch.object(cli_main, "_is_windows", return_value=True)
def test_pause_keeps_target_gateway_when_foreign_gateway_is_also_discovered(
    _winp,
    monkeypatch,
    tmp_path,
):
    """Filtering a foreign PID must not discard this install's mapped fleet."""
    import gateway.status as status_mod
    import hermes_cli.gateway as gateway_mod

    target_pid = 101
    foreign_pid = 202
    target_home = tmp_path / "target-home"
    profile_home = target_home / "profiles" / "work"
    profile_home.mkdir(parents=True)
    profile_proc = SimpleNamespace(
        profile="work",
        path=profile_home,
        pid=target_pid,
    )
    records = {
        target_pid: _gateway_record(target_pid),
        foreign_pid: _gateway_record(
            foreign_pid,
            exe=r"C:\Python311\python.exe",
            cwd=str(tmp_path / "foreign-home"),
        ),
    }
    monkeypatch.setitem(sys.modules, "psutil", _fake_gateway_psutil(records))
    monkeypatch.setattr(update_cmd, "get_default_hermes_root", lambda: target_home)
    monkeypatch.setattr(
        gateway_mod,
        "find_gateway_pids",
        lambda **_k: [target_pid, foreign_pid],
    )
    monkeypatch.setattr(
        gateway_mod,
        "find_profile_gateway_processes",
        lambda: [profile_proc],
    )
    monkeypatch.setattr(gateway_mod, "_get_restart_drain_timeout", lambda: 0.1)
    waited_for = []
    monkeypatch.setattr(
        cli_main,
        "_wait_for_windows_update_gateway_exit",
        lambda pids, *, timeout: waited_for.extend(pids) or set(),
    )
    terminated = []
    monkeypatch.setattr(
        status_mod,
        "terminate_pid",
        lambda value, force=False: terminated.append((value, force)),
    )

    token = cli_main._pause_windows_gateways_for_update(
        require_structured_resume=True
    )

    assert token is not None
    assert token["profiles"] == {"work": target_pid}
    assert token["unmapped_pids"] == []
    assert waited_for == [target_pid]
    assert terminated == []


@patch.object(cli_main, "_is_windows", return_value=True)
def test_pause_refuses_foreign_process_named_by_target_profile_state(
    _winp,
    monkeypatch,
    tmp_path,
):
    """A target PID file pointing outside the install remains fail-closed."""
    import gateway.status as status_mod
    import hermes_cli.gateway as gateway_mod

    pid = 202
    target_home = tmp_path / "target-home"
    foreign_home = tmp_path / "foreign-home"
    profile_proc = SimpleNamespace(
        profile="work",
        path=target_home / "profiles" / "work",
        pid=pid,
    )
    records = {
        pid: _gateway_record(
            pid,
            exe=r"C:\Python311\python.exe",
            cwd=str(foreign_home),
        )
    }
    monkeypatch.setitem(sys.modules, "psutil", _fake_gateway_psutil(records))
    monkeypatch.setattr(update_cmd, "get_default_hermes_root", lambda: target_home)
    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda **_k: [pid])
    monkeypatch.setattr(
        gateway_mod,
        "find_profile_gateway_processes",
        lambda: [profile_proc],
    )
    terminated = []
    monkeypatch.setattr(
        status_mod,
        "terminate_pid",
        lambda value, force=False: terminated.append((value, force)),
    )

    with pytest.raises(update_cmd._GatewayOutsideInstall):
        cli_main._pause_windows_gateways_for_update(require_structured_resume=True)

    assert terminated == []


@patch.object(cli_main, "_is_windows", return_value=True)
def test_pause_refuses_reused_unmapped_pid_before_force_stop(
    _winp,
    monkeypatch,
):
    """A discovery PID cannot authorize killing a later process with that PID."""
    import gateway.status as status_mod
    import hermes_cli.gateway as gateway_mod

    pid = 202
    reused = {"value": False}
    gateway_argv = [
        str(cli_main.PROJECT_ROOT / "venv" / "Scripts" / "python.exe"),
        "-m",
        "hermes_cli.main",
        "gateway",
        "run",
    ]

    class FakeProcess:
        def __init__(self, value):
            assert int(value) == pid
            self.pid = pid

        def create_time(self):
            return 200.0 if reused["value"] else 100.0

        def cmdline(self):
            return list(gateway_argv)

        def exe(self):
            return gateway_argv[0]

        def cwd(self):
            return str(cli_main.PROJECT_ROOT)

    class NoSuchProcess(Exception):
        pass

    fake_psutil = types.SimpleNamespace(
        Process=FakeProcess,
        NoSuchProcess=NoSuchProcess,
        AccessDenied=PermissionError,
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda **_k: [pid])
    monkeypatch.setattr(gateway_mod, "find_profile_gateway_processes", lambda: [])
    monkeypatch.setattr(gateway_mod, "_get_restart_drain_timeout", lambda: 0.1)
    def mark_reused(_pids, *, timeout):
        reused["value"] = True
        return set()

    monkeypatch.setattr(cli_main, "_wait_for_windows_update_gateway_exit", mark_reused)
    terminated = []
    monkeypatch.setattr(
        status_mod,
        "terminate_pid",
        lambda value, force=False: terminated.append((value, force)),
    )

    with pytest.raises(RuntimeError, match="changed before force-stop"):
        cli_main._pause_windows_gateways_for_update()

    assert terminated == []


@patch.object(cli_main, "_is_windows", return_value=True)
def test_pause_refuses_unreadable_identity_immediately_before_force_stop(
    _winp,
    monkeypatch,
):
    """AccessDenied after drain is a refusal, never bare-PID kill authority."""
    import gateway.status as status_mod
    import hermes_cli.gateway as gateway_mod

    pid = 202
    records = {pid: _gateway_record(pid)}
    unreadable: set[int] = set()
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        _fake_gateway_psutil(records, unreadable=unreadable),
    )
    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda **_k: [pid])
    monkeypatch.setattr(gateway_mod, "find_profile_gateway_processes", lambda: [])
    monkeypatch.setattr(gateway_mod, "_get_restart_drain_timeout", lambda: 0.1)

    def make_unreadable(_pids, *, timeout):
        unreadable.add(pid)
        return set()

    monkeypatch.setattr(
        cli_main, "_wait_for_windows_update_gateway_exit", make_unreadable
    )
    terminated = []
    monkeypatch.setattr(
        status_mod,
        "terminate_pid",
        lambda value, force=False: terminated.append((value, force)),
    )

    with pytest.raises(RuntimeError, match="changed before force-stop"):
        cli_main._pause_windows_gateways_for_update()

    assert terminated == []


# ---------------------------------------------------------------------------
# venv-side launcher ancestors (the uv launcher/worker split)
#
# A gateway started through the venv shim is two processes:
#   venv\Scripts\python.exe (launcher)  ->  uv\python\...\python.exe (worker)
# The gateway's PID file records the WORKER, so find_gateway_pids() (and the
# pause set built from it) only ever sees the worker. The venv-holder guard
# matches on the venv path prefix, so it only ever sees the LAUNCHER. The two
# sets were disjoint: a gateway the updater had just stopped still tripped the
# guard, aborting every update ("venv-blocked: N process(es) hold the install").
# ---------------------------------------------------------------------------


def _fake_psutil_tree(tree, venv_exe, worker_exe, dead=None):
    """Build a psutil stand-in where ``tree`` maps worker pid -> parent pid.

    Parents whose pid is even are venv-side (``venv_exe``); odd parents are
    unrelated ancestors (``worker_exe``) that must NOT be returned. Pids in
    ``dead`` (a live reference — later additions count) are uninspectable:
    construction raises, exactly like psutil.NoSuchProcess for an exited
    process.
    """

    records: dict[int, dict] = {}
    for worker_pid, parent_pid in tree.items():
        records[int(worker_pid)] = _gateway_record(
            int(worker_pid), exe=worker_exe
        )
        parent_exe = venv_exe if int(parent_pid) % 2 == 0 else worker_exe
        parent_argv = (
            _gateway_record(int(parent_pid), exe=parent_exe)["argv"]
            if int(parent_pid) % 2 == 0
            else [parent_exe, "/c", "start-hermes"]
        )
        records[int(parent_pid)] = _gateway_record(
            int(parent_pid), exe=parent_exe, argv=parent_argv
        )
    return _fake_gateway_psutil(records, parents=tree, dead=dead)


@patch.object(cli_main, "_is_windows", return_value=True)
def test_venv_launcher_ancestors_returns_venv_side_parent(_winp, monkeypatch):
    """The worker's venv-side parent is reported so the guard set is covered."""
    venv_exe = str(cli_main.PROJECT_ROOT / "venv" / "Scripts" / "python.exe")
    worker_exe = r"C:\Users\x\AppData\Roaming\uv\python\cpython-3.11\python.exe"

    # worker 200 -> launcher 100 (even == venv-side)
    fake = _fake_psutil_tree({200: 100}, venv_exe, worker_exe)
    monkeypatch.setitem(sys.modules, "psutil", fake)

    worker = update_cmd._capture_gateway_stop_identity(
        200, role="gateway_worker", root=cli_main.PROJECT_ROOT
    )
    assert worker is not None

    launchers = cli_main._venv_launcher_ancestors([worker])
    assert [(identity.pid, identity.role) for identity in launchers] == [
        (100, "gateway_launcher")
    ]


@patch.object(cli_main, "_is_windows", return_value=True)
def test_venv_launcher_ancestors_ignores_non_venv_parents(_winp, monkeypatch):
    """A Scheduled Task's cmd.exe / an operator shell is not a venv holder."""
    venv_exe = str(cli_main.PROJECT_ROOT / "venv" / "Scripts" / "python.exe")
    worker_exe = r"C:\Windows\System32\cmd.exe"

    # worker 200 -> parent 101 (odd == NOT venv-side)
    fake = _fake_psutil_tree({200: 101}, venv_exe, worker_exe)
    monkeypatch.setitem(sys.modules, "psutil", fake)

    worker = update_cmd._capture_gateway_stop_identity(
        200, role="gateway_worker", root=cli_main.PROJECT_ROOT
    )
    assert worker is not None

    assert cli_main._venv_launcher_ancestors([worker]) == []


@patch.object(cli_main, "_is_windows", return_value=True)
def test_venv_launcher_ancestors_is_empty_without_pids(_winp):
    """No mapped gateways means nothing to walk up from."""
    assert cli_main._venv_launcher_ancestors([]) == []


@patch.object(cli_main, "_is_windows", return_value=True)
def test_pause_kill_set_covers_venv_guard_abort_set(
    _winp,
    monkeypatch,
    tmp_path,
):
    """INVARIANT: whatever the venv guard would abort on must be stopped.

    This is the contract the two PID-resolution paths must satisfy. Before the
    launcher walk existed, ``terminated`` held only the uv-side worker while
    the guard reported the venv-side launcher, so the update aborted forever
    despite a "successful" pause.
    """
    import hermes_cli.gateway as gateway_mod
    import gateway.status as status_mod

    venv_exe = str(cli_main.PROJECT_ROOT / "venv" / "Scripts" / "python.exe")
    worker_exe = r"C:\Users\x\AppData\Roaming\uv\python\cpython-3.11\python.exe"

    profile_home = tmp_path / "profiles" / "default"
    profile_home.mkdir(parents=True)
    # The PID file records the WORKER (even-numbered parent 400 is its launcher).
    worker_pid, launcher_pid = 500, 400
    profile_proc = SimpleNamespace(
        profile="default", path=profile_home, pid=worker_pid
    )

    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda **_k: [worker_pid])
    monkeypatch.setattr(
        gateway_mod, "find_profile_gateway_processes", lambda **_k: [profile_proc]
    )
    monkeypatch.setattr(gateway_mod, "_get_restart_drain_timeout", lambda: 0.1)
    # Graceful drain succeeds: the worker exits, leaving zero survivors — and
    # an exited worker is UNINSPECTABLE afterwards, exactly like the real
    # process table. Resolving the launcher after this point is impossible,
    # so the pause must snapshot launcher ancestors before draining. This is
    # precisely the case that used to leave the launcher alive and abort.
    drained_dead: set[int] = set()

    def _drain_marks_workers_dead(pids, *, timeout):
        drained_dead.update(int(p) for p in pids)
        return set()

    monkeypatch.setattr(
        cli_main,
        "_wait_for_windows_update_gateway_exit",
        _drain_marks_workers_dead,
    )

    fake = _fake_psutil_tree(
        {worker_pid: launcher_pid}, venv_exe, worker_exe, dead=drained_dead
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)

    terminated = []
    monkeypatch.setattr(
        status_mod,
        "terminate_pid",
        lambda pid, force=False: terminated.append(int(pid)),
    )

    cli_main._pause_windows_gateways_for_update()

    # What the downstream venv-holder guard would report as blocking.
    guard_would_abort_on = {launcher_pid}
    assert guard_would_abort_on.issubset(set(terminated)), (
        f"pause stopped {sorted(terminated)} but the venv guard aborts on "
        f"{sorted(guard_would_abort_on)} — disjoint sets abort the update"
    )


@patch.object(cli_main, "_is_windows", return_value=True)
def test_pause_force_stop_revalidates_launcher_and_surviving_worker(
    _winp,
    monkeypatch,
    tmp_path,
):
    """Both trampoline halves need their own live identity proof before kill."""
    import gateway.status as status_mod
    import hermes_cli.gateway as gateway_mod

    worker_pid, launcher_pid = 500, 400
    venv_exe = str(cli_main.PROJECT_ROOT / "venv" / "Scripts" / "python.exe")
    worker_exe = str(
        cli_main.PROJECT_ROOT / ".hermes-runtime" / "python" / "python.exe"
    )
    profile_home = tmp_path / "profiles" / "default"
    profile_home.mkdir(parents=True)
    profile_proc = SimpleNamespace(
        profile="default", path=profile_home, pid=worker_pid
    )

    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda **_k: [worker_pid])
    monkeypatch.setattr(
        gateway_mod, "find_profile_gateway_processes", lambda **_k: [profile_proc]
    )
    monkeypatch.setattr(gateway_mod, "_get_restart_drain_timeout", lambda: 0.1)
    monkeypatch.setattr(
        cli_main,
        "_wait_for_windows_update_gateway_exit",
        lambda _pids, *, timeout: {worker_pid},
    )
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        _fake_psutil_tree({worker_pid: launcher_pid}, venv_exe, worker_exe),
    )
    terminated: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        status_mod,
        "terminate_pid",
        lambda pid, force=False: terminated.append((int(pid), bool(force))),
    )

    cli_main._pause_windows_gateways_for_update()

    assert terminated == [(launcher_pid, True), (worker_pid, True)]


# ---------------------------------------------------------------------------
# _leftover_pausable_gateway_pids (the guard-level gateway fallback)
#
# The pause stops every gateway discovery finds, but the venv-holder guard
# sees the process table as it is NOW. A supervisor (Scheduled Task, login
# watchdog) can respawn a gateway inside the pause→guard window, and some
# spawn paths never register in discovery at all. Those holders are exactly
# what the pause machinery exists to stop — the guard nominates them for a
# stop-and-recheck instead of dead-ending, and refuses the moment any
# non-gateway holder is present.
# ---------------------------------------------------------------------------


GATEWAY_ARGV = [
    r"C:\x\venv\Scripts\python.exe",
    "-m",
    "hermes_cli.main",
    "gateway",
    "run",
]


def _fake_psutil_cmdlines(argv_by_pid):
    """psutil stand-in serving live argv per pid; unknown pids raise."""

    records = {
        int(pid): _gateway_record(
            int(pid), argv=list(argv), exe=str(argv[0]), created_at=100.0 + int(pid)
        )
        for pid, argv in argv_by_pid.items()
    }
    return _fake_gateway_psutil(records)


def test_leftover_holders_that_are_all_gateways_are_nominated(monkeypatch):
    """Respawned/unmapped gateway holders get stopped, not dead-ended on."""
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        _fake_psutil_cmdlines({300: GATEWAY_ARGV, 301: GATEWAY_ARGV}),
    )
    matches = [
        (300, "python.exe", "truncated..."),
        (301, "python.exe", "truncated..."),
    ]

    identities = cli_main._leftover_pausable_gateway_pids(matches)
    assert identities is not None
    assert [(value.pid, value.created_at) for value in identities] == [
        (300, 400.0),
        (301, 401.0),
    ]


def test_one_non_gateway_holder_keeps_the_hard_refusal(monkeypatch):
    """A REPL/backend holder means the guard must abort exactly as before."""
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        _fake_psutil_cmdlines(
            {300: GATEWAY_ARGV, 400: [r"C:\x\venv\Scripts\python.exe", "-i"]}
        ),
    )
    matches = [(300, "python.exe", "..."), (400, "python.exe", "...")]

    assert cli_main._leftover_pausable_gateway_pids(matches) is None


def test_unreadable_live_argv_never_falls_back_to_captured_text(monkeypatch):
    """Display-only captured text cannot authorize a force-stop."""
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil_cmdlines({}))
    gateway_prefix = r"venv\Scripts\python.exe -m hermes_cli.main gateway run"

    assert cli_main._leftover_pausable_gateway_pids(
        [(300, "python.exe", gateway_prefix)]
    ) == []
    assert (
        cli_main._leftover_pausable_gateway_pids(
            [
                (300, "python.exe", gateway_prefix),
                (400, "python.exe", "python.exe -i"),
            ]
        )
        == []
    )


def test_gateway_identity_revalidates_creation_time_before_force_stop(monkeypatch):
    fake = _fake_psutil_cmdlines({300: GATEWAY_ARGV})
    monkeypatch.setitem(sys.modules, "psutil", fake)
    identities = cli_main._leftover_pausable_gateway_pids(
        [(300, "python.exe", "display only")]
    )
    assert identities is not None and len(identities) == 1

    replacement = _fake_psutil_cmdlines({300: GATEWAY_ARGV})
    original_process = replacement.Process

    class ReusedProc(original_process):
        def create_time(self):
            return 999.0

    replacement.Process = ReusedProc
    monkeypatch.setitem(sys.modules, "psutil", replacement)

    assert not cli_main._revalidate_pausable_gateway_identity(identities[0])


def test_late_gateway_identity_revalidates_exact_live_argv(monkeypatch):
    """A late-spawn snapshot cannot authorize a different PID occupant."""
    records = {300: _gateway_record(300, argv=GATEWAY_ARGV, created_at=400.0)}
    monkeypatch.setitem(sys.modules, "psutil", _fake_gateway_psutil(records))
    identities = cli_main._leftover_pausable_gateway_pids(
        [(300, "python.exe", "display only")]
    )
    assert identities is not None and len(identities) == 1

    records[300]["argv"] = [records[300]["exe"], "-i"]

    assert not cli_main._revalidate_pausable_gateway_identity(identities[0])











# ---------------------------------------------------------------------------
# cmd_update integration — concurrent-instance gate
# ---------------------------------------------------------------------------
