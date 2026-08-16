"""U7 completion (R7/KTD7/KTD14): every-run provenance wiring in the release
script's ``main()`` -- ``_fold_provenance()``, ``_emit_result()``, and
``fail()``'s single choke point.

Direct unit tests exercise these against monkeypatched seams so a ledger bug
can never be mistaken for a release-script bug. The U6 retirement bridge
seam lives in ``test_fork_integration_retirement_bridge.py``; the U11 canary
entry point lives in ``test_fork_integration_canary.py``.

Every test either passes an explicit history path or sets
``FORK_INTEGRATION_LEDGER_HISTORY_PATH`` -- never the real ``HERMES_HOME``
location (this repo's ``tests/conftest.py`` also sandboxes ``HERMES_HOME``
session-wide as a second line of defense).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.fork_integration import ledger
from scripts.fork_integration.release import mod as release

_HISTORY_ENV = ledger._HISTORY_ENV_OVERRIDE  # "FORK_INTEGRATION_LEDGER_HISTORY_PATH"


# ── shared helpers ───────────────────────────────────────────────────────────


def _init_repo(tmp_path: Path, name: str = "worktree") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Provenance Wiring Test"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def _minimal_manifest_document() -> dict[str, Any]:
    return {
        "schema": 3,
        "integration_branch": "fork-integration",
        "upstream": {"remote": "origin", "ref": "refs/heads/main"},
        "upstream_foundations": [{
            "id": "test-foundation",
            "repository": "owner/upstream",
            "pull_request": 1,
            "approved_head": "a" * 40,
            "base_ref": "main",
            "patches": [{"commit": "b" * 40, "stable_patch_id": "c" * 40, "subject": "foundation subject"}],
        }],
        "fork": {"remote": "fork", "repository": "owner/fork"},
        "components": [{
            "id": "test-component", "source_ref": "d" * 40,
            "patches": [{"commit": "d" * 40, "stable_patch_id": "e" * 40, "subject": "component subject"}],
        }],
    }


# ── _fold_provenance: direct unit tests ─────────────────────────────────────


def test_fold_provenance_folds_states_transitions_and_retirement_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NOTE ON MONKEYPATCH SCOPE: ``release._ledger_module()`` loads
    ``ledger.py`` by path into its OWN, SEPARATE module instance (same
    reason as ``_proposals_module()`` -- see that function's docstring),
    distinct from this test file's ``from scripts.fork_integration import
    ledger`` import. Patching attributes on the latter has no effect on the
    former; the history path must be redirected via its env-var override
    instead (works identically for either module instance, since both read
    ``os.environ`` fresh on every call)."""
    repo = _init_repo(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_minimal_manifest_document()), encoding="utf-8")
    history_path = tmp_path / "history.jsonl"

    monkeypatch.setattr(release, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(release, "WORKTREE", repo)
    monkeypatch.setattr(release, "BLOCKLIST_PATH", tmp_path / "blocklist.json")
    monkeypatch.setenv(_HISTORY_ENV, str(history_path))

    payload: dict[str, Any] = {"ok": True}
    release._fold_provenance(payload, dry_run=False)

    assert "provenance" in payload
    provenance = payload["provenance"]
    assert set(provenance) == {"states", "transitions", "retirement_candidates"}
    component_pin = "component:test-component:" + "d" * 40
    foundation_pin = "foundation:test-foundation:" + "b" * 40
    assert provenance["states"] == {component_pin: "private-only", foundation_pin: "private-only"}
    assert {"pin_id": component_pin, "from": None, "to": "private-only"} in provenance["transitions"]
    assert {"pin_id": foundation_pin, "from": None, "to": "private-only"} in provenance["transitions"]
    assert provenance["retirement_candidates"] == []
    # Non-dry-run appends exactly one JSONL line.
    assert len(history_path.read_text(encoding="utf-8").splitlines()) == 1


def test_fold_provenance_dry_run_derives_but_never_appends_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_minimal_manifest_document()), encoding="utf-8")
    history_path = tmp_path / "history.jsonl"

    monkeypatch.setattr(release, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(release, "WORKTREE", repo)
    monkeypatch.setattr(release, "BLOCKLIST_PATH", tmp_path / "blocklist.json")
    monkeypatch.setenv(_HISTORY_ENV, str(history_path))

    real_ledger = release._ledger_module()
    append_calls: list[Any] = []
    monkeypatch.setattr(real_ledger, "append_history", lambda *a, **k: append_calls.append((a, k)))

    payload: dict[str, Any] = {"ok": True, "dry_run": True}
    release._fold_provenance(payload, dry_run=True)

    assert "provenance" in payload
    assert payload["provenance"]["states"]
    assert append_calls == []
    assert not history_path.exists()


def test_fold_provenance_ledger_exception_is_reported_never_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KTD7: provenance informs, it never gates -- and never breaks the run
    it is attached to."""

    def boom() -> Any:
        raise RuntimeError("ledger module exploded")

    monkeypatch.setattr(release, "_ledger_module", boom)
    monkeypatch.setattr(release, "log", lambda message: None)

    payload: dict[str, Any] = {"ok": False, "error": "unrelated failure"}
    release._fold_provenance(payload, dry_run=False)  # must not raise

    assert payload["provenance"] == {"error": "RuntimeError: ledger module exploded"}
    assert payload["ok"] is False  # the run's own outcome is untouched


def test_fold_provenance_uses_this_runs_fetched_upstream_when_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_minimal_manifest_document()), encoding="utf-8")
    history_path = tmp_path / "history.jsonl"

    monkeypatch.setattr(release, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(release, "WORKTREE", repo)
    monkeypatch.setattr(release, "BLOCKLIST_PATH", tmp_path / "blocklist.json")
    monkeypatch.setenv(_HISTORY_ENV, str(history_path))

    real_ledger = release._ledger_module()  # the SAME instance _fold_provenance() uses
    seen_refs: list[str] = []
    original_derive = real_ledger.derive

    def spy_derive(*args: Any, **kwargs: Any) -> Any:
        seen_refs.append(kwargs.get("upstream_ref"))
        return original_derive(*args, **kwargs)

    monkeypatch.setattr(real_ledger, "derive", spy_derive)

    release.reset_run_provenance_state()
    try:
        release._fold_provenance({}, dry_run=False)
        assert seen_refs == [release._fallback_upstream_ref()]

        release.record_run_upstream_ref("f" * 40)
        release._fold_provenance({}, dry_run=False)
        assert seen_refs[-1] == "f" * 40
    finally:
        release.reset_run_provenance_state()


# ── fail() carries provenance + canary flag (single choke point) ───────────


def test_fail_folds_provenance_into_its_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_minimal_manifest_document()), encoding="utf-8")

    monkeypatch.setattr(release, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(release, "WORKTREE", repo)
    monkeypatch.setattr(release, "BLOCKLIST_PATH", tmp_path / "blocklist.json")
    monkeypatch.setattr(release, "LOG_PATH", tmp_path / "logs" / "release.log")
    monkeypatch.setenv(_HISTORY_ENV, str(tmp_path / "history.jsonl"))
    release.reset_run_provenance_state()
    release.reset_run_canary_state()

    with pytest.raises(SystemExit) as excinfo:
        release.fail("simulated failure")

    assert excinfo.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == "simulated failure"
    assert "provenance" in payload
    assert "canary" not in payload  # not a canary run


def test_fail_folds_canary_flag_when_canary_manifest_is_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The canary flag is folded through the SAME choke point as provenance
    (see ``fail()``); the U11 test suite covers ``--canary-manifest``'s own
    mechanics (``apply_canary_manifest``, the shipped fixture, the real
    ``main()`` run) in ``test_fork_integration_canary.py``."""
    repo = _init_repo(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_minimal_manifest_document()), encoding="utf-8")

    monkeypatch.setattr(release, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(release, "WORKTREE", repo)
    monkeypatch.setattr(release, "BLOCKLIST_PATH", tmp_path / "blocklist.json")
    monkeypatch.setattr(release, "LOG_PATH", tmp_path / "logs" / "release.log")
    monkeypatch.setattr(release, "CANARY_MANIFEST_ACTIVE", True)
    monkeypatch.setenv(_HISTORY_ENV, str(tmp_path / "history.jsonl"))
    release.reset_run_provenance_state()

    with pytest.raises(SystemExit):
        release.fail("canary-forced failure")

    payload = json.loads(capsys.readouterr().out)
    assert payload["canary"] is True


# ── smoke: expected entry points exist ──────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "_ledger_module", "_fold_provenance", "_fold_canary_flag", "_emit_result",
        "reset_run_provenance_state", "record_run_upstream_ref", "_fallback_upstream_ref",
    ],
)
def test_expected_entry_points_exist(name: str) -> None:
    assert hasattr(release, name)
