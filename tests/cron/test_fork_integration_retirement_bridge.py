"""U7->U6 retirement bridge: ``release.generate_retirement_proposals()``.

Tests the SEAM the release script owns: for each pin id
``ledger.retirement_candidates()`` names, call
``proposals.generate_or_refresh_retirement()`` and report the outcome.
``ledger.py`` itself never imports ``proposals.py`` (see both modules'
docstrings), so this bridge -- and its tests -- live at the release-script
layer. ``proposals.py``'s own retirement machinery (``derive_retirement_edit``,
the surgical removal text-editor, ``generate_or_refresh_retirement``,
``reverify_retirement``, and ``approve()``'s dispatch) is tested directly in
``test_fork_integration_proposals.py``.

Every test sets ``FORK_INTEGRATION_PROPOSALS_DIR`` -- never the real
``HERMES_HOME`` proposal store.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.fork_integration import proposals
from scripts.fork_integration.release import mod as release


def _init_repo(tmp_path: Path, name: str = "worktree") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Retirement Bridge Test"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def test_generate_retirement_proposals_creates_exactly_one_proposal_and_dedupes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(release, "WORKTREE", repo)
    monkeypatch.setenv(proposals.PROPOSALS_DIR_ENV, str(tmp_path / "store"))

    pin_id = "component:test-component:" + "1" * 40
    record = {
        "pin_id": pin_id, "kind": "component", "commit": "1" * 40,
        "stable_patch_id": "2" * 40, "subject": "fix: thing", "state": "absorbed-verbatim",
        "evidence": {"candidate_commit": "3" * 40, "candidate_patch_id": "2" * 40},
    }

    class FakeLedger:
        @staticmethod
        def default_history_path() -> Path:
            return tmp_path / "history.jsonl"

        @staticmethod
        def retirement_candidates(history_path: Path, *, repo_dir: Path, upstream_ref: str) -> list[str]:
            return [pin_id]

    monkeypatch.setattr(release, "_ledger_module", lambda: FakeLedger)

    generated = release.generate_retirement_proposals(upstream_ref="origin/main", records=[record])

    assert len(generated) == 1
    assert generated[0]["pin_id"] == pin_id
    assert generated[0]["refreshed"] is True

    store = proposals.ProposalStore(tmp_path / "store")
    artifacts = store.list_all()
    assert len(artifacts) == 1
    assert artifacts[0]["recommended_edit"] == {
        "operation": "remove_manifest_pin", "pin_kind": "component", "pin_id": "test-component",
        "patch_commit": "1" * 40, "patch_stable_patch_id": "2" * 40,
    }

    # Dedupe: the same candidate, generated again, does not fork a second artifact.
    generated_again = release.generate_retirement_proposals(upstream_ref="origin/main", records=[record])
    assert len(generated_again) == 1
    assert generated_again[0]["refreshed"] is False
    assert len(store.list_all()) == 1


def test_generate_retirement_proposals_no_candidates_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(release, "WORKTREE", repo)
    monkeypatch.setenv(proposals.PROPOSALS_DIR_ENV, str(tmp_path / "store"))

    class FakeLedger:
        @staticmethod
        def default_history_path() -> Path:
            return tmp_path / "history.jsonl"

        @staticmethod
        def retirement_candidates(history_path: Path, *, repo_dir: Path, upstream_ref: str) -> list[str]:
            return []

    monkeypatch.setattr(release, "_ledger_module", lambda: FakeLedger)

    assert release.generate_retirement_proposals(upstream_ref="origin/main", records=[]) == []


def test_generate_retirement_proposals_one_bad_pin_does_not_break_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-effort (KTD7): a pin whose retirement generation fails is logged
    and skipped, never raised into the release path."""
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(release, "WORKTREE", repo)
    monkeypatch.setattr(release, "log", lambda message: None)
    monkeypatch.setenv(proposals.PROPOSALS_DIR_ENV, str(tmp_path / "store"))

    good_id = "component:good-component:" + "1" * 40
    bad_id = "component:bad-component:" + "9" * 40
    good_record = {
        "pin_id": good_id, "kind": "component", "commit": "1" * 40,
        "stable_patch_id": "2" * 40, "subject": "fix: good",
        "evidence": {"candidate_commit": "3" * 40},
    }
    bad_record = {
        "pin_id": bad_id, "kind": "component", "commit": "9" * 40,
        "stable_patch_id": "8" * 40, "subject": "fix: bad",
        "evidence": {},  # no candidate_commit -> generate_or_refresh_retirement raises
    }

    class FakeLedger:
        @staticmethod
        def default_history_path() -> Path:
            return tmp_path / "history.jsonl"

        @staticmethod
        def retirement_candidates(history_path: Path, *, repo_dir: Path, upstream_ref: str) -> list[str]:
            return [bad_id, good_id]

    monkeypatch.setattr(release, "_ledger_module", lambda: FakeLedger)

    generated = release.generate_retirement_proposals(
        upstream_ref="origin/main", records=[good_record, bad_record],
    )

    assert [item["pin_id"] for item in generated] == [good_id]


def test_generate_retirement_proposals_ledger_exception_returns_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KTD7: the bridge itself is best-effort -- a broken ledger loader must
    not raise into the caller (``_fold_provenance``, which would otherwise
    lose the states/transitions it already computed)."""
    monkeypatch.setattr(release, "log", lambda message: None)

    def boom() -> Any:
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(release, "_ledger_module", boom)

    assert release.generate_retirement_proposals(upstream_ref="origin/main", records=[]) == []


@pytest.mark.parametrize("name", ["generate_retirement_proposals"])
def test_expected_entry_points_exist(name: str) -> None:
    assert hasattr(release, name)
