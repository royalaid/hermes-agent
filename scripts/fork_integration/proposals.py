#!/usr/bin/env python
"""U6 reconciliation proposals: the churn state machine behind park-and-continue.

Upstream rewrites the same logical change repeatedly (the #63047 journal fix
has existed under at least three SHAs with three patch-ids). Every rewrite
used to strand a manifest pin until a human performed manifest surgery, and
the release script's response was a hard refusal ("same-subject but
non-equivalent required patch") that skipped the nightly.

This module replaces that refusal with evidence plus a human-approved edit:

  * **Detection (R1).** A candidate is an upstream commit whose SUBJECT LINE
    is *exactly* the pin's subject (never a substring, never a message-body
    match), which is NOT a ``Revert "..."``, which is an ancestor of the
    upstream tip fetched in that run, and whose patch-id is not blocklisted.
    A secondary, deliberately weaker detector catches retitled re-lands by
    changed-path overlap and marks them ``low_confidence``.
  * **Park-and-continue (KTD13/R19).** Generation never halts a run. The
    release script records the parked pin, tags its provenance
    ``pin-parked-pending-proposal:<id>``, and keeps applying the pin's last
    verified form. Only a failing re-apply of that last-good form aborts.
  * **Approval (R2).** ``approve`` is interactive-only, re-verifies that the
    candidate is still upstream's current form, RE-DERIVES the manifest edit
    from that fresh verification, and requires byte-equality with the stored
    fragment. Anything else transitions the proposal to ``stale-invalidated``
    rather than applying a stale or tampered edit.
  * **Blocklist (R3).** Rejected candidates, and candidate patch-ids
    superseded by a later rewrite, are appended to
    ``fork-integration-blocklist.json`` so they never count as equivalent and
    are never re-proposed.
  * **Absorption mechanism (R4).** The generated edit only ever appends to a
    pin's existing ``accepted_output_patch_ids`` -- the mechanism the release
    script already uses -- never a parallel one.

State machine (KTD3)::

    generated -> pending-approval -> approved -> applied
                        |-> rejected
                        |-> stale-invalidated -> (regenerate) -> pending-approval

``generated`` is also the terminal parking state for an ``evidence:
"unavailable"`` proposal: when either side of the interdiff cannot be
resolved, the artifact fails closed and says so instead of fabricating
evidence, and the run still parks-and-continues without it.

Stdlib only, and deliberately importable both as ``scripts.fork_integration.
proposals`` (pytest, from the repo root) and by path (the release script's
``_proposals_module()``, because the operational directory is flat).
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

# ── Schema / state vocabulary ────────────────────────────────────────────────

SCHEMA = 1

STATE_GENERATED = "generated"
STATE_PENDING_APPROVAL = "pending-approval"
STATE_APPROVED = "approved"
STATE_REJECTED = "rejected"
STATE_STALE_INVALIDATED = "stale-invalidated"
STATE_APPLIED = "applied"
STATES: tuple[str, ...] = (
    STATE_GENERATED, STATE_PENDING_APPROVAL, STATE_APPROVED,
    STATE_REJECTED, STATE_STALE_INVALIDATED, STATE_APPLIED,
)
#: States in which a proposal is still parking its pin and awaiting a human.
OPEN_STATES: frozenset[str] = frozenset({STATE_GENERATED, STATE_PENDING_APPROVAL})

EVIDENCE_COMPLETE = "complete"
EVIDENCE_UNAVAILABLE = "unavailable"

#: Regenerations after which the delivery escalates to churn-livelock (KTD3).
CHURN_LIVELOCK_REGENERATIONS = 3
#: Upper bound on candidates recorded (and diffed) in one proposal.
MAX_CANDIDATES = 5

# Retitled-re-land detector bounds. This secondary detector only runs when
# exact-subject detection found nothing, and it walks history with BOTH a
# pathspec and a date/count bound so a nightly run cannot turn it into a
# full-history scan per pin.
RETITLED_MIN_PIN_OVERLAP = 0.60
RETITLED_MIN_CANDIDATE_OVERLAP = 0.30
RETITLED_SEARCH_LIMIT = 50
RETITLED_SEARCH_SINCE = "180 days ago"
RETITLED_MAX_PIN_FILES = 60

REPO_TRACKED_SUBDIR = "scripts/fork_integration"
MANIFEST_FILENAME = "hermes-integration-manifest.json"
BLOCKLIST_FILENAME = "fork-integration-blocklist.json"
SYNC_FILENAME = "sync.py"

PROPOSALS_DIR_ENV = "FORK_INTEGRATION_PROPOSALS_DIR"
NONINTERACTIVE_ENV = "PROPOSALS_ALLOW_NONINTERACTIVE"

#: Default artifact store. Derived exactly like the release script's other
#: ``HERMES_HOME`` paths; overridable per-instance and via the env var above
#: (tests always pass an explicit root).
DEFAULT_PROPOSALS_DIR = (
    Path.home() / "AppData" / "Local" / "hermes" / "review-artifacts" / "fork-integration-proposals"
)

#: Object-retention refs for evidence (U6 owns keep-refs; the plan defers only
#: safety-branch cleanup). ``refs/pinned/<pin id>/<stable patch id>``.
KEEP_REF_PREFIX = "refs/pinned"

PROVENANCE_TAG_PREFIX = "pin-parked-pending-proposal"

_COMMIT_FOOTER = (
    "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>\n"
    "Claude-Session: https://claude.ai/code/session_01EfgNpcfi1s5tyBaCpjivG6"
)

_HEX40 = re.compile(r"[0-9a-f]{40}")


class ProposalError(RuntimeError):
    """Raised for every refusal this module makes deliberately.

    Detection helpers never raise it for "nothing found" -- that is an empty
    result, not an error. Generation raises only when the STORE cannot be
    written; unresolvable git evidence degrades to ``evidence: "unavailable"``
    so a run can still park-and-continue.
    """


# ── git plumbing ─────────────────────────────────────────────────────────────


def _no_window_kwargs() -> dict[str, Any]:
    """Keep git subprocesses from flashing a console window under the
    Windows scheduler (same treatment the release script gives its own)."""
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0), "startupinfo": startupinfo}


class Git:
    """Minimal git accessor bound to one repository directory.

    Deliberately independent of the release script's ``run()``/``git()``:
    the approval CLI runs standalone (no manifest import, no worktree
    assumptions), and proposal generation must keep working when it is
    handed a throwaway fixture repo.
    """

    def __init__(self, repo_dir: Path | str, *, timeout: int = 300) -> None:
        self.repo_dir = Path(repo_dir)
        self.timeout = timeout

    def _argv(self, args: Sequence[str]) -> list[str]:
        return ["git", "-C", str(self.repo_dir), "-c", "rerere.enabled=false", *args]

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            self._argv(args), text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=self.timeout, **_no_window_kwargs(),
        )
        if check and result.returncode:
            raise ProposalError(
                f"git {' '.join(args[:3])} failed ({result.returncode}): "
                f"{(result.stderr or '').strip()[:400]}"
            )
        return result

    def text(self, *args: str, check: bool = True) -> str:
        return self.run(*args, check=check).stdout.strip()

    def ok(self, *args: str) -> bool:
        return self.run(*args, check=False).returncode == 0

    def is_ancestor(self, commit: str, descendant: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", commit, descendant)

    def exists(self, ref: str) -> bool:
        return self.run("cat-file", "-e", f"{ref}^{{commit}}", check=False).returncode == 0

    def resolve(self, ref: str) -> str:
        return self.text("rev-parse", "--verify", f"{ref}^{{commit}}")

    def patch_id(self, ref: str) -> str:
        """Whitespace-insensitive patch identity, computed exactly the way
        the release script's ``stable_patch_id()`` computes it."""
        show = self.run("show", "--format=", "--binary", ref)
        result = subprocess.run(
            ["git", "patch-id", "--stable"], input=show.stdout, text=True, encoding="utf-8",
            errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=self.timeout, **_no_window_kwargs(),
        )
        if result.returncode or not result.stdout.strip():
            raise ProposalError(f"could not calculate stable patch identity for {ref}")
        return result.stdout.split()[0]

    def changed_files(self, ref: str) -> list[str]:
        out = self.text("diff-tree", "--no-commit-id", "--name-only", "-r", ref, check=False)
        return [line for line in out.splitlines() if line]

    def keep_ref(self, pin_id: str, stable_patch_id: str, commit: str) -> str | None:
        """Pin one commit against garbage collection so a proposal's evidence
        (and an outgoing pin's history) survives upstream's rewrites.

        Best-effort: a keep-ref that cannot be written must never fail a
        release run, so the caller gets ``None`` instead of an exception.
        """
        ref = f"{KEEP_REF_PREFIX}/{pin_id}/{stable_patch_id}"
        if not self.ok("update-ref", ref, commit):
            return None
        return ref


# ── canonical hashing ────────────────────────────────────────────────────────


def canonical_bytes(value: Any) -> bytes:
    """Canonical JSON encoding used for every hash and byte-equality check."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def artifact_hash(artifact: dict[str, Any]) -> str:
    """sha256 of the canonical artifact JSON *minus* ``artifact_sha256``."""
    return canonical_sha256({k: v for k, v in artifact.items() if k != "artifact_sha256"})


def proposal_id(pin: dict[str, Any]) -> str:
    """Stable short id derived from the PIN only.

    Candidates deliberately do not participate: when upstream rewrites again,
    the same proposal must be regenerated in place (carrying ``regen_count``
    and the superseded ids) rather than silently becoming a second artifact.
    """
    seed = "\x00".join([
        str(pin.get("kind", "")), str(pin.get("id", "")),
        str(pin.get("commit", "")), str(pin.get("stable_patch_id", "")),
    ])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── blocklist (R3) ───────────────────────────────────────────────────────────


def empty_blocklist() -> dict[str, Any]:
    return {"schema": SCHEMA, "entries": []}


def load_blocklist(path: Path | str) -> dict[str, Any]:
    """Read the blocklist. An ABSENT file is an empty blocklist; a present
    but unreadable/malformed one fails closed -- silently treating a corrupt
    blocklist as empty would let a rejected candidate be re-absorbed."""
    path = Path(path)
    if not path.exists():
        return empty_blocklist()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProposalError(f"blocklist is unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA or not isinstance(payload.get("entries"), list):
        raise ProposalError(f"blocklist has an unsupported schema or shape: {path}")
    for entry in payload["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("patch_id"), str):
            raise ProposalError(f"blocklist has a malformed entry: {path}")
    return payload


def blocklisted_patch_ids(path: Path | str) -> set[str]:
    return {str(entry["patch_id"]) for entry in load_blocklist(path)["entries"]}


def append_blocklist_entries(path: Path | str, entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Append entries (skipping patch-ids already present) and rewrite the
    file. Returns the new blocklist document."""
    path = Path(path)
    document = load_blocklist(path)
    known = {str(entry["patch_id"]) for entry in document["entries"]}
    added = []
    for entry in entries:
        patch_id = str(entry.get("patch_id", ""))
        if not patch_id or patch_id in known:
            continue
        known.add(patch_id)
        added.append({
            "patch_id": patch_id,
            "pin_id": str(entry.get("pin_id", "")),
            "reason": str(entry.get("reason", "")),
            "actor": str(entry.get("actor", "")),
            "recorded_at": str(entry.get("recorded_at") or _now()),
        })
    document["entries"].extend(added)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    return document


def _atomic_write_text(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


# ── detection (R1) ───────────────────────────────────────────────────────────


def _log_subject_rows(git: Git, ref: str, *args: str) -> list[tuple[str, str]]:
    """``git log --format=%H%x00%s`` rows as ``(sha, subject)`` pairs."""
    out = git.text("log", ref, "--format=%H%x00%s", *args, check=False)
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        sha, _, subject = line.partition("\x00")
        if sha:
            rows.append((sha, subject))
    return rows


def is_revert_subject(subject: str) -> bool:
    return subject.startswith('Revert "')


def exact_subject_candidates(git: Git, ref: str, subject: str) -> list[str]:
    """Commits reachable from *ref* whose subject line is EXACTLY *subject*.

    ``--grep`` is only the cheap pre-filter (it matches anywhere in the
    message); exact subject equality is the contract, and ``Revert "..."``
    re-lands never qualify.
    """
    rows = _log_subject_rows(git, ref, "--fixed-strings", f"--grep={subject}")
    return [
        sha for sha, candidate_subject in rows
        if candidate_subject == subject and not is_revert_subject(candidate_subject)
    ]


def _file_sets(git: Git, shas: Sequence[str]) -> dict[str, set[str]]:
    """Changed-file sets for several commits in ONE ``git show``.

    The retitled detector needs each candidate's FULL file set (a pathspec-
    limited ``git log --name-only`` would report only the overlapping files
    and make the candidate-side ratio meaninglessly 1.0), but one subprocess
    per candidate turns a nightly run into hundreds of git invocations.
    """
    if not shas:
        return {}
    result = git.run(
        "show", "--no-color", "--format=%x01%H", "--name-only", "-m", "--first-parent", *shas, check=False,
    )
    if result.returncode:
        return {}
    sets: dict[str, set[str]] = {}
    current: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("\x01"):
            current = line[1:].strip()
            sets.setdefault(current, set())
            continue
        if current and line.strip():
            sets[current].add(line.strip())
    return sets


def describe_candidate(git: Git, sha: str, *, patch_id: str) -> dict[str, Any]:
    """Author/committer/signature evidence recorded for every candidate (R1)."""
    raw = git.text("log", "-1", "--format=%an <%ae>%x00%cn <%ce>%x00%G?%x00%s", sha, check=False)
    parts = raw.split("\x00")
    while len(parts) < 4:
        parts.append("")
    return {
        "sha": sha,
        "stable_patch_id": patch_id,
        "subject": parts[3],
        "author": parts[0],
        "committer": parts[1],
        # git's %G? -- "N" (no signature), "G" (good), "B" (bad), "U"/"X"/"Y"/"R"/"E".
        "signature_state": parts[2] or "N",
        "low_confidence": False,
    }


def retitled_candidates(
    git: Git, *, pin_commit: str, upstream_tip: str, pin_subject: str,
) -> list[tuple[str, float]]:
    """Weak secondary detector for a re-land that upstream RETITLED.

    Pure changed-path overlap, on purpose: a shared-path ratio is cheap,
    explainable in a review, and cannot be mistaken for patch identity.
    Every hit is marked ``low_confidence`` and parks exactly like any other
    candidate -- it never gets a stronger claim than "a human should look".
    """
    pin_files = git.changed_files(pin_commit)
    if not pin_files or len(pin_files) > RETITLED_MAX_PIN_FILES:
        return []
    pin_set = set(pin_files)
    rows = [
        (sha, subject) for sha, subject in _log_subject_rows(
            git, upstream_tip, "--no-merges", f"--max-count={RETITLED_SEARCH_LIMIT}",
            f"--since={RETITLED_SEARCH_SINCE}", "--", *pin_files,
        )
        if subject != pin_subject and not is_revert_subject(subject)
    ]
    file_sets = _file_sets(git, [sha for sha, _subject in rows])
    scored: list[tuple[str, float]] = []
    for sha, _subject in rows:
        candidate_files = file_sets.get(sha, set())
        if not candidate_files:
            continue
        shared = len(pin_set & candidate_files)
        pin_ratio = shared / len(pin_set)
        candidate_ratio = shared / len(candidate_files)
        if pin_ratio >= RETITLED_MIN_PIN_OVERLAP and candidate_ratio >= RETITLED_MIN_CANDIDATE_OVERLAP:
            scored.append((sha, round(pin_ratio, 4)))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:MAX_CANDIDATES]


def detect_candidates(
    git: Git,
    pin: dict[str, Any],
    *,
    search_ref: str,
    upstream_tip: str,
    blocked: Iterable[str] = (),
    accepted_patch_ids: Iterable[str] = (),
    patch_id_of: Callable[[str], str] | None = None,
    detect_retitled: bool = True,
) -> dict[str, Any]:
    """Resolve the eligible reconciliation candidates for one pin.

    Returns ``{"candidates": [...], "low_confidence": bool}``. An empty
    candidate list is the normal, non-exceptional answer: a non-ancestor
    same-subject commit, a ``Revert "..."``, and a blocklisted patch-id all
    yield no proposal at all.
    """
    blocked_ids = set(blocked)
    accepted = set(accepted_patch_ids)
    identity = patch_id_of or git.patch_id
    subject = str(pin.get("subject", ""))

    records: list[dict[str, Any]] = []
    for sha in exact_subject_candidates(git, search_ref, subject):
        if not git.is_ancestor(sha, upstream_tip):
            continue
        try:
            candidate_patch_id = identity(sha)
        except Exception:
            continue
        if candidate_patch_id in blocked_ids or candidate_patch_id in accepted:
            continue
        records.append(describe_candidate(git, sha, patch_id=candidate_patch_id))
        if len(records) >= MAX_CANDIDATES:
            break
    if records:
        return {"candidates": records, "low_confidence": False}

    if not detect_retitled or not git.exists(str(pin.get("commit", ""))):
        return {"candidates": [], "low_confidence": False}

    for sha, ratio in retitled_candidates(
        git, pin_commit=str(pin["commit"]), upstream_tip=upstream_tip, pin_subject=subject,
    ):
        try:
            candidate_patch_id = identity(sha)
        except Exception:
            continue
        if candidate_patch_id in blocked_ids or candidate_patch_id in accepted:
            continue
        described = describe_candidate(git, sha, patch_id=candidate_patch_id)
        described["low_confidence"] = True
        described["shared_path_ratio"] = ratio
        records.append(described)
    return {"candidates": records, "low_confidence": bool(records)}


# ── the recommended manifest edit (R4) ───────────────────────────────────────


def derive_manifest_edit(pin: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """The ready-to-apply manifest fragment for absorbing *candidate*.

    Only ever an append to the pin's existing ``accepted_output_patch_ids``
    (R4: extend the accepted-patch-identity mechanism, never a parallel one).
    Approval re-derives this from a freshly re-verified candidate and demands
    byte-equality with the stored copy, so its shape must stay a pure
    function of (pin, candidate).
    """
    return {
        "operation": "append_accepted_output_patch_id",
        "pin_kind": str(pin["kind"]),
        "pin_id": str(pin["id"]),
        "patch_commit": str(pin["commit"]),
        "patch_stable_patch_id": str(pin["stable_patch_id"]),
        "candidate_commit": str(candidate["sha"]),
        "accepted_output_patch_id": str(candidate["stable_patch_id"]),
    }


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _render_accepted_ids(ids: Sequence[str], indent: int) -> list[str]:
    """Render ``accepted_output_patch_ids`` in the manifest's own hand style:
    one id stays inline, several expand one-per-line."""
    pad = " " * indent
    key = '"accepted_output_patch_ids": '
    if len(ids) == 1:
        return [f'{pad}{key}[{json.dumps(ids[0])}],']
    body = [f'{pad}  {json.dumps(value)},' for value in ids[:-1]]
    body.append(f'{pad}  {json.dumps(ids[-1])}')
    return [f"{pad}{key}[", *body, f"{pad}],"]


def apply_manifest_edit_text(text: str, edit: dict[str, Any]) -> str:
    """Apply *edit* to the manifest SOURCE TEXT, preserving its formatting.

    A whole-file ``json.dumps`` round-trip is not byte-preserving for this
    manifest (it is hand-formatted: single-element id arrays inline, a
    deliberate blank line), and an approval commit that reformats the entire
    reviewable contract is exactly the kind of unreviewable change this
    system exists to remove. So the edit is surgical: locate the patch object
    by its unique ``"commit": "<sha>"`` line and touch only its
    ``accepted_output_patch_ids``.

    Fails closed when the anchor is missing or ambiguous.
    """
    if edit.get("operation") != "append_accepted_output_patch_id":
        raise ProposalError(f"unsupported manifest edit operation: {edit.get('operation')!r}")
    new_id = str(edit["accepted_output_patch_id"])
    if not _HEX40.fullmatch(new_id):
        raise ProposalError(f"refusing a manifest edit with a malformed patch id: {new_id!r}")

    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    anchor = f'"commit": "{edit["patch_commit"]}"'
    matches = [index for index, line in enumerate(lines) if anchor in line]
    if len(matches) != 1:
        raise ProposalError(
            f"manifest anchor for {edit['patch_commit']} is missing or ambiguous "
            f"(occurrences={len(matches)}); refusing an automated edit"
        )
    start = matches[0]
    indent = _line_indent(lines[start])

    # Bound the patch object: it ends at the first following line whose
    # indent is smaller than the key indent (its closing brace).
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped and _line_indent(lines[index]) < indent:
            end = index
            break

    key_index = next(
        (index for index in range(start, end) if lines[index].strip().startswith('"accepted_output_patch_ids"')),
        None,
    )
    if key_index is None:
        existing: list[str] = []
        after = next(
            (index for index in range(start, end) if lines[index].strip().startswith('"stable_patch_id"')),
            start,
        )
        replace_from, replace_to = after + 1, after + 1
    else:
        block_end = key_index
        if not lines[key_index].rstrip().endswith("],"):
            block_end = next(
                (index for index in range(key_index, end) if lines[index].strip().startswith("],")),
                key_index,
            )
        raw_block = newline.join(lines[key_index:block_end + 1]).strip().rstrip(",")
        _key, _sep, array_text = raw_block.partition(":")
        try:
            existing = list(json.loads(array_text.strip()))
        except json.JSONDecodeError as exc:
            raise ProposalError(f"could not parse existing accepted_output_patch_ids: {exc}") from exc
        replace_from, replace_to = key_index, block_end + 1

    if new_id in existing:
        return text
    rendered = _render_accepted_ids([*existing, new_id], indent)
    updated = [*lines[:replace_from], *rendered, *lines[replace_to:]]
    return newline.join(updated)


def apply_manifest_edit(manifest_path: Path | str, edit: dict[str, Any]) -> str:
    """Apply the edit on disk and verify the result still parses as the
    manifest it claims to be. Returns the new text."""
    manifest_path = Path(manifest_path)
    original = manifest_path.read_text(encoding="utf-8", newline="")
    updated = apply_manifest_edit_text(original, edit)
    parsed = json.loads(updated)
    pin_patch = _find_manifest_patch(parsed, edit)
    if str(edit["accepted_output_patch_id"]) not in pin_patch.get("accepted_output_patch_ids", []):
        raise ProposalError("manifest edit did not take effect; refusing to write")
    _atomic_write_text(manifest_path, updated)
    return updated


def _find_manifest_patch(manifest: dict[str, Any], edit: dict[str, Any]) -> dict[str, Any]:
    containers = (
        [("foundation", item) for item in manifest.get("upstream_foundations", [])]
        + [("component", item) for item in manifest.get("components", [])]
    )
    for kind, container in containers:
        if kind != edit["pin_kind"] or container.get("id") != edit["pin_id"]:
            continue
        for patch in container.get("patches", []):
            if patch.get("commit") == edit["patch_commit"]:
                return patch
    raise ProposalError(
        f"manifest has no {edit['pin_kind']} {edit['pin_id']!r} patch {edit['patch_commit']}"
    )


def manifest_pin_index(manifest: dict[str, Any]) -> dict[tuple[str, str], tuple[str, str]]:
    """``(commit, stable_patch_id) -> (kind, container id)`` for every pin."""
    index: dict[tuple[str, str], tuple[str, str]] = {}
    for kind, key in (("foundation", "upstream_foundations"), ("component", "components")):
        for container in manifest.get(key, []) or []:
            for patch in container.get("patches", []) or []:
                index.setdefault(
                    (str(patch.get("commit")), str(patch.get("stable_patch_id"))),
                    (kind, str(container.get("id"))),
                )
    return index


# ── artifact store ───────────────────────────────────────────────────────────


class ProposalStore:
    """Hash-stamped proposal artifacts plus their sibling ``.diff`` evidence."""

    def __init__(self, root: Path | str | None = None) -> None:
        if root is None:
            root = os.environ.get(PROPOSALS_DIR_ENV) or DEFAULT_PROPOSALS_DIR
        self.root = Path(root)

    def artifact_path(self, identifier: str) -> Path:
        return self.root / f"{identifier}.json"

    def diff_path(self, identifier: str) -> Path:
        return self.root / f"{identifier}.diff"

    def load(self, identifier: str) -> dict[str, Any] | None:
        path = self.artifact_path(identifier)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProposalError(f"proposal artifact is unreadable: {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ProposalError(f"proposal artifact is not a JSON object: {path}")
        return payload

    def save(self, artifact: dict[str, Any]) -> dict[str, Any]:
        """Recompute ``artifact_sha256`` and write atomically.

        The stored hash is always recomputed here rather than trusted from
        the caller, so a tampered fragment cannot travel with a matching
        hash written by the tamperer -- ``approve`` compares the operator's
        ``--artifact-hash`` against BOTH the stored value and a fresh
        recomputation.
        """
        artifact["artifact_sha256"] = artifact_hash(artifact)
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.artifact_path(artifact["id"]), json.dumps(artifact, indent=2, ensure_ascii=False) + "\n")
        return artifact

    def write_diff(self, identifier: str, text: str) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.diff_path(identifier)
        _atomic_write_text(path, text)
        return {"file": path.name, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}

    def list_all(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        artifacts = []
        for path in sorted(self.root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and payload.get("id"):
                artifacts.append(payload)
        return artifacts

    def list_open(self) -> list[dict[str, Any]]:
        return [item for item in self.list_all() if item.get("state") in OPEN_STATES]


# ── generation (KTD3 step 1) ─────────────────────────────────────────────────


def _collect_evidence(git: Git, pin: dict[str, Any], candidates: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Return ``(diff_text, interdiff_stat)``; ``None`` on either side means
    evidence is unavailable and the proposal must say so rather than invent
    a summary (KTD3 / U6 approach step 3)."""
    pin_commit = str(pin.get("commit", ""))
    chunks: list[str] = []
    for candidate in candidates:
        show = git.run("show", "--format=fuller", "--no-color", candidate["sha"], check=False)
        if show.returncode:
            return None, None
        chunks.append(f"### candidate {candidate['sha']}\n{show.stdout}")
    if not chunks:
        return None, None
    diff_text = "\n".join(chunks)

    if not git.exists(pin_commit) or not git.exists(f"{pin_commit}^"):
        return diff_text, None
    recommended = candidates[0]["sha"]
    if not git.exists(f"{recommended}^"):
        return diff_text, None
    result = git.run(
        "range-diff", "--no-color", "-s",
        f"{pin_commit}^..{pin_commit}", f"{recommended}^..{recommended}", check=False,
    )
    if result.returncode:
        return diff_text, None
    return diff_text, result.stdout.strip()


def generate_or_refresh(
    store: ProposalStore,
    git: Git,
    *,
    pin: dict[str, Any],
    candidates: list[dict[str, Any]],
    low_confidence: bool = False,
    upstream_ref: str = "",
    upstream_tip: str = "",
) -> dict[str, Any]:
    """Create, keep, or regenerate the proposal for one churned pin.

    Dedupe rule: the same pin with the same candidate patch-id SET keeps the
    existing open artifact untouched (a nightly job must not rewrite the same
    proposal every night). A changed candidate set regenerates in place and
    increments ``regen_count``; three regenerations raise ``churn_livelock``
    so the delivery can escalate instead of looping forever (KTD3).
    """
    if not candidates:
        raise ProposalError("refusing to generate a proposal with no candidates")
    identifier = proposal_id(pin)
    existing = store.load(identifier)
    candidate_ids = [str(item["stable_patch_id"]) for item in candidates]

    if existing is not None:
        previous_ids = [str(item.get("stable_patch_id")) for item in existing.get("candidates", [])]
        unchanged = set(previous_ids) == set(candidate_ids)
        if unchanged and existing.get("state") in OPEN_STATES:
            existing["refreshed"] = False
            return existing
        regen_count = int(existing.get("regen_count", 0)) + 1
        superseded = sorted(
            set(existing.get("superseded_patch_ids", []))
            | {value for value in previous_ids if value not in set(candidate_ids)}
        )
        created_at = str(existing.get("created_at") or _now())
        history = list(existing.get("history", []))
    else:
        regen_count = 0
        superseded = []
        created_at = _now()
        history = []

    diff_text, interdiff_stat = _collect_evidence(git, pin, candidates)
    evidence = EVIDENCE_COMPLETE if (diff_text is not None and interdiff_stat is not None) else EVIDENCE_UNAVAILABLE
    diff_reference = store.write_diff(identifier, diff_text) if diff_text is not None else None

    git.keep_ref(str(pin["id"]), str(pin["stable_patch_id"]), str(pin["commit"]))

    history.append({
        "at": _now(),
        "event": "generated" if existing is None else "regenerated",
        "detail": f"candidates={','.join(candidate_ids)}",
    })
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "id": identifier,
        "created_at": created_at,
        "updated_at": _now(),
        # Fail closed, do not fake evidence: an unresolvable interdiff side
        # parks the pin from `generated` and never claims approval-readiness.
        "state": STATE_PENDING_APPROVAL if evidence == EVIDENCE_COMPLETE else STATE_GENERATED,
        "evidence": evidence,
        "regen_count": regen_count,
        "churn_livelock": regen_count >= CHURN_LIVELOCK_REGENERATIONS,
        "low_confidence": bool(low_confidence),
        "pin": {
            "kind": str(pin["kind"]), "id": str(pin["id"]), "commit": str(pin["commit"]),
            "stable_patch_id": str(pin["stable_patch_id"]), "subject": str(pin["subject"]),
        },
        "upstream_ref": upstream_ref,
        "upstream_tip": upstream_tip,
        "candidates": candidates,
        "recommended_candidate": candidates[0]["sha"],
        "candidate_diff": diff_reference,
        "interdiff_stat": interdiff_stat,
        "recommended_edit": derive_manifest_edit(pin, candidates[0]),
        "lineage": {
            "subject": str(pin["subject"]),
            "file_set": sorted(git.changed_files(str(pin["commit"]))) if git.exists(str(pin["commit"])) else [],
        },
        "superseded_patch_ids": superseded,
        "history": history,
    }
    saved = store.save(artifact)
    saved["refreshed"] = True
    return saved


# ── approval / rejection (KTD3 steps 2-4, R2/R3) ─────────────────────────────


def _repo_file(repo_dir: Path, name: str, override: Path | str | None) -> Path:
    return Path(override) if override else Path(repo_dir) / REPO_TRACKED_SUBDIR / name


def _mark_stale(store: ProposalStore, artifact: dict[str, Any], reason: str) -> dict[str, Any]:
    artifact["state"] = STATE_STALE_INVALIDATED
    artifact["updated_at"] = _now()
    artifact.setdefault("history", []).append({"at": _now(), "event": "stale-invalidated", "detail": reason})
    artifact["stale_reason"] = reason
    store.save(artifact)
    return artifact


#: Fragment keys that identify WHICH candidate is being absorbed. Lineage
#: approval is allowed to move these (that is its entire purpose); every
#: other key must still be byte-equal to the stored fragment.
LINEAGE_MUTABLE_EDIT_KEYS: frozenset[str] = frozenset({"candidate_commit", "accepted_output_patch_id"})


def _describe_verified(git: Git, sha: str, stored: dict[str, Any], patch_id: str) -> dict[str, Any]:
    fresh = describe_candidate(git, sha, patch_id=patch_id)
    fresh["low_confidence"] = bool(stored.get("low_confidence"))
    if "shared_path_ratio" in stored:
        fresh["shared_path_ratio"] = stored["shared_path_ratio"]
    return fresh


def reverify_candidate(
    git: Git,
    artifact: dict[str, Any],
    *,
    upstream_tip: str,
    lineage: bool = False,
    patch_id_of: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Re-verify the recommended candidate against CURRENT upstream (R2).

    Returns ``{"ok": bool, "reason": str, "candidate": {...}}``.

    Default (sha-anchored) approval is strict: the stored candidate SHA must
    still exist, still carry the pin's exact subject, and still be an
    ancestor of the current upstream tip. (Its patch-id cannot drift while
    its sha is fixed -- content determines the sha -- so that check is a
    tautology here and the real currency test is the ancestry one.)

    ``lineage=True`` is KTD3's livelock escape: approval targets the LINEAGE
    (subject + changed-file set) instead of one sha, so a context-drift
    re-land -- the same change re-applied against different surrounding
    lines, hence a different patch-id under a different sha -- can be
    absorbed without waiting for upstream to stop moving. It re-resolves the
    candidate against the current tip and may legitimately return a
    different sha than the artifact stored; ``approve`` therefore relaxes
    byte-equality on exactly the two candidate-identifying fragment keys
    (see ``LINEAGE_MUTABLE_EDIT_KEYS``) and on nothing else.
    """
    identity = patch_id_of or git.patch_id
    pin = artifact["pin"]
    stored = next(
        (item for item in artifact.get("candidates", []) if item["sha"] == artifact.get("recommended_candidate")),
        None,
    )
    if stored is None:
        return {"ok": False, "reason": "artifact has no recommended candidate", "candidate": None}
    sha = str(stored["sha"])
    stored_files = sorted(artifact.get("lineage", {}).get("file_set", []))

    sha_is_current = (
        git.exists(sha)
        and git.text("log", "-1", "--format=%s", sha, check=False) == pin["subject"]
        and git.is_ancestor(sha, upstream_tip)
    )
    if sha_is_current:
        if lineage and sorted(git.changed_files(sha)) != stored_files:
            return {
                "ok": False,
                "reason": f"lineage file set changed for {sha}: {sorted(git.changed_files(sha))} != {stored_files}",
                "candidate": None,
            }
        return {"ok": True, "reason": "", "candidate": _describe_verified(git, sha, stored, identity(sha))}

    if not lineage:
        if not git.exists(sha):
            return {"ok": False, "reason": f"candidate {sha} is no longer present in the repository", "candidate": None}
        subject = git.text("log", "-1", "--format=%s", sha, check=False)
        if subject != pin["subject"]:
            return {"ok": False, "reason": f"candidate subject changed: {subject!r} != {pin['subject']!r}", "candidate": None}
        return {
            "ok": False,
            "reason": (
                f"candidate {sha} is no longer an ancestor of upstream tip {upstream_tip}; "
                "upstream rewrote it again (use --lineage to approve the lineage instead)"
            ),
            "candidate": None,
        }

    # Lineage re-resolution: the newest exact-subject, same-file-set ancestor
    # of the current tip. Never a revert, never a different change that
    # merely shares a subject.
    for replacement in exact_subject_candidates(git, upstream_tip, pin["subject"]):
        if not git.is_ancestor(replacement, upstream_tip):
            continue
        if sorted(git.changed_files(replacement)) != stored_files:
            continue
        return {
            "ok": True, "reason": "",
            "candidate": _describe_verified(git, replacement, stored, identity(replacement)),
        }
    return {
        "ok": False,
        "reason": (
            f"no lineage match for subject {pin['subject']!r} with file set {stored_files} "
            f"under upstream tip {upstream_tip}"
        ),
        "candidate": None,
    }


def approve(
    identifier: str,
    *,
    artifact_hash_arg: str,
    store: ProposalStore,
    repo_dir: Path | str,
    upstream_tip: str | None = None,
    lineage: bool = False,
    manifest_path: Path | str | None = None,
    blocklist_path: Path | str | None = None,
    approver: str | None = None,
    allow_noninteractive: bool = False,
    commit: bool = True,
    patch_id_of: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Apply an approved reconciliation, or refuse and say why.

    Order matters and every step is a refusal point:

      1. interactive channel only (R2/R12: an agent must not be able to
         drive approval);
      2. the operator's ``--artifact-hash`` must equal BOTH the stored
         ``artifact_sha256`` and a fresh recomputation (a tampered fragment
         changes the recomputation);
      3. the candidate must still be upstream's current form;
      4. the manifest edit is RE-DERIVED from that fresh verification and
         must be byte-equal to the stored fragment;
      5. only then: manifest edit + blocklist append + one attributable
         commit + keep-ref + ``state -> applied``.

    Steps 2-4 transition to ``stale-invalidated`` instead of raising, because
    that is a real state in the machine and the next run regenerates from it.
    """
    if not allow_noninteractive and not sys.stdin.isatty():
        raise ProposalError(
            "approval requires an interactive channel (R2): stdin is not a TTY. "
            f"Set {NONINTERACTIVE_ENV}=1 only in tests."
        )
    artifact = store.load(identifier)
    if artifact is None:
        raise ProposalError(f"no such proposal: {identifier}")
    if artifact.get("state") not in OPEN_STATES:
        raise ProposalError(f"proposal {identifier} is {artifact.get('state')}, not open for approval")

    recomputed = artifact_hash(artifact)
    stored_hash = str(artifact.get("artifact_sha256", ""))
    if artifact_hash_arg != stored_hash or artifact_hash_arg != recomputed:
        _mark_stale(
            store, artifact,
            f"artifact hash mismatch (given={artifact_hash_arg}, stored={stored_hash}, recomputed={recomputed})",
        )
        return {"ok": False, "state": artifact["state"], "reason": artifact["stale_reason"], "id": identifier}

    git = Git(repo_dir)
    if upstream_tip:
        resolved_tip = git.resolve(upstream_tip)
    else:
        upstream_ref = str(artifact.get("upstream_ref") or "")
        if not upstream_ref:
            raise ProposalError("proposal has no upstream_ref; pass --upstream-tip explicitly")
        remote = upstream_ref.split("/", 1)[0]
        git.run("fetch", remote, "--prune", check=False)
        resolved_tip = git.resolve(upstream_ref)

    verification = reverify_candidate(
        git, artifact, upstream_tip=resolved_tip, lineage=lineage, patch_id_of=patch_id_of,
    )
    if not verification["ok"]:
        _mark_stale(store, artifact, verification["reason"])
        return {
            "ok": False, "state": artifact["state"], "reason": verification["reason"], "id": identifier,
            "guidance": "the next run regenerates this proposal with fresh evidence; re-approve then",
            "churn_livelock": bool(artifact.get("churn_livelock")),
        }

    rederived = derive_manifest_edit(artifact["pin"], verification["candidate"])
    stored_edit = artifact.get("recommended_edit") or {}
    if lineage:
        comparable = {k: v for k, v in rederived.items() if k not in LINEAGE_MUTABLE_EDIT_KEYS}
        stored_comparable = {k: v for k, v in stored_edit.items() if k not in LINEAGE_MUTABLE_EDIT_KEYS}
    else:
        comparable, stored_comparable = rederived, stored_edit
    if canonical_bytes(comparable) != canonical_bytes(stored_comparable):
        reason = "re-derived manifest edit is not byte-equal to the stored fragment"
        _mark_stale(store, artifact, reason)
        return {
            "ok": False, "state": artifact["state"], "reason": reason, "id": identifier,
            "guidance": "the next run regenerates this proposal with fresh evidence; re-approve then",
        }

    repo_dir = Path(repo_dir)
    manifest_file = _repo_file(repo_dir, MANIFEST_FILENAME, manifest_path)
    blocklist_file = _repo_file(repo_dir, BLOCKLIST_FILENAME, blocklist_path)
    approver = approver or getpass.getuser()
    approved_at = _now()
    candidate = verification["candidate"]

    apply_manifest_edit(manifest_file, rederived)

    superseded = [
        value for value in artifact.get("superseded_patch_ids", [])
        if value not in {candidate["stable_patch_id"], artifact["pin"]["stable_patch_id"]}
    ]
    superseded.extend(
        str(item["stable_patch_id"]) for item in artifact.get("candidates", [])
        if str(item["stable_patch_id"]) not in {candidate["stable_patch_id"], artifact["pin"]["stable_patch_id"]}
    )
    superseded = sorted(set(superseded))
    if superseded:
        append_blocklist_entries(blocklist_file, [
            {
                "patch_id": value, "pin_id": artifact["pin"]["id"],
                "reason": f"superseded by approved absorption of {candidate['sha']} (proposal {identifier})",
                "actor": approver, "recorded_at": approved_at,
            }
            for value in superseded
        ])

    message = (
        f"fix(fork-integration): absorb upstream rewrite of {artifact['pin']['id']} ({identifier})\n"
        "\n"
        f"Approved-By: {approver} at {approved_at}\n"
        f"Candidate: {candidate['sha']} (patch-id {candidate['stable_patch_id']})\n"
        f"Pin: {artifact['pin']['kind']} {artifact['pin']['id']} {artifact['pin']['commit']}\n"
        f"Proposal: {identifier} (artifact sha256 {stored_hash})\n"
        f"Superseded-Patch-Ids: {', '.join(superseded) if superseded else 'none'}\n"
        f"Approval-Mode: {'lineage' if lineage else 'patch-id'}\n"
        "\n"
        f"{_COMMIT_FOOTER}\n"
    )
    commit_sha = ""
    if commit:
        paths = [str(manifest_file)] + ([str(blocklist_file)] if superseded else [])
        git.run("add", "--", *paths)
        git.run("commit", "-m", message)
        commit_sha = git.text("rev-parse", "HEAD")

    keep_ref = git.keep_ref(
        artifact["pin"]["id"], artifact["pin"]["stable_patch_id"], artifact["pin"]["commit"],
    )

    artifact["state"] = STATE_APPLIED
    artifact["updated_at"] = approved_at
    artifact["approval"] = {
        "approver": approver, "approved_at": approved_at, "candidate": candidate["sha"],
        "candidate_stable_patch_id": candidate["stable_patch_id"], "lineage": bool(lineage),
        "commit": commit_sha, "upstream_tip": resolved_tip,
    }
    artifact.setdefault("history", []).append({
        "at": approved_at, "event": "applied", "detail": f"{approver} -> {candidate['sha']}",
    })
    store.save(artifact)

    manifest_sha256 = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
    blocklist_sha256 = hashlib.sha256(blocklist_file.read_bytes()).hexdigest() if blocklist_file.exists() else ""
    return {
        "ok": True, "id": identifier, "state": STATE_APPLIED, "commit": commit_sha,
        "approver": approver, "candidate": candidate["sha"],
        "accepted_output_patch_id": candidate["stable_patch_id"],
        "superseded_patch_ids": superseded, "keep_ref": keep_ref,
        "manifest_sha256": manifest_sha256, "blocklist_sha256": blocklist_sha256,
        "restamp_commands": restamp_commands(repo_dir, manifest_sha256, blocklist_sha256),
    }


def restamp_commands(repo_dir: Path | str, manifest_sha256: str, blocklist_sha256: str) -> list[str]:
    """The U2 entry-point commands an operator must run so the approved edit
    reaches the operational copies attributably (KTD2 approach step 5)."""
    sync_script = Path(repo_dir) / REPO_TRACKED_SUBDIR / SYNC_FILENAME
    dest = Path.home() / "AppData" / "Local" / "hermes" / "scripts"
    commands = [
        f'python "{sync_script}" restamp-file --name {MANIFEST_FILENAME} '
        f'--approved-sha256 {manifest_sha256} --repo "{repo_dir}" --dest "{dest}"'
    ]
    if blocklist_sha256:
        commands.append(
            f'python "{sync_script}" restamp-file --name {BLOCKLIST_FILENAME} '
            f'--approved-sha256 {blocklist_sha256} --repo "{repo_dir}" --dest "{dest}"'
        )
    return commands


def reject(
    identifier: str,
    *,
    reason: str,
    store: ProposalStore,
    repo_dir: Path | str,
    blocklist_path: Path | str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Reject a proposal and blocklist its candidates (R3).

    Every candidate patch-id in the artifact is recorded, so a rejected
    rewrite can never come back as an "equivalent" absorption or as a fresh
    proposal on the next run.
    """
    if not reason.strip():
        raise ProposalError("rejection requires a reason")
    artifact = store.load(identifier)
    if artifact is None:
        raise ProposalError(f"no such proposal: {identifier}")
    if artifact.get("state") == STATE_APPLIED:
        raise ProposalError(f"proposal {identifier} is already applied")
    actor = actor or getpass.getuser()
    rejected_at = _now()
    blocklist_file = _repo_file(Path(repo_dir), BLOCKLIST_FILENAME, blocklist_path)
    patch_ids = sorted({
        str(item["stable_patch_id"]) for item in artifact.get("candidates", [])
        if str(item["stable_patch_id"]) != artifact["pin"]["stable_patch_id"]
    })
    append_blocklist_entries(blocklist_file, [
        {
            "patch_id": value, "pin_id": artifact["pin"]["id"],
            "reason": reason, "actor": actor, "recorded_at": rejected_at,
        }
        for value in patch_ids
    ])
    artifact["state"] = STATE_REJECTED
    artifact["updated_at"] = rejected_at
    artifact["rejection"] = {"actor": actor, "rejected_at": rejected_at, "reason": reason}
    artifact.setdefault("history", []).append({"at": rejected_at, "event": "rejected", "detail": reason})
    store.save(artifact)
    return {
        "ok": True, "id": identifier, "state": STATE_REJECTED,
        "blocklisted_patch_ids": patch_ids, "actor": actor,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────


def _default_repo_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="proposals.py",
        description="U6 reconciliation proposals: list, approve, or reject upstream-churn proposals.",
    )
    parser.add_argument("--proposals-dir", type=Path, default=None, help="Artifact store root (default: HERMES_HOME review-artifacts).")
    parser.add_argument("--repo", type=Path, default=None, help="Repository clone holding scripts/fork_integration/ (default: this checkout).")
    subparsers = parser.add_subparsers(dest="command", required=True)

    listing = subparsers.add_parser("list", help="List proposals and their states.")
    listing.add_argument("--all", action="store_true", help="Include closed proposals (default: open only).")

    approve_parser = subparsers.add_parser("approve", help="Approve a proposal (interactive channel only).")
    approve_parser.add_argument("id")
    approve_parser.add_argument("--artifact-hash", required=True, dest="artifact_hash")
    approve_parser.add_argument("--lineage", action="store_true", help="Approve the lineage (subject + file set) instead of an exact patch-id.")
    approve_parser.add_argument("--upstream-tip", default=None, help="Skip the fetch and re-verify against this tip.")
    approve_parser.add_argument("--manifest", type=Path, default=None)
    approve_parser.add_argument("--blocklist", type=Path, default=None)
    approve_parser.add_argument("--no-commit", action="store_true", help="Edit the files but do not create the commit.")

    reject_parser = subparsers.add_parser("reject", help="Reject a proposal and blocklist its candidates.")
    reject_parser.add_argument("id")
    reject_parser.add_argument("--reason", required=True)
    reject_parser.add_argument("--blocklist", type=Path, default=None)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    store = ProposalStore(args.proposals_dir)
    repo_dir = args.repo or _default_repo_dir()
    try:
        if args.command == "list":
            artifacts = store.list_all() if args.all else store.list_open()
            print(json.dumps({
                "ok": True,
                "count": len(artifacts),
                "proposals": [
                    {
                        "id": item.get("id"), "state": item.get("state"), "evidence": item.get("evidence"),
                        "pin": item.get("pin", {}).get("id"), "subject": item.get("pin", {}).get("subject"),
                        "regen_count": item.get("regen_count"), "churn_livelock": item.get("churn_livelock"),
                        "low_confidence": item.get("low_confidence"),
                        "artifact_sha256": item.get("artifact_sha256"),
                    }
                    for item in artifacts
                ],
            }, indent=2, sort_keys=True))
            return 0
        if args.command == "approve":
            outcome = approve(
                args.id, artifact_hash_arg=args.artifact_hash, store=store, repo_dir=repo_dir,
                upstream_tip=args.upstream_tip, lineage=args.lineage, manifest_path=args.manifest,
                blocklist_path=args.blocklist, commit=not args.no_commit,
                allow_noninteractive=os.environ.get(NONINTERACTIVE_ENV, "").strip() in {"1", "true", "yes"},
            )
            print(json.dumps(outcome, indent=2, sort_keys=True))
            if outcome.get("ok"):
                for command in outcome.get("restamp_commands", []):
                    print(f"NEXT (U2 sync boundary): {command}")
                return 0
            return 2
        if args.command == "reject":
            outcome = reject(
                args.id, reason=args.reason, store=store, repo_dir=repo_dir, blocklist_path=args.blocklist,
            )
            print(json.dumps(outcome, indent=2, sort_keys=True))
            return 0
    except ProposalError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 2
    raise AssertionError(f"unhandled command: {args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
