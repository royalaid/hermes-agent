"""Tests for the hermes-tools-as-MCP server module surface.

We don't run a live MCP session in unit tests — that requires the codex
subprocess + client + an event loop. These tests pin the static
contract: the module imports, the EXPOSED_TOOLS list is sane, and the
build helper assembles a server when the SDK is present.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import get_args

import hermes_mcp_update_gate as update_gate

from agent.transports.hermes_tools_mcp_server import (
    _signature_from_schema,
    _watch_for_update_quiesce,
)


class TestSignatureFromSchema:
    """Test the JSON Schema -> Python signature conversion."""

    def test_simple_required_string_param(self):
        """A required string param becomes str with no default."""
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        sig, annots = _signature_from_schema(schema)

        assert len(sig.parameters) == 1
        param = sig.parameters["query"]
        assert param.name == "query"
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
        assert annots["query"] == str
        assert param.default is inspect.Parameter.empty



    def test_skip_private_params(self):
        """Params starting with '_' are excluded from the signature."""
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "_internal": {"type": "string"},
            },
            "required": ["query", "_internal"],
        }
        sig, annots = _signature_from_schema(schema)

        assert "_internal" not in sig.parameters
        assert "_internal" not in annots
        assert "query" in sig.parameters

    def test_all_json_types(self):
        """All JSON schema types map to correct Python types."""
        schema = {
            "type": "object",
            "properties": {
                "s": {"type": "string"},
                "i": {"type": "integer"},
                "n": {"type": "number"},
                "b": {"type": "boolean"},
                "a": {"type": "array"},
                "o": {"type": "object"},
            },
            "required": ["s", "i", "n", "b", "a", "o"],
        }
        sig, annots = _signature_from_schema(schema)

        assert annots["s"] == str
        assert annots["i"] == int
        assert annots["n"] == float
        assert annots["b"] == bool
        assert annots["a"] == list
        assert annots["o"] == dict








class TestModuleSurface:
    def test_module_imports_clean(self):
        from agent.transports import hermes_tools_mcp_server as m
        assert callable(m.main)
        assert callable(m._build_server)
        assert isinstance(m.EXPOSED_TOOLS, tuple)
        assert len(m.EXPOSED_TOOLS) > 0

    def test_exposed_tools_are_safe_subset(self):
        """We MUST NOT expose tools codex already has, because codex'
        own builtins are better-integrated with its sandbox + approvals.
        Specifically: no terminal/shell, no read_file/write_file, no
        patch — those are codex's built-in tools."""
        from agent.transports.hermes_tools_mcp_server import EXPOSED_TOOLS
        forbidden = {
            "terminal", "shell", "read_file", "write_file", "patch",
            "search_files", "process",
        }
        leaked = forbidden & set(EXPOSED_TOOLS)
        assert not leaked, (
            f"these tools must NOT be exposed via the codex callback "
            f"because codex has built-in equivalents: {leaked}"
        )






class TestMain:
    def test_main_exits_before_build_when_update_lease_is_active(self, monkeypatch):
        import agent.transports.hermes_tools_mcp_server as m

        build_called = False

        def build():
            nonlocal build_called
            build_called = True
            raise AssertionError("server build must not run under an update lease")

        monkeypatch.setattr(m, "_update_quiesce_requested", lambda: True)
        monkeypatch.setattr(m, "_build_server", build)

        assert m.main([]) == 0
        assert build_called is False

    def test_running_bridge_watch_exits_cooperatively(self):
        requested = iter([False, True])
        exits = []

        _watch_for_update_quiesce(
            threading.Event(),
            requested=lambda: next(requested),
            exit_process=exits.append,
            poll_seconds=0,
        )

        assert exits == [0]

    def test_running_bridge_watch_quiesces_when_marker_probe_fails(self):
        exits = []

        _watch_for_update_quiesce(
            threading.Event(),
            requested=lambda: (_ for _ in ()).throw(PermissionError("denied")),
            exit_process=exits.append,
            poll_seconds=0,
        )

        assert exits == [0]

    def test_running_bridge_process_exits_when_lease_appears(self, tmp_path):
        root = Path(__file__).resolve().parents[3]
        home = tmp_path / "hermes-home"
        marker = home / update_gate.MARKER_NAME
        started = tmp_path / "bridge-started"
        normal_return = tmp_path / "bridge-returned-normally"
        home.mkdir()

        child_code = "\n".join(
            (
                "import sys, time",
                "from pathlib import Path",
                "from agent.transports import hermes_tools_mcp_server as m",
                "root, started, normal = map(Path, sys.argv[1:4])",
                "m.infer_install_root = lambda: root",
                "class BlockingServer:",
                "    def run(self):",
                "        started.write_text('started', encoding='utf-8')",
                "        time.sleep(60)",
                "        normal.write_text('normal', encoding='utf-8')",
                "m._build_server = lambda: BlockingServer()",
                "raise SystemExit(m.main([]))",
            )
        )
        env = os.environ.copy()
        env["HERMES_HOME"] = str(home)
        process = subprocess.Popen(
            [
                sys.executable,
                "-X",
                "utf8",
                "-c",
                child_code,
                str(root),
                str(started),
                str(normal_return),
            ],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            deadline = time.monotonic() + 10
            while (
                not started.exists()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)

            assert started.exists(), "MCP bridge did not reach its blocking runner"
            assert process.poll() is None
            assert not marker.exists()

            update_gate.write_quiesce_lease(
                root,
                marker=marker,
                owner_pid=os.getpid(),
                lifetime_seconds=60,
                handoff_grace_seconds=30,
            )

            returncode = process.wait(timeout=10)
            stdout, stderr = process.communicate()
            assert returncode == 0, stdout + stderr
            assert not normal_return.exists()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def test_main_returns_2_when_mcp_unavailable(self, monkeypatch):
        """When the mcp package isn't installed, main() should exit
        cleanly with code 2 and an install hint, not crash."""
        import agent.transports.hermes_tools_mcp_server as m

        def boom_build(*a, **kw):
            raise ImportError("mcp not installed")

        monkeypatch.setattr(m, "_build_server", boom_build)
        rc = m.main(["--verbose"])
        assert rc == 2

    def test_main_handles_keyboard_interrupt(self, monkeypatch):
        import agent.transports.hermes_tools_mcp_server as m

        class FakeServer:
            def run(self):
                raise KeyboardInterrupt()

        monkeypatch.setattr(m, "_build_server", lambda: FakeServer())
        rc = m.main([])
        assert rc == 0

    def test_main_returns_1_on_runtime_error(self, monkeypatch):
        import agent.transports.hermes_tools_mcp_server as m

        class CrashingServer:
            def run(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(m, "_build_server", lambda: CrashingServer())
        rc = m.main([])
        assert rc == 1
