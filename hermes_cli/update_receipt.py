"""Validated success receipts for the Hermes updater."""

import json
import os
import re
import time as _time
from pathlib import Path


_UPDATE_RECEIPT_NAME = ".hermes-update-receipt.json"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]{16,128}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _receipt_path(root: Path) -> Path:
    del root
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / _UPDATE_RECEIPT_NAME


def _sanitize_update_receipt(value: object, root: Path) -> dict | None:
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
    for field in ("target_sha", "resulting_head", "archive_sha"):
        candidate = value.get(field)
        if candidate is not None and (
            not isinstance(candidate, str) or _SHA_RE.fullmatch(candidate) is None
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
        or len(shas["archive_sha"]) != 64
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


def _load_update_receipt(root: Path) -> dict | None:
    try:
        value = json.loads(_receipt_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return _sanitize_update_receipt(value, root)


def _write_update_receipt(
    root: Path,
    *,
    invocation_id: str,
    lease_id: str,
    mode: str,
    branch: str,
    remote: str | None,
    target_ref: str | None,
    target_sha: str | None,
    resulting_head: str | None,
    archive_sha: str | None,
    gateway_resume_deferred: bool,
    health: dict[str, bool],
) -> dict:
    value = {
        "schema_version": 1,
        "invocation_id": invocation_id,
        "lease_id": lease_id,
        "mode": mode,
        "root": os.path.normcase(os.path.realpath(root)),
        "remote": remote,
        "branch": branch,
        "target_ref": target_ref,
        "target_sha": target_sha,
        "resulting_head": resulting_head,
        "archive_sha": archive_sha,
        "timestamp": int(_time.time()),
        "success": True,
        "gateway_resume_deferred": bool(gateway_resume_deferred),
        "health": health,
    }
    sanitized = _sanitize_update_receipt(value, root)
    if sanitized is None:
        raise ValueError("refusing to write an invalid update receipt")
    path = _receipt_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(sanitized, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)
    return sanitized
