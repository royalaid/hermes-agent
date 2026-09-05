"""Regression: the Windows update hand-off log must survive concurrent readers.

``scripts/desktop-update/windows.ps1`` appended to
``logs/desktop-update-handoff.log`` with ``Add-Content``. Twice on 2026-09-05
the console filled with ``Add-Content : The process cannot access the file ...
because it is being used by another process`` and the log ended mid-update
while the console kept going -- first under a git-bash ``tail -F`` that held
the file for minutes, then under nothing more than a ``wc -l`` / ``tail -n +N``
poll every 15 s (each open/close in milliseconds).

Root cause, measured under Windows PowerShell 5.1 with
``[System.IO.File]::Open`` probes against every holder shape:

* ``Add-Content`` opens the log with ``FileShare.Write`` ONLY. Every log
  reader -- git-bash ``tail``/``wc``, ``Get-Content -Tail``, any .NET reader --
  holds Read access, so the two handles are mutually exclusive: while a reader
  has the file, Add-Content's open is refused (70/500 appends lost under a
  tight git-bash poll, 287/300 under a ``Get-Content -Tail`` loop), and while
  Add-Content has it the reader is refused.
* Add-Content's ``IOException`` is a NON-terminating error. Under the
  script's ``$ErrorActionPreference = "Continue"`` the ``try { Add-Content }
  catch { retry }`` wrapper printed the red error, fell through to ``return``
  and never entered ``catch``: the retry never ran and the drop counter never
  moved.

The fix appends through a ``FileStream`` opened ``Append``/``Write`` with
``FileShare.ReadWrite | Delete`` (0/500 and 0/300 failures under the same
pollers), UTF-8 without BOM, one write per batch, a bounded retry on the .NET
exception (which IS terminating under any preference), and honest drop
accounting: lines that still cannot be written are counted and announced by a
``WARNING`` line on the next successful write. These are source-contract
assertions; the executable proof is ``-SelfTestLog`` (``windows_only``
below), which holds the log open with the reader shape that broke Add-Content
and asserts that no line was lost.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_PS1 = REPO_ROOT / "scripts" / "desktop-update" / "windows.ps1"


def _src() -> str:
    return WINDOWS_PS1.read_text(encoding="utf-8")


def _function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    end = src.index("\nfunction ", start + 1)
    return src[start:end]


def _add_lines(src: str) -> str:
    return _function(src, "Add-HandoffLogLines")


def _code(body: str) -> str:
    # The function's comment block names the cmdlets it replaces; only code
    # lines count for the absence checks.
    return "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))


def test_log_append_never_goes_through_add_content() -> None:
    src = _src()
    code = _code(_add_lines(src))
    assert "Add-Content" not in code, (
        "Add-Content opens the hand-off log with FileShare.Write only, which "
        "is refused while any reader (tail -F, wc -l, Get-Content -Tail) has "
        "the file open"
    )
    assert "Set-Content" not in code and "Out-File" not in code
    # The pipe-drain fixture's child appends to ITS OWN progress log with
    # Add-Content; that file is not the hand-off log. Pin the hand-off log
    # path specifically.
    assert "Add-Content -LiteralPath $LogPath" not in src


def test_log_append_opens_the_file_with_read_write_delete_sharing() -> None:
    body = _add_lines(_src())
    assert "[System.IO.FileMode]::Append" in body
    assert "[System.IO.FileAccess]::Write" in body
    assert "[System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete" in body, (
        "the appender must share Read, Write and Delete so every reader shape "
        "(and a log rotation) can coexist with it"
    )


def test_log_append_is_utf8_without_bom_and_one_write_per_batch() -> None:
    src = _src()
    body = _add_lines(src)
    assert "$script:HandoffLogUtf8 = [System.Text.UTF8Encoding]::new($false)" in src
    assert "$script:HandoffLogUtf8.GetBytes" in body
    # One encoded payload per call: the batch is joined before the stream is
    # opened, not written line by line.
    assert '($payload -join "`r`n") + "`r`n"' in body
    assert body.count("$stream.Write(") == 1


def test_log_append_retries_on_terminating_exceptions_and_counts_drops() -> None:
    src = _src()
    body = _add_lines(src)
    # The script runs with Continue; a cmdlet's non-terminating IOException
    # never reaches catch under that preference. The retry must be driven by
    # the .NET constructor exception, which is terminating regardless.
    assert '$ErrorActionPreference = "Continue"' in src
    assert "for ($attempt = 0; $attempt -lt $script:HandoffLogRetries" in body
    assert "} catch {" in body
    assert "Start-Sleep -Milliseconds $script:HandoffLogRetryMs" in body
    assert "$script:HandoffLogDrops += $Lines.Count" in body
    assert "$script:HandoffLogDrops = 0" in body
    assert not re.search(r"^\s*throw\b", _code(body), re.M), (
        "a lost log line must never throw into the update path"
    )


def test_dropped_lines_are_announced_on_the_next_successful_write() -> None:
    body = _add_lines(_src())
    notice = body.index("WARNING: {1} hand-off log line(s) could not be written")
    write = body.index("$stream.Write(")
    assert notice < write, "the drop notice must be part of the next successful write's payload"
    assert "$payload = @($notice) + $Lines" in body


def test_line_format_and_console_echo_are_unchanged() -> None:
    src = _src()
    write_log = _function(src, "Write-HandoffLog")
    assert '"{0:yyyy-MM-ddTHH:mm:ssK} {1}" -f (Get-Date), $Message' in write_log
    assert "Add-HandoffLogLines @($line)" in write_log
    assert "Write-Host $line" in write_log
    step_lines = _function(src, "Write-StepLines")
    assert "Add-HandoffLogLines $lines" in step_lines
    assert "foreach ($line in $lines) { Write-Host $line }" in step_lines


def test_progress_server_runspace_never_touches_the_log() -> None:
    # Only the main thread appends to the hand-off log. The /progress runspace
    # serves $State over loopback and must stay that way: a second writer in
    # another thread would reintroduce a sharing race we own ourselves.
    src = _src()
    start = src.index("[void]$ps.AddScript({")
    end = src.index("[void]$ps.BeginInvoke()", start)
    runspace = src[start:end]
    for forbidden in ("Write-HandoffLog", "Add-HandoffLogLines", "$LogPath", "Add-Content"):
        assert forbidden not in runspace


def test_self_test_has_a_log_arm() -> None:
    src = _src()
    assert "[switch]$SelfTestLog" in src
    assert (
        "if (-not $SelfTestUi -and -not $SelfTestPipeDrain -and -not $SelfTestLog -and -not $InstallRoot)"
        in src
    )
    assert "LOG SELF-TEST: PASS" in src
    assert "LOG SELF-TEST: FAIL" in src
    # Both arms: the reader shape that broke Add-Content, and the floor.
    assert "[System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)" in src
    assert "[System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)" in src


@pytest.mark.windows_only
def test_log_survives_a_concurrent_reader_and_accounts_for_blocked_writes(
    tmp_path: Path,
) -> None:
    """Execute the real logging path while a second process holds the log.

    A separate PowerShell process holds ``desktop-update-handoff.log`` open
    with Read access and ``FileShare.ReadWrite`` -- the handle a ``tail -F`` /
    ``Get-Content -Tail`` holds -- for the whole run. ``-SelfTestLog`` then
    writes 50 lines and a 2-line batch through ``Write-HandoffLog`` /
    ``Add-HandoffLogLines`` (all must land), and separately writes 3 lines
    under a ``FileShare.Read`` holder that refuses every writer (all must be
    counted and announced by a ``WARNING`` after release). The fixture reads
    the log back itself; this test re-checks the file so the proof does not
    depend on the fixture's own bookkeeping, and asserts that no
    ``Add-Content`` error text reached the console.
    """
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = (
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    if not powershell.is_file():
        pytest.skip(f"Windows PowerShell not found at {powershell}")

    # No -InstallRoot => the script logs under $env:TEMP\logs.
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log = log_dir / "desktop-update-handoff.log"

    holder_script = (
        f"$fs = [System.IO.File]::Open('{log}', 'OpenOrCreate', 'Read', 'ReadWrite'); "
        "Write-Output holding; [Console]::Out.Flush(); Start-Sleep -Seconds 120"
    )
    holder = subprocess.Popen(
        [str(powershell), "-NoProfile", "-Command", holder_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert holder.stdout is not None
        first = holder.stdout.readline().strip()
        assert first == "holding", f"reader holder did not start: {first!r}"
        assert log.is_file(), "the holder should have created the (empty) log"

        env = {**os.environ, "TEMP": str(tmp_path), "TMP": str(tmp_path)}
        result = subprocess.run(
            [
                str(powershell),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(WINDOWS_PS1),
                "-SelfTestLog",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            cwd=str(REPO_ROOT),
        )
    finally:
        holder.kill()
        holder.wait()

    console = result.stdout + result.stderr
    assert "LOG SELF-TEST: PASS" in result.stdout, (
        "The hand-off log lost lines to a concurrent reader, or blocked writes "
        f"were not accounted for. Fixture diagnosis follows.\n--- stdout ---\n"
        f"{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert result.returncode == 0, (
        f"-SelfTestLog exited {result.returncode}.\n--- stdout ---\n"
        f"{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "being used by another process" not in console, (
        "a sharing violation reached the console: the appender is not sharing "
        "the file with readers"
    )

    data = log.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), "fresh log must not start with a UTF-8 BOM"
    text = data.decode("utf-8")
    assert text.count("SELF-TEST: shared-reader line ") == 50
    assert text.count("SELF-TEST: shared-reader batch ") == 2
    assert "SELF-TEST: blocked line" not in text
    assert "WARNING: 3 hand-off log line(s) could not be written" in text
    assert re.search(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} SELF-TEST: after release\r?$",
        text,
        re.M,
    ), "line format must stay '{yyyy-MM-ddTHH:mm:ssK} message'"
