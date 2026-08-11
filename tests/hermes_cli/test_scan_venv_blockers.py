"""Tests for hermes_cli/_scan_venv_blockers.py.

Tests call the real production functions (``main``, ``_redact_sensitive_cmdline``).
Most patch the detector directly; one Windows-only regression exercises the
real process table using only subprocesses owned by that test.
"""

from __future__ import annotations

import builtins
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import agent.redact as redact_module
import hermes_cli._scan_venv_blockers as scanner
from hermes_cli._scan_venv_blockers import (
    _hermes_cli_command,
    _is_pausable_gateway,
    _probe_fail_json,
    _redact_sensitive_cmdline,
    main,
)


# ---------------------------------------------------------------------------
# main() — stdout, stderr, exit code (with patched detector)
# ---------------------------------------------------------------------------


def test_main_invalid_arguments_emit_one_fail_closed_json_document(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        main([])

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert len(captured.out.splitlines()) == 1
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["blocked"] is True
    assert payload["reason"] == "invalid_arguments"
    assert payload["error"]["code"] == "invalid_arguments"
    assert "--root" in captured.err


def test_main_probe_exception_emits_one_fail_closed_json_document(
    monkeypatch, capsys
) -> None:
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(
        scanner,
        "scan_venv_blockers",
        lambda _root: (_ for _ in ()).throw(PermissionError("probe denied")),
    )

    with pytest.raises(SystemExit) as raised:
        main(["--root", str(root)])

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert len(captured.out.splitlines()) == 1
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["blocked"] is True
    assert payload["reason"] == "probe_failed"
    assert payload["error"] == {
        "code": "probe_failed",
        "message": "probe denied",
    }
    assert captured.err.strip() == "probe denied"


def _psutil_fake() -> dict:
    """Return a sys.modules dict entry that makes psutil appear available."""
    return {
        "psutil": types.SimpleNamespace(
            Process=lambda *a: MagicMock(),
            NoSuchProcess=type("NoSuchProcess", (Exception,), {}),
        )
    }


def _detector_proc(pid, exe, name, cmdline=None, cwd=""):
    proc = MagicMock()
    proc.info = {
        "pid": pid,
        "exe": exe,
        "name": name,
        "cmdline": cmdline or [],
        "cwd": cwd,
    }
    return proc


def test_strict_detector_excludes_only_self_and_keeps_venv_parent_candidate(
    monkeypatch, tmp_path: Path
) -> None:
    import os
    import hermes_cli.update_cmd as update_cmd

    venv_python = str(tmp_path / "venv" / "Scripts" / "python.exe")
    fake_psutil = types.SimpleNamespace(
        process_iter=lambda _attrs: iter(
            [
                _detector_proc(os.getpid(), venv_python, "python.exe"),
                _detector_proc(555, venv_python, "python.exe"),
            ]
        )
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert update_cmd._detect_venv_python_processes(
        root=tmp_path, strict=True
    ) == [(555, "python.exe", "")]


def test_strict_detector_excludes_exact_immediate_hermes_console_shim(
    monkeypatch, tmp_path: Path
) -> None:
    import os
    import hermes_cli.update_cmd as update_cmd

    shim = tmp_path / "venv" / "Scripts" / "hermes.exe"
    parent = types.SimpleNamespace(
        pid=555,
        exe=lambda: str(shim),
        cmdline=lambda: [str(shim), "update", "--preflight"],
    )
    current = types.SimpleNamespace(parent=lambda: parent)
    fake_psutil = types.SimpleNamespace(
        Process=lambda pid: current if int(pid) == os.getpid() else parent,
        process_iter=lambda _attrs: iter(
            [_detector_proc(555, str(shim), "hermes.exe", [str(shim), "update"])]
        ),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert update_cmd._detect_venv_python_processes(
        root=tmp_path, strict=True
    ) == []


def test_strict_detector_excludes_exact_immediate_python_venv_trampoline(
    monkeypatch, tmp_path: Path
) -> None:
    import os
    import hermes_cli.update_cmd as update_cmd

    python = tmp_path / "venv" / "Scripts" / "python.exe"
    argv = [
        str(python),
        "-m",
        "hermes_cli.main",
        "update",
        "--preflight",
        "--json",
    ]
    parent = types.SimpleNamespace(
        pid=556,
        exe=lambda: str(python),
        cmdline=lambda: argv,
    )
    current = types.SimpleNamespace(parent=lambda: parent)
    fake_psutil = types.SimpleNamespace(
        Process=lambda pid: current if int(pid) == os.getpid() else parent,
        process_iter=lambda _attrs: iter(
            [_detector_proc(556, str(python), "python.exe", argv)]
        ),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert update_cmd._detect_venv_python_processes(
        root=tmp_path, strict=True
    ) == []


def test_strict_detector_keeps_immediate_python_venv_non_update_parent(
    monkeypatch, tmp_path: Path
) -> None:
    import os
    import hermes_cli.update_cmd as update_cmd

    python = tmp_path / "venv" / "Scripts" / "python.exe"
    argv = [str(python), "-m", "hermes_cli.main", "serve"]
    parent = types.SimpleNamespace(
        pid=557,
        exe=lambda: str(python),
        cmdline=lambda: argv,
    )
    current = types.SimpleNamespace(parent=lambda: parent)
    fake_psutil = types.SimpleNamespace(
        Process=lambda pid: current if int(pid) == os.getpid() else parent,
        process_iter=lambda _attrs: iter(
            [_detector_proc(557, str(python), "python.exe", argv)]
        ),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert update_cmd._detect_venv_python_processes(
        root=tmp_path, strict=True
    ) == [(557, "python.exe", " ".join(argv))]


@pytest.mark.parametrize(
    "global_args",
    [
        ["--safe-mode"],
        ["--provider", "openrouter"],
        ["--ignore-user-config", "--yolo"],
        ["--"],
    ],
)
def test_strict_detector_excludes_current_shim_with_global_options_before_update(
    monkeypatch, tmp_path: Path, global_args: list[str]
) -> None:
    import os
    import hermes_cli.update_cmd as update_cmd

    shim = tmp_path / "venv" / "Scripts" / "hermes.exe"
    argv = [str(shim), *global_args, "update", "--preflight"]
    parent = types.SimpleNamespace(
        pid=555,
        exe=lambda: str(shim),
        cmdline=lambda: argv,
    )
    current = types.SimpleNamespace(parent=lambda: parent)
    fake_psutil = types.SimpleNamespace(
        Process=lambda pid: current if int(pid) == os.getpid() else parent,
        process_iter=lambda _attrs: iter(
            [_detector_proc(555, str(shim), "hermes.exe", argv)]
        ),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert update_cmd._detect_venv_python_processes(
        root=tmp_path, strict=True
    ) == []


@pytest.mark.parametrize(
    "argv_tail",
    [
        ["--", "--", "update"],
        ["--", "chat", "update"],
        ["--update"],
    ],
)
def test_current_update_shim_rejects_ambiguous_end_of_options(
    argv_tail: list[str],
) -> None:
    import hermes_cli.update_cmd as update_cmd

    assert not update_cmd._is_current_update_shim_argv(
        [r"C:\Hermes\venv\Scripts\hermes.exe", *argv_tail]
    )


def test_strict_detector_keeps_higher_target_venv_python_ancestor(
    monkeypatch, tmp_path: Path
) -> None:
    import os
    import hermes_cli.update_cmd as update_cmd

    shell = types.SimpleNamespace(
        pid=444,
        exe=lambda: r"C:\Windows\System32\cmd.exe",
        cmdline=lambda: [r"C:\Windows\System32\cmd.exe"],
    )
    current = types.SimpleNamespace(parent=lambda: shell)
    venv_python = tmp_path / "venv" / "Scripts" / "python.exe"
    fake_psutil = types.SimpleNamespace(
        Process=lambda pid: current if int(pid) == os.getpid() else shell,
        process_iter=lambda _attrs: iter(
            [
                _detector_proc(
                    333,
                    str(venv_python),
                    "python.exe",
                    [str(venv_python), "-m", "agent.run"],
                )
            ]
        ),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert update_cmd._detect_venv_python_processes(
        root=tmp_path, strict=True
    ) == [(333, "python.exe", f"{venv_python} -m agent.run")]


def test_strict_detector_keeps_target_candidate_with_unreadable_exe(
    monkeypatch, tmp_path: Path
) -> None:
    import hermes_cli.update_cmd as update_cmd

    fake_psutil = types.SimpleNamespace(
        process_iter=lambda _attrs: iter(
            [
                _detector_proc(
                    777,
                    None,
                    "python.exe",
                    ["python.exe", "-m", "hermes_cli.main", "serve"],
                    cwd=str(tmp_path),
                )
            ]
        )
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert update_cmd._detect_venv_python_processes(
        root=tmp_path, strict=True
    ) == [(777, "python.exe", "python.exe -m hermes_cli.main serve")]


def test_strict_detector_fails_closed_when_python_identity_is_fully_unreadable(
    monkeypatch, tmp_path: Path
) -> None:
    import hermes_cli.update_cmd as update_cmd

    proc = MagicMock()
    proc.info = {
        "pid": 778,
        "exe": None,
        "name": "python.exe",
        "cmdline": None,
        "cwd": None,
    }
    fake_psutil = types.SimpleNamespace(process_iter=lambda _attrs: iter([proc]))
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    with pytest.raises(RuntimeError, match=r"process 778 .* identity metadata was unreadable"):
        update_cmd._detect_venv_python_processes(root=tmp_path, strict=True)






# ---------------------------------------------------------------------------
# _redact_sensitive_cmdline
# ---------------------------------------------------------------------------


def test_redact_long_flag_value_space_separated() -> None:
    """--token SECRET must preserve --token and emit --token <redacted>."""
    raw = "python.exe -m hermes_cli.main serve --token ghp_abc123 --host 10.0.0.1"
    result = _redact_sensitive_cmdline(raw)
    assert result == "python.exe -m hermes_cli.main serve --token <redacted>"
    assert "ghp_abc123" not in result




def test_redact_sensitive_text_failure_returns_fully_redacted() -> None:
    """When agent.redact.redact_sensitive_text raises, the entire result
    must equal '<redacted>' so PID and name still provide diagnostics."""
    with patch.object(
        redact_module,
        "redact_sensitive_text",
        side_effect=RuntimeError("no redactor"),
    ):
        result = _redact_sensitive_cmdline("python.exe --token abc123")

    assert result == "<redacted>"


def test_redact_session_key() -> None:
    """--session-key <identifier> must redact the value and everything after."""
    raw = "python.exe -m tui_gateway.slash_worker --session-key 20260712-abcdef --model test"
    result = _redact_sensitive_cmdline(raw)
    assert result == "python.exe -m tui_gateway.slash_worker --session-key <redacted>"


def test_redact_normal_host_port_profile_remain() -> None:
    raw = "python.exe -m hermes_cli.main serve --host 10.0.0.1 --port 9119 --profile work"
    result = _redact_sensitive_cmdline(raw)
    assert "10.0.0.1" in result
    assert "9119" in result
    assert "work" in result


def test_redact_no_sensitive_flags_is_noop() -> None:
    raw = "python.exe -m hermes_cli.main serve --host 127.0.0.1"
    assert _redact_sensitive_cmdline(raw) == raw


def test_redact_empty_string() -> None:
    assert _redact_sensitive_cmdline("") == ""


def test_redact_short_flags_not_redacted() -> None:
    """Short flags -t (toolset), -p (profile), -k are NOT redacted."""
    raw = "python.exe -m hermes_cli.main serve -t web -p default -k somearg"
    result = _redact_sensitive_cmdline(raw)
    assert result == raw  # short flags pass through unchanged


# ---------------------------------------------------------------------------
# _is_pausable_gateway — the gateway exemption
#
# `hermes-setup` always invokes `hermes update --yes --gateway`, whose
# `_pause_windows_gateways_for_update()` stops running gateways itself. The
# Desktop preflight must therefore not report gateway launcher/worker chains
# as blockers — doing so aborts the handoff before the component that can
# handle them ever runs.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmdline",
    [
        # venv-side launcher, exactly as the scheduled task spawns it
        r"C:\Users\u\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
        " -m hermes_cli.main gateway run --replace",
        # uv-side worker re-running the same argv (quoted exe, double space)
        r'"C:\Users\u\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe"'
        "  -m hermes_cli.main gateway run --replace",
        # profile-scoped gateway
        "python.exe -m hermes_cli.main --profile work gateway run",
        # a profile literally NAMED "gateway" — the profile value must not
        # shadow the subcommand token (the hand-rolled matcher regressed this)
        "python.exe -m hermes_cli.main --profile gateway gateway run",
        "python.exe -m hermes_cli.main -p gateway gateway run",
        # case variations survive
        "PYTHON.EXE -m hermes_cli.main GATEWAY RUN",
    ],
)
def test_is_pausable_gateway_accepts_gateway_run_chains(cmdline: str) -> None:
    assert _is_pausable_gateway(cmdline) is True


@pytest.mark.parametrize(
    "cmdline",
    [
        # desktop backend: no pause machinery downstream, must keep blocking
        "python.exe -m hermes_cli.main serve --host 127.0.0.1 --port 8756",
        # other gateway subcommands are not running gateways
        "python.exe -m hermes_cli.main gateway stop",
        "python.exe -m hermes_cli.main gateway status",
        "python.exe -m hermes_cli.main gateway install",
        # A bare gateway command is not proof that the operative action was run.
        "python.exe -m hermes_cli.main gateway",
        # operator REPL / stray script
        "python.exe",
        "python.exe myscript.py gateway run",  # not a hermes_cli.main invocation
        "",
    ],
)
def test_is_pausable_gateway_rejects_everything_else(cmdline: str) -> None:
    assert _is_pausable_gateway(cmdline) is False


def test_hermes_command_parser_is_exact_and_understands_python_options() -> None:
    assert (
        _hermes_cli_command(
            "python.exe -X utf8 -m hermes_cli.main serve --host 127.0.0.1"
        )
        == "serve"
    )
    assert (
        _hermes_cli_command(
            "python.exe -m hermes_cli.main --profile serve gateway run"
        )
        == "gateway"
    )
    assert (
        _hermes_cli_command(
            "python.exe -m agent.transports.hermes_tools_mcp_server"
        )
        is None
    )
    assert (
        _hermes_cli_command(
            "python.exe script.py -m hermes_cli.main serve"
        )
        is None
    )
    assert (
        _hermes_cli_command(
            "python.exe -m hermes_cli.main --unknown serve"
        )
        is None
    )


def test_gateway_classifier_preserves_live_argv_token_boundaries() -> None:
    argv = [
        "python.exe",
        "-m",
        "hermes_cli.main",
        "serve",
        "--title",
        "x gateway run",
    ]

    assert _hermes_cli_command(argv) == "serve"
    assert not _is_pausable_gateway(argv)


def test_strict_detector_keeps_exe_less_exact_mcp_candidate(
    monkeypatch, tmp_path: Path
) -> None:
    import hermes_cli.update_cmd as update_cmd

    module = "agent.transports.hermes_tools_mcp_server"
    fake_psutil = types.SimpleNamespace(
        process_iter=lambda _attrs: iter(
            [
                _detector_proc(
                    778,
                    None,
                    "python.exe",
                    ["python.exe", "-m", module],
                    cwd=str(tmp_path),
                )
            ]
        )
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert update_cmd._detect_venv_python_processes(
        root=tmp_path, strict=True
    ) == [(778, "python.exe", f"python.exe -m {module}")]


def _run_main_with_detector(monkeypatch, capsys, matches):
    """Run main() with the process detector patched to return *matches*."""
    for name, mod in _psutil_fake().items():
        monkeypatch.setitem(sys.modules, name, mod)
    import hermes_cli.update_cmd as update_cmd

    monkeypatch.setattr(
        update_cmd,
        "_detect_venv_python_processes",
        lambda *, root, strict: matches,
    )
    live = {
        int(pid): scanner._ProcessSnapshot(
            pid=int(pid),
            ppid=1,
            name=str(name),
            exe=scanner._tokens(cmdline)[0] if scanner._tokens(cmdline) else "python.exe",
            argv=tuple(scanner._tokens(cmdline)),
            created_at=100.0 + int(pid),
            process=MagicMock(),
        )
        for pid, name, cmdline in matches
    }
    monkeypatch.setattr(scanner, "_snapshot_for_pid", lambda pid: live[int(pid)])
    root = Path(__file__).resolve().parents[2]
    with pytest.raises(SystemExit) as excinfo:
        main(["--root", str(root)])
    out = capsys.readouterr().out
    return excinfo.value.code, json.loads(out)


def test_probe_fail_json_is_unambiguous_failure() -> None:
    """A failed probe must not look like a clear scan (#83149).

    Humans and naive callers used to read ``blocked: false`` as "no holders"
    when psutil was missing after a gutted venv. The document must mark
    ``probe_failed`` and keep ``ok`` false.
    """
    data = json.loads(_probe_fail_json("psutil is not available: No module named 'psutil'"))
    assert data["ok"] is False
    assert data["probe_failed"] is True
    assert data["blocked"] is False
    assert data["processes"] == []
    assert "psutil" in data["error"]


def test_main_psutil_missing_is_probe_failure_not_clear(monkeypatch, capsys):
    """Missing psutil exits non-zero with probe_failed JSON — never a clear scan."""
    real_import = builtins.__import__

    def _no_psutil(name, *args, **kwargs):
        if name == "psutil" or name.startswith("psutil."):
            raise ImportError("No module named 'psutil'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    monkeypatch.delitem(sys.modules, "psutil", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        main()
    captured = capsys.readouterr()
    assert excinfo.value.code == 1
    data = json.loads(captured.out)
    assert data["ok"] is False
    assert data["probe_failed"] is True
    assert "psutil" in captured.err.lower()


def test_main_exempts_gateway_chain_but_keeps_other_holders(monkeypatch, capsys):
    """A gateway launcher/worker pair alone must scan clear; a non-gateway
    holder alongside it must still block (and be the only reported PID)."""
    gateway_launcher = (
        12,
        "python.exe",
        r"C:\x\venv\Scripts\python.exe -m hermes_cli.main gateway run --replace",
    )
    gateway_worker = (
        34,
        "python.exe",
        r'"C:\u\uv\python\python.exe"  -m hermes_cli.main gateway run --replace',
    )
    stray_repl = (56, "python.exe", r"C:\x\venv\Scripts\python.exe")

    # Gateway chain only → clear
    code, data = _run_main_with_detector(
        monkeypatch, capsys, [gateway_launcher, gateway_worker]
    )
    assert code == 0
    assert data["ok"] is True
    assert data["blocked"] is False
    assert data["processes"] == []
    assert data["pausable_gateways"] == 2

    # Gateway chain + stray REPL → blocked, reporting only the REPL
    code, data = _run_main_with_detector(
        monkeypatch, capsys, [gateway_launcher, gateway_worker, stray_repl]
    )
    assert code == 0
    assert data["blocked"] is True
    assert [p["pid"] for p in data["processes"]] == [56]
    assert data["pausable_gateways"] == 2


def test_main_desktop_serve_backend_still_blocks(monkeypatch, capsys):
    """The desktop's own `serve` backend has no downstream pause — it must
    keep blocking exactly as before the exemption."""
    serve = (
        78,
        "python.exe",
        r"C:\x\venv\Scripts\python.exe -m hermes_cli.main serve --host 127.0.0.1",
    )
    code, data = _run_main_with_detector(monkeypatch, capsys, [serve])
    assert code == 0
    assert data["blocked"] is True
    assert [p["pid"] for p in data["processes"]] == [78]
    assert data["pausable_gateways"] == 0

def test_main_gateway_with_long_managed_runtime_path_is_exempt(monkeypatch, capsys):
    r"""Regression: the detector must hand the FULL cmdline to the exemption.

    Gateways launched via the managed-runtime interpreter carry a >120-char
    exe path (`.hermes-runtime\python\generation-...\cpython-3.11-...`).
    The old `cmdline_raw[:120]` truncation in the detector cut the cmdline
    before `-m hermes_cli.main gateway run`, so the exemption never matched
    and every Desktop update aborted with 'Update didn't finish'.
    Here the detector returns full cmdlines (post-fix contract); the scan
    must exempt the gateway and truncate only the *displayed* cmdline.
    """
    long_exe = (
        r'"C:\Users\u\AppData\Local\hermes\hermes-agent\.hermes-runtime\python'
        r"\generation-1785095035-66720-be29ea9c\cpython-3.11-windows-x86_64-none"
        r'\python.exe"'
    )
    assert len(long_exe) > 120  # the truncation point was inside the exe path
    gateway = (91, "python.exe", long_exe + "  -m hermes_cli.main gateway run --replace")
    code, data = _run_main_with_detector(monkeypatch, capsys, [gateway])
    assert code == 0
    assert data["blocked"] is False
    assert data["processes"] == []
    assert data["pausable_gateways"] == 1

    # A long-path NON-gateway holder still blocks, with cmdline truncated for display.
    stray = (92, "python.exe", long_exe + "  -m some_other_module --serve-forever")
    code, data = _run_main_with_detector(monkeypatch, capsys, [gateway, stray])
    assert data["blocked"] is True
    assert [p["pid"] for p in data["processes"]] == [92]
    assert len(data["processes"][0]["cmdline"]) <= 120


class _FakeProcess:
    def __init__(self, *, parents=()):
        self._parents = list(parents)
        self.kills = 0

    def parents(self):
        return list(self._parents)

    def kill(self):
        self.kills += 1


class _FakeAncestor:
    def __init__(self, name: str, exe: str, argv: list[str]):
        self._name = name
        self._exe = exe
        self._argv = argv

    def name(self):
        return self._name

    def exe(self):
        return self._exe

    def cmdline(self):
        return list(self._argv)


def _snapshot(
    *,
    pid: int,
    ppid: int,
    exe: Path,
    argv: tuple[str, ...],
    created_at: float,
    process: _FakeProcess | None = None,
):
    return scanner._ProcessSnapshot(
        pid=pid,
        ppid=ppid,
        name="python.exe",
        exe=str(exe),
        argv=argv,
        created_at=created_at,
        process=process or _FakeProcess(),
    )


def test_scan_reports_worker_before_wrapper_and_keeps_relationship(
    monkeypatch, tmp_path: Path
) -> None:
    import hermes_cli.update_cmd as update_cmd

    root = tmp_path / "install"
    venv = root / "venv"
    module = "agent.transports.hermes_tools_mcp_server"
    wrapper = _snapshot(
        pid=10,
        ppid=1,
        exe=venv / "Scripts" / "python.exe",
        argv=(str(venv / "Scripts" / "python.exe"), "-m", module),
        created_at=100.0,
    )
    worker = _snapshot(
        pid=20,
        ppid=10,
        exe=root / ".hermes-runtime" / "python" / "generation" / "python.exe",
        argv=("python.exe", "-m", module),
        created_at=101.0,
    )
    monkeypatch.setattr(scanner, "_validated_root", lambda _root: (root, venv))
    monkeypatch.setattr(
        update_cmd,
        "_detect_venv_python_processes",
        lambda *, root, strict: [
            (10, "python.exe", " ".join(wrapper.argv)),
            (20, "python.exe", " ".join(worker.argv)),
        ],
    )
    monkeypatch.setattr(
        scanner,
        "_snapshot_for_pid",
        lambda pid: {10: wrapper, 20: worker}[pid],
    )

    result = scanner.scan_venv_blockers(root)

    assert [entry["pid"] for entry in result["mcp_bridges"]] == [20, 10]
    assert result["mcp_bridges"][0]["role"] == "mcp_bridge_worker"
    assert result["mcp_bridges"][0]["wrapper_pid"] == 10
    assert result["mcp_bridges"][1]["role"] == "mcp_bridge_wrapper"
    assert all(entry["owner"] == "unknown" for entry in result["mcp_bridges"])


def test_unreadable_target_gateway_is_a_hard_blocker_not_exempted(
    monkeypatch, tmp_path: Path
) -> None:
    import hermes_cli.update_cmd as update_cmd

    root = tmp_path / "install"
    venv = root / "venv"
    monkeypatch.setattr(scanner, "_validated_root", lambda _root: (root, venv))
    monkeypatch.setattr(
        update_cmd,
        "_detect_venv_python_processes",
        lambda *, root, strict: [
            (77, "python.exe", "python.exe -m hermes_cli.main gateway run")
        ],
    )
    monkeypatch.setattr(
        scanner,
        "_snapshot_for_pid",
        lambda _pid: (_ for _ in ()).throw(PermissionError("access denied")),
    )

    result = scanner.scan_venv_blockers(root)

    assert result["blocked"] is True
    assert result["pausable_gateways"] == 0
    assert [process["pid"] for process in result["processes"]] == [77]
    assert result["processes"][0]["actionability"] == "hard_block"


def test_managed_worker_remains_exactly_actionable_after_wrapper_dies(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path / "install"
    venv = root / "venv"
    process = _FakeProcess(
        parents=[_FakeAncestor("codex.exe", r"C:\tools\codex.exe", ["codex.exe"])]
    )
    worker = _snapshot(
        pid=20,
        ppid=10,
        exe=root / ".hermes-runtime" / "python" / "generation" / "python.exe",
        argv=("python.exe", "-m", "agent.transports.hermes_tools_mcp_server"),
        created_at=101.0,
        process=process,
    )
    monkeypatch.setattr(scanner, "_validated_root", lambda _root: (root, venv))
    monkeypatch.setattr(scanner, "_snapshot_for_pid", lambda pid: worker)

    assert scanner.terminate_mcp_bridge(root, pid=20, created_at=101.0)
    assert process.kills == 1


def test_node_hosted_claude_ancestry_is_attributed_exactly(tmp_path: Path) -> None:
    claude = _FakeAncestor(
        "node.exe",
        r"C:\Program Files\nodejs\node.exe",
        [
            r"C:\Program Files\nodejs\node.exe",
            r"C:\Users\u\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\cli.js",
        ],
    )
    snapshot = _snapshot(
        pid=20,
        ppid=10,
        exe=tmp_path / "python.exe",
        argv=("python.exe", "-m", "agent.transports.hermes_tools_mcp_server"),
        created_at=101.0,
        process=_FakeProcess(parents=[claude]),
    )

    assert scanner._owner_from_ancestry(snapshot) == "claude"
    record = scanner._mcp_record(
        snapshot, role="mcp_bridge_worker", wrapper_pid=None
    )
    assert record["actionable"] is True
    assert record["action"] == "terminate_exact_mcp"


@pytest.mark.parametrize("created_at", [float("nan"), float("inf"), -1.0, 102.0])
def test_terminate_refuses_invalid_or_reused_process_identity(
    monkeypatch, tmp_path: Path, created_at: float
) -> None:
    root = tmp_path / "install"
    venv = root / "venv"
    process = _FakeProcess()
    record = {
        "created_at": 101.0,
        "owner": "codex",
        "role": "mcp_bridge_worker",
        "actionable": True,
        "action": "terminate_exact_mcp",
    }
    monkeypatch.setattr(scanner, "_validated_root", lambda _root: (root, venv))
    monkeypatch.setattr(
        scanner,
        "_live_mcp_bridge_process",
        lambda _root, _pid: (process, record),
    )

    assert not scanner.terminate_mcp_bridge(root, pid=20, created_at=created_at)
    assert process.kills == 0


def test_terminate_refuses_unknown_owner_even_for_exact_worker(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path / "install"
    venv = root / "venv"
    process = _FakeProcess()
    record = {
        "created_at": 101.0,
        "owner": "unknown",
        "role": "mcp_bridge_worker",
        "actionable": False,
        "action": "refuse",
    }
    monkeypatch.setattr(scanner, "_validated_root", lambda _root: (root, venv))
    monkeypatch.setattr(
        scanner,
        "_live_mcp_bridge_process",
        lambda _root, _pid: (process, record),
    )

    assert not scanner.terminate_mcp_bridge(root, pid=20, created_at=101.0)
    assert process.kills == 0


def test_terminate_rereads_and_refuses_changed_live_argv(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path / "install"
    venv = root / "venv"
    process = _FakeProcess()
    changed = _snapshot(
        pid=20,
        ppid=10,
        exe=root / ".hermes-runtime" / "python" / "generation" / "python.exe",
        argv=("python.exe", "-m", "hermes_cli.main", "serve"),
        created_at=101.0,
        process=process,
    )
    monkeypatch.setattr(scanner, "_validated_root", lambda _root: (root, venv))
    monkeypatch.setattr(scanner, "_snapshot_for_pid", lambda pid: changed)

    assert not scanner.terminate_mcp_bridge(root, pid=20, created_at=101.0)
    assert process.kills == 0


@pytest.mark.windows_only
def test_native_scanner_preserves_identity_redacts_and_refuses_stale_identity(
    monkeypatch, tmp_path: Path
) -> None:
    """Exercise the Windows psutil boundary without touching a live runtime."""
    import psutil

    root = Path(scanner.__file__).resolve().parents[1]
    target_root, venv = scanner._validated_root(root)
    module = "agent.transports.hermes_tools_mcp_server"
    synthetic = tmp_path / "synthetic"
    transport = synthetic / "agent" / "transports"
    transport.mkdir(parents=True)
    (synthetic / "agent" / "__init__.py").write_text("", encoding="utf-8")
    (transport / "__init__.py").write_text("", encoding="utf-8")
    (transport / "hermes_tools_mcp_server.py").write_text(
        "import time\ntime.sleep(120)\n",
        encoding="utf-8",
    )

    secret = "native-scanner-secret-9f6f1a"
    hidden = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    children: list[subprocess.Popen[bytes]] = []
    try:
        bridge = subprocess.Popen(
            [sys.executable, "-m", module],
            cwd=synthetic,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=hidden,
        )
        children.append(bridge)
        secret_holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(120)",
                "--token",
                secret,
            ],
            cwd=synthetic,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=hidden,
        )
        children.append(secret_holder)

        # Owner attribution has separate ancestry tests above. Keep this test
        # focused on the real Windows pid/exe/argv/create-time reads.
        monkeypatch.setattr(scanner, "_owner_from_ancestry", lambda _snapshot: "codex")

        deadline = time.monotonic() + 10.0
        payload = None
        bridge_record = None
        holder_record = None
        while time.monotonic() < deadline:
            assert bridge.poll() is None
            assert secret_holder.poll() is None
            payload = scanner.scan_venv_blockers(target_root)
            bridge_record = next(
                (
                    entry
                    for entry in payload["mcp_bridges"]
                    if entry["pid"] == bridge.pid
                ),
                None,
            )
            holder_record = next(
                (
                    entry
                    for entry in payload["processes"]
                    if entry["pid"] == secret_holder.pid
                ),
                None,
            )
            if bridge_record is not None and holder_record is not None:
                break
            time.sleep(0.05)

        assert payload is not None
        assert bridge_record is not None
        assert holder_record is not None

        bridge_snapshot = scanner._snapshot_for_pid(bridge.pid)
        holder_snapshot = scanner._snapshot_for_pid(secret_holder.pid)
        assert bridge_snapshot is not None
        assert holder_snapshot is not None
        assert bridge_snapshot.pid == bridge.pid
        assert scanner.is_exact_mcp_module_argv(bridge_snapshot.argv)
        assert scanner._within(bridge_snapshot.exe, venv)
        assert bridge_record["role"] == "mcp_bridge_wrapper"
        assert bridge_record["owner"] == "codex"
        assert bridge_record["actionable"] is True
        assert bridge_record["created_at"] == pytest.approx(
            bridge_snapshot.created_at, abs=0.01
        )

        raw_holder_cmdline = " ".join(holder_snapshot.argv)
        assert secret in raw_holder_cmdline, "the OS snapshot must contain the fixture secret"
        redacted = scanner._redact_sensitive_cmdline(raw_holder_cmdline)
        assert secret not in redacted
        assert "--token <redacted>" in redacted
        assert secret not in json.dumps(payload)
        assert holder_record["created_at"] == pytest.approx(
            holder_snapshot.created_at, abs=0.01
        )

        stale_created_at = float(bridge_record["created_at"]) + 10.0
        assert not scanner.terminate_mcp_bridge(
            target_root,
            pid=bridge.pid,
            created_at=stale_created_at,
        )
        assert bridge.poll() is None, "a stale identity must not kill the live process"
        live_snapshot = scanner._snapshot_for_pid(bridge.pid)
        assert live_snapshot is not None
        assert live_snapshot.created_at == pytest.approx(
            bridge_snapshot.created_at, abs=0.01
        )
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
