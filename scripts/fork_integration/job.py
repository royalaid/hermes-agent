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
raise SystemExit(main(["--repo", str(repo), "--upstream-cutoff-hour", "8", "--publish", "--check",
    json.dumps([str(python), "-m", "pytest", "tests/cron/test_fork_integration_refresh.py", "-q"]),
    "--wake-agent-on-failure"]))
