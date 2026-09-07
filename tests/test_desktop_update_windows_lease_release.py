"""Source contracts: the Windows hand-off releases its MCP bridge lease and
reports whether ``hermes update`` left a receipt.

Every successful Desktop-driven update on 2026-09-05 left
``%LOCALAPPDATA%\\hermes\\.hermes-venv-quiesce`` on disk, naming the finished
hand-off's pid, until its 20-minute expiry: ``Adopt-McpBridgeLease`` rewrote
the lease under ``$PID`` and nothing ever released it. The same runs left no
``logs/update_receipts/latest.json`` and no surviving log said so.

``scripts/desktop-update/windows.ps1`` now

* remembers the exact lease body it adopted and, in the same ``finally`` that
  removes the update marker, moves the lease aside, compares, and deletes only
  that body (``Remove-BridgeLeaseIfOwned``; anything newer is put back), and
* logs ``update receipt: written ...`` / ``update receipt: NOT written by this
  run`` / ``update receipt: none on disk`` right after the update step
  (``Write-UpdateReceiptState``), timed against the step's start.

These assertions pin the wiring; the marker-release shape they mirror
(``Remove-MarkerIfOwned``) is exercised by ``-SelfTestMarker``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_PS1 = REPO_ROOT / "scripts" / "desktop-update" / "windows.ps1"


def _src() -> str:
    return WINDOWS_PS1.read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    match = re.search(rf"^function {re.escape(name)}\b", src, flags=re.M)
    assert match, f"{name} is not defined in windows.ps1"
    start = match.start()
    end = src.index("\nfunction ", start + 1)
    return src[start:end]


def test_adoption_remembers_the_exact_lease_body() -> None:
    body = _function_body(_src(), "Adopt-McpBridgeLease")
    assert "$script:BridgeLeaseBody = $updatedRaw" in body
    # Only after the exact reread proves the adoption landed.
    assert body.index('"$($check.lease_id)" -eq $BridgeLeaseId') < body.index(
        "$script:BridgeLeaseBody = $updatedRaw"
    )


def test_release_function_is_move_compare_delete() -> None:
    body = _function_body(_src(), "Remove-BridgeLeaseIfOwned")
    assert "if ($NoMarkerCleanup) { return }" in body
    assert "-not $script:BridgeLeaseBody" in body
    assert "[System.IO.File]::Move($BridgeLeasePath, $tombstone)" in body
    assert "if ($raw -eq $script:BridgeLeaseBody)" in body
    assert "[System.IO.File]::Delete($tombstone)" in body
    # A body that is not ours goes back where it was.
    assert "[System.IO.File]::Move($tombstone, $BridgeLeasePath)" in body
    # The tombstone name is one the Python gate recognises and retires.
    assert re.search(r'\.cas-release-\$PID-\$\(\[Guid\]::NewGuid\(\)', body)
    assert 'Write-HandoffLog "released MCP bridge lease (exact adopted body)"' in body


def test_release_runs_in_the_finally_after_marker_removal() -> None:
    src = _src()
    finalizer = src[src.rindex("} finally {") :]
    marker = finalizer.index("Remove-MarkerIfOwned")
    lease = finalizer.index("Remove-BridgeLeaseIfOwned")
    assert marker < lease
    # Same branch: the fail-closed (tree not safe) branch keeps both.
    assert "TreeSafeToFinalize" in finalizer
    assert finalizer[: finalizer.index("} else {")].count("Remove-BridgeLeaseIfOwned") == 0


def test_receipt_state_is_logged_after_the_update_step() -> None:
    src = _src()
    started = src.index("$updateStepStartedUtc = [DateTime]::UtcNow")
    step = src.index('$res = Invoke-HermesStep $pythonExe $updateArgs "update"')
    report = src.index("Write-UpdateReceiptState $updateStepStartedUtc")
    assert started < step < report
    # After the retry, so the retried run's receipt is the one judged.
    assert src.index('Write-HandoffLog "retry exit code: $($res.Code)"') < report

    body = _function_body(src, "Write-UpdateReceiptState")
    assert 'Join-Path (Join-Path $LogDir "update_receipts") "latest.json"' in body
    assert "update receipt: none on disk" in body
    assert "update receipt: written" in body
    assert "update receipt: NOT written by this run" in body
    assert "$fi.LastWriteTimeUtc -ge $SinceUtc" in body
