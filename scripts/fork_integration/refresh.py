#!/usr/bin/env python3
"""Compose fresh upstream with the published fork range, then publish by lease."""
from __future__ import annotations
import argparse, json, os, shutil, subprocess, sys, tempfile, time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"}
FETCH_RATE_LIMIT_RETRY_DELAYS = (30, 120, 300)
class RefreshError(RuntimeError): pass
@dataclass(frozen=True)
class TreeAssertion:
    path: str
    contains: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    ordered: tuple[tuple[str, str], ...] = ()
KILL_ALL_MERGE = "merge: integrate kill-all Windows updater baseline"
MERGE_ASSERTIONS: dict[str, tuple[TreeAssertion, ...]] = {
    KILL_ALL_MERGE: (TreeAssertion(
        "apps/desktop/electron/main.ts", contains=("function forceKillAllHermesBackendTrees",),
        absent=("from './venv-blocker-scan'", "scanVenvBlockers(updateRoot)"),
        ordered=(("forceKillAllHermesBackendTrees(updateRoot)", "const lock = await releaseBackendLockForUpdate(updateRoot)"),)),),
    "merge: integrate live Windows update transport": (
        TreeAssertion("apps/desktop/electron/updater-process.ts", contains=("export function resolveWindowsUpdateTransport",)),
        TreeAssertion("apps/desktop/electron/main.ts", contains=("resolveWindowsUpdateTransport,",
            "resolveWindowsUpdateTransport(resolveUpdateRoot())", "windowsUpdateTransport?.kind === 'manual'",
            "windowsUpdateTransport?.kind === 'script' ? windowsUpdateTransport.handoff : null"))),
    "merge: integrate Windows updater transport regression coverage": (TreeAssertion(
        "apps/desktop/electron/updater-process.test.ts", contains=(
            "test('resolveWindowsUpdateTransport selects the live checkout script'",
            "test('resolveWindowsUpdateTransport requires a manual update without a live script'")),),
}
def _git(repo: Path, *args: str, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(GIT_ENV)
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=timeout,
        creationflags=CREATE_NO_WINDOW, env=env)
    if check and done.returncode: raise RefreshError(f"git {' '.join(args)} failed: {(done.stderr or done.stdout).strip()}")
    return done
def _fetch(repo: Path, remote: str, ref: str) -> str:
    for delay in (*FETCH_RATE_LIMIT_RETRY_DELAYS, None):
        try:
            _git(repo, "fetch", "--no-tags", "--quiet", remote, ref)
            return _git(repo, "rev-parse", "FETCH_HEAD").stdout.strip()
        except RefreshError as error:
            message = str(error).lower()
            if delay is None or ("429" not in message and "rate limit" not in message and "rate-limit" not in message):
                raise
            time.sleep(delay)
    raise AssertionError("fetch retry loop exhausted without returning or raising")
def _select_upstream(repo: Path, fetched: str, explicit: str | None, hour: int | None,
                     now: datetime | None) -> tuple[str, str | None]:
    if explicit:
        selected = _git(repo, "rev-parse", f"{explicit}^{{commit}}").stdout.strip()
        if _git(repo, "merge-base", "--is-ancestor", selected, fetched, check=False).returncode:
            raise RefreshError("explicit upstream SHA is not in fetched upstream history")
        return selected, None
    if hour is None: return fetched, None
    if not 0 <= hour <= 23: raise RefreshError("upstream cutoff hour must be between 0 and 23 UTC")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = current.replace(hour=hour, minute=0, second=0, microsecond=0)
    if current < cutoff: cutoff -= timedelta(days=1)
    selected = _git(repo, "rev-list", "--first-parent", "-1", f"--before={cutoff.isoformat()}", fetched).stdout.strip()
    if not selected: raise RefreshError(f"no upstream commit exists at or before {cutoff.isoformat()}")
    return selected, cutoff.isoformat()
def _remote_head(repo: Path, remote: str, ref: str) -> str | None:
    output = _git(repo, "ls-remote", "--heads", remote, ref).stdout.strip()
    return output.split()[0] if output else None
def _lines(repo: Path, *args: str) -> list[str]: return [x for x in _git(repo, *args).stdout.splitlines() if x]
def _cherry(repo: Path, upstream: str, published: str, base: str) -> dict[str, str]: return {x.split()[1]: x[0] for x in _lines(repo, "cherry", upstream, published, base)}
def _assert_tree(repo: Path, revision: str, assertions: Sequence[TreeAssertion]) -> None:
    for assertion in assertions:
        read = _git(repo, "show", f"{revision}:{assertion.path}", check=False)
        if read.returncode: raise RefreshError(f"merge assertion missing {assertion.path} at {revision}")
        text = read.stdout
        if any(x not in text for x in assertion.contains) or any(x in text for x in assertion.absent): raise RefreshError(f"merge assertion failed for {assertion.path} at {revision}")
        for before, after in assertion.ordered:
            if text.find(before) < 0 or text.find(after) <= text.find(before):
                raise RefreshError(f"merge ordering assertion failed for {assertion.path} at {revision}")
def _run_checks(scratch: Path, checks: Sequence[Sequence[str]]) -> None:
    for command in checks:
        done = subprocess.run(list(command), cwd=scratch, capture_output=True, text=True,
            encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=1800,
            creationflags=CREATE_NO_WINDOW)
        if done.returncode:
            raise RefreshError(f"focused check failed ({' '.join(command)}): {(done.stderr or done.stdout).strip()}")
def _scratch_rebase(repo: Path, published: str, upstream: str, base: str,
                    assertions: Sequence[TreeAssertion], checks: Sequence[Sequence[str]]) -> tuple[str, set[str], set[str]]:
    scratch = Path(tempfile.mkdtemp(prefix="hermes-fork-refresh-"))
    added, candidate, stopped, rerere_resolved = False, "", set(), set()
    try:
        _git(repo, "worktree", "add", "--quiet", "--detach", str(scratch), published)
        added = True
        done = _git(scratch, "rebase", "--rebase-merges", "--reapply-cherry-picks", "--empty=stop",
                    "--no-update-refs", "--no-autostash", "--onto", upstream, base, check=False, timeout=1800)
        while done.returncode:
            conflicts = _git(scratch, "diff", "--name-only", "--diff-filter=U").stdout.strip()
            head = _git(scratch, "rev-parse", "--verify", "REBASE_HEAD", check=False)
            if conflicts or head.returncode:
                raise RefreshError(f"rebase conflict: {(done.stderr or done.stdout).strip()}")
            unstaged = _git(scratch, "diff", "--name-only").stdout.strip()
            untracked = _git(scratch, "ls-files", "--others", "--exclude-standard").stdout.strip()
            staged = _git(scratch, "diff", "--cached", "--name-only").stdout.strip()
            if unstaged or untracked:
                raise RefreshError(f"rebase conflict: {(done.stderr or done.stdout).strip()}")
            if staged:
                # rerere.autoupdate can resolve a known semantic conflict and
                # stage the result while `git rebase` still exits non-zero.
                # Continue that proven resolution; a clean stop is the only
                # state that represents an empty replay.
                rerere_resolved.add(head.stdout.strip())
                done = _git(scratch, "rebase", "--continue", check=False, timeout=1800)
                continue
            stopped.add(head.stdout.strip())
            done = _git(scratch, "rebase", "--skip", check=False, timeout=1800)
        candidate = _git(scratch, "rev-parse", "HEAD").stdout.strip()
        if _git(scratch, "merge-base", "--is-ancestor", upstream, candidate, check=False).returncode:
            raise RefreshError("candidate does not contain fetched upstream")
        _assert_tree(scratch, candidate, assertions)
        _run_checks(scratch, checks)
    finally:
        cleanup_error, primary_error = None, sys.exc_info()[1]
        if added:
            _git(scratch, "rebase", "--abort", check=False)
            removed = _git(repo, "worktree", "remove", "--force", str(scratch), check=False)
            cleanup_error = (removed.stderr or removed.stdout).strip() if removed.returncode else None
        if scratch.exists() and not cleanup_error:
            shutil.rmtree(scratch)
        if cleanup_error:
            raise RefreshError(f"{primary_error}; scratch cleanup failed: {cleanup_error}" if primary_error else f"scratch cleanup failed: {cleanup_error}") from primary_error
    return candidate, stopped, rerere_resolved
def _push_with_lease(repo: Path, remote: str, ref: str, captured: str, candidate: str) -> bool:
    current = _remote_head(repo, remote, ref)
    if current == candidate: return False
    if current != captured: raise RefreshError(f"lease rejected: remote moved to {current}")
    pushed = _git(repo, "push", "--porcelain", f"--force-with-lease={ref}:{captured}",
                  remote, f"{candidate}:{ref}", check=False)
    if pushed.returncode:
        if _remote_head(repo, remote, ref) == candidate:
            return False
        raise RefreshError(f"lease rejected: {(pushed.stderr or pushed.stdout).strip()}")
    if _remote_head(repo, remote, ref) != candidate: raise RefreshError("push reported success but remote does not equal candidate")
    return True
def compose(repo: str | Path, *, upstream_remote: str = "upstream", published_remote: str = "origin",
            upstream_ref: str = "refs/heads/main", published_ref: str = "refs/heads/fork-integration",
            dry_run: bool = True, checks: Sequence[Sequence[str]] = (), upstream_sha: str | None = None,
            upstream_cutoff_hour: int | None = None, now_utc: datetime | None = None,
            merge_assertions: dict[str, tuple[TreeAssertion, ...]] | None = None) -> dict:
    repo = Path(repo).resolve()
    fetched = _fetch(repo, upstream_remote, upstream_ref)
    upstream, cutoff = _select_upstream(repo, fetched, upstream_sha, upstream_cutoff_hour, now_utc)
    published = _fetch(repo, published_remote, published_ref)
    bases = _lines(repo, "merge-base", "--all", upstream, published)
    if len(bases) != 1: raise RefreshError(f"expected one merge base, found {len(bases)}")
    base = bases[0]
    result = {"fetched_upstream_head": fetched, "upstream_sha": upstream, "upstream_cutoff": cutoff,
              "captured_upstream": upstream, "captured_published": published, "merge_base": base,
              "candidate": published, "status": "already_current", "dispositions": [], "pushed": False}
    if not _git(repo, "merge-base", "--is-ancestor", upstream, published, check=False).returncode:
        return result
    rows = [line.split() for line in _lines(repo, "rev-list", "--reverse", "--topo-order", "--parents", published, f"^{base}")]
    commits, parents = [row[0] for row in rows], {row[0]: row[1:] for row in rows}
    merges = [sha for sha in commits if len(parents[sha]) > 1]
    merge_set = set(merges)
    registry = MERGE_ASSERTIONS if merge_assertions is None else merge_assertions
    subject_lines = _lines(repo, "log", "--no-walk=unsorted", "--format=%H%x00%s", *merges) if merges else []
    subjects = dict(line.split("\x00", 1) for line in subject_lines)
    resolved = {sha: registry.get(sha) or registry.get(subjects[sha]) for sha in merges}
    missing = [sha for sha in merges if resolved[sha] is None]
    if missing: raise RefreshError(f"uncharacterized merge(s): {', '.join(missing)}")
    assertions = tuple(item for sha in merges for item in resolved[sha])
    _assert_tree(repo, published, assertions)
    non_merges = [sha for sha in commits if sha not in merge_set]
    before = _cherry(repo, upstream, published, base)
    if set(before) != set(non_merges): raise RefreshError("captured non-merge range lacks stable patch dispositions")
    candidate, stopped, rerere_resolved = _scratch_rebase(repo, published, upstream, base, assertions, checks)
    after = _cherry(repo, candidate, published, base)
    failed, represented = [sha for sha in non_merges if after.get(sha) != "-"], {}
    kill_merge = next((sha for sha in merges if subjects[sha] == KILL_ALL_MERGE), None)
    kill_side = parents[kill_merge][1] if kill_merge else None
    if kill_side in failed: represented[kill_side] = kill_merge
    failed = [sha for sha in failed if sha not in represented and sha not in rerere_resolved]
    if failed: raise RefreshError(f"fork changes missing from candidate: {', '.join(failed)}")
    result["candidate"] = candidate
    dispositions = {sha: {"commit": sha, "status": "represented_by_merge_assertion" if sha in represented
        else "rerere_resolved" if sha in rerere_resolved else "empty" if sha in stopped else "replayed",
        "represented_by": represented.get(sha),
        "preexisting_patch": before[sha] == "-"} for sha in non_merges}
    result["dispositions"] = [{"commit": sha, "status": "characterized_merge_assertion",
        "assertion_count": len(resolved[sha])} if sha in merge_set else dispositions[sha] for sha in commits]
    if dry_run:
        result["status"] = "candidate_ready"
        return result
    result["pushed"] = _push_with_lease(repo, published_remote, published_ref, published, candidate)
    result["status"] = "published" if result["pushed"] else "already_published"
    return result
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="."); parser.add_argument("--upstream-remote", default="upstream")
    parser.add_argument("--published-remote", default="origin")
    parser.add_argument("--upstream-ref", default="refs/heads/main")
    parser.add_argument("--published-ref", default="refs/heads/fork-integration")
    parser.add_argument("--publish", action="store_true"); parser.add_argument("--check", action="append", default=[])
    parser.add_argument("--wake-agent-on-failure", action="store_true")
    parser.add_argument("--upstream-sha")
    parser.add_argument("--upstream-cutoff-hour", type=int)
    args = parser.parse_args(argv)
    try:
        decoded_checks = [json.loads(value) for value in args.check]
        if any(not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command) for command in decoded_checks): raise RefreshError("each --check must be a non-empty JSON array of strings")
        result = compose(args.repo, upstream_remote=args.upstream_remote, published_remote=args.published_remote,
            upstream_ref=args.upstream_ref, published_ref=args.published_ref, dry_run=not args.publish,
            upstream_sha=args.upstream_sha, upstream_cutoff_hour=args.upstream_cutoff_hour,
            checks=tuple(tuple(command) for command in decoded_checks))
        if args.wake_agent_on_failure:
            result["wakeAgent"] = False
        print(json.dumps(result, sort_keys=True))
        return 0
    except (RefreshError, subprocess.TimeoutExpired, OSError, TypeError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        return 1
if __name__ == "__main__":
    raise SystemExit(main())
