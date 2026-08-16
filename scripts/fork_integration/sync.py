#!/usr/bin/env python
"""U2 sync boundary (KTD2, R14): the only path by which the fork-integration
release system's operational copies at ``%HERMES_HOME%\\scripts`` may change.

Two attributable entry points, both implemented here:

  1. Post-publish sync -- ``hermes-integration-release-windows.py`` calls
     ``sync()`` immediately after a successful publish of the exact
     ``fork-integration`` SHA containing these files (see that script's
     ``sync_operational_copies()``).
  2. Provisional/break-glass deploy -- ``python sync.py deploy --from-sha
     <committed SHA> --reason <text>`` for an in-window investigator or an
     authorized operator when the publish path itself is broken. The stamp
     records ``provisional: true``; the next successful publish re-stamps.

Never mid-run, never on a tick, never a direct edit. ``verify()`` compares
the operational copies against the published (or provisionally deployed)
git tree's blob hashes via a repo clone -- the embedded stamp only names
*which* SHA to check; it is never itself the authority for whether a file
is correct. A mismatch at run start (see release.py's
``integration_scripts_integrity_check``) permits exactly one action --
re-sync from a verified SHA -- never a bypass flag.

TRACKED_SET decision (this module's docstring is the record of record for
it, per the U2 runbook):

  - The five files named explicitly by the U2 plan unit are tracked:
    ``hermes-integration-release-windows.py``,
    ``hermes-release-failure-investigator.py``,
    ``hermes-integration-manifest.json``,
    ``test_hermes_integration_release_windows.py``, ``overdue_check.py``.
  - ``proposals.py`` and ``fork-integration-blocklist.json`` (U6) are
    tracked for the same reason as ``sync.py`` below: the release script
    loads ``proposals.py`` as a sibling file (``_proposals_module()``) to
    detect churn candidates and park pins, and reads the blocklist beside
    the manifest so a rejected patch-id can never be re-absorbed
    operationally. Both are approval-mutated (``proposals.py approve``
    appends blocklist entries), which makes them exactly the kind of file
    that must arrive through an attributable sync boundary rather than by
    hand. NOTE the deploy ordering this implies: the first operational sync
    carrying them must come from a published SHA that contains them --
    until then the operational generation keeps running its own older
    ``sync.py`` with the older tracked set, which is unaffected.
  - ``sync.py`` (this file) is ALSO tracked -- a deviation from the plan's
    literal enumeration, made deliberately: ``release.py`` loads this
    module as a sibling file via ``importlib`` (the operational directory
    is flat, not a package -- see ``_sync_module()`` there) for BOTH the
    run-start integrity gate and the post-publish hook. If this file were
    not itself deployed through the same attributable sync boundary it
    enforces, it would have to be placed operationally by hand forever,
    which directly contradicts R14 ("operational copies update only
    through the U2 sync entry points"). Bootstrapping this file into an
    environment that has never run a sync before is a one-time manual
    step (or an operator-run ``deploy``), exactly like every other tracked
    file's first arrival.
  - ``release.py``/``investigator.py``/``__init__.py`` (the in-repo
    importlib shims) and ``README.md`` are EXCLUDED. They exist solely so
    pytest, running from the repo root with normal dotted imports, can
    reach the dash-named scripts as
    ``from scripts.fork_integration.release import mod`` -- a repo-only
    concern. Nothing in the operational directory ever imports
    ``scripts.fork_integration.release`` as a package (the scheduler and
    the investigator both invoke ``hermes-integration-release-windows.py``
    directly as a script); the shims and the README would be inert dead
    weight there, and giving them presence in TRACKED_SET would only
    create a second, meaningless place for drift to be "detected".
  - The canary manifest path used by U11's forced-failure proof (a future
    ``--canary-manifest <path>`` release.py flag) is explicitly OUTSIDE
    this set on purpose: the canary needs to inject a bad manifest without
    tripping the integrity gate that this module enforces.

Staged atomic deploy mechanics (``sync()``):

  1. Resolve ``from_sha`` to a real commit in ``repo_dir`` (fails closed on
     an unresolvable ref).
  2. Stage every ``TRACKED_SET`` file's blob
     (``git -C repo_dir show <sha>:scripts/fork_integration/<name>``) into
     a temp directory that is a *sibling* of ``dest_dir``'s files (so the
     later ``os.replace`` calls are same-filesystem, and therefore atomic
     per file). Each staged file is re-read and re-hashed immediately
     after being written, and compared against the in-memory blob hash --
     this catches a truncated/corrupted *write*, independent of whatever
     already validated the *read*.
  3. Only once every tracked file is staged and independently verified
     does the commit phase begin: one ``os.replace`` per file, in
     ``TRACKED_SET`` order.
  4. The stamp is written strictly last, after every file has landed --
     it is the single commit point for the whole generation.

A failure at any point during staging (step 1-2) leaves ``dest_dir``
completely untouched: nothing has been replaced yet. A failure during the
commit phase (step 3) -- e.g. the process is killed between two
``os.replace`` calls -- can leave ``dest_dir`` in a torn state where some
tracked files reflect the new generation and others still reflect the old
one. The stamp is the safety net for exactly this case: because it is
never rewritten until every replace has succeeded, it keeps naming the OLD
sha, and the next ``verify()`` call will correctly detect a hash mismatch
for whichever file(s) the interrupted commit phase actually reached --
never silently accepting the torn state as a valid generation. Recovery in
either case is the same single action described above: re-sync.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# ── Tracked file set (see module docstring for the inclusion/exclusion
# rationale) ────────────────────────────────────────────────────────────
TRACKED_SET: tuple[str, ...] = (
    "hermes-integration-release-windows.py",
    "hermes-release-failure-investigator.py",
    "hermes-integration-manifest.json",
    "test_hermes_integration_release_windows.py",
    "overdue_check.py",
    "sync.py",
    "proposals.py",
    "fork-integration-blocklist.json",
)

MANIFEST_FILENAME = "hermes-integration-manifest.json"
BLOCKLIST_FILENAME = "fork-integration-blocklist.json"
assert MANIFEST_FILENAME in TRACKED_SET
assert BLOCKLIST_FILENAME in TRACKED_SET

# Path, relative to a repo_dir's root, holding the tracked files -- matches
# this file's own location in this repo (KTD1: scripts/fork_integration/).
REPO_TRACKED_SUBDIR = "scripts/fork_integration"

SYNC_STAMP_FILENAME = "hermes-integration-sync-stamp.json"
_DEST_LOCK_FILENAME = ".fork-integration-sync.lock"


class SyncError(RuntimeError):
    """Raised by ``sync()``/``restamp_manifest()`` when a staged deploy or
    stamp amendment cannot safely complete.

    ``verify()`` never raises this: it exists to be checked/branched on by
    a caller (the release script's run-start gate, or this module's own
    CLI), so it always returns a structured ``{"ok": False, ...}`` dict for
    every expected failure mode instead.
    """


# ── git plumbing helpers ─────────────────────────────────────────────────


def _tracked_path(name: str) -> str:
    return f"{REPO_TRACKED_SUBDIR}/{name}"


def _resolve_commit(repo_dir: Path, ref: str) -> str:
    """Resolve ``ref`` to a full commit id inside ``repo_dir``, fail closed."""
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, text=True,
    )
    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        detail = (result.stderr or "").strip()
        raise SyncError(f"source sha does not resolve to a commit in {repo_dir}: {ref!r} ({detail})")
    return sha


def _read_blob(repo_dir: Path, sha: str, name: str) -> bytes:
    """Read one tracked file's exact committed bytes from the git tree.

    Captured without ``text=True`` deliberately: several tracked files
    (the CRLF-preserved manifest, in particular) are marked ``-text`` in
    this repo's ``.gitattributes`` specifically so git never rewrites their
    line endings. Reading raw bytes here keeps that guarantee end to end.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "show", f"{sha}:{_tracked_path(name)}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise SyncError(f"could not read {name!r} from the git tree at {sha}: {stderr}")
    return result.stdout


# ── dest-directory exclusion ─────────────────────────────────────────────


@contextmanager
def _dest_directory_exclusion(dest_dir: Path) -> Iterator[None]:
    """Advisory exclusion held over ``dest_dir`` for a sync/restamp commit
    phase (U2 Approach: "Exclusion held over the scripts directory during
    the swap").

    Deliberately simple relative to ``release.py``'s ``exclusive_lock()``:
    no holder-identity/stale-reclaim protocol. A sync or a manifest
    restamp is short-lived and rare (once per publish, or an occasional
    manual break-glass/approval action); a contended lock just refuses
    immediately rather than reclaiming.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    lock_path = dest_dir / _DEST_LOCK_FILENAME
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SyncError(f"another sync/deploy is already in progress: {lock_path}")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _write_stamp(dest_dir: Path, stamp: dict[str, Any]) -> None:
    """Write the stamp via temp-file + os.replace -- the same same-directory
    atomic-rename discipline as every tracked file, so the stamp write
    itself can never leave a half-written stamp behind."""
    fd, tmp_name = tempfile.mkstemp(prefix=".fork-integration-sync-stamp-", dir=dest_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(stamp, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, str(dest_dir / SYNC_STAMP_FILENAME))
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


# ── public API ────────────────────────────────────────────────────────────


def sync(
    from_sha: str,
    repo_dir: Path | str,
    dest_dir: Path | str,
    *,
    provisional: bool = False,
    reason: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Staged, atomic, tree-verified deploy of ``TRACKED_SET`` from
    ``repo_dir``'s git history at ``from_sha`` into ``dest_dir``.

    Returns the written stamp dict on success. Raises ``SyncError`` on any
    failure; a failure during staging leaves ``dest_dir`` completely
    untouched, and the stamp (written strictly last) is never rewritten
    unless every tracked file was already replaced successfully. See the
    module docstring for the full staged-deploy contract and the torn-state
    note for failures mid commit-phase.
    """
    repo_dir = Path(repo_dir)
    dest_dir = Path(dest_dir)
    resolved_sha = _resolve_commit(repo_dir, from_sha)

    with _dest_directory_exclusion(dest_dir):
        staging_dir = Path(tempfile.mkdtemp(prefix=".fork-integration-sync-staging-", dir=dest_dir))
        try:
            staged: dict[str, tuple[Path, str]] = {}
            for name in TRACKED_SET:
                content = _read_blob(repo_dir, resolved_sha, name)
                digest = hashlib.sha256(content).hexdigest()
                staged_path = staging_dir / name
                staged_path.write_bytes(content)
                # Re-read + re-hash from disk: verifies the *write*, not
                # merely trusting the in-memory bytes we just computed the
                # digest from.
                on_disk_digest = hashlib.sha256(staged_path.read_bytes()).hexdigest()
                if on_disk_digest != digest:
                    raise SyncError(f"staged file hash mismatch immediately after write: {name}")
                staged[name] = (staged_path, digest)

            # Commit phase: nothing above touched dest_dir. From here on,
            # each os.replace is individually atomic, but the set of them
            # is not -- see the module docstring's torn-state note.
            for name in TRACKED_SET:
                staged_path, _digest = staged[name]
                os.replace(str(staged_path), str(dest_dir / name))

            stamp = {
                "source_sha": resolved_sha,
                "files": {name: digest for name, (_p, digest) in staged.items()},
                "provisional": bool(provisional),
                "reason": reason,
                "actor": actor,
                "synced_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_stamp(dest_dir, stamp)
            return stamp
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)


def verify(dest_dir: Path | str, repo_dir: Path | str) -> dict[str, Any]:
    """Resolve the stamped ``source_sha`` and compare every tracked file's
    on-disk content against the git TREE at that sha -- never against the
    stamp's own recorded per-file hashes, which are informational only.

    This is what makes a consistently-rewritten file+stamp still fail: an
    out-of-band editor can make a dest file and its stamp entry agree with
    each other, but cannot make them agree with what is actually committed
    in ``repo_dir`` at the stamped sha.

    Never raises for an expected failure mode (no stamp, corrupt stamp,
    unreachable sha, a tracked file missing from disk or from the tree,
    hash mismatch) -- always returns a structured dict with ``"ok"`` and
    enough detail to act on. Callers that must fail closed do so
    themselves.
    """
    dest_dir = Path(dest_dir)
    repo_dir = Path(repo_dir)
    stamp_path = dest_dir / SYNC_STAMP_FILENAME

    if not stamp_path.is_file():
        return {"ok": False, "reason": "no_stamp", "stamp_path": str(stamp_path)}
    try:
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": "corrupt_stamp", "error": str(exc), "stamp_path": str(stamp_path)}
    if not isinstance(stamp, dict):
        return {"ok": False, "reason": "corrupt_stamp", "error": "stamp is not a JSON object", "stamp_path": str(stamp_path)}

    source_sha = stamp.get("source_sha")
    if not isinstance(source_sha, str) or not source_sha:
        return {"ok": False, "reason": "corrupt_stamp", "error": "stamp has no source_sha", "stamp_path": str(stamp_path)}

    try:
        resolved_sha = _resolve_commit(repo_dir, source_sha)
    except SyncError as exc:
        return {"ok": False, "reason": "unreachable_sha", "source_sha": source_sha, "error": str(exc)}

    problems: list[dict[str, str]] = []
    checked: dict[str, str] = {}
    for name in TRACKED_SET:
        try:
            tree_content = _read_blob(repo_dir, resolved_sha, name)
        except SyncError as exc:
            problems.append({"file": name, "reason": "missing_in_tree", "detail": str(exc)})
            continue
        expected_digest = hashlib.sha256(tree_content).hexdigest()
        dest_path = dest_dir / name
        if not dest_path.is_file():
            problems.append({"file": name, "reason": "missing_file"})
            continue
        actual_digest = hashlib.sha256(dest_path.read_bytes()).hexdigest()
        checked[name] = actual_digest
        if actual_digest != expected_digest:
            problems.append({
                "file": name,
                "reason": "hash_mismatch",
                "expected_sha256": expected_digest,
                "actual_sha256": actual_digest,
            })

    return {
        "ok": not problems,
        "source_sha": resolved_sha,
        "stamped_source_sha": source_sha,
        "files": checked,
        "problems": problems,
        "stamp_path": str(stamp_path),
    }


def restamp_file(dest_dir: Path | str, repo_dir: Path | str, name: str, approved_sha256: str) -> dict[str, Any]:
    """Narrow attestation primitive for the U6 approval path (KTD2 Approach
    step 5: approvals commit to ``fork-integration`` and re-stamp the
    approval-mutated operational file in place).

    Generalized from the original manifest-only primitive because a U6
    approval mutates TWO tracked files -- the manifest and the blocklist --
    and both must be attested by the same mechanism; ``restamp_manifest()``
    remains as a thin back-compat wrapper.

    Does NOT copy any file content, and does NOT touch ``source_sha`` or
    any other tracked file's stamped hash -- it only asserts "``name``
    currently on disk is byte-for-byte what was approved" and records that
    single fact in the stamp. Recomputes the file's sha256 fresh from
    ``dest_dir`` every call; never trusts a caller-supplied hash for
    anything except the equality check itself.

    Refuses (raises ``SyncError``) unless:
      - ``name`` is a tracked file (an untracked name has no stamp entry to
        amend and would be a silent no-op);
      - ``repo_dir`` looks like a real git working tree (a basic
        environment sanity check -- an approval always runs against a real
        repo clone in practice);
      - a prior stamp already exists in ``dest_dir`` (there must be a
        prior generation to amend; this is not a bootstrap entry point);
      - the file exists in ``dest_dir``; and
      - its current sha256 equals ``approved_sha256`` exactly.

    Any other delta -- one byte different from what was approved -- fails
    closed rather than silently re-stamping something nobody reviewed.
    """
    dest_dir = Path(dest_dir)
    repo_dir = Path(repo_dir)
    if name not in TRACKED_SET:
        raise SyncError(f"refusing to restamp an untracked file: {name!r}")

    probe = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, text=True,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        raise SyncError(f"repo_dir is not a git working tree: {repo_dir}")

    stamp_path = dest_dir / SYNC_STAMP_FILENAME
    if not stamp_path.is_file():
        raise SyncError(f"no prior sync stamp to amend: {stamp_path}")
    try:
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"existing stamp is unreadable: {exc}") from exc
    if not isinstance(stamp, dict):
        raise SyncError("existing stamp is not a JSON object")

    file_path = dest_dir / name
    if not file_path.is_file():
        raise SyncError(f"{name} is missing from dest_dir: {file_path}")
    actual_sha256 = hashlib.sha256(file_path.read_bytes()).hexdigest()
    if actual_sha256 != approved_sha256:
        raise SyncError(
            f"{name} on disk does not match the approved hash; refusing to restamp "
            f"(expected={approved_sha256}, actual={actual_sha256})"
        )

    files = dict(stamp.get("files") or {})
    files[name] = actual_sha256
    new_stamp = {**stamp, "files": files}

    with _dest_directory_exclusion(dest_dir):
        _write_stamp(dest_dir, new_stamp)
    return new_stamp


def restamp_manifest(dest_dir: Path | str, repo_dir: Path | str, approved_fragment_sha256: str) -> dict[str, Any]:
    """Back-compat wrapper: restamp the manifest specifically.

    Kept so the original ``restamp-manifest`` CLI verb and its callers keep
    working unchanged now that :func:`restamp_file` carries the mechanics.
    """
    return restamp_file(dest_dir, repo_dir, MANIFEST_FILENAME, approved_fragment_sha256)


# ── CLI ────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sync.py",
        description="U2 sync boundary: staged, atomic, tree-verified deploy of the "
        "fork-integration release system's tracked files to their operational copies.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    deploy = subparsers.add_parser("deploy", help="Stage and atomically deploy TRACKED_SET from a git tree.")
    deploy.add_argument("--from-sha", required=True, help="Commit-ish to deploy from (resolved in --repo).")
    deploy.add_argument("--repo", required=True, type=Path, help="Git clone/worktree containing scripts/fork_integration/.")
    deploy.add_argument("--dest", required=True, type=Path, help="Operational directory to deploy into.")
    deploy.add_argument("--provisional", action="store_true", help="Mark this deploy break-glass/provisional (KTD2).")
    deploy.add_argument("--reason", default=None, help="Required context for a provisional deploy; recorded in the stamp.")
    deploy.add_argument("--actor", default=None, help="Identity performing this deploy; recorded in the stamp.")

    verify_parser = subparsers.add_parser("verify", help="Verify --dest's tracked files against --repo's tree at the stamped sha.")
    verify_parser.add_argument("--repo", required=True, type=Path)
    verify_parser.add_argument("--dest", required=True, type=Path)

    restamp = subparsers.add_parser(
        "restamp-manifest",
        help="Re-stamp only the manifest entry after an out-of-band approved edit (U6 hook).",
    )
    restamp.add_argument("--repo", required=True, type=Path)
    restamp.add_argument("--dest", required=True, type=Path)
    restamp.add_argument(
        "--approved-fragment", required=True, dest="approved_fragment",
        help="sha256 the operational manifest's current on-disk content must equal.",
    )

    restamp_any = subparsers.add_parser(
        "restamp-file",
        help="Re-stamp one tracked file's entry after an out-of-band approved edit (U6 hook).",
    )
    restamp_any.add_argument("--repo", required=True, type=Path)
    restamp_any.add_argument("--dest", required=True, type=Path)
    restamp_any.add_argument("--name", required=True, help=f"Tracked file name; one of {', '.join(TRACKED_SET)}.")
    restamp_any.add_argument(
        "--approved-sha256", required=True, dest="approved_sha256",
        help="sha256 the operational file's current on-disk content must equal.",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "deploy":
            if args.provisional and not args.reason:
                raise SyncError("--provisional requires --reason")
            stamp = sync(
                args.from_sha, args.repo, args.dest,
                provisional=args.provisional, reason=args.reason, actor=args.actor,
            )
            print(json.dumps({"ok": True, "command": "deploy", **stamp}, indent=2, sort_keys=True))
            return 0
        if args.command == "verify":
            result = verify(args.dest, args.repo)
            print(json.dumps({"command": "verify", **result}, indent=2, sort_keys=True))
            return 0 if result.get("ok") else 2
        if args.command == "restamp-manifest":
            stamp = restamp_manifest(args.dest, args.repo, args.approved_fragment)
            print(json.dumps({"ok": True, "command": "restamp-manifest", **stamp}, indent=2, sort_keys=True))
            return 0
        if args.command == "restamp-file":
            stamp = restamp_file(args.dest, args.repo, args.name, args.approved_sha256)
            print(json.dumps({"ok": True, "command": "restamp-file", **stamp}, indent=2, sort_keys=True))
            return 0
    except SyncError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 2
    raise AssertionError(f"unhandled command: {args.command!r}")  # argparse enforces the choice set


if __name__ == "__main__":
    sys.exit(main())
