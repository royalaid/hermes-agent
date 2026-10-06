"""Native Windows regression for the manual-backend restart extracted from #101502."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
from unittest.mock import patch

import psutil
import pytest

from hermes_cli import dashboard_procs, main_dashboard
from hermes_cli._subprocess_compat import windows_detach_popen_kwargs


def _free_fixed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_http(port, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1) as response:
                if response.status == 200:
                    return response.status
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    raise AssertionError(f"backend did not become ready on {port}")


def _owned_processes(home):
    found = []
    for proc in psutil.process_iter():
        try:
            if proc.pid != os.getpid() and proc.environ().get("HERMES_HOME") == str(home):
                found.append(proc)
        except (psutil.Error, OSError):
            pass
    return found


def _cleanup(home):
    # Only processes carrying this test's unique home are ours to terminate.
    owned = _owned_processes(home)
    for proc in owned:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    psutil.wait_procs(owned, timeout=10)
    assert not _owned_processes(home), "test-owned process survived cleanup"


def verify_native_restart(repo, home, *, fail_restore=False):
    """Drive the real update cleanup, scoped to an isolated home, without running an update."""
    home.mkdir(parents=True, exist_ok=True)
    port = _free_fixed_port()
    argv = [sys.executable, str(repo / "hermes"), "serve", "--host", "127.0.0.1", "--port", str(port)]
    env = dict(os.environ, HERMES_HOME=str(home), PYTHONUNBUFFERED="1", PYTHONUTF8="1")
    for key in list(env):
        if key.startswith(("HERMES_PARENT_", "HERMES_DESKTOP", "HERMES_SESSION_")):
            env.pop(key)
        elif key.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIALS")):
            env.pop(key)
    output = io.StringIO()
    try:
        with open(home / "initial.log", "wb") as log:
            proc = subprocess.Popen(argv, cwd=repo, env=env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    **windows_detach_popen_kwargs())
        _wait_http(port)
        # The venv shim can have a separate worker. Capture every scoped identity before the kill.
        old = [p for p in _owned_processes(home) if p.cmdline()[1:] == argv[1:]]
        old_pids = {p.pid for p in old}
        old_argv = {p.pid: p.cmdline() for p in old}
        assert old_pids and all(command[1:] == argv[1:] for command in old_argv.values()), old_argv
        initial_listener = next(c.pid for c in psutil.net_connections(kind="tcp")
                                if c.status == psutil.CONN_LISTEN and c.laddr.port == port)
        listener_argv = psutil.Process(initial_listener).cmdline()
        blocker = None
        respawn = main_dashboard._respawn_dashboard_processes

        def occupy_then_restore(commands):
            nonlocal blocker
            blocker = socket.socket()
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            blocker.bind(("127.0.0.1", port))
            blocker.listen()
            return respawn(commands)

        with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(output):
            if fail_restore:
                # Claim the stopped backend's port before the real restore command starts.
                with patch.object(main_dashboard, "_respawn_dashboard_processes", side_effect=occupy_then_restore):
                    result = dashboard_procs._kill_stale_dashboard_processes(
                        restart_managed=True, scope_home=str(home))
            else:
                result = dashboard_procs._kill_stale_dashboard_processes(
                    restart_managed=True, scope_home=str(home))
        print(json.dumps({"old": old_argv, "result": result, "output": output.getvalue()}))
        assert initial_listener in result["matched"] and set(result["matched"]) <= old_pids, result
        assert not result["failed"] and set(result["killed"]) == set(result["matched"]), result
        if fail_restore:
            assert set(result["unrecovered"]) == set(result["killed"]), result
            assert "failed to restart" in output.getvalue() and "✓ restarted" not in output.getvalue()
            psutil.wait_procs(_owned_processes(home), timeout=10)
            remaining = _owned_processes(home)
            assert not remaining, [(p.pid, p.ppid(), p.cmdline()) for p in remaining]
            blocker_owner = next(c.pid for c in psutil.net_connections(kind="tcp")
                                 if c.status == psutil.CONN_LISTEN and c.laddr.port == port)
            assert blocker_owner == os.getpid()
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
            receipt = {"port": port, "old_pids": sorted(old_pids), "argv": argv,
                       "failure_report": output.getvalue(), "result": result,
                       "unrelated_listener_pid": blocker_owner, "unrelated_listener_untouched": True}
        else:
            assert result["unrecovered"] == [], result
            status = _wait_http(port)
            tail = ["serve", "--host", "127.0.0.1", "--port", str(port)]
            new = [p for p in _owned_processes(home)
                   if list(dashboard_procs._normalize_dashboard_cmdline(p.cmdline())) == tail]
            new_pids = {p.pid for p in new}
            assert new_pids and old_pids.isdisjoint(new_pids)
            new_listener = next(c.pid for c in psutil.net_connections(kind="tcp")
                                if c.status == psutil.CONN_LISTEN and c.laddr.port == port)
            restored_argv = psutil.Process(new_listener).cmdline()
            assert list(dashboard_procs._normalize_dashboard_cmdline(restored_argv)) == tail
            assert list(dashboard_procs._normalize_dashboard_cmdline(listener_argv)) == tail
            assert "✓ restarted" in output.getvalue()
            receipt = {"port": port, "old_pids": sorted(old_pids), "new_pids": sorted(new_pids),
                       "old_argv": listener_argv, "new_argv": restored_argv, "argument_tail": tail, "old_listener": initial_listener, "new_listener": new_listener, "http_status": status, "result": result, "output": output.getvalue()}
        print(json.dumps(receipt, ensure_ascii=True))
        return receipt
    finally:
        if 'blocker' in locals() and blocker is not None:
            blocker.close()
        _cleanup(home)
        if 'proc' in locals():
            proc.wait(timeout=10)


@pytest.mark.platforms("windows")
def test_real_manual_backend_restored(tmp_path):
    verify_native_restart(Path(__file__).resolve().parents[2], tmp_path / "manual home with spaces")


@pytest.mark.platforms("windows")
def test_real_stop_reports_occupied_restore_port(tmp_path):
    verify_native_restart(Path(__file__).resolve().parents[2], tmp_path / "failed home with spaces", fail_restore=True)
