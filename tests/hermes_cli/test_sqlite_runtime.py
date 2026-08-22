"""Behavioral tests for exact-interpreter SQLite runtime inspection."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from hermes_cli.sqlite_runtime import (
    is_sqlite_wal_reset_vulnerable,
    probe_sqlite_runtime,
)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ((3, 6, 23), False),
        ((3, 7, 0), True),
        ((3, 44, 5), True),
        ((3, 44, 6), False),
        ((3, 45, 0), True),
        ((3, 50, 6), True),
        ((3, 50, 7), False),
        ((3, 51, 2), True),
        ((3, 51, 3), False),
        ((3, 53, 1), False),
    ],
)
def test_wal_reset_vulnerability_matrix(
    version: tuple[int, ...],
    expected: bool,
) -> None:
    assert is_sqlite_wal_reset_vulnerable(version) is expected


def test_probe_reports_the_requested_interpreters_linked_sqlite() -> None:
    info = probe_sqlite_runtime(sys.executable)

    assert info is not None
    assert info.executable.resolve() == Path(sys.executable).resolve()
    assert info.base_prefix.resolve() == Path(sys.base_prefix).resolve()
    assert info.python_version == sys.version_info[:3]
    assert info.sqlite_version == sqlite3.sqlite_version_info
    assert info.sqlite_version_string == sqlite3.sqlite_version

    with sqlite3.connect(":memory:") as conn:
        source_id = conn.execute("SELECT sqlite_source_id()").fetchone()[0]
    assert info.sqlite_source_id == source_id


def test_probe_starts_interpreter_isolated_without_writing_bytecode(
    tmp_path: Path,
) -> None:
    venv_dir = tmp_path / "probe-venv"
    venv.EnvBuilder(with_pip=False).create(venv_dir)
    venv_python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    purelib = Path(
        subprocess.run(
            [
                str(venv_python),
                "-I",
                "-B",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    startup_observation = tmp_path / "startup-observation.json"
    startup_module = purelib / "_hermes_sqlite_probe_startup.py"
    startup_module.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "from pathlib import Path",
                (
                    f"Path({str(startup_observation)!r}).write_text("
                    "json.dumps({"
                    "'isolated': bool(sys.flags.isolated), "
                    "'dont_write_bytecode': bool(sys.flags.dont_write_bytecode), "
                    "'argv0': sys.argv[0]"
                    "}), encoding='utf-8')"
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (purelib / "hermes-sqlite-probe-startup.pth").write_text(
        "import _hermes_sqlite_probe_startup\n",
        encoding="utf-8",
    )
    startup_bytecode = Path(importlib.util.cache_from_source(str(startup_module)))
    assert not startup_bytecode.exists()

    info = probe_sqlite_runtime(venv_python)

    assert info is not None
    assert (
        json.loads(startup_observation.read_text(encoding="utf-8")),
        startup_bytecode.exists(),
    ) == (
        {
            "isolated": True,
            "dont_write_bytecode": True,
            "argv0": "-c",
        },
        False,
    )


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX executable probe stub")
def test_probe_uses_child_payload_and_sanitizes_python_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_python = tmp_path / "reported-python"
    payload = {
        "base_prefix": str(tmp_path / "reported-base"),
        "executable": str(fake_python),
        "python_version": [3, 11, 15],
        "sqlite_version": [9, 8, 7],
        "sqlite_version_string": "9.8.7-child",
        "sqlite_source_id": "child-source-id",
    }
    fake_python.write_text(
        "\n".join([
            "#!/bin/sh",
            '[ "$1" = "-I" ] && [ "$2" = "-B" ] && [ "$3" = "-c" ] || exit 10',
            '[ -z "${PYTHONHOME+x}" ] || exit 11',
            '[ -z "${PYTHONPATH+x}" ] || exit 12',
            f"printf '%s\\n' {shlex.quote(json.dumps(payload))}",
        ])
        + "\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "poison-home"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "poison-path"))

    info = probe_sqlite_runtime(fake_python)

    assert info is not None
    assert info.executable == fake_python
    assert info.base_prefix == tmp_path / "reported-base"
    assert info.sqlite_version == (9, 8, 7)
    assert info.sqlite_version_string == "9.8.7-child"
    assert info.sqlite_source_id == "child-source-id"
