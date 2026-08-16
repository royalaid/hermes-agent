"""U11 canary entry point: ``--canary-manifest``, ``apply_canary_manifest()``,
and the shipped ``canary-manifest.example.json`` fixture.

The end-to-end test drives ``release.main()`` for real against a throwaway
local bare-repo + clone (real git plumbing, no network) so the wiring is
proven at the integration boundary: the run reaches (and fails inside)
``verify_manifest_sources()`` on the canary pin, the existing failure path
runs (pre-push restoration, the investigator spawn, ``fail()``), and the
result JSON carries ``"canary": true``.

Every test that calls ``apply_canary_manifest()`` (directly, or indirectly
via ``release.main()``) restores the module's manifest-derived globals
afterward -- ``apply_canary_manifest()`` mutates them directly (not through
monkeypatch), and a leaked canary manifest would break every later test in
the same pytest process.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.fork_integration import ledger
from scripts.fork_integration.release import mod as release

_HISTORY_ENV = ledger._HISTORY_ENV_OVERRIDE


def _minimal_manifest_document() -> dict[str, Any]:
    deadbeef = "deadbeef" * 5
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
            "id": "test-component", "source_ref": deadbeef,
            "patches": [{"commit": deadbeef, "stable_patch_id": deadbeef, "subject": "canary: forced verify failure"}],
        }],
    }


_MANIFEST_GLOBAL_NAMES = (
    "MANIFEST_PATH", "MANIFEST", "BRANCH", "UPSTREAM_REMOTE", "UPSTREAM_REF",
    "REPOSITORY", "UPSTREAM_FOUNDATIONS", "FOUNDATION_PATCHES", "REQUIRED_PATCHES",
)


def _snapshot_manifest_globals() -> dict[str, Any]:
    return {name: getattr(release, name) for name in _MANIFEST_GLOBAL_NAMES}


def _restore_manifest_globals(snapshot: dict[str, Any]) -> None:
    for name, value in snapshot.items():
        setattr(release, name, value)
    release.reset_run_canary_state()


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Canary Test"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


# ── the shipped fixture ──────────────────────────────────────────────────────


def test_canary_manifest_example_file_is_schema_valid_and_its_pin_is_unresolvable(
    tmp_path: Path,
) -> None:
    """The shipped fixture itself: loader-green, and its deadbeef pin exists
    in no real git repository (a fresh throwaway repo is proof enough --
    nothing manufactures that SHA by accident)."""
    canary_path = release.SCRIPT_DIR / "canary-manifest.example.json"
    assert canary_path.is_file()
    manifest = json.loads(canary_path.read_text(encoding="utf-8"))
    canary_component = next(c for c in manifest["components"] if c["id"] == "canary-forced-verify-failure")
    deadbeef = canary_component["patches"][0]["commit"]
    assert deadbeef == "deadbeef" * 5
    assert canary_component["patches"][0]["subject"] == "canary: forced verify failure"

    snapshot = _snapshot_manifest_globals()
    try:
        release.MANIFEST_PATH = canary_path
        loaded = release.load_manifest()  # must not raise -- schema-valid
    finally:
        _restore_manifest_globals(snapshot)
    assert any(c["id"] == "canary-forced-verify-failure" for c in loaded["components"])

    repo = _init_repo(tmp_path)
    result = subprocess.run(
        ["git", "cat-file", "-t", deadbeef], cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode != 0  # unresolvable: never a real commit


def test_canary_manifest_filename_is_outside_syncs_tracked_set() -> None:
    """R14/U11: the run-start integrity gate must never stamp-check the
    canary file -- proved structurally, not by special-casing the gate."""
    sync = release._sync_module()
    assert "canary-manifest.example.json" not in sync.TRACKED_SET
    assert release.MANIFEST_PATH.name in sync.TRACKED_SET


# ── apply_canary_manifest() ──────────────────────────────────────────────────


def test_apply_canary_manifest_swaps_globals_for_this_run_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release, "log", lambda message: None)
    document = _minimal_manifest_document()
    document["integration_branch"] = "canary-test-branch"
    path = tmp_path / "canary.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    snapshot = _snapshot_manifest_globals()
    try:
        assert release.CANARY_MANIFEST_ACTIVE is False
        release.apply_canary_manifest(str(path))
        assert release.CANARY_MANIFEST_ACTIVE is True
        assert release.MANIFEST_PATH == path
        assert release.BRANCH == "canary-test-branch"
        assert release.UPSTREAM_FOUNDATIONS == document["upstream_foundations"]
    finally:
        _restore_manifest_globals(snapshot)
    assert release.CANARY_MANIFEST_ACTIVE is False
    assert release.MANIFEST_PATH != path


def test_canary_failure_investigator_receives_expected_canary_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deliberate U11 failure must remain identifiable after sanitization.

    The investigator only receives the incident artifact, not the release
    process's result JSON.  Preserve the canary context there so it can
    classify this known forced failure without treating the deadbeef pin as a
    release-system defect.
    """
    received: dict[str, Any] = {}

    class FakeInvestigator:
        @staticmethod
        def record_failure(**kwargs: Any) -> dict[str, Any]:
            received.update(kwargs)
            return {"signature": "test", "spawn": False, "occurrences": 1}

        @staticmethod
        def maybe_launch_investigator(_result: dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(release, "_failure_investigator_module", lambda: FakeInvestigator)
    monkeypatch.setattr(release, "CANARY_MANIFEST_ACTIVE", True)
    monkeypatch.setattr(release, "log", lambda _message: None)

    release.launch_failure_investigator(stage="verify_manifest", error="forced canary failure")

    assert received["canary"] is True


# ── end-to-end: a real main() run under --canary-manifest ──────────────────


def _init_bare_and_worktree(tmp_path: Path, branch: str) -> Path:
    """A local bare repo (standing in for BOTH the upstream and fork
    remotes) plus a clone checked out on ``branch`` with "origin" and
    "fork" remotes -- real git plumbing, no network, so ``main()``'s own
    ``git fetch``/``rev-parse`` calls succeed for real before the canary
    manifest's ``verify_manifest_sources()`` failure fires."""
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=seed, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=seed, check=True)
    subprocess.run(["git", "config", "user.name", "Canary Test"], cwd=seed, check=True)
    (seed / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=seed, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", str(bare), "main:main", f"main:{branch}"], cwd=seed, check=True)

    repo = tmp_path / "worktree"
    subprocess.run(["git", "clone", "-q", str(bare), str(repo)], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Canary Test"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", branch], cwd=repo, check=True)
    subprocess.run(["git", "remote", "rename", "origin", "fork"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=repo, check=True)
    return repo


def test_canary_manifest_run_fails_at_verify_manifest_sources_with_canary_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The U11 end-to-end wiring: a real ``main()`` run under
    ``--canary-manifest`` reaches (and fails inside) ``verify_manifest_sources``
    on the canary pin, invokes the existing failure path -- pre-push
    restoration, the investigator spawn (spied here), ``fail()`` -- and the
    result JSON carries ``"canary": true``. Also proves the integrity gate
    still runs (and, per the structural test above, never stamp-checks the
    canary file itself)."""
    repo = _init_bare_and_worktree(tmp_path, branch="fork-integration")
    canary_path = tmp_path / "canary-manifest.json"
    canary_path.write_text(json.dumps(_minimal_manifest_document()), encoding="utf-8")

    original_run = release.run

    def run_in_repo(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("cwd", repo)
        return original_run(*args, **kwargs)

    investigator_calls: list[dict[str, str]] = []

    monkeypatch.setattr(release, "run", run_in_repo)
    monkeypatch.setattr(release, "WORKTREE", repo)
    monkeypatch.setattr(release, "BLOCKLIST_PATH", tmp_path / "blocklist.json")
    monkeypatch.setattr(release, "HERMES_HOME", tmp_path / "hermes")
    monkeypatch.setattr(release, "LOG_PATH", tmp_path / "logs" / "release.log")
    monkeypatch.setattr(release, "LOCK_PATH", tmp_path / "locks" / "release.lock")
    monkeypatch.setattr(release, "ensure_clean_identity", lambda: (
        release.git("rev-parse", "HEAD"), release.git("rev-parse", "HEAD"),
    ))
    monkeypatch.setattr(release, "synchronize_to_published_head", lambda local, published: published)
    monkeypatch.setattr(release, "verify_upstream_foundations", lambda: [])
    monkeypatch.setattr(release, "emit_fleet_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(release, "resolve_failure_investigator_success", lambda: None)
    monkeypatch.setattr(
        release, "launch_failure_investigator",
        lambda *, stage, error: investigator_calls.append({"stage": stage, "error": error}),
    )
    monkeypatch.setenv(_HISTORY_ENV, str(tmp_path / "history.jsonl"))
    monkeypatch.setattr(sys, "argv", [
        "hermes-integration-release-windows.py", "--canary-manifest", str(canary_path),
    ])

    snapshot = _snapshot_manifest_globals()
    try:
        with pytest.raises(SystemExit) as excinfo:
            release.main()
    finally:
        _restore_manifest_globals(snapshot)

    assert excinfo.value.code == 1
    assert len(investigator_calls) == 1
    assert investigator_calls[0]["stage"] == "verify_manifest"
    assert "test-component" in investigator_calls[0]["error"]
    assert "unavailable" in investigator_calls[0]["error"]

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    parsed = [json.loads(line) for line in lines]
    final = parsed[-1]
    assert "stage" not in final  # the final line is still the plain result object
    assert final["ok"] is False
    assert final["canary"] is True
    assert "provenance" in final
    assert release.CANARY_MANIFEST_ACTIVE is False  # restored, not leaked to later tests


@pytest.mark.parametrize("name", ["apply_canary_manifest", "reset_run_canary_state", "CANARY_MANIFEST_ACTIVE"])
def test_expected_entry_points_exist(name: str) -> None:
    assert hasattr(release, name)
