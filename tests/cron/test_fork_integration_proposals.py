"""U6: reconciliation proposals, park-and-continue, blocklist, keep-refs.

Every scenario runs against a REAL throwaway git repository. Patch identity,
ancestry, subject equality and interdiffs are the entire subject matter here,
so mocking git would only test the mock. The release-script side is exercised
through its documented monkeypatch seams (``run``/``WORKTREE``/``MANIFEST``),
the same idiom ``test_fork_integration_release.py`` established.

The acceptance proof at the bottom (``#63047 replay``) is the one that
matters most: the real three-SHA churn, replayed, must absorb with exactly
one human approval, zero manual manifest edits, and no skipped run.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.fork_integration import proposals
from scripts.fork_integration.release import mod as release

PIN_SUBJECT = "fix(desktop): bound inflight journal persistence (#63047)"


# ── git fixture helpers ──────────────────────────────────────────────────────


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        args, cwd=repo, text=True, encoding="utf-8", capture_output=True, check=True,
    ).stdout.strip()


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True)
    _run(repo, "git", "init", "-q")
    _run(repo, "git", "config", "user.email", "test@example.invalid")
    _run(repo, "git", "config", "user.name", "Fork Integration Proposals Test")
    _run(repo, "git", "config", "core.autocrlf", "false")
    _run(repo, "git", "config", "commit.gpgsign", "false")
    return repo


def _commit(repo: Path, files: dict[str, str], subject: str) -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _run(repo, "git", "add", "-A")
    _run(repo, "git", "commit", "-q", "-m", subject)
    return _run(repo, "git", "rev-parse", "HEAD")


class Churn:
    """A base commit, a pinned fork commit, and a mutable upstream line."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = _init_repo(tmp_path)
        self.base = _commit(self.repo, {"base.txt": "base\n"}, "base")
        self.trunk = _run(self.repo, "git", "branch", "--show-current")
        self.git = proposals.Git(self.repo)

    def pin(self, files: dict[str, str], subject: str = PIN_SUBJECT) -> dict[str, Any]:
        _run(self.repo, "git", "checkout", "-q", "-b", "pin-source", self.base)
        commit = _commit(self.repo, files, subject)
        _run(self.repo, "git", "checkout", "-q", self.trunk)
        return {
            "commit": commit,
            "subject": subject,
            "stable_patch_id": self.git.patch_id(commit),
        }

    def upstream(self, files: dict[str, str], subject: str) -> str:
        return _commit(self.repo, files, subject)

    def rewind_upstream(self, to: str) -> None:
        _run(self.repo, "git", "reset", "-q", "--hard", to)

    def tip(self) -> str:
        return _run(self.repo, "git", "rev-parse", "HEAD")


def _pin_record(churn: Churn, patch: dict[str, Any], *, kind: str = "component", pin_id: str = "test-component") -> dict[str, Any]:
    return {
        "kind": kind, "id": pin_id, "commit": patch["commit"],
        "stable_patch_id": patch["stable_patch_id"], "subject": patch["subject"],
    }


# ── manifest fixture ─────────────────────────────────────────────────────────


def _manifest_document(patch: dict[str, Any], *, accepted: list[str] | None = None) -> dict[str, Any]:
    component_patch: dict[str, Any] = {
        "commit": patch["commit"],
        "stable_patch_id": patch["stable_patch_id"],
        "subject": patch["subject"],
    }
    if accepted:
        component_patch["accepted_output_patch_ids"] = list(accepted)
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
        "fork": {"repository": "owner/fork"},
        "components": [{"id": "test-component", "source_ref": "fork/test", "patches": [component_patch]}],
    }


def _multi_patch_manifest_document(patch: dict[str, Any], decoy: dict[str, Any]) -> dict[str, Any]:
    """Two patches in ONE component (U7 retirement tests need a container
    that is not emptied by removing the pin under test -- see
    ``_remove_manifest_pin_text``'s sole-element refusal)."""
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
        "fork": {"repository": "owner/fork"},
        "components": [{
            "id": "test-component", "source_ref": "fork/test",
            "patches": [
                {"commit": patch["commit"], "stable_patch_id": patch["stable_patch_id"], "subject": patch["subject"]},
                {"commit": decoy["commit"], "stable_patch_id": decoy["stable_patch_id"], "subject": decoy["subject"]},
            ],
        }],
    }


def _install_manifest(repo: Path, document: dict[str, Any]) -> Path:
    path = repo / proposals.REPO_TRACKED_SUBDIR / proposals.MANIFEST_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    blocklist = repo / proposals.REPO_TRACKED_SUBDIR / proposals.BLOCKLIST_FILENAME
    blocklist.write_text(json.dumps(proposals.empty_blocklist(), indent=2) + "\n", encoding="utf-8")
    _run(repo, "git", "add", "-A")
    _run(repo, "git", "commit", "-q", "-m", "manifest fixture")
    return path


def _assert_loader_green(monkeypatch: pytest.MonkeyPatch, manifest_path: Path) -> dict[str, Any]:
    """The edited manifest must still satisfy every loader invariant."""
    with monkeypatch.context() as patched:
        patched.setattr(release, "MANIFEST_PATH", manifest_path)
        return release.load_manifest()


# ── release-module binding ───────────────────────────────────────────────────


@pytest.fixture
def bound_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the release module's git/worktree/blocklist/store at temp paths.

    ``release.run``'s ``cwd`` default is bound at ``def`` time to the module's
    WORKTREE constant, so reassigning WORKTREE alone does not redirect
    ``git()``; wrap ``run`` as well (documented in
    ``test_fork_integration_release.py``).
    """
    def bind(repo: Path) -> dict[str, Path]:
        original_run = release.run

        def run_in_repo(*args: Any, **kwargs: Any) -> Any:
            kwargs.setdefault("cwd", repo)
            return original_run(*args, **kwargs)

        store_root = tmp_path / "proposal-store"
        blocklist = tmp_path / "blocklist.json"
        monkeypatch.setattr(release, "run", run_in_repo)
        monkeypatch.setattr(release, "WORKTREE", repo)
        monkeypatch.setattr(release, "LOG_PATH", tmp_path / "logs" / "release.log")
        monkeypatch.setattr(release, "BLOCKLIST_PATH", blocklist)
        monkeypatch.setenv(proposals.PROPOSALS_DIR_ENV, str(store_root))
        release.reset_run_reconciliation_state()
        return {"store_root": store_root, "blocklist": blocklist}

    yield bind
    release.reset_run_reconciliation_state()


def _bind_manifest(monkeypatch: pytest.MonkeyPatch, patch: dict[str, Any]) -> None:
    monkeypatch.setattr(release, "MANIFEST", _manifest_document(patch))


# ── 1. generation + dedupe + park-and-continue ───────────────────────────────


def test_churned_pin_parks_once_and_the_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound_release
) -> None:
    """One pending proposal, no duplicate on the second run, and resolution
    continues exactly as if no candidate existed (KTD13/R19)."""
    churn = Churn(tmp_path)
    patch = churn.pin({"pinfile.txt": "fork version\n"})
    churn.upstream({"upstream-rewrite.txt": "upstream version\n"}, PIN_SUBJECT)
    tip = churn.tip()
    paths = bound_release(churn.repo)
    _bind_manifest(monkeypatch, patch)

    to_apply, absorbed = release.patch_resolution(tip, [patch], upstream_tip=tip)

    # Park-and-continue: the pin still goes to the apply path.
    assert to_apply == [patch]
    assert absorbed == []
    assert len(release.PARKED_PINS) == 1
    note = release.PARKED_PINS[0]
    assert note["pin_id"] == "test-component"
    assert note["state"] == proposals.STATE_PENDING_APPROVAL
    assert note["evidence"] == proposals.EVIDENCE_COMPLETE
    assert note["churn_livelock"] is False

    store = proposals.ProposalStore(paths["store_root"])
    artifact = store.load(note["proposal_id"])
    assert artifact is not None
    assert artifact["pin"]["subject"] == PIN_SUBJECT
    assert artifact["regen_count"] == 0
    assert artifact["candidates"][0]["signature_state"] == "N"
    assert artifact["candidates"][0]["author"].startswith("Fork Integration Proposals Test")
    assert artifact["interdiff_stat"]
    assert artifact["recommended_edit"]["operation"] == "append_accepted_output_patch_id"
    # Full candidate diff lives in the sibling .diff, referenced by name+hash.
    diff_path = store.diff_path(artifact["id"])
    assert diff_path.is_file()
    assert artifact["candidate_diff"]["file"] == diff_path.name
    assert artifact["candidate_diff"]["sha256"] == proposals.hashlib.sha256(
        diff_path.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
    # Keep-ref for the outgoing pinned commit (evidence retention).
    keep_ref = f"{proposals.KEEP_REF_PREFIX}/test-component/{patch['stable_patch_id']}"
    assert _run(churn.repo, "git", "rev-parse", keep_ref) == patch["commit"]

    # Second run, same candidate set: no duplicate, no regeneration.
    first_written = store.artifact_path(artifact["id"]).read_text(encoding="utf-8")
    release.reset_run_reconciliation_state()
    release.patch_resolution(tip, [patch], upstream_tip=tip)
    assert len(list(store.root.glob("*.json"))) == 1
    assert store.artifact_path(artifact["id"]).read_text(encoding="utf-8") == first_written
    assert store.load(artifact["id"])["regen_count"] == 0


def test_parked_pin_application_carries_the_provenance_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound_release
) -> None:
    """R19: the parked pin's last verified form still applies, and the output
    record is tagged ``pin-parked-pending-proposal:<id>``."""
    churn = Churn(tmp_path)
    patch = churn.pin({"pinfile.txt": "fork version\n"})
    churn.upstream({"upstream-rewrite.txt": "upstream version\n"}, PIN_SUBJECT)
    tip = churn.tip()
    bound_release(churn.repo)
    _bind_manifest(monkeypatch, patch)

    to_apply, _absorbed = release.patch_resolution(tip, [patch], upstream_tip=tip)
    proposal_id = release.PARKED_PINS[0]["proposal_id"]

    records = release.apply_required_patches(
        to_apply, published_input_head=tip, upstream_head=tip, kind="component",
    )

    assert len(records) == 1
    assert records[0]["status"] == "applied"
    assert records[0]["provenance"] == f"pin-parked-pending-proposal:{proposal_id}"
    assert release.parked_pin_summary() == [
        {"pin_id": "test-component", "proposal_id": proposal_id, "churn_livelock": False}
    ]


# ── 2/3. eligibility: ancestry and reverts ───────────────────────────────────


def test_same_subject_non_ancestor_produces_no_proposal(tmp_path: Path) -> None:
    """R1: a same-subject commit that is not an ancestor of the run's upstream
    tip is not a candidate -- it is some other branch's work."""
    churn = Churn(tmp_path)
    patch = churn.pin({"pinfile.txt": "fork version\n"})
    tip = churn.tip()
    _run(churn.repo, "git", "checkout", "-q", "-b", "sidebranch", churn.base)
    churn.upstream({"sideline.txt": "elsewhere\n"}, PIN_SUBJECT)
    side_head = churn.tip()
    _run(churn.repo, "git", "checkout", "-q", churn.trunk)

    detected = proposals.detect_candidates(
        churn.git, _pin_record(churn, patch), search_ref=side_head, upstream_tip=tip,
    )

    assert detected["candidates"] == []


def test_revert_subjects_never_qualify(tmp_path: Path) -> None:
    """R1: an upstream ``Revert "..."`` is never a re-land candidate."""
    churn = Churn(tmp_path)
    patch = churn.pin({"pinfile.txt": "fork version\n"})
    # Upstream's message CONTAINS the pin's subject (so --grep matches) but
    # its subject line is a revert of it.
    churn.upstream({"reverted.txt": "x\n"}, f'Revert "{PIN_SUBJECT}"')
    tip = churn.tip()

    assert proposals.detect_candidates(
        churn.git, _pin_record(churn, patch), search_ref=tip, upstream_tip=tip, detect_retitled=False,
    )["candidates"] == []

    # And a pin whose own subject is a revert cannot be matched either.
    revert_pin = dict(_pin_record(churn, patch), subject=f'Revert "{PIN_SUBJECT}"')
    assert proposals.detect_candidates(
        churn.git, revert_pin, search_ref=tip, upstream_tip=tip, detect_retitled=False,
    )["candidates"] == []


# ── 4. retitled re-land (low confidence) ─────────────────────────────────────


def test_retitled_reland_is_caught_with_the_low_confidence_marker(tmp_path: Path) -> None:
    """No exact-subject candidate, but upstream touched (nearly) the same
    files: a weak, explicitly-marked match, never a strong claim."""
    churn = Churn(tmp_path)
    shared = {"a.txt": "fork a\n", "b.txt": "fork b\n", "c.txt": "fork c\n"}
    patch = churn.pin(shared)
    churn.upstream(
        {"a.txt": "upstream a\n", "b.txt": "upstream b\n", "c.txt": "upstream c\n"},
        "fix(desktop): rewrite journal persistence bounds",
    )
    tip = churn.tip()

    detected = proposals.detect_candidates(
        churn.git, _pin_record(churn, patch), search_ref=tip, upstream_tip=tip,
    )

    assert len(detected["candidates"]) == 1
    assert detected["low_confidence"] is True
    assert detected["candidates"][0]["low_confidence"] is True
    assert detected["candidates"][0]["shared_path_ratio"] == 1.0
    assert detected["candidates"][0]["subject"] == "fix(desktop): rewrite journal persistence bounds"


def test_unrelated_upstream_work_is_not_a_retitled_reland(tmp_path: Path) -> None:
    churn = Churn(tmp_path)
    patch = churn.pin({"a.txt": "fork a\n", "b.txt": "fork b\n", "c.txt": "fork c\n"})
    churn.upstream({"unrelated.txt": "nothing to do with the pin\n"}, "chore: unrelated")
    tip = churn.tip()

    assert proposals.detect_candidates(
        churn.git, _pin_record(churn, patch), search_ref=tip, upstream_tip=tip,
    )["candidates"] == []


# ── manifest edit mechanics ──────────────────────────────────────────────────


def test_manifest_edit_preserves_hand_formatting_of_the_real_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The approval edit must be reviewable: exactly the touched key changes,
    line endings and the rest of the hand formatting survive."""
    with Path(release.MANIFEST_PATH).open("r", encoding="utf-8", newline="") as _fh:
        source = _fh.read()
    document = json.loads(source)
    pin_patch = document["components"][0]["patches"][0]
    new_id = "f" * 40
    edit = {
        "operation": "append_accepted_output_patch_id",
        "pin_kind": "component", "pin_id": document["components"][0]["id"],
        "patch_commit": pin_patch["commit"], "patch_stable_patch_id": pin_patch["stable_patch_id"],
        "candidate_commit": "e" * 40, "accepted_output_patch_id": new_id,
    }

    updated = proposals.apply_manifest_edit_text(source, edit)

    assert ("\r\n" in updated) == ("\r\n" in source)
    before = source.split("\r\n" if "\r\n" in source else "\n")
    after = updated.split("\r\n" if "\r\n" in updated else "\n")
    changed = [line for line in after if line not in before]
    assert len(changed) <= 3, changed
    reparsed = json.loads(updated)
    assert new_id in reparsed["components"][0]["patches"][0]["accepted_output_patch_ids"]
    # Nothing else moved.
    reparsed["components"][0]["patches"][0]["accepted_output_patch_ids"] = pin_patch.get(
        "accepted_output_patch_ids", []
    )
    assert reparsed == document

    path = tmp_path / "manifest.json"
    path.write_text(updated, encoding="utf-8", newline="")
    _assert_loader_green(monkeypatch, path)


def test_manifest_edit_inserts_the_key_when_absent_and_is_idempotent() -> None:
    document = _manifest_document({"commit": "1" * 40, "stable_patch_id": "2" * 40, "subject": PIN_SUBJECT})
    text = json.dumps(document, indent=2) + "\n"
    edit = {
        "operation": "append_accepted_output_patch_id",
        "pin_kind": "component", "pin_id": "test-component",
        "patch_commit": "1" * 40, "patch_stable_patch_id": "2" * 40,
        "candidate_commit": "3" * 40, "accepted_output_patch_id": "4" * 40,
    }

    once = proposals.apply_manifest_edit_text(text, edit)
    twice = proposals.apply_manifest_edit_text(once, edit)

    assert json.loads(once)["components"][0]["patches"][0]["accepted_output_patch_ids"] == ["4" * 40]
    assert twice == once
    second = proposals.apply_manifest_edit_text(once, {**edit, "accepted_output_patch_id": "5" * 40})
    assert json.loads(second)["components"][0]["patches"][0]["accepted_output_patch_ids"] == ["4" * 40, "5" * 40]


def test_manifest_edit_refuses_an_ambiguous_anchor() -> None:
    text = json.dumps({"a": {"commit": "1" * 40}, "b": {"commit": "1" * 40}}, indent=2)
    with pytest.raises(proposals.ProposalError, match="missing or ambiguous"):
        proposals.apply_manifest_edit_text(text, {
            "operation": "append_accepted_output_patch_id",
            "pin_kind": "component", "pin_id": "x",
            "patch_commit": "1" * 40, "patch_stable_patch_id": "2" * 40,
            "candidate_commit": "3" * 40, "accepted_output_patch_id": "4" * 40,
        })


# ── 5. approval happy path ───────────────────────────────────────────────────


def _generate(churn: Churn, tmp_path: Path, patch: dict[str, Any], tip: str) -> tuple[proposals.ProposalStore, dict[str, Any]]:
    store = proposals.ProposalStore(tmp_path / "store")
    pin = _pin_record(churn, patch)
    detected = proposals.detect_candidates(churn.git, pin, search_ref=tip, upstream_tip=tip)
    artifact = proposals.generate_or_refresh(
        store, churn.git, pin=pin, candidates=detected["candidates"],
        low_confidence=detected["low_confidence"], upstream_ref="origin/main", upstream_tip=tip,
    )
    return store, artifact


def test_approval_applies_the_edit_commits_and_blocklists_superseded_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    churn = Churn(tmp_path)
    patch = churn.pin({"pinfile.txt": "fork version\n"})
    churn.upstream({"rewrite-a.txt": "upstream a\n"}, PIN_SUBJECT)
    churn.upstream({"rewrite-b.txt": "upstream b\n"}, PIN_SUBJECT)
    tip = churn.tip()
    manifest_path = _install_manifest(churn.repo, _manifest_document(patch))
    tip = churn.tip()
    store, artifact = _generate(churn, tmp_path, patch, tip)
    assert len(artifact["candidates"]) == 2
    approved_candidate = artifact["recommended_candidate"]
    other_patch_id = next(
        item["stable_patch_id"] for item in artifact["candidates"] if item["sha"] != approved_candidate
    )

    outcome = proposals.approve(
        artifact["id"], artifact_hash_arg=artifact["artifact_sha256"], store=store,
        repo_dir=churn.repo, upstream_tip=tip, allow_noninteractive=True, approver="tester",
    )

    assert outcome["ok"] is True
    assert outcome["state"] == proposals.STATE_APPLIED
    assert outcome["superseded_patch_ids"] == [other_patch_id]

    # Manifest mutated, and still loader-green.
    loaded = _assert_loader_green(monkeypatch, manifest_path)
    accepted = loaded["components"][0]["patches"][0]["accepted_output_patch_ids"]
    assert accepted == [outcome["accepted_output_patch_id"]]

    # Blocklist appended with the superseded (non-approved) candidate.
    blocklist = proposals.load_blocklist(churn.repo / proposals.REPO_TRACKED_SUBDIR / proposals.BLOCKLIST_FILENAME)
    assert [entry["patch_id"] for entry in blocklist["entries"]] == [other_patch_id]
    assert blocklist["entries"][0]["actor"] == "tester"

    # One attributable commit carrying approver, candidate, proposal, footer.
    message = _run(churn.repo, "git", "log", "-1", "--format=%B")
    assert artifact["id"] in message
    assert "Approved-By: tester at " in message
    assert f"Candidate: {approved_candidate}" in message
    assert "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>" in message
    assert "Claude-Session: https://claude.ai/code/session_01EfgNpcfi1s5tyBaCpjivG6" in message
    committed = _run(churn.repo, "git", "show", "--stat", "--format=", "HEAD")
    assert proposals.MANIFEST_FILENAME in committed
    assert proposals.BLOCKLIST_FILENAME in committed

    # Keep-ref for the outgoing pin, artifact applied, operator told how to sync.
    assert _run(churn.repo, "git", "rev-parse", outcome["keep_ref"]) == patch["commit"]
    assert store.load(artifact["id"])["state"] == proposals.STATE_APPLIED
    assert any("restamp-file" in command for command in outcome["restamp_commands"])
    assert any(proposals.BLOCKLIST_FILENAME in command for command in outcome["restamp_commands"])


# ── 6. tampering ─────────────────────────────────────────────────────────────


def test_tampered_fragment_is_stale_invalidated_without_touching_the_manifest(
    tmp_path: Path,
) -> None:
    churn = Churn(tmp_path)
    patch = churn.pin({"pinfile.txt": "fork version\n"})
    churn.upstream({"rewrite.txt": "upstream\n"}, PIN_SUBJECT)
    manifest_path = _install_manifest(churn.repo, _manifest_document(patch))
    tip = churn.tip()
    store, artifact = _generate(churn, tmp_path, patch, tip)
    before = manifest_path.read_text(encoding="utf-8")

    # (a) fragment edited in place, hash left alone -> recomputation catches it.
    tampered = dict(artifact)
    tampered["recommended_edit"] = dict(artifact["recommended_edit"], accepted_output_patch_id="9" * 40)
    store.artifact_path(artifact["id"]).write_text(json.dumps(tampered, indent=2), encoding="utf-8")

    outcome = proposals.approve(
        artifact["id"], artifact_hash_arg=artifact["artifact_sha256"], store=store,
        repo_dir=churn.repo, upstream_tip=tip, allow_noninteractive=True, approver="tester",
    )
    assert outcome["ok"] is False
    assert store.load(artifact["id"])["state"] == proposals.STATE_STALE_INVALIDATED
    assert manifest_path.read_text(encoding="utf-8") == before

    # (b) fragment edited AND re-stamped -> re-derivation byte-equality catches it.
    revived = store.load(artifact["id"])
    revived["state"] = proposals.STATE_PENDING_APPROVAL
    revived["recommended_edit"] = dict(artifact["recommended_edit"], accepted_output_patch_id="9" * 40)
    store.save(revived)

    outcome = proposals.approve(
        artifact["id"], artifact_hash_arg=revived["artifact_sha256"], store=store,
        repo_dir=churn.repo, upstream_tip=tip, allow_noninteractive=True, approver="tester",
    )
    assert outcome["ok"] is False
    assert "byte-equal" in outcome["reason"]
    assert store.load(artifact["id"])["state"] == proposals.STATE_STALE_INVALIDATED
    assert manifest_path.read_text(encoding="utf-8") == before


# ── 7. upstream moved again + churn livelock ─────────────────────────────────


def test_upstream_rewrite_before_approval_is_stale_invalidated(tmp_path: Path) -> None:
    churn = Churn(tmp_path)
    patch = churn.pin({"pinfile.txt": "fork version\n"})
    manifest_path = _install_manifest(churn.repo, _manifest_document(patch))
    fixture_head = churn.tip()
    churn.upstream({"rewrite-a.txt": "upstream a\n"}, PIN_SUBJECT)
    tip = churn.tip()
    store, artifact = _generate(churn, tmp_path, patch, tip)
    before = manifest_path.read_text(encoding="utf-8")

    # Upstream rewrites: the recommended candidate is no longer on the line.
    churn.rewind_upstream(fixture_head)
    churn.upstream({"rewrite-b.txt": "upstream b\n"}, PIN_SUBJECT)
    new_tip = churn.tip()

    outcome = proposals.approve(
        artifact["id"], artifact_hash_arg=artifact["artifact_sha256"], store=store,
        repo_dir=churn.repo, upstream_tip=new_tip, allow_noninteractive=True, approver="tester",
    )

    assert outcome["ok"] is False
    assert "no longer an ancestor" in outcome["reason"]
    assert store.load(artifact["id"])["state"] == proposals.STATE_STALE_INVALIDATED
    assert manifest_path.read_text(encoding="utf-8") == before
    assert "regenerates" in outcome["guidance"]


def test_three_regenerations_escalate_to_churn_livelock(tmp_path: Path) -> None:
    """KTD3: after three stale-and-regenerate cycles the delivery escalates."""
    churn = Churn(tmp_path)
    patch = churn.pin({"pinfile.txt": "fork version\n"})
    store = proposals.ProposalStore(tmp_path / "store")
    pin = _pin_record(churn, patch)

    artifact = None
    for index in range(4):
        churn.rewind_upstream(churn.base)
        churn.upstream({f"rewrite-{index}.txt": f"upstream {index}\n"}, PIN_SUBJECT)
        tip = churn.tip()
        detected = proposals.detect_candidates(churn.git, pin, search_ref=tip, upstream_tip=tip)
        assert len(detected["candidates"]) == 1
        artifact = proposals.generate_or_refresh(
            store, churn.git, pin=pin, candidates=detected["candidates"],
            upstream_ref="origin/main", upstream_tip=tip,
        )
        assert artifact["regen_count"] == index

    assert artifact is not None
    assert artifact["regen_count"] == 3
    assert artifact["churn_livelock"] is True
    # Still one artifact: regeneration never forks a second proposal.
    assert len(list(store.root.glob("*.json"))) == 1
    # Every superseded rewrite is remembered for the eventual approval.
    assert len(artifact["superseded_patch_ids"]) == 3


# ── 8. approval channel ──────────────────────────────────────────────────────


def test_non_interactive_approve_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R2/R12: approval must not be drivable by an agent or a script."""
    churn = Churn(tmp_path)
    patch = churn.pin({"pinfile.txt": "fork version\n"})
    churn.upstream({"rewrite.txt": "upstream\n"}, PIN_SUBJECT)
    _install_manifest(churn.repo, _manifest_document(patch))
    tip = churn.tip()
    store, artifact = _generate(churn, tmp_path, patch, tip)

    exit_code = proposals.main([
        "--proposals-dir", str(store.root), "--repo", str(churn.repo),
        "approve", artifact["id"], "--artifact-hash", artifact["artifact_sha256"],
        "--upstream-tip", tip,
    ])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "interactive channel" in payload["error"]
    assert store.load(artifact["id"])["state"] == proposals.STATE_PENDING_APPROVAL
    assert not sys.stdin.isatty()  # the refusal above was the real code path


# ── 9. rejection is durable ──────────────────────────────────────────────────


def test_rejected_candidate_never_absorbs_or_is_reproposed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound_release
) -> None:
    """R3: a rejected patch-id is neither equivalent nor proposable, even if
    a manifest still lists it as an accepted output identity."""
    churn = Churn(tmp_path)
    patch = churn.pin({"pinfile.txt": "fork version\n"})
    churn.upstream({"rewrite.txt": "upstream\n"}, PIN_SUBJECT)
    _install_manifest(churn.repo, _manifest_document(patch))
    tip = churn.tip()
    store, artifact = _generate(churn, tmp_path, patch, tip)
    candidate = artifact["candidates"][0]

    outcome = proposals.reject(
        artifact["id"], reason="upstream's rewrite drops the bound", store=store,
        repo_dir=churn.repo, actor="tester",
    )
    assert outcome["blocklisted_patch_ids"] == [candidate["stable_patch_id"]]
    assert store.load(artifact["id"])["state"] == proposals.STATE_REJECTED

    blocklist_path = churn.repo / proposals.REPO_TRACKED_SUBDIR / proposals.BLOCKLIST_FILENAME
    blocked = proposals.blocklisted_patch_ids(blocklist_path)
    assert blocked == {candidate["stable_patch_id"]}

    # Never proposed again.
    assert proposals.detect_candidates(
        churn.git, artifact["pin"], search_ref=tip, upstream_tip=tip, blocked=blocked,
    )["candidates"] == []

    # Never absorbed again, even when the manifest accepts that identity.
    paths = bound_release(churn.repo)
    Path(paths["blocklist"]).write_text(blocklist_path.read_text(encoding="utf-8"), encoding="utf-8")
    release.reset_run_reconciliation_state()
    accepting = dict(patch, accepted_output_patch_ids=[candidate["stable_patch_id"]])
    _bind_manifest(monkeypatch, accepting)

    to_apply, absorbed = release.patch_resolution(tip, [accepting], upstream_tip=tip)

    assert absorbed == []
    assert to_apply == [accepting]
    # And no fresh proposal was generated for the blocklisted candidate.
    assert release.PARKED_PINS == []


def test_corrupt_blocklist_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "blocklist.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(proposals.ProposalError, match="unreadable"):
        proposals.blocklisted_patch_ids(path)
    assert proposals.blocklisted_patch_ids(tmp_path / "absent.json") == set()


# ── evidence-unavailable fail-closed ─────────────────────────────────────────


def test_unresolvable_interdiff_side_is_recorded_as_evidence_unavailable(tmp_path: Path) -> None:
    """U6 approach 3: never fabricate evidence -- say it is unavailable and
    let the run park-and-continue without it."""
    churn = Churn(tmp_path)
    patch = churn.pin({"pinfile.txt": "fork version\n"})
    churn.upstream({"rewrite.txt": "upstream\n"}, PIN_SUBJECT)
    tip = churn.tip()
    store = proposals.ProposalStore(tmp_path / "store")
    pin = _pin_record(churn, patch)
    detected = proposals.detect_candidates(churn.git, pin, search_ref=tip, upstream_tip=tip)
    # The pin side of the interdiff cannot be resolved: the pinned commit is
    # not in this repository at all.
    unresolvable = dict(pin, commit="0" * 40)

    artifact = proposals.generate_or_refresh(
        store, churn.git, pin=unresolvable, candidates=detected["candidates"],
        upstream_ref="origin/main", upstream_tip=tip,
    )

    assert artifact["evidence"] == proposals.EVIDENCE_UNAVAILABLE
    assert artifact["state"] == proposals.STATE_GENERATED
    assert artifact["interdiff_stat"] is None
    assert artifact["candidate_diff"] is not None  # the candidate side resolved


# ── 10. #63047 replay: one approval, zero manual edits, no skipped run ───────


def test_63047_three_sha_churn_replay_absorbs_with_one_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound_release
) -> None:
    """The acceptance proof (U6 verification).

    Replays the real shape of the #63047 churn: a pinned fork commit, an
    upstream re-land (candidate A, patch-id X), then a second upstream
    rewrite (candidate B, patch-id Y). Both nightly runs must COMPLETE
    (park-and-continue), one approval must absorb the change, and the next
    run must resolve it as absorbed with no human touching the manifest.
    """
    churn = Churn(tmp_path)
    patch = churn.pin({"apps/desktop/src/lib/inflight-turn-journal.ts": "fork bound\n"})
    manifest_path = _install_manifest(churn.repo, _manifest_document(patch))
    manifest_fixture_head = churn.tip()
    paths = bound_release(churn.repo)
    _bind_manifest(monkeypatch, patch)
    store = proposals.ProposalStore(paths["store_root"])

    # ── run 1: upstream re-lands the change as candidate A (patch-id X) ──
    churn.upstream({"apps/desktop/src/lib/inflight-turn-journal.ts": "upstream bound A\n"}, PIN_SUBJECT)
    tip_a = churn.tip()
    run_one = release.patch_resolution(tip_a, [patch], upstream_tip=tip_a)
    assert run_one == ([patch], [])  # the run continues; nothing skipped
    proposal_id = release.PARKED_PINS[0]["proposal_id"]
    patch_id_x = store.load(proposal_id)["candidates"][0]["stable_patch_id"]

    # ── run 2: upstream rewrites it again as candidate B (patch-id Y) ──
    churn.rewind_upstream(manifest_fixture_head)
    churn.upstream({"apps/desktop/src/lib/inflight-turn-journal.ts": "upstream bound B\n"}, PIN_SUBJECT)
    tip_b = churn.tip()
    release.reset_run_reconciliation_state()
    run_two = release.patch_resolution(tip_b, [patch], upstream_tip=tip_b)
    assert run_two == ([patch], [])
    artifact = store.load(proposal_id)
    assert artifact["regen_count"] == 1
    assert artifact["superseded_patch_ids"] == [patch_id_x]
    patch_id_y = artifact["candidates"][0]["stable_patch_id"]
    assert patch_id_y != patch_id_x
    assert len(list(store.root.glob("*.json"))) == 1  # one lineage, one proposal

    # ── the single human action ──
    outcome = proposals.approve(
        artifact["id"], artifact_hash_arg=artifact["artifact_sha256"], store=store,
        repo_dir=churn.repo, upstream_tip=tip_b, allow_noninteractive=True, approver="tester",
    )
    assert outcome["ok"] is True
    assert outcome["accepted_output_patch_id"] == patch_id_y
    assert outcome["superseded_patch_ids"] == [patch_id_x]

    # ── run 3: absorbed, with no manual manifest edit anywhere ──
    absorbed_manifest = _assert_loader_green(monkeypatch, manifest_path)
    absorbed_patch = absorbed_manifest["components"][0]["patches"][0]
    assert absorbed_patch["accepted_output_patch_ids"] == [patch_id_y]
    release.reset_run_reconciliation_state()
    Path(paths["blocklist"]).write_text(
        (churn.repo / proposals.REPO_TRACKED_SUBDIR / proposals.BLOCKLIST_FILENAME).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr(release, "MANIFEST", absorbed_manifest)
    tip_final = churn.tip()

    to_apply, absorbed = release.patch_resolution(tip_final, [absorbed_patch], upstream_tip=tip_final)

    assert to_apply == []
    assert len(absorbed) == 1
    assert absorbed[0]["output_patch_id"] == patch_id_y
    assert release.PARKED_PINS == []


# ── 11. context drift resolved by lineage approval ───────────────────────────


def test_context_drift_reland_is_absorbed_through_lineage_approval(tmp_path: Path) -> None:
    """A byte-identical change re-applied against drifted context lines gets
    a different patch-id under a new sha. Sha-anchored approval must refuse
    it; ``--lineage`` (subject + file set) is the sanctioned escape (KTD3)."""
    churn = Churn(tmp_path)
    original = "alpha\nbravo\ncharlie\ndelta\n"
    _commit(churn.repo, {"drift.txt": original}, "seed drift file")
    seeded = churn.tip()

    _run(churn.repo, "git", "checkout", "-q", "-b", "pin-source", seeded)
    pin_commit = _commit(churn.repo, {"drift.txt": "alpha\nbravo\ncharlie\nXRAY\ndelta\n"}, PIN_SUBJECT)
    _run(churn.repo, "git", "checkout", "-q", churn.trunk)
    patch = {"commit": pin_commit, "subject": PIN_SUBJECT, "stable_patch_id": churn.git.patch_id(pin_commit)}
    _install_manifest(churn.repo, _manifest_document(patch))
    fixture_head = churn.tip()

    # Upstream re-lands the same insertion (candidate A).
    churn.upstream({"drift.txt": "alpha\nbravo\ncharlie\nXRAY\ndelta\n"}, PIN_SUBJECT)
    tip_a = churn.tip()
    store, artifact = _generate(churn, tmp_path, patch, tip_a)
    stored_candidate = artifact["recommended_candidate"]

    # Upstream rebases: a context line changed first, so the SAME insertion
    # now carries different context -> new sha, new patch-id.
    churn.rewind_upstream(fixture_head)
    churn.upstream({"drift.txt": "alpha\nBRAVO2\ncharlie\ndelta\n"}, "chore: rename bravo")
    churn.upstream({"drift.txt": "alpha\nBRAVO2\ncharlie\nXRAY\ndelta\n"}, PIN_SUBJECT)
    tip_b = churn.tip()
    drifted = churn.git.patch_id(tip_b)
    assert drifted != churn.git.patch_id(stored_candidate)

    # Sha-anchored approval refuses (and the artifact goes stale).
    refused = proposals.approve(
        artifact["id"], artifact_hash_arg=artifact["artifact_sha256"], store=store,
        repo_dir=churn.repo, upstream_tip=tip_b, allow_noninteractive=True, approver="tester",
    )
    assert refused["ok"] is False
    assert "--lineage" in refused["reason"]

    # Lineage approval re-resolves to the drifted re-land and absorbs it.
    revived = store.load(artifact["id"])
    revived["state"] = proposals.STATE_PENDING_APPROVAL
    store.save(revived)

    outcome = proposals.approve(
        artifact["id"], artifact_hash_arg=revived["artifact_sha256"], store=store,
        repo_dir=churn.repo, upstream_tip=tip_b, lineage=True, allow_noninteractive=True, approver="tester",
    )

    assert outcome["ok"] is True
    assert outcome["candidate"] == tip_b
    assert outcome["accepted_output_patch_id"] == drifted
    message = _run(churn.repo, "git", "log", "-1", "--format=%B")
    assert "Approval-Mode: lineage" in message


# ── 12. U7 retirement bridge: same state machine, removal instead of growth ──


def test_derive_retirement_edit_is_a_pure_function_of_the_pin() -> None:
    pin = {"kind": "component", "id": "comp1", "commit": "1" * 40, "stable_patch_id": "2" * 40, "subject": "x"}
    assert proposals.derive_retirement_edit(pin) == {
        "operation": "remove_manifest_pin", "pin_kind": "component", "pin_id": "comp1",
        "patch_commit": "1" * 40, "patch_stable_patch_id": "2" * 40,
    }


def _three_patch_text() -> str:
    return json.dumps(
        {"components": [{"id": "comp1", "source_ref": "x", "patches": [
            {"commit": "1" * 40, "stable_patch_id": "2" * 40, "subject": "first"},
            {"commit": "3" * 40, "stable_patch_id": "4" * 40, "subject": "second"},
            {"commit": "5" * 40, "stable_patch_id": "6" * 40, "subject": "third"},
        ]}]},
        indent=2,
    )


def _removal_edit(commit: str, stable_patch_id: str) -> dict[str, Any]:
    return {
        "operation": "remove_manifest_pin", "pin_kind": "component", "pin_id": "comp1",
        "patch_commit": commit, "patch_stable_patch_id": stable_patch_id,
    }


def test_remove_manifest_pin_text_middle_element() -> None:
    updated = proposals.apply_manifest_edit_text(_three_patch_text(), _removal_edit("3" * 40, "4" * 40))
    patches = json.loads(updated)["components"][0]["patches"]
    assert [p["commit"] for p in patches] == ["1" * 40, "5" * 40]


def test_remove_manifest_pin_text_first_element() -> None:
    updated = proposals.apply_manifest_edit_text(_three_patch_text(), _removal_edit("1" * 40, "2" * 40))
    patches = json.loads(updated)["components"][0]["patches"]
    assert [p["commit"] for p in patches] == ["3" * 40, "5" * 40]


def test_remove_manifest_pin_text_last_element_strips_preceding_comma() -> None:
    updated = proposals.apply_manifest_edit_text(_three_patch_text(), _removal_edit("5" * 40, "6" * 40))
    parsed = json.loads(updated)  # would raise json.JSONDecodeError on a stray trailing comma
    patches = parsed["components"][0]["patches"]
    assert [p["commit"] for p in patches] == ["1" * 40, "3" * 40]


def test_remove_manifest_pin_text_sole_patch_refuses_to_empty_the_container() -> None:
    text = json.dumps(
        {"components": [{"id": "c", "source_ref": "x", "patches": [
            {"commit": "7" * 40, "stable_patch_id": "8" * 40, "subject": "only"},
        ]}]},
        indent=2,
    )
    with pytest.raises(proposals.ProposalError, match="sole patch"):
        proposals.apply_manifest_edit_text(text, _removal_edit("7" * 40, "8" * 40))


def test_apply_manifest_edit_text_rejects_unknown_operation() -> None:
    with pytest.raises(proposals.ProposalError, match="unsupported manifest edit operation"):
        proposals.apply_manifest_edit_text("{}", {"operation": "bogus"})


def test_generate_or_refresh_retirement_requires_absorbing_commit_evidence(tmp_path: Path) -> None:
    churn = Churn(tmp_path)
    patch = churn.pin({"pinfile.txt": "x\n"})
    store = proposals.ProposalStore(tmp_path / "store")
    with pytest.raises(proposals.ProposalError, match="absorbing-commit evidence"):
        proposals.generate_or_refresh_retirement(store, churn.git, pin=_pin_record(churn, patch), evidence={})


def test_generate_or_refresh_retirement_dedupes_the_open_proposal(tmp_path: Path) -> None:
    churn = Churn(tmp_path)
    patch = churn.pin({"pinfile.txt": "x\n"})
    absorbing = churn.upstream({"pinfile.txt": "x\n"}, PIN_SUBJECT)
    store = proposals.ProposalStore(tmp_path / "store")
    pin = _pin_record(churn, patch)
    evidence = {"candidate_commit": absorbing}

    first = proposals.generate_or_refresh_retirement(store, churn.git, pin=pin, evidence=evidence)
    assert first["refreshed"] is True
    assert first["state"] == proposals.STATE_PENDING_APPROVAL
    assert first["recommended_edit"]["operation"] == "remove_manifest_pin"

    second = proposals.generate_or_refresh_retirement(store, churn.git, pin=pin, evidence=evidence)
    assert second["refreshed"] is False
    assert second["id"] == first["id"]
    assert len(list(store.root.glob("*.json"))) == 1


def test_retirement_approval_removes_the_pin_and_stays_loader_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bridge's acceptance proof: a pin the ledger found stably
    absorbed-verbatim retires through proposals.py's SAME approve() verb --
    no blocklist entry (nothing was superseded), manifest loader-green
    afterward, and the decoy sibling pin untouched."""
    churn = Churn(tmp_path)
    patch = churn.pin({"pinfile.txt": "same content\n"})
    # NOT churn.pin() again -- that recreates the "pin-source" branch, which
    # already exists after the first call. A plain commit on the current
    # (trunk) branch is all a decoy sibling pin needs.
    decoy_commit = _commit(churn.repo, {"decoyfile.txt": "decoy\n"}, "fix: unrelated decoy")
    decoy = {
        "commit": decoy_commit, "subject": "fix: unrelated decoy",
        "stable_patch_id": churn.git.patch_id(decoy_commit),
    }
    manifest_path = _install_manifest(churn.repo, _multi_patch_manifest_document(patch, decoy))
    # Upstream absorbs the pin VERBATIM: same content + same subject -> same patch-id.
    absorbing = churn.upstream({"pinfile.txt": "same content\n"}, PIN_SUBJECT)
    tip = churn.tip()

    store = proposals.ProposalStore(tmp_path / "store")
    pin = _pin_record(churn, patch)
    evidence = {"candidate_commit": absorbing, "candidate_patch_id": patch["stable_patch_id"], "upstream_ref": "origin/main"}
    artifact = proposals.generate_or_refresh_retirement(
        store, churn.git, pin=pin, evidence=evidence, upstream_ref="origin/main", upstream_tip=tip,
    )

    outcome = proposals.approve(
        artifact["id"], artifact_hash_arg=artifact["artifact_sha256"], store=store,
        repo_dir=churn.repo, upstream_tip=tip, allow_noninteractive=True, approver="tester",
    )

    assert outcome["ok"] is True
    assert outcome["operation"] == "remove_manifest_pin"
    assert outcome["superseded_patch_ids"] == []
    assert store.load(artifact["id"])["state"] == proposals.STATE_APPLIED

    loaded = _assert_loader_green(monkeypatch, manifest_path)
    remaining = loaded["components"][0]["patches"]
    assert [p["commit"] for p in remaining] == [decoy["commit"]]  # only the decoy survives

    blocklist = proposals.load_blocklist(churn.repo / proposals.REPO_TRACKED_SUBDIR / proposals.BLOCKLIST_FILENAME)
    assert blocklist["entries"] == []  # a retirement supersedes nothing

    message = _run(churn.repo, "git", "log", "-1", "--format=%B")
    assert "retire absorbed pin test-component" in message
    assert "Approval-Mode: retirement" in message
    assert "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>" in message
    committed = _run(churn.repo, "git", "show", "--stat", "--format=", "HEAD")
    assert proposals.MANIFEST_FILENAME in committed
    assert proposals.BLOCKLIST_FILENAME not in committed  # nothing superseded, blocklist untouched


def test_retirement_approval_stale_invalidates_when_no_longer_absorbed_verbatim(tmp_path: Path) -> None:
    """R2 applied to retirement: upstream rewrites again before approval, the
    pin is no longer verifiably absorbed-verbatim -> stale-invalidated, no
    manifest edit."""
    churn = Churn(tmp_path)
    patch = churn.pin({"pinfile.txt": "same content\n"})
    # NOT churn.pin() again -- that recreates the "pin-source" branch, which
    # already exists after the first call. A plain commit on the current
    # (trunk) branch is all a decoy sibling pin needs.
    decoy_commit = _commit(churn.repo, {"decoyfile.txt": "decoy\n"}, "fix: unrelated decoy")
    decoy = {
        "commit": decoy_commit, "subject": "fix: unrelated decoy",
        "stable_patch_id": churn.git.patch_id(decoy_commit),
    }
    manifest_path = _install_manifest(churn.repo, _multi_patch_manifest_document(patch, decoy))
    fixture_head = churn.tip()
    before = manifest_path.read_text(encoding="utf-8")
    absorbing = churn.upstream({"pinfile.txt": "same content\n"}, PIN_SUBJECT)
    tip = churn.tip()

    store = proposals.ProposalStore(tmp_path / "store")
    pin = _pin_record(churn, patch)
    evidence = {"candidate_commit": absorbing, "candidate_patch_id": patch["stable_patch_id"]}
    artifact = proposals.generate_or_refresh_retirement(
        store, churn.git, pin=pin, evidence=evidence, upstream_ref="origin/main", upstream_tip=tip,
    )

    # Upstream rewrites: the absorbing line is gone, and nothing else on the
    # new tip carries the pin's own patch-id under its exact subject.
    churn.rewind_upstream(fixture_head)
    churn.upstream({"pinfile.txt": "a different fix entirely\n"}, PIN_SUBJECT)
    new_tip = churn.tip()

    outcome = proposals.approve(
        artifact["id"], artifact_hash_arg=artifact["artifact_sha256"], store=store,
        repo_dir=churn.repo, upstream_tip=new_tip, allow_noninteractive=True, approver="tester",
    )

    assert outcome["ok"] is False
    assert store.load(artifact["id"])["state"] == proposals.STATE_STALE_INVALIDATED
    assert manifest_path.read_text(encoding="utf-8") == before
