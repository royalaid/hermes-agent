"""Regression: the desktop rebuild inside ``hermes update`` must stay visible.

On 2026-09-05 the Desktop hand-off killed a healthy ~10 minute desktop rebuild
with its 600 s idle watchdog. ``_run_logged_subprocess`` buffered the whole
build until it exited (nothing on ``hermes update``'s stdout for the duration),
and in ``--gateway`` mode the update.log mirror is a no-op, so
``_log_only_write`` discarded the captured output as well -- nothing on the
pipe, nothing in the log, nothing anywhere.

The helper now streams lines into update.log as they arrive, falls back to
the log file when the mirror is off, and prints a short progress line to
stdout every ``progress_every`` seconds.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import hermes_cli.update_cmd as update_cmd
from hermes_cli.main import _UpdateOutputStream

SLOW_CHILD = (
    "import sys, time\n"
    "for i in range(4):\n"
    "    print(f'build line {i}', flush=True)\n"
    "    time.sleep(0.15)\n"
)


def test_streams_lines_and_prints_progress_while_the_build_runs(monkeypatch):
    terminal = io.StringIO()
    log = io.StringIO()
    monkeypatch.setattr(sys, "stdout", _UpdateOutputStream(terminal, log))

    result = update_cmd._run_logged_subprocess(
        [sys.executable, "-c", SLOW_CHILD], progress_every=0.1
    )

    assert result.returncode == 0
    assert [f"build line {i}" in result.stdout for i in range(4)] == [True] * 4
    assert "build line 3" in log.getvalue(), "captured output must reach update.log"
    progress = [line for line in terminal.getvalue().splitlines() if "desktop build running" in line]
    assert progress, "a progress line must reach stdout while the child is still running"
    assert "build line" not in terminal.getvalue().replace("lines captured", ""), (
        "raw build output must not be echoed to the terminal"
    )


def test_quick_child_prints_no_progress_line(monkeypatch):
    terminal = io.StringIO()
    log = io.StringIO()
    monkeypatch.setattr(sys, "stdout", _UpdateOutputStream(terminal, log))

    result = update_cmd._run_logged_subprocess([sys.executable, "-c", "print('fast')"])

    assert result.returncode == 0
    assert "fast" in log.getvalue()
    assert terminal.getvalue() == ""


def test_falls_back_to_the_log_file_when_the_mirror_is_off(monkeypatch, tmp_path: Path):
    plain = io.StringIO()
    monkeypatch.setattr(sys, "stdout", plain)

    import hermes_cli.config as config

    monkeypatch.setattr(config, "get_hermes_home", lambda: tmp_path)

    result = update_cmd._run_logged_subprocess([sys.executable, "-c", "print('gateway-mode build')"])

    assert result.returncode == 0
    written = (tmp_path / "logs" / "update.log").read_text(encoding="utf-8")
    assert "gateway-mode build" in written
    assert plain.getvalue() == ""


def test_nonzero_exit_and_output_survive(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    import hermes_cli.config as config

    monkeypatch.setattr(config, "get_hermes_home", lambda: tmp_path)

    result = update_cmd._run_logged_subprocess(
        [sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"]
    )

    assert result.returncode == 3
    assert "boom" in result.stdout
