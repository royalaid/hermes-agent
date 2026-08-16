#!/usr/bin/env python
"""Provenance derivation, JSONL history, and report for fork-integration (U7).

Answers "did carried change X get merged upstream, and in what form?" from
ground truth on every run (R7): the manifest's declared patch identities,
live git ancestry/patch-identity search against the tracked upstream ref, and
a best-effort ``gh pr view`` lookup for components sourced from a named fork
branch. Nothing here gates a release (KTD7) -- ``derive()``/``report()`` only
inform; the manifest validators in ``hermes-integration-release-windows.py``
remain the sole enforcement layer.

State vocabulary (R5), one of:
  - ``absorbed-verbatim``   an ancestor of ``upstream_ref`` has the exact
                             same subject AND the pin's own stable patch-id.
  - ``absorbed-modified``   an ancestor of ``upstream_ref`` has the same
                             subject and a patch-id from the pin's *extra*
                             ``accepted_output_patch_ids`` (a recorded
                             modified-but-equivalent form), OR the pin
                             declares a ``reviewed_replacement`` (the fork
                             itself carries the modified/replacement form).
  - ``superseded``          the pin's own identity, or its declared
                             replacement, has been recorded in the blocklist
                             file (R3) -- its natural absorption path is
                             blocked pending reconciliation.
  - ``pr-open``             the pin's component is sourced from a named fork
                             branch (``source_ref`` starting with ``fork/``)
                             and a best-effort ``gh pr view`` on that branch
                             reports an open PR.
  - ``private-only``        none of the above: no upstream match, no open
                             PR (or PR status is unknown -- see below).
  - ``excluded_until_*``    passthrough for a manifest-recorded exclusion
                             marker. The manifest's current schema (3) does
                             not carry such a field anywhere in
                             ``upstream_foundations[].patches[]`` or
                             ``components[].patches[]`` -- this branch is
                             forward-compatible and untested against the
                             live manifest today; state derivation otherwise
                             covers only the five core states above.

``gh`` degradation (decision 2): ANY failure (missing binary, auth, rate
limit, network) degrades to ``evidence={"pr": "unknown-offline"}`` and NEVER
raises. At most one ``gh`` call is made per component, and none at all when
``gh_enabled=False``.

Persistence is intentionally simple (KTD4): an append-only JSONL history
(one line per run, one record per carried patch) plus a generated markdown
report -- no database. ``append_history`` never rewrites or prunes a prior
line, so a lineage's current state is always answerable even if it was last
touched months ago (R6).

Retirement candidates bridge to U6 (``proposals.py``): a pin absorbed
verbatim for several consecutive runs, whose absorbing candidate is still an
ancestor of the live upstream tip, is worth retiring from the manifest
through that state machine. This module only COMPUTES and reports the
candidate list (``retirement_candidates()`` below); it never mutates the
manifest or invokes proposal machinery itself. The bridge that turns a
candidate pin_id into an actual retire-pin proposal --
``generate_retirement_proposals()`` -- lives in the release script (the one
module already permitted to import both this module and ``proposals.py``),
calling ``proposals.generate_or_refresh_retirement()``. Deliberate: this
module still does not import ``proposals.py``, keeping its own git-layer
tests (see the module docstring above) independent of a concurrently-edited
state machine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Vocabulary (R5)
# ---------------------------------------------------------------------------

CORE_STATES = (
    "absorbed-verbatim",
    "absorbed-modified",
    "superseded",
    "pr-open",
    "private-only",
)
EXCLUDED_STATE_PREFIX = "excluded_until_"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST_PATH = SCRIPT_DIR / "hermes-integration-manifest.json"
DEFAULT_BLOCKLIST_PATH = SCRIPT_DIR / "fork-integration-blocklist.json"
_HISTORY_ENV_OVERRIDE = "FORK_INTEGRATION_LEDGER_HISTORY_PATH"


def _hermes_home() -> Path:
    """Mirror overdue_check.py's env-var > platform-default HERMES_HOME
    resolution (a deliberate read-only mirror; this module also imports
    nothing from the package so it stays usable outside a full checkout)."""
    env_value = os.environ.get("HERMES_HOME", "").strip()
    if env_value:
        return Path(env_value)
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".local" / "share" / "hermes"


def default_history_path() -> Path:
    """Resolve the JSONL history path.

    Priority: the ``FORK_INTEGRATION_LEDGER_HISTORY_PATH`` env override, then
    ``HERMES_HOME``-derived default. Callers that must not touch the real
    location (all tests) should pass an explicit ``history_path`` argument
    rather than relying on this default.
    """
    env_value = os.environ.get(_HISTORY_ENV_OVERRIDE, "").strip()
    if env_value:
        return Path(env_value)
    return _hermes_home() / "review-artifacts" / "fork-integration-history" / "provenance-history.jsonl"


# ---------------------------------------------------------------------------
# Git plumbing (stdlib subprocess only -- deliberately self-contained; this
# module does not import the release script/sync/proposals modules a
# concurrent unit is actively editing).
# ---------------------------------------------------------------------------


def _windows_subprocess_kwargs() -> dict[str, Any]:
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": startupinfo,
    }


def git(*args: str, cwd: Path, timeout: int = 120, check: bool = True) -> str:
    """Minimal git runner. ``check=False`` never raises on a nonzero exit;
    the caller inspects the (possibly empty) stdout instead."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        **_windows_subprocess_kwargs(),
    )
    if check and result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def stable_patch_id(commit: str, *, cwd: Path) -> str | None:
    """Git's whitespace-insensitive patch identity for one commit.

    Returns ``None`` (never raises) when it cannot be computed -- missing
    commit, unreachable object, shallow clone, timeout. Provenance
    derivation must keep answering for every *other* pin even when one
    commit's identity is currently uncomputable.
    """
    try:
        show = subprocess.run(
            ["git", "show", "--format=", "--binary", commit],
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=120,
            **_windows_subprocess_kwargs(),
        )
        if show.returncode:
            return None
        result = subprocess.run(
            ["git", "patch-id", "--stable"],
            input=show.stdout,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=120,
            **_windows_subprocess_kwargs(),
        )
        if result.returncode or not result.stdout.strip():
            return None
        return result.stdout.split()[0]
    except (OSError, subprocess.TimeoutExpired):
        return None


def _same_subject_candidates(subject: str, upstream_ref: str, *, cwd: Path) -> list[str]:
    """Commits reachable from ``upstream_ref`` with an EXACTLY equal subject.

    Mirrors the release script's ``git log <upstream_ref> --format=%H
    --fixed-strings --grep=<subject>`` candidate search, extended to fetch
    the subject back (``%x00%s``) so results are filtered to exact equality
    rather than trusting ``--grep``'s substring match (R1's "not a
    substring" rule, applied here too since a false-positive candidate would
    otherwise misreport provenance).
    """
    try:
        output = git(
            "log",
            upstream_ref,
            "--format=%H%x00%s",
            "--fixed-strings",
            f"--grep={subject}",
            cwd=cwd,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        return []
    candidates: list[str] = []
    for line in output.splitlines():
        if "\x00" not in line:
            continue
        commit, _, candidate_subject = line.partition("\x00")
        if commit and candidate_subject == subject:
            candidates.append(commit)
    return candidates


def is_ancestor(commit: str, ref: str, *, cwd: Path) -> bool:
    """Best-effort ancestry check. Degrades to ``False`` on any failure --
    never raises; retirement candidacy is additive/informational, not a
    gate."""
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, ref],
            cwd=cwd,
            timeout=60,
            **_windows_subprocess_kwargs(),
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _gh_pr_view(branch: str, *, cwd: Path, gh_exe: str = "gh") -> dict[str, Any]:
    """Best-effort ``gh pr view <branch> --json state,url,mergedAt``.

    ANY failure (missing binary, auth, rate limit, non-JSON output, timeout)
    degrades to ``{"pr": "unknown-offline"}`` -- never raises (decision 2).
    """
    try:
        result = subprocess.run(
            [gh_exe, "pr", "view", branch, "--json", "state,url,mergedAt"],
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
            **_windows_subprocess_kwargs(),
        )
        if result.returncode != 0:
            return {"pr": "unknown-offline"}
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            return {"pr": "unknown-offline"}
        return {
            "pr_state": payload.get("state"),
            "pr_url": payload.get("url"),
            "pr_merged_at": payload.get("mergedAt"),
        }
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"pr": "unknown-offline"}


# ---------------------------------------------------------------------------
# Manifest / blocklist loading (read-only; no schema re-validation here --
# that stays release.py's job. Malformed input fails loudly rather than
# silently under-reporting pins.)
# ---------------------------------------------------------------------------


def _load_manifest(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"fork-integration manifest is not an object: {path}")
    return payload


def _load_blocklist(path: str | Path) -> dict[str, dict[str, Any]]:
    """``{stable_patch_id: entry}`` map. Tolerates an absent file -- R3's
    blocklist is written by U6 (``proposals.py``), which may not have run,
    or may not exist yet, in any given checkout."""
    blocklist_path = Path(path)
    if not blocklist_path.exists():
        return {}
    try:
        payload = json.loads(blocklist_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries: Iterable[Any]
    if isinstance(payload, dict):
        entries = payload.get("entries", [])
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = []
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("patch_id"), str):
            result[entry["patch_id"]] = entry
    return result


def _exclusion_state(patch: dict[str, Any], parent: dict[str, Any]) -> str | None:
    """Forward-compatible passthrough for a manifest-recorded exclusion
    marker (R5's ``excluded_until_*`` vocabulary). Schema 3 -- the
    manifest's current schema -- has no such field on
    ``upstream_foundations[].patches[]`` or ``components[].patches[]``; this
    branch is therefore never exercised by the live manifest today. Stated
    honestly rather than silently assumed correct."""
    for source in (patch, parent):
        for key in ("status", "excluded_until", "state"):
            value = source.get(key)
            if isinstance(value, str) and value.startswith(EXCLUDED_STATE_PREFIX):
                return value
    return None


# ---------------------------------------------------------------------------
# Per-patch state derivation
# ---------------------------------------------------------------------------


def _derive_patch(
    *,
    kind: str,
    parent_id: str,
    parent: dict[str, Any],
    patch: dict[str, Any],
    repo_dir: Path,
    upstream_ref: str,
    blocklist: dict[str, dict[str, Any]],
    pr_evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    commit = patch["commit"]
    own_id = patch["stable_patch_id"]
    subject = patch["subject"]
    pin_id = f"{kind}:{parent_id}:{commit}"

    def _record(state: str, evidence: dict[str, Any]) -> dict[str, Any]:
        return {
            "pin_id": pin_id,
            "kind": kind,
            "commit": commit,
            "stable_patch_id": own_id,
            "subject": subject,
            "state": state,
            "evidence": evidence,
        }

    exclusion = _exclusion_state(patch, parent)
    if exclusion is not None:
        return _record(exclusion, {"reason": "manifest-recorded exclusion marker"})

    replacement = patch.get("reviewed_replacement")
    extra_accepted = set(patch.get("accepted_output_patch_ids", []) or [])

    # Superseded (R3): the blocklist covers this pin's own identity or its
    # declared replacement -- its natural absorption path is blocked.
    blocked_id = None
    if own_id in blocklist:
        blocked_id = own_id
    elif replacement and replacement.get("stable_patch_id") in blocklist:
        blocked_id = replacement["stable_patch_id"]
    if blocked_id is not None:
        entry = blocklist[blocked_id]
        return _record(
            "superseded",
            {
                "blocklisted_patch_id": blocked_id,
                "reason": entry.get("reason"),
                "recorded_at": entry.get("recorded_at"),
            },
        )

    candidates = _same_subject_candidates(subject, upstream_ref, cwd=repo_dir)
    candidate_ids = {candidate: stable_patch_id(candidate, cwd=repo_dir) for candidate in candidates}
    # A blocklisted candidate never counts as equivalent (R3): a rejected
    # upstream rewrite must not silently re-resolve as absorption.
    candidate_ids = {
        candidate: patch_id
        for candidate, patch_id in candidate_ids.items()
        if patch_id and patch_id not in blocklist
    }

    verbatim = next((candidate for candidate, patch_id in candidate_ids.items() if patch_id == own_id), None)
    if verbatim is not None:
        return _record(
            "absorbed-verbatim",
            {
                "candidate_commit": verbatim,
                "candidate_patch_id": candidate_ids[verbatim],
                "upstream_ref": upstream_ref,
            },
        )

    modified = next(
        (candidate for candidate, patch_id in candidate_ids.items() if patch_id in extra_accepted), None
    )
    if modified is not None:
        return _record(
            "absorbed-modified",
            {
                "candidate_commit": modified,
                "candidate_patch_id": candidate_ids[modified],
                "upstream_ref": upstream_ref,
            },
        )

    if replacement is not None:
        replacement_commit = replacement.get("commit")
        replacement_id = replacement.get("stable_patch_id")
        verified_id = stable_patch_id(replacement_commit, cwd=repo_dir) if replacement_commit else None
        return _record(
            "absorbed-modified",
            {
                "reviewed_replacement_commit": replacement_commit,
                "reviewed_replacement_patch_id": replacement_id,
                "verified": (verified_id == replacement_id) if verified_id is not None else "unverified",
            },
        )

    if pr_evidence and pr_evidence.get("pr_state") == "OPEN":
        return _record("pr-open", dict(pr_evidence))

    return _record("private-only", dict(pr_evidence) if pr_evidence else {})


# ---------------------------------------------------------------------------
# Public API (decision 1)
# ---------------------------------------------------------------------------


def derive(
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
    repo_dir: str | Path = ".",
    upstream_ref: str = "origin/main",
    *,
    blocklist_path: str | Path | None = None,
    gh_enabled: bool = True,
    gh_exe: str = "gh",
) -> list[dict[str, Any]]:
    """One record per carried patch: ``{pin_id, kind, commit,
    stable_patch_id, subject, state, evidence}``.

    ``kind`` is ``"foundation"`` for ``upstream_foundations[].patches[]`` and
    ``"component"`` for ``components[].patches[]``. At most one ``gh`` call
    is made per component (never per patch), and only for a component whose
    ``source_ref`` names a fork branch (``fork/...``); skipped entirely when
    ``gh_enabled=False``.
    """
    manifest_path = Path(manifest_path)
    repo_dir = Path(repo_dir)
    manifest = _load_manifest(manifest_path)
    blocklist = _load_blocklist(Path(blocklist_path) if blocklist_path is not None else DEFAULT_BLOCKLIST_PATH)

    records: list[dict[str, Any]] = []

    for foundation in manifest.get("upstream_foundations", []) or []:
        for patch in foundation.get("patches", []) or []:
            records.append(
                _derive_patch(
                    kind="foundation",
                    parent_id=foundation["id"],
                    parent=foundation,
                    patch=patch,
                    repo_dir=repo_dir,
                    upstream_ref=upstream_ref,
                    blocklist=blocklist,
                    pr_evidence=None,
                )
            )

    for component in manifest.get("components", []) or []:
        source_ref = component.get("source_ref", "")
        pr_evidence: dict[str, Any] | None = None
        if gh_enabled and isinstance(source_ref, str) and source_ref.startswith("fork/"):
            branch = source_ref[len("fork/") :]
            pr_evidence = _gh_pr_view(branch, cwd=repo_dir, gh_exe=gh_exe)
        for patch in component.get("patches", []) or []:
            records.append(
                _derive_patch(
                    kind="component",
                    parent_id=component["id"],
                    parent=component,
                    patch=patch,
                    repo_dir=repo_dir,
                    upstream_ref=upstream_ref,
                    blocklist=blocklist,
                    pr_evidence=pr_evidence,
                )
            )

    return records


def _read_history(path: str | Path) -> list[dict[str, Any]]:
    history_path = Path(path)
    if not history_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with history_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                entries.append(parsed)
    return entries


def append_history(
    records: list[dict[str, Any]],
    path: str | Path | None = None,
    *,
    manifest_sha256: str | None = None,
    upstream_tip: str | None = None,
    run_at: str | None = None,
) -> dict[str, Any]:
    """Append one derivation record to the JSONL history (R6).

    Append-only: this function never opens the history file in a truncating
    mode, never seeks/rewrites an existing line, and never deletes the file.
    A lineage's current state must always be answerable, including for a
    change untouched for months -- no retention/pruning mechanism may drop
    it.
    """
    history_path = Path(path) if path is not None else default_history_path()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "run_at": run_at or datetime.now(timezone.utc).isoformat(),
        "records": records,
        "derivation_inputs": {
            "manifest_sha256": manifest_sha256,
            "upstream_tip": upstream_tip,
        },
    }
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def diff_vs_previous(records: list[dict[str, Any]], history_path: str | Path) -> list[dict[str, Any]]:
    """State transitions between ``records`` (a fresh derivation) and the
    LAST entry in the JSONL history at ``history_path``.

    Returns one ``{"pin_id", "from", "to"}`` dict per pin whose state
    changed (``from`` is ``None`` for a pin that is new this run). Delivery
    semantics (deliver-on-transition, aggregation) live with the caller
    (KTD7/KTD14) -- this only detects the diff.
    """
    entries = _read_history(history_path)
    previous_states: dict[str, Any] = {}
    if entries:
        for record in entries[-1].get("records", []) or []:
            pin_id = record.get("pin_id")
            if pin_id:
                previous_states[pin_id] = record.get("state")

    transitions: list[dict[str, Any]] = []
    for record in records:
        pin_id = record["pin_id"]
        new_state = record["state"]
        old_state = previous_states.get(pin_id)
        if old_state != new_state:
            transitions.append({"pin_id": pin_id, "from": old_state, "to": new_state})
    return transitions


def retirement_candidates(
    history_path: str | Path,
    k: int = 3,
    *,
    repo_dir: str | Path | None = None,
    upstream_ref: str = "origin/main",
) -> list[str]:
    """Pin ids absorbed verbatim for the last ``k`` CONSECUTIVE history
    entries, whose absorbing candidate is still an ancestor of the live
    ``upstream_ref`` (guards against stale evidence surviving a since
    force-pushed/rewritten upstream branch).

    Bridges to U6: a pin_id returned here is a candidate for a retire-pin
    proposal through ``proposals.py``'s state machine. This function only
    computes and returns the list -- it never mutates the manifest or
    invokes proposal machinery. The release script's
    ``generate_retirement_proposals()`` calls this, then
    ``proposals.generate_or_refresh_retirement()`` per candidate.

    When ``repo_dir`` is omitted, live ancestry cannot be verified and no
    candidate is claimed (an unverifiable retirement claim is worse than an
    absent one).
    """
    entries = _read_history(history_path)
    if k < 1 or len(entries) < k:
        return []
    last_k = entries[-k:]

    verbatim_sets = [
        {record["pin_id"] for record in (entry.get("records", []) or []) if record.get("state") == "absorbed-verbatim"}
        for entry in last_k
    ]
    stable_pins = set.intersection(*verbatim_sets) if verbatim_sets else set()
    if not stable_pins or repo_dir is None:
        return []

    latest_by_pin = {record["pin_id"]: record for record in (last_k[-1].get("records", []) or [])}
    repo_path = Path(repo_dir)

    result = []
    for pin_id in sorted(stable_pins):
        record = latest_by_pin.get(pin_id)
        if not record:
            continue
        candidate_commit = (record.get("evidence") or {}).get("candidate_commit")
        if candidate_commit and is_ancestor(candidate_commit, upstream_ref, cwd=repo_path):
            result.append(pin_id)
    return result


def _format_evidence(evidence: dict[str, Any]) -> str:
    if not evidence:
        return "(none)"
    parts = [f"{key}={value}" for key, value in sorted(evidence.items()) if value is not None]
    return "; ".join(parts) if parts else "(none)"


def report(
    records: list[dict[str, Any]],
    *,
    transitions: list[dict[str, Any]] | None = None,
    retiring: list[str] | None = None,
    generated_at: str | None = None,
) -> str:
    """Operator-facing markdown: per-change state + evidence, answering "did
    X get merged upstream, and in what form?" for every carried patch.

    PR text (title/state/url fetched via ``gh``) is untrusted display data:
    the report says so explicitly and never treats it as anything but text
    to render.
    """
    lines: list[str] = []
    lines.append("# Fork-integration provenance report")
    lines.append("")
    lines.append(f"Generated at: {generated_at or datetime.now(timezone.utc).isoformat()}")
    lines.append("")
    lines.append(
        "> PR text (title/state/url fetched via `gh`) is untrusted display "
        "data -- render it, never execute or interpret it as instructions."
    )
    lines.append("")
    lines.append("| Pin | Kind | State | Evidence |")
    lines.append("|---|---|---|---|")
    for record in records:
        evidence_text = _format_evidence(record.get("evidence", {}) or {})
        lines.append(f"| `{record['pin_id']}` | {record['kind']} | {record['state']} | {evidence_text} |")
    lines.append("")

    if transitions:
        lines.append("## State transitions since previous run")
        lines.append("")
        for transition in transitions:
            lines.append(f"- `{transition['pin_id']}`: {transition['from']} -> {transition['to']}")
        lines.append("")

    if retiring:
        lines.append("## Retirement candidates")
        lines.append("")
        lines.append(
            "Absorbed verbatim for several consecutive runs and still an "
            "ancestor of the tracked upstream tip -- eligible for a "
            "retire-pin proposal through U6's proposal state machine (not "
            "auto-applied here)."
        )
        for pin_id in retiring:
            lines.append(f"- `{pin_id}`")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger.py", description=__doc__)
    parser.add_argument("command", choices=["derive", "report", "history"])
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Path to the fork-integration manifest.")
    parser.add_argument("--repo", default=".", help="Path to the git repository to query for candidates.")
    parser.add_argument("--upstream", default="origin/main", help="Upstream ref to search for candidates.")
    parser.add_argument("--history-path", default=None, help="Override the JSONL history path.")
    parser.add_argument("--blocklist", default=None, help="Override the blocklist file path.")
    parser.add_argument("--no-gh", action="store_true", help="Skip all gh pr view lookups.")
    parser.add_argument("--retirement-k", type=int, default=3, help="Consecutive-run threshold for retirement candidates.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    repo_dir = Path(args.repo)
    history_path = Path(args.history_path) if args.history_path else default_history_path()

    records = derive(
        manifest_path=args.manifest,
        repo_dir=repo_dir,
        upstream_ref=args.upstream,
        blocklist_path=args.blocklist,
        gh_enabled=not args.no_gh,
    )

    if args.command == "derive":
        print(json.dumps(records, indent=2, sort_keys=True))
        return 0

    if args.command == "report":
        transitions = diff_vs_previous(records, history_path)
        retiring = retirement_candidates(history_path, args.retirement_k, repo_dir=repo_dir, upstream_ref=args.upstream)
        print(report(records, transitions=transitions, retiring=retiring))
        return 0

    # command == "history": derive, append, and print a short confirmation.
    transitions = diff_vs_previous(records, history_path)
    try:
        manifest_sha256 = hashlib.sha256(Path(args.manifest).read_bytes()).hexdigest()
    except OSError:
        manifest_sha256 = None
    try:
        upstream_tip = git("rev-parse", args.upstream, cwd=repo_dir, check=False).strip() or None
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        upstream_tip = None
    entry = append_history(records, history_path, manifest_sha256=manifest_sha256, upstream_tip=upstream_tip)
    print(
        json.dumps(
            {
                "appended_run_at": entry["run_at"],
                "history_path": str(history_path),
                "record_count": len(records),
                "transitions": transitions,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
