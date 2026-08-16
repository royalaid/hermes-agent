"""U5: characterize the 2026-08-13 audit-item behaviors as they currently
stand in the imported ``hermes-integration-release-windows.py``.

  - ``main()`` validates foundations against the reconstructed *records*
    ledger, not a live upstream/rebased-head scan (anchor: ``main()``'s
    ``validate_required_foundations(upstream, rebased_output_head,
    records=foundation_records)`` call, ~line 1785).
  - A failure before push restores the checkout to the fetched
    ``published_input_head`` via ``restore_pre_push_checkout``, guarded by
    ``not branch_pushed`` (anchor: except handler, ~line 1904).
  - ``--dry-run``'s ``inspect_dry_run()`` reads remote tips through
    ``remote_ref_head`` (``git ls-remote``) and never calls ``git fetch``
    (anchor: ~line 1313, ~1361-1362).

These pin current behavior only; the 2026-08-15 script largely fixed the
2026-08-13 audit items already. The residuals that survived are closed in a
follow-up commit (see ``_validate_required_records`` and
``has_integration_release``).
"""

from __future__ import annotations

import subprocess
import sys
from contextlib import nullcontext
from typing import Any

import pytest

from scripts.fork_integration.release import mod as release

_HEX40_A = "a" * 40


def _harmless_run(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    # `git()` prepends `-c rerere.*=false` flags before the real subcommand,
    # so "rev-parse" is not necessarily args[1] -- check membership instead.
    if args and args[0] == "git" and "rev-parse" in args:
        return subprocess.CompletedProcess(args, 0, stdout=f"{_HEX40_A}\n", stderr="")
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _quiet_failure_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence every best-effort side channel that would otherwise touch the
    real filesystem (log file, Fleet receipt, failure investigator) under
    the real HERMES_HOME during a characterization run."""
    monkeypatch.setattr(release, "log", lambda message: None)
    monkeypatch.setattr(release, "launch_failure_investigator", lambda **kwargs: None)
    monkeypatch.setattr(release, "emit_fleet_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(release, "resolve_failure_investigator_success", lambda: None)
    monkeypatch.setattr(
        release, "fail", lambda message, code=1: (_ for _ in ()).throw(SystemExit(code))
    )


# ── validate_required_foundations is called with records=... ───────────────


def test_main_validates_foundations_against_reconstructed_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Characterization: main() passes a non-None ``records`` ledger into
    ``validate_required_foundations`` rather than relying on its live-scan
    fallback (the ``records is None`` branch)."""
    calls: list[tuple[str, str, Any]] = []

    def spy_validate_foundations(upstream: str, rebased_head: str, *, records: Any = None) -> None:
        calls.append((upstream, rebased_head, records))
        raise RuntimeError("characterization-stop-after-foundations")

    monkeypatch.setattr(release, "run", _harmless_run)
    monkeypatch.setattr(release, "exclusive_lock", nullcontext)
    monkeypatch.setattr(release, "ensure_clean_identity", lambda: (_HEX40_A, _HEX40_A))
    monkeypatch.setattr(release, "synchronize_to_published_head", lambda local, published: published)
    monkeypatch.setattr(release, "verify_upstream_foundations", lambda: [])
    monkeypatch.setattr(release, "verify_manifest_sources", lambda: None)
    monkeypatch.setattr(release, "published_integration_range", lambda published, upstream: (published, []))
    monkeypatch.setattr(release, "output_is_already_based_on_current_upstream", lambda published, upstream: True)
    monkeypatch.setattr(release, "_exact_published_records", lambda commits: [])
    monkeypatch.setattr(release, "patch_resolution", lambda current, patches, **kwargs: ([], []))
    monkeypatch.setattr(release, "upstream_patch_resolution", lambda current, **kwargs: ([], []))
    monkeypatch.setattr(release, "validate_required_components", lambda *args, **kwargs: None)
    monkeypatch.setattr(release, "validate_required_foundations", spy_validate_foundations)
    monkeypatch.setattr(release, "restore_pre_push_checkout", lambda head: None)
    monkeypatch.setattr(sys, "argv", ["hermes-integration-release-windows.py"])
    _quiet_failure_paths(monkeypatch)

    with pytest.raises(SystemExit):
        release.main()

    assert len(calls) == 1
    upstream, rebased_head, records = calls[0]
    assert records is not None, "main() must call validate_required_foundations with a records ledger"
    assert records == []  # empty because every resolution stub above produced no patches


# ── pre-push failure restores to published_input_head ───────────────────────


def test_main_pre_push_failure_restores_published_input_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Characterization: a failure before the branch is pushed calls
    restore_pre_push_checkout(published_input_head), guarded by
    ``not branch_pushed`` -- never invoked once the push has happened."""
    restore_calls: list[str] = []

    def raise_before_reconstruction() -> list[dict[str, Any]]:
        raise RuntimeError("simulated failure before push")

    monkeypatch.setattr(release, "run", _harmless_run)
    monkeypatch.setattr(release, "exclusive_lock", nullcontext)
    monkeypatch.setattr(release, "ensure_clean_identity", lambda: (_HEX40_A, _HEX40_A))
    monkeypatch.setattr(release, "synchronize_to_published_head", lambda local, published: published)
    monkeypatch.setattr(release, "verify_upstream_foundations", raise_before_reconstruction)
    monkeypatch.setattr(release, "restore_pre_push_checkout", lambda head: restore_calls.append(head))
    monkeypatch.setattr(sys, "argv", ["hermes-integration-release-windows.py"])
    _quiet_failure_paths(monkeypatch)

    with pytest.raises(SystemExit):
        release.main()

    # published_input_head is resolved via `git rev-parse refs/remotes/<fork>/<branch>`
    # before verify_upstream_foundations runs; _harmless_run answers every
    # rev-parse with _HEX40_A.
    assert restore_calls == [_HEX40_A]


# ── --dry-run reads remotes via remote_ref_head, never fetches ──────────────


def test_dry_run_reads_remotes_via_remote_ref_head_never_fetches(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Characterization: inspect_dry_run() resolves both remote tips through
    remote_ref_head (git ls-remote) and never issues `git fetch`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Fork Integration Test"], cwd=repo, check=True)
    (repo / "file.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, text=True, capture_output=True, check=True,
    ).stdout.strip()

    original_run = release.run
    recorded: list[tuple[Any, ...]] = []

    def run_in_repo(*args: Any, **kwargs: Any) -> Any:
        recorded.append(args)
        kwargs.setdefault("cwd", repo)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(release, "run", run_in_repo)
    monkeypatch.setattr(release, "WORKTREE", repo)
    monkeypatch.setattr(release, "BRANCH", branch)

    remote_ref_calls: list[tuple[str, str]] = []

    def fake_remote_ref_head(remote: str, ref: str) -> str:
        remote_ref_calls.append((remote, ref))
        return "a" * 40

    monkeypatch.setattr(release, "remote_ref_head", fake_remote_ref_head)

    result = release.inspect_dry_run()

    assert result["ok"] is True
    assert len(remote_ref_calls) == 2
    assert {call[0] for call in remote_ref_calls} == {release.FORK_REMOTE, release.UPSTREAM_REMOTE}
    assert not any(args and args[0] == "git" and "fetch" in args for args in recorded)
