"""Behavior contracts for native OpenAI compaction checkpoint primitives."""

from __future__ import annotations

import math

import pytest

from agent.native_openai_compaction import (
    NativeCompactionCheckpoint,
    NativeCompactionIdentity,
    apply_checkpoint,
    canonical_input_sha256,
    checkpoint_matches,
)

OPAQUE_OUTPUT = [
    {"type": "compaction", "encrypted_content": "opaque-A", "x_future": 1},
    {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "retained"}],
    },
    {"type": "compaction", "encrypted_content": "opaque-B"},
]


def _identity(**overrides) -> NativeCompactionIdentity:
    values = {
        "provider": "openai",
        "api_mode": "codex_responses",
        "model": "gpt-5",
        "base_url": "https://api.openai.com/v1",
        "issuer_kind": "api_key",
        "credential_scope": "account-1",
        "replay_encrypted_reasoning": True,
    }
    values.update(overrides)
    return NativeCompactionIdentity(**values)


def _checkpoint(
    ordinary_input: list[dict] | None = None, **overrides
) -> NativeCompactionCheckpoint:
    ordinary_input = ordinary_input or [{"role": "user", "content": "before"}]
    values = {
        "session_id": "session-1",
        "identity": _identity(),
        "source_input_item_count": len(ordinary_input),
        "source_input_sha256": canonical_input_sha256(ordinary_input),
        "output": OPAQUE_OUTPUT,
        "compact_response_id": "resp_123",
        "compact_created_at": 10.5,
        "input_item_count": len(ordinary_input),
        "output_item_count": len(OPAQUE_OUTPUT),
        "generation": 1,
        "created_at": 11.0,
        "updated_at": 12.0,
    }
    values.update(overrides)
    return NativeCompactionCheckpoint(**values)


def test_canonical_hash_is_stable_across_dictionary_key_order():
    left = [{"role": "user", "content": {"text": "hello", "kind": "input"}}]
    right = [{"content": {"kind": "input", "text": "hello"}, "role": "user"}]

    assert canonical_input_sha256(left) == canonical_input_sha256(right)


def test_canonical_hash_changes_when_prefix_content_changes():
    assert canonical_input_sha256([{"text": "one"}]) != canonical_input_sha256(
        [{"text": "two"}]
    )


def test_checkpoint_round_trip_preserves_complete_output_order():
    checkpoint = _checkpoint()

    assert checkpoint.output == OPAQUE_OUTPUT
    assert checkpoint.output_json


def test_prefix_validation_rejects_shorter_input():
    checkpoint = _checkpoint(
        [{"value": 1}, {"value": 2}], source_input_item_count=2
    )

    assert not checkpoint_matches(checkpoint.identity, checkpoint, [{"value": 1}])


def test_prefix_validation_rejects_hash_mismatch():
    checkpoint = _checkpoint([{"value": 1}])

    assert not checkpoint_matches(checkpoint.identity, checkpoint, [{"value": 2}])


def test_projection_returns_deep_copies_and_does_not_mutate_inputs():
    ordinary = [{"value": "prefix"}, {"nested": {"value": "tail"}}]
    checkpoint = _checkpoint(ordinary[:1])
    expected_ordinary = [{"value": "prefix"}, {"nested": {"value": "tail"}}]

    projected = apply_checkpoint(checkpoint, ordinary)
    projected[0]["encrypted_content"] = "changed"
    projected[-1]["nested"]["value"] = "changed"

    assert ordinary == expected_ordinary
    assert checkpoint.output == OPAQUE_OUTPUT


def test_identity_normalizes_all_route_fields_consistently():
    identity = NativeCompactionIdentity(
        provider=" OpenAI ",
        api_mode=" CODEX_RESPONSES ",
        model=" GPT-5 ",
        base_url=" HTTPS://API.OPENAI.COM/V1/ ",
        issuer_kind=" API_KEY ",
        credential_scope=" ACCOUNT-1 ",
        replay_encrypted_reasoning=1,
    )

    assert identity == _identity()


def test_identity_mismatch_disables_projection():
    ordinary = [{"value": 1}]
    checkpoint = _checkpoint(ordinary)

    assert not checkpoint_matches(_identity(model="gpt-5-mini"), checkpoint, ordinary)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_input_item_count", -1),
        ("input_item_count", -1),
        ("output_item_count", -1),
        ("generation", 0),
        ("created_at", -1.0),
        ("updated_at", -1.0),
        ("compact_created_at", -1.0),
    ],
)
def test_checkpoint_rejects_invalid_numeric_invariants(field, value):
    with pytest.raises(ValueError, match=field):
        _checkpoint(**{field: value})


@pytest.mark.parametrize("output", [[], {}, "not-json", [math.nan], [{"bad": object()}]])
def test_checkpoint_rejects_empty_non_list_or_non_json_output_without_payload_leak(output):
    with pytest.raises(ValueError) as exc_info:
        _checkpoint(output=output)

    message = str(exc_info.value)
    assert "opaque-A" not in message
    assert "not-json" not in message


def test_canonical_hash_rejects_non_json_and_nan_without_payload_leak():
    for items in ([{"private": object()}], [{"private": math.nan}]):
        with pytest.raises(ValueError) as exc_info:
            canonical_input_sha256(items)
        assert "private" not in str(exc_info.value)


def test_checkpoint_output_is_isolated_from_constructor_and_property_mutation():
    supplied = [{"nested": ["original"]}]
    checkpoint = _checkpoint(output=supplied, output_item_count=1)
    supplied[0]["nested"][0] = "constructor-mutated"
    fetched = checkpoint.output
    fetched[0]["nested"][0] = "property-mutated"

    assert checkpoint.output == [{"nested": ["original"]}]


def test_redacted_metadata_contains_only_safe_operational_fields():
    checkpoint = _checkpoint()

    metadata = checkpoint.redacted_metadata()

    assert metadata == {
        "provider": "openai",
        "api_mode": "codex_responses",
        "model": "gpt-5",
        "base_url": "https://api.openai.com/v1",
        "issuer_kind": "api_key",
        "credential_scope": "account-1",
        "replay_encrypted_reasoning": True,
        "source_input_item_count": 1,
        "source_input_sha256": checkpoint.source_input_sha256[:12],
        "input_item_count": 1,
        "output_item_count": 3,
        "generation": 1,
        "compact_response_id": "resp_123",
        "compact_created_at": 10.5,
        "created_at": 11.0,
        "updated_at": 12.0,
    }
    rendered = repr(metadata)
    assert "opaque-A" not in rendered
    assert "retained" not in rendered
    assert "output_json" not in rendered
