"""Tests for scripts/fork_integration/ledger.py (U7: provenance derivation,
JSONL history, report).

``ledger.py`` is deliberately self-contained (stdlib + subprocess git +
optional gh) and imports nothing from the release script / sync.py /
proposals.py that a concurrent unit is actively editing. These tests mirror
that boundary: they monkeypatch ledger's own git-layer functions
(``_same_subject_candidates``, ``stable_patch_id``, ``is_ancestor``,
``_gh_pr_view``) rather than reaching into the release script's fixtures.

Every test passes an explicit ``history_path``/``blocklist_path`` under
``tmp_path`` -- never the real ``HERMES_HOME`` location -- per the U7
decision that tests must never write to the real location.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.fork_integration import ledger

_HEX40_A = "a" * 40
_HEX40_B = "b" * 40
_HEX40_C = "c" * 40
_HEX40_D = "d" * 40


def _write_manifest(tmp_path: Path, manifest: dict[str, Any]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _manifest(*, components: list[dict[str, Any]] | None = None, foundations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema": 3,
        "integration_branch": "fork-integration",
        "upstream": {"remote": "origin", "ref": "refs/heads/main"},
        "upstream_foundations": foundations or [],
        "fork": {"remote": "fork", "repository": "test-owner/test-fork"},
        "components": components or [],
    }


def _component(component_id: str, source_ref: str, patches: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": component_id, "source_ref": source_ref, "patches": patches}


def _patch(**overrides: Any) -> dict[str, Any]:
    base = {"commit": _HEX40_A, "stable_patch_id": "own-id", "subject": "fix: thing"}
    base.update(overrides)
    return base


# ── The five core states (R5) ───────────────────────────────────────────────


def test_derive_absorbed_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A same-subject upstream candidate whose patch-id equals the pin's OWN
    stable_patch_id is absorbed-verbatim."""
    patch = _patch()
    manifest_path = _write_manifest(tmp_path, _manifest(components=[_component("comp1", _HEX40_B, [patch])]))

    monkeypatch.setattr(ledger, "_same_subject_candidates", lambda subject, upstream_ref, *, cwd: ["cand1"])
    monkeypatch.setattr(ledger, "stable_patch_id", lambda commit, *, cwd: "own-id" if commit == "cand1" else None)

    records = ledger.derive(manifest_path, repo_dir=tmp_path, upstream_ref="origin/main", gh_enabled=False)

    assert len(records) == 1
    record = records[0]
    assert record["kind"] == "component"
    assert record["state"] == "absorbed-verbatim"
    assert record["evidence"]["candidate_commit"] == "cand1"
    assert record["evidence"]["candidate_patch_id"] == "own-id"
    assert record["evidence"]["upstream_ref"] == "origin/main"


def test_derive_absorbed_modified_via_accepted_output_patch_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A same-subject candidate whose patch-id is an EXTRA accepted id (not
    the pin's own) is absorbed-modified."""
    patch = _patch(accepted_output_patch_ids=["modified-id"])
    manifest_path = _write_manifest(tmp_path, _manifest(components=[_component("comp1", _HEX40_B, [patch])]))

    monkeypatch.setattr(ledger, "_same_subject_candidates", lambda subject, upstream_ref, *, cwd: ["cand2"])
    monkeypatch.setattr(ledger, "stable_patch_id", lambda commit, *, cwd: "modified-id" if commit == "cand2" else None)

    records = ledger.derive(manifest_path, repo_dir=tmp_path, upstream_ref="origin/main", gh_enabled=False)

    assert records[0]["state"] == "absorbed-modified"
    assert records[0]["evidence"]["candidate_commit"] == "cand2"
    assert records[0]["evidence"]["candidate_patch_id"] == "modified-id"


def test_derive_absorbed_modified_via_reviewed_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No upstream candidate at all, but the manifest declares a reviewed
    replacement (the fork carries the modified form itself) -> modified."""
    patch = _patch(reviewed_replacement={"commit": _HEX40_D, "stable_patch_id": "repl-id"})
    manifest_path = _write_manifest(tmp_path, _manifest(components=[_component("comp1", _HEX40_B, [patch])]))

    monkeypatch.setattr(ledger, "_same_subject_candidates", lambda subject, upstream_ref, *, cwd: [])
    monkeypatch.setattr(ledger, "stable_patch_id", lambda commit, *, cwd: "repl-id" if commit == _HEX40_D else None)

    records = ledger.derive(manifest_path, repo_dir=tmp_path, upstream_ref="origin/main", gh_enabled=False)

    assert records[0]["state"] == "absorbed-modified"
    assert records[0]["evidence"]["reviewed_replacement_commit"] == _HEX40_D
    assert records[0]["evidence"]["reviewed_replacement_patch_id"] == "repl-id"
    assert records[0]["evidence"]["verified"] is True


def test_derive_superseded_via_blocklist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pin's own stable_patch_id is recorded in the blocklist file ->
    superseded, even when an upstream candidate search would otherwise run."""
    patch = _patch()
    manifest_path = _write_manifest(tmp_path, _manifest(components=[_component("comp1", _HEX40_B, [patch])]))
    blocklist_path = tmp_path / "blocklist.json"
    blocklist_path.write_text(
        json.dumps({"entries": [{"patch_id": "own-id", "reason": "rejected churn candidate", "recorded_at": "2026-08-01"}]}),
        encoding="utf-8",
    )

    def _fail_if_called(*args: Any, **kwargs: Any) -> list[str]:
        raise AssertionError("candidate search must not run once the pin's own id is blocklisted")

    monkeypatch.setattr(ledger, "_same_subject_candidates", _fail_if_called)

    records = ledger.derive(
        manifest_path, repo_dir=tmp_path, upstream_ref="origin/main", blocklist_path=blocklist_path, gh_enabled=False
    )

    assert records[0]["state"] == "superseded"
    assert records[0]["evidence"]["blocklisted_patch_id"] == "own-id"
    assert records[0]["evidence"]["reason"] == "rejected churn candidate"


def test_derive_pr_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A component sourced from a named fork branch with an open upstream PR
    (per a best-effort gh lookup) resolves to pr-open when nothing absorbed
    it."""
    patch = _patch()
    manifest_path = _write_manifest(tmp_path, _manifest(components=[_component("comp1", "fork/some-branch", [patch])]))

    monkeypatch.setattr(ledger, "_same_subject_candidates", lambda subject, upstream_ref, *, cwd: [])

    seen_branches = []

    def fake_gh_pr_view(branch: str, *, cwd: Path, gh_exe: str = "gh") -> dict[str, Any]:
        seen_branches.append(branch)
        return {"pr_state": "OPEN", "pr_url": "https://github.com/example/pull/1", "pr_merged_at": None}

    monkeypatch.setattr(ledger, "_gh_pr_view", fake_gh_pr_view)

    records = ledger.derive(manifest_path, repo_dir=tmp_path, upstream_ref="origin/main", gh_enabled=True)

    assert seen_branches == ["some-branch"]  # "fork/" prefix stripped
    assert records[0]["state"] == "pr-open"
    assert records[0]["evidence"]["pr_state"] == "OPEN"
    assert records[0]["evidence"]["pr_url"] == "https://github.com/example/pull/1"


def test_derive_private_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No upstream match, no fork-branch PR source (a raw commit source_ref)
    -> private-only with empty evidence, and gh is never invoked."""
    patch = _patch()
    manifest_path = _write_manifest(tmp_path, _manifest(components=[_component("comp1", _HEX40_B, [patch])]))

    monkeypatch.setattr(ledger, "_same_subject_candidates", lambda subject, upstream_ref, *, cwd: [])

    def _fail_if_called(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("gh must not be called for a non-fork source_ref")

    monkeypatch.setattr(ledger, "_gh_pr_view", _fail_if_called)

    records = ledger.derive(manifest_path, repo_dir=tmp_path, upstream_ref="origin/main", gh_enabled=True)

    assert records[0]["state"] == "private-only"
    assert records[0]["evidence"] == {}


def test_derive_excluded_until_passthrough(tmp_path: Path) -> None:
    """A manifest-recorded exclusion marker passes through as the literal
    excluded_until_* state (forward-compatible; schema 3 does not carry this
    field today -- see ledger.py's module docstring)."""
    patch = _patch(status="excluded_until_native_compaction_ready")
    manifest_path = _write_manifest(tmp_path, _manifest(components=[_component("comp1", _HEX40_B, [patch])]))

    records = ledger.derive(manifest_path, repo_dir=tmp_path, upstream_ref="origin/main", gh_enabled=False)

    assert records[0]["state"] == "excluded_until_native_compaction_ready"
    assert records[0]["evidence"]["reason"] == "manifest-recorded exclusion marker"


def test_derive_covers_foundation_patches_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """upstream_foundations[].patches[] are carried changes too (kind='foundation')."""
    foundation = {
        "id": "found1",
        "repository": "upstream-owner/upstream-repo",
        "pull_request": 1,
        "approved_head": _HEX40_A,
        "base_ref": "main",
        "patches": [_patch()],
    }
    manifest_path = _write_manifest(tmp_path, _manifest(foundations=[foundation]))
    monkeypatch.setattr(ledger, "_same_subject_candidates", lambda subject, upstream_ref, *, cwd: [])

    records = ledger.derive(manifest_path, repo_dir=tmp_path, upstream_ref="origin/main", gh_enabled=False)

    assert len(records) == 1
    assert records[0]["kind"] == "foundation"
    assert records[0]["pin_id"] == f"foundation:found1:{_HEX40_A}"


# ── gh degradation (decision 2) ─────────────────────────────────────────────


def test_gh_pr_view_missing_binary_degrades_to_unknown_offline(tmp_path: Path) -> None:
    """A genuinely absent gh executable never raises -- degrades cleanly."""
    result = ledger._gh_pr_view("some-branch", cwd=tmp_path, gh_exe="definitely-not-a-real-gh-binary-xyz")
    assert result == {"pr": "unknown-offline"}


def test_gh_pr_view_nonzero_exit_degrades_to_unknown_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: Any, **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="not authenticated")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = ledger._gh_pr_view("some-branch", cwd=tmp_path)
    assert result == {"pr": "unknown-offline"}


def test_gh_pr_view_malformed_json_degrades_to_unknown_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: Any, **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(args, returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = ledger._gh_pr_view("some-branch", cwd=tmp_path)
    assert result == {"pr": "unknown-offline"}


def test_derive_gh_unavailable_does_not_fail_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: gh unavailable for a fork-branch component degrades the
    record's evidence but derive() completes without raising, and the state
    stays a valid vocabulary value (private-only, since no PR is confirmed)."""
    patch = _patch()
    manifest_path = _write_manifest(tmp_path, _manifest(components=[_component("comp1", "fork/some-branch", [patch])]))

    monkeypatch.setattr(ledger, "_same_subject_candidates", lambda subject, upstream_ref, *, cwd: [])

    records = ledger.derive(
        manifest_path, repo_dir=tmp_path, upstream_ref="origin/main",
        gh_enabled=True, gh_exe="definitely-not-a-real-gh-binary-xyz",
    )

    assert records[0]["state"] == "private-only"
    assert records[0]["evidence"] == {"pr": "unknown-offline"}


def test_derive_no_gh_skips_lookup_entirely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch = _patch()
    manifest_path = _write_manifest(tmp_path, _manifest(components=[_component("comp1", "fork/some-branch", [patch])]))
    monkeypatch.setattr(ledger, "_same_subject_candidates", lambda subject, upstream_ref, *, cwd: [])

    def _fail_if_called(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("gh_enabled=False must skip gh entirely")

    monkeypatch.setattr(ledger, "_gh_pr_view", _fail_if_called)

    records = ledger.derive(manifest_path, repo_dir=tmp_path, upstream_ref="origin/main", gh_enabled=False)
    assert records[0]["state"] == "private-only"


def test_gh_called_at_most_once_per_component(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patches = [_patch(commit=_HEX40_A, stable_patch_id="id-a"), _patch(commit=_HEX40_B, stable_patch_id="id-b")]
    manifest_path = _write_manifest(tmp_path, _manifest(components=[_component("comp1", "fork/some-branch", patches)]))
    monkeypatch.setattr(ledger, "_same_subject_candidates", lambda subject, upstream_ref, *, cwd: [])

    call_count = {"n": 0}

    def fake_gh_pr_view(branch: str, *, cwd: Path, gh_exe: str = "gh") -> dict[str, Any]:
        call_count["n"] += 1
        return {"pr_state": "OPEN", "pr_url": "https://example/pull/1", "pr_merged_at": None}

    monkeypatch.setattr(ledger, "_gh_pr_view", fake_gh_pr_view)

    records = ledger.derive(manifest_path, repo_dir=tmp_path, upstream_ref="origin/main", gh_enabled=True)

    assert call_count["n"] == 1
    assert len(records) == 2
    assert all(record["state"] == "pr-open" for record in records)


# ── Blocklist file absence tolerance ───────────────────────────────────────


def test_load_blocklist_tolerates_missing_file(tmp_path: Path) -> None:
    assert ledger._load_blocklist(tmp_path / "does-not-exist.json") == {}


def test_load_blocklist_tolerates_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "blocklist.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert ledger._load_blocklist(path) == {}


# ── stable_patch_id / candidate search against a real git repo ─────────────


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Fork Integration Ledger Test"], cwd=repo, check=True)


def test_stable_patch_id_matches_real_git_patch_id(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "file.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add file"], cwd=repo, check=True)

    actual = ledger.stable_patch_id("HEAD", cwd=repo)

    show = subprocess.run(["git", "show", "--format=", "--binary", "HEAD"], cwd=repo, text=True, capture_output=True, check=True)
    expected = subprocess.run(
        ["git", "patch-id", "--stable"], input=show.stdout, text=True, capture_output=True, check=True
    ).stdout.split()[0]
    assert actual == expected


def test_stable_patch_id_returns_none_for_unresolvable_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "file.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add file"], cwd=repo, check=True)

    assert ledger.stable_patch_id(_HEX40_C, cwd=repo) is None


def test_same_subject_candidates_exact_equality_only(tmp_path: Path) -> None:
    """A candidate whose subject merely CONTAINS the target subject as a
    substring must not count (R1's exact-equality rule, applied here too)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "file.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fix: thing"], cwd=repo, check=True)
    (repo / "file.txt").write_text("v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fix: thing but longer"], cwd=repo, check=True)

    candidates = ledger._same_subject_candidates("fix: thing", "HEAD", cwd=repo)

    assert len(candidates) == 1
    subject = subprocess.run(
        ["git", "show", "-s", "--format=%s", candidates[0]], cwd=repo, text=True, capture_output=True, check=True
    ).stdout.strip()
    assert subject == "fix: thing"


def test_is_ancestor_real_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "file.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=repo, check=True)
    first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()
    (repo / "file.txt").write_text("v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "second"], cwd=repo, check=True)

    assert ledger.is_ancestor(first, "HEAD", cwd=repo) is True
    assert ledger.is_ancestor(_HEX40_C, "HEAD", cwd=repo) is False


# ── Append-only JSONL history (R6, KTD4) ────────────────────────────────────


def test_append_history_writes_one_jsonl_line_per_call(tmp_path: Path) -> None:
    history_path = tmp_path / "history.jsonl"
    records1 = [{"pin_id": "p1", "kind": "component", "commit": _HEX40_A, "stable_patch_id": "s1", "subject": "x", "state": "private-only", "evidence": {}}]

    entry1 = ledger.append_history(records1, history_path, manifest_sha256="abc123", upstream_tip="deadbeef")

    assert history_path.exists()
    lines = history_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["records"] == records1
    assert parsed["derivation_inputs"] == {"manifest_sha256": "abc123", "upstream_tip": "deadbeef"}
    assert parsed["run_at"] == entry1["run_at"]
    assert "run_at" in parsed

    records2 = [{"pin_id": "p2", "kind": "component", "commit": _HEX40_B, "subject": "y", "stable_patch_id": "s2", "state": "pr-open", "evidence": {}}]
    ledger.append_history(records2, history_path)

    lines = history_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # appended, first line untouched
    assert json.loads(lines[0])["records"] == records1
    assert json.loads(lines[1])["records"] == records2


def test_default_history_path_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "custom-history" / "provenance-history.jsonl"
    monkeypatch.setenv("FORK_INTEGRATION_LEDGER_HISTORY_PATH", str(target))
    assert ledger.default_history_path() == target


def test_history_survives_many_runs_without_pruning(tmp_path: Path) -> None:
    """A months-old lineage's entry is never dropped by later runs."""
    history_path = tmp_path / "history.jsonl"
    old_record = [
        {
            "pin_id": "old-pin", "kind": "component", "commit": _HEX40_A,
            "stable_patch_id": "old-id", "subject": "old change", "state": "absorbed-modified", "evidence": {},
        }
    ]
    ledger.append_history(old_record, history_path, run_at="2026-01-01T00:00:00+00:00")

    for i in range(50):
        ledger.append_history(
            [{"pin_id": f"other-{i}", "kind": "component", "commit": _HEX40_B, "stable_patch_id": "x", "subject": "y", "state": "private-only", "evidence": {}}],
            history_path,
            run_at=f"2026-02-{(i % 28) + 1:02d}T00:00:00+00:00",
        )

    entries = ledger._read_history(history_path)
    assert len(entries) == 51
    assert entries[0]["records"] == old_record  # first entry unchanged, still present


def test_ledger_source_has_no_pruning_or_truncation_path() -> None:
    """Static guard (R6): history must be append-only. Grep the source
    rather than trust a docstring -- no unlink/truncate/rmtree of the
    history file, and no truncating 'w' open anywhere in the module."""
    source = Path(ledger.__file__).read_text(encoding="utf-8")
    assert ".unlink(" not in source
    assert ".truncate(" not in source
    assert "shutil.rmtree" not in source
    assert "os.remove(" not in source
    assert '.open("w"' not in source
    assert ".open('w'" not in source


# ── Transition detection ────────────────────────────────────────────────────


def test_diff_vs_previous_no_history_reports_everything_as_new(tmp_path: Path) -> None:
    history_path = tmp_path / "missing.jsonl"
    records = [{"pin_id": "p1", "kind": "component", "commit": _HEX40_A, "stable_patch_id": "s", "subject": "x", "state": "private-only", "evidence": {}}]

    transitions = ledger.diff_vs_previous(records, history_path)

    assert transitions == [{"pin_id": "p1", "from": None, "to": "private-only"}]


def test_diff_vs_previous_detects_exactly_one_changed_pin(tmp_path: Path) -> None:
    history_path = tmp_path / "history.jsonl"
    run1 = [
        {"pin_id": "p1", "kind": "component", "commit": _HEX40_A, "stable_patch_id": "s1", "subject": "x", "state": "private-only", "evidence": {}},
        {"pin_id": "p2", "kind": "component", "commit": _HEX40_B, "stable_patch_id": "s2", "subject": "y", "state": "absorbed-verbatim", "evidence": {}},
    ]
    ledger.append_history(run1, history_path)

    run2 = [
        {"pin_id": "p1", "kind": "component", "commit": _HEX40_A, "stable_patch_id": "s1", "subject": "x", "state": "pr-open", "evidence": {}},
        {"pin_id": "p2", "kind": "component", "commit": _HEX40_B, "stable_patch_id": "s2", "subject": "y", "state": "absorbed-verbatim", "evidence": {}},
    ]

    transitions = ledger.diff_vs_previous(run2, history_path)

    assert transitions == [{"pin_id": "p1", "from": "private-only", "to": "pr-open"}]


def test_diff_vs_previous_unchanged_run_reports_no_transitions(tmp_path: Path) -> None:
    history_path = tmp_path / "history.jsonl"
    records = [{"pin_id": "p1", "kind": "component", "commit": _HEX40_A, "stable_patch_id": "s1", "subject": "x", "state": "private-only", "evidence": {}}]
    ledger.append_history(records, history_path)

    assert ledger.diff_vs_previous(records, history_path) == []


# ── Retirement candidates ───────────────────────────────────────────────────


def _verbatim_record(pin_id: str = "component:comp1:" + _HEX40_A) -> dict[str, Any]:
    return {
        "pin_id": pin_id, "kind": "component", "commit": _HEX40_A, "stable_patch_id": "sid",
        "subject": "x", "state": "absorbed-verbatim",
        "evidence": {"candidate_commit": "cand-sha", "candidate_patch_id": "sid", "upstream_ref": "origin/main"},
    }


def test_retirement_candidates_three_consecutive_absorbed_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    history_path = tmp_path / "history.jsonl"
    for _ in range(3):
        ledger.append_history([_verbatim_record()], history_path)
    monkeypatch.setattr(ledger, "is_ancestor", lambda commit, ref, *, cwd: True)

    candidates = ledger.retirement_candidates(history_path, k=3, repo_dir=tmp_path, upstream_ref="origin/main")

    assert candidates == [_verbatim_record()["pin_id"]]


def test_retirement_candidates_two_consecutive_is_not_enough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    history_path = tmp_path / "history.jsonl"
    for _ in range(2):
        ledger.append_history([_verbatim_record()], history_path)
    monkeypatch.setattr(ledger, "is_ancestor", lambda commit, ref, *, cwd: True)

    candidates = ledger.retirement_candidates(history_path, k=3, repo_dir=tmp_path, upstream_ref="origin/main")

    assert candidates == []


def test_retirement_candidates_requires_consecutive_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A streak broken by one non-absorbed-verbatim run in the middle does
    not count, even though the total absorbed-verbatim count is 3."""
    history_path = tmp_path / "history.jsonl"
    verbatim = _verbatim_record()
    broken = {**verbatim, "state": "pr-open"}
    ledger.append_history([verbatim], history_path)
    ledger.append_history([broken], history_path)
    ledger.append_history([verbatim], history_path)
    monkeypatch.setattr(ledger, "is_ancestor", lambda commit, ref, *, cwd: True)

    candidates = ledger.retirement_candidates(history_path, k=3, repo_dir=tmp_path, upstream_ref="origin/main")

    assert candidates == []


def test_retirement_candidates_requires_live_ancestry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Three consecutive absorbed-verbatim runs but the absorbing candidate
    is no longer an ancestor of the live upstream tip (rewritten branch) ->
    not a retirement candidate."""
    history_path = tmp_path / "history.jsonl"
    for _ in range(3):
        ledger.append_history([_verbatim_record()], history_path)
    monkeypatch.setattr(ledger, "is_ancestor", lambda commit, ref, *, cwd: False)

    candidates = ledger.retirement_candidates(history_path, k=3, repo_dir=tmp_path, upstream_ref="origin/main")

    assert candidates == []


def test_retirement_candidates_without_repo_dir_claims_nothing(tmp_path: Path) -> None:
    """No repo context supplied -> ancestry cannot be verified -> no claim,
    even with a 3-run streak (an unverifiable retirement claim is worse than
    none)."""
    history_path = tmp_path / "history.jsonl"
    for _ in range(3):
        ledger.append_history([_verbatim_record()], history_path)

    assert ledger.retirement_candidates(history_path, k=3, repo_dir=None) == []


# ── Report ───────────────────────────────────────────────────────────────


def test_report_names_evidence_for_every_state_claim() -> None:
    records = [
        {
            "pin_id": "component:comp1:" + _HEX40_A, "kind": "component", "commit": _HEX40_A,
            "stable_patch_id": "s1", "subject": "x", "state": "absorbed-verbatim",
            "evidence": {"candidate_commit": "cand1", "candidate_patch_id": "id1"},
        },
        {
            "pin_id": "component:comp2:" + _HEX40_B, "kind": "component", "commit": _HEX40_B,
            "stable_patch_id": "s2", "subject": "y", "state": "pr-open",
            "evidence": {"pr_state": "OPEN", "pr_url": "https://github.com/example/pull/1"},
        },
        {
            "pin_id": "foundation:found1:" + _HEX40_C, "kind": "foundation", "commit": _HEX40_C,
            "stable_patch_id": "s3", "subject": "z", "state": "superseded",
            "evidence": {"blocklisted_patch_id": "s3", "reason": "rejected"},
        },
    ]

    markdown = ledger.report(records)

    assert "PR text" in markdown and "untrusted" in markdown
    assert "Generated at:" in markdown
    for record in records:
        assert record["pin_id"] in markdown
        assert record["state"] in markdown
    assert "cand1" in markdown
    assert "id1" in markdown
    assert "OPEN" in markdown
    assert "rejected" in markdown


def test_report_includes_transitions_and_retirement_sections() -> None:
    records = [
        {"pin_id": "p1", "kind": "component", "commit": _HEX40_A, "stable_patch_id": "s1", "subject": "x", "state": "pr-open", "evidence": {}},
    ]
    markdown = ledger.report(
        records,
        transitions=[{"pin_id": "p1", "from": "private-only", "to": "pr-open"}],
        retiring=["p2"],
    )

    assert "State transitions" in markdown
    assert "private-only" in markdown and "pr-open" in markdown
    assert "Retirement candidates" in markdown
    assert "p2" in markdown


def test_report_omits_optional_sections_when_absent() -> None:
    records = [{"pin_id": "p1", "kind": "component", "commit": _HEX40_A, "stable_patch_id": "s1", "subject": "x", "state": "private-only", "evidence": {}}]
    markdown = ledger.report(records)
    assert "State transitions" not in markdown
    assert "Retirement candidates" not in markdown


# ── Real-manifest smoke test (no network, no real upstream) ───────────────


def test_derive_against_real_manifest_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """derive() against THIS repo's actual manifest, with a fully
    monkeypatched git layer returning canned no-match outputs, must run
    clean end to end -- no network, no real upstream fetch."""
    monkeypatch.setattr(ledger, "_same_subject_candidates", lambda subject, upstream_ref, *, cwd: [])
    monkeypatch.setattr(ledger, "stable_patch_id", lambda commit, *, cwd: None)
    monkeypatch.setattr(ledger, "is_ancestor", lambda commit, ref, *, cwd: False)

    records = ledger.derive(
        manifest_path=ledger.DEFAULT_MANIFEST_PATH,
        repo_dir=tmp_path,
        upstream_ref="origin/main",
        gh_enabled=False,
    )

    manifest = json.loads(ledger.DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
    expected_count = sum(len(f["patches"]) for f in manifest["upstream_foundations"]) + sum(
        len(c["patches"]) for c in manifest["components"]
    )
    assert len(records) == expected_count
    assert expected_count > 0

    valid_states = set(ledger.CORE_STATES)
    for record in records:
        assert record["state"] in valid_states or record["state"].startswith(ledger.EXCLUDED_STATE_PREFIX)
        assert record["pin_id"]
        assert record["kind"] in ("foundation", "component")

    # Known truth (this week): the #63047 journal fix foundation patches
    # both carry a reviewed_replacement -> absorbed-modified, independent of
    # the (here, fully mocked-out) live upstream candidate search.
    journal_records = [r for r in records if r["pin_id"].startswith("foundation:inflight-journal-per-session:")]
    assert journal_records
    assert all(r["state"] == "absorbed-modified" for r in journal_records)

    markdown = ledger.report(records)
    assert "Fork-integration provenance report" in markdown


# ── CLI ──────────────────────────────────────────────────────────────────


def test_main_derive_prints_json_and_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    patch = _patch()
    manifest_path = _write_manifest(tmp_path, _manifest(components=[_component("comp1", _HEX40_B, [patch])]))
    monkeypatch.setattr(ledger, "_same_subject_candidates", lambda subject, upstream_ref, *, cwd: [])

    exit_code = ledger.main(["derive", "--manifest", str(manifest_path), "--repo", str(tmp_path), "--no-gh"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload[0]["state"] == "private-only"


def test_main_report_prints_markdown_and_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    patch = _patch()
    manifest_path = _write_manifest(tmp_path, _manifest(components=[_component("comp1", _HEX40_B, [patch])]))
    monkeypatch.setattr(ledger, "_same_subject_candidates", lambda subject, upstream_ref, *, cwd: [])
    history_path = tmp_path / "history.jsonl"

    exit_code = ledger.main(
        ["report", "--manifest", str(manifest_path), "--repo", str(tmp_path), "--no-gh", "--history-path", str(history_path)]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Fork-integration provenance report" in out
    assert not history_path.exists()  # report never appends


def test_main_history_appends_and_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    patch = _patch()
    manifest_path = _write_manifest(tmp_path, _manifest(components=[_component("comp1", _HEX40_B, [patch])]))
    monkeypatch.setattr(ledger, "_same_subject_candidates", lambda subject, upstream_ref, *, cwd: [])
    history_path = tmp_path / "history.jsonl"

    exit_code = ledger.main(
        ["history", "--manifest", str(manifest_path), "--repo", str(tmp_path), "--no-gh", "--history-path", str(history_path)]
    )

    assert exit_code == 0
    assert history_path.exists()
    assert len(history_path.read_text(encoding="utf-8").splitlines()) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["record_count"] == 1


# ── Smoke: expected entry points exist ──────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    ["derive", "append_history", "diff_vs_previous", "report", "retirement_candidates", "main", "CORE_STATES"],
)
def test_expected_entry_points_exist(name: str) -> None:
    assert hasattr(ledger, name)
