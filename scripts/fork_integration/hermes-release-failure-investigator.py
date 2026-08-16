#!/usr/bin/env python
"""Create one durable Desktop investigation session for an integration-release failure.

The release process imports this file by path.  It deliberately accepts only a
sanitized incident artifact when run as a detached helper: credentials and the
generated prompt never cross the process boundary in argv.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ARTIFACT_SUBDIR = Path("review-artifacts") / "release-failures"
STATE_SUBDIR = Path("cron") / "failure-investigators"
# U9/KTD12: spawner-minted authority records live beside the incident state.
AUTHORITY_SUBDIR = Path("cron") / "authority"
# U9/KTD5: hard cap on a finish-authority window, regardless of the schedule.
AUTHORITY_MAX_WINDOW_SECONDS = 4 * 60 * 60
#: The only privileged actions a token may ever carry (R12).
AUTHORITY_ACTIONS = ("push", "publish")
# The helper must give the standing goal enough time to reproduce and verify a
# local fix.  Tests inject a small budget through ``run_artifact``.
LIFECYCLE_SECONDS = 2 * 60 * 60
GOAL_STATUS_INTERVAL_SECONDS = 2.0
_IN_PROCESS_STATE_LOCK = threading.RLock()


def default_home() -> Path:
    """The Hermes home this helper reads state from when not given one."""
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return Path(configured)
    return Path.home() / "AppData" / "Local" / "hermes"


def python_executable() -> str:
    return sys.executable


def redact(text: str) -> str:
    """Redact the credential shapes shared with the release script."""
    redacted = re.sub(r"://[^\s/@]+@", "://[REDACTED]@", text)
    redacted = re.sub(r"(?i)(\bAuthorization\s*:\s*(?:Bearer|Basic)\s+)[^\s,;]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(\b(?:Bearer|Basic)\s+)[^\s,;]+", r"\1[REDACTED]", redacted)
    return re.sub(
        r'''(?ix)((?:"|')?\b(?:password|token|secret|api[_-]?key)\b(?:"|')?\s*(?:=|:)\s*)(?:"(?:\\.|[^"])*"|'(?:\\.|[^'])*'|[^\s,;\}\]]+)''',
        r"\1[REDACTED]", redacted,
    )


def normalize_error(error: str) -> str:
    text = redact(error).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\b[0-9a-fA-F]{7,40}\b", "<SHA>", text)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}[T ][0-2]\d:[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?\b", "<TIMESTAMP>", text)
    text = re.sub(r"(?i)\bpid\s*(?:=|:|\s)\s*\d+\b", "PID=<PID>", text)
    return re.sub(r"\s+", " ", text).strip()[:4000]


def signature_for(job_id: str, stage: str, normalized_error: str) -> str:
    payload = json.dumps([job_id, stage, normalized_error], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        temp = Path(handle.name)
    os.replace(temp, path)


@contextmanager
def _state_lock(path: Path):
    with _IN_PROCESS_STATE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8")
        locked = False
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write("0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            try:
                if locked and os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                elif locked:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


# ── U9/KTD12/R20: spawner-minted finish authority ───────────────────────────
#
# HONEST LIMIT, stated once here and repeated at the enforcement point in
# ``hermes-integration-release-windows.py``: this host runs the scheduler, the
# investigator session, and the operator under ONE user account with ambient
# git/gh credentials.  A file-based token therefore bounds accidents, drift,
# and prompt-drift -- an investigator can only finish a release inside a
# window that a NON-AGENT process minted, for the incident it was spawned for,
# and only for the two actions the window names -- but it is not a control
# against a fully hostile in-context agent, which could mint its own record.
# No HMAC is used: a shared secret readable by the same account would be
# theater.  The enforcement point outside this host is GitHub branch
# protection on ``origin/fork-integration`` (deferred, user-owned; R20).


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 instant into an aware UTC datetime, or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def authority_token_path(home: Path, job_id: str, signature: str) -> Path:
    return Path(home) / AUTHORITY_SUBDIR / f"{job_id}-{signature}.json"


def next_scheduled_fire(home: Path, job_id: str) -> datetime | None:
    """This job's next scheduled fire from the cron jobs store.

    ``None`` means "no resolvable next fire" -- an unreadable store, a job
    that is absent or disabled, or an unparseable ``next_run_at``.  KTD5
    makes that case expire the window immediately rather than fall back to
    the 4h cap: an authority window that cannot be bounded by the schedule
    is not granted at all.
    """
    try:
        store = json.loads((Path(home) / "cron" / "jobs.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    jobs = store.get("jobs") if isinstance(store, dict) else None
    if not isinstance(jobs, list):
        return None
    for job in jobs:
        if not isinstance(job, dict) or str(job.get("id", "")) != job_id:
            continue
        if job.get("enabled") is False:
            return None
        return parse_timestamp(job.get("next_run_at"))
    return None


def authority_window_end(
    issued_at: datetime,
    next_fire: datetime | None,
    *,
    max_seconds: float = AUTHORITY_MAX_WINDOW_SECONDS,
    cap: datetime | None = None,
) -> datetime:
    """KTD5's ``min(next scheduled fire, issue + 4h)``, frozen at mint.

    ``cap`` bounds a REPLACEMENT finisher to the abandoned finisher's own
    window end, so a die-and-replace cycle can never walk the authority
    window forward.

    Deviation from KTD5's wording, recorded deliberately: KTD5 says the
    window is computed "from a monotonic timestamp".  There is no monotonic
    clock shared across the spawner process, the investigator session, and
    the release process -- ``time.monotonic()`` is per-process by
    definition.  The mechanism that delivers KTD5's actual intent (a
    schedule edit after the spawn cannot extend a live window) is the
    FROZEN wall-clock ``expires_at``: it is computed once, at mint, written
    into the record, and never recomputed by any later reader.
    """
    end = issued_at + timedelta(seconds=max(0.0, max_seconds))
    if next_fire is None:
        # No resolvable next fire: issue an already-expired window rather
        # than an unbounded one (KTD5).
        return issued_at
    end = min(end, next_fire)
    if cap is not None:
        end = min(end, cap)
    # A next fire already in the past yields an immediately-expired window
    # rather than a negative one.
    return max(end, issued_at)


def authority_claim(token: dict[str, Any]) -> dict[str, Any]:
    """The immutable half of an authority record.

    ``session_id`` is deliberately excluded: it is patched in after
    ``session.create`` returns and must not change the record's identity.
    ``token_sha256`` is excluded because it is the digest OF this claim.
    """
    return {
        "job_id": str(token.get("job_id", "")),
        "incident_signature": str(token.get("incident_signature", "")),
        "issued_at": str(token.get("issued_at", "")),
        "expires_at": str(token.get("expires_at", "")),
        "allowed_actions": sorted(str(action) for action in token.get("allowed_actions", []) or []),
    }


def authority_token_sha256(token: dict[str, Any]) -> str:
    payload = json.dumps(authority_claim(token), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def mint_authority_token(
    *,
    home: Path,
    job_id: str,
    signature: str,
    now: datetime | None = None,
    cap: datetime | None = None,
    allowed_actions: tuple[str, ...] = AUTHORITY_ACTIONS,
) -> dict[str, Any]:
    """Write the authority record for one spawned finisher and return it.

    Returns ``{"path", "token", "token_sha256"}``.  The caller records
    ``token_sha256`` in the incident entry; that record -- not the token
    file's own self-declared digest -- is what the release script trusts.
    """
    issued = (now or _utc_now()).astimezone(timezone.utc)
    expires = authority_window_end(issued, next_scheduled_fire(home, job_id), cap=cap)
    token = {
        "schema": 1,
        "job_id": job_id,
        "incident_signature": signature,
        "session_id": None,
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
        "allowed_actions": [str(action) for action in allowed_actions],
    }
    token["token_sha256"] = authority_token_sha256(token)
    path = authority_token_path(home, job_id, signature)
    _atomic_json(path, token)
    return {"path": path, "token": token, "token_sha256": token["token_sha256"]}


def attach_session_to_authority(token_path: Path, session_id: str) -> bool:
    """Patch the created session id into a minted record (digest-neutral)."""
    try:
        token = json.loads(Path(token_path).read_text(encoding="utf-8"))
        if not isinstance(token, dict):
            return False
        token["session_id"] = session_id
        _atomic_json(Path(token_path), token)
        return True
    except (OSError, json.JSONDecodeError):
        return False


def incident_state_path(home: Path, job_id: str) -> Path:
    return Path(home) / STATE_SUBDIR / f"{job_id}.json"


def read_incident_state(path: Path) -> dict[str, Any]:
    """Read an incident state file without writing anything."""
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def _open_incidents(state: dict[str, Any]) -> dict[str, Any]:
    open_incidents = state.get("open")
    return open_incidents if isinstance(open_incidents, dict) else {}


def _closed_incidents(state: dict[str, Any]) -> list[dict[str, Any]]:
    closed = state.get("closed")
    return [entry for entry in closed if isinstance(entry, dict)] if isinstance(closed, list) else []


# ── U9/KTD5/R11: incident schema v2 -- heartbeat, closures, one finisher ────

#: ``open`` holds only LIVE incidents; a closed one moves to ``closed``
#: carrying its ``closure`` and its signature. Entries gain ``session_id``,
#: ``spawned_at``, ``heartbeat_at``, ``token_sha256`` (the authority digest
#: the release gate checks against) and ``token_expires_at``.
INCIDENT_SCHEMA = 2
#: A finisher that has not beaten in this long is treated as dead (R11).
#: This interval is also what damps a die-and-replace cycle: a replacement
#: is born fresh, so nothing can replace IT for at least this long.
HEARTBEAT_STALE_SECONDS = 20 * 60
CLOSURE_STATES = ("resolved", "expired", "abandoned", "superseded")


def _v2_entry(entry: dict[str, Any] | None = None) -> dict[str, Any]:
    """One incident entry with every v2 field present."""
    source = entry if isinstance(entry, dict) else {}
    merged: dict[str, Any] = {
        "occurrences": int(source.get("occurrences", 0) or 0),
        "stage": str(source.get("stage", "")),
        "status": str(source.get("status", "pending")),
        "session_id": source.get("session_id"),
        "spawned_at": source.get("spawned_at"),
        "heartbeat_at": source.get("heartbeat_at"),
        "token_sha256": source.get("token_sha256"),
        "token_expires_at": source.get("token_expires_at"),
        "closure": source.get("closure"),
    }
    for key, value in source.items():
        merged.setdefault(key, value)
    return merged


def blank_incident_state(job_id: str) -> dict[str, Any]:
    return {"schema": INCIDENT_SCHEMA, "job_id": job_id, "open": {}, "closed": [], "failed": {}}


def migrate_incident_state(state: Any, job_id: str, *, now: datetime | None = None) -> tuple[dict[str, Any], bool]:
    """Return ``(v2 state, migrated?)`` for any state file shape.

    A schema-1 file's open incidents predate the authority window, the
    heartbeat, and the session link: nothing can prove whether their
    "admitted" investigator is still alive, and no token was ever minted for
    them, so they can never authorize anything.  They are closed
    ``superseded`` rather than silently upgraded -- generically, for any open
    schema-1 entry (which on this host is the two 2026-08-15 incidents).
    """
    if not isinstance(state, dict) or not state:
        return blank_incident_state(job_id), False
    state.setdefault("job_id", job_id)
    if not isinstance(state.get("open"), dict):
        state["open"] = {}
    if not isinstance(state.get("failed"), dict):
        state["failed"] = {}
    if not isinstance(state.get("closed"), list):
        state["closed"] = []
    if state.get("schema") == INCIDENT_SCHEMA:
        return state, False
    stamp = (now or _utc_now()).isoformat()
    for signature, entry in list(state["open"].items()):
        state["closed"].append({
            **_v2_entry(entry if isinstance(entry, dict) else None),
            "signature": signature,
            "closure": {"state": "superseded", "at": stamp, "reason": "schema-v2 migration"},
        })
    state["open"] = {}
    state["schema"] = INCIDENT_SCHEMA
    return state, True


def load_incident_state(state_path: Path, job_id: str, *, now: datetime | None = None) -> tuple[dict[str, Any], bool]:
    """Read + migrate in memory. Callers holding the lock persist the result."""
    return migrate_incident_state(read_incident_state(state_path), job_id, now=now)


def _artifact_path_for(home: Path, signature: str) -> Path:
    return Path(home) / ARTIFACT_SUBDIR / f"{signature}.json"


def _record_in_artifact(artifact_path: Path, updates: dict[str, Any]) -> None:
    """Best-effort artifact merge: a diagnostics write never fails a caller."""
    try:
        artifact = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
        if not isinstance(artifact, dict):
            return
        artifact.update(updates)
        _atomic_json(Path(artifact_path), artifact)
    except (OSError, json.JSONDecodeError):
        return


def close_incident(state: dict[str, Any], signature: str, *, closure_state: str, reason: str,
                   home: Path | None = None, now: datetime | None = None) -> dict[str, Any] | None:
    """Move one open incident into ``closed`` with a closure record.

    Mutates ``state`` in place; the caller holds the lock and persists.  The
    closure is mirrored into the incident artifact so the record travels with
    the evidence the run summary points at.
    """
    if closure_state not in CLOSURE_STATES:
        raise ValueError(f"unknown closure state: {closure_state}")
    entry = state.get("open", {}).pop(signature, None)
    if not isinstance(entry, dict):
        return None
    closure = {"state": closure_state, "at": (now or _utc_now()).isoformat(), "reason": reason}
    closed_entry = {**_v2_entry(entry), "signature": signature, "closure": closure}
    state.setdefault("closed", []).append(closed_entry)
    if home is not None:
        _record_in_artifact(_artifact_path_for(home, signature), {
            "closure": closure, "investigator_status": f"closed_{closure_state}",
        })
    return closed_entry


def finisher_liveness(entry: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Is this incident's finisher still alive and still in its window?

    "Dead session" is detected through the heartbeat, not through a session
    probe: the session is the only thing that beats, so a session that died,
    hung, or was closed stops beating.  Stated as a limit rather than
    claimed as liveness detection.
    """
    moment = (now or _utc_now()).astimezone(timezone.utc)
    if not entry.get("token_sha256"):
        return {"finisher": False, "live": False, "reason": "no_finisher"}
    expires = parse_timestamp(entry.get("token_expires_at"))
    if expires is None or moment >= expires:
        return {"finisher": True, "live": False, "reason": "window_expired", "expires_at": entry.get("token_expires_at")}
    beat = parse_timestamp(entry.get("heartbeat_at")) or parse_timestamp(entry.get("spawned_at"))
    if beat is None or (moment - beat).total_seconds() >= HEARTBEAT_STALE_SECONDS:
        return {"finisher": True, "live": False, "reason": "heartbeat_stale",
                "last_beat_at": entry.get("heartbeat_at") or entry.get("spawned_at"),
                "expires_at": entry.get("token_expires_at")}
    return {"finisher": True, "live": True, "reason": "live", "expires_at": entry.get("token_expires_at")}


def heartbeat(*, home: Path, job_id: str, signature: str, now: datetime | None = None) -> dict[str, Any]:
    """Record liveness for one open incident (called by the session itself)."""
    moment = (now or _utc_now()).astimezone(timezone.utc)
    state_path = incident_state_path(home, job_id)
    with _state_lock(state_path.with_suffix(".lock")):
        state, _migrated = load_incident_state(state_path, job_id, now=moment)
        entry = state["open"].get(signature)
        if not isinstance(entry, dict) or entry.get("closure"):
            _atomic_json(state_path, state)
            return {"ok": False, "reason": "incident_not_open", "job_id": job_id, "signature": signature}
        entry["heartbeat_at"] = moment.isoformat()
        _atomic_json(state_path, state)
    _record_in_artifact(_artifact_path_for(home, signature), {"heartbeat_at": moment.isoformat()})
    return {"ok": True, "job_id": job_id, "signature": signature, "heartbeat_at": moment.isoformat()}


def verify_authority(
    *,
    token_path: Any,
    job_id: str,
    action: str,
    home: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate one privileged action against a minted authority record.

    Called immediately before EACH privileged action (never once per run),
    so a window that expires mid-run refuses the next action.  Read-only:
    it never mutates the token, the incident state, or the artifact.

    Refusal reasons name the check that failed:
    ``authority_token_absent``, ``authority_token_unparseable``,
    ``authority_token_malformed``, ``authority_token_job_mismatch``,
    ``authority_token_expired``, ``authority_action_not_allowed``,
    ``authority_incident_record_missing``, ``authority_incident_closed``,
    ``authority_incident_token_unrecorded``,
    ``authority_token_sha256_mismatch``.
    """
    moment = (now or _utc_now()).astimezone(timezone.utc)
    verdict: dict[str, Any] = {"ok": False, "action": action, "job_id": job_id, "checked_at": moment.isoformat()}

    if not token_path:
        return {**verdict, "reason": "authority_token_absent"}
    path = Path(token_path)
    try:
        token = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {**verdict, "reason": "authority_token_unparseable", "detail": type(exc).__name__, "token_path": str(path)}
    if not isinstance(token, dict):
        return {**verdict, "reason": "authority_token_unparseable", "detail": "not a JSON object", "token_path": str(path)}

    signature = token.get("incident_signature")
    allowed = token.get("allowed_actions")
    if not isinstance(signature, str) or not signature or not isinstance(allowed, list):
        return {**verdict, "reason": "authority_token_malformed", "token_path": str(path)}
    verdict["incident_signature"] = signature

    if str(token.get("job_id", "")) != job_id:
        return {**verdict, "reason": "authority_token_job_mismatch", "token_job_id": str(token.get("job_id", ""))}

    expires = parse_timestamp(token.get("expires_at"))
    if expires is None:
        return {**verdict, "reason": "authority_token_malformed", "detail": "expires_at is not an ISO instant"}
    verdict["expires_at"] = expires.isoformat()
    if moment >= expires:
        return {**verdict, "reason": "authority_token_expired"}

    if action not in [str(item) for item in allowed]:
        return {**verdict, "reason": "authority_action_not_allowed", "allowed_actions": [str(item) for item in allowed]}

    state = read_incident_state(incident_state_path(home, job_id))
    entry = _open_incidents(state).get(signature)
    if not isinstance(entry, dict) or entry.get("closure"):
        closed = any(str(item.get("signature", "")) == signature for item in _closed_incidents(state))
        return {**verdict, "reason": "authority_incident_closed" if (closed or isinstance(entry, dict)) else "authority_incident_record_missing"}
    recorded = entry.get("token_sha256")
    if not isinstance(recorded, str) or not recorded:
        return {**verdict, "reason": "authority_incident_token_unrecorded"}
    # A plain comparison: both digests are non-secret values recorded in
    # files this account can read. A constant-time compare here would imply
    # a secrecy property this design deliberately does not claim.
    if recorded != authority_token_sha256(token):
        return {**verdict, "reason": "authority_token_sha256_mismatch"}
    return {**verdict, "ok": True, "reason": "authority_granted", "session_id": token.get("session_id")}


def record_failure(*, job_id: str, stage: str, error: str, home: Path, worktree: Path,
                   script_path: Path, log_path: Path, test_path: Path, manifest_path: Path,
                   investigator: dict[str, str] | None = None) -> dict[str, Any]:
    """Atomically record a sanitized failure and decide whether it is newly open.

    ``spawn`` here means only "this signature was not already open".  Whether
    a finisher is actually launched is decided by
    ``plan_investigator_launch`` against the job's live finisher (R11), not
    by this flag alone.
    """
    normalized = normalize_error(error)
    signature = signature_for(job_id, stage, normalized)
    state_path = home / STATE_SUBDIR / f"{job_id}.json"
    artifact_path = home / ARTIFACT_SUBDIR / f"{signature}.json"
    now = _utc_now()
    with _state_lock(state_path.with_suffix(".lock")):
        state, _migrated = load_incident_state(state_path, job_id, now=now)
        open_incidents = state["open"]
        failed_incidents = state["failed"]
        previous = open_incidents.get(signature)
        previous = previous if isinstance(previous, dict) and not previous.get("closure") else None
        occurrence = int(previous.get("occurrences", 0)) + 1 if previous else 1
        spawn = previous is None
        status = "admitted" if (previous or {}).get("status") == "admitted" else "pending"
        entry = _v2_entry(previous)
        entry.update({"occurrences": occurrence, "stage": stage, "status": status, "last_seen_at": now.isoformat()})
        entry.setdefault("first_seen_at", now.isoformat())
        open_incidents[signature] = entry
        failed_incidents.pop(signature, None)
        _atomic_json(state_path, state)
        artifact = {
            "schema": 2, "job_id": job_id, "stage": stage, "failure_class": stage,
            "signature": signature, "occurrences": occurrence, "normalized_error": normalized,
            "artifact_path": str(artifact_path), "state_path": str(state_path),
            "home": str(home),
            "investigator_status": status,
            "session_id": entry.get("session_id"),
            "spawned_at": entry.get("spawned_at"),
            "heartbeat_at": entry.get("heartbeat_at"),
            "worktree": str(worktree),
            "paths": {"release_script": str(script_path), "log": str(log_path), "tests": str(test_path), "manifest": str(manifest_path)},
            "investigator": dict(investigator or {}),
        }
        _atomic_json(artifact_path, artifact)
    return {
        "spawn": spawn, "signature": signature, "occurrences": occurrence,
        "artifact_path": str(artifact_path), "job_id": job_id, "home": str(home),
        "state_path": str(state_path),
    }


def _artifact_state_path(artifact: dict[str, Any]) -> Path | None:
    value = artifact.get("state_path")
    return Path(value) if isinstance(value, str) and value else None


def _update_investigator_status(artifact_path: Path, artifact: dict[str, Any], *, status: str,
                                failure: str | None = None) -> None:
    """Update state and artifact under one lock using sanitized status values."""
    state_path = _artifact_state_path(artifact)
    signature = artifact.get("signature")
    if state_path is None or not isinstance(signature, str) or not signature:
        return
    with _state_lock(state_path.with_suffix(".lock")):
        state, _migrated = load_incident_state(state_path, str(artifact.get("job_id", "")))
        open_incidents = state["open"]
        failed_incidents = state["failed"]
        if status == "investigator_failed":
            # A pre-admission failure retires the incident AND its minted
            # authority: with no open entry, no token can be validated.
            open_incidents.pop(signature, None)
            failed_incidents[signature] = {"status": status, "reason": failure or "admission_failed"}
        else:
            incident = open_incidents.get(signature)
            if isinstance(incident, dict):
                incident["status"] = status
        _atomic_json(state_path, state)
        artifact["investigator_status"] = status
        if failure:
            artifact["investigator_failure"] = failure
        else:
            artifact.pop("investigator_failure", None)
        _atomic_json(artifact_path, artifact)


def mark_investigator_failed(artifact_path: Path, artifact: dict[str, Any], reason: str) -> None:
    """Make a pre-admission incident retryable without writing exception text."""
    _update_investigator_status(artifact_path, artifact, status="investigator_failed", failure=reason)


def mark_investigator_admitted(artifact_path: Path, artifact: dict[str, Any]) -> None:
    """Persist prompt admission; later bounded-helper exit must not clear dedupe."""
    _update_investigator_status(artifact_path, artifact, status="admitted")


def resolve_success(job_id: str, home: Path) -> None:
    """Only a completed real release resolves prior incidents for this job.

    v2: resolution is a closure transition, not a delete -- the incident (and
    its finisher's session id and heartbeat history) stays auditable, and any
    token it minted stops validating the moment it leaves ``open``.
    """
    state_path = Path(home) / STATE_SUBDIR / f"{job_id}.json"
    if not state_path.is_file():
        return
    with _state_lock(state_path.with_suffix(".lock")):
        state, migrated = load_incident_state(state_path, job_id)
        closed_any = False
        for signature in list(state["open"]):
            if close_incident(state, signature, closure_state="resolved",
                              reason="release completed successfully", home=Path(home)) is not None:
                closed_any = True
        if closed_any or migrated:
            _atomic_json(state_path, state)


def _windows_hidden_kwargs(*, detached: bool) -> dict[str, Any]:
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if detached:
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    return {"startupinfo": startupinfo, "creationflags": flags}


def spawn_detached(argv: list[str]) -> None:
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_windows_hidden_kwargs(detached=True))


def plan_investigator_launch(*, home: Path, job_id: str, signature: str,
                             artifact_path: Path | None = None,
                             now: datetime | None = None) -> dict[str, Any]:
    """Decide attach-vs-spawn for this job and mint the token when spawning.

    One active finisher per job per authority window (R11):

    - A LIVE finisher (unexpired window AND fresh heartbeat) absorbs the new
      failure: the occurrence is appended to its incident and no session is
      spawned.
    - A finisher whose heartbeat went stale is closed ``abandoned`` and gets
      EXACTLY ONE replacement, whose window is capped at the abandoned
      finisher's own window end so a die-and-replace cycle cannot walk the
      window forward.
    - A finisher whose window simply ran out is closed ``expired``; that is
      the window ending, not a death, so it alone does not justify a
      replacement inside the same pass.
    - ``spawn_backoff`` damps a pathological rapid death cycle.

    Runs under the incident state lock and returns the decision; the caller
    performs the (unlockable) detached spawn.
    """
    moment = (now or _utc_now()).astimezone(timezone.utc)
    state_path = incident_state_path(home, job_id)
    artifact = Path(artifact_path) if artifact_path else _artifact_path_for(home, signature)
    decision: dict[str, Any] = {"action": "skip", "reason": "unknown", "signature": signature,
                                "closed": [], "job_id": job_id}
    with _state_lock(state_path.with_suffix(".lock")):
        state, _migrated = load_incident_state(state_path, job_id, now=moment)
        live_signature: str | None = None
        stale: list[tuple[str, dict[str, Any]]] = []
        expired: list[tuple[str, dict[str, Any]]] = []
        for candidate, entry in list(state["open"].items()):
            if not isinstance(entry, dict):
                continue
            liveness = finisher_liveness(entry, now=moment)
            if not liveness["finisher"]:
                continue
            if liveness["live"]:
                live_signature = live_signature or candidate
            elif liveness["reason"] == "heartbeat_stale":
                stale.append((candidate, entry))
            else:
                expired.append((candidate, entry))

        if live_signature is not None:
            live_entry = state["open"][live_signature]
            if live_signature != signature:
                attached = live_entry.setdefault("attached", [])
                if isinstance(attached, list):
                    attached.append({"signature": signature, "at": moment.isoformat()})
            _atomic_json(state_path, state)
            decision.update({
                "action": "attach", "reason": "live_finisher_in_window",
                "attached_to": live_signature,
                "session_id": live_entry.get("session_id"),
                "expires_at": live_entry.get("token_expires_at"),
            })
            return decision

        cap: datetime | None = None
        last_spawned: datetime | None = None
        for candidate, entry in stale:
            window_end = parse_timestamp(entry.get("token_expires_at"))
            if window_end is not None:
                cap = window_end if cap is None else min(cap, window_end)
            spawned = parse_timestamp(entry.get("spawned_at"))
            if spawned is not None:
                last_spawned = spawned if last_spawned is None else max(last_spawned, spawned)
            close_incident(state, candidate, closure_state="abandoned", home=home, now=moment,
                           reason=("finisher heartbeat stale for at least "
                                   f"{HEARTBEAT_STALE_SECONDS // 60} minutes; "
                                   f"last beat {entry.get('heartbeat_at') or entry.get('spawned_at')}"))
            decision["closed"].append({"signature": candidate, "state": "abandoned"})
        for candidate, entry in expired:
            close_incident(state, candidate, closure_state="expired", home=home, now=moment,
                           reason=f"authority window ended at {entry.get('token_expires_at')}")
            decision["closed"].append({"signature": candidate, "state": "expired"})

        entry = state["open"].get(signature)
        if not isinstance(entry, dict):
            # The failing signature was itself just closed (it WAS the dead
            # finisher). Re-open it so the replacement has an incident.
            entry = _v2_entry({"occurrences": 1, "stage": "", "status": "pending"})
            state["open"][signature] = entry
        if expired and authority_window_end(moment, next_scheduled_fire(home, job_id), cap=cap) <= moment:
            # The previous finisher's window ran out and the schedule offers no
            # new one. Replacing a dead window with another dead window would
            # spawn a powerless session on every subsequent failure.
            _atomic_json(state_path, state)
            decision.update({"action": "skip", "reason": "window_unavailable"})
            return decision

        minted = mint_authority_token(home=home, job_id=job_id, signature=signature, now=moment, cap=cap)
        entry.update({
            "spawned_at": moment.isoformat(),
            "heartbeat_at": None,
            "session_id": None,
            "token_sha256": minted["token_sha256"],
            "token_expires_at": minted["token"]["expires_at"],
        })
        _atomic_json(state_path, state)
        _record_in_artifact(artifact, {
            "spawned_at": entry["spawned_at"],
            "authority": {
                "token_path": str(minted["path"]),
                "token_sha256": minted["token_sha256"],
                "expires_at": minted["token"]["expires_at"],
                "allowed_actions": list(minted["token"]["allowed_actions"]),
            },
        })
        decision.update({
            "action": "spawn",
            "reason": "replacement_for_abandoned_finisher" if stale else "no_live_finisher",
            "token_path": str(minted["path"]),
            "token_sha256": minted["token_sha256"],
            "expires_at": minted["token"]["expires_at"],
        })
        return decision


def maybe_launch_investigator(result: dict[str, Any]) -> dict[str, Any]:
    """Attach to the job's live finisher, or spawn exactly one replacement."""
    home = result.get("home")
    job_id = result.get("job_id")
    signature = result.get("signature")
    if not home or not job_id or not signature:
        # Pre-U9 caller shape: fall back to the old first-occurrence rule
        # rather than silently dropping the investigation.
        if result.get("spawn"):
            spawn_detached([python_executable(), str(Path(__file__)), "--artifact", str(result["artifact_path"])])
            return {"action": "spawn", "reason": "legacy_result_shape"}
        return {"action": "skip", "reason": "legacy_result_shape"}
    decision = plan_investigator_launch(
        home=Path(home), job_id=str(job_id), signature=str(signature),
        artifact_path=Path(result["artifact_path"]) if result.get("artifact_path") else None,
    )
    if decision["action"] == "spawn":
        spawn_detached([
            python_executable(), str(Path(__file__)),
            "--artifact", str(result["artifact_path"]),
            "--authority-token", str(decision["token_path"]),
        ])
    return decision


def session_title(artifact: dict[str, Any]) -> str:
    """The sidebar identity of an investigator session (R11).

    Keyed on the investigator's own identity -- job + incident -- never on
    the ``cron_session`` marker, which nightly RUN sessions use to stay out
    of recents.
    """
    return f"Release investigator · {artifact.get('job_id', 'unknown-job')} · {str(artifact.get('signature', ''))[:8]}"


def heartbeat_command(artifact: dict[str, Any]) -> str:
    return (
        f'"{python_executable()}" "{Path(__file__)}" heartbeat '
        f'--job {artifact.get("job_id", "")} --signature {artifact.get("signature", "")}'
    )


def build_goal(artifact: dict[str, Any], *, authority_token_path: Any = None) -> str:
    """The scoped finish contract (R12).

    Goal text DESCRIBES the contract; the release script's ``require_authority``
    gate ENFORCES the privileged half of it (R20).  Everything forbidden here
    that can be checked mechanically is also checked mechanically: proposal
    approval refuses a non-TTY caller in ``proposals.py``, operational copies
    are tree-verified at run start by ``sync.py``, and push/publish require an
    unexpired token for this incident.
    """
    signature = str(artifact.get("signature", ""))
    token = str(authority_token_path or artifact.get("authority", {}).get("token_path", "<no token minted>"))
    return (
        f"Finish or fail-closed the Hermes integration release for job {artifact.get('job_id', '')} "
        f"incident {signature}. "
        "STEP 1, before ANY mutation: collect orphan evidence -- the origin/fork-integration tip versus the "
        "expected published head, the scheduler worktree HEAD, and the release lock's holder/pid/age -- and write "
        f"it into the incident artifact {artifact.get('artifact_path', '')}. An orphaned run may already be "
        "mid-publish; deciding before that evidence exists is the failure mode this step removes. "
        "STEP 2: diagnose from the incident artifact and the run's live cron progress transcript (the per-stage "
        "NDJSON in the run session) before anything else. "
        "STEP 3: classify. AUTOMATION BUG -> make the smallest local fix in the repo checkout, cover it with a "
        "test, commit it to the repo branch, and deploy it to the operational copies ONLY with "
        "`sync.py deploy --from-sha <committed sha> --provisional --reason \"<incident>\"`; then re-run the day's "
        f"release with `--holder investigator-{signature[:8]} --authority-token {token}`. "
        "REAL RECONSTRUCTION CONFLICT (upstream rewrote a pinned patch, or a genuine merge conflict) -> review the "
        "reconstruction review artifact and STOP: that is a human decision. "
        "Allowed inside the authority window, and only through that re-run: force-with-lease push, prerelease "
        "publish, public checksum verification. The window is frozen at spawn and re-checked in code immediately "
        "before each privileged action; when it has expired, stop and report rather than seeking another route. "
        f"Call `{heartbeat_command(artifact)}` between major steps -- a stale heartbeat closes this incident as "
        "abandoned and replaces you. "
        "FORBIDDEN, always, with no exception and no workaround: running any installer or the built Hermes-Setup.exe; "
        "changing cron jobs, schedules, or creating any cron job (no recursive scheduling); changing credentials; "
        "restarting the gateway or the Desktop app; deleting any release that is not an integration-* prerelease "
        "created by this automation; approving a reconciliation proposal (`proposals.py approve` is interactive-only "
        "and refuses a non-TTY caller -- do not attempt it and never set PROPOSALS_ALLOW_NONINTERACTIVE); editing "
        "the operational copies under the Hermes scripts directory directly; any unrelated edit. "
        "Keep this goal active until the release is finished or the blocker is reported. Report the blocked action "
        "instead of working around it."
    )


def build_prompt(artifact: dict[str, Any], *, authority_token_path: Any = None) -> str:
    paths = artifact["paths"]
    token = str(authority_token_path or artifact.get("authority", {}).get("token_path", "<no token minted>"))
    return (
        f"Investigate incident {artifact['signature']} at stage {artifact['stage']}.\n"
        f"Read the sanitized incident artifact: {artifact.get('artifact_path', '<this artifact>')}\n"
        f"Scheduler worktree: {artifact['worktree']}\nRelease script: {paths['release_script']}\n"
        f"Log: {paths['log']}\nTests: {paths['tests']}\nManifest: {paths['manifest']}\n"
        f"Authority token: {token}\n"
        f"Heartbeat: {heartbeat_command(artifact)}\n"
        f"Failure: {artifact['normalized_error']}\n"
        "Start with orphan evidence (origin tip vs expected published head, worktree HEAD, release lock state) and "
        "write it into the artifact before any mutation. Then reproduce the failure, add or adjust a regression "
        "test, and make the smallest local fix; deploy it only through `sync.py deploy --provisional` and re-run "
        "the release with your --holder and --authority-token. Do not run installers, do not change cron jobs or "
        "credentials, do not restart the gateway, do not approve reconciliation proposals, do not edit the "
        "operational copies directly, and do not make unrelated edits."
    )


class StdioTransport:
    def __init__(self, process: subprocess.Popen[str]):
        self.process = process
        self.next_id = 1

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        rid = self.next_id
        self.next_id += 1
        assert self.process.stdin and self.process.stdout
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("tui gateway closed before RPC response")
            response = json.loads(line)
            if response.get("id") == rid:
                return response


def _default_transport() -> tuple[subprocess.Popen[str], StdioTransport]:
    process = subprocess.Popen(
        [python_executable(), "-m", "tui_gateway.entry"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", **_windows_hidden_kwargs(detached=False),
    )
    return process, StdioTransport(process)


def _result(response: dict[str, Any]) -> dict[str, Any] | None:
    value = response.get("result")
    return value if isinstance(value, dict) else None


def _stop_process(process: Any) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _process_is_running(process: Any) -> bool:
    try:
        return process.poll() is None
    except Exception:
        return False


def _goal_status(response: dict[str, Any]) -> str | None:
    """Parse only known goal statuses from a slash response."""
    result = _result(response)
    if not result:
        return None
    status = result.get("status")
    if isinstance(status, str) and status.lower() in {"active", "done", "paused", "blocked"}:
        return status.lower()
    output = result.get("output")
    if not isinstance(output, str):
        return None
    text = output.lower()
    if re.search(r"\bgoal done\b", text):
        return "done"
    if re.search(r"\bgoal\s*\(paused\b|\bgoal paused\b", text):
        return "paused"
    if re.search(r"\bgoal\s*\(blocked\b|\bgoal blocked\b", text):
        return "blocked"
    if re.search(r"\bgoal\s*\(active\b", text):
        return "active"
    return None


def _prompt_was_admitted(response: dict[str, Any]) -> bool:
    return "error" not in response and _result(response) is not None


def session_create_params(artifact: dict[str, Any]) -> dict[str, Any]:
    """The exact ``session.create`` params the spawner uses (R11).

    ``source="desktop"`` is what makes the session visible in the Desktop
    sidebar's recents; ``cron_session`` carries the cron run id for turn
    scoping only (the gateway never persists it, so it can never be the key
    anything filters on).  The title carries job + incident so the session is
    identifiable in the sidebar without opening it.
    """
    cfg = artifact.get("investigator") if isinstance(artifact.get("investigator"), dict) else {}
    return {
        "source": "desktop",
        "cwd": str(artifact["worktree"]),
        "title": session_title(artifact),
        "model": str(cfg.get("model", "")),
        "provider": str(cfg.get("provider", "")),
        "reasoning_effort": str(cfg.get("reasoning_effort", "")),
        "close_on_disconnect": False,
        "cron_session": str(artifact["job_id"]),
    }


def set_incident_session(home: Path, job_id: str, signature: str, session_id: str) -> bool:
    """Link the created session to its incident (R11: incidents carry the id)."""
    state_path = incident_state_path(home, job_id)
    with _state_lock(state_path.with_suffix(".lock")):
        state, migrated = load_incident_state(state_path, job_id)
        entry = state["open"].get(signature)
        if not isinstance(entry, dict):
            if migrated:
                _atomic_json(state_path, state)
            return False
        entry["session_id"] = session_id
        _atomic_json(state_path, state)
    return True


def run_artifact(artifact_path: Path, *, authority_token_path: Path | None = None,
                 transport_factory: Callable[[], tuple[Any, Any]] = _default_transport,
                 lifecycle_seconds: float = LIFECYCLE_SECONDS,
                 poll_interval_seconds: float = GOAL_STATUS_INTERVAL_SECONDS,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> bool:
    """Admit an investigator, then supervise the standing goal for a useful bounded window."""
    artifact: dict[str, Any] | None = None
    process: Any | None = None
    admitted = False
    failure_reason = "artifact_or_gateway_unavailable"
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        if not isinstance(artifact, dict):
            return False
        artifact["artifact_path"] = str(artifact_path)
    except Exception:
        return False
    try:
        process, transport = transport_factory()
        if not _process_is_running(process):
            failure_reason = "gateway_exited_before_admission"
            return False
        created = _result(transport.request("session.create", session_create_params(artifact)))
        if not created or not isinstance(created.get("session_id"), str) or not created["session_id"] or not isinstance(created.get("stored_session_id"), str) or not created["stored_session_id"]:
            failure_reason = "session_create_failed"
            return False
        session_id = created["session_id"]
        # The record was minted before this process existed; the session id is
        # the one field it cannot know at mint, so it is patched in here (and
        # into the incident) without changing the digested claim.
        if authority_token_path is not None:
            try:
                attach_session_to_authority(Path(authority_token_path), session_id)
            except Exception:
                pass
        try:
            home = artifact.get("home")
            if home:
                set_incident_session(Path(home), str(artifact["job_id"]), str(artifact["signature"]), session_id)
        except Exception:
            pass
        if not _process_is_running(process):
            failure_reason = "gateway_exited_before_admission"
            return False
        goal_text = build_goal(artifact, authority_token_path=authority_token_path)
        goal = _result(transport.request("slash.exec", {"session_id": session_id, "command": f"goal {goal_text}"}))
        if not goal or goal.get("type") != "send":
            failure_reason = "goal_setup_failed"
            return False
        if not _process_is_running(process):
            failure_reason = "gateway_exited_before_admission"
            return False
        prompt = build_prompt(artifact, authority_token_path=authority_token_path)
        if not _prompt_was_admitted(transport.request("prompt.submit", {"session_id": session_id, "text": prompt})):
            failure_reason = "prompt_submit_failed"
            return False
        admitted = True
        try:
            mark_investigator_admitted(artifact_path, artifact)
        except Exception:
            # Prompt admission is authoritative for dedupe even when status persistence is unavailable.
            pass
        deadline = clock() + max(0.0, lifecycle_seconds)
        interval = max(0.01, poll_interval_seconds)
        while _process_is_running(process) and clock() < deadline:
            status = _goal_status(transport.request("slash.exec", {"session_id": session_id, "command": "goal status"}))
            if status in {"done", "paused", "blocked"}:
                break
            remaining = deadline - clock()
            if remaining <= 0:
                break
            sleep(min(interval, remaining))
        return True
    except Exception:
        return False
    finally:
        if artifact is not None and not admitted:
            try:
                mark_investigator_failed(artifact_path, artifact, failure_reason)
            except Exception:
                pass
        if process is not None:
            _stop_process(process)


def _heartbeat_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hermes-release-failure-investigator.py heartbeat")
    parser.add_argument("--job", required=True, help="cron job id owning the incident")
    parser.add_argument("--signature", required=True, help="incident signature")
    parser.add_argument("--home", default=None, help="Hermes home (default: HERMES_HOME or the standard path)")
    args = parser.parse_args(argv)
    home = Path(args.home) if args.home else default_home()
    outcome = heartbeat(home=home, job_id=args.job, signature=args.signature)
    print(json.dumps(outcome, ensure_ascii=False, sort_keys=True))
    return 0 if outcome.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "heartbeat":
        return _heartbeat_main(arguments[1:])
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--authority-token", default=None, dest="authority_token",
                        help="path to this finisher's spawner-minted authority record")
    args = parser.parse_args(arguments)
    token = Path(args.authority_token) if args.authority_token else None
    return 0 if run_artifact(Path(args.artifact), authority_token_path=token) else 1


if __name__ == "__main__":
    raise SystemExit(main())
