"""Validated atomic-success receipts for the Hermes updater.

The observable update history remains in :mod:`hermes_cli.update_receipt`.
This module owns the atomic handoff receipt consumed by update lifecycle code.
"""

import json
import os
import re
import time as _time
from pathlib import Path

from hermes_cli.update_receipt import _sanitize_update_receipt


_UPDATE_RECEIPT_NAME = ".hermes-update-receipt.json"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]{16,128}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _receipt_path(root: Path) -> Path:
    del root
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / _UPDATE_RECEIPT_NAME


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
