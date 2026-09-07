"""Regression: the Windows update hand-off must show step progress while it runs.

``scripts/desktop-update/windows.ps1`` used to collect a step's stdout/stderr in
memory and write it to the hand-off log only after ``Invoke-HermesStep``
returned. ``hermes update`` spends minutes in its desktop rebuild with nothing
on its pipes (the build streams to ``logs/update.log``), so the hand-off
window showed a single ``running:`` line for the whole update (2026-09-05).

The fix streams complete lines as they arrive and writes a ``still running``
heartbeat with the update log's size, age and last line during pipe-silent
stretches. These are source-contract assertions; the executable proof is the
``livelog`` arm of ``-SelfTestPipeDrain`` (``windows_only``, exercised by
``tests/test_desktop_update_windows_pipe_drain.py``): the step's own child
polls the hand-off log for its first line and encodes the answer in its exit
code, so a pass cannot be a timing coincidence.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_PS1 = REPO_ROOT / "scripts" / "desktop-update" / "windows.ps1"


def _src() -> str:
    return WINDOWS_PS1.read_text(encoding="utf-8")


def _invoke_step(src: str) -> str:
    start = src.index("function Invoke-HermesStep(")
    end = src.index("\n$finalCode = 1", start)
    return src[start:end]


def test_step_lines_are_written_inside_the_drain_loop() -> None:
    body = _invoke_step(_src())
    assert "function Write-StepLines" in _src()
    loop = body.index("while ($true) {")
    exited = body.index("if ($proc.HasExited) {", loop)
    streamed = body.index("$outFlushed = Write-StepLines $outSink $outFlushed $outPrefix", loop)
    assert loop < streamed < exited, (
        "stdout must be flushed to the hand-off log inside the drain loop, before the "
        "exit check -- otherwise the window is silent until the step ends"
    )
    assert "$errFlushed = Write-StepLines $errSink $errFlushed $errPrefix" in body


def test_step_output_is_not_dumped_again_after_exit() -> None:
    body = _invoke_step(_src())
    assert 'foreach ($ln in ($outText -split "`r?`n"))' not in body, (
        "the post-exit dump would log every line a second time now that lines stream live"
    )
    assert "Write-StepLines $outSink $outFlushed $outPrefix -Final" in body
    assert "Write-StepLines $errSink $errFlushed $errPrefix -Final" in body


def test_heartbeat_covers_pipe_silent_stretches() -> None:
    src = _src()
    assert "HERMES_UPDATE_STEP_HEARTBEAT_SECONDS" in src
    assert "$script:StepHeartbeatSeconds = 30" in src
    assert "function Get-StepHeartbeatLine" in src
    assert "still running: " in src
    body = _invoke_step(src)
    assert "Write-HandoffLog (Get-StepHeartbeatLine $Tag $proc.Id $stepStartedAt $lastProgressAt)" in body
    # The heartbeat names the update log's state: that file, not stdout, is
    # where a real build's progress lives.
    heartbeat = src[src.index("function Get-StepHeartbeatLine"):]
    heartbeat = heartbeat[: heartbeat.index("\n}\n") + 3]
    assert "$script:StepProgressLogPath" in heartbeat
    assert "Get-FileTailLine" in heartbeat


def test_ui_stage_is_still_orchestrator_owned() -> None:
    # Child output is logged, never parsed into the progress window; the
    # progress contract test relies on stages coming only from control flow.
    body = _invoke_step(_src())
    assert "Publish-UiProgress" not in body
    assert "$script:UiStage" not in body


def test_self_test_has_a_livelog_arm() -> None:
    src = _src()
    assert "livelog" in src
    assert '"livelog"' in src, "the livelog arm must run through the real Invoke-HermesStep"
    assert "live line one" in src
    assert "livelog arm exit code" in src
