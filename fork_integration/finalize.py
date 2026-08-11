"""Read-only finalization of review-required replacement components."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any, Sequence

from .audit import GitProbe
from .manifest import Finding, PATCH_ROLES, validate_manifest


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPLACEMENT_FIELDS = frozenset(
    {"source_commit", "integration_commit", "role", "related_to"}
)


class ReplacementFinalizationBlocked(RuntimeError):
    def __init__(self, findings: list[Finding]):
        super().__init__("replacement component finalization blocked")
        self.findings = findings


def _add(
    findings: list[Finding],
    code: str,
    message: str,
    *,
    component: str | None = None,
    patch: int | None = None,
) -> None:
    findings.append(Finding(code, "error", message, component, patch))


def finalize_component_replacement(
    manifest: dict[str, Any],
    repository: str | Path,
    *,
    component_id: str,
    source_ref: str,
    replacements: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Return a finalized draft derived only from real local commit objects.

    The caller must first create a non-integration provenance branch containing
    every historical source commit plus the coherent replacement commits, and
    set ``integration.expected_head_commit`` to the final integration head.
    Historical ``review_required`` patches become ``folded``; existing
    ``superseded`` patches remain non-replayed. Only verified replacement patch
    IDs enter ``required_patch_ids``. The input object and repository are never
    modified.
    """

    draft = deepcopy(manifest)
    findings: list[Finding] = []
    components = draft.get("components")
    if not isinstance(components, list):
        raise ReplacementFinalizationBlocked(validate_manifest(draft))
    component = next(
        (
            item
            for item in components
            if isinstance(item, dict) and item.get("id") == component_id
        ),
        None,
    )
    if component is None:
        _add(findings, "replacement_component_missing", "component id was not found")
        raise ReplacementFinalizationBlocked(findings)

    integration = draft.get("integration")
    integration_ref = (
        integration.get("ref") if isinstance(integration, dict) else None
    )
    expected_head = (
        integration.get("expected_head_commit")
        if isinstance(integration, dict)
        else None
    )
    probe = GitProbe(repository)
    if (
        not isinstance(source_ref, str)
        or not source_ref.startswith("refs/heads/")
        or not probe.check_ref_format(source_ref)
    ):
        _add(
            findings,
            "replacement_source_ref_invalid",
            "replacement source_ref must be a Git-valid refs/heads/... branch",
            component=component_id,
        )
        source_tip = None
    else:
        source_tip = probe.resolve_commit(source_ref)
        if source_tip is None:
            _add(
                findings,
                "replacement_source_ref_unavailable",
                "replacement source_ref is not available locally",
                component=component_id,
            )
    if source_ref == integration_ref or (
        source_tip is not None
        and isinstance(expected_head, str)
        and probe.is_ancestor(source_tip, expected_head) is True
    ):
        _add(
            findings,
            "replacement_source_is_integration",
            "replacement provenance source ref must not be reachable from the integration lineage",
            component=component_id,
        )
    if (
        not isinstance(expected_head, str)
        or not _FULL_SHA_RE.fullmatch(expected_head)
        or probe.resolve_commit(expected_head) != expected_head
    ):
        _add(
            findings,
            "replacement_integration_head_invalid",
            "integration.expected_head_commit must name the real final local head",
            component=component_id,
        )

    if not isinstance(replacements, Sequence) or isinstance(
        replacements, (str, bytes)
    ) or not replacements:
        _add(
            findings,
            "replacement_identity_missing",
            "at least one exact replacement source/final commit pair is required",
            component=component_id,
        )
        raise ReplacementFinalizationBlocked(findings)

    replacement_patches: list[dict[str, Any]] = []
    primary_replacement: str | None = None
    for index, replacement in enumerate(replacements):
        if not isinstance(replacement, dict):
            _add(
                findings,
                "replacement_identity_invalid",
                "replacement identity must be an object",
                component=component_id,
                patch=index,
            )
            continue
        unexpected = replacement.keys() - _REPLACEMENT_FIELDS
        if unexpected:
            _add(
                findings,
                "replacement_identity_invalid",
                "replacement identity contains undeclared fields",
                component=component_id,
                patch=index,
            )
        source_commit = replacement.get("source_commit")
        final_commit = replacement.get("integration_commit")
        role = replacement.get("role", "implementation")
        related_to = replacement.get("related_to")
        if not (
            isinstance(source_commit, str)
            and _FULL_SHA_RE.fullmatch(source_commit)
            and probe.resolve_commit(source_commit) == source_commit
            and isinstance(final_commit, str)
            and _FULL_SHA_RE.fullmatch(final_commit)
            and probe.resolve_commit(final_commit) == final_commit
        ):
            _add(
                findings,
                "replacement_identity_invalid",
                "replacement source and integration commits must be exact available SHAs",
                component=component_id,
                patch=index,
            )
            continue
        if source_commit == final_commit:
            _add(
                findings,
                "replacement_source_final_same",
                "replacement source and final commits must be distinct objects",
                component=component_id,
                patch=index,
            )
            continue
        if (
            isinstance(expected_head, str)
            and probe.is_ancestor(source_commit, expected_head) is True
        ):
            _add(
                findings,
                "replacement_source_integration_lineage",
                "replacement source commit must not be reachable from the integration lineage",
                component=component_id,
                patch=index,
            )
            continue
        if not isinstance(role, str) or role not in PATCH_ROLES:
            _add(
                findings,
                "replacement_role_invalid",
                "replacement role is not a supported patch role",
                component=component_id,
                patch=index,
            )
            continue
        if source_tip is None or probe.is_ancestor(source_commit, source_tip) is not True:
            _add(
                findings,
                "replacement_source_not_reachable",
                "replacement source commit is not reachable from source_ref",
                component=component_id,
                patch=index,
            )
        if (
            not isinstance(expected_head, str)
            or probe.is_ancestor(final_commit, expected_head) is not True
        ):
            _add(
                findings,
                "replacement_final_not_reachable",
                "replacement integration commit is not reachable from expected head",
                component=component_id,
                patch=index,
            )
        source_patch_id = probe.stable_patch_id(source_commit)
        final_patch_id = probe.stable_patch_id(final_commit)
        source_subject = probe.subject(source_commit)
        final_subject = probe.subject(final_commit)
        if (
            source_patch_id is None
            or final_patch_id is None
            or source_patch_id != final_patch_id
        ):
            _add(
                findings,
                "replacement_patch_identity_mismatch",
                "replacement source and integration commits are not patch-equivalent",
                component=component_id,
                patch=index,
            )
            continue
        if source_subject is None or source_subject != final_subject:
            _add(
                findings,
                "replacement_subject_mismatch",
                "replacement integration commit must preserve the source subject",
                component=component_id,
                patch=index,
            )
            continue
        if related_to is None and primary_replacement is not None:
            related_to = primary_replacement
        if related_to is not None and (
            not isinstance(related_to, str)
            or not _FULL_SHA_RE.fullmatch(related_to)
        ):
            _add(
                findings,
                "replacement_relationship_invalid",
                "replacement related_to must be an exact source commit SHA",
                component=component_id,
                patch=index,
            )
            continue
        patch: dict[str, Any] = {
            "subject": source_subject,
            "role": role,
            "disposition": "required",
            "source": {
                "commit": source_commit,
                "stable_patch_id": source_patch_id,
            },
            "integration": {
                "state": "expected",
                "commit": final_commit,
                "stable_patch_id": final_patch_id,
            },
        }
        if related_to is not None:
            patch["related_to"] = related_to
        replacement_patches.append(patch)
        if primary_replacement is None:
            primary_replacement = source_commit

    if findings:
        raise ReplacementFinalizationBlocked(findings)
    assert primary_replacement is not None

    historical_patches = component.get("patches")
    if not isinstance(historical_patches, list):
        _add(
            findings,
            "replacement_history_invalid",
            "component patches must be an array",
            component=component_id,
        )
        raise ReplacementFinalizationBlocked(findings)
    for index, patch in enumerate(historical_patches):
        if not isinstance(patch, dict):
            _add(
                findings,
                "replacement_history_invalid",
                "historical patch must be an object",
                component=component_id,
                patch=index,
            )
            continue
        source = patch.get("source") if isinstance(patch.get("source"), dict) else {}
        source_commit = source.get("commit")
        if (
            not isinstance(source_commit, str)
            or source_tip is None
            or probe.is_ancestor(source_commit, source_tip) is not True
        ):
            _add(
                findings,
                "replacement_history_not_reachable",
                "historical source commit is not reachable from replacement source_ref",
                component=component_id,
                patch=index,
            )
            continue
        disposition = patch.get("disposition")
        if disposition == "review_required":
            patch["disposition"] = "folded"
        elif disposition != "superseded":
            _add(
                findings,
                "replacement_history_disposition_invalid",
                "historical patches must be review_required or superseded before finalization",
                component=component_id,
                patch=index,
            )
            continue
        patch["integration"] = {
            "state": "not_replayed",
            "commit": None,
            "stable_patch_id": None,
        }
        patch["related_to"] = primary_replacement

    if findings:
        raise ReplacementFinalizationBlocked(findings)
    component["upstream_status"] = "required"
    component.pop("intended_upstream_status", None)
    component_source = component.get("source")
    if not isinstance(component_source, dict):
        _add(
            findings,
            "replacement_history_invalid",
            "component source must be an object",
            component=component_id,
        )
        raise ReplacementFinalizationBlocked(findings)
    component_source["ref"] = source_ref
    component["patches"] = [*historical_patches, *replacement_patches]
    component[
        "review_notes"
    ] = "Historical repair-chain commits are folded or superseded; only verified coherent replacement patches are replayed."
    draft["required_patch_ids"] = [
        patch["source"]["stable_patch_id"]
        for item in components
        if isinstance(item, dict)
        for patch in (
            item.get("patches") if isinstance(item.get("patches"), list) else []
        )
        if isinstance(patch, dict)
        and patch.get("disposition") == "required"
        and isinstance(patch.get("source"), dict)
        and isinstance(patch["source"].get("stable_patch_id"), str)
    ]
    final_findings = validate_manifest(draft)
    if final_findings:
        raise ReplacementFinalizationBlocked(final_findings)
    return draft
