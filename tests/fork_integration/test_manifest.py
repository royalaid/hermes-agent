from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from fork_integration.manifest import load_manifest, migrate_schema_1, validate_manifest


SOURCE_COMMIT = "1" * 40
SOURCE_PATCH = "2" * 40
FINAL_COMMIT = "3" * 40
FINAL_PATCH = SOURCE_PATCH


def valid_manifest() -> dict:
    return {
        "schema_version": 2,
        "manifest_state": "ready",
        "repositories": {
            "upstream": {"url": "https://example.test/upstream.git"},
            "fork": {"url": "https://example.test/fork.git"},
        },
        "integration": {
            "repository": "fork",
            "ref": "refs/heads/fork-integration",
            "upstream_repository": "upstream",
            "upstream_ref": "refs/heads/main",
            "expected_base_commit": "5" * 40,
            "expected_head_commit": "6" * 40,
        },
        "required_categories": ["updater"],
        "required_patch_ids": [SOURCE_PATCH],
        "components": [
            {
                "id": "native-windows-updater",
                "category": "updater",
                "upstream_status": "required",
                "source": {
                    "repository": "fork",
                    "ref": "refs/heads/fix/native-windows-updater",
                },
                "tests": ["tests/updater/test_native_windows.py"],
                "patches": [
                    {
                        "subject": "fix(updater): release Windows runtime holders",
                        "role": "implementation",
                        "disposition": "required",
                        "source": {
                            "commit": SOURCE_COMMIT,
                            "stable_patch_id": SOURCE_PATCH,
                        },
                        "integration": {
                            "state": "expected",
                            "commit": FINAL_COMMIT,
                            "stable_patch_id": FINAL_PATCH,
                        },
                    }
                ],
            }
        ],
    }


def codes(manifest: dict) -> set[str]:
    return {finding.code for finding in validate_manifest(manifest)}


def test_valid_manifest_satisfies_structural_contract():
    assert validate_manifest(valid_manifest()) == []


def test_rejects_same_source_and_target_ref():
    manifest = valid_manifest()
    manifest["components"][0]["source"]["ref"] = manifest["integration"]["ref"]

    assert "source_target_same_ref" in codes(manifest)


def test_rejects_same_source_and_target_ref_through_repository_alias():
    manifest = valid_manifest()
    manifest["repositories"]["fork-alias"] = {
        "url": manifest["repositories"]["fork"]["url"]
    }
    manifest["components"][0]["source"] = {
        "repository": "fork-alias",
        "ref": manifest["integration"]["ref"],
    }

    assert "source_target_same_ref" in codes(manifest)


def test_rejects_sha_typo_and_duplicate_stable_patch_identity():
    manifest = valid_manifest()
    duplicate = deepcopy(manifest["components"][0])
    duplicate["id"] = "second-updater-component"
    duplicate["patches"][0]["source"]["commit"] = "abc123"
    manifest["components"].append(duplicate)

    result = codes(manifest)

    assert "invalid_full_sha" in result
    assert "duplicate_stable_patch_id" in result


def test_rejects_same_subject_with_non_equivalent_source_identity():
    manifest = valid_manifest()
    duplicate = deepcopy(manifest["components"][0])
    duplicate["id"] = "another-updater-component"
    duplicate["patches"][0]["source"]["commit"] = "7" * 40
    duplicate["patches"][0]["source"]["stable_patch_id"] = "8" * 40
    duplicate["patches"][0]["integration"]["stable_patch_id"] = "9" * 40
    manifest["components"].append(duplicate)

    assert "same_subject_non_equivalent" in codes(manifest)


def test_rejects_manifest_without_updater_component():
    manifest = valid_manifest()
    manifest["components"][0]["category"] = "desktop"

    result = codes(manifest)

    assert "missing_updater_component" in result
    assert "missing_required_category" in result


def test_pending_identity_requires_review_and_contains_no_fake_sha():
    manifest = valid_manifest()
    component = manifest["components"][0]
    component["upstream_status"] = "review_required"
    component["intended_upstream_status"] = "required"
    component["patches"][0]["integration"] = {
        "state": "pending",
        "commit": None,
        "stable_patch_id": None,
    }
    manifest["manifest_state"] = "review_required"

    assert validate_manifest(manifest) == []


def test_replay_ledger_rejects_review_or_superseded_patch_identity():
    manifest = valid_manifest()
    component = manifest["components"][0]
    component["upstream_status"] = "review_required"
    component["intended_upstream_status"] = "superseded"
    patch = component["patches"][0]
    patch["disposition"] = "superseded"
    patch["integration"] = {
        "state": "not_replayed",
        "commit": None,
        "stable_patch_id": None,
    }
    manifest["manifest_state"] = "review_required"

    assert "required_patch_missing" in codes(manifest)

    manifest["required_patch_ids"] = []
    assert validate_manifest(manifest) == []


def test_runtime_contract_rejects_missing_and_additional_properties():
    manifest = valid_manifest()
    del manifest["required_patch_ids"]
    manifest["undeclared"] = True
    manifest["repositories"]["fork"]["undeclared"] = True
    manifest["components"][0]["patches"][0]["source"]["undeclared"] = True

    result = codes(manifest)

    assert "missing_required_field" in result
    assert "unexpected_field" in result


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda manifest: manifest.update(migration_notes=[""]), "invalid_migration_notes"),
        (
            lambda manifest: manifest["repositories"]["fork"].update(
                legacy_remote=42
            ),
            "invalid_legacy_repository_identity",
        ),
        (
            lambda manifest: manifest.update(required_categories=["Not Valid"]),
            "invalid_required_categories",
        ),
        (
            lambda manifest: manifest["components"][0].update(review_notes=""),
            "invalid_review_notes",
        ),
        (
            lambda manifest: manifest["components"][0]["source"].update(
                legacy_ref=42
            ),
            "invalid_legacy_source_ref",
        ),
    ],
)
def test_runtime_contract_rejects_optional_values_outside_schema(
    mutate, expected_code: str
):
    manifest = valid_manifest()
    mutate(manifest)

    assert expected_code in codes(manifest)


def test_runtime_contract_handles_malformed_patch_collection():
    manifest = valid_manifest()
    manifest["components"][0]["patches"] = 42

    assert "missing_patches" in codes(manifest)


@pytest.mark.parametrize("invalid_ready_state", ["review_component", "pending_patch"])
def test_committed_schema_and_runtime_both_reject_nonready_content_in_ready_manifest(
    invalid_ready_state: str,
):
    manifest = valid_manifest()
    component = manifest["components"][0]
    if invalid_ready_state == "review_component":
        component["upstream_status"] = "review_required"
        component["intended_upstream_status"] = "required"
    else:
        component["upstream_status"] = "review_required"
        component["intended_upstream_status"] = "required"
        component["patches"][0]["disposition"] = "review_required"
        component["patches"][0]["integration"] = {
            "state": "pending",
            "commit": None,
            "stable_patch_id": None,
        }
        manifest["required_patch_ids"] = []
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "fork_integration"
        / "manifest.schema.v2.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert list(Draft202012Validator(schema).iter_errors(manifest))
    assert validate_manifest(manifest)


def test_ready_manifest_requires_all_immutable_final_identities():
    manifest = valid_manifest()
    manifest["integration"]["expected_head_commit"] = None
    manifest["components"][0]["patches"][0]["integration"]["commit"] = None

    assert "ready_identity_missing" in codes(manifest)


def test_ready_unused_repository_requires_nonempty_url_in_schema_and_runtime():
    manifest = valid_manifest()
    manifest["repositories"]["unused"] = {"url": None}
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "fork_integration"
        / "manifest.schema.v2.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert list(Draft202012Validator(schema).iter_errors(manifest))
    assert "ready_repository_identity_missing" in codes(manifest)


@pytest.mark.parametrize(
    ("repository_id", "valid"),
    [
        ("extra-repository", True),
        ("Extra", False),
        ("bad_name", False),
        ("-leading", False),
        ("trailing-", False),
        ("space name", False),
        ("terminal-newline\n", False),
        ("", False),
    ],
)
def test_repository_identifier_schema_and_runtime_have_exact_parity(
    repository_id: str, valid: bool,
):
    manifest = valid_manifest()
    manifest["repositories"][repository_id] = {
        "url": "https://example.test/extra.git"
    }
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "fork_integration"
        / "manifest.schema.v2.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema_rejected = bool(list(Draft202012Validator(schema).iter_errors(manifest)))
    runtime_rejected = "invalid_repository_id" in codes(manifest)

    assert schema_rejected is not valid
    assert runtime_rejected is not valid


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda manifest: manifest["components"][0]["patches"][0].update(
                subject=" \t"
            ),
            "missing_subject",
        ),
        (
            lambda manifest: manifest["components"][0].update(tests=[" \t"]),
            "missing_owning_tests",
        ),
        (
            lambda manifest: manifest["components"][0].update(
                review_notes=" \t"
            ),
            "invalid_review_notes",
        ),
        (
            lambda manifest: manifest.update(migration_notes=[" \t"]),
            "invalid_migration_notes",
        ),
    ],
)
def test_schema_and_runtime_reject_whitespace_only_required_text(
    mutate, expected_code: str,
):
    manifest = valid_manifest()
    mutate(manifest)
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "fork_integration"
        / "manifest.schema.v2.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert list(Draft202012Validator(schema).iter_errors(manifest))
    assert expected_code in codes(manifest)


def test_required_replay_source_and_final_patch_identities_must_match():
    manifest = valid_manifest()
    manifest["components"][0]["patches"][0]["integration"][
        "stable_patch_id"
    ] = "4" * 40

    assert "required_patch_identity_mismatch" in codes(manifest)


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda manifest: manifest.update(manifest_state=[]), "invalid_manifest_state"),
        (
            lambda manifest: manifest["components"][0].update(
                upstream_status={}
            ),
            "invalid_upstream_status",
        ),
        (
            lambda manifest: manifest["components"][0].update(
                intended_upstream_status=[]
            ),
            "invalid_intended_upstream_status",
        ),
        (
            lambda manifest: manifest["components"][0]["patches"][0].update(
                role=[]
            ),
            "invalid_patch_role",
        ),
        (
            lambda manifest: manifest["components"][0]["patches"][0].update(
                disposition={}
            ),
            "invalid_patch_disposition",
        ),
        (
            lambda manifest: manifest["components"][0]["patches"][0][
                "integration"
            ].update(state=[]),
            "invalid_integration_state",
        ),
        (
            lambda manifest: manifest["components"][0]["patches"][0].update(
                related_to=[]
            ),
            "invalid_full_sha",
        ),
        (
            lambda manifest: manifest["components"][0]["patches"][0].update(
                integration=[]
            ),
            "missing_integration_identity",
        ),
    ],
)
def test_container_valued_enum_and_relationship_fields_are_findings(
    mutate, expected_code: str
):
    manifest = valid_manifest()
    mutate(manifest)

    assert expected_code in codes(manifest)


def test_related_to_cannot_refer_to_its_own_source_commit():
    manifest = valid_manifest()
    patch = manifest["components"][0]["patches"][0]
    patch["related_to"] = patch["source"]["commit"]

    assert "self_referential_patch_relationship" in codes(manifest)


@pytest.mark.parametrize("field", ["ref", "upstream_ref", "source_ref"])
def test_manifest_forbids_annotated_tag_refs(field: str):
    manifest = valid_manifest()
    if field == "source_ref":
        manifest["components"][0]["source"]["ref"] = "refs/tags/v1"
        expected_code = "invalid_source_ref"
    else:
        manifest["integration"][field] = "refs/tags/v1"
        expected_code = "invalid_ref"

    assert expected_code in codes(manifest)


def test_manifest_rejects_git_invalid_branch_ref_components():
    manifest = valid_manifest()
    manifest["integration"]["ref"] = "refs/heads/release.lock"

    assert "invalid_ref" in codes(manifest)


@pytest.mark.parametrize("url", [None, "", "   "])
def test_ready_repository_url_null_and_blank_match_schema_rejection(url):
    manifest = valid_manifest()
    manifest["repositories"]["fork"]["url"] = url
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "fork_integration"
        / "manifest.schema.v2.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert list(Draft202012Validator(schema).iter_errors(manifest))
    assert validate_manifest(manifest)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.test/Owner/Repo.git",
        "https://example.test/Owner/Repo.git?token=secret",
        "https://example.test/Owner/Repo.git#secret",
    ],
)
def test_manifest_rejects_repository_url_secrets_and_suffix_metadata(url: str):
    manifest = valid_manifest()
    manifest["repositories"]["fork"]["url"] = url
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "fork_integration"
        / "manifest.schema.v2.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert "unsafe_repository_url" in codes(manifest)
    assert list(Draft202012Validator(schema).iter_errors(manifest))


def test_json_loader_rejects_duplicate_keys(tmp_path: Path):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version": 2, "schema_version": 1}', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="duplicate key 'schema_version'"):
        load_manifest(duplicate)


def test_schema_1_migration_is_stdout_ready_review_draft_without_final_ids():
    legacy = {
        "schema": 1,
        "integration_branch": "fork-integration",
        "upstream": {"remote": "origin", "ref": "refs/heads/main"},
        "fork": {"remote": "fork", "repository": "owner/repository"},
        "components": [
            {
                "id": "legacy-feature",
                "source_ref": "fork/feature",
                "patches": [
                    {
                        "commit": SOURCE_COMMIT,
                        "stable_patch_id": SOURCE_PATCH,
                        "subject": "fix(feature): keep behavior",
                    }
                ],
            }
        ],
    }

    draft = migrate_schema_1(legacy)
    patch = draft["components"][0]["patches"][0]

    assert draft["schema_version"] == 2
    assert draft["manifest_state"] == "review_required"
    assert draft["components"][0]["upstream_status"] == "review_required"
    assert draft["components"][0]["source"]["ref"] is None
    assert patch["source"]["commit"] == SOURCE_COMMIT
    assert patch["integration"] == {
        "state": "pending",
        "commit": None,
        "stable_patch_id": None,
    }
