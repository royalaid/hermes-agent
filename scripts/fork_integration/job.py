#!/usr/bin/env python3
"""Cron adapter for the direct fork-integration refresh."""
import json, os
from pathlib import Path
from refresh import main

root = Path(os.environ.get("HERMES_HOME", Path(__file__).resolve().parents[2]))
repo = root if (root / "pyproject.toml").is_file() else root / "hermes-agent"
preferred = repo / ".venv" / "Scripts" / "python.exe"
configured = Path(os.environ.get("HERMES_PYTHON", "C:/Python311/python.exe"))
python = configured if configured.is_file() else preferred if preferred.is_file() else repo / "venv" / "Scripts" / "python.exe"
# The syntax gate runs FIRST and against the scratch worktree (`--tree` defaults
# to the check's cwd); esbuild is resolved from the main checkout because a
# worktree has no node_modules. Without it nothing in the compose ever parses
# the tree it is about to publish — which is how a tip whose `hermes` CLI,
# gateway and desktop bundle were all unparseable reached this machine.
raise SystemExit(main(["--repo", str(repo), "--upstream-cutoff-hour", "8", "--publish",
    "--check", json.dumps([str(python), str(repo / "scripts" / "fork_integration" / "syntax_gate.py"),
        "--esbuild-root", str(repo)]),
    "--check", json.dumps([str(python), "-m", "pytest", "tests/cron/test_fork_integration_refresh.py", "-q"]),
    "--wake-agent-on-failure"]))
