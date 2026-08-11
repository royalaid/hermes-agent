"""Deterministic isolated reconstruction from a schema-v2 manifest."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any, Sequence

from .audit import (
    DISABLED_HOOKS_PATH,
    GitProbe,
    RepositoryBinding,
    component_cohesion_findings,
    sanitized_git_environment,
)
from .manifest import Finding, findings_as_dicts, validate_manifest


@dataclass(frozen=True)
class ReplayPatch:
    component: str
    subject: str
    source_commit: str
    source_patch_id: str
    expected_patch_id: str
    expected_commit: str | None
    committer_environment: dict[str, str]


@dataclass(frozen=True)
class PublicationTarget:
    repository_identity: str
    ref: str
    expected_old_commit: str
    expected_new_commit: str


class PreparationBlocked(RuntimeError):
    def __init__(self, findings: list[Finding], *, dry_run: bool = False):
        super().__init__("fork integration preparation blocked by preflight")
        self.findings = findings
        self.dry_run = dry_run

    def to_dict(self) -> dict[str, Any]:
        return {
            "prepared": False,
            "dry_run": self.dry_run,
            "writes": [],
            "findings": findings_as_dicts(self.findings),
        }


class PreparationFailed(RuntimeError):
    def __init__(self, message: str, receipt: dict[str, Any]):
        super().__init__(message)
        self.receipt = receipt


def _add(
    findings: list[Finding],
    code: str,
    message: str,
    *,
    component: str | None = None,
    patch: int | None = None,
) -> None:
    finding = Finding(code, "error", message, component, patch)
    if finding not in findings:
        findings.append(finding)


def _preflight(
    manifest: dict[str, Any], repository: Path, probe: GitProbe, *, dry_run: bool
) -> tuple[
    str,
    str,
    list[ReplayPatch],
    list[dict[str, Any]],
    PublicationTarget,
]:
    findings = validate_manifest(manifest)
    if manifest.get("manifest_state") != "ready":
        _add(findings, "manifest_not_ready", "prepare requires manifest_state=ready")
    integration = manifest.get("integration") if isinstance(manifest.get("integration"), dict) else {}
    base = integration.get("expected_base_commit")
    resolved_base = probe.resolve_commit(base) if isinstance(base, str) else None
    if resolved_base != base:
        _add(findings, "immutable_upstream_missing", "expected_base_commit is not an available immutable commit")
    expected_head = integration.get("expected_head_commit")
    resolved_head = (
        probe.resolve_commit(expected_head) if isinstance(expected_head, str) else None
    )
    if resolved_head != expected_head:
        _add(
            findings,
            "immutable_integration_head_missing",
            "expected_head_commit is not an available immutable commit",
        )
    if resolved_base is not None and resolved_head is not None:
        if probe.is_ancestor(resolved_base, resolved_head) is not True:
            _add(
                findings,
                "integration_base_not_ancestor",
                "expected_base_commit is not an ancestor of expected_head_commit",
            )
    integration_ref = integration.get("ref")
    if (
        not isinstance(integration_ref, str)
        or not integration_ref.startswith("refs/heads/")
        or not probe.check_ref_format(integration_ref)
    ):
        _add(findings, "invalid_prepare_branch", "integration.ref must be a refs/heads/... branch")
        branch = ""
    else:
        branch = integration_ref.removeprefix("refs/heads/")
        if not probe.check_branch_format(branch):
            _add(
                findings,
                "invalid_prepare_branch",
                "integration branch fails git check-ref-format --branch",
            )

    repositories = (
        manifest.get("repositories")
        if isinstance(manifest.get("repositories"), dict)
        else {}
    )

    def repository_url(repository_name: object) -> str | None:
        declared = (
            repositories.get(repository_name, {})
            if isinstance(repository_name, str)
            else {}
        )
        url = declared.get("url") if isinstance(declared, dict) else None
        return url if isinstance(url, str) and url.strip() else None

    repository_binding = RepositoryBinding(repository, probe.remote_urls())

    integration_repository_name = integration.get("repository")
    integration_repository_url = repository_url(integration_repository_name)
    integration_repository_resolution = repository_binding.resolve(
        integration_repository_url
    )
    integration_repository_bound = integration_repository_resolution.is_bound
    integration_repository_identity = integration_repository_resolution.identity
    integration_transport_url = integration_repository_resolution.transport_url
    if not integration_repository_bound:
        _add(
            findings,
            "prepare_integration_repository_unbound",
            "declared integration repository is not bound to a configured local remote",
        )
    local_integration_tip = (
        probe.resolve_commit(integration_ref)
        if isinstance(integration_ref, str) and probe.check_ref_format(integration_ref)
        else None
    )
    if isinstance(expected_head, str) and local_integration_tip != expected_head:
        _add(
            findings,
            "prepare_integration_ref_mismatch",
            "local integration ref does not equal expected_head_commit",
        )
    published_old_commit: str | None = None
    if (
        manifest.get("manifest_state") == "ready"
        and integration_repository_bound
        and isinstance(integration_ref, str)
    ):
        published = probe.live_ref(integration_transport_url, integration_ref)
        observed = published.get("commit")
        if not isinstance(observed, str) or observed == "unknown":
            _add(
                findings,
                "prepare_published_ref_unknown",
                "live integration ref could not supply an expected-old publication SHA",
            )
        else:
            published_old_commit = observed

    upstream_repository_url = repository_url(integration.get("upstream_repository"))
    upstream_repository_resolution = repository_binding.resolve(
        upstream_repository_url
    )
    upstream_repository_bound = upstream_repository_resolution.is_bound
    upstream_transport_url = upstream_repository_resolution.transport_url
    if not upstream_repository_bound:
        _add(
            findings,
            "prepare_upstream_repository_unbound",
            "declared upstream repository is not bound to a configured local remote",
        )
    if manifest.get("manifest_state") == "ready" and upstream_repository_bound:
        live_upstream = probe.live_ref(
            upstream_transport_url,
            integration.get("upstream_ref"),
        )
        if live_upstream.get("commit") != base:
            _add(
                findings,
                "prepare_upstream_base_mismatch",
                "live upstream ref does not equal expected_base_commit",
            )

    patch_by_id: dict[str, tuple[dict[str, Any], int, dict[str, Any]]] = {}
    skipped: list[dict[str, Any]] = []
    live_source_cache: dict[tuple[str, str], dict[str, Any]] = {}
    components = manifest.get("components") if isinstance(manifest.get("components"), list) else []
    for component in components:
        if not isinstance(component, dict):
            continue
        component_id = component.get("id") if isinstance(component.get("id"), str) else "unknown"
        component_patch_values = (
            component.get("patches")
            if isinstance(component.get("patches"), list)
            else []
        )
        has_required_patch = any(
            isinstance(item, dict) and item.get("disposition") == "required"
            for item in component_patch_values
        )
        source = component.get("source") if isinstance(component.get("source"), dict) else {}
        source_ref = source.get("ref")
        source_ref_valid = isinstance(source_ref, str) and probe.check_ref_format(source_ref)
        source_tip = probe.resolve_commit(source_ref) if source_ref_valid else None
        source_url = repository_url(source.get("repository"))
        source_repository_resolution = repository_binding.resolve(source_url)
        source_repository_bound = source_repository_resolution.is_bound
        source_repository_identity = source_repository_resolution.identity
        source_transport_url = source_repository_resolution.transport_url
        if not source_ref_valid:
            _add(
                findings,
                "invalid_prepare_source_ref",
                "component source ref fails git check-ref-format",
                component=component_id,
            )
        if not source_repository_bound:
            _add(
                findings,
                "prepare_source_repository_unbound",
                "component source repository is not bound to a configured local remote",
                component=component_id,
            )
        if (
            source_ref == integration_ref
            and source_repository_identity is not None
            and source_repository_identity == integration_repository_identity
        ):
            _add(
                findings,
                "source_target_same_ref",
                "component source and integration target are the same canonical repository ref",
                component=component_id,
            )
        if (
            source_tip is not None
            and local_integration_tip is not None
            and source_tip == local_integration_tip
        ):
            _add(
                findings,
                "circular_source_ref",
                "component source ref resolves to the integration branch tip",
                component=component_id,
            )
        if (
            has_required_patch
            and source_tip is not None
            and resolved_head is not None
            and probe.is_ancestor(source_tip, resolved_head) is True
        ):
            _add(
                findings,
                "source_ref_integration_lineage",
                "required component source ref is reachable from the integration lineage",
                component=component_id,
            )
        if (
            manifest.get("manifest_state") == "ready"
            and source_ref_valid
            and source_repository_bound
        ):
            if source_transport_url is not None:
                cache_key = (source_transport_url, source_ref)
                if cache_key not in live_source_cache:
                    live_source_cache[cache_key] = probe.live_ref(
                        source_transport_url, source_ref
                    )
                live_source = live_source_cache[cache_key]
                if live_source.get("commit") == "unknown":
                    _add(
                        findings,
                        "prepare_source_ref_live_unknown",
                        "component source ref could not be proven live",
                        component=component_id,
                    )
                elif source_tip is not None and live_source.get("commit") != source_tip:
                    _add(
                        findings,
                        "prepare_source_ref_live_mismatch",
                        "live component source ref does not equal its local ref",
                        component=component_id,
                    )
        component_patches = component_patch_values
        for patch_index, patch in enumerate(component_patches):
            if not isinstance(patch, dict):
                continue
            source_identity = patch.get("source") if isinstance(patch.get("source"), dict) else {}
            disposition = patch.get("disposition")
            source_commit = source_identity.get("commit")
            source_patch_id = source_identity.get("stable_patch_id")
            if isinstance(source_patch_id, str):
                patch_by_id[source_patch_id] = (component, patch_index, patch)
            if not isinstance(source_commit, str) or probe.resolve_commit(source_commit) != source_commit:
                _add(
                    findings,
                    "prepare_source_commit_unavailable",
                    "source commit is not available by its full immutable identity",
                    component=component_id,
                    patch=patch_index,
                )
                continue
            computed_patch_id = probe.stable_patch_id(source_commit)
            if computed_patch_id != source_patch_id:
                _add(
                    findings,
                    "prepare_source_patch_mismatch",
                    f"computed stable patch ID is {computed_patch_id or 'unknown'}",
                    component=component_id,
                    patch=patch_index,
                )
            if source_tip is None:
                _add(
                    findings,
                    "prepare_source_ref_unavailable",
                    "source ref is unavailable locally",
                    component=component_id,
                    patch=patch_index,
                )
            elif probe.is_ancestor(source_commit, source_tip) is not True:
                _add(
                    findings,
                    "prepare_source_not_reachable",
                    "source commit is not reachable from source.ref",
                    component=component_id,
                    patch=patch_index,
                )
            if disposition == "absorbed_upstream" and isinstance(base, str):
                present = probe.patch_present(base, source_commit)
                if present is not True:
                    _add(
                        findings,
                        "absorbed_patch_missing",
                        "absorbed patch stable identity is not represented in expected_base_commit",
                        component=component_id,
                        patch=patch_index,
                    )
                else:
                    skipped.append(
                        {
                            "component": component_id,
                            "source_commit": source_commit,
                            "stable_patch_id": source_patch_id,
                            "reason": "absorbed_stable_patch_id_present",
                        }
                    )
            elif isinstance(disposition, str) and disposition in {
                "superseded",
                "folded",
            }:
                skipped.append(
                    {
                        "component": component_id,
                        "source_commit": source_commit,
                        "stable_patch_id": source_patch_id,
                        "reason": disposition,
                    }
                )
        findings.extend(component_cohesion_findings(component, probe))

    replay: list[ReplayPatch] = []
    required_patch_ids = manifest.get("required_patch_ids")
    if not isinstance(required_patch_ids, list):
        required_patch_ids = []
    for required_patch_id in required_patch_ids:
        if not isinstance(required_patch_id, str):
            continue
        owner = patch_by_id.get(required_patch_id)
        if owner is None:
            _add(
                findings,
                "required_patch_missing",
                f"required patch has no component patch: {required_patch_id}",
            )
            continue
        component, patch_index, patch = owner
        component_id = component.get("id")
        if (
            patch.get("disposition") != "required"
            or component.get("upstream_status") != "required"
        ):
            _add(
                findings,
                "required_patch_not_replayable",
                "required patch ledger entry does not name a required patch disposition",
                component=component_id,
                patch=patch_index,
            )
            continue
        source_identity = (
            patch.get("source") if isinstance(patch.get("source"), dict) else {}
        )
        final_identity = (
            patch.get("integration")
            if isinstance(patch.get("integration"), dict)
            else {}
        )
        source_commit = source_identity.get("commit")
        expected_patch_id = final_identity.get("stable_patch_id")
        expected_commit = final_identity.get("commit")
        if final_identity.get("state") != "expected" or not isinstance(expected_patch_id, str):
            _add(
                findings,
                "required_patch_final_identity_missing",
                "required replay patch needs an expected final stable patch identity",
                component=component_id,
                patch=patch_index,
            )
            continue
        if source_commit == expected_commit and isinstance(source_commit, str):
            _add(
                findings,
                "source_final_commit_same",
                "required source and integration commits must be distinct objects",
                component=component_id,
                patch=patch_index,
            )
        if (
            resolved_head is not None
            and isinstance(source_commit, str)
            and probe.is_ancestor(source_commit, resolved_head) is True
        ):
            _add(
                findings,
                "source_commit_integration_lineage",
                "required source commit is reachable from the integration lineage",
                component=component_id,
                patch=patch_index,
            )
        if source_identity.get("stable_patch_id") != expected_patch_id:
            _add(
                findings,
                "prepare_required_patch_identity_mismatch",
                "required source patch does not equal its declared final patch identity",
                component=component_id,
                patch=patch_index,
            )
            continue
        if (
            not isinstance(expected_commit, str)
            or probe.resolve_commit(expected_commit) != expected_commit
        ):
            _add(
                findings,
                "required_patch_final_commit_unavailable",
                "required replay patch needs an available immutable final commit",
                component=component_id,
                patch=patch_index,
            )
            continue
        if probe.stable_patch_id(expected_commit) != expected_patch_id:
            _add(
                findings,
                "required_patch_final_identity_mismatch",
                "expected final commit does not match its stable patch identity",
                component=component_id,
                patch=patch_index,
            )
        if resolved_head is not None and probe.is_ancestor(expected_commit, resolved_head) is not True:
            _add(
                findings,
                "required_patch_final_not_reachable",
                "expected final commit is not reachable from expected_head_commit",
                component=component_id,
                patch=patch_index,
            )
        if not isinstance(source_commit, str):
            continue
        committer_environment = probe.committer_environment(expected_commit)
        if committer_environment is None:
            _add(
                findings,
                "integration_committer_unavailable",
                "cannot read deterministic committer identity from expected final commit",
                component=component_id,
                patch=patch_index,
            )
            continue
        replay.append(
            ReplayPatch(
                component=component_id,
                subject=patch.get("subject", ""),
                source_commit=source_commit,
                source_patch_id=source_identity["stable_patch_id"],
                expected_patch_id=expected_patch_id,
                expected_commit=expected_commit,
                committer_environment=committer_environment,
            )
        )

    for patch in replay:
        source_metadata = probe.replay_metadata(patch.source_commit)
        final_metadata = (
            probe.replay_metadata(patch.expected_commit)
            if patch.expected_commit is not None
            else None
        )
        if source_metadata is None or final_metadata is None:
            _add(
                findings,
                "prepare_replay_metadata_unavailable",
                "cannot inspect cherry-pick-preserved source and integration metadata",
                component=patch.component,
            )
        elif source_metadata != final_metadata:
            _add(
                findings,
                "prepare_replay_metadata_mismatch",
                "expected integration commit does not preserve source author and message metadata",
                component=patch.component,
            )

    if resolved_base is not None and resolved_head is not None:
        expected_history = [
            patch.expected_commit
            for patch in replay
            if patch.expected_commit is not None
        ]
        actual_history = probe.linear_commits_between(resolved_base, resolved_head)
        if actual_history is None or actual_history != expected_history:
            _add(
                findings,
                "prepare_integration_history_mismatch",
                "base-to-head history must be a linear chain of exactly the ordered required integration commits",
            )

    errors = [finding for finding in findings if finding.severity == "error"]
    if errors:
        raise PreparationBlocked(findings, dry_run=dry_run)
    assert isinstance(integration_repository_url, str)
    assert isinstance(integration_repository_identity, str)
    assert isinstance(integration_ref, str)
    assert isinstance(expected_head, str)
    assert isinstance(published_old_commit, str)
    return (
        base,
        branch,
        replay,
        skipped,
        PublicationTarget(
            repository_identity=integration_repository_identity,
            ref=integration_ref,
            expected_old_commit=published_old_commit,
            expected_new_commit=expected_head,
        ),
    )


def _run_mutation(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> str:
    command = (
        "git",
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
        *args,
    )
    env = sanitized_git_environment(environment)
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        env=env,
        timeout=120,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise RuntimeError(detail)
    return completed.stdout.strip()


def prepare_worktree(
    manifest: dict[str, Any],
    repository: str | Path,
    target: str | Path,
    *,
    dry_run: bool = False,
    probe: GitProbe | None = None,
) -> dict[str, Any]:
    """Reconstruct the ordered patch stack in a new standalone clone.

    All identity and completeness gates execute before the target is created.
    The caller checkout is never checked out, reset, or assigned a new ref.
    """

    source_repository = Path(repository).resolve()
    target_repository = Path(target).resolve()
    if target_repository.exists():
        raise PreparationBlocked(
            [Finding("prepare_target_exists", "error", "prepare target already exists")],
            dry_run=dry_run,
        )
    try:
        target_repository.relative_to(source_repository)
    except ValueError:
        pass
    else:
        raise PreparationBlocked(
            [
                Finding(
                    "prepare_target_inside_source",
                    "error",
                    "prepare target must be outside the caller checkout",
                )
            ],
            dry_run=dry_run,
        )

    git = probe or GitProbe(source_repository)
    base, branch, replay, skipped, publication = _preflight(
        manifest, source_repository, git, dry_run=dry_run
    )
    receipt: dict[str, Any] = {
        "schema_version": manifest.get("schema_version"),
        "prepared": False,
        "dry_run": dry_run,
        "source_repository": str(source_repository),
        "target_repository": str(target_repository),
        "integration_branch": f"refs/heads/{branch}",
        "upstream_commit": base,
        "prepared_head": "unknown",
        "applied": [],
        "skipped": skipped,
        "publication": {
            "repository_identity": publication.repository_identity,
            "ref": publication.ref,
            "expected_old_commit": publication.expected_old_commit,
            "expected_new_commit": publication.expected_new_commit,
        },
        "push_performed": False,
        "caller_checkout_touched": False,
        "writes": [] if dry_run else [str(target_repository)],
    }
    if dry_run:
        receipt["would_apply"] = [
            {
                "component": patch.component,
                "source_commit": patch.source_commit,
                "source_patch_id": patch.source_patch_id,
                "expected_patch_id": patch.expected_patch_id,
            }
            for patch in replay
        ]
        return receipt

    try:
        _run_mutation(
            (
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                "--",
                str(source_repository),
                str(target_repository),
            ),
        )
        _run_mutation(
            ("checkout", "--detach", base),
            cwd=target_repository,
        )
        _run_mutation(
            ("branch", "--force", "--", branch, base),
            cwd=target_repository,
        )
        _run_mutation(("checkout", branch), cwd=target_repository)
        target_probe = GitProbe(target_repository)
        for patch in replay:
            _run_mutation(
                ("cherry-pick", patch.source_commit),
                cwd=target_repository,
                environment=patch.committer_environment,
            )
            prepared_commit = target_probe.resolve_commit("HEAD")
            prepared_patch_id = (
                target_probe.stable_patch_id(prepared_commit) if prepared_commit else None
            )
            applied = {
                "component": patch.component,
                "subject": patch.subject,
                "source_commit": patch.source_commit,
                "source_patch_id": patch.source_patch_id,
                "prepared_commit": prepared_commit or "unknown",
                "prepared_patch_id": prepared_patch_id or "unknown",
            }
            receipt["applied"].append(applied)
            if prepared_patch_id != patch.expected_patch_id:
                raise PreparationFailed(
                    "prepared patch identity does not match manifest expectation", receipt
                )
            if patch.expected_commit is not None and prepared_commit != patch.expected_commit:
                raise PreparationFailed(
                    "prepared commit identity does not match manifest expectation", receipt
                )
        prepared_head = target_probe.resolve_commit("HEAD")
        receipt["prepared_head"] = prepared_head or "unknown"
        expected_head = manifest["integration"].get("expected_head_commit")
        if expected_head is not None and prepared_head != expected_head:
            raise PreparationFailed(
                "prepared head does not match integration.expected_head_commit", receipt
            )
        receipt["prepared"] = True
        return receipt
    except PreparationFailed:
        raise
    except subprocess.TimeoutExpired as exc:
        raise PreparationFailed("git mutation timed out", receipt) from exc
    except (OSError, RuntimeError) as exc:
        raise PreparationFailed(str(exc), receipt) from exc
