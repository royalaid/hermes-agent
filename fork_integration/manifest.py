"""Schema-v2 fork integration manifest validation.

The manifest is deliberately repository-owned.  It never consults HERMES_HOME
and it represents unknown or pending identities with JSON null values rather
than plausible-looking placeholder SHAs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Any, Iterable
from urllib.parse import urlsplit


SCHEMA_VERSION = 2
UPSTREAM_STATUSES = frozenset(
    {"required", "absorbed", "superseded", "review_required"}
)
INTEGRATION_STATES = frozenset({"expected", "not_replayed", "pending"})
PATCH_ROLES = frozenset({"implementation", "test", "documentation"})
PATCH_DISPOSITIONS = frozenset(
    {"required", "absorbed_upstream", "superseded", "review_required", "folded"}
)
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ID_RE = re.compile(r"^[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?$")

_ROOT_REQUIRED = frozenset(
    {
        "schema_version",
        "manifest_state",
        "repositories",
        "integration",
        "required_categories",
        "required_patch_ids",
        "components",
    }
)
_ROOT_ALLOWED = _ROOT_REQUIRED | {"migration_notes"}
_REPOSITORY_REQUIRED = frozenset({"url"})
_REPOSITORY_ALLOWED = _REPOSITORY_REQUIRED | {
    "legacy_remote",
    "legacy_repository",
}
_INTEGRATION_REQUIRED = frozenset(
    {
        "repository",
        "ref",
        "upstream_repository",
        "upstream_ref",
        "expected_base_commit",
        "expected_head_commit",
    }
)
_COMPONENT_REQUIRED = frozenset(
    {"id", "category", "upstream_status", "source", "tests", "patches"}
)
_COMPONENT_ALLOWED = _COMPONENT_REQUIRED | {
    "intended_upstream_status",
    "review_notes",
}
_COMPONENT_SOURCE_REQUIRED = frozenset({"repository", "ref"})
_COMPONENT_SOURCE_ALLOWED = _COMPONENT_SOURCE_REQUIRED | {"legacy_ref"}
_PATCH_REQUIRED = frozenset(
    {"subject", "role", "disposition", "source", "integration"}
)
_PATCH_ALLOWED = _PATCH_REQUIRED | {"related_to"}
_PATCH_SOURCE_REQUIRED = frozenset({"commit", "stable_patch_id"})
_PATCH_INTEGRATION_REQUIRED = frozenset({"state", "commit", "stable_patch_id"})


@dataclass(frozen=True)
class Finding:
    """One stable, machine-readable manifest or audit finding."""

    code: str
    severity: str
    message: str
    component: str | None = None
    patch: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_manifest(path: str | Path) -> dict[str, Any]:
    """Load a JSON manifest without resolving any machine-local state."""

    value = parse_manifest_json(Path(path).read_text(encoding="utf-8"))
    return value


def parse_manifest_json(text: str) -> dict[str, Any]:
    """Parse a manifest while rejecting duplicate keys at every object level."""

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"manifest contains duplicate key {key!r}")
            value[key] = item
        return value

    value = json.loads(text, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError("manifest root must be a JSON object")
    return value


def _finding(
    findings: list[Finding],
    code: str,
    message: str,
    *,
    component: str | None = None,
    patch: int | None = None,
    severity: str = "error",
) -> None:
    findings.append(Finding(code, severity, message, component, patch))


def _is_full_sha(value: object) -> bool:
    return isinstance(value, str) and FULL_SHA_RE.fullmatch(value) is not None


def _is_enum_value(value: object, allowed: frozenset[str] | set[str]) -> bool:
    return isinstance(value, str) and value in allowed


def _valid_ref(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("refs/heads/"):
        return False
    forbidden = (" ", "~", "^", ":", "?", "*", "[", "\\", "..", "@{")
    components = value.split("/")
    return (
        not value.endswith(("/", "."))
        and not any(part in value for part in forbidden)
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
        and all(
            component
            and not component.startswith(".")
            and not component.endswith(".lock")
            for component in components
        )
    )


def _repository_url_is_safe(value: str) -> bool:
    parsed = urlsplit(value)
    if parsed.query or parsed.fragment:
        return False
    if parsed.scheme and parsed.netloc and (
        parsed.username is not None or parsed.password is not None
    ):
        return False
    return True


def _require_full_sha_or_null(
    findings: list[Finding],
    value: object,
    field: str,
    *,
    component: str,
    patch: int | None = None,
) -> None:
    if value is not None and not _is_full_sha(value):
        _finding(
            findings,
            "invalid_full_sha",
            f"{field} must be null or exactly 40 lowercase hexadecimal characters",
            component=component,
            patch=patch,
        )


def _validate_object_shape(
    findings: list[Finding],
    value: dict[str, Any],
    *,
    required: frozenset[str],
    allowed: frozenset[str],
    label: str,
    component: str | None = None,
    patch: int | None = None,
) -> None:
    """Enforce JSON Schema required/additionalProperties object semantics."""

    for field in sorted(required - value.keys()):
        _finding(
            findings,
            "missing_required_field",
            f"{label} is missing required field {field!r}",
            component=component,
            patch=patch,
        )
    for field in sorted(value.keys() - allowed):
        _finding(
            findings,
            "unexpected_field",
            f"{label} contains undeclared field {field!r}",
            component=component,
            patch=patch,
        )


def validate_manifest(manifest: dict[str, Any]) -> list[Finding]:
    """Validate schema-v2 invariants that do not require a Git repository."""

    findings: list[Finding] = []
    _validate_object_shape(
        findings,
        manifest,
        required=_ROOT_REQUIRED,
        allowed=_ROOT_ALLOWED,
        label="manifest",
    )
    if manifest.get("schema_version") != SCHEMA_VERSION:
        _finding(
            findings,
            "unsupported_schema",
            f"schema_version must be {SCHEMA_VERSION}",
        )

    state = manifest.get("manifest_state")
    if not _is_enum_value(state, {"ready", "review_required"}):
        _finding(
            findings,
            "invalid_manifest_state",
            "manifest_state must be ready or review_required",
        )

    migration_notes = manifest.get("migration_notes")
    if migration_notes is not None and (
        not isinstance(migration_notes, list)
        or not all(
            isinstance(note, str) and note.strip() for note in migration_notes
        )
    ):
        _finding(
            findings,
            "invalid_migration_notes",
            "migration_notes must be an array of non-empty strings",
        )

    repositories = manifest.get("repositories")
    if not isinstance(repositories, dict) or not repositories:
        _finding(findings, "missing_repositories", "repositories must be a non-empty object")
        repositories = {}
    else:
        for name, repository in repositories.items():
            if not isinstance(name, str) or not ID_RE.fullmatch(name):
                _finding(findings, "invalid_repository_id", f"invalid repository id: {name!r}")
            if not isinstance(repository, dict):
                _finding(findings, "invalid_repository", f"repository {name!r} must be an object")
                continue
            _validate_object_shape(
                findings,
                repository,
                required=_REPOSITORY_REQUIRED,
                allowed=_REPOSITORY_ALLOWED,
                label=f"repository {name!r}",
            )
            url = repository.get("url")
            if url is not None and (not isinstance(url, str) or not url.strip()):
                _finding(findings, "invalid_repository_url", f"repository {name!r} has an invalid url")
            elif isinstance(url, str) and not _repository_url_is_safe(url):
                _finding(
                    findings,
                    "unsafe_repository_url",
                    f"repository {name!r} URL must not contain userinfo, query, or fragment",
                )
            for legacy_field in ("legacy_remote", "legacy_repository"):
                legacy_value = repository.get(legacy_field)
                if legacy_value is not None and not isinstance(legacy_value, str):
                    _finding(
                        findings,
                        "invalid_legacy_repository_identity",
                        f"repository {name!r} {legacy_field} must be a string or null",
                    )

    integration = manifest.get("integration")
    if not isinstance(integration, dict):
        _finding(findings, "missing_integration", "integration must be an object")
        integration = {}
    else:
        _validate_object_shape(
            findings,
            integration,
            required=_INTEGRATION_REQUIRED,
            allowed=_INTEGRATION_REQUIRED,
            label="integration",
            component="integration",
        )

    integration_repository = integration.get("repository")
    upstream_repository = integration.get("upstream_repository")
    integration_ref = integration.get("ref")
    upstream_ref = integration.get("upstream_ref")
    for field, repository in (
        ("integration.repository", integration_repository),
        ("integration.upstream_repository", upstream_repository),
    ):
        if not isinstance(repository, str) or repository not in repositories:
            _finding(findings, "unknown_repository", f"{field} does not name a declared repository")
    for field, ref in (("integration.ref", integration_ref), ("integration.upstream_ref", upstream_ref)):
        if not _valid_ref(ref):
            _finding(
                findings,
                "invalid_ref",
                f"{field} must be a full refs/heads/... branch name",
            )
    for field in ("expected_base_commit", "expected_head_commit"):
        _require_full_sha_or_null(findings, integration.get(field), f"integration.{field}", component="integration")
        if state == "ready" and not _is_full_sha(integration.get(field)):
            _finding(
                findings,
                "ready_identity_missing",
                f"ready manifests require a full integration.{field}",
                component="integration",
            )
    if state == "ready":
        for repository_name, repository in repositories.items():
            url = repository.get("url") if isinstance(repository, dict) else None
            if not isinstance(url, str) or not url.strip():
                _finding(
                    findings,
                    "ready_repository_identity_missing",
                    f"ready manifest repository {repository_name!r} requires a non-empty URL",
                    component="integration",
                )

    required_categories = manifest.get("required_categories")
    if not isinstance(required_categories, list) or not all(
        isinstance(item, str) and ID_RE.fullmatch(item) for item in required_categories
    ):
        _finding(
            findings,
            "invalid_required_categories",
            "required_categories must be an array of lower-kebab-case identifiers",
        )
        required_categories = []
    elif len(required_categories) != len(set(required_categories)):
        _finding(
            findings,
            "duplicate_required_category",
            "required_categories entries must be unique",
        )
    required_patch_ids = manifest.get("required_patch_ids")
    if not isinstance(required_patch_ids, list):
        _finding(
            findings,
            "invalid_required_patch_ids",
            "required_patch_ids must be an ordered array of stable patch IDs",
        )
        required_patch_ids = []
    else:
        seen_required_patch_ids: set[str] = set()
        for patch_id in required_patch_ids:
            if not _is_full_sha(patch_id):
                _finding(
                    findings,
                    "invalid_required_patch_id",
                    "every required_patch_ids entry must be a full stable patch ID",
                )
            elif patch_id in seen_required_patch_ids:
                _finding(
                    findings,
                    "duplicate_required_patch_id",
                    f"required patch ID is repeated: {patch_id}",
                )
            else:
                seen_required_patch_ids.add(patch_id)

    components = manifest.get("components")
    if not isinstance(components, list):
        _finding(findings, "missing_components", "components must be an array")
        components = []
    elif not components:
        _finding(findings, "missing_components", "components must contain at least one component")

    component_ids: set[str] = set()
    categories: set[str] = set()
    source_patch_owners: dict[str, tuple[str, int]] = {}
    integration_patch_owners: dict[str, tuple[str, int]] = {}
    stable_patch_owners: dict[str, tuple[str, int]] = {}
    subject_owners: dict[str, tuple[str, int, str | None]] = {}
    component_required_patch_ids: list[str] = []

    for component_index, component_value in enumerate(components):
        if not isinstance(component_value, dict):
            _finding(findings, "invalid_component", f"component {component_index} must be an object")
            continue
        component = component_value
        component_id = component.get("id")
        label = component_id if isinstance(component_id, str) else f"component-{component_index}"
        _validate_object_shape(
            findings,
            component,
            required=_COMPONENT_REQUIRED,
            allowed=_COMPONENT_ALLOWED,
            label="component",
            component=label,
        )
        if not isinstance(component_id, str) or not ID_RE.fullmatch(component_id):
            _finding(findings, "invalid_component_id", "component id must be lower-kebab-case", component=label)
        elif component_id in component_ids:
            _finding(findings, "duplicate_component_id", f"duplicate component id {component_id}", component=label)
        else:
            component_ids.add(component_id)

        category = component.get("category")
        if not isinstance(category, str) or not ID_RE.fullmatch(category):
            _finding(findings, "invalid_category", "component category must be lower-kebab-case", component=label)
        else:
            categories.add(category)

        upstream_status = component.get("upstream_status")
        if not _is_enum_value(upstream_status, UPSTREAM_STATUSES):
            _finding(
                findings,
                "invalid_upstream_status",
                f"upstream_status must be one of {sorted(UPSTREAM_STATUSES)}",
                component=label,
            )
        intended_status = component.get("intended_upstream_status")
        if intended_status is not None and not _is_enum_value(
            intended_status, {"required", "absorbed", "superseded"}
        ):
            _finding(
                findings,
                "invalid_intended_upstream_status",
                "intended_upstream_status must be required, absorbed, or superseded",
                component=label,
            )
        if intended_status is not None and upstream_status != "review_required":
            _finding(
                findings,
                "unexpected_intended_upstream_status",
                "intended_upstream_status is only valid while upstream_status is review_required",
                component=label,
            )
        review_notes = component.get("review_notes")
        if review_notes is not None and (
            not isinstance(review_notes, str) or not review_notes.strip()
        ):
            _finding(
                findings,
                "invalid_review_notes",
                "review_notes must be a non-empty string",
                component=label,
            )

        tests = component.get("tests")
        if not isinstance(tests, list) or not tests or not all(
            isinstance(test, str) and test.strip() for test in tests
        ):
            _finding(findings, "missing_owning_tests", "component must name at least one owning test", component=label)
        elif len(tests) != len(set(tests)):
            _finding(
                findings,
                "duplicate_owning_test",
                "component owning tests must be unique",
                component=label,
            )

        source = component.get("source")
        if not isinstance(source, dict):
            _finding(findings, "missing_source", "component source must be an object", component=label)
            source = {}
        else:
            _validate_object_shape(
                findings,
                source,
                required=_COMPONENT_SOURCE_REQUIRED,
                allowed=_COMPONENT_SOURCE_ALLOWED,
                label="component source",
                component=label,
            )
        source_repository = source.get("repository")
        source_ref = source.get("ref")
        legacy_ref = source.get("legacy_ref")
        if not isinstance(source_repository, str) or source_repository not in repositories:
            _finding(findings, "unknown_source_repository", "source.repository is not declared", component=label)
        if source_ref is not None and not _valid_ref(source_ref):
            _finding(
                findings,
                "invalid_source_ref",
                "source.ref must be null or a full refs/heads/... branch name",
                component=label,
            )
        if legacy_ref is not None and not isinstance(legacy_ref, str):
            _finding(
                findings,
                "invalid_legacy_source_ref",
                "source.legacy_ref must be a string or null",
                component=label,
            )
        source_repository_value = (
            repositories.get(source_repository, {})
            if isinstance(source_repository, str)
            else {}
        )
        integration_repository_value = (
            repositories.get(integration_repository, {})
            if isinstance(integration_repository, str)
            else {}
        )
        source_url = (
            source_repository_value.get("url")
            if isinstance(source_repository_value, dict)
            else None
        )
        integration_url = (
            integration_repository_value.get("url")
            if isinstance(integration_repository_value, dict)
            else None
        )
        same_repository = source_repository == integration_repository or (
            source_url is not None and source_url == integration_url
        )
        if same_repository and source_ref == integration_ref and source_ref is not None:
            _finding(
                findings,
                "source_target_same_ref",
                "component source ref is the integration target ref",
                component=label,
            )
        if source_ref is None and upstream_status != "review_required":
            _finding(
                findings,
                "missing_source_ref",
                "only review_required components may omit an immutable source ref",
                component=label,
            )
        if state == "ready":
            if source_ref is None:
                _finding(
                    findings,
                    "ready_identity_missing",
                    "ready component source.ref must be present",
                    component=label,
                )
            if not isinstance(source_url, str) or not source_url.strip():
                _finding(
                    findings,
                    "ready_repository_identity_missing",
                    "ready component source repository requires a URL",
                    component=label,
                )

        patches = component.get("patches")
        if not isinstance(patches, list) or not patches:
            _finding(findings, "missing_patches", "component must contain patches", component=label)
            continue
        source_commits: set[str] = set()
        for patch_index, patch_value in enumerate(patches):
            if not isinstance(patch_value, dict):
                _finding(findings, "invalid_patch", "patch must be an object", component=label, patch=patch_index)
                continue
            patch = patch_value
            _validate_object_shape(
                findings,
                patch,
                required=_PATCH_REQUIRED,
                allowed=_PATCH_ALLOWED,
                label="patch",
                component=label,
                patch=patch_index,
            )
            subject = patch.get("subject")
            if not isinstance(subject, str) or not subject.strip():
                _finding(findings, "missing_subject", "patch subject must be non-empty", component=label, patch=patch_index)
                subject = ""
            role = patch.get("role")
            if not _is_enum_value(role, PATCH_ROLES):
                _finding(findings, "invalid_patch_role", f"patch role must be one of {sorted(PATCH_ROLES)}", component=label, patch=patch_index)
            disposition = patch.get("disposition")
            if not _is_enum_value(disposition, PATCH_DISPOSITIONS):
                _finding(
                    findings,
                    "invalid_patch_disposition",
                    f"patch disposition must be one of {sorted(PATCH_DISPOSITIONS)}",
                    component=label,
                    patch=patch_index,
                )
            if disposition == "review_required" and upstream_status != "review_required":
                _finding(
                    findings,
                    "review_patch_in_ready_component",
                    "review_required patch disposition requires a review_required component",
                    component=label,
                    patch=patch_index,
                )
            if upstream_status == "absorbed" and disposition != "absorbed_upstream":
                _finding(
                    findings,
                    "absorbed_component_has_replay_patch",
                    "absorbed components may contain only absorbed_upstream patches",
                    component=label,
                    patch=patch_index,
                )
            if upstream_status == "superseded" and not _is_enum_value(
                disposition, {"superseded", "folded"}
            ):
                _finding(
                    findings,
                    "superseded_component_has_live_patch",
                    "superseded components may contain only superseded or folded patches",
                    component=label,
                    patch=patch_index,
                )
            if upstream_status == "required" and not _is_enum_value(
                disposition, {"required", "folded", "superseded"}
            ):
                _finding(
                    findings,
                    "required_component_has_nonrequired_patch",
                    "required components may contain only required, folded, or superseded patches",
                    component=label,
                    patch=patch_index,
                )

            source_identity = patch.get("source")
            if not isinstance(source_identity, dict):
                _finding(findings, "missing_source_identity", "patch source identity must be an object", component=label, patch=patch_index)
                source_identity = {}
            else:
                _validate_object_shape(
                    findings,
                    source_identity,
                    required=_PATCH_SOURCE_REQUIRED,
                    allowed=_PATCH_SOURCE_REQUIRED,
                    label="patch source identity",
                    component=label,
                    patch=patch_index,
                )
            source_commit = source_identity.get("commit")
            source_patch_id = source_identity.get("stable_patch_id")
            _require_full_sha_or_null(findings, source_commit, "patch.source.commit", component=label, patch=patch_index)
            _require_full_sha_or_null(findings, source_patch_id, "patch.source.stable_patch_id", component=label, patch=patch_index)
            if source_commit is None or source_patch_id is None:
                if upstream_status != "review_required":
                    _finding(
                        findings,
                        "missing_source_identity",
                        "only review_required components may have incomplete source identities",
                        component=label,
                        patch=patch_index,
                    )
            if state == "ready" and (
                not _is_full_sha(source_commit) or not _is_full_sha(source_patch_id)
            ):
                _finding(
                    findings,
                    "ready_identity_missing",
                    "ready patches require full source commit and stable patch identities",
                    component=label,
                    patch=patch_index,
                )
            if isinstance(source_commit, str):
                if source_commit in source_commits:
                    _finding(findings, "duplicate_source_commit", "source commit is repeated in one component", component=label, patch=patch_index)
                source_commits.add(source_commit)
            if _is_full_sha(source_patch_id):
                if disposition == "required":
                    component_required_patch_ids.append(source_patch_id)
                owner = source_patch_owners.get(source_patch_id)
                if owner is not None and owner != (label, patch_index):
                    _finding(
                        findings,
                        "duplicate_stable_patch_id",
                        f"source stable patch id is already owned by {owner[0]} patch {owner[1]}",
                        component=label,
                        patch=patch_index,
                    )
                source_patch_owners[source_patch_id] = (label, patch_index)
                global_owner = stable_patch_owners.get(source_patch_id)
                if global_owner is not None and global_owner != (label, patch_index):
                    _finding(
                        findings,
                        "duplicate_stable_patch_id",
                        f"stable patch id is already owned by {global_owner[0]} patch {global_owner[1]}",
                        component=label,
                        patch=patch_index,
                    )
                stable_patch_owners[source_patch_id] = (label, patch_index)

            related_to = patch.get("related_to")
            if related_to is not None:
                _require_full_sha_or_null(findings, related_to, "patch.related_to", component=label, patch=patch_index)
                if isinstance(related_to, str) and related_to == source_commit:
                    _finding(
                        findings,
                        "self_referential_patch_relationship",
                        "patch.related_to must not refer to the same patch source commit",
                        component=label,
                        patch=patch_index,
                    )

            final_identity = patch.get("integration")
            if not isinstance(final_identity, dict):
                _finding(findings, "missing_integration_identity", "patch integration identity must be an object", component=label, patch=patch_index)
                final_identity = {}
            else:
                _validate_object_shape(
                    findings,
                    final_identity,
                    required=_PATCH_INTEGRATION_REQUIRED,
                    allowed=_PATCH_INTEGRATION_REQUIRED,
                    label="patch integration identity",
                    component=label,
                    patch=patch_index,
                )
            final_state = final_identity.get("state")
            final_commit = final_identity.get("commit")
            final_patch_id = final_identity.get("stable_patch_id")
            if not _is_enum_value(final_state, INTEGRATION_STATES):
                _finding(findings, "invalid_integration_state", f"integration state must be one of {sorted(INTEGRATION_STATES)}", component=label, patch=patch_index)
            _require_full_sha_or_null(findings, final_commit, "patch.integration.commit", component=label, patch=patch_index)
            _require_full_sha_or_null(findings, final_patch_id, "patch.integration.stable_patch_id", component=label, patch=patch_index)
            if final_state == "expected" and final_patch_id is None:
                _finding(findings, "missing_expected_patch_id", "expected integration patches require a stable patch id", component=label, patch=patch_index)
            if state == "ready" and final_state == "expected" and (
                not _is_full_sha(final_commit) or not _is_full_sha(final_patch_id)
            ):
                _finding(
                    findings,
                    "ready_identity_missing",
                    "ready expected patches require full integration commit and stable patch identities",
                    component=label,
                    patch=patch_index,
                )
            if final_state == "pending" and (final_commit is not None or final_patch_id is not None):
                _finding(findings, "pending_identity_is_not_null", "pending integration identities must be null", component=label, patch=patch_index)
            if final_state == "pending" and upstream_status != "review_required":
                _finding(findings, "pending_without_review", "pending integration identity requires review_required", component=label, patch=patch_index)
            if final_state == "not_replayed" and (final_commit is not None or final_patch_id is not None):
                _finding(findings, "not_replayed_has_identity", "not_replayed integration identities must be null", component=label, patch=patch_index)
            if final_state == "not_replayed" and not _is_enum_value(
                disposition, {"absorbed_upstream", "superseded", "folded"}
            ):
                _finding(
                    findings,
                    "invalid_not_replayed_disposition",
                    "not_replayed requires an absorbed_upstream, superseded, or folded patch disposition",
                    component=label,
                    patch=patch_index,
                )
            if (
                _is_enum_value(
                    disposition, {"absorbed_upstream", "superseded", "folded"}
                )
                and final_state != "not_replayed"
            ):
                _finding(
                    findings,
                    "non_replay_disposition_has_replay_state",
                    f"{disposition} patches must use integration.state=not_replayed",
                    component=label,
                    patch=patch_index,
                )
            if disposition == "review_required" and final_state != "pending":
                _finding(
                    findings,
                    "review_patch_has_final_identity",
                    "review_required patches must keep integration identity pending",
                    component=label,
                    patch=patch_index,
                )
            if (
                disposition == "required"
                and _is_full_sha(source_patch_id)
                and _is_full_sha(final_patch_id)
                and source_patch_id != final_patch_id
            ):
                _finding(
                    findings,
                    "required_patch_identity_mismatch",
                    "required replay source and integration stable patch IDs must be equal",
                    component=label,
                    patch=patch_index,
                )
            if _is_full_sha(final_patch_id):
                owner = integration_patch_owners.get(final_patch_id)
                if owner is not None and owner != (label, patch_index):
                    _finding(
                        findings,
                        "duplicate_stable_patch_id",
                        f"integration stable patch id is already owned by {owner[0]} patch {owner[1]}",
                        component=label,
                        patch=patch_index,
                    )
                integration_patch_owners[final_patch_id] = (label, patch_index)
                global_owner = stable_patch_owners.get(final_patch_id)
                if global_owner is not None and global_owner != (label, patch_index):
                    _finding(
                        findings,
                        "duplicate_stable_patch_id",
                        f"stable patch id is already owned by {global_owner[0]} patch {global_owner[1]}",
                        component=label,
                        patch=patch_index,
                    )
                stable_patch_owners[final_patch_id] = (label, patch_index)

            if subject:
                prior = subject_owners.get(subject)
                if prior is not None and prior[:2] != (label, patch_index):
                    prior_patch_id = prior[2]
                    if prior_patch_id != source_patch_id:
                        _finding(
                            findings,
                            "same_subject_non_equivalent",
                            f"subject is reused with a different source patch identity by {prior[0]} patch {prior[1]}",
                            component=label,
                            patch=patch_index,
                        )
                else:
                    subject_owners[subject] = (label, patch_index, source_patch_id if isinstance(source_patch_id, str) else None)

        for patch_index, patch_value in enumerate(patches):
            if isinstance(patch_value, dict) and patch_value.get("related_to") is not None:
                related_to = patch_value["related_to"]
                if not isinstance(related_to, str) or related_to not in source_commits:
                    _finding(
                        findings,
                        "invalid_patch_relationship",
                        "related_to must name another source commit in the same component",
                        component=label,
                        patch=patch_index,
                    )

    for required_category in required_categories:
        if required_category not in categories:
            _finding(
                findings,
                "missing_required_category",
                f"manifest has no component in required category {required_category!r}",
            )
    if "updater" not in categories:
        _finding(findings, "missing_updater_component", "manifest must include a native updater component")
    required_patch_set = {
        patch_id for patch_id in required_patch_ids if _is_full_sha(patch_id)
    }
    component_required_patch_set = set(component_required_patch_ids)
    for patch_id in sorted(required_patch_set - component_required_patch_set):
        _finding(
            findings,
            "required_patch_missing",
            f"required patch ledger entry is absent from required components: {patch_id}",
        )
    for patch_id in sorted(component_required_patch_set - required_patch_set):
        _finding(
            findings,
            "required_patch_not_listed",
            f"required component patch is absent from required_patch_ids: {patch_id}",
        )
    if (
        required_patch_set == component_required_patch_set
        and required_patch_ids != component_required_patch_ids
    ):
        _finding(
            findings,
            "required_patch_order_mismatch",
            "required_patch_ids must match required component patch order",
        )
    if state == "ready" and any(
        isinstance(component, dict) and component.get("upstream_status") == "review_required"
        for component in components
    ):
        _finding(findings, "ready_manifest_has_review_component", "ready manifests cannot contain review_required components")
    if state == "ready" and any(
        isinstance(patch, dict) and patch.get("disposition") == "review_required"
        for component in components
        if isinstance(component, dict)
        for patch in (
            component.get("patches")
            if isinstance(component.get("patches"), list)
            else []
        )
    ):
        _finding(
            findings,
            "ready_manifest_has_review_patch",
            "ready manifests cannot contain review_required patch dispositions",
        )
    return findings


def migrate_schema_1(legacy: dict[str, Any]) -> dict[str, Any]:
    """Return a review-required schema-v2 draft without inventing identities.

    Legacy remote names and source refs are preserved as evidence only because
    they are machine-local names, not portable repository provenance.
    """

    if legacy.get("schema") != 1:
        raise ValueError("input is not a schema-1 manifest")
    fork = legacy.get("fork") if isinstance(legacy.get("fork"), dict) else {}
    upstream = legacy.get("upstream") if isinstance(legacy.get("upstream"), dict) else {}
    integration_branch = legacy.get("integration_branch")
    integration_ref = (
        f"refs/heads/{integration_branch}"
        if isinstance(integration_branch, str) and integration_branch
        else "refs/heads/REVIEW-REQUIRED"
    )
    legacy_components = legacy.get("components", [])
    if not isinstance(legacy_components, list):
        raise ValueError("schema-1 components must be an array")
    migrated_components: list[dict[str, Any]] = []
    for index, old_component in enumerate(legacy_components):
        if not isinstance(old_component, dict):
            raise ValueError(f"schema-1 components[{index}] must be an object")
        component_id = old_component.get("id")
        if not isinstance(component_id, str) or not component_id:
            component_id = f"review-required-{index + 1}"
        patches: list[dict[str, Any]] = []
        legacy_patches = old_component.get("patches", [])
        if not isinstance(legacy_patches, list):
            raise ValueError(
                f"schema-1 components[{index}].patches must be an array"
            )
        for patch_index, old_patch in enumerate(legacy_patches):
            if not isinstance(old_patch, dict):
                raise ValueError(
                    f"schema-1 components[{index}].patches[{patch_index}] "
                    "must be an object"
                )
            commit = old_patch.get("commit") if _is_full_sha(old_patch.get("commit")) else None
            patch_id = (
                old_patch.get("stable_patch_id")
                if _is_full_sha(old_patch.get("stable_patch_id"))
                else None
            )
            patches.append(
                {
                    "subject": old_patch.get("subject") or "REVIEW REQUIRED",
                    "role": "implementation",
                    "disposition": "review_required",
                    "source": {"commit": commit, "stable_patch_id": patch_id},
                    "integration": {
                        "state": "pending",
                        "commit": None,
                        "stable_patch_id": None,
                    },
                }
            )
        migrated_components.append(
            {
                "id": component_id,
                "category": "review-required",
                "upstream_status": "review_required",
                "source": {
                    "repository": "fork",
                    "ref": None,
                    "legacy_ref": old_component.get("source_ref"),
                },
                "tests": [],
                "review_notes": "Assign category, portable source ref, owning tests, and final identities.",
                "patches": patches,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_state": "review_required",
        "repositories": {
            "upstream": {"url": None, "legacy_remote": upstream.get("remote")},
            "fork": {
                "url": None,
                "legacy_remote": fork.get("remote"),
                "legacy_repository": fork.get("repository"),
            },
        },
        "integration": {
            "repository": "fork",
            "ref": integration_ref,
            "upstream_repository": "upstream",
            "upstream_ref": upstream.get("ref") or "refs/heads/REVIEW-REQUIRED",
            "expected_base_commit": None,
            "expected_head_commit": None,
        },
        "required_categories": ["updater"],
        "required_patch_ids": [],
        "components": migrated_components,
        "migration_notes": [
            "All component dispositions require review.",
            "No integration commit or patch identity was inferred from a subject.",
            "Legacy remote and source-ref names are evidence only.",
        ],
    }


def findings_as_dicts(findings: Iterable[Finding]) -> list[dict[str, Any]]:
    return [finding.to_dict() for finding in findings]
