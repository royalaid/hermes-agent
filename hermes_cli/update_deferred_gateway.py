"""Authenticated post-mutation gateway fleet resume plans."""

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time as _time
from pathlib import Path
from typing import NoReturn

from hermes_mcp_update_gate import _publish_exclusive_atomic

from hermes_cli.update_quiesce import (
    _claim_update_quiesce_lease,
    _release_update_quiesce_lease,
    _transfer_update_quiesce_lease,
)
from hermes_cli.update_receipt import _IDENTIFIER_RE, _load_update_receipt
from hermes_cli.update_transaction import _UpdateTransaction


_DEFERRED_GATEWAY_PLAN_PREFIX = ".hermes-gateway-resume-"
# Native wrappers bound source/dependency mutation at 60 minutes and the
# outside-Job fleet-resume phase at 5 minutes. Keep one additional minute for
# descendant-drain/clock scheduling while still bounding abandoned recovery.
_DEFERRED_GATEWAY_PLAN_TTL_SECONDS = 66 * 60
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _deferred_gateway_plan_path(
    root: Path, invocation_id: str, *, completed: bool = False
) -> Path:
    """Return the install-global private fleet-plan path."""
    del root
    from hermes_constants import get_default_hermes_root

    suffix = ".completed" if completed else ".json"
    return get_default_hermes_root() / (
        f"{_DEFERRED_GATEWAY_PLAN_PREFIX}{invocation_id}{suffix}"
    )


def _gateway_plan_auth(payload: dict, lease_id: str) -> str:
    authenticated = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hmac.new(lease_id.encode("utf-8"), authenticated, hashlib.sha256).hexdigest()


def _sanitize_deferred_gateway_plan(
    value: object,
    *,
    root: Path,
    invocation_id: str,
    lease_id: str,
    now: float | None = None,
) -> dict | None:
    """Validate a private no-argv gateway fleet plan and its capability MAC."""
    if not isinstance(value, dict):
        return None
    expected = {
        "schema_version",
        "invocation_id",
        "lease_fingerprint",
        "install_root",
        "created_at",
        "expires_at",
        "profiles",
        "cold_start_if_installed",
        "auth",
    }
    if (
        set(value) != expected
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
    ):
        return None
    if value.get("invocation_id") != invocation_id:
        return None
    if not _IDENTIFIER_RE.fullmatch(invocation_id):
        return None
    if not _IDENTIFIER_RE.fullmatch(lease_id):
        return None
    expected_fingerprint = hashlib.sha256(lease_id.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(
        str(value.get("lease_fingerprint", "")), expected_fingerprint
    ):
        return None
    if os.path.normcase(os.path.realpath(str(value.get("install_root", "")))) != os.path.normcase(
        os.path.realpath(root)
    ):
        return None
    if type(value.get("created_at")) is not int or type(value.get("expires_at")) is not int:
        return None
    created_at = value["created_at"]
    expires_at = value["expires_at"]
    current = _time.time() if now is None else float(now)
    if not math.isfinite(current) or not (
        created_at > 0
        and created_at <= expires_at
        and expires_at - created_at <= _DEFERRED_GATEWAY_PLAN_TTL_SECONDS
        and created_at <= current + 5
        and current <= expires_at
    ):
        return None
    if type(value.get("cold_start_if_installed")) is not bool:
        return None
    raw_profiles = value.get("profiles")
    if not isinstance(raw_profiles, list):
        return None
    profiles: list[dict] = []
    seen: set[str] = set()
    for entry in raw_profiles:
        if not isinstance(entry, dict) or set(entry) != {
            "name",
            "old_pid",
            "created_at",
        }:
            return None
        name = entry.get("name")
        if (
            not isinstance(name, str)
            or _PROFILE_NAME_RE.fullmatch(name) is None
            or name in {".", ".."}
            or name in seen
        ):
            return None
        if type(entry.get("old_pid")) is not int or isinstance(
            entry.get("created_at"), bool
        ) or not isinstance(entry.get("created_at"), (int, float)):
            return None
        old_pid = entry["old_pid"]
        process_created_at = float(entry["created_at"])
        if (
            isinstance(entry.get("old_pid"), bool)
            or old_pid <= 0
            or not math.isfinite(process_created_at)
            or process_created_at <= 0
        ):
            return None
        seen.add(name)
        profiles.append(
            {"name": name, "old_pid": old_pid, "created_at": process_created_at}
        )
    unsigned = {key: value[key] for key in expected if key != "auth"}
    auth = value.get("auth")
    if not isinstance(auth, str) or not hmac.compare_digest(
        auth, _gateway_plan_auth(unsigned, lease_id)
    ):
        return None
    return {
        **unsigned,
        "profiles": profiles,
        "auth": auth,
    }


def _write_deferred_gateway_plan(
    root: Path,
    *,
    transaction: _UpdateTransaction,
) -> Path:
    invocation_id = transaction.invocation_id
    lease = transaction.lease
    token = transaction.gateway_resume_plan or {}
    if not isinstance(invocation_id, str) or not isinstance(lease, dict):
        raise RuntimeError("deferred gateway plan lacks update correlation")
    lease_id = lease.get("lease_id")
    if not isinstance(lease_id, str):
        raise RuntimeError("deferred gateway plan lacks lease correlation")
    existing_path = transaction.deferred_gateway_plan_path
    if isinstance(existing_path, Path):
        try:
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("deferred gateway plan became unreadable") from exc
        if _sanitize_deferred_gateway_plan(
            existing,
            root=root,
            invocation_id=invocation_id,
            lease_id=lease_id,
        ) is None:
            raise RuntimeError("deferred gateway plan correlation changed")
        return existing_path
    if token.get("unmapped") or token.get("unmapped_pids"):
        raise RuntimeError("unmapped gateways cannot be deferred safely")
    identities = token.get("profile_identities") or {}
    profiles = []
    for name, old_pid in sorted((token.get("profiles") or {}).items()):
        identity = identities.get(name)
        if not isinstance(identity, dict):
            raise RuntimeError(f"gateway profile {name!r} has no process identity")
        if (
            type(old_pid) is not int
            or type(identity.get("pid")) is not int
            or identity.get("pid") != old_pid
        ):
            raise RuntimeError(
                f"gateway profile {name!r} process identity does not match its PID"
            )
        profiles.append(
            {
                "name": str(name),
                "old_pid": int(old_pid),
                "created_at": float(identity["created_at"]),
            }
        )
    created_at = int(_time.time())
    unsigned = {
        "schema_version": 1,
        "invocation_id": invocation_id,
        "lease_fingerprint": hashlib.sha256(lease_id.encode("utf-8")).hexdigest(),
        "install_root": os.path.normcase(os.path.realpath(root)),
        "created_at": created_at,
        "expires_at": created_at + _DEFERRED_GATEWAY_PLAN_TTL_SECONDS,
        "profiles": profiles,
        "cold_start_if_installed": bool(token.get("cold_start_if_installed")),
    }
    payload = {**unsigned, "auth": _gateway_plan_auth(unsigned, lease_id)}
    sanitized = _sanitize_deferred_gateway_plan(
        payload,
        root=root,
        invocation_id=invocation_id,
        lease_id=lease_id,
        now=created_at,
    )
    if sanitized is None:
        raise RuntimeError("refusing invalid deferred gateway plan")
    path = _deferred_gateway_plan_path(root, invocation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _publish_exclusive_atomic(
        path,
        json.dumps(sanitized, sort_keys=True, separators=(",", ":")),
        temporary_prefix=".hermes-gateway-plan-",
        short_write_message="short write while publishing gateway resume plan",
    )
    transaction.deferred_gateway_plan_path = path
    return path


def _load_deferred_gateway_plan(
    path: Path,
    *,
    root: Path,
    invocation_id: str,
    lease_id: str,
) -> tuple[str, dict] | None:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except FileNotFoundError:
        # A crash can occur after consume moves the pending name but before it
        # publishes the completed record. Recover only authenticated,
        # byte-identical consume tombstones; malformed or divergent evidence
        # remains fail-closed for manual recovery.
        candidates = sorted(path.parent.glob(f"{path.name}.consume-*"))
        if not candidates:
            return None
        recovered_raw: str | None = None
        recovered_value: dict | None = None
        for candidate in candidates:
            try:
                candidate_raw = candidate.read_text(encoding="utf-8")
                candidate_value = json.loads(candidate_raw)
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "deferred gateway plan recovery is unreadable"
                ) from exc
            candidate_plan = _sanitize_deferred_gateway_plan(
                candidate_value,
                root=root,
                invocation_id=invocation_id,
                lease_id=lease_id,
            )
            if candidate_plan is None:
                raise RuntimeError("deferred gateway plan recovery is invalid")
            if recovered_raw is not None and candidate_raw != recovered_raw:
                raise RuntimeError("deferred gateway plan recoveries diverged")
            recovered_raw = candidate_raw
            recovered_value = candidate_plan
        assert recovered_raw is not None and recovered_value is not None
        try:
            os.link(candidates[0], path)
        except FileExistsError:
            try:
                if path.read_text(encoding="utf-8") != recovered_raw:
                    raise RuntimeError("deferred gateway plan changed during recovery")
            except OSError as exc:
                raise RuntimeError(
                    "deferred gateway plan recovery could not be proven"
                ) from exc
        except OSError as exc:
            raise RuntimeError("deferred gateway plan could not be restored") from exc
        return recovered_raw, recovered_value
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("deferred gateway plan is unreadable") from exc
    sanitized = _sanitize_deferred_gateway_plan(
        value,
        root=root,
        invocation_id=invocation_id,
        lease_id=lease_id,
    )
    if sanitized is None:
        raise RuntimeError("deferred gateway plan is invalid or expired")
    return raw, sanitized


def _consume_deferred_gateway_plan(path: Path, expected_raw: str) -> bool:
    """Consume exact pending bytes into an idempotent completed record."""
    completed = path.with_suffix(".completed")
    tombstone = path.with_name(
        f"{path.name}.consume-{os.getpid()}-{secrets.token_hex(8)}"
    )
    try:
        os.replace(path, tombstone)
    except OSError:
        return False
    try:
        moved_raw = tombstone.read_text(encoding="utf-8")
    except OSError:
        # The rename already consumed the only discoverable pending name.
        # Restore the exact inode without overwriting anything so a transient
        # read/sharing failure cannot strand a stopped fleet with no retry
        # path. Retain the tombstone as fail-closed evidence if restoration
        # itself cannot be proven.
        try:
            os.link(tombstone, path)
        except OSError:
            pass
        return False
    if moved_raw != expected_raw:
        try:
            os.link(tombstone, path)
        except OSError:
            pass
        return False
    try:
        os.link(tombstone, completed)
    except (FileExistsError, OSError):
        try:
            os.link(tombstone, path)
        except OSError:
            pass
        return False
    try:
        tombstone.unlink()
    except OSError:
        # A completed record is terminal authority only after the pending
        # tombstone is retired. Roll it back and restore the exact pending
        # bytes so replay cannot skip lease cleanup after a partial consume.
        try:
            completed.unlink()
        except OSError:
            pass
        try:
            os.link(tombstone, path)
        except OSError:
            pass
        return False
    return True


def _validate_deferred_update_request(args) -> None:
    invocation_id = getattr(args, "invocation_id", None)
    lease_id = getattr(args, "bridge_lease_id", None)
    if invocation_id is not None and (
        not isinstance(invocation_id, str)
        or _IDENTIFIER_RE.fullmatch(invocation_id) is None
    ):
        raise ValueError("invalid --invocation-id")
    if lease_id is not None and (
        not isinstance(lease_id, str)
        or _IDENTIFIER_RE.fullmatch(lease_id) is None
    ):
        raise ValueError("invalid --bridge-lease-id")
    if not bool(getattr(args, "defer_gateway_resume", False)):
        return
    incompatible = [
        flag
        for flag in ("check", "preflight", "drain", "resume_deferred_gateway")
        if bool(getattr(args, flag, False))
    ]
    if incompatible:
        raise ValueError(
            "--defer-gateway-resume cannot be combined with --"
            + incompatible[0].replace("_", "-")
        )
    if not bool(getattr(args, "gateway", False)):
        raise ValueError("--defer-gateway-resume requires --gateway")
    if invocation_id is None:
        raise ValueError("--defer-gateway-resume requires a valid --invocation-id")
    if lease_id is None:
        raise ValueError("--defer-gateway-resume requires a valid --bridge-lease-id")


def _profile_process_still_matches(old_pid: int, created_at: float) -> bool:
    """Fail closed when the pre-update process identity cannot be disproved."""
    try:
        import psutil  # type: ignore
    except ImportError as exc:
        raise RuntimeError("psutil is required to verify gateway identity") from exc
    try:
        process = psutil.Process(int(old_pid))
        live_created = float(process.create_time())
    except psutil.NoSuchProcess:
        return False
    except Exception as exc:
        raise RuntimeError("could not revalidate prior gateway process identity") from exc
    if not math.isfinite(live_created):
        raise RuntimeError("prior gateway process creation time is invalid")
    return abs(live_created - float(created_at)) <= 0.001


def _running_gateway_profiles() -> dict[str, int]:
    from hermes_cli.gateway import find_profile_gateway_processes

    return {
        str(process.profile): int(process.pid)
        for process in find_profile_gateway_processes()
    }


def _spawn_deferred_gateway_profile(profile: str) -> int:
    """Start one derived Hermes profile without accepting caller argv."""
    from hermes_constants import get_default_hermes_root
    from hermes_cli import gateway_windows

    default_root = get_default_hermes_root()
    profile_home = default_root if profile == "default" else default_root / "profiles" / profile
    if profile != "default" and not profile_home.is_dir():
        raise RuntimeError(f"gateway profile {profile!r} no longer exists")
    previous = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(profile_home)
    try:
        return int(gateway_windows._spawn_detached())
    finally:
        if previous is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous


def _wait_for_deferred_gateway_profile(profile: str, *, timeout: float = 20.0) -> bool:
    deadline = _time.monotonic() + max(0.1, float(timeout))
    while _time.monotonic() < deadline:
        try:
            if profile in _running_gateway_profiles():
                return True
        except Exception:
            pass
        _time.sleep(0.2)
    return False


def _resume_deferred_gateway_fleet(plan: dict) -> None:
    """Resume only the authenticated structured fleet, idempotently."""
    running = _running_gateway_profiles()
    for entry in plan["profiles"]:
        profile = str(entry["name"])
        running_pid = running.get(profile)
        if running_pid is not None:
            if int(running_pid) == int(entry["old_pid"]) and _profile_process_still_matches(
                int(entry["old_pid"]), float(entry["created_at"])
            ):
                raise RuntimeError(
                    f"prior gateway profile {profile!r} is still running"
                )
            # A different verified profile PID (or a recycled numeric PID
            # whose creation identity does not match) is a prior successful
            # partial-resume result. Do not start a duplicate.
            continue
        if _profile_process_still_matches(
            int(entry["old_pid"]), float(entry["created_at"])
        ):
            raise RuntimeError(
                f"prior gateway profile {profile!r} is still running"
            )
        if _spawn_deferred_gateway_profile(profile) <= 0:
            raise RuntimeError(f"gateway profile {profile!r} did not start")
        if not _wait_for_deferred_gateway_profile(profile):
            raise RuntimeError(f"gateway profile {profile!r} did not become ready")
        running = _running_gateway_profiles()

    if plan["cold_start_if_installed"] and not plan["profiles"]:
        if "default" not in running:
            from hermes_cli import gateway_windows

            if gateway_windows.is_installed():
                if _spawn_deferred_gateway_profile("default") <= 0:
                    raise RuntimeError("default gateway did not start")
                if not _wait_for_deferred_gateway_profile("default"):
                    raise RuntimeError("default gateway did not become ready")


def _cmd_update_resume_deferred_gateway(args, *, root: Path) -> NoReturn:
    """Consume one authenticated deferred fleet plan outside mutation Jobs."""
    invocation_id = getattr(args, "invocation_id", None)
    lease_id = getattr(args, "bridge_lease_id", None)
    requested_root = getattr(args, "resume_root", None)
    if (
        not isinstance(invocation_id, str)
        or _IDENTIFIER_RE.fullmatch(invocation_id) is None
        or not isinstance(lease_id, str)
        or _IDENTIFIER_RE.fullmatch(lease_id) is None
        or not isinstance(requested_root, str)
        or os.path.normcase(os.path.realpath(requested_root))
        != os.path.normcase(os.path.realpath(root))
    ):
        print("✗ Invalid deferred gateway resume request.")
        raise SystemExit(1)

    pending_path = _deferred_gateway_plan_path(root, invocation_id)
    completed_path = _deferred_gateway_plan_path(root, invocation_id, completed=True)
    completed = _load_deferred_gateway_plan(
        completed_path,
        root=root,
        invocation_id=invocation_id,
        lease_id=lease_id,
    )

    from hermes_cli.update_lock import UpdateLock
    from hermes_mcp_update_gate import marker_path, read_quiesce_lease

    if completed is not None and read_quiesce_lease(marker_path()) is None:
        print("✓ Deferred gateway fleet was already resumed.")
        raise SystemExit(0)

    update_lock = UpdateLock()
    lease: dict | None = None
    prior_owner_pid: int | None = None
    success = False
    try:
        if not update_lock.acquire() or not update_lock.prove_claim():
            raise RuntimeError("update handoff lock is not owned by this transaction")
        prior = read_quiesce_lease(marker_path())
        if not (
            isinstance(prior, dict)
            and prior.get("schema_version") == 1
            and prior.get("lease_id") == lease_id
        ):
            raise RuntimeError("deferred gateway lease is missing or changed")
        prior_owner_pid = int(prior.get("owner_pid", 0))
        lease = _claim_update_quiesce_lease(root, expected_lease_id=lease_id)
        # The native parent must prove that its exact spawned child held the
        # capability even when a fast no-op resume adopts and clears the lease
        # between marker polls.  Emit no capability bytes: this frame is only
        # an identity-bound observation, and terminal success still requires
        # the correlated receipt/plan plus exact lease cleanup.
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "event": "deferred-gateway-lease-adopted",
                    "invocation_id": invocation_id,
                    "owner_pid": os.getpid(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
        receipt = _load_update_receipt(root)
        if receipt is not None and receipt.get("invocation_id") == invocation_id:
            if not (
                receipt.get("lease_id") == lease_id
                and receipt.get("gateway_resume_deferred") is True
            ):
                raise RuntimeError("deferred gateway receipt correlation failed")
        if completed is None:
            loaded = _load_deferred_gateway_plan(
                pending_path,
                root=root,
                invocation_id=invocation_id,
                lease_id=lease_id,
            )
            if loaded is None:
                raise RuntimeError("deferred gateway plan is missing")
            raw, plan = loaded
            _resume_deferred_gateway_fleet(plan)
            if not _consume_deferred_gateway_plan(pending_path, raw):
                raise RuntimeError("deferred gateway plan changed before consume")
        success = True
    except Exception as exc:
        print(f"✗ Deferred gateway resume failed: {exc}")
    finally:
        try:
            if lease is not None:
                if success:
                    try:
                        released = _release_update_quiesce_lease(root, lease)
                    except Exception:
                        released = False
                    if not released:
                        success = False
                        print("✗ Deferred gateway lease cleanup could not be proven.")
                if not success and prior_owner_pid is not None and prior_owner_pid > 0:
                    try:
                        _transfer_update_quiesce_lease(
                            root, lease, new_owner_pid=prior_owner_pid
                        )
                    except Exception:
                        # Retain the child-owned/foreign marker as fail-closed
                        # evidence. The native parent will reject terminal
                        # success and can run its bounded recovery flow.
                        pass
        finally:
            update_lock.release()
    if success:
        print(
            "✓ Deferred gateway fleet was already resumed."
            if completed is not None
            else "✓ Deferred gateway fleet resumed."
        )
    raise SystemExit(0 if success else 1)
