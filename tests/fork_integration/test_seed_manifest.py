from __future__ import annotations

from fork_integration.cli import DEFAULT_MANIFEST
from fork_integration.manifest import load_manifest, validate_manifest


def components_by_id(manifest: dict) -> dict[str, dict]:
    return {component["id"]: component for component in manifest["components"]}


def test_seed_manifest_is_structurally_valid_review_draft():
    manifest = load_manifest(DEFAULT_MANIFEST)

    assert validate_manifest(manifest) == []
    assert manifest["manifest_state"] == "review_required"
    assert manifest["integration"]["expected_head_commit"] is None


def test_seed_pins_repository_refs_base_and_patch_dispositions():
    manifest = load_manifest(DEFAULT_MANIFEST)
    assert manifest["repositories"] == {
        "upstream": {"url": "https://github.com/NousResearch/hermes-agent.git"},
        "fork": {"url": "https://github.com/royalaid/hermes-agent.git"},
    }
    assert manifest["integration"] == {
        "repository": "fork",
        "ref": "refs/heads/fork-integration",
        "upstream_repository": "upstream",
        "upstream_ref": "refs/heads/main",
        "expected_base_commit": "e5bc6b21868efad57414b1d28abbbb5ce26765c9",
        "expected_head_commit": None,
    }
    dispositions = {
        component["id"]: [patch["disposition"] for patch in component["patches"]]
        for component in manifest["components"]
    }
    assert dispositions == {
        "windows-host-path": ["required"],
        "reasoning-item-boundaries": ["required"] * 4,
        "desktop-hitch-diagnostics": ["required"] * 8,
        "gateway-config-offloop": ["required"] * 2,
        "thread-stream-instrumentation": ["required"],
        "inflight-journal-bounded": ["required"] * 2,
        "delegation-argv-classifier": ["required"],
        "fork-update-status": ["required"],
        "native-compaction-upstream-core": ["absorbed_upstream"],
        "native-compaction-replay-safety": ["required"] * 2,
        "legacy-reasoning-summary-boundaries": ["required"],
        "diagnostics-test-block-duration": ["required"],
        "bootstrap-build-ref-pin": ["required"],
        "native-windows-updater-mcp": ["review_required"] * 10
        + ["superseded"] * 3,
    }


def test_seed_preserves_legacy_fork_features_without_claiming_missing_sources():
    manifest = load_manifest(DEFAULT_MANIFEST)
    components = components_by_id(manifest)
    retained = {
        "windows-host-path",
        "reasoning-item-boundaries",
        "desktop-hitch-diagnostics",
        "gateway-config-offloop",
        "thread-stream-instrumentation",
        "inflight-journal-bounded",
        "delegation-argv-classifier",
        "fork-update-status",
    }

    assert retained <= components.keys()
    for component_id in retained:
        component = components[component_id]
        assert component["upstream_status"] == "review_required"
        assert component["intended_upstream_status"] == "required"
        assert component["source"]["ref"] is None
        assert component["tests"]


def test_native_compaction_core_is_absorbed_and_not_replayed():
    component = components_by_id(load_manifest(DEFAULT_MANIFEST))[
        "native-compaction-upstream-core"
    ]
    patch = component["patches"][0]

    assert component["upstream_status"] == "absorbed"
    assert patch["source"]["commit"] == "5e1b50115f01cda8f8749a347d6a75aeda03ff18"
    assert patch["source"]["stable_patch_id"] == "5eb223dec2ffeeeadbccaa143fd7e7e00dbc856e"
    assert patch["integration"] == {
        "state": "not_replayed",
        "commit": None,
        "stable_patch_id": None,
    }
    assert "9c12c494" in component["review_notes"]
    assert "e13fd" in component["review_notes"]


def test_native_replay_test_and_fixes_are_separate_from_diagnostics_and_bootstrap():
    components = components_by_id(load_manifest(DEFAULT_MANIFEST))
    replay = components["native-compaction-replay-safety"]

    assert [patch["source"]["commit"] for patch in replay["patches"]] == [
        "8786aeca47be65e767a279e93791d89253a41cb7",
        "9b82e111bf4531d6a82bce905a94cbdcab47e647",
    ]
    assert replay["patches"][0]["role"] == "test"
    assert replay["patches"][0]["related_to"] == replay["patches"][1]["source"]["commit"]
    assert components["legacy-reasoning-summary-boundaries"]["patches"][0]["source"]["commit"].startswith("67541cb5")
    assert components["diagnostics-test-block-duration"]["patches"][0]["source"]["commit"].startswith("f6e3c608")
    assert components["bootstrap-build-ref-pin"]["patches"][0]["source"]["commit"].startswith("adafd77")
    assert all(
        component.get("source", {}).get("ref")
        != "refs/heads/integrate/openai-native-compaction-rebased-20260809"
        for component in components.values()
    )


def test_windows_updater_provenance_is_ordered_and_final_identity_is_pending():
    component = components_by_id(load_manifest(DEFAULT_MANIFEST))["native-windows-updater-mcp"]

    assert [patch["source"]["commit"] for patch in component["patches"]] == [
        "79035c62fa624a9be8ef6214891a41e644c41dcc",
        "b6d6a0838f05a5a2f9d7fc350f006f0998705794",
        "43022558a040cf03e6f6f4761f5ac178eb737128",
        "e950fbb0fc9c7a0bc96c55e763b709d7b48437cf",
        "b028dd5d7f99a3a8e36b345a7796947f18bf77df",
        "a68f85c46c1ebdc94fb4b95feb7de9685b34b0b3",
        "e0573c650e8d995f9ad2527c276dfe0e2dddf7f5",
        "deedb3db7f0e962b4e79f4c27ab24eaa350bcf2c",
        "126528a77bda687013c274d8deaf9bcf5e5b97a5",
        "fb3ead63b596d5cd48c3c53d03e16503794b99f9",
        "48cf9e5266a68d6f46cd43969f538a43f39d2aba",
        "79959e421703fbead19f43d7b7edf3f50470dbb6",
        "28e28682e056952920b94574e5413a981031ebf2",
    ]
    assert component["upstream_status"] == "review_required"
    assert component["intended_upstream_status"] == "required"
    assert [patch["source"]["stable_patch_id"] for patch in component["patches"]] == [
        "6db9757f0d97335a2d020a2dabf02d31c7620a98",
        "50ee09f1e291897c88d2289c80b0f00383f0d4ac",
        "38fbf024b0be130d4645c5fdef2d9caf73c3ca7b",
        "38899fefccebae7010af17aed85ff844dd0234de",
        "eb3397df7e2f07bdc649f2814a73c94cd58b27c1",
        "e01de898a2208e61e79703c8067fe1fc94ef293f",
        "0cc51d321b466467bea1ac2748dcb7f6696b8106",
        "443aee472da1a3fbf9689935db493fd135aa9d59",
        "2f439b403ed30012bfda1395d42d620843f2683b",
        "bfa6138d92f63e83a80bf77e0d00cc0cf1351c98",
        "718a7fb88308a5e74af5b0a3344036778962fbda",
        "ecf4c06c7c4799dc48f0a72045919f49dc0f5dde",
        "0f651f8c1330f7958b04fe3ece9fe99c5498c28d",
    ]
    assert all(
        patch["disposition"] == "review_required"
        for patch in component["patches"][:10]
    )
    assert all(
        patch["integration"]
        == {"state": "pending", "commit": None, "stable_patch_id": None}
        for patch in component["patches"][:10]
    )
    assert all(
        patch["disposition"] == "superseded"
        and patch["integration"]
        == {"state": "not_replayed", "commit": None, "stable_patch_id": None}
        for patch in component["patches"][10:]
    )
    assert "apps/desktop/electron/handoff-exit.test.ts" not in component["tests"]


def test_required_patch_ledger_contains_only_required_dispositions():
    manifest = load_manifest(DEFAULT_MANIFEST)
    required_from_components = [
        patch["source"]["stable_patch_id"]
        for component in manifest["components"]
        for patch in component["patches"]
        if patch["disposition"] == "required"
    ]

    assert manifest["required_patch_ids"] == required_from_components
