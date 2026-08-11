"""Fail-closed publication transaction for a verified integration head."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Callable, Sequence

from .audit import (
    DISABLED_HOOKS_PATH,
    GitProbe,
    audit_publication_candidate,
    canonical_repository_identity,
    sanitized_git_environment,
)


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
_CONFIG_VARIABLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")


class PublicationBlocked(RuntimeError):
    def __init__(self, report: dict[str, Any]):
        super().__init__("publication candidate preflight failed")
        self.report = report


class PublicationFailed(RuntimeError):
    def __init__(self, message: str, receipt: dict[str, Any]):
        super().__init__(message)
        self.receipt = receipt


def _redacted_block_report(report: dict[str, Any]) -> dict[str, Any]:
    """Keep publication diagnostics without returning repository URLs/commands."""

    candidate = report.get("release_candidate", {})
    expectations = report.get("expectations", {})
    return {
        "ready": False,
        "publication_state": "not_attempted",
        "release_candidate": {
            "commit": candidate.get("commit", "unknown"),
            "manifest_blob": candidate.get("manifest_blob", "unknown"),
            "clean": candidate.get("clean", "unknown"),
        },
        "expectations": {
            "integration_base": expectations.get("integration_base", "unknown"),
            "integration_head": expectations.get("integration_head", "unknown"),
            "published": expectations.get("published", "unknown"),
        },
        "findings": report.get("findings", []),
    }


def _push_command(
    transport_repository: Path,
    repository_url: str,
    ref: str,
    expected_old_commit: str,
    expected_new_commit: str,
) -> tuple[str, ...]:
    return (
        "git",
        "-c",
        f"safe.directory={transport_repository.as_posix()}",
        "-c",
        f"core.hooksPath={DISABLED_HOOKS_PATH}",
        "-c",
        "commit.gpgSign=false",
        "-c",
        "tag.gpgSign=false",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.useReplaceRefs=false",
        "-C",
        str(transport_repository),
        "push",
        "--porcelain",
        "--atomic",
        "--no-verify",
        "--no-follow-tags",
        f"--force-with-lease={ref}:{expected_old_commit}",
        "--",
        repository_url,
        f"{expected_new_commit}:{ref}",
    )


def _ambient_config_environment() -> dict[str, str]:
    """Permit explicit system/global config reads without inherited Git routing."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )
    return environment


def _parse_config_records(output: str) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
    for record in output.split("\0"):
        if not record:
            continue
        if "\n" in record:
            key, value = record.split("\n", 1)
        elif " " in record:
            key, value = record.split(" ", 1)
        else:
            key, value = record, ""
        records.append((key, value))
    return records


def _sanitize_credential_entries(
    entries: Sequence[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Retain only serializable credential.* values from trusted config scopes."""

    sanitized: list[tuple[str, str]] = []
    for key, value in entries:
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if any(character in key + value for character in ("\0", "\r", "\n")):
            continue
        parts = key.split(".")
        if (
            len(parts) < 2
            or parts[0].casefold() != "credential"
            or not _CONFIG_VARIABLE_RE.fullmatch(parts[-1])
            or any(not part for part in parts[1:-1])
        ):
            continue
        sanitized.append((key, value))
    return sanitized


def _read_ambient_credential_entries(
    *, run: RunCommand = subprocess.run
) -> list[tuple[str, str]]:
    """Read direct system/global credential entries without following includes."""

    entries: list[tuple[str, str]] = []
    for scope in ("--system", "--global"):
        command = (
            "git",
            "config",
            scope,
            "--no-includes",
            "--null",
            "--get-regexp",
            r"^credential\.",
        )
        try:
            completed = run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                env=_ambient_config_environment(),
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("credential configuration discovery failed") from exc
        if completed.returncode == 0:
            entries.extend(_parse_config_records(completed.stdout))
        elif completed.returncode != 1:
            raise RuntimeError("credential configuration discovery failed")
    return _sanitize_credential_entries(entries)


def _escape_config_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _credential_config_text(entries: Sequence[tuple[str, str]]) -> str:
    lines: list[str] = []
    for key, value in _sanitize_credential_entries(entries):
        parts = key.split(".")
        variable = parts[-1]
        if len(parts) == 2:
            lines.append("[credential]")
        else:
            subsection = _escape_config_value(".".join(parts[1:-1]))
            lines.append(f'[credential "{subsection}"]')
        lines.append(f'\t{variable} = "{_escape_config_value(value)}"')
    return "\n".join(lines) + ("\n" if lines else "")


def _secure_write_new(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    os.chmod(path, 0o600)


def _replace_bare_config(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(
                "[core]\n"
                "\trepositoryformatversion = 0\n"
                "\tfilemode = false\n"
                "\tbare = true\n"
            )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    os.chmod(path, 0o600)


def _prepare_transport_repository(source: Path, target: Path) -> None:
    completed = subprocess.run(
        (
            "git",
            "clone",
            "--bare",
            "--no-hardlinks",
            "--",
            str(source),
            str(target),
        ),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        cwd=target.parent,
        env=sanitized_git_environment(),
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError("isolated transport repository creation failed")
    _replace_bare_config(target / "config")


def publish_release_candidate(
    repository: str | Path,
    candidate_commit: str,
    expected_old_commit: str,
    *,
    run: RunCommand = subprocess.run,
    reconcile_run: RunCommand = subprocess.run,
    credential_config_run: RunCommand = subprocess.run,
) -> dict[str, Any]:
    """Atomically publish the candidate's declared head under an exact lease.

    This is the only mutating operation in the fork integration subsystem. It
    first audits the candidate's committed canonical manifest against the live
    expected-old ref, pushes one ref atomically by URL, then requires a fresh
    ``git ls-remote`` observation of the exact new SHA.  A push error or
    timeout is never treated as proof that the remote was unchanged: every
    attempted push is reconciled once, and the push is never retried here.
    """

    repository_path = Path(repository).resolve()
    preflight = audit_publication_candidate(
        repository_path, candidate_commit, expected_old_commit
    )
    if not preflight.get("ready"):
        raise PublicationBlocked(_redacted_block_report(preflight))

    published = preflight.get("identities", {}).get("published", {})
    expectations = preflight.get("expectations", {})
    repository_url = published.get("repository")
    ref = published.get("ref")
    observed_old = published.get("commit")
    expected_new_commit = expectations.get("integration_head")
    if not (
        isinstance(repository_url, str)
        and repository_url
        and isinstance(ref, str)
        and ref.startswith("refs/heads/")
        and GitProbe(repository_path).check_ref_format(ref)
        and isinstance(observed_old, str)
        and observed_old == expected_old_commit
        and _FULL_SHA_RE.fullmatch(expected_old_commit)
        and isinstance(expected_new_commit, str)
        and _FULL_SHA_RE.fullmatch(expected_new_commit)
    ):
        raise PublicationBlocked(_redacted_block_report(preflight))

    repository_identity = canonical_repository_identity(
        repository_url, base=repository_path
    )
    receipt: dict[str, Any] = {
        "publication_state": "unknown",
        "repository_identity": repository_identity,
        "ref": ref,
        "candidate_commit": candidate_commit,
        "expected_old_commit": expected_old_commit,
        "expected_new_commit": expected_new_commit,
        "lease": f"{ref}:{expected_old_commit}",
        "atomic": True,
        "preflight": {
            "ready": True,
            "candidate_commit": preflight.get("release_candidate", {}).get(
                "commit", "unknown"
            ),
            "manifest_blob": preflight.get("release_candidate", {}).get(
                "manifest_blob", "unknown"
            ),
        },
        "push": {"outcome": "unknown", "returncode": None},
        "post_push": {"commit": "unknown", "state": "unknown"},
        "transport": {"isolated": True, "cleanup": "pending"},
    }
    push_attempted = False
    setup_failed = False
    cleanup_failed = False
    temporary = tempfile.TemporaryDirectory(prefix="hermes-fork-publish-")
    try:
        temporary_root = Path(temporary.name)
        os.chmod(temporary_root, 0o700)
        transport_repository = temporary_root / "transport.git"
        credential_config = temporary_root / "credential.gitconfig"
        credentials = _read_ambient_credential_entries(
            run=credential_config_run
        )
        _secure_write_new(
            credential_config, _credential_config_text(credentials)
        )
        _prepare_transport_repository(repository_path, transport_repository)
        command = _push_command(
            transport_repository,
            repository_url,
            ref,
            expected_old_commit,
            expected_new_commit,
        )
        push_environment = sanitized_git_environment()
        push_environment["GIT_CONFIG_GLOBAL"] = str(credential_config)
        push_attempted = True
        try:
            completed = run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                env=push_environment,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            receipt["push"] = {"outcome": "timeout", "returncode": None}
        except subprocess.SubprocessError:
            receipt["push"] = {
                "outcome": "subprocess_error",
                "returncode": None,
            }
        except OSError:
            receipt["push"] = {"outcome": "start_error", "returncode": None}
        else:
            receipt["push"] = {
                "outcome": (
                    "exit_zero" if completed.returncode == 0 else "exit_nonzero"
                ),
                "returncode": completed.returncode,
            }
    except (OSError, RuntimeError, subprocess.SubprocessError):
        setup_failed = not push_attempted
    finally:
        try:
            temporary.cleanup()
            receipt["transport"]["cleanup"] = "complete"
        except OSError:
            cleanup_failed = True
            receipt["transport"]["cleanup"] = "failed"

    if setup_failed:
        receipt["publication_state"] = "not_attempted"
        raise PublicationFailed(
            "isolated publication transport setup failed", receipt
        )

    post_push = GitProbe(repository_path, run=reconcile_run).live_ref(
        repository_url, ref
    )
    observed_commit = post_push.get("commit")
    receipt["post_push"] = {
        "ref": ref,
        "commit": observed_commit if isinstance(observed_commit, str) else "unknown",
        "state": post_push.get("state", "unknown"),
    }
    if observed_commit == expected_new_commit:
        receipt["publication_state"] = "published"
        if cleanup_failed:
            raise PublicationFailed(
                "publication was verified but isolated transport cleanup failed",
                receipt,
            )
        return receipt
    if observed_commit == expected_old_commit:
        if receipt["push"]["outcome"] == "timeout":
            receipt["publication_state"] = "unknown"
            raise PublicationFailed(
                "publication outcome remains unknown after push timeout",
                receipt,
            )
        receipt["publication_state"] = "not_published"
        raise PublicationFailed(
            "publication was reconciled at the expected-old commit", receipt
        )
    if isinstance(observed_commit, str) and _FULL_SHA_RE.fullmatch(observed_commit):
        receipt["publication_state"] = "conflict"
        raise PublicationFailed(
            "publication conflict: live ref moved to a third commit", receipt
        )
    receipt["publication_state"] = "unknown"
    raise PublicationFailed(
        "publication outcome is unknown because live ref reconciliation failed",
        receipt,
    )
