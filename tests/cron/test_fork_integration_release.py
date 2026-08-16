"""Characterization tests for the byte-pure-imported fork-integration release
system (``scripts/fork_integration/``).

These pin the U1 baseline behavior of the imported
``hermes-integration-release-windows.py`` without editing it: the manifest
loader's golden/rejection paths, ``patch_resolution``'s three branches, and
``stable_patch_id`` against a real git repo. Deeper fixture coverage (fake
worktrees, full ``main()`` flows) arrives with U2.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.fork_integration.release import mod as release


# ── Golden manifest loader ──────────────────────────────────────────────────


def test_load_manifest_golden_succeeds_against_imported_manifest() -> None:
    """The imported manifest.json (module.MANIFEST_PATH) is itself valid."""
    manifest = release.load_manifest()
    assert manifest["schema"] == 3
    assert manifest["components"]
    assert manifest["upstream_foundations"]


# ── Manifest loader rejections ──────────────────────────────────────────────
#
# Built from a minimal synthetic manifest (not a mutation of the real one —
# the real manifest carries a lot of unrelated historical data that would
# make these mutations fragile). ``load_manifest`` only checks the top-level
# key *set*, not the contents of "upstream"/"fork", so those can be trivial.

_HEX40_A = "a" * 40
_HEX40_B = "b" * 40
_HEX40_C = "c" * 40
_HEX40_D = "d" * 40


def _minimal_valid_manifest() -> dict[str, Any]:
    return {
        "schema": 3,
        "integration_branch": "test-integration-branch",
        "upstream": {"remote": "upstream", "ref": "refs/heads/main"},
        "fork": {"repository": "test-owner/test-fork"},
        "upstream_foundations": [
            {
                "id": "foundation-1",
                "repository": "upstream-owner/upstream-repo",
                "pull_request": 1,
                "approved_head": _HEX40_A,
                "base_ref": "main",
                "patches": [
                    {
                        "commit": _HEX40_B,
                        "stable_patch_id": _HEX40_C,
                        "subject": "fix: something",
                        "accepted_output_patch_ids": [_HEX40_C],
                        "reviewed_replacement": {
                            "commit": _HEX40_D,
                            "stable_patch_id": _HEX40_C,
                        },
                    }
                ],
            }
        ],
        "components": [
            {
                "id": "component-1",
                "source_ref": "fork/some-branch",
                "patches": [
                    {
                        "commit": _HEX40_D,
                        "stable_patch_id": _HEX40_C,
                        "subject": "fix: something",
                    }
                ],
            }
        ],
    }


def _write_manifest(tmp_path: Path, manifest: dict[str, Any]) -> Path:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def test_load_manifest_golden_synthetic_manifest_is_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity check: the synthetic fixture itself passes before mutating it."""
    manifest_path = _write_manifest(tmp_path, _minimal_valid_manifest())
    monkeypatch.setattr(release, "MANIFEST_PATH", manifest_path)
    release.load_manifest()  # must not raise


def test_load_manifest_rejects_foundation_replacement_identity_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Foundation replacement's stable_patch_id must be in accepted_output_patch_ids."""
    manifest = _minimal_valid_manifest()
    replacement = manifest["upstream_foundations"][0]["patches"][0]["reviewed_replacement"]
    replacement["stable_patch_id"] = _HEX40_A  # not in accepted_output_patch_ids=[_HEX40_C]
    manifest_path = _write_manifest(tmp_path, manifest)
    monkeypatch.setattr(release, "MANIFEST_PATH", manifest_path)

    with pytest.raises(RuntimeError, match="reviewed replacement identity is not accepted"):
        release.load_manifest()


def test_load_manifest_rejects_replacement_not_a_declared_component_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Foundation replacement (commit, stable_patch_id) must match a component patch."""
    manifest = _minimal_valid_manifest()
    # Keep the replacement internally consistent (accepted) but make the
    # component's own patch identity diverge from it.
    manifest["components"][0]["patches"][0]["stable_patch_id"] = _HEX40_B
    manifest_path = _write_manifest(tmp_path, manifest)
    monkeypatch.setattr(release, "MANIFEST_PATH", manifest_path)

    with pytest.raises(RuntimeError, match="reviewed replacement is not a declared component patch"):
        release.load_manifest()


def test_load_manifest_rejects_malformed_stable_patch_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-hex40 stable_patch_id on a replacement is a malformed-identity error."""
    manifest = _minimal_valid_manifest()
    manifest["upstream_foundations"][0]["patches"][0]["reviewed_replacement"]["stable_patch_id"] = "zzz"
    manifest_path = _write_manifest(tmp_path, manifest)
    monkeypatch.setattr(release, "MANIFEST_PATH", manifest_path)

    with pytest.raises(RuntimeError, match="malformed reviewed replacement"):
        release.load_manifest()


# ── patch_resolution branches ───────────────────────────────────────────────


def _patch(**overrides: Any) -> dict[str, Any]:
    base = {"commit": _HEX40_A, "stable_patch_id": "stable-source", "subject": "fix: thing"}
    base.update(overrides)
    return base


def test_patch_resolution_absorbs_when_candidate_patch_id_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-subject upstream candidate whose patch-id is accepted is absorbed."""
    patch = _patch(stable_patch_id="stable-source", accepted_output_patch_ids=["accepted-id"])

    def fake_git(*args: str, **kwargs: Any) -> str:
        assert args[0] == "log"
        return "cand1\n"

    def fake_stable_patch_id(ref: str) -> str:
        assert ref == "cand1"
        return "accepted-id"

    monkeypatch.setattr(release, "git", fake_git)
    monkeypatch.setattr(release, "stable_patch_id", fake_stable_patch_id)

    to_apply, absorbed = release.patch_resolution("upstream-ref", [patch])

    assert to_apply == []
    assert absorbed == [
        {
            "commit": _HEX40_A,
            "subject": "fix: thing",
            "upstream_commit": "cand1",
            "output_patch_id": "accepted-id",
        }
    ]


def test_patch_resolution_parks_same_subject_non_equivalent_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U6/KTD13 supersedes the U1 characterization pinned here.

    A same-subject candidate whose patch-id is NOT accepted used to raise
    "same-subject but non-equivalent" and skip the nightly. It now
    park-and-continues: the pin is queued for application exactly as if no
    candidate existed (R19). Detection itself is off here — no
    ``upstream_tip`` is supplied, so no proposal may be generated (see the
    proposals suite for the parking path with a tip)."""
    patch = _patch(stable_patch_id="stable-source")  # no reviewed_replacement

    def fake_git(*args: str, **kwargs: Any) -> str:
        assert args[0] == "log"
        return "cand2\n"

    def fake_stable_patch_id(ref: str) -> str:
        return "different-id"

    monkeypatch.setattr(release, "git", fake_git)
    monkeypatch.setattr(release, "stable_patch_id", fake_stable_patch_id)

    to_apply, absorbed = release.patch_resolution("upstream-ref", [patch])

    assert to_apply == [patch]
    assert absorbed == []
    assert release.PARKED_PINS == []


def test_patch_resolution_defers_to_apply_when_replacement_present_and_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-subject candidate that still doesn't match, but WITH a reviewed
    replacement declared, does not raise — the patch is queued in to_apply
    (apply_required_patches later substitutes the reviewed replacement)."""
    patch = _patch(
        stable_patch_id="stable-source",
        reviewed_replacement={"commit": _HEX40_D, "stable_patch_id": "stable-replacement"},
    )

    def fake_git(*args: str, **kwargs: Any) -> str:
        assert args[0] == "log"
        return "cand3\n"

    def fake_stable_patch_id(ref: str) -> str:
        return "different-id"

    monkeypatch.setattr(release, "git", fake_git)
    monkeypatch.setattr(release, "stable_patch_id", fake_stable_patch_id)

    to_apply, absorbed = release.patch_resolution("upstream-ref", [patch])

    assert absorbed == []
    assert to_apply == [patch]


# ── stable_patch_id against a real git repo ─────────────────────────────────


def test_stable_patch_id_matches_real_git_patch_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """module.stable_patch_id('HEAD') must equal the same two-step computation
    (`git show --format= --binary HEAD | git patch-id --stable`) run directly."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Fork Integration Test"], cwd=repo, check=True)
    (repo / "file.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add file"], cwd=repo, check=True)

    # release.run()'s `cwd` parameter defaults to the module-level WORKTREE
    # constant *bound at function-definition time* (it is a keyword-only
    # parameter default, evaluated once at `def run(...)`), so reassigning
    # module.WORKTREE after import does NOT change what an unqualified
    # `run(...)` call uses. Wrap `run` instead so the default cwd this test
    # needs is supplied explicitly on every call, exactly like a caller that
    # never sets `cwd` would experience if WORKTREE really did point at the
    # temp repo.
    original_run = release.run

    def run_in_repo(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("cwd", repo)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(release, "run", run_in_repo)

    actual = release.stable_patch_id("HEAD")

    show = subprocess.run(
        ["git", "show", "--format=", "--binary", "HEAD"],
        cwd=repo, text=True, capture_output=True, check=True,
    )
    expected = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=show.stdout, text=True, capture_output=True, check=True,
    ).stdout.split()[0]

    assert actual == expected


# ── Smoke test ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    ["main", "exclusive_lock", "patch_resolution", "verify_manifest_sources", "load_manifest"],
)
def test_expected_entry_points_exist(name: str) -> None:
    assert hasattr(release, name)
