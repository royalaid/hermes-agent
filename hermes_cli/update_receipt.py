"""Structured update receipts + post-update fleet version verification.

Phase 1 of the fleet-update reliability plan (#91277): the updater must
*prove* its outcome instead of assuming it.

Two additive capabilities, both designed so a failure inside them can never
break an update (every public entry point is exception-swallowing):

1. **Update receipt** — a machine-readable JSON record of what one
   ``hermes update`` run discovered, did, skipped (and why), written to
   ``<HERMES_HOME>/logs/update_receipts/``. Silent-failure classes this
   makes visible: #88848 (helper died after "success" printed), #74973
   (restart silently skipped), #85753 (restart phase never ran), #81193
   (desktop shows failure for a successful update).

2. **Fleet version verification** — after the restart phase, read every
   profile's ``gateway_state.json``, compare each live gateway's stamped
   ``code_sha`` (written by ``gateway/status.py`` on every runtime-status
   write) against the freshly-updated checkout's HEAD, and print a fleet
   version matrix. Mixed-version fleets (#88654, #69754, #77553, #56717)
   become a loud, actionable report instead of a latent state.

Deployment-kind awareness (docker/image-managed installs) rides on
``hermes_cli.build_info.get_code_identity()``: an image build reports
``source="build-file"`` and the receipt records that the install is not
in-place updatable.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import logging

logger = logging.getLogger(__name__)

_RECEIPT_DIR_NAME = "update_receipts"
_RECEIPT_KEEP = 20  # keep the last N receipts per profile home

# Module-level current receipt. ``hermes update`` is a single-threaded CLI
# command; a module singleton lets the 7k-line updater record steps from
# any depth without threading a handle through every helper.
_current: Optional["UpdateReceipt"] = None


# Handoff-receipt (``.hermes-update-receipt.json``) validation vocabulary.
# Distinct from the fleet receipts above: a handoff receipt is the terminal
# mutation proof consumed by the desktop/bootstrap updater, so its identity
# fields are matched at exact widths (full 40-hex git SHAs, 64-hex archive
# digests) rather than the loose short-SHA form accepted elsewhere.
_UPDATE_RECEIPT_NAME = ".hermes-update-receipt.json"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]{16,128}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_ARCHIVE_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UpdateReceipt:
    """Collects the observable facts of one ``hermes update`` run."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {
            "schema": 1,
            "started_at": _utc_now_iso(),
            "finished_at": None,
            "pid": os.getpid(),
            "outcome": "running",  # running | success | partial | failed
            "gateway_resume_deferred": "--defer-gateway-resume" in sys.argv[1:],
            "pre_update": {},
            "post_update": {},
            "steps": [],
            "skips": [],
            "gateway_restart": {},
            "fleet": [],
        }
        try:
            from hermes_cli.build_info import get_code_identity

            self.data["pre_update"] = get_code_identity()
        except Exception:
            pass

    # -- recording ---------------------------------------------------------
    def step(self, name: str, ok: bool, detail: str = "") -> None:
        self.data["steps"].append(
            {"name": name, "ok": bool(ok), "detail": detail, "at": _utc_now_iso()}
        )

    def skip(self, name: str, reason: str) -> None:
        self.data["skips"].append(
            {"name": name, "reason": reason, "at": _utc_now_iso()}
        )

    def gateway_restart_result(
        self,
        *,
        restarted_services: list | None = None,
        relaunched_profiles: list | None = None,
        externally_supervised_profiles: list | None = None,
        killed_pids: list | None = None,
        failed_units: list | None = None,
        incomplete: bool = False,
        phase_error: str = "",
    ) -> None:
        self.data["gateway_restart"] = {
            "restarted_services": list(restarted_services or []),
            "relaunched_profiles": list(relaunched_profiles or []),
            "externally_supervised_profiles": list(
                externally_supervised_profiles or []
            ),
            "killed_pids": [int(p) for p in (killed_pids or [])],
            "failed_units": [str(u) for u in (failed_units or [])],
            "incomplete": bool(incomplete),
            "phase_error": phase_error,
        }

    def finalize(self, outcome: str) -> None:
        # A deferred staging child proves source mutation, not terminal
        # activation. The native parent still owns gateway relaunch and lease
        # cleanup, so child-local exit 0 cannot publish terminal success.
        if outcome == "success" and self.data["gateway_resume_deferred"]:
            outcome = "partial"
        self.data["outcome"] = outcome
        self.data["finished_at"] = _utc_now_iso()
        try:
            from hermes_cli.build_info import get_code_identity

            self.data["post_update"] = get_code_identity(refresh=True)
        except Exception:
            pass


def _receipt_dir() -> Path:
    from hermes_cli.config import get_hermes_home

    return get_hermes_home() / "logs" / _RECEIPT_DIR_NAME


def begin_update_receipt() -> None:
    """Start recording a new update receipt. Never raises."""
    global _current
    try:
        _current = UpdateReceipt()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not start update receipt: %s", exc)
        _current = None


def record_step(name: str, ok: bool, detail: str = "") -> None:
    """Record one update step outcome. No-op when no receipt is active."""
    try:
        if _current is not None:
            _current.step(name, ok, detail)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not record update step %s: %s", name, exc)


def record_skip(name: str, reason: str) -> None:
    """Record a skipped step WITH the reason it was skipped."""
    try:
        if _current is not None:
            _current.skip(name, reason)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not record update skip %s: %s", name, exc)


def record_gateway_restart(**kwargs: Any) -> None:
    """Record the gateway restart phase outcome (see UpdateReceipt)."""
    try:
        if _current is not None:
            _current.gateway_restart_result(**kwargs)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not record gateway restart result: %s", exc)


def finalize_update_receipt(
    outcome: str, fleet: list | None = None, stop_reason: str = ""
) -> Optional[Path]:
    """Finalize + persist the receipt. Returns the written path or None.

    ``outcome`` is one of ``success`` / ``partial`` / ``failed`` /
    ``refused``. Exactly-once by construction: the module singleton is
    popped first, so a second call (e.g. the command-boundary safety net
    after an inner path already finalized) is a no-op returning None.
    """
    global _current
    receipt = _current
    _current = None
    if receipt is None:
        return None
    try:
        receipt.finalize(outcome)
        if stop_reason:
            from hermes_cli.update_diagnostics import sanitize_receipt_reason

            receipt.data["stop_reason"] = sanitize_receipt_reason(stop_reason)
        if fleet is not None:
            receipt.data["fleet"] = fleet
        directory = _receipt_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = directory / f"update_{stamp}_{os.getpid()}.json"
        path.write_text(
            json.dumps(receipt.data, indent=2, default=str), encoding="utf-8"
        )
        # Stable pointer for the dashboard/desktop: latest receipt.
        latest = directory / "latest.json"
        try:
            latest.write_text(
                json.dumps(receipt.data, indent=2, default=str), encoding="utf-8"
            )
        except OSError:
            pass
        _prune_old_receipts(directory)
        return path
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not write update receipt: %s", exc)
        return None


def finalize_pending_update_receipt(
    exit_code: Optional[int] = None, stop_reason: str = ""
) -> Optional[Path]:
    """Command-boundary safety net: persist a still-open receipt, if any.

    ``hermes update`` has many early-termination paths (Windows
    concurrent-instance preflight, venv-holder refusal, head-pinned no-op,
    fetch failure — all ``sys.exit``) that predate the inner finalize
    call sites. Any receipt still open when the update COMMAND unwinds is
    finalized here so every post-begin run leaves a record — the
    refused/failed runs are exactly the ones a receipt matters most for
    (review on #91283). No-op when no receipt is open (the inner paths
    already finalized — exactly-once via the popped singleton) or when
    recording was never started. Never raises.

    Outcome mapping: exit 0/None → ``success`` (a path that completed
    without an explicit inner finalize), exit 2 → ``refused`` (the
    updater's preflight-refusal convention), anything else → ``failed``.
    """
    if _current is None:
        return None
    if exit_code in (0, None):
        outcome = "success"
    elif exit_code == 2:
        outcome = "refused"
    else:
        outcome = "failed"
    try:
        receipt = _current
        if receipt is not None and exit_code is not None:
            receipt.data["exit_code"] = int(exit_code)
    except Exception:
        pass
    return finalize_update_receipt(outcome, stop_reason=stop_reason)


def _sanitize_update_receipt(value: object, root: Path) -> dict | None:
    """Validate one handoff receipt, returning its canonical form or None.

    Fails closed: every field must be present, exactly typed, and bound to
    ``root``.  The git and archive modes are mutually exclusive in their
    identity fields, so a receipt can never describe both.
    """
    if not isinstance(value, dict):
        return None
    expected_receipt_keys = {
        "schema_version",
        "invocation_id",
        "lease_id",
        "mode",
        "root",
        "remote",
        "branch",
        "target_ref",
        "target_sha",
        "resulting_head",
        "archive_sha",
        "timestamp",
        "success",
        "gateway_resume_deferred",
        "health",
    }
    if set(value) != expected_receipt_keys:
        return None
    try:
        timestamp = int(value["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    if value.get("schema_version") != 1 or value.get("success") is not True:
        return None
    if type(value.get("gateway_resume_deferred")) is not bool:
        return None
    if value.get("mode") not in {"git", "archive"} or timestamp <= 0:
        return None
    if os.path.normcase(os.path.realpath(str(value.get("root", "")))) != os.path.normcase(
        os.path.realpath(root)
    ):
        return None
    invocation_id = value.get("invocation_id")
    lease_id = value.get("lease_id")
    if not isinstance(invocation_id, str) or _IDENTIFIER_RE.fullmatch(invocation_id) is None:
        return None
    if not isinstance(lease_id, str) or _IDENTIFIER_RE.fullmatch(lease_id) is None:
        return None
    branch = value.get("branch")
    if not isinstance(branch, str) or not branch:
        return None
    remote = value.get("remote")
    target_ref = value.get("target_ref")
    if remote is not None and not isinstance(remote, str):
        return None
    if target_ref is not None and not isinstance(target_ref, str):
        return None
    shas: dict[str, str | None] = {}
    for field, pattern in (
        ("target_sha", _SHA_RE),
        ("resulting_head", _SHA_RE),
        ("archive_sha", _ARCHIVE_SHA_RE),
    ):
        candidate = value.get(field)
        if candidate is not None and (
            not isinstance(candidate, str) or pattern.fullmatch(candidate) is None
        ):
            return None
        shas[field] = candidate.lower() if candidate else None
    if value["mode"] == "git":
        if (
            not remote
            or not target_ref
            or shas["target_sha"] is None
            or shas["resulting_head"] is None
            or shas["target_sha"] != shas["resulting_head"]
            or shas["archive_sha"] is not None
        ):
            return None
    elif (
        remote is not None
        or target_ref is not None
        or shas["target_sha"] is not None
        or shas["resulting_head"] is not None
        or shas["archive_sha"] is None
    ):
        return None
    health = value.get("health")
    expected_health = {
        "critical_syntax",
        "critical_imports",
        "dependencies",
        "node_dependencies",
    }
    if not isinstance(health, dict) or set(health) != expected_health:
        return None
    if any(type(health[field]) is not bool for field in expected_health) or not all(
        health[field] for field in expected_health
    ):
        return None
    return {
        "schema_version": 1,
        "invocation_id": invocation_id,
        "lease_id": lease_id,
        "mode": value["mode"],
        "root": os.path.normcase(os.path.realpath(root)),
        "remote": remote,
        "branch": branch,
        "target_ref": target_ref,
        **shas,
        "timestamp": timestamp,
        "success": True,
        "gateway_resume_deferred": bool(value["gateway_resume_deferred"]),
        "health": {field: bool(health[field]) for field in sorted(expected_health)},
    }


def _prune_old_receipts(directory: Path) -> None:
    try:
        receipts = sorted(
            (p for p in directory.glob("update_*.json") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in receipts[_RECEIPT_KEEP:]:
            try:
                stale.unlink()
            except OSError:
                pass
    except Exception:
        pass


def read_latest_receipt() -> Optional[dict[str, Any]]:
    """Read the most recent update receipt, or None. Never raises."""
    try:
        path = _receipt_dir() / "latest.json"
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Fleet version verification
# ---------------------------------------------------------------------------

def collect_fleet_versions() -> list[dict[str, Any]]:
    """Snapshot every profile's gateway code identity vs. the current tree.

    Returns one entry per profile home that has a ``gateway_state.json``
    with a live PID::

        {"profile": str, "pid": int, "code_sha": str|None,
         "code_version": str|None, "state": "current"|"stale"|"unknown"}

    ``stale``   — gateway stamped a code_sha that differs from the updated
                  checkout's HEAD (it is still serving pre-update modules).
    ``unknown`` — gateway predates the code-identity stamp (started before
                  this feature landed) or identity could not be resolved.
    Never raises; a probe failure yields an empty list.
    """
    results: list[dict[str, Any]] = []
    try:
        from hermes_cli.build_info import get_code_identity

        expected_sha = (get_code_identity(refresh=True) or {}).get("sha")
    except Exception:
        expected_sha = None

    try:
        from gateway.status import _pid_exists, read_runtime_status
        from hermes_cli.profiles import (
            _get_default_hermes_home,
            _get_profiles_root,
            _PROFILE_ID_RE,
        )

        homes: list[tuple[str, Path]] = []
        default_home = _get_default_hermes_home()
        if default_home.is_dir():
            homes.append(("default", default_home))
        profiles_root = _get_profiles_root()
        if profiles_root.is_dir():
            for entry in sorted(profiles_root.iterdir()):
                if entry.is_dir() and entry.name != "default" and _PROFILE_ID_RE.match(entry.name):
                    homes.append((entry.name, entry))

        for profile, home in homes:
            # Prefer the gateway-owned control socket (#92091): a live
            # `identify` answer is authoritative — no PID-reuse or stale-file
            # heuristics. Fall back to gateway_state.json for gateways that
            # predate the socket or whose socket didn't bind.
            identity = None
            try:
                from gateway.control_socket import identify_gateway

                identity = identify_gateway(home)
            except Exception:
                identity = None
            if identity:
                try:
                    pid = int(identity.get("pid"))
                except (TypeError, ValueError):
                    pid = None
                if pid is not None:
                    code_sha = identity.get("code_sha")
                    if not code_sha or not expected_sha:
                        state = "unknown"
                    elif str(code_sha) == str(expected_sha):
                        state = "current"
                    else:
                        state = "stale"
                    results.append(
                        {
                            "profile": profile,
                            "pid": pid,
                            "code_sha": str(code_sha) if code_sha else None,
                            "code_version": identity.get("code_version"),
                            "state": state,
                            "source": "socket",
                        }
                    )
                    continue
            status_path = home / "gateway_state.json"
            record = read_runtime_status(status_path)
            if not record:
                continue
            pid = record.get("pid")
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                continue
            if not _pid_exists(pid):
                continue
            code_sha = record.get("code_sha")
            if not code_sha or not expected_sha:
                state = "unknown"
            elif str(code_sha) == str(expected_sha):
                state = "current"
            else:
                state = "stale"
            results.append(
                {
                    "profile": profile,
                    "pid": pid,
                    "code_sha": str(code_sha) if code_sha else None,
                    "code_version": record.get("code_version"),
                    "state": state,
                }
            )
    except Exception as exc:
        logger.debug("Fleet version probe failed: %s", exc)
    return results


def print_fleet_version_matrix(fleet: list[dict[str, Any]]) -> bool:
    """Print the post-update fleet version matrix.

    Returns True when at least one gateway is provably stale (still
    serving pre-update code), so the caller can escalate. ``unknown``
    entries are reported but do NOT fail the update: gateways started
    before the code-identity stamp existed have no sha to compare, and
    failing on them would turn this feature's own rollout into a
    false-positive storm.
    """
    if not fleet:
        return False
    any_stale = False
    print()
    print("Fleet version check:")
    for entry in fleet:
        sha = entry.get("code_sha")
        short = sha[:8] if isinstance(sha, str) and sha else "?"
        state = entry.get("state")
        profile = entry.get("profile")
        pid = entry.get("pid")
        if state == "current":
            print(f"  ✓ {profile} (pid {pid}) @ {short} — up to date")
        elif state == "stale":
            any_stale = True
            print(f"  ✗ {profile} (pid {pid}) @ {short} — STALE (pre-update code)")
        else:
            print(
                f"  ? {profile} (pid {pid}) — version unknown "
                "(gateway predates version stamping; restart to enable)"
            )
    if any_stale:
        print()
        print("  ⚠ Stale gateways keep serving pre-update code until restarted:")
        print("      hermes gateway restart                # active profile")
        print("      hermes -p <profile> gateway restart   # named profile")
    return any_stale
