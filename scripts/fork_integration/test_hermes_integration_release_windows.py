"""Regression tests for the integration release reconstruction guardrails.

Run with: python -m unittest -v test_hermes_integration_release_windows.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).with_name("hermes-integration-release-windows.py")
SPEC = importlib.util.spec_from_file_location("integration_release", SCRIPT)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@contextmanager
def chdir(path: Path):
    before = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(before)


class IntegrationReleaseRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="hermes-integration-release-test-")
        self.repo = Path(self.temp.name)
        self.command("git", "init", "-q")
        self.command("git", "config", "user.email", "test@example.invalid")
        self.command("git", "config", "user.name", "Integration test")
        self.write_and_commit("base.txt", "base\n", "base")
        self.base = self.command("git", "rev-parse", "HEAD").stdout.strip()
        self.initial_branch = self.command("git", "branch", "--show-current").stdout.strip()
        self.original_run = release.run
        self.original_git = release.git
        self.original_log_path = release.LOG_PATH
        self.original_review_dir = release.REVIEW_DIR
        self.original_launch_failure_investigator = release.launch_failure_investigator
        self.original_resolve_failure_investigator_success = release.resolve_failure_investigator_success
        self.original_resolution_backend = release.RESOLUTION_BACKEND
        # A test must never spawn the real claude backend: a no-op backend
        # changes nothing, fails verification, and degrades to fail-closed.
        release.RESOLUTION_BACKEND = lambda _prompt, _files: ""
        release.run = self.local_run
        release.LOG_PATH = self.repo / ".git" / "release.log"
        release.REVIEW_DIR = self.repo / "reviews"
        release.launch_failure_investigator = lambda **_kwargs: None
        release.resolve_failure_investigator_success = lambda: None
        # Parking/caching must never touch the machine's real ledgers.
        self.original_parked_path = release.PARKED_COMMITS_PATH
        release.PARKED_COMMITS_PATH = self.repo / ".git" / "parked-commits.json"
        self.original_cache_path = release.RESOLUTION_CACHE_PATH
        release.RESOLUTION_CACHE_PATH = self.repo / ".git" / "resolution-cache.json"

    def tearDown(self) -> None:
        release.PARKED_COMMITS_PATH = self.original_parked_path
        release.RESOLUTION_CACHE_PATH = self.original_cache_path
        release.run = self.original_run
        release.git = self.original_git
        release.LOG_PATH = self.original_log_path
        release.REVIEW_DIR = self.original_review_dir
        release.launch_failure_investigator = self.original_launch_failure_investigator
        release.resolve_failure_investigator_success = self.original_resolve_failure_investigator_success
        release.RESOLUTION_BACKEND = self.original_resolution_backend
        self.temp.cleanup()

    def command(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args, cwd=self.repo, text=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check,
        )

    def local_run(self, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        check = bool(kwargs.get("check", True))
        result = self.command(*args, check=False)
        if check and result.returncode:
            raise RuntimeError(result.stderr.strip() or f"{' '.join(args)} failed")
        return result

    def write_and_commit(self, filename: str, content: str, subject: str) -> str:
        (self.repo / filename).write_text(content, encoding="utf-8")
        self.command("git", "add", filename)
        self.command("git", "commit", "-qm", subject)
        return self.command("git", "rev-parse", "HEAD").stdout.strip()

    def command_at(self, repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args, cwd=repo, text=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check,
        )

    def write_and_commit_at(self, repo: Path, filename: str, content: str, subject: str) -> str:
        (repo / filename).write_text(content, encoding="utf-8")
        self.command_at(repo, "git", "add", filename)
        self.command_at(repo, "git", "commit", "-qm", subject)
        return self.command_at(repo, "git", "rev-parse", "HEAD").stdout.strip()

    def published_branch_fixture(self, *, divergent: bool = False) -> dict[str, str]:
        root = Path(self.temp.name)
        fork = root / "fork.git"
        publisher = root / "publisher"
        scheduler = root / "scheduler"
        self.command_at(root, "git", "init", "-q", "--bare", str(fork))
        self.command_at(root, "git", "clone", "-q", str(fork), str(publisher))
        self.command_at(publisher, "git", "config", "user.email", "publisher@example.invalid")
        self.command_at(publisher, "git", "config", "user.name", "Publisher")
        self.command_at(publisher, "git", "checkout", "-qb", release.BRANCH)
        base = self.write_and_commit_at(publisher, "integration.txt", "base\n", "integration base")
        self.command_at(publisher, "git", "push", "-q", "-u", "origin", release.BRANCH)
        if not divergent:
            automated = self.write_and_commit_at(publisher, "automated.txt", "automated\n", "automated integration tip")
            self.command_at(publisher, "git", "push", "-q", "origin", release.BRANCH)
        self.command_at(root, "git", "clone", "-q", "-o", release.FORK_REMOTE, "--branch", release.BRANCH, str(fork), str(scheduler))
        self.command_at(scheduler, "git", "config", "user.email", "scheduler@example.invalid")
        self.command_at(scheduler, "git", "config", "user.name", "Scheduler")
        if divergent:
            automated = self.write_and_commit_at(scheduler, "automated.txt", "automated\n", "automated integration tip")
        published = self.write_and_commit_at(publisher, "user.txt", "user\n", "published user integration")
        self.command_at(publisher, "git", "push", "-q", "origin", release.BRANCH)
        self.command_at(scheduler, "git", "fetch", "-q", release.FORK_REMOTE, f"+refs/heads/{release.BRANCH}:refs/remotes/{release.FORK_REMOTE}/{release.BRANCH}")
        self.repo = scheduler
        return {"base": base, "local": automated, "published": published}

    def test_clean_stale_scheduler_fast_forwards_to_fetched_published_head(self) -> None:
        fixture = self.published_branch_fixture()
        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), fixture["local"])
        self.assertEqual(self.command("git", "merge-base", "--is-ancestor", fixture["local"], fixture["published"], check=False).returncode, 0)
        with chdir(self.repo):
            lease = release.synchronize_to_published_head(fixture["local"], fixture["published"])
        self.assertEqual(lease, fixture["published"])
        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), fixture["published"])
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")

    def test_clean_divergent_scheduler_adopts_published_head_and_saves_local_tip(self) -> None:
        fixture = self.published_branch_fixture(divergent=True)
        self.assertEqual(self.command("git", "merge-base", fixture["local"], fixture["published"]).stdout.strip(), fixture["base"])
        with chdir(self.repo):
            lease = release.synchronize_to_published_head(fixture["local"], fixture["published"])
        self.assertEqual(lease, fixture["published"])
        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), fixture["published"])
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")
        safety_refs = self.command("git", "for-each-ref", "--format=%(refname)", "--points-at", fixture["local"], "refs/heads/safety", "refs/heads/recovery").stdout.splitlines()
        self.assertTrue(safety_refs, "former divergent local tip must remain reachable from a safety/recovery ref")

    def test_published_head_synchronization_refuses_dirty_worktree(self) -> None:
        fixture = self.published_branch_fixture()
        (self.repo / "automated.txt").write_text("dirty\n", encoding="utf-8")
        with chdir(self.repo), self.assertRaisesRegex(RuntimeError, r"dirty (?:working tree|worktree)"):
            release.synchronize_to_published_head(fixture["local"], fixture["published"])
        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), fixture["local"])
        self.assertNotEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")

    def test_published_head_synchronization_refuses_wrong_checked_out_branch(self) -> None:
        fixture = self.published_branch_fixture()
        self.command("git", "checkout", "-qb", "not-fork-integration")
        with chdir(self.repo), self.assertRaisesRegex(RuntimeError, rf"checked out branch.*{re.escape(release.BRANCH)}"):
            release.synchronize_to_published_head(fixture["local"], fixture["published"])
        self.assertEqual(self.command("git", "branch", "--show-current").stdout.strip(), "not-fork-integration")
        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), fixture["local"])

    def test_published_head_synchronization_refuses_missing_published_ref(self) -> None:
        fixture = self.published_branch_fixture()
        published_ref = f"refs/remotes/{release.FORK_REMOTE}/{release.BRANCH}"
        self.command("git", "update-ref", "-d", published_ref)
        with chdir(self.repo), self.assertRaisesRegex(RuntimeError, rf"missing.*{re.escape(published_ref)}"):
            release.synchronize_to_published_head(fixture["local"], fixture["published"])
        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), fixture["local"])

    def test_abort_after_prior_success_is_clean_but_does_not_reset_entire_series(self) -> None:
        self.command("git", "checkout", "-qb", "series")
        first = self.write_and_commit("first.txt", "first\n", "first required patch")
        conflict = self.write_and_commit("base.txt", "series\n", "conflicting required patch")
        self.command("git", "checkout", "-q", self.initial_branch)
        self.write_and_commit("base.txt", "upstream\n", "upstream conflicting change")
        self.command("git", "cherry-pick", first)
        failed = self.command("git", "cherry-pick", conflict, check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.command("git", "cherry-pick", "--abort")
        with chdir(self.repo):
            in_progress, dirty = release.cherry_pick_is_cleanly_aborted()
        self.assertFalse(in_progress)
        self.assertFalse(dirty)
        self.assertEqual(self.command("git", "rev-list", "--count", f"{self.base}..HEAD").stdout.strip(), "2")

    def test_exact_upstream_equivalent_is_absorbed_not_reapplied(self) -> None:
        self.command("git", "checkout", "-qb", "series")
        source = self.write_and_commit("feature.txt", "feature\n", "required feature")
        with chdir(self.repo):
            source_patch_id = release.stable_patch_id(source)
        self.command("git", "checkout", "-q", self.initial_branch)
        self.command("git", "cherry-pick", source)
        upstream = self.command("git", "rev-parse", "HEAD").stdout.strip()
        old_required = release.REQUIRED_PATCHES
        release.REQUIRED_PATCHES = [{"commit": source, "subject": "required feature", "stable_patch_id": source_patch_id}]
        try:
            with chdir(self.repo):
                to_apply, absorbed = release.upstream_patch_resolution(upstream)
        finally:
            release.REQUIRED_PATCHES = old_required
        self.assertEqual(to_apply, [])
        self.assertEqual(len(absorbed), 1)
        self.assertEqual(absorbed[0]["commit"], source)
        self.assertEqual(absorbed[0]["upstream_commit"], upstream)

    def test_published_head_already_in_upstream_has_empty_range_without_replay_mutation(self) -> None:
        upstream = self.write_and_commit("upstream.txt", "upstream\n", "upstream tip")
        recorded: list[tuple[str, ...]] = []

        def recording_git(*args: str, **kwargs: object) -> str:
            recorded.append(args)
            return self.original_git(*args, **kwargs)

        release.git = recording_git
        with chdir(self.repo):
            for published in (upstream, self.base):
                base, commits = release.published_integration_range(published, upstream)
                replayed = release.replay_published_integration_range(published, upstream)
                self.assertEqual(base, published)
                self.assertEqual(commits, [])
                self.assertEqual(replayed, [])
        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), upstream)
        self.assertFalse(
            any(args[:2] in {("reset", "--hard"), ("cherry-pick", "--allow-empty")} for args in recorded),
            f"empty published range must not reset or cherry-pick: {recorded}",
        )

    def test_full_range_skips_patch_already_present_upstream_and_replays_distinct_commit(self) -> None:
        self.command("git", "checkout", "-qb", "published", self.base)
        duplicate = self.write_and_commit("duplicate.txt", "same patch\n", "published duplicate")
        distinct = self.write_and_commit("distinct.txt", "published only\n", "published distinct")
        published = self.command("git", "rev-parse", "HEAD").stdout.strip()
        self.command("git", "checkout", "-q", self.initial_branch)
        self.command("git", "cherry-pick", duplicate)
        upstream = self.command("git", "rev-parse", "HEAD").stdout.strip()
        self.command("git", "checkout", "-q", "published")

        with chdir(self.repo):
            replayed = release.replay_published_integration_range(published, upstream)

        self.assertEqual(replayed, [distinct])
        self.assertEqual(
            self.command("git", "log", "--reverse", "--format=%s", f"{upstream}..HEAD").stdout.splitlines(),
            ["published distinct"],
        )
        self.assertEqual(self.command("git", "show", "HEAD:duplicate.txt").stdout, "same patch\n")

    def test_unrelated_published_and_upstream_histories_are_rejected_before_replay(self) -> None:
        self.command("git", "checkout", "--orphan", "unrelated")
        self.command("git", "rm", "-q", "-rf", ".")
        unrelated_head = self.write_and_commit("other.txt", "other\n", "unrelated root")
        self.command("git", "checkout", "-q", self.initial_branch)
        with chdir(self.repo), self.assertRaisesRegex(RuntimeError, r"unique merge base.*merge_bases=\[\]"):
            release.published_integration_range(unrelated_head, self.base)

    def test_main_does_not_push_when_published_replay_conflicts(self) -> None:
        pushed: list[tuple[str, ...]] = []
        old_lock = release.exclusive_lock
        old_argv = __import__("sys").argv
        old_fail = release.fail
        old_emit = release.emit_fleet_receipt
        old_identity = release.ensure_clean_identity
        old_sync = release.synchronize_to_published_head
        old_foundations = release.verify_upstream_foundations
        old_sources = release.verify_manifest_sources
        old_resolution = release.patch_resolution
        old_upstream_resolution = release.upstream_patch_resolution
        old_range = release.published_integration_range
        old_replay = release.replay_published_integration_range

        def push_recording_run(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args and args[0] == "git" and "push" in args:
                pushed.append(args)
            stdout = ""
            if "rev-parse" in args:
                stdout = self.base + "\n"
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        release.run = push_recording_run
        release.exclusive_lock = nullcontext
        release.fail = lambda message, code=1: (_ for _ in ()).throw(SystemExit(code))
        release.emit_fleet_receipt = lambda *args, **kwargs: None
        release.ensure_clean_identity = lambda: (self.base, self.base)
        release.synchronize_to_published_head = lambda local, published: published
        release.verify_upstream_foundations = lambda: []
        release.verify_manifest_sources = lambda: None
        release.patch_resolution = lambda upstream, patches, **_kwargs: ([], [])
        release.upstream_patch_resolution = lambda upstream, **_kwargs: ([], [])
        release.published_integration_range = lambda published, upstream: (self.base, ["conflicting-commit"])
        release.replay_published_integration_range = lambda published, upstream: (_ for _ in ()).throw(RuntimeError("replay conflict"))
        try:
            __import__("sys").argv = [str(SCRIPT)]
            with self.assertRaises(SystemExit):
                release.main()
        finally:
            __import__("sys").argv = old_argv
            release.run = self.local_run
            release.exclusive_lock = old_lock
            release.fail = old_fail
            release.emit_fleet_receipt = old_emit
            release.ensure_clean_identity = old_identity
            release.synchronize_to_published_head = old_sync
            release.verify_upstream_foundations = old_foundations
            release.verify_manifest_sources = old_sources
            release.patch_resolution = old_resolution
            release.upstream_patch_resolution = old_upstream_resolution
            release.published_integration_range = old_range
            release.replay_published_integration_range = old_replay
        self.assertEqual(pushed, [], "a replay conflict must prevent git push in main")

    def test_complete_published_range_replays_direct_commits_in_original_order(self) -> None:
        self.command("git", "checkout", "-qb", "published", self.base)
        required = self.write_and_commit("required.txt", "required\n", "required component")
        direct = self.write_and_commit("direct.txt", "direct\n", "direct user fix")
        canary = self.write_and_commit("canary.txt", "canary\n", "canary")
        published = self.command("git", "rev-parse", "HEAD").stdout.strip()
        self.command("git", "checkout", "-q", self.initial_branch)
        self.write_and_commit("upstream-1.txt", "one\n", "new upstream 1")
        upstream = self.write_and_commit("upstream-2.txt", "two\n", "new upstream 2")
        self.command("git", "checkout", "-q", "published")

        with chdir(self.repo):
            old_base, commits = release.published_integration_range(published, upstream)
            replayed = release.replay_published_integration_range(published, upstream)

        self.assertEqual(old_base, self.base)
        self.assertEqual(commits, [required, direct, canary])
        self.assertEqual(replayed, [required, direct, canary])
        self.assertEqual(
            self.command("git", "log", "--reverse", "--format=%s", f"{upstream}..HEAD").stdout.splitlines(),
            ["required component", "direct user fix", "canary"],
        )
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")

    def test_published_merge_replays_first_parent_delta_including_unique_resolution(self) -> None:
        self.command("git", "checkout", "-qb", "published", self.base)
        direct = self.write_and_commit("direct.txt", "direct\n", "published direct")
        self.command("git", "checkout", "-qb", "published-side", self.base)
        side = self.write_and_commit("side.txt", "side\n", "published side")
        self.command("git", "checkout", "-q", "published")
        self.command("git", "merge", "-q", "--no-ff", "--no-commit", "published-side")
        (self.repo / "resolution.txt").write_text("merge-only resolution\n", encoding="utf-8")
        self.command("git", "add", "resolution.txt")
        self.command("git", "commit", "-qm", "published merge with resolution")
        merge = self.command("git", "rev-parse", "HEAD").stdout.strip()
        self.command("git", "checkout", "-q", self.initial_branch)
        upstream = self.write_and_commit("upstream.txt", "upstream\n", "new upstream")
        self.command("git", "checkout", "-q", "published")

        with chdir(self.repo):
            _base, commits = release.published_integration_range(merge, upstream)
            records = release.replay_published_integration_range(merge, upstream, return_records=True)
            output = release.git("rev-parse", "HEAD")
            release.validate_published_commit_preservation(commits, upstream, output, records=records)
            invalid_direct_records = [dict(record) for record in records]
            invalid_direct_records[0]["output_patch_id"] = "f" * 40
            with self.assertRaisesRegex(RuntimeError, r"not preserved by patch identity"):
                release.validate_published_commit_preservation(
                    commits, upstream, output, records=invalid_direct_records
                )

        self.assertEqual(commits, [direct, side, merge])
        self.assertEqual([record["source_commit"] for record in records], commits)
        self.assertEqual(records[-1]["status"], "applied_merge_mainline")
        self.assertEqual(records[-1]["mainline"], 1)
        self.assertEqual(self.command("git", "show", "HEAD:resolution.txt").stdout, "merge-only resolution\n")
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")

    def test_published_merge_already_represented_is_skipped_with_ledger_record(self) -> None:
        self.command("git", "checkout", "-qb", "published", self.base)
        direct = self.write_and_commit("direct.txt", "direct\n", "published direct")
        self.command("git", "checkout", "-qb", "published-side", self.base)
        side = self.write_and_commit("side.txt", "side\n", "published side")
        self.command("git", "checkout", "-q", "published")
        self.command("git", "merge", "-q", "--no-ff", "published-side", "-m", "published merge")
        merge = self.command("git", "rev-parse", "HEAD").stdout.strip()
        self.command("git", "checkout", "-q", self.initial_branch)
        upstream = self.write_and_commit("upstream.txt", "upstream\n", "new upstream")
        self.command("git", "checkout", "-q", "published")

        with chdir(self.repo):
            _base, commits = release.published_integration_range(merge, upstream)
            records = release.replay_published_integration_range(merge, upstream, return_records=True)
            output = release.git("rev-parse", "HEAD")
            release.validate_published_commit_preservation(commits, upstream, output, records=records)

        self.assertEqual(commits, [direct, side, merge])
        self.assertEqual(records[-1]["status"], "merge_delta_already_represented")
        self.assertEqual(records[-1]["output_commit"], output)
        self.assertNotEqual(self.command("git", "rev-parse", "--verify", "-q", "CHERRY_PICK_HEAD", check=False).returncode, 0)
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")

    def test_conflict_with_reconciliation_disabled_parks_and_continues(self) -> None:
        """Even with the resolver off, content never halts the rebase (2026-08-17)."""
        self.command("git", "checkout", "-qb", "published", self.base)
        conflict = self.write_and_commit("base.txt", "published\n", "direct conflicting user fix")
        published = self.command("git", "rev-parse", "HEAD").stdout.strip()
        self.command("git", "checkout", "-q", self.initial_branch)
        upstream = self.write_and_commit("base.txt", "upstream\n", "new upstream conflict")
        self.command("git", "checkout", "-q", "published")
        old_reconcile = release.IN_JOB_RECONCILIATION
        release.IN_JOB_RECONCILIATION = False
        try:
            with chdir(self.repo):
                records = release.replay_published_integration_range(published, upstream, return_records=True)
        finally:
            release.IN_JOB_RECONCILIATION = old_reconcile

        parked = [record for record in records if record["status"] == "parked_unresolved"]
        self.assertEqual([record["source_commit"] for record in parked], [conflict])
        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), upstream)
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")
        self.assertNotEqual(self.command("git", "rev-parse", "--verify", "-q", "CHERRY_PICK_HEAD", check=False).returncode, 0)
        ledger = json.loads((self.repo / ".git" / "parked-commits.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["entries"][0]["commit"], conflict)

    def _conflicting_published_range(self) -> tuple[str, str, str]:
        self.command("git", "checkout", "-qb", "published", self.base)
        conflict = self.write_and_commit("base.txt", "published\n", "direct conflicting user fix")
        published = self.command("git", "rev-parse", "HEAD").stdout.strip()
        self.command("git", "checkout", "-q", self.initial_branch)
        upstream = self.write_and_commit("base.txt", "upstream\n", "new upstream conflict")
        self.command("git", "checkout", "-q", "published")
        return conflict, published, upstream

    @contextmanager
    def _reconciliation(self, backend):
        old = (
            release.REVIEW_DIR, release.IN_JOB_RECONCILIATION,
            release.RESOLUTION_BACKEND, release.WORKTREE,
        )
        release.REVIEW_DIR = self.repo / ".git" / "reviews"
        release.IN_JOB_RECONCILIATION = True
        release.RESOLUTION_BACKEND = backend
        release.WORKTREE = self.repo
        try:
            with chdir(self.repo):
                yield
        finally:
            (
                release.REVIEW_DIR, release.IN_JOB_RECONCILIATION,
                release.RESOLUTION_BACKEND, release.WORKTREE,
            ) = old

    def test_git_retries_stale_index_lock_only_under_exclusive_lock(self) -> None:
        """A mid-run index.lock must not wedge replay or restoration."""
        stale = self.repo / ".git" / "index.lock"
        stale.write_text("", encoding="utf-8")
        release_lock = self.repo / ".git" / "release.lock"
        old_worktree, old_lock_path = release.WORKTREE, release.LOCK_PATH
        release.WORKTREE = self.repo
        release.LOCK_PATH = release_lock
        try:
            with self.assertRaisesRegex(RuntimeError, "index.lock"):
                release.git("reset", "--hard", "HEAD")
            self.assertTrue(stale.exists())
            with release.exclusive_lock("test-investigator"):
                release.git("reset", "--hard", "HEAD")
        finally:
            release.WORKTREE, release.LOCK_PATH = old_worktree, old_lock_path
        self.assertFalse(stale.exists())
        self.assertFalse(release_lock.exists())

    def test_batch_patch_ids_match_single_computation(self) -> None:
        """One pipeline must produce byte-identical identities to per-commit calls."""
        first = self.write_and_commit("a.txt", "alpha\n", "first change")
        second = self.write_and_commit("b.txt", "beta\n", "second change")
        self.command("git", "commit", "-q", "--allow-empty", "-m", "authored empty")
        empty = self.command("git", "rev-parse", "HEAD").stdout.strip()

        with chdir(self.repo):
            batch = release._batch_patch_ids([first, second, empty])
            self.assertEqual(batch[first], release.stable_patch_id(first))
            self.assertEqual(batch[second], release.stable_patch_id(second))
            self.assertIsNone(batch[empty])
            self.assertEqual(release._batch_patch_ids([]), {})

    def test_dead_holder_lock_is_reclaimed_immediately(self) -> None:
        """User directive 2026-08-17: dead PID = dead lock, no age grace."""
        recent = release.datetime.now(release.timezone.utc).isoformat()
        # PID 4_000_000 exceeds Windows' practical PID space: reliably dead.
        self.assertTrue(release._lock_is_reclaimable(
            {"holder": "scheduler", "pid": 4_000_000, "started_at": recent}
        ))
        # A lock without a provable owner is equally reclaimable.
        self.assertTrue(release._lock_is_reclaimable(
            {"holder": "unknown holder", "pid": None, "started_at": None}
        ))
        # A live holder is never reclaimed.
        self.assertFalse(release._lock_is_reclaimable(
            {"holder": "scheduler", "pid": os.getpid(), "started_at": recent}
        ))

    def test_claude_resolution_backend_exceeds_observed_turn_exhaustion(self) -> None:
        captured: list[tuple[tuple[str, ...], dict[str, object]]] = []

        def recording_run(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, stdout="resolved")

        release.run = recording_run
        try:
            result = release._claude_resolution_backend("resolve", ["base.txt"])
        finally:
            release.run = self.local_run

        self.assertEqual(result, "resolved")
        self.assertEqual(len(captured), 1)
        args, kwargs = captured[0]
        max_turns = int(args[args.index("--max-turns") + 1])
        self.assertGreater(max_turns, 25)
        self.assertGreaterEqual(int(str(kwargs["timeout"])), 1200)

    def test_replay_conflict_is_reconciled_in_job_and_recorded(self) -> None:
        conflict, published, upstream = self._conflicting_published_range()
        calls: list[list[str]] = []

        def backend(prompt: str, files: list[str]) -> str:
            calls.append(list(files))
            (self.repo / "base.txt").write_text("published\nupstream\n", encoding="utf-8")
            return "union resolution applied"

        with self._reconciliation(backend):
            records = release.replay_published_integration_range(published, upstream, return_records=True)

        self.assertEqual(calls, [["base.txt"]])
        resolved = [record for record in records if record["status"] == "applied_in_job_resolution"]
        self.assertEqual(len(resolved), 1)
        record = resolved[0]
        head = self.command("git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(record["source_commit"], conflict)
        self.assertEqual(record["conflicted_files"], ["base.txt"])
        self.assertEqual(record["output_commit"], head)
        self.assertNotEqual(head, published)
        self.assertEqual(self.command("git", "show", "HEAD:base.txt").stdout, "published\nupstream\n")
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")
        self.assertNotEqual(self.command("git", "rev-parse", "--verify", "-q", "CHERRY_PICK_HEAD", check=False).returncode, 0)
        artifacts = list((self.repo / ".git" / "reviews").glob("reconstruction-resolution-*.json"))
        self.assertEqual(len(artifacts), 1)
        payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "resolved_in_job")
        self.assertEqual(payload["source_commit"], conflict)
        self.assertEqual(payload["conflicted_files"], ["base.txt"])
        self.assertEqual(record["resolution_artifact"], str(artifacts[0]))
        # No human review request was written: the job proved the resolution.
        self.assertEqual(list((self.repo / ".git" / "reviews").glob("reconstruction-[0-9]*.json")), [])
        # Regression (landmine found 2026-08-17): the preservation validator
        # must accept the artifact-backed identity change instead of failing
        # the run after a successful in-job resolution.
        with chdir(self.repo):
            release.validate_published_commit_preservation(
                [conflict], upstream, head, records=records
            )

    def test_generated_file_conflict_is_preresolved_not_sent_to_backend(self) -> None:
        """uv.lock never reaches the resolver: base side taken, regen deferred."""
        # uv.lock must exist in the shared base: a real repo's lock file is
        # tracked history, so the conflict presents as both-modified (UU).
        self.write_and_commit("uv.lock", "base lock\n", "add base lock")
        lock_base = self.command("git", "rev-parse", "HEAD").stdout.strip()
        self.command("git", "checkout", "-qb", "published", lock_base)
        (self.repo / "uv.lock").write_text("fork lock\n", encoding="utf-8")
        (self.repo / "base.txt").write_text("published\n", encoding="utf-8")
        self.command("git", "add", "uv.lock", "base.txt")
        self.command("git", "commit", "-qm", "fork change with lock")
        conflict = self.command("git", "rev-parse", "HEAD").stdout.strip()
        published = conflict
        self.command("git", "checkout", "-q", self.initial_branch)
        (self.repo / "uv.lock").write_text("upstream lock\n", encoding="utf-8")
        (self.repo / "base.txt").write_text("upstream\n", encoding="utf-8")
        self.command("git", "add", "uv.lock", "base.txt")
        self.command("git", "commit", "-qm", "upstream change with lock")
        upstream = self.command("git", "rev-parse", "HEAD").stdout.strip()
        self.command("git", "checkout", "-q", "published")

        seen_by_backend: list[list[str]] = []

        def backend(prompt: str, files: list[str]) -> str:
            seen_by_backend.append(list(files))
            (self.repo / "base.txt").write_text("published\nupstream\n", encoding="utf-8")
            return "resolved editable only"

        old_regen = release.UV_LOCK_REGEN
        release.UV_LOCK_REGEN = False
        try:
            with self._reconciliation(backend):
                records = release.replay_published_integration_range(published, upstream, return_records=True)
        finally:
            release.UV_LOCK_REGEN = old_regen

        self.assertEqual(seen_by_backend, [["base.txt"]])
        resolved = [record for record in records if record["status"] == "applied_in_job_resolution"]
        self.assertEqual(len(resolved), 1)
        # The generated file carries the reconstruction base's (upstream) side.
        self.assertEqual(self.command("git", "show", "HEAD:uv.lock").stdout, "upstream lock\n")
        self.assertEqual(self.command("git", "show", "HEAD:base.txt").stdout, "published\nupstream\n")
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")
        artifacts = list((self.repo / ".git" / "reviews").glob("reconstruction-resolution-*.json"))
        payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["generated_files"], ["uv.lock"])

    def test_unresolvable_conflict_parks_and_replay_continues(self) -> None:
        """Content never halts the rebase: the commit parks, the run goes on."""
        conflict, published, upstream = self._conflicting_published_range()
        old_parked = release.PARKED_COMMITS_PATH
        release.PARKED_COMMITS_PATH = self.repo / ".git" / "parked-commits.json"
        try:
            with self._reconciliation(lambda _prompt, _files: "refused"):
                records = release.replay_published_integration_range(published, upstream, return_records=True)
        finally:
            parked_path = release.PARKED_COMMITS_PATH
            release.PARKED_COMMITS_PATH = old_parked

        parked = [record for record in records if record["status"] == "parked_unresolved"]
        self.assertEqual(len(parked), 1)
        self.assertEqual(parked[0]["source_commit"], conflict)
        # The rebase moved on: HEAD is the upstream reconstruction, clean.
        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), upstream)
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")
        # The object is pinned and the ledger carries an open retry entry.
        self.assertEqual(
            self.command("git", "rev-parse", parked[0]["keep_ref"]).stdout.strip(), conflict
        )
        ledger = json.loads(parked_path.read_text(encoding="utf-8"))
        self.assertEqual(ledger["entries"][0]["commit"], conflict)
        self.assertEqual(ledger["entries"][0]["status"], "open")
        # The preservation validator accepts the parked record as evidence.
        with chdir(self.repo):
            release.validate_published_commit_preservation([conflict], upstream, upstream, records=records)

    def test_agent_resolved_parked_commit_is_applied_on_retry(self) -> None:
        """An agent's out-of-band resolution in the ledger wins the retry."""
        self.command("git", "checkout", "-qb", "agent-side", self.base)
        resolved = self.write_and_commit("agent.txt", "agent resolution\n", "agent resolved fix")
        self.command("git", "checkout", "-q", self.initial_branch)
        old_parked = release.PARKED_COMMITS_PATH
        release.PARKED_COMMITS_PATH = self.repo / ".git" / "parked-commits.json"
        release.PARKED_COMMITS_PATH.parent.mkdir(parents=True, exist_ok=True)
        release.PARKED_COMMITS_PATH.write_text(json.dumps({"schema": 1, "entries": [{
            "commit": "0" * 40, "subject": "original parked", "attempts": 1,
            "keep_ref": "refs/pinned/parked/000000000000", "status": "open",
            "resolved_commit": resolved,
        }]}), encoding="utf-8")
        try:
            with self._reconciliation(lambda _prompt, _files: "unused"):
                records = release.retry_parked_commits()
        finally:
            parked_path = release.PARKED_COMMITS_PATH
            release.PARKED_COMMITS_PATH = old_parked

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "applied_from_parked")
        self.assertEqual(self.command("git", "show", "HEAD:agent.txt").stdout, "agent resolution\n")
        ledger = json.loads(parked_path.read_text(encoding="utf-8"))
        self.assertEqual(ledger["entries"][0]["status"], "applied")

    def test_upstream_pin_liveness_rules(self) -> None:
        """File pin: honored while fresh, ignored+removed when expired."""
        tip = self.base
        pinned = self.write_and_commit("pin.txt", "pin\n", "pinned base commit")
        old_pin_path = release.UPSTREAM_PIN_PATH
        release.UPSTREAM_PIN_PATH = self.repo / ".git" / "upstream-pin.json"
        try:
            with chdir(self.repo):
                # Fresh pin wins over the tip.
                release.UPSTREAM_PIN_PATH.write_text(json.dumps({
                    "sha": pinned,
                    "expires_at": (release.datetime.now(release.timezone.utc)
                                   + __import__("datetime").timedelta(hours=1)).isoformat(),
                }), encoding="utf-8")
                self.assertEqual(release._resolve_upstream_base(tip), pinned)
                # Expired pin is ignored AND removed (self-cleaning).
                release.UPSTREAM_PIN_PATH.write_text(json.dumps({
                    "sha": pinned,
                    "expires_at": (release.datetime.now(release.timezone.utc)
                                   - __import__("datetime").timedelta(hours=1)).isoformat(),
                }), encoding="utf-8")
                self.assertEqual(release._resolve_upstream_base(tip), tip)
                self.assertFalse(release.UPSTREAM_PIN_PATH.exists())
                # A successful publish consumes a live pin.
                release.UPSTREAM_PIN_PATH.write_text(json.dumps({"sha": pinned}), encoding="utf-8")
                release._consume_upstream_pin_on_publish()
                self.assertFalse(release.UPSTREAM_PIN_PATH.exists())
        finally:
            release.UPSTREAM_PIN_PATH = old_pin_path

    def test_ntfs_case_phantoms_are_tolerated_not_dirt(self) -> None:
        """Two index entries differing only by case can never both exist on
        NTFS; the perpetual 'modified' loser is a phantom (2026-08-17 11:39)."""
        blob = subprocess.run(
            ("git", "hash-object", "-w", "--stdin"), cwd=self.repo, input="x\n",
            text=True, capture_output=True,
        ).stdout.strip()
        self.command("git", "update-index", "--add", "--cacheinfo", f"100644,{blob},Case.txt")
        self.command("git", "update-index", "--add", "--cacheinfo", f"100644,{blob},case.txt")
        old_worktree = release.WORKTREE
        release.WORKTREE = self.repo
        try:
            with chdir(self.repo):
                self.assertTrue(self.command("git", "status", "--porcelain").stdout.strip())
                self.assertEqual(release._real_dirt(
                    self.command("git", "status", "--porcelain").stdout.splitlines()
                ), [])
                release._ensure_pristine_tree("phantom-test")  # must not raise
        finally:
            release.WORKTREE = old_worktree

    def test_pristine_tree_self_heals_external_interference(self) -> None:
        """Mid-run deletion/edit of OUR files is interference to restore, not
        a run-killer (three live sightings on contributors/emails 2026-08-17)."""
        (self.repo / "base.txt").unlink()
        (self.repo / "debris.txt").write_text("junk\n", encoding="utf-8")
        old_worktree = release.WORKTREE
        release.WORKTREE = self.repo
        try:
            with chdir(self.repo):
                release._ensure_pristine_tree("test")
        finally:
            release.WORKTREE = old_worktree
        self.assertEqual((self.repo / "base.txt").read_text(encoding="utf-8"), "base\n")
        self.assertFalse((self.repo / "debris.txt").exists())
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")

    def test_applied_record_with_context_drifted_patch_id_passes_preservation(self) -> None:
        """A clean pick whose patch-id drifted from neighboring resolutions is
        preserved by construction, never a validation failure (2026-08-17 09:32)."""
        self.command("git", "checkout", "-qb", "published", self.base)
        commit = self.write_and_commit("drift.txt", "change\n", "clean pick with drifting context")
        self.command("git", "checkout", "-q", self.initial_branch)
        upstream = self.command("git", "rev-parse", "HEAD").stdout.strip()
        records = [{
            "kind": "published", "status": "applied", "source_commit": commit,
            "output_commit": upstream, "output_patch_id": "0" * 40,
        }]
        with chdir(self.repo):
            release.validate_published_commit_preservation([commit], upstream, upstream, records=records)

    def test_cached_resolution_replays_without_backend(self) -> None:
        """A failed run's proven resolution is reused, never re-derived."""
        conflict, published, upstream = self._conflicting_published_range()
        # Simulate the prior attempt's resolved output: upstream + union.
        self.command("git", "checkout", "-q", upstream)
        resolved = self.write_and_commit("base.txt", "published\nupstream\n", "direct conflicting user fix")
        self.command("git", "checkout", "-q", "published")
        release.RESOLUTION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        release.RESOLUTION_CACHE_PATH.write_text(json.dumps({
            conflict: {"resolved_commit": resolved, "keep_ref": "refs/pinned/resolved/test"}
        }), encoding="utf-8")

        def backend(_prompt: str, _files: list[str]) -> str:
            raise AssertionError("cache hit must not invoke the backend")

        with self._reconciliation(backend):
            records = release.replay_published_integration_range(published, upstream, return_records=True)

        resolved_records = [r for r in records if r["status"] == "applied_in_job_resolution"]
        self.assertEqual(len(resolved_records), 1)
        self.assertEqual(self.command("git", "show", "HEAD:base.txt").stdout, "published\nupstream\n")
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")

    def test_successful_resolution_is_recorded_in_cache(self) -> None:
        conflict, published, upstream = self._conflicting_published_range()

        def backend(prompt: str, files: list[str]) -> str:
            (self.repo / "base.txt").write_text("published\nupstream\n", encoding="utf-8")
            return "resolved"

        with self._reconciliation(backend):
            release.replay_published_integration_range(published, upstream, return_records=True)

        cache = json.loads(release.RESOLUTION_CACHE_PATH.read_text(encoding="utf-8"))
        self.assertIn(conflict, cache)
        head = self.command("git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(cache[conflict]["resolved_commit"], head)

    def test_resolution_collapsing_to_empty_records_already_present(self) -> None:
        """A union that equals HEAD is presence, not failure (2026-08-17 05:52)."""
        conflict, published, upstream = self._conflicting_published_range()

        def backend(prompt: str, files: list[str]) -> str:
            # Resolve by taking exactly the upstream side: the pick collapses
            # to an empty delta.
            (self.repo / "base.txt").write_text("upstream\n", encoding="utf-8")
            return "took upstream side"

        with self._reconciliation(backend):
            records = release.replay_published_integration_range(published, upstream, return_records=True)

        statuses = [record["status"] for record in records]
        self.assertEqual(statuses, ["resolved_as_already_present"])
        self.assertEqual(records[0]["source_commit"], conflict)
        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), upstream)
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")
        with chdir(self.repo):
            release.validate_published_commit_preservation([conflict], upstream, upstream, records=records)

    def test_replay_conflict_backend_leaving_markers_parks_the_commit(self) -> None:
        """An unprovable resolution parks the commit; the rebase never stops."""
        conflict, published, upstream = self._conflicting_published_range()

        with self._reconciliation(lambda prompt, files: "changed nothing"):
            records = release.replay_published_integration_range(published, upstream, return_records=True)

        parked = [record for record in records if record["status"] == "parked_unresolved"]
        self.assertEqual([record["source_commit"] for record in parked], [conflict])
        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), upstream)
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")
        self.assertNotEqual(self.command("git", "rev-parse", "--verify", "-q", "CHERRY_PICK_HEAD", check=False).returncode, 0)

    def test_replay_conflict_backend_touching_extra_files_parks_and_scrubs(self) -> None:
        """Backend overreach parks the commit AND its debris never survives."""
        conflict, published, upstream = self._conflicting_published_range()

        def backend(prompt: str, files: list[str]) -> str:
            (self.repo / "base.txt").write_text("published\nupstream\n", encoding="utf-8")
            (self.repo / "rogue.txt").write_text("overreach\n", encoding="utf-8")
            return "overreached"

        with self._reconciliation(backend):
            records = release.replay_published_integration_range(published, upstream, return_records=True)

        parked = [record for record in records if record["status"] == "parked_unresolved"]
        self.assertEqual([record["source_commit"] for record in parked], [conflict])
        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), upstream)
        self.assertFalse((self.repo / "rogue.txt").exists())
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")

    def test_merge_conflict_restoration_clears_index_and_worktree(self) -> None:
        self.command("git", "checkout", "-qb", "published", self.base)
        self.command("git", "checkout", "-qb", "published-side", self.base)
        side = self.write_and_commit("side.txt", "side\n", "published side")
        self.command("git", "checkout", "-q", "published")
        self.command("git", "merge", "-q", "--no-ff", "--no-commit", "published-side")
        (self.repo / "resolution.txt").write_text("published merge resolution\n", encoding="utf-8")
        self.command("git", "add", "resolution.txt")
        self.command("git", "commit", "-qm", "published merge resolution")
        published = self.command("git", "rev-parse", "HEAD").stdout.strip()
        self.command("git", "checkout", "-q", self.initial_branch)
        upstream = self.write_and_commit("resolution.txt", "upstream resolution\n", "upstream resolution")
        self.command("git", "checkout", "-q", "published")
        old_reconcile = release.IN_JOB_RECONCILIATION
        release.IN_JOB_RECONCILIATION = False
        try:
            with chdir(self.repo):
                records = release.replay_published_integration_range(published, upstream, return_records=True)
        finally:
            release.IN_JOB_RECONCILIATION = old_reconcile

        # The side-parent commit replays; the conflicted merge delta parks
        # with its mainline bookkeeping intact; the rebase finishes clean.
        parked = [record for record in records if record["status"] == "parked_unresolved"]
        self.assertEqual([record["source_commit"] for record in parked], [published])
        self.assertEqual(parked[0]["mainline"], 1)
        self.assertEqual(self.command("git", "show", "HEAD:side.txt").stdout, "side\n")
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")
        self.assertNotEqual(self.command("git", "rev-parse", "--verify", "-q", "CHERRY_PICK_HEAD", check=False).returncode, 0)

    def test_clean_reconstruction_failure_writes_a_two_reviewer_brief(self) -> None:
        old_review_dir = release.REVIEW_DIR
        release.REVIEW_DIR = self.repo / "reviews"
        failed = {"commit": "deadbeef1234", "subject": "failed patch"}
        try:
            request = release.write_reconstruction_review_request("upstream-sha", failed, RuntimeError("conflict"))
        finally:
            release.REVIEW_DIR = old_review_dir
        payload = json.loads(request.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "review_required")
        self.assertEqual(payload["failed_patch"], failed)
        self.assertIn("two independent reviews", payload["reviewer_instruction"])
        self.assertIn("--model opus", payload["suggested_claude_command"])

    def test_required_components_allow_extra_direct_published_commit(self) -> None:
        self.command("git", "checkout", "-qb", "components")
        required = self.write_and_commit("required.txt", "required\n", "required component")
        direct = self.write_and_commit("direct.txt", "direct\n", "direct user fix")
        required_patch = {
            "commit": required,
            "subject": "required component",
            "stable_patch_id": release.stable_patch_id(required),
        }
        old_required = release.REQUIRED_PATCHES
        release.REQUIRED_PATCHES = [required_patch]
        try:
            with chdir(self.repo):
                release.validate_required_components(self.base, direct)
        finally:
            release.REQUIRED_PATCHES = old_required

    def test_required_components_reject_missing_component(self) -> None:
        required = self.write_and_commit("required.txt", "required\n", "required component")
        required_patch = {
            "commit": required,
            "subject": "required component",
            "stable_patch_id": release.stable_patch_id(required),
        }
        old_required = release.REQUIRED_PATCHES
        release.REQUIRED_PATCHES = [required_patch]
        try:
            with chdir(self.repo), self.assertRaisesRegex(RuntimeError, r"missing required component"):
                release.validate_required_components(self.base, self.base)
        finally:
            release.REQUIRED_PATCHES = old_required

    def test_required_component_absorbed_by_upstream_passes(self) -> None:
        self.command("git", "checkout", "-qb", "source")
        source = self.write_and_commit("required.txt", "required\n", "required component")
        patch = {"commit": source, "subject": "required component", "stable_patch_id": release.stable_patch_id(source)}
        self.command("git", "checkout", "-q", self.initial_branch)
        self.command("git", "cherry-pick", source)
        upstream = self.command("git", "rev-parse", "HEAD").stdout.strip()
        old_required = release.REQUIRED_PATCHES
        release.REQUIRED_PATCHES = [patch]
        try:
            with chdir(self.repo):
                release.validate_required_components(upstream, upstream)
        finally:
            release.REQUIRED_PATCHES = old_required

    def test_required_component_same_subject_non_equivalent_fails(self) -> None:
        self.command("git", "checkout", "-qb", "source")
        source = self.write_and_commit("required.txt", "required\n", "required component")
        patch = {"commit": source, "subject": "required component", "stable_patch_id": release.stable_patch_id(source)}
        self.command("git", "checkout", "-q", self.initial_branch)
        upstream = self.write_and_commit("other.txt", "different\n", "required component")
        old_required = release.REQUIRED_PATCHES
        release.REQUIRED_PATCHES = [patch]
        try:
            with chdir(self.repo), self.assertRaisesRegex(RuntimeError, r"same-subject but non-equivalent"):
                release.validate_required_components(upstream, upstream)
        finally:
            release.REQUIRED_PATCHES = old_required

    def test_required_component_rewritten_patch_equivalent_is_accepted(self) -> None:
        self.command("git", "checkout", "-qb", "source")
        source = self.write_and_commit("required.txt", "required\n", "required component")
        patch = {"commit": source, "subject": "required component", "stable_patch_id": release.stable_patch_id(source)}
        self.command("git", "checkout", "-q", self.initial_branch)
        equivalent = self.write_and_commit("required.txt", "required\n", "rewritten integration commit")
        old_required = release.REQUIRED_PATCHES
        release.REQUIRED_PATCHES = [patch]
        try:
            with chdir(self.repo):
                release.validate_required_components(self.base, equivalent)
        finally:
            release.REQUIRED_PATCHES = old_required

    def test_required_component_reviewed_output_identity_is_accepted(self) -> None:
        self.command("git", "checkout", "-qb", "source")
        source = self.write_and_commit("required.txt", "required\n", "required component")
        patch = {
            "commit": source,
            "subject": "required component",
            "stable_patch_id": release.stable_patch_id(source),
        }
        self.command("git", "checkout", "-q", self.initial_branch)
        reviewed = self.write_and_commit("required.txt", "required after conflict resolution\n", "required component")
        patch["accepted_output_patch_ids"] = [release.stable_patch_id(reviewed)]
        old_required = release.REQUIRED_PATCHES
        release.REQUIRED_PATCHES = [patch]
        try:
            with chdir(self.repo):
                release.validate_required_components(self.base, reviewed)
        finally:
            release.REQUIRED_PATCHES = old_required

    def test_patch_resolution_accepts_reviewed_conflict_identity(self) -> None:
        self.command("git", "checkout", "-qb", "source-reviewed")
        source = self.write_and_commit("reviewed.txt", "source\n", "reviewed required component")
        self.command("git", "checkout", "-q", self.initial_branch)
        reviewed = self.write_and_commit("reviewed.txt", "reviewed resolution\n", "reviewed required component")
        patch = {
            "commit": source,
            "subject": "reviewed required component",
            "stable_patch_id": release.stable_patch_id(source),
            "accepted_output_patch_ids": [release.stable_patch_id(reviewed)],
        }
        with chdir(self.repo):
            to_apply, represented = release.patch_resolution(reviewed, [patch])
        self.assertEqual(to_apply, [])
        self.assertEqual(represented[0]["upstream_commit"], reviewed)
        self.assertEqual(represented[0]["output_patch_id"], patch["accepted_output_patch_ids"][0])

    def test_patch_resolution_defers_same_subject_candidate_to_reviewed_replacement(self) -> None:
        self.command("git", "checkout", "-qb", "source-reviewed")
        source = self.write_and_commit("source.txt", "source\n", "required foundation")
        source_patch_id = release.stable_patch_id(source)
        self.command("git", "checkout", "-q", self.initial_branch)
        upstream = self.write_and_commit("upstream.txt", "different\n", "required foundation")
        self.command("git", "checkout", "-qb", "reviewed-replacement", self.initial_branch)
        replacement = self.write_and_commit("replacement.txt", "reviewed\n", "reviewed replacement")
        replacement_patch_id = release.stable_patch_id(replacement)
        self.command("git", "checkout", "-q", self.initial_branch)
        patch = {
            "commit": source,
            "subject": "required foundation",
            "stable_patch_id": source_patch_id,
            "accepted_output_patch_ids": [replacement_patch_id],
            "reviewed_replacement": {
                "commit": replacement,
                "stable_patch_id": replacement_patch_id,
            },
        }

        with chdir(self.repo):
            to_apply, absorbed = release.patch_resolution(upstream, [patch])

        self.assertEqual(to_apply, [patch])
        self.assertEqual(absorbed, [])

    def test_patch_resolution_accepts_reviewed_record_with_rewritten_subject(self) -> None:
        self.command("git", "checkout", "-qb", "source-recorded")
        source = self.write_and_commit("recorded.txt", "source\n", "source subject")
        self.command("git", "checkout", "-q", self.initial_branch)
        reviewed = self.write_and_commit("recorded.txt", "reviewed resolution\n", "rewritten subject")
        reviewed_patch_id = release.stable_patch_id(reviewed)
        patch = {
            "commit": source,
            "subject": "source subject",
            "stable_patch_id": release.stable_patch_id(source),
            "accepted_output_patch_ids": [reviewed_patch_id],
        }
        records = [{
            "kind": "published", "status": "exact_reachable", "source_commit": reviewed,
            "output_commit": reviewed, "output_patch_id": reviewed_patch_id,
        }]
        with chdir(self.repo):
            to_apply, represented = release.patch_resolution(reviewed, [patch], records=records)
        self.assertEqual(to_apply, [])
        self.assertEqual(represented[0]["upstream_commit"], reviewed)
        self.assertEqual(represented[0]["output_patch_id"], reviewed_patch_id)

    def test_required_patch_application_records_source_to_output(self) -> None:
        self.command("git", "checkout", "-qb", "required-source")
        foundation = self.write_and_commit("foundation.txt", "foundation\n", "required foundation")
        component = self.write_and_commit("component.txt", "component\n", "required component")
        self.command("git", "checkout", "-q", self.initial_branch)
        published = self.command("git", "rev-parse", "HEAD").stdout.strip()
        patches = [
            {"commit": foundation, "subject": "required foundation", "stable_patch_id": release.stable_patch_id(foundation)},
            {"commit": component, "subject": "required component", "stable_patch_id": release.stable_patch_id(component)},
        ]
        with chdir(self.repo):
            records = release.apply_required_patches(
                patches, published_input_head=published, upstream_head=published, kind="required"
            )
        self.assertEqual([record["source_commit"] for record in records], [foundation, component])
        self.assertTrue(all(record["status"] == "applied" for record in records))
        self.assertEqual(self.command("git", "show", "HEAD:foundation.txt").stdout, "foundation\n")
        self.assertEqual(self.command("git", "show", "HEAD:component.txt").stdout, "component\n")

    def test_required_patch_conflict_is_reconciled_in_job_despite_unapproved_identity(self) -> None:
        """A pin conflict resolves in-job; the artifact stands in for identity approval."""
        self.command("git", "checkout", "-qb", "required-source")
        source = self.write_and_commit("base.txt", "required fix\n", "required component fix")
        patch_id = release.stable_patch_id(source)
        self.command("git", "checkout", "-q", self.initial_branch)
        published = self.write_and_commit("base.txt", "upstream drift\n", "conflicting upstream drift")
        patch = {"commit": source, "subject": "required component fix", "stable_patch_id": patch_id}

        def backend(prompt: str, files: list[str]) -> str:
            (self.repo / "base.txt").write_text("upstream drift\nrequired fix\n", encoding="utf-8")
            return "union resolution"

        with self._reconciliation(backend):
            records = release.apply_required_patches(
                [patch], published_input_head=published, upstream_head=published, kind="component"
            )

        self.assertEqual(len(records), 1)
        record = records[0]
        head = self.command("git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(record["status"], "applied_in_job_resolution")
        self.assertEqual(record["kind"], "component")
        self.assertEqual(record["source_commit"], source)
        self.assertEqual(record["output_commit"], head)
        self.assertEqual(record["conflicted_files"], ["base.txt"])
        # The resolved identity is intentionally NOT the approved patch-id.
        self.assertNotEqual(record["output_patch_id"], patch_id)
        self.assertEqual(self.command("git", "show", "HEAD:base.txt").stdout, "upstream drift\nrequired fix\n")
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")
        artifacts = list((self.repo / ".git" / "reviews").glob("reconstruction-resolution-*.json"))
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(json.loads(artifacts[0].read_text(encoding="utf-8"))["kind"], "component")
        # Regression (landmine found 2026-08-17): the required-record
        # validator must accept the artifact-backed identity instead of
        # rejecting it as unapproved after a successful in-job resolution.
        with chdir(self.repo):
            release._validate_required_records([patch], head, records, "component")

    def test_required_patch_conflict_parks_pin_when_backend_refuses(self) -> None:
        """An unresolvable pin parks and the reconstruction continues (2026-08-17)."""
        self.command("git", "checkout", "-qb", "required-source")
        source = self.write_and_commit("base.txt", "required fix\n", "required component fix")
        patch_id = release.stable_patch_id(source)
        self.command("git", "checkout", "-q", self.initial_branch)
        published = self.write_and_commit("base.txt", "upstream drift\n", "conflicting upstream drift")
        patch = {"commit": source, "subject": "required component fix", "stable_patch_id": patch_id}

        dispatched: list[str] = []
        old_launch = release.launch_failure_investigator
        release.launch_failure_investigator = lambda **kwargs: dispatched.append(kwargs.get("stage", ""))
        try:
            with self._reconciliation(lambda _prompt, _files: "changed nothing"):
                records = release.apply_required_patches(
                    [patch], published_input_head=published, upstream_head=published, kind="component"
                )
        finally:
            release.launch_failure_investigator = old_launch

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "parked_unresolved")
        self.assertEqual(records[0]["source_commit"], source)
        self.assertEqual(dispatched, ["parked_pin_resolution"])
        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), published)
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")
        # The required-record validator accepts the parked record.
        with chdir(self.repo):
            release._validate_required_records([patch], published, records, "component")

    def test_required_patch_already_present_as_an_empty_cherry_pick_is_recorded(self) -> None:
        """A clean empty pick proves the required patch is already in the target tree."""
        self.command("git", "checkout", "-qb", "required-source")
        source = self.write_and_commit("base.txt", "required\n", "required component")
        patch_id = release.stable_patch_id(source)
        self.command("git", "checkout", "-q", self.initial_branch)
        published = self.write_and_commit("base.txt", "required\n", "equivalent upstream change")
        patch = {
            "commit": source,
            "subject": "required component",
            "stable_patch_id": patch_id,
        }

        with chdir(self.repo):
            records = release.apply_required_patches(
                [patch], published_input_head=published, upstream_head=published, kind="component"
            )

        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), published)
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")
        self.assertNotEqual(
            self.command("git", "rev-parse", "--verify", "-q", "CHERRY_PICK_HEAD", check=False).returncode,
            0,
        )
        self.assertEqual(records, [{
            "kind": "component",
            "status": "already_present_after_empty_cherry_pick",
            "source_commit": source,
            "output_commit": published,
            "output_patch_id": patch_id,
        }])

    def test_reviewed_foundation_replacement_applies_instead_of_conflicting_source(self) -> None:
        self.command("git", "checkout", "-qb", "foundation-conflict-source", self.base)
        source = self.write_and_commit("base.txt", "foundation source\n", "conflicting pinned foundation")
        source_patch_id = release.stable_patch_id(source)
        self.command("git", "checkout", "-qb", "reviewed-foundation-replacement", self.base)
        replacement = self.write_and_commit(
            "replacement.txt", "reviewed resolution\n", "reviewed foundation replacement"
        )
        replacement_patch_id = release.stable_patch_id(replacement)
        self.command("git", "checkout", "-q", self.initial_branch)
        upstream = self.write_and_commit("base.txt", "new upstream\n", "upstream conflict")
        patch = {
            "commit": source,
            "subject": "conflicting pinned foundation",
            "stable_patch_id": source_patch_id,
            "accepted_output_patch_ids": [replacement_patch_id],
            "reviewed_replacement": {
                "commit": replacement,
                "stable_patch_id": replacement_patch_id,
            },
        }

        with chdir(self.repo):
            records = release.apply_required_patches(
                [patch], published_input_head=upstream, upstream_head=upstream, kind="foundation"
            )

        self.assertEqual(self.command("git", "show", "HEAD:base.txt").stdout, "new upstream\n")
        self.assertEqual(self.command("git", "show", "HEAD:replacement.txt").stdout, "reviewed resolution\n")
        self.assertEqual(records, [{
            "kind": "foundation",
            "status": "applied_reviewed_replacement",
            "source_commit": source,
            "applied_commit": replacement,
            "output_commit": self.command("git", "rev-parse", "HEAD").stdout.strip(),
            "output_patch_id": replacement_patch_id,
        }])

    def test_reviewed_foundation_replacement_verification_fails_closed(self) -> None:
        replacement = self.write_and_commit(
            "replacement.txt", "reviewed resolution\n", "reviewed foundation replacement"
        )
        replacement_patch_id = release.stable_patch_id(replacement)
        old_manifest, old_foundations = release.MANIFEST, release.UPSTREAM_FOUNDATIONS
        release.MANIFEST = {"components": []}
        try:
            cases = (
                ("f" * 40, replacement_patch_id, r"replacement patch is unavailable"),
                (replacement, "e" * 40, r"replacement patch identity changed"),
            )
            for commit, patch_id, message in cases:
                with self.subTest(commit=commit, patch_id=patch_id):
                    release.UPSTREAM_FOUNDATIONS = [{
                        "id": "foundation",
                        "patches": [{
                            "commit": "a" * 40,
                            "stable_patch_id": "b" * 40,
                            "subject": "foundation",
                            "accepted_output_patch_ids": [patch_id],
                            "reviewed_replacement": {
                                "commit": commit,
                                "stable_patch_id": patch_id,
                            },
                        }],
                    }]
                    with chdir(self.repo), self.assertRaisesRegex(RuntimeError, message):
                        release.verify_manifest_sources()
        finally:
            release.MANIFEST, release.UPSTREAM_FOUNDATIONS = old_manifest, old_foundations

    def test_later_component_duplicate_is_absorbed_by_applied_foundation_replacement(self) -> None:
        self.command("git", "checkout", "-qb", "foundation-source", self.base)
        source = self.write_and_commit("source.txt", "foundation source\n", "pinned foundation")
        self.command("git", "checkout", "-qb", "reviewed-replacement", self.base)
        replacement = self.write_and_commit("replacement.txt", "reviewed\n", "reviewed replacement")
        replacement_patch_id = release.stable_patch_id(replacement)
        self.command("git", "checkout", "-q", self.initial_branch)
        patch = {
            "commit": source,
            "subject": "pinned foundation",
            "stable_patch_id": release.stable_patch_id(source),
            "accepted_output_patch_ids": [replacement_patch_id],
            "reviewed_replacement": {
                "commit": replacement,
                "stable_patch_id": replacement_patch_id,
            },
        }
        component = {
            "commit": replacement,
            "subject": "reviewed replacement",
            "stable_patch_id": replacement_patch_id,
        }
        with chdir(self.repo):
            foundation_records = release.apply_required_patches(
                [patch], published_input_head=self.base, upstream_head=self.base, kind="foundation"
            )
            output = release.git("rev-parse", "HEAD")
            to_apply, absorbed = release.patch_resolution(output, [component], records=foundation_records)

        self.assertEqual(to_apply, [])
        self.assertEqual(len(absorbed), 1)
        self.assertEqual(absorbed[0]["commit"], replacement)
        self.assertEqual(absorbed[0]["upstream_commit"], foundation_records[0]["output_commit"])
        self.assertEqual(absorbed[0]["output_patch_id"], replacement_patch_id)

    def test_required_foundation_conflict_parks_and_continues(self) -> None:
        """Foundation pins follow the same park-not-stop directive (2026-08-17)."""
        self.command("git", "checkout", "-qb", "required-conflict-source")
        source = self.write_and_commit("base.txt", "required\n", "conflicting required foundation")
        self.command("git", "checkout", "-q", self.initial_branch)
        published = self.write_and_commit("base.txt", "published\n", "published direct change")
        patch = {"commit": source, "subject": "conflicting required foundation", "stable_patch_id": release.stable_patch_id(source)}
        old_reconcile = release.IN_JOB_RECONCILIATION
        release.IN_JOB_RECONCILIATION = False
        try:
            with chdir(self.repo):
                records = release.apply_required_patches(
                    [patch], published_input_head=published, upstream_head=published, kind="foundation"
                )
        finally:
            release.IN_JOB_RECONCILIATION = old_reconcile
        self.assertEqual([record["status"] for record in records], ["parked_unresolved"])
        self.assertEqual(records[0]["kind"], "foundation")
        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), published)
        self.assertNotEqual(self.command("git", "rev-parse", "--verify", "-q", "CHERRY_PICK_HEAD", check=False).returncode, 0)
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")

    def test_reviewed_component_replacement_bypasses_source_conflict(self) -> None:
        self.command("git", "checkout", "-qb", "component-conflict-source")
        (self.repo / "base.txt").write_text("component source\n", encoding="utf-8")
        (self.repo / "component.txt").write_text("component payload\n", encoding="utf-8")
        self.command("git", "add", "base.txt", "component.txt")
        self.command("git", "commit", "-qm", "conflicting required component")
        source = self.command("git", "rev-parse", "HEAD").stdout.strip()
        self.command("git", "checkout", "-q", self.initial_branch)
        published = self.write_and_commit("base.txt", "upstream reviewed value\n", "upstream conflict")

        self.command("git", "checkout", "-qb", "reviewed-component-output", published)
        reviewed_output = self.write_and_commit(
            "component.txt", "component payload\n", "reviewed component replacement"
        )
        reviewed_patch_id = release.stable_patch_id(reviewed_output)
        self.command("git", "checkout", "-q", self.initial_branch)
        self.command("git", "reset", "--hard", published)

        patch = {
            "commit": source,
            "subject": "conflicting required component",
            "stable_patch_id": release.stable_patch_id(source),
            "accepted_output_patch_ids": [reviewed_patch_id],
            "reviewed_replacement": {
                "commit": reviewed_output,
                "stable_patch_id": reviewed_patch_id,
            },
        }
        with chdir(self.repo):
            records = release.apply_required_patches(
                [patch], published_input_head=published, upstream_head=published, kind="component"
            )

        self.assertNotEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), published)
        self.assertEqual(self.command("git", "show", "HEAD:base.txt").stdout, "upstream reviewed value\n")
        self.assertEqual(self.command("git", "show", "HEAD:component.txt").stdout, "component payload\n")
        self.assertEqual(records[0]["status"], "applied_reviewed_replacement")
        self.assertEqual(records[0]["applied_commit"], reviewed_output)
        self.assertEqual(records[0]["output_patch_id"], reviewed_patch_id)

    def test_bootstrap_repository_pin_declares_the_reviewed_replacement(self) -> None:
        component = next(
            item for item in release.MANIFEST["components"]
            if item["id"] == "codex-responses-native-compaction-rebased"
        )
        patch = next(
            item for item in component["patches"]
            if item["commit"] == "adafd77fa07f68dcaf58d9b28466c0bdd4cb115c"
        )

        self.assertEqual(patch["reviewed_replacement"], {
            "commit": "403e7dad2183c50c187210d2a4aeefb8bb77fabf",
            "stable_patch_id": "344c9a5942f28cc1d5367ada2a91a9a77ede6f0e",
            "source_ref": "fork/reconcile/fork-integration-20260814",
        })
        self.assertIn("344c9a5942f28cc1d5367ada2a91a9a77ede6f0e", patch["accepted_output_patch_ids"])

    def test_reviewed_component_replacement_already_applied_by_prior_component_is_recorded(self) -> None:
        """One reviewed commit may prove two ordered manifest components."""
        self.command("git", "checkout", "-qb", "component-conflict-source")
        source = self.write_and_commit("base.txt", "component source\n", "conflicting required component")
        self.command("git", "checkout", "-q", self.initial_branch)
        published = self.write_and_commit("base.txt", "upstream value\n", "upstream conflict")
        self.command("git", "checkout", "-qb", "reviewed-component-output", published)
        replacement = self.write_and_commit("component.txt", "reviewed payload\n", "reviewed component replacement")
        replacement_patch_id = release.stable_patch_id(replacement)
        self.command("git", "checkout", "-q", self.initial_branch)
        self.command("git", "reset", "--hard", published)

        patches = [
            {
                "commit": replacement,
                "subject": "reviewed component replacement",
                "stable_patch_id": replacement_patch_id,
            },
            {
                "commit": source,
                "subject": "conflicting required component",
                "stable_patch_id": release.stable_patch_id(source),
                "accepted_output_patch_ids": [replacement_patch_id],
                "reviewed_replacement": {
                    "commit": replacement,
                    "stable_patch_id": replacement_patch_id,
                },
            },
        ]

        with chdir(self.repo):
            records = release.apply_required_patches(
                patches, published_input_head=published, upstream_head=published, kind="component"
            )

        output = self.command("git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(self.command("git", "show", "HEAD:component.txt").stdout, "reviewed payload\n")
        self.assertEqual([record["source_commit"] for record in records], [replacement, source])
        self.assertEqual(records[0]["status"], "applied")
        self.assertEqual(records[1], {
            "kind": "component",
            "status": "represented_by_prior_application",
            "source_commit": source,
            "applied_commit": replacement,
            "output_commit": output,
            "output_patch_id": replacement_patch_id,
        })

    def test_direct_component_already_applied_as_prior_reviewed_replacement_is_recorded(self) -> None:
        """A later direct component must reuse an earlier reviewed replacement."""
        self.command("git", "checkout", "-qb", "original-component-source")
        source = self.write_and_commit("base.txt", "source value\n", "conflicting source")
        self.command("git", "checkout", "-q", self.initial_branch)
        published = self.write_and_commit("base.txt", "upstream value\n", "upstream conflict")
        self.command("git", "checkout", "-qb", "reviewed-component-output", published)
        replacement = self.write_and_commit("component.txt", "reviewed payload\n", "reviewed replacement")
        replacement_patch_id = release.stable_patch_id(replacement)
        self.command("git", "checkout", "-q", self.initial_branch)
        self.command("git", "reset", "--hard", published)

        patches = [
            {
                "commit": source,
                "subject": "conflicting source",
                "stable_patch_id": release.stable_patch_id(source),
                "accepted_output_patch_ids": [replacement_patch_id],
                "reviewed_replacement": {
                    "commit": replacement,
                    "stable_patch_id": replacement_patch_id,
                },
            },
            {
                "commit": replacement,
                "subject": "reviewed replacement",
                "stable_patch_id": replacement_patch_id,
            },
        ]

        with chdir(self.repo):
            records = release.apply_required_patches(
                patches, published_input_head=published, upstream_head=published, kind="component"
            )

        self.assertEqual(records[0]["status"], "applied_reviewed_replacement")
        self.assertEqual(records[1], {
            "kind": "component",
            "status": "represented_by_prior_application",
            "source_commit": replacement,
            "output_commit": records[0]["output_commit"],
            "output_patch_id": replacement_patch_id,
        })

    def test_reviewed_component_replacement_is_proven_by_its_declared_source(self) -> None:
        self.command("git", "checkout", "-qb", "original-component-source", self.base)
        original = self.write_and_commit("component.txt", "original\n", "original component patch")
        self.command("git", "checkout", "-qb", "reviewed-replacement-source", self.base)
        replacement = self.write_and_commit("component.txt", "reviewed\n", "reviewed component replacement")
        replacement_patch_id = release.stable_patch_id(replacement)
        self.command("git", "checkout", "-q", self.initial_branch)
        self.command("git", "remote", "add", release.FORK_REMOTE, str(self.repo))
        old_manifest, old_foundations = release.MANIFEST, release.UPSTREAM_FOUNDATIONS
        release.MANIFEST = {"components": [{
            "id": "component",
            "source_ref": "fork/original-component-source",
            "patches": [{
                "commit": original,
                "subject": "original component patch",
                "stable_patch_id": release.stable_patch_id(original),
                "accepted_output_patch_ids": [replacement_patch_id],
                "reviewed_replacement": {
                    "commit": replacement,
                    "stable_patch_id": replacement_patch_id,
                    "source_ref": "fork/reviewed-replacement-source",
                },
            }],
        }]}
        try:
            release.UPSTREAM_FOUNDATIONS = []
            with chdir(self.repo):
                release.verify_manifest_sources()
        finally:
            release.MANIFEST, release.UPSTREAM_FOUNDATIONS = old_manifest, old_foundations

    def test_manifest_verification_allows_slow_source_ancestry_checks(self) -> None:
        """Fetched source history can take longer than the old 60-second budget to walk."""
        self.command("git", "checkout", "-qb", "component-source", self.base)
        source = self.write_and_commit("component.txt", "payload\n", "component patch")
        patch_id = release.stable_patch_id(source)
        self.command("git", "checkout", "-q", self.initial_branch)
        self.command("git", "remote", "add", release.FORK_REMOTE, str(self.repo))
        old_manifest, old_foundations, old_run = release.MANIFEST, release.UPSTREAM_FOUNDATIONS, release.run
        ancestry_timeouts: list[int] = []

        def slow_ancestry_run(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[:3] == ("git", "merge-base", "--is-ancestor"):
                timeout = int(kwargs.get("timeout", 900))
                ancestry_timeouts.append(timeout)
                if timeout < 300:
                    raise subprocess.TimeoutExpired(args, timeout)
            return self.local_run(*args, **kwargs)

        release.MANIFEST = {"components": [{
            "id": "component",
            "source_ref": "fork/component-source",
            "patches": [{
                "commit": source,
                "subject": "component patch",
                "stable_patch_id": patch_id,
            }],
        }]}
        try:
            release.UPSTREAM_FOUNDATIONS = []
            release.run = slow_ancestry_run
            with chdir(self.repo):
                release.verify_manifest_sources()
        finally:
            release.run = old_run
            release.MANIFEST, release.UPSTREAM_FOUNDATIONS = old_manifest, old_foundations

        self.assertEqual(ancestry_timeouts, [300])

    def test_rebased_component_source_accepts_stable_patch_equivalent(self) -> None:
        """A force-pushed source may rewrite commit IDs without changing its patch."""
        self.command("git", "checkout", "-qb", "component-source", self.base)
        original = self.write_and_commit("component.txt", "payload\n", "component patch")
        patch_id = release.stable_patch_id(original)
        self.command("git", "reset", "--hard", self.base)
        rewritten = self.write_and_commit("component.txt", "payload\n", "component patch")
        self.command("git", "commit", "--amend", "--no-edit", "--date", "2000-01-01T00:00:00+0000")
        rewritten = self.command("git", "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(original, rewritten)
        self.assertEqual(patch_id, release.stable_patch_id(rewritten))
        self.command("git", "checkout", "-q", self.initial_branch)
        self.command("git", "remote", "add", release.FORK_REMOTE, str(self.repo))
        old_manifest, old_foundations = release.MANIFEST, release.UPSTREAM_FOUNDATIONS
        release.MANIFEST = {"components": [{
            "id": "component",
            "source_ref": "fork/component-source",
            "patches": [{
                "commit": original,
                "subject": "component patch",
                "stable_patch_id": patch_id,
            }],
        }]}
        try:
            release.UPSTREAM_FOUNDATIONS = []
            with chdir(self.repo):
                release.verify_manifest_sources()
        finally:
            release.MANIFEST, release.UPSTREAM_FOUNDATIONS = old_manifest, old_foundations

    def test_record_backed_validators_do_not_scan_represented_history(self) -> None:
        required = self.write_and_commit("required.txt", "required\n", "required component")
        patch_id = release.stable_patch_id(required)
        patch = {"commit": required, "subject": "required component", "stable_patch_id": patch_id}
        record = {
            "source_commit": required,
            "output_commit": required,
            "output_patch_id": patch_id,
            "status": "applied",
            "kind": "component",
        }
        old_required, old_represented = release.REQUIRED_PATCHES, release._represented_commits
        release.REQUIRED_PATCHES = [patch]
        release._represented_commits = lambda *_args: (_ for _ in ()).throw(AssertionError("full history scan forbidden"))
        try:
            with chdir(self.repo):
                release.validate_required_components(self.base, required, records=[record])
                release.validate_published_commit_preservation([required], self.base, required, records=[record])
        finally:
            release.REQUIRED_PATCHES, release._represented_commits = old_required, old_represented

    def test_required_foundation_missing_from_reconstructed_output_is_rejected(self) -> None:
        """Foundation sources alone do not prove the reconstructed output."""
        self.command("git", "checkout", "-qb", "foundation-source")
        source = self.write_and_commit("foundation.txt", "foundation\n", "pinned foundation")
        foundation_patch = {
            "commit": source,
            "subject": "pinned foundation",
            "stable_patch_id": release.stable_patch_id(source),
        }
        old_foundations = release.FOUNDATION_PATCHES
        release.FOUNDATION_PATCHES = [foundation_patch]
        try:
            with chdir(self.repo), self.assertRaisesRegex(RuntimeError, r"missing required foundation"):
                release.validate_required_foundations(self.base, self.base)
        finally:
            release.FOUNDATION_PATCHES = old_foundations

    def test_main_validates_replayed_direct_commit_without_scope_rejection(self) -> None:
        calls: list[tuple[str, object]] = []
        old_lock, old_argv, old_fail = release.exclusive_lock, __import__("sys").argv, release.fail
        old_emit, old_identity, old_sync, old_foundations = release.emit_fleet_receipt, release.ensure_clean_identity, release.synchronize_to_published_head, release.verify_upstream_foundations
        old_sources, old_resolution, old_upstream_resolution = release.verify_manifest_sources, release.patch_resolution, release.upstream_patch_resolution
        old_range, old_replay = release.published_integration_range, release.replay_published_integration_range
        old_required_validator, old_foundation_validator, old_preservation_validator = (
            release.validate_required_components, release.validate_required_foundations, release.validate_published_commit_preservation
        )
        old_verify_release, old_run = release.verify_existing_integration_release, release.run
        old_current_output = release.output_is_already_based_on_current_upstream

        def harmless_run(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            stdout = f"{self.base}\n" if args[:2] == ("git", "rev-parse") else ""
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        release.run = harmless_run
        release.exclusive_lock = nullcontext
        release.fail = lambda message, code=1: (_ for _ in ()).throw(AssertionError(message))
        release.emit_fleet_receipt = lambda *args, **kwargs: None
        release.ensure_clean_identity = lambda: (self.base, self.base)
        release.synchronize_to_published_head = lambda local, published: published
        release.verify_upstream_foundations = lambda: []
        release.verify_manifest_sources = lambda: None
        release.patch_resolution = lambda upstream, patches, **_kwargs: ([], [])
        release.upstream_patch_resolution = lambda upstream, **_kwargs: ([], [])
        release.published_integration_range = lambda published, upstream: (self.base, ["direct-user-commit"])
        release.replay_published_integration_range = lambda published, upstream, **_kwargs: [{
            "kind": "published", "status": "applied", "source_commit": "direct-user-commit",
            "output_commit": self.base, "output_patch_id": "a" * 40,
        }]
        release.validate_required_components = lambda upstream, head, **_kwargs: calls.append(("required", (upstream, head)))
        release.validate_required_foundations = lambda upstream, head, **_kwargs: calls.append(("foundations", (upstream, head)))
        release.validate_published_commit_preservation = lambda commits, upstream, head, **_kwargs: calls.append(("preserved", (commits, upstream, head)))
        release.verify_existing_integration_release = lambda head, expected_sha=None: {"complete": True, "reason": "release_complete"}
        release.output_is_already_based_on_current_upstream = lambda published, upstream: False
        try:
            __import__("sys").argv = [str(SCRIPT)]
            self.assertEqual(release.main(), 0)
        finally:
            __import__("sys").argv = old_argv
            release.run = old_run
            release.exclusive_lock, release.fail, release.emit_fleet_receipt = old_lock, old_fail, old_emit
            release.ensure_clean_identity, release.synchronize_to_published_head, release.verify_upstream_foundations = old_identity, old_sync, old_foundations
            release.verify_manifest_sources, release.patch_resolution, release.upstream_patch_resolution = old_sources, old_resolution, old_upstream_resolution
            release.published_integration_range, release.replay_published_integration_range = old_range, old_replay
            release.validate_required_components, release.validate_required_foundations, release.validate_published_commit_preservation = (
                old_required_validator, old_foundation_validator, old_preservation_validator
            )
            release.verify_existing_integration_release = old_verify_release
            release.output_is_already_based_on_current_upstream = old_current_output
        # v2 core (2026-08-17): the branch is the source of truth — the only
        # validator main() wires is published-commit preservation.
        self.assertEqual([name for name, _value in calls], ["preserved"])
        self.assertEqual(calls[0][1][0], ["direct-user-commit"])

    def test_force_with_lease_uses_fetched_sha_and_rejects_remote_race_without_retry(self) -> None:
        fixture = self.published_branch_fixture()
        published = fixture["published"]
        self.command("git", "reset", "--hard", published)
        output = self.write_and_commit("rebased.txt", "rebased output\n", "rebased output")
        publisher = Path(self.temp.name) / "publisher"
        raced = self.write_and_commit_at(publisher, "race.txt", "remote race\n", "remote moved after fetch")
        self.command_at(publisher, "git", "push", "-q", "origin", release.BRANCH)
        recorded: list[tuple[str, ...]] = []
        original_git = release.git

        def recording_git(*args: str, **kwargs: object) -> str:
            recorded.append(args)
            return original_git(*args, **kwargs)

        release.git = recording_git
        try:
            with chdir(self.repo), self.assertRaises(RuntimeError):
                release.push_rebased_output(published, output)
        finally:
            release.git = original_git
        pushes = [args for args in recorded if args and args[0] == "push"]
        self.assertEqual(len(pushes), 1, "a lease rejection must never be retried with a newer SHA")
        self.assertIn(f"--force-with-lease=refs/heads/{release.BRANCH}:{published}", pushes[0])
        self.assertEqual(
            self.command_at(publisher, "git", "rev-parse", "HEAD").stdout.strip(), raced,
            "the raced remote tip must not be clobbered",
        )

    def test_pre_push_restoration_targets_published_input_not_pre_run_local(self) -> None:
        fixture = self.published_branch_fixture(divergent=True)
        self.command("git", "reset", "--hard", fixture["published"])
        self.write_and_commit("transaction.txt", "temporary\n", "temporary reconstruction")
        with chdir(self.repo):
            release.restore_pre_push_checkout(fixture["published"])
        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), fixture["published"])
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")
        self.assertNotEqual(fixture["local"], fixture["published"])

    def test_recovery_decision_skips_replay_for_current_output_missing_release(self) -> None:
        decision = release.release_recovery_decision(
            published_input_head="published-sha",
            rebased_output_head="output-sha",
            branch_is_current_output=True,
            release_exists=False,
        )
        self.assertEqual(decision, {"replay": False, "push": False, "publish_release": True, "reason": "release_missing_for_current_output"})

    def test_recovery_decision_reports_complete_current_output_idempotently(self) -> None:
        decision = release.release_recovery_decision(
            published_input_head="output-sha",
            rebased_output_head="output-sha",
            branch_is_current_output=True,
            release_exists=True,
        )
        self.assertEqual(decision, {"replay": False, "push": False, "publish_release": False, "reason": "integration_and_release_already_current"})

    def test_existing_release_requires_public_manifests_and_installer_checksum(self) -> None:
        """A name/size match alone is not a completed public release."""
        commit = "a" * 40
        tag = f"integration-20260813-{commit[:12]}"
        old_metadata, old_asset_bytes = release.public_github_payload, getattr(release, "public_release_asset_bytes", None)
        release.public_github_payload = lambda _url: [{"tag_name": tag, "target_commitish": commit, "assets": [
            {"name": "Hermes-Setup.exe", "browser_download_url": "https://public/installer"},
            {"name": "SHA256SUMS.txt", "browser_download_url": "https://public/sums"},
            {"name": "PROVENANCE.json", "browser_download_url": "https://public/provenance"},
        ]}]
        payloads = {
            "https://public/installer": b"installer bytes",
            "https://public/sums": b"0" * 64 + b"  Hermes-Setup.exe\n",
            "https://public/provenance": json.dumps({"repository": release.REPOSITORY, "branch": release.BRANCH, "commit": commit, "launcher": "Hermes-Setup.exe", "sha256": "0" * 64}).encode(),
        }
        try:
            release.public_release_asset_bytes = lambda url: payloads[url]
            decision = release.verify_existing_integration_release(commit, expected_sha="0" * 64)
        finally:
            release.public_github_payload = old_metadata
            if old_asset_bytes is None:
                delattr(release, "public_release_asset_bytes")
            else:
                release.public_release_asset_bytes = old_asset_bytes
        self.assertFalse(decision["complete"])
        self.assertIn("checksum", str(decision["reason"]))

    def test_restore_pre_push_checkout_removes_untracked_failure_artifacts(self) -> None:
        fixture = self.published_branch_fixture(divergent=True)
        self.command("git", "reset", "--hard", fixture["published"])
        (self.repo / "failed-build-artifact.txt").write_text("untracked", encoding="utf-8")
        with chdir(self.repo):
            release.restore_pre_push_checkout(fixture["published"])
        self.assertFalse((self.repo / "failed-build-artifact.txt").exists())
        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), fixture["published"])
        self.assertEqual(self.command("git", "status", "--porcelain").stdout.strip(), "")

    def test_main_recovery_validates_required_components_before_build(self) -> None:
        """v2: a recovered output missing a published commit dies before build."""
        calls: list[str] = []
        old = {name: getattr(release, name) for name in (
            "exclusive_lock", "fail", "emit_fleet_receipt", "ensure_clean_identity", "synchronize_to_published_head",
            "verify_upstream_foundations", "verify_manifest_sources", "patch_resolution", "upstream_patch_resolution",
            "published_integration_range", "output_is_already_based_on_current_upstream", "_exact_published_records", "validate_required_components",
            "validate_published_commit_preservation", "run",
        )}
        old_argv = __import__("sys").argv
        release.exclusive_lock = nullcontext
        release.fail = lambda message, code=1: (_ for _ in ()).throw(SystemExit(message))
        release.emit_fleet_receipt = lambda *args, **kwargs: None
        release.ensure_clean_identity = lambda: ("stale", "published")
        release.synchronize_to_published_head = lambda *_args: "published"
        release.verify_upstream_foundations = lambda: []
        release.verify_manifest_sources = lambda: None
        release.patch_resolution = lambda *_args, **_kwargs: ([], [])
        release.upstream_patch_resolution = lambda *_args, **_kwargs: ([], [])
        release.published_integration_range = lambda *_args: ("base", ["published-component-lost"])
        release.output_is_already_based_on_current_upstream = lambda *_args: True
        release._exact_published_records = lambda commits: [{
            "kind": "published", "status": "exact_reachable", "source_commit": commits[0],
            "output_commit": commits[0], "output_patch_id": "a" * 40,
        }]
        release.validate_required_components = lambda *_args, **_kwargs: None
        release.validate_published_commit_preservation = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(RuntimeError("missing required component"))
        )
        def no_build_run(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if "fetch" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[-2:] == ("rev-parse", "HEAD"):
                return subprocess.CompletedProcess(args, 0, stdout="published\n", stderr="")
            if args[-2:] == ("rev-parse", "origin/main"):
                return subprocess.CompletedProcess(args, 0, stdout="upstream\n", stderr="")
            if args[-2:] == ("rev-parse", "refs/remotes/fork/fork-integration"):
                return subprocess.CompletedProcess(args, 0, stdout="published\n", stderr="")
            raise AssertionError(f"build/publish must not run: {args}")
        release.run = no_build_run
        try:
            __import__("sys").argv = [str(SCRIPT)]
            with self.assertRaisesRegex(SystemExit, "missing required component"):
                release.main()
        finally:
            __import__("sys").argv = old_argv
            for name, value in old.items():
                setattr(release, name, value)
        # no_build_run's AssertionError guard already proves the build never
        # started; the preservation failure is the death before it.
        self.assertEqual(calls, [])

    def test_main_recovery_adopts_published_input_then_builds_missing_release_without_reconstruction(self) -> None:
        """Canonical adoption may reset locally; recovery must not reconstruct or rewrite it."""
        calls: list[tuple[str, object]] = []
        old = {name: getattr(release, name) for name in (
            "exclusive_lock", "ensure_clean_identity", "synchronize_to_published_head", "verify_upstream_foundations",
            "verify_manifest_sources", "patch_resolution", "upstream_patch_resolution", "published_integration_range",
            "replay_published_integration_range", "output_is_already_based_on_current_upstream",
            "validate_required_components", "validate_required_foundations", "validate_published_commit_preservation",
            "push_rebased_output", "run",
        )}
        old_argv = __import__("sys").argv

        def stop_at_release_build(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(("run", args))
            if args[:4] == ("npm", "--workspace", "apps/bootstrap-installer", "run"):
                raise SystemExit("release-only build reached")
            if args[-2:] == ("rev-parse", "refs/remotes/fork/fork-integration"):
                return subprocess.CompletedProcess(args, 0, stdout="published-current-output\n", stderr="")
            if args[-2:] == ("rev-parse", "origin/main"):
                return subprocess.CompletedProcess(args, 0, stdout="fetched-upstream\n", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        release.exclusive_lock = nullcontext
        release.ensure_clean_identity = lambda: ("stale-local-scheduler-tip", "published-current-output")
        release.synchronize_to_published_head = lambda local, published: calls.append(("adopt", (local, published))) or published
        release.verify_upstream_foundations = lambda: []
        release.verify_manifest_sources = lambda: None
        release.patch_resolution = lambda upstream, patches, **_kwargs: ([], [])
        release.upstream_patch_resolution = lambda upstream, **_kwargs: ([], [])
        release.published_integration_range = lambda published, upstream: ("base", [])
        release.replay_published_integration_range = lambda *args: (_ for _ in ()).throw(AssertionError("recovery must not replay"))
        release.output_is_already_based_on_current_upstream = lambda published, upstream: True
        release.validate_required_components = lambda *_args, **_kwargs: calls.append(("validate_required", None))
        release.validate_required_foundations = lambda *_args, **_kwargs: calls.append(("validate_foundations", None))
        release.validate_published_commit_preservation = lambda *_args, **_kwargs: calls.append(("validate_preservation", None))
        release.push_rebased_output = lambda *args: (_ for _ in ()).throw(AssertionError("recovery must not push"))
        release.run = stop_at_release_build
        try:
            __import__("sys").argv = [str(SCRIPT)]
            with self.assertRaisesRegex(SystemExit, "release-only build reached"):
                release.main()
        finally:
            __import__("sys").argv = old_argv
            for name, value in old.items():
                setattr(release, name, value)

        adoption = ("adopt", ("stale-local-scheduler-tip", "published-current-output"))
        self.assertIn(adoption, calls)
        after_adoption = calls[calls.index(adoption) + 1:]
        self.assertFalse(any(name == "run" and "reset" in value and "--hard" in value for name, value in after_adoption))
        self.assertFalse(any(name == "run" and "push" in value for name, value in after_adoption))
        self.assertEqual(
            [value for name, value in calls if name == "run" and value[:4] == ("npm", "--workspace", "apps/bootstrap-installer", "run")],
            [("npm", "--workspace", "apps/bootstrap-installer", "run", "typecheck")],
        )

    def test_main_wires_post_push_recovery_after_checksum_backed_release_verification(self) -> None:
        """A prior pushed output may become a no-op only after release integrity is verified."""
        recovery_calls: list[dict[str, object]] = []
        old = {name: getattr(release, name) for name in (
            "exclusive_lock", "ensure_clean_identity", "synchronize_to_published_head", "verify_upstream_foundations",
            "verify_manifest_sources", "patch_resolution", "upstream_patch_resolution", "published_integration_range",
            "output_is_already_based_on_current_upstream", "validate_required_components",
            "validate_required_foundations", "validate_published_commit_preservation", "resolve_built_launcher",
            "sha256", "verify_existing_integration_release", "release_recovery_decision", "git", "run", "publish_release",
            "integration_scripts_integrity_check",
        )}
        old_argv = __import__("sys").argv
        launcher = self.repo / "Hermes-Setup.exe"
        launcher.write_bytes(b"x" * 1_000_001)

        def fake_git(*args: str, **_kwargs: object) -> str:
            if args[:2] == ("rev-parse", "origin/main"):
                return "upstream"
            if args[:2] == ("rev-parse", "refs/remotes/fork/fork-integration"):
                return "published-current-output"
            if args[:2] == ("rev-parse", "HEAD"):
                return "published-current-output"
            if args[:2] == ("ls-remote", release.FORK_REMOTE):
                return "published-current-output\trefs/heads/fork-integration"
            return ""

        release.exclusive_lock = nullcontext
        # This test wires post-push recovery; the integrity gate has its own
        # coverage and must not couple this test to the machine's live
        # operational generation (a worktree TRACKED_SET newer than the
        # deployed stamp would otherwise fail main() before recovery runs).
        release.integration_scripts_integrity_check = lambda **_kwargs: {
            "ok": True, "source_sha": "stubbed", "stamped_source_sha": "stubbed", "files": {},
        }
        release.ensure_clean_identity = lambda: ("stale-local", "published-current-output")
        release.synchronize_to_published_head = lambda *_args: "published-current-output"
        release.verify_upstream_foundations = lambda: []
        release.verify_manifest_sources = lambda: None
        release.patch_resolution = lambda *_args, **_kwargs: ([], [])
        release.upstream_patch_resolution = lambda *_args, **_kwargs: ([], [])
        release.published_integration_range = lambda *_args: ("base", [])
        release.output_is_already_based_on_current_upstream = lambda *_args: True
        release.validate_required_components = lambda *_args, **_kwargs: None
        release.validate_required_foundations = lambda *_args, **_kwargs: None
        release.validate_published_commit_preservation = lambda *_args, **_kwargs: None
        release.resolve_built_launcher = lambda: launcher
        release.sha256 = lambda _path: "checksum"
        release.verify_existing_integration_release = lambda *_args, **_kwargs: {"complete": True, "reason": "release_complete"}
        release.release_recovery_decision = lambda **kwargs: recovery_calls.append(kwargs) or {
            "replay": False, "push": False, "publish_release": not bool(kwargs["release_exists"]),
            "reason": "integration_and_release_already_current" if kwargs["release_exists"] else "release_missing_for_current_output",
        }
        release.git = fake_git
        release.run = lambda *args, **_kwargs: subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        release.publish_release = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("complete recovery must not publish"))
        try:
            __import__("sys").argv = [str(SCRIPT)]
            self.assertEqual(release.main(), 0)
        finally:
            __import__("sys").argv = old_argv
            for name, value in old.items():
                setattr(release, name, value)

        self.assertEqual(len(recovery_calls), 1)
        self.assertTrue(recovery_calls[0]["release_exists"])
        self.assertTrue(recovery_calls[0]["branch_is_current_output"])

    def test_dry_run_inspection_is_non_mutating_and_reports_replay_provenance(self) -> None:
        """Dry-run reads remote refs without fetching or touching the scheduler checkout."""
        fixture = self.published_branch_fixture(divergent=True)
        publisher = Path(self.temp.name) / "publisher"
        self.command_at(publisher, "git", "branch", "main", fixture["base"])
        self.command_at(publisher, "git", "checkout", "-q", "main")
        upstream = self.write_and_commit_at(publisher, "upstream.txt", "upstream\n", "upstream main")
        self.command_at(publisher, "git", "push", "-q", "origin", "main")
        self.command("git", "remote", "add", release.UPSTREAM_REMOTE, str(Path(self.temp.name) / "fork.git"))
        self.command("git", "fetch", "-q", release.UPSTREAM_REMOTE, "refs/heads/main:refs/remotes/origin/main")

        before_head = self.command("git", "rev-parse", "HEAD").stdout.strip()
        before_status = self.command("git", "status", "--porcelain").stdout
        before_refs = self.command("git", "for-each-ref", "--format=%(refname) %(objectname)").stdout
        before_safety = self.command("git", "for-each-ref", "refs/heads/safety").stdout
        old_worktree = release.WORKTREE
        release.WORKTREE = self.repo
        try:
            with chdir(self.repo):
                result = release.inspect_dry_run()
        finally:
            release.WORKTREE = old_worktree
        self.assertEqual(self.command("git", "rev-parse", "HEAD").stdout.strip(), before_head)
        self.assertEqual(self.command("git", "status", "--porcelain").stdout, before_status)
        self.assertEqual(self.command("git", "for-each-ref", "--format=%(refname) %(objectname)").stdout, before_refs)
        self.assertEqual(self.command("git", "for-each-ref", "refs/heads/safety").stdout, before_safety)
        self.assertEqual(result["pre_run_local_head"], fixture["local"])
        self.assertEqual(result["published_input_head"], fixture["published"])
        self.assertTrue(result["local_would_sync_to_published"])
        self.assertEqual(result["old_upstream_base"], fixture["base"])
        self.assertEqual(result["current_upstream"], upstream)
        self.assertEqual(result["published_commit_count"], 1)
        self.assertEqual(result["absorbed_commit_count"], 0)
        self.assertEqual(result["commits_to_replay"], [fixture["published"]])
        self.assertTrue(result["would_rebase_complete_published_range"])
        self.assertEqual(result["push_lease_head"], fixture["published"])
        self.assertEqual(result["upstream_provenance"], "local_tracking_matches_remote")

    def test_main_post_push_failure_never_resets_or_rewrites_backwards(self) -> None:
        commands: list[tuple[str, ...]] = []
        failures: list[str] = []
        old = {name: getattr(release, name) for name in (
            "exclusive_lock", "fail", "emit_fleet_receipt", "ensure_clean_identity", "synchronize_to_published_head",
            "verify_upstream_foundations", "verify_manifest_sources", "patch_resolution", "upstream_patch_resolution",
            "published_integration_range", "replay_published_integration_range", "validate_required_components", "validate_required_foundations",
            "validate_published_commit_preservation", "output_is_already_based_on_current_upstream", "resolve_built_launcher",
            "sha256", "git", "run", "publish_release",
        )}
        old_argv = __import__("sys").argv
        launcher = self.repo / "Hermes-Setup.exe"
        launcher.write_bytes(b"x" * 1_000_001)

        def fake_git(*args: str, **kwargs: object) -> str:
            commands.append(args)
            if args[:2] == ("rev-parse", "refs/remotes/fork/fork-integration"):
                return "published-input"
            if args[:2] == ("rev-parse", "HEAD"):
                return "rebased-output"
            if args[:2] == ("ls-remote", release.FORK_REMOTE):
                return "rebased-output\trefs/heads/fork-integration"
            return ""

        release.exclusive_lock = nullcontext
        release.fail = lambda message, code=1: (failures.append(message), (_ for _ in ()).throw(SystemExit(code)))[1]
        release.emit_fleet_receipt = lambda *args, **kwargs: None
        release.ensure_clean_identity = lambda: ("pre-run-local", "ignored")
        release.synchronize_to_published_head = lambda local, published: published
        release.verify_upstream_foundations = lambda: []
        release.verify_manifest_sources = lambda: None
        release.patch_resolution = lambda upstream, patches, **_kwargs: ([], [])
        release.upstream_patch_resolution = lambda upstream, **_kwargs: ([], [])
        release.published_integration_range = lambda published, upstream: ("base", ["published-commit"])
        release.replay_published_integration_range = lambda published, upstream, **_kwargs: []
        release.validate_required_components = lambda upstream, head, **_kwargs: None
        release.validate_required_foundations = lambda upstream, head, **_kwargs: None
        release.validate_published_commit_preservation = lambda commits, upstream, head, **_kwargs: None
        release.output_is_already_based_on_current_upstream = lambda published, upstream: False
        release.resolve_built_launcher = lambda: launcher
        release.sha256 = lambda path: "checksum"
        release.git = fake_git
        release.run = lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        release.publish_release = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("release publication failed"))
        try:
            __import__("sys").argv = [str(SCRIPT)]
            with self.assertRaises(SystemExit):
                release.main()
        finally:
            __import__("sys").argv = old_argv
            for name, value in old.items():
                setattr(release, name, value)
        self.assertTrue(any(args and args[0] == "push" for args in commands))
        self.assertFalse(any(args[:2] == ("reset", "--hard") for args in commands))
        self.assertIn("branch_pushed=true", failures[0])

    def test_main_dry_run_skips_log_lock_and_all_mutating_paths(self) -> None:
        calls: list[str] = []
        old = {name: getattr(release, name) for name in (
            "inspect_dry_run", "log", "exclusive_lock", "run", "git", "synchronize_to_published_head",
            "replay_published_integration_range", "push_rebased_output", "publish_release", "resolve_built_launcher",
            "_sync_module",
        )}
        old_argv = __import__("sys").argv
        result = {
            "ok": True, "dry_run": True, "published_input_head": "published", "old_upstream_base": None,
            "current_upstream": "upstream", "commits_to_replay": [], "absorbed_commit_count": 0,
            "push_lease_head": "published",
        }
        def forbidden(name: str):
            return lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError(f"dry-run must not invoke {name}"))
        release.inspect_dry_run = lambda: calls.append("inspect") or result
        release.log = forbidden("log")
        release.exclusive_lock = forbidden("lock")
        release.run = forbidden("run")
        release.git = forbidden("git")
        release.synchronize_to_published_head = forbidden("synchronize")
        release.replay_published_integration_range = forbidden("replay")
        release.push_rebased_output = forbidden("push")
        release.publish_release = forbidden("release")
        release.resolve_built_launcher = forbidden("build")
        # U2: the run-start integrity gate now runs before the dry-run
        # inspection too. Stub sync.py's verify() as a clean pass so this
        # test's "nothing but inspect_dry_run runs" contract stays exact,
        # without performing real git subprocess calls against the real
        # WORKTREE/HERMES_HOME (which are not overridden in this test).
        release._sync_module = lambda: SimpleNamespace(
            verify=lambda dest, repo: {"ok": True, "source_sha": "stub", "files": {}, "problems": []}
        )
        try:
            __import__("sys").argv = [str(SCRIPT), "--dry-run"]
            self.assertEqual(release.main(), 0)
        finally:
            __import__("sys").argv = old_argv
            for name, value in old.items():
                setattr(release, name, value)
        self.assertEqual(calls, ["inspect"])

    def test_main_failure_launches_sanitized_investigator_without_masking_exit(self) -> None:
        launches: list[dict[str, str]] = []
        old_lock, old_identity, old_emit, old_fail = (
            release.exclusive_lock, release.ensure_clean_identity, release.emit_fleet_receipt, release.fail,
        )
        old_argv = __import__("sys").argv
        release.exclusive_lock = nullcontext
        release.ensure_clean_identity = lambda: (_ for _ in ()).throw(
            RuntimeError("Authorization: Bearer secret-token pid=1234 at abcdef1234567")
        )
        release.launch_failure_investigator = lambda **kwargs: launches.append(kwargs)
        release.emit_fleet_receipt = lambda *_args, **_kwargs: None
        release.fail = lambda _message, code=1: (_ for _ in ()).throw(SystemExit(code))
        try:
            __import__("sys").argv = [str(SCRIPT)]
            with self.assertRaises(SystemExit) as exited:
                release.main()
        finally:
            __import__("sys").argv = old_argv
            release.exclusive_lock, release.ensure_clean_identity, release.emit_fleet_receipt, release.fail = (
                old_lock, old_identity, old_emit, old_fail,
            )
        self.assertEqual(exited.exception.code, 1)
        self.assertEqual(launches[0]["stage"], "identity")
        self.assertNotIn("secret-token", launches[0]["error"])
        self.assertIn("[REDACTED]", launches[0]["error"])

    def test_failure_investigator_uses_the_real_fleet_cron_job_id(self) -> None:
        self.assertEqual(release.FLEET_JOB_ID, "1ab4c7013fef")
        self.assertRegex(release.FLEET_JOB_ID, r"^[0-9a-f]{12}$")
        calls: list[tuple[str, object]] = []
        class Investigator:
            def record_failure(self, **kwargs):
                calls.append(("record", kwargs["job_id"]))
                return {"signature": "safe", "spawn": False, "occurrences": 1, "artifact_path": "C:/safe/artifact.json"}
            def maybe_launch_investigator(self, result): calls.append(("launch", result))
            def resolve_success(self, job_id, home): calls.append(("resolve", job_id))
        old_module, old_log = release._failure_investigator_module, release.log
        release._failure_investigator_module = lambda: Investigator()
        release.log = lambda _line: None
        try:
            self.original_launch_failure_investigator(stage="identity", error="safe")
            self.original_resolve_failure_investigator_success()
        finally:
            release._failure_investigator_module, release.log = old_module, old_log
        self.assertEqual(calls[0], ("record", "1ab4c7013fef"))
        self.assertEqual(calls[2], ("resolve", "1ab4c7013fef"))

    def test_remote_ref_head_reports_zero_match(self) -> None:
        old_run = release.run
        release.run = lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout="", stderr="")
        try:
            with self.assertRaisesRegex(RuntimeError, r"zero matching remote refs.*fork.*refs/heads/missing"):
                release.remote_ref_head("fork", "refs/heads/missing")
        finally:
            release.run = old_run

    def test_remote_ref_head_reports_malformed_output(self) -> None:
        old_run = release.run
        release.run = lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout="not-a-sha\trefs/heads/main\n", stderr="")
        try:
            with self.assertRaisesRegex(RuntimeError, r"malformed remote ref output.*not-a-sha"):
                release.remote_ref_head("fork", "refs/heads/main")
        finally:
            release.run = old_run

    def test_remote_ref_head_reports_multiple_matches(self) -> None:
        old_run = release.run
        output = "a" * 40 + "\trefs/heads/main\n" + "b" * 40 + "\trefs/heads/main-alt\n"
        release.run = lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=output, stderr="")
        try:
            with self.assertRaisesRegex(RuntimeError, r"multiple matching remote refs.*count=2"):
                release.remote_ref_head("fork", "refs/heads/main")
        finally:
            release.run = old_run

    def test_remote_ref_head_reports_sanitized_ls_remote_failure(self) -> None:
        old_run = release.run
        release.run = lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 128, stdout="", stderr=(
                "fatal: https://user:url-credential@example.invalid/repo denied "
                "Authorization: Bearer bearer-secret \"token\": \"json-token-secret\"\nlast detail"
            )
        )
        try:
            with self.assertRaisesRegex(RuntimeError, r"ls-remote failed.*exit=128.*last detail") as caught:
                release.remote_ref_head("fork", "refs/heads/main")
        finally:
            release.run = old_run
        for secret in ("user", "url-credential", "bearer-secret", "json-token-secret"):
            self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn("user:", str(caught.exception))

    def test_run_redacts_credentials_before_durable_failure_log(self) -> None:
        """Generic command failures must never persist process output secrets."""
        recorded: list[str] = []
        old_log, old_resolve, old_subprocess_run = release.log, release.resolve_executable, release.subprocess.run
        secret_output = (
            "fatal https://alice:url-password@example.invalid/repo "
            "Authorization: Bearer bearer-token-value Authorization: Basic basic-credential "
            "password=hunter2 token=token-value secret=secret-value api_key=api-key-value "
            "{\"token\": \"json-token-value\", \"api_key\":\"json-api-key-value\", "
            "\"PASSWORD\": \"json-password-value\"}"
        )
        release.log = recorded.append
        release.resolve_executable = lambda name: name
        release.subprocess.run = lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["git"], 1, stdout=secret_output, stderr="stderr token: another-token"
        )
        release.run = self.original_run
        try:
            with self.assertRaises(RuntimeError):
                release.run("git", "status")
        finally:
            release.run = self.local_run
            release.log, release.resolve_executable, release.subprocess.run = old_log, old_resolve, old_subprocess_run
        durable = "\n".join(recorded)
        for secret in (
            "alice", "url-password", "bearer-token-value", "basic-credential", "hunter2",
            "token-value", "secret-value", "api-key-value", "json-token-value",
            "json-api-key-value", "json-password-value", "another-token",
        ):
            self.assertNotIn(secret, durable)
        self.assertIn("[REDACTED]", durable)
        self.assertIn("COMMAND_FAILURE executable=git exit=1", durable)
        self.assertIn("fatal", durable)

    def test_run_can_suppress_checked_failure_logging_for_dry_run_inspection(self) -> None:
        recorded: list[str] = []
        old_log, old_resolve, old_subprocess_run = release.log, release.resolve_executable, release.subprocess.run
        release.log = recorded.append
        release.resolve_executable = lambda name: name
        release.subprocess.run = lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["git"], 1, stdout="inspect failure", stderr=""
        )
        release.run = self.original_run
        try:
            with self.assertRaises(RuntimeError):
                release.run("git", "status", log_failure=False)
        finally:
            release.run = self.local_run
            release.log, release.resolve_executable, release.subprocess.run = old_log, old_resolve, old_subprocess_run
        self.assertEqual(recorded, [])

    def test_dry_run_git_commands_disable_optional_locks(self) -> None:
        """Inspection must not refresh local Git metadata through optional locks."""
        observed_envs: list[dict[str, str]] = []
        old_resolve, old_subprocess_run = release.resolve_executable, release.subprocess.run
        release.resolve_executable = lambda name: name
        release.subprocess.run = lambda _command, **kwargs: observed_envs.append(kwargs["env"]) or subprocess.CompletedProcess(
            ["git"], 0, stdout="", stderr=""
        )
        release.run = self.original_run
        try:
            with release.dry_run_inspection_logging_disabled():
                release.run("git", "status")
        finally:
            release.run = self.local_run
            release.resolve_executable, release.subprocess.run = old_resolve, old_subprocess_run
        self.assertEqual(observed_envs[0].get("GIT_OPTIONAL_LOCKS"), "0")

    def test_dry_run_defers_when_published_remote_object_is_unavailable_locally(self) -> None:
        published, upstream, local = "a" * 40, "b" * 40, "c" * 40
        old = {name: getattr(release, name) for name in ("WORKTREE", "git", "remote_ref_head", "run")}
        commands: list[tuple[str, ...]] = []
        release.WORKTREE = self.repo
        def fake_git(*args: str, **_kwargs: object) -> str:
            commands.append(args)
            values = {
                ("branch", "--show-current"): release.BRANCH,
                ("status", "--porcelain"): "",
                ("rev-parse", "HEAD"): local,
                ("rev-parse", "--verify", "-q", "refs/remotes/origin/main"): upstream,
            }
            return values.get(args, "")
        release.git = fake_git
        release.remote_ref_head = lambda remote, _ref: published if remote == release.FORK_REMOTE else upstream
        release.run = lambda *args, **_kwargs: subprocess.CompletedProcess(args, 1, stdout="", stderr="missing object")
        try:
            result = release.inspect_dry_run()
        finally:
            for name, value in old.items():
                setattr(release, name, value)
        self.assertEqual(result["inspection_deferred_reason"], "published_remote_object_not_available_locally")
        self.assertEqual(result["published_input_head"], published)
        self.assertEqual(result["current_upstream"], upstream)
        self.assertEqual(result["push_lease_head"], published)
        mutating_git_verbs = {"fetch", "reset", "push", "cherry-pick"}
        self.assertFalse(any(command and command[0] in mutating_git_verbs for command in commands))


if __name__ == "__main__":
    unittest.main(verbosity=2)
