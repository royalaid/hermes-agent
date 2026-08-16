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
    """Atomically record a sanitized failure and decide whether it is newly open."""
    normalized = normalize_error(error)
    signature = signature_for(job_id, stage, normalized)
    state_path = home / STATE_SUBDIR / f"{job_id}.json"
    artifact_path = home / ARTIFACT_SUBDIR / f"{signature}.json"
    with _state_lock(state_path.with_suffix(".lock")):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            state = {"schema": 1, "job_id": job_id, "open": {}}
        open_incidents = state.setdefault("open", {})
        failed_incidents = state.setdefault("failed", {})
        previous = open_incidents.get(signature)
        occurrence = int(previous.get("occurrences", 0)) + 1 if isinstance(previous, dict) else 1
        spawn = not isinstance(previous, dict)
        prior_status = previous.get("status") if isinstance(previous, dict) else None
        status = "admitted" if prior_status == "admitted" else "pending"
        open_incidents[signature] = {"occurrences": occurrence, "stage": stage, "status": status}
        failed_incidents.pop(signature, None)
        _atomic_json(state_path, state)
        artifact = {
            "schema": 1, "job_id": job_id, "stage": stage, "failure_class": stage,
            "signature": signature, "occurrences": occurrence, "normalized_error": normalized,
            "artifact_path": str(artifact_path), "state_path": str(state_path),
            "investigator_status": status,
            "worktree": str(worktree),
            "paths": {"release_script": str(script_path), "log": str(log_path), "tests": str(test_path), "manifest": str(manifest_path)},
            "investigator": dict(investigator or {}),
        }
        _atomic_json(artifact_path, artifact)
    return {"spawn": spawn, "signature": signature, "occurrences": occurrence, "artifact_path": str(artifact_path)}


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
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            state = {"schema": 1, "job_id": artifact.get("job_id", ""), "open": {}, "failed": {}}
        open_incidents = state.setdefault("open", {})
        failed_incidents = state.setdefault("failed", {})
        if status == "investigator_failed":
            open_incidents.pop(signature, None)
            failed_incidents[signature] = {"status": status, "reason": failure or "admission_failed"}
        else:
            incident = open_incidents.get(signature)
            if isinstance(incident, dict):
                incident["status"] = status
            _atomic_json(state_path, state)
        if status == "investigator_failed":
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
    """Only a completed real release resolves prior incidents for this job."""
    state_path = home / STATE_SUBDIR / f"{job_id}.json"
    if not state_path.is_file():
        return
    with _state_lock(state_path.with_suffix(".lock")):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        if state.get("open"):
            state["open"] = {}
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


def maybe_launch_investigator(result: dict[str, Any]) -> None:
    if result.get("spawn"):
        spawn_detached([python_executable(), str(Path(__file__)), "--artifact", str(result["artifact_path"])])


def build_goal(artifact: dict[str, Any]) -> str:
    return (
        f"Investigate and locally fix Hermes integration release incident {artifact['signature']}. "
        "Work only in the scheduler worktree. Preserve the published branch and last good release. "
        "Keep this goal active until the failure is reproduced, covered by TDD, and a local fix is verified. "
        "Stop before push, release publication, installer execution, cron mutation, credential change, gateway restart, "
        "or any unrelated edit, and report the blocked action."
    )


def build_prompt(artifact: dict[str, Any]) -> str:
    paths = artifact["paths"]
    return (
        f"Investigate incident {artifact['signature']} at stage {artifact['stage']}.\n"
        f"Read the sanitized incident artifact: {artifact.get('artifact_path', '<this artifact>')}\n"
        f"Scheduler worktree: {artifact['worktree']}\nRelease script: {paths['release_script']}\n"
        f"Log: {paths['log']}\nTests: {paths['tests']}\nManifest: {paths['manifest']}\n"
        f"Failure: {artifact['normalized_error']}\n"
        "Reproduce the failure, add or adjust a TDD regression test, and make the smallest local fix. "
        "Do not push, do not create a release or installer, do not change cron scheduling, do not restart the gateway, do not change credentials, and do not make unrelated edits."
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


def run_artifact(artifact_path: Path, *, transport_factory: Callable[[], tuple[Any, Any]] = _default_transport,
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
        cfg = artifact.get("investigator") if isinstance(artifact.get("investigator"), dict) else {}
        create = {
            "source": "desktop", "cwd": str(artifact["worktree"]), "title": f"Release failure {artifact['signature'][:12]}",
            "model": str(cfg.get("model", "")), "provider": str(cfg.get("provider", "")),
            "reasoning_effort": str(cfg.get("reasoning_effort", "")), "close_on_disconnect": False,
            "cron_session": str(artifact["job_id"]),
        }
        created = _result(transport.request("session.create", create))
        if not created or not isinstance(created.get("session_id"), str) or not created["session_id"] or not isinstance(created.get("stored_session_id"), str) or not created["stored_session_id"]:
            failure_reason = "session_create_failed"
            return False
        session_id = created["session_id"]
        if not _process_is_running(process):
            failure_reason = "gateway_exited_before_admission"
            return False
        goal = _result(transport.request("slash.exec", {"session_id": session_id, "command": f"goal {build_goal(artifact)}"}))
        if not goal or goal.get("type") != "send":
            failure_reason = "goal_setup_failed"
            return False
        if not _process_is_running(process):
            failure_reason = "gateway_exited_before_admission"
            return False
        prompt = build_prompt(artifact)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True)
    args = parser.parse_args()
    return 0 if run_artifact(Path(args.artifact)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
