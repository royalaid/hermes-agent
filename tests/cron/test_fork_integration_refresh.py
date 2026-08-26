from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.fork_integration import refresh


def git(repo: Path, *args: str, check: bool = True, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(refresh.GIT_ENV)
    env.update(extra_env or {})
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=30,
        creationflags=refresh.CREATE_NO_WINDOW,
        env=env,
    )
    if check:
        result.check_returncode()
    return result


class Repos:
    def __init__(self, root: Path) -> None:
        self.author = root / "author"
        self.upstream = root / "upstream.git"
        self.origin = root / "origin.git"
        self.runner = root / "runner"
        self.author.mkdir()
        git(self.author, "init", "--initial-branch=main")
        git(self.author, "config", "user.email", "refresh@test.invalid")
        git(self.author, "config", "user.name", "Refresh Test")
        self.write("shared.txt", "base\n")
        self.commit("base", "2026-08-20T00:00:00+00:00")
        self.base = self.head()
        git(root, "init", "--bare", str(self.upstream))
        git(root, "init", "--bare", str(self.origin))
        git(self.author, "remote", "add", "upstream", str(self.upstream))
        git(self.author, "remote", "add", "origin", str(self.origin))
        git(self.author, "push", "upstream", "main:main")
        git(self.author, "branch", "fork-integration")
        git(self.author, "push", "origin", "fork-integration:fork-integration")
        git(self.origin, "symbolic-ref", "HEAD", "refs/heads/fork-integration")
        git(root, "clone", "--branch", "fork-integration", str(self.origin), str(self.runner))
        git(self.runner, "remote", "add", "upstream", str(self.upstream))
        git(self.runner, "config", "user.email", "refresh@test.invalid")
        git(self.runner, "config", "user.name", "Refresh Test")

    def checkout(self, branch: str, start: str | None = None) -> None:
        if start is None:
            git(self.author, "checkout", branch)
        else:
            git(self.author, "checkout", "-b", branch, start)

    def write(self, name: str, content: str) -> None:
        path = self.author / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(self, message: str, when: str | None = None) -> str:
        git(self.author, "add", ".")
        dates = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when} if when else None
        git(self.author, "commit", "-m", message, extra_env=dates)
        return self.head()

    def head(self) -> str:
        return git(self.author, "rev-parse", "HEAD").stdout.strip()

    def fork_change(self, name: str, content: str, message: str = "fork change") -> str:
        self.checkout("fork-integration")
        self.write(name, content)
        sha = self.commit(message)
        git(self.author, "push", "origin", "fork-integration")
        return sha

    def upstream_change(self, name: str, content: str, message: str = "upstream change", when: str | None = None) -> str:
        self.checkout("main")
        self.write(name, content)
        sha = self.commit(message, when)
        git(self.author, "push", "upstream", "main")
        return sha

    def origin_head(self) -> str:
        return git(self.origin, "rev-parse", "refs/heads/fork-integration").stdout.strip()


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    return Repos(tmp_path)


def compose(repos: Repos, **kwargs):
    return refresh.compose(
        repos.runner,
        upstream_remote="upstream",
        published_remote="origin",
        checks=(),
        **kwargs,
    )


def show(repos: Repos, sha: str, path: str) -> str:
    return git(repos.runner, "show", f"{sha}:{path}").stdout


def test_already_current_does_not_construct_or_push(repos: Repos, monkeypatch: pytest.MonkeyPatch) -> None:
    repos.fork_change("fork.txt", "fork\n")
    monkeypatch.setattr(refresh, "_scratch_rebase", lambda *args, **kwargs: pytest.fail("scratch used"))
    monkeypatch.setattr(refresh, "_push_with_lease", lambda *args, **kwargs: pytest.fail("push used"))

    result = compose(repos, dry_run=False)

    assert result["status"] == "already_current"
    assert result["candidate"] == result["captured_published"]
    assert result["pushed"] is False


def test_linear_advance_replays_fork_range(repos: Repos) -> None:
    fork_sha = repos.fork_change("fork.txt", "fork\n")
    upstream_sha = repos.upstream_change("upstream.txt", "upstream\n")

    result = compose(repos, dry_run=True)

    assert result["status"] == "candidate_ready"
    assert git(repos.runner, "merge-base", "--is-ancestor", upstream_sha, result["candidate"], check=False).returncode == 0
    assert show(repos, result["candidate"], "fork.txt") == "fork\n"
    assert {entry["commit"]: entry["status"] for entry in result["dispositions"]}[fork_sha] == "replayed"


def test_daily_cutoff_keeps_same_upstream_sha_within_window(repos: Repos) -> None:
    repos.fork_change("fork.txt", "fork\n")
    admitted = repos.upstream_change("admitted.txt", "admitted\n", when="2026-08-25T08:30:00+00:00")
    late_one = repos.upstream_change("late-one.txt", "late one\n", when="2026-08-25T09:30:00+00:00")
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)

    first = compose(repos, dry_run=True, upstream_cutoff_hour=9, now_utc=now)
    late_two = repos.upstream_change("late-two.txt", "late two\n", when="2026-08-25T12:30:00+00:00")
    second = compose(repos, dry_run=True, upstream_cutoff_hour=9, now_utc=now)

    assert first["upstream_sha"] == second["upstream_sha"] == admitted
    assert first["fetched_upstream_head"] == late_one
    assert second["fetched_upstream_head"] == late_two
    assert first["upstream_cutoff"] == second["upstream_cutoff"] == "2026-08-25T09:00:00+00:00"
    explicit = compose(repos, dry_run=True, upstream_sha=admitted, upstream_cutoff_hour=9, now_utc=now)
    assert explicit["upstream_sha"] == admitted
    assert explicit["upstream_cutoff"] is None


def test_daily_cutoff_advances_after_next_boundary(repos: Repos) -> None:
    repos.fork_change("fork.txt", "fork\n")
    first_sha = repos.upstream_change("first.txt", "first\n", when="2026-08-25T08:30:00+00:00")
    first = compose(
        repos, dry_run=True, upstream_cutoff_hour=9,
        now_utc=datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
    )
    next_sha = repos.upstream_change("next.txt", "next\n", when="2026-08-26T08:30:00+00:00")
    second = compose(
        repos, dry_run=True, upstream_cutoff_hour=9,
        now_utc=datetime(2026, 8, 26, 12, tzinfo=timezone.utc),
    )

    assert first["upstream_sha"] == first_sha
    assert second["upstream_sha"] == next_sha
    assert second["upstream_sha"] != first["upstream_sha"]


def test_merge_composition_is_retained_when_characterized(repos: Repos) -> None:
    repos.checkout("fork-integration")
    repos.write("left.txt", "left\n")
    repos.commit("left")
    repos.checkout("side", repos.base)
    repos.write("right.txt", "right\n")
    repos.commit("right")
    repos.checkout("fork-integration")
    git(repos.author, "merge", "--no-ff", "side", "-m", "compose sides")
    merge_sha = repos.head()
    git(repos.author, "push", "origin", "fork-integration")
    repos.upstream_change("upstream.txt", "upstream\n")
    assertions = {
        merge_sha: (
            refresh.TreeAssertion("left.txt", contains=("left",)),
            refresh.TreeAssertion("right.txt", contains=("right",)),
        )
    }

    result = compose(repos, dry_run=True, merge_assertions=assertions)

    assert show(repos, result["candidate"], "left.txt") == "left\n"
    assert show(repos, result["candidate"], "right.txt") == "right\n"
    merge_disposition = next(item for item in result["dispositions"] if item["commit"] == merge_sha)
    assert merge_disposition == {
        "commit": merge_sha,
        "status": "characterized_merge_assertion",
        "assertion_count": 2,
    }
    assert result["dispositions"][-1] == merge_disposition


def partial_patch_merge(
    repos: Repos, merge_message: str = "characterized composition"
) -> tuple[str, str, dict[str, tuple[refresh.TreeAssertion, ...]]]:
    repos.checkout("main")
    for name in ("effect-a.txt", "effect-b.txt", "effect-c.txt"):
        repos.write(name, "old\n")
    setup = repos.commit("shared setup")
    git(repos.author, "push", "upstream", "main")
    git(repos.author, "push", "--force", "origin", f"{setup}:fork-integration")
    git(repos.author, "branch", "-f", "fork-integration", setup)
    repos.checkout("side", setup)
    for name in ("effect-a.txt", "effect-b.txt", "effect-c.txt"):
        repos.write(name, "new\n")
    side_sha = repos.commit("side patch with two effects")
    repos.checkout("fork-integration")
    repos.write("left.txt", "left\n")
    repos.commit("left side")
    git(repos.author, "merge", "--no-ff", "side", "-m", merge_message)
    merge_sha = repos.head()
    git(repos.author, "push", "origin", "fork-integration")
    repos.checkout("main")
    repos.write("effect-a.txt", "new\n")
    repos.commit("upstream carries half of side patch")
    git(repos.author, "push", "upstream", "main")
    assertions = {merge_sha: tuple(
        refresh.TreeAssertion(name, contains=("new",), absent=("old",))
        for name in ("effect-a.txt", "effect-b.txt", "effect-c.txt")
    )}
    return side_sha, merge_sha, assertions


def test_arbitrary_non_merge_patch_id_miss_still_fails(repos: Repos) -> None:
    side_sha, _merge_sha, assertions = partial_patch_merge(repos)

    with pytest.raises(refresh.RefreshError, match=side_sha):
        compose(repos, dry_run=True, merge_assertions=assertions)


def test_explicit_side_parent_mapping_requires_its_merge_assertion(
    repos: Repos, monkeypatch: pytest.MonkeyPatch
) -> None:
    side_sha, merge_sha, assertions = partial_patch_merge(repos, refresh.KILL_ALL_MERGE)

    result = compose(repos, dry_run=True, merge_assertions=assertions)

    disposition = {entry["commit"]: entry for entry in result["dispositions"]}[side_sha]
    assert disposition["status"] == "represented_by_merge_assertion"
    assert disposition["represented_by"] == merge_sha
    bad = {merge_sha: (refresh.TreeAssertion("effect-c.txt", contains=("unproved-value",)),)}
    with pytest.raises(refresh.RefreshError, match="merge assertion failed"):
        compose(repos, dry_run=True, merge_assertions=bad)


def test_rewritten_merge_identity_and_side_parent_survive_second_generation(repos: Repos) -> None:
    side_sha, merge_sha, assertions = partial_patch_merge(repos, refresh.KILL_ALL_MERGE)
    stable_assertions = {refresh.KILL_ALL_MERGE: assertions[merge_sha]}

    first = compose(repos, dry_run=False, merge_assertions=stable_assertions)
    assert first["status"] == "published"
    assert repos.origin_head() == first["candidate"]
    assert next(item for item in first["dispositions"] if item["commit"] == side_sha)["status"] == "represented_by_merge_assertion"
    repos.checkout("main")
    repos.write("effect-b.txt", "new\n")
    repos.commit("upstream carries another side-patch effect")
    git(repos.author, "push", "upstream", "main")

    second = compose(repos, dry_run=True, merge_assertions=stable_assertions)

    merge_items = [item for item in second["dispositions"] if item["status"] == "characterized_merge_assertion"]
    assert len(merge_items) == 1
    assert merge_items[0]["commit"] != merge_sha
    represented = [item for item in second["dispositions"] if item["status"] == "represented_by_merge_assertion"]
    assert represented == [{
        "commit": represented[0]["commit"],
        "status": "represented_by_merge_assertion",
        "represented_by": merge_items[0]["commit"],
        "preexisting_patch": False,
    }]


def test_upstream_retained_patch_becomes_empty_without_duplication(repos: Repos) -> None:
    fork_sha = repos.fork_change("shared.txt", "base\nshared\n", "shared fork patch")
    repos.upstream_change("shared.txt", "base\nshared\n", "shared upstream patch")

    result = compose(repos, dry_run=True)

    assert show(repos, result["candidate"], "shared.txt") == "base\nshared\n"
    dispositions = {entry["commit"]: entry for entry in result["dispositions"]}
    assert dispositions[fork_sha]["status"] == "empty"
    assert git(repos.runner, "rev-list", "--count", f'{result["captured_upstream"]}..{result["candidate"]}').stdout.strip() == "0"


def test_upstream_applied_then_reverted_patch_is_replayed(repos: Repos) -> None:
    fork_sha = repos.fork_change("shared.txt", "base\nshared\n", "shared fork patch")
    repos.upstream_change("shared.txt", "base\nshared\n", "shared upstream patch")
    git(repos.author, "revert", "--no-edit", "HEAD")
    git(repos.author, "push", "upstream", "main")

    result = compose(repos, dry_run=True)

    assert show(repos, result["candidate"], "shared.txt") == "base\nshared\n"
    assert {entry["commit"]: entry["status"] for entry in result["dispositions"]}[fork_sha] == "replayed"


def test_uncharacterized_merge_stops_before_replay(repos: Repos, monkeypatch: pytest.MonkeyPatch) -> None:
    repos.checkout("side", repos.base)
    repos.write("side.txt", "side\n")
    repos.commit("side")
    repos.checkout("fork-integration")
    git(repos.author, "merge", "--no-ff", "side", "-m", "uncharacterized")
    merge_sha = repos.head()
    git(repos.author, "push", "origin", "fork-integration")
    repos.upstream_change("upstream.txt", "upstream\n")
    monkeypatch.setattr(refresh, "_scratch_rebase", lambda *args, **kwargs: pytest.fail("scratch used"))

    with pytest.raises(refresh.RefreshError, match=merge_sha):
        compose(repos, dry_run=True, merge_assertions={})


def test_conflict_cleans_scratch_and_keeps_remote(repos: Repos) -> None:
    repos.fork_change("shared.txt", "fork\n")
    published = repos.origin_head()
    repos.upstream_change("shared.txt", "upstream\n")

    with pytest.raises(refresh.RefreshError, match="conflict"):
        compose(repos, dry_run=False)

    assert repos.origin_head() == published
    worktrees = git(repos.runner, "worktree", "list", "--porcelain").stdout
    assert worktrees.count("worktree ") == 1


def test_failed_worktree_deregistration_retains_scratch_and_primary_error(
    repos: Repos, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repos.fork_change("fork.txt", "fork\n")
    repos.upstream_change("upstream.txt", "upstream\n")
    scratch = tmp_path / "retained-scratch"
    monkeypatch.setattr(refresh.tempfile, "mkdtemp", lambda **_kwargs: str(scratch))
    original_git = refresh._git

    def fail_remove(repo, *args, **kwargs):
        if args[:3] == ("worktree", "remove", "--force"):
            return subprocess.CompletedProcess(["git"], 1, "", "forced remove failure")
        return original_git(repo, *args, **kwargs)

    monkeypatch.setattr(refresh, "_git", fail_remove)
    monkeypatch.setattr(refresh, "_run_checks", lambda *_args: (_ for _ in ()).throw(refresh.RefreshError("primary failure")))
    try:
        with pytest.raises(refresh.RefreshError, match="primary failure; scratch cleanup failed"):
            compose(repos, dry_run=True)
        assert scratch.exists()
    finally:
        git(repos.runner, "worktree", "remove", "--force", str(scratch))


def test_lease_race_stops_without_overwriting_remote(repos: Repos, monkeypatch: pytest.MonkeyPatch) -> None:
    repos.fork_change("fork.txt", "fork\n")
    repos.upstream_change("upstream.txt", "upstream\n")
    original_push = refresh._push_with_lease

    def race(repo, remote, published_ref, captured, candidate):
        repos.fork_change("race.txt", "race\n", "racing writer")
        return original_push(repo, remote, published_ref, captured, candidate)

    monkeypatch.setattr(refresh, "_push_with_lease", race)

    with pytest.raises(refresh.RefreshError, match="lease"):
        compose(repos, dry_run=False)

    assert git(repos.origin, "show", f"{repos.origin_head()}:race.txt").stdout == "race\n"


def test_lease_rejection_accepts_remote_already_at_candidate(repos: Repos, monkeypatch: pytest.MonkeyPatch) -> None:
    repos.fork_change("fork.txt", "fork\n")
    repos.upstream_change("upstream.txt", "upstream\n")
    original_push = refresh._push_with_lease

    def converge(repo, remote, published_ref, captured, candidate):
        git(repos.runner, "push", "origin", f"{candidate}:{published_ref}", "--force")
        return original_push(repo, remote, published_ref, captured, candidate)

    monkeypatch.setattr(refresh, "_push_with_lease", converge)

    result = compose(repos, dry_run=False)

    assert result["status"] == "already_published"
    assert result["pushed"] is False
    assert repos.origin_head() == result["candidate"]


def test_dry_run_never_uses_push_path(repos: Repos, monkeypatch: pytest.MonkeyPatch) -> None:
    repos.fork_change("fork.txt", "fork\n")
    published = repos.origin_head()
    repos.upstream_change("upstream.txt", "upstream\n")
    monkeypatch.setattr(refresh, "_push_with_lease", lambda *args, **kwargs: pytest.fail("push used"))

    result = compose(repos, dry_run=True)

    assert result["status"] == "candidate_ready"
    assert result["pushed"] is False
    assert repos.origin_head() == published


def test_main_reports_timeout_as_structured_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("git fetch", 600)

    monkeypatch.setattr(refresh, "compose", timeout)

    assert refresh.main([]) == 1
    output = capsys.readouterr().out
    assert '"status": "failed"' in output
    assert "timed out" in output


def test_main_preserves_quoted_check_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def run_checks(repo, **kwargs):
        refresh._run_checks(Path(repo), kwargs["checks"])
        pytest.fail("failing check passed")

    monkeypatch.setattr(refresh, "compose", run_checks)
    command = json.dumps([sys.executable, "-c", "raise SystemExit(7)"])

    assert refresh.main(["--repo", str(tmp_path), "--check", command]) == 1
    output = capsys.readouterr().out
    assert '"status": "failed"' in output
    assert "focused check failed" in output
