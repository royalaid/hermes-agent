"""Behavior contracts for native OpenAI compaction checkpoint primitives."""

from __future__ import annotations

import copy
import math
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor
from agent.native_openai_compaction import (
    NativeCompactionCandidate,
    NativeCompactionCheckpoint,
    NativeCompactionCut,
    NativeCompactionFailure,
    NativeCompactionIdentity,
    apply_checkpoint,
    canonical_input_sha256,
    checkpoint_matches,
    request_native_compaction_candidate,
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


class _FakeCompactClient:
    def __init__(self, compact=object()):
        self.responses = SimpleNamespace(compact=compact)


class _CheckpointCapableDB:
    def load_native_openai_checkpoint(self):
        pass

    def upsert_native_openai_checkpoint(self):
        pass

    def delete_native_openai_checkpoint(self):
        pass


_CHECKPOINT_DB = _CheckpointCapableDB()


def _policy(
    *,
    enabled=True,
    builtin=True,
    session_db=_CHECKPOINT_DB,
    session_id="session-1",
    session_state_bound=True,
):
    from agent.native_openai_compaction import NativeCompactionPolicy

    compressor = object.__new__(ContextCompressor) if builtin else object()
    return NativeCompactionPolicy.from_runtime(
        feature_enabled=enabled,
        context_compressor=compressor,
        session_db=session_db,
        session_id=session_id,
        session_state_bound=session_state_bound,
    )


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("openai", "https://api.openai.com/v1"),
        (" OPENAI ", "HTTPS://API.OPENAI.COM:443/v1/"),
        ("openai-codex", "https://chatgpt.com/backend-api/codex"),
        (" OPENAI-CODEX ", "HTTPS://CHATGPT.COM:443/backend-api/codex/"),
    ],
)
def test_policy_allows_only_the_two_first_party_responses_routes(provider, base_url):
    assert _policy().is_eligible(
        client=_FakeCompactClient(compact=lambda **_: None),
        provider=provider,
        api_mode=" CODEX_RESPONSES ",
        base_url=base_url,
    )


@pytest.mark.parametrize(
    ("provider", "api_mode", "base_url"),
    [
        ("openai", "chat_completions", "https://api.openai.com/v1"),
        ("openai", "codex_app_server", "https://api.openai.com/v1"),
        ("openai", "codex_responses", "https://example.com/v1"),
        ("openai", "codex_responses", "https://api.openai.com.evil.test/v1"),
        ("openai", "codex_responses", "https://api.openai.com/v1/extra"),
        ("openai", "codex_responses", "https://api.openai.com/v1?token=secret"),
        ("openai", "codex_responses", "https://user:pass@api.openai.com/v1"),
        ("openai-codex", "codex_responses", "https://chatgpt.com.evil.test/backend-api/codex"),
        ("openai-codex", "codex_responses", "https://chatgpt.com/backend-api/codex?token=secret"),
        ("azure", "codex_responses", "https://example.openai.azure.com/openai/v1"),
        ("xai", "codex_responses", "https://api.x.ai/v1"),
        ("custom", "codex_responses", "https://api.openai.com/v1"),
        ("openai-codex", "codex_responses", "https://api.openai.com/v1"),
        ("openai", "codex_responses", "https://chatgpt.com/backend-api/codex"),
    ],
)
def test_policy_rejects_non_allowlisted_effective_routes(provider, api_mode, base_url):
    assert not _policy().is_eligible(
        client=_FakeCompactClient(compact=lambda **_: None),
        provider=provider,
        api_mode=api_mode,
        base_url=base_url,
    )


@pytest.mark.parametrize(
    "policy",
    [
        _policy(enabled=False),
        _policy(builtin=False),
        _policy(session_db=None),
        _policy(session_id=""),
        _policy(session_id=None),
    ],
)
def test_policy_rejects_disabled_custom_engine_or_missing_session_state(policy):
    assert not policy.is_eligible(
        client=_FakeCompactClient(compact=lambda **_: None),
        provider="openai",
        api_mode="codex_responses",
        base_url="https://api.openai.com/v1",
    )


def test_policy_rejects_context_compressor_subclass():
    class CustomCompressor(ContextCompressor):
        pass

    from agent.native_openai_compaction import NativeCompactionPolicy

    policy = NativeCompactionPolicy.from_runtime(
        feature_enabled=True,
        context_compressor=object.__new__(CustomCompressor),
        session_db=_CHECKPOINT_DB,
        session_id="session-1",
        session_state_bound=True,
    )

    assert not policy.built_in_compressor
    assert not policy.is_eligible(
        client=_FakeCompactClient(compact=lambda **_: None),
        provider="openai",
        api_mode="codex_responses",
        base_url="https://api.openai.com/v1",
    )


class _RaisingRouteValue:
    def __str__(self):
        raise RuntimeError("must fail closed")

    def __bool__(self):
        raise RuntimeError("must fail closed")


class _SpoofedRouteValue:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value


@pytest.mark.parametrize("field", ["provider", "api_mode", "base_url"])
@pytest.mark.parametrize("raising", [False, True])
def test_policy_rejects_non_plain_string_route_fields_without_raising(field, raising):
    route = {
        "provider": "openai",
        "api_mode": "codex_responses",
        "base_url": "https://api.openai.com/v1",
    }
    route[field] = (
        _RaisingRouteValue() if raising else _SpoofedRouteValue(route[field])
    )

    assert not _policy().is_eligible(
        client=_FakeCompactClient(compact=lambda **_: None),
        **route,
    )


class _RaisingSessionDB:
    def __getattr__(self, name):
        if name.endswith("_native_openai_checkpoint"):
            raise RuntimeError(f"cannot inspect {name}")
        raise AttributeError(name)


@pytest.mark.parametrize(
    "session_db",
    [
        object(),
        SimpleNamespace(load_native_openai_checkpoint=lambda: None),
        _RaisingSessionDB(),
    ],
)
def test_policy_requires_all_checkpoint_persistence_capabilities(session_db):
    assert not _policy(session_db=session_db).has_session_state


def test_policy_requires_successful_session_state_binding():
    assert not _policy(session_state_bound=False).has_session_state


class _RaisingSessionID(str):
    def strip(self, *args, **kwargs):
        raise RuntimeError("must fail closed")


def test_policy_rejects_malformed_session_id_without_raising():
    assert not _policy(session_id=_RaisingSessionID("session-1")).has_session_state


def test_policy_stores_booleans_only():
    policy = _policy()

    assert vars(policy) == {
        "feature_enabled": True,
        "built_in_compressor": True,
        "has_session_state": True,
    }
    assert all(type(value) is bool for value in vars(policy).values())


@pytest.mark.parametrize(
    "client",
    [
        None,
        object(),
        SimpleNamespace(responses=object()),
        _FakeCompactClient(compact=None),
        _FakeCompactClient(compact="not-callable"),
    ],
)
def test_policy_rejects_client_without_callable_responses_compact(client):
    assert not _policy().is_eligible(
        client=client,
        provider="openai",
        api_mode="codex_responses",
        base_url="https://api.openai.com/v1",
    )


class _RaisingResponsesClient:
    @property
    def responses(self):
        raise RuntimeError("must fail closed")


class _RaisingCompactResponses:
    @property
    def compact(self):
        raise RuntimeError("must fail closed")


@pytest.mark.parametrize(
    "client",
    [_RaisingResponsesClient(), SimpleNamespace(responses=_RaisingCompactResponses())],
)
def test_policy_rejects_raising_client_properties_without_raising(client):
    assert not _policy().is_eligible(
        client=client,
        provider="openai",
        api_mode="codex_responses",
        base_url="https://api.openai.com/v1",
    )


def test_policy_uses_current_effective_route_state_on_every_check():
    policy = _policy()
    client = _FakeCompactClient(compact=lambda **_: None)

    assert policy.is_eligible(
        client=client,
        provider="openai",
        api_mode="codex_responses",
        base_url="https://api.openai.com/v1",
    )
    assert not policy.is_eligible(
        client=client,
        provider="xai",
        api_mode="codex_responses",
        base_url="https://api.x.ai/v1",
    )


def test_policy_repr_and_error_paths_do_not_retain_or_expose_secrets():
    sentinels = (
        "fake-api-key-sentinel",
        "credential-scope-sentinel",
        "url-user-sentinel",
        "url-password-sentinel",
        "url-query-sentinel",
        "url-fragment-sentinel",
    )
    secret_url = (
        f"https://{sentinels[2]}:{sentinels[3]}@api.openai.com/v1"
        f"?token={sentinels[4]}#{sentinels[5]}"
    )
    client = _FakeCompactClient(compact=lambda **_: None)
    client.api_key = sentinels[0]
    client.credential_scope = sentinels[1]
    policy = _policy()

    rendered = repr(policy)
    try:
        assert not policy.is_eligible(
            client=client,
            provider="openai",
            api_mode="codex_responses",
            base_url=secret_url,
        )
    except Exception as exc:  # pragma: no cover - fail-closed must not raise
        rendered += repr(exc)

    assert all(sentinel not in rendered for sentinel in sentinels)
    assert "_FakeCompactClient" not in rendered


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


@pytest.mark.parametrize(
    "items",
    [
        [{"nested-secret-key": ["safe", ("tuple-secret-value",)]}],
        [{7: "non-string-key-secret-value"}],
    ],
)
def test_canonical_hash_rejects_values_outside_strict_json_domain_without_payload_leak(
    items,
):
    with pytest.raises(ValueError) as exc_info:
        canonical_input_sha256(items)

    message = str(exc_info.value)
    assert "nested-secret-key" not in message
    assert "tuple-secret-value" not in message
    assert "non-string-key-secret-value" not in message


@pytest.mark.parametrize(
    "output",
    [
        [{"nested-secret-key": ["safe", ("tuple-secret-value",)]}],
        [{7: "non-string-key-secret-value"}],
    ],
)
def test_checkpoint_rejects_output_outside_strict_json_domain_without_payload_leak(
    output,
):
    with pytest.raises(ValueError) as exc_info:
        _checkpoint(output=output, output_item_count=1)

    message = str(exc_info.value)
    assert "nested-secret-key" not in message
    assert "tuple-secret-value" not in message
    assert "non-string-key-secret-value" not in message


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
def test_checkpoint_rejects_none_for_required_timestamps(field):
    with pytest.raises(ValueError, match=field):
        _checkpoint(**{field: None})


def test_checkpoint_accepts_none_for_optional_compact_created_at():
    checkpoint = _checkpoint(compact_created_at=None)

    assert checkpoint.compact_created_at is None


def test_checkpoint_output_is_isolated_from_constructor_and_property_mutation():
    supplied = [{"nested": ["original"]}]
    checkpoint = _checkpoint(output=supplied, output_item_count=1)
    supplied[0]["nested"][0] = "constructor-mutated"
    fetched = checkpoint.output
    fetched[0]["nested"][0] = "property-mutated"

    assert checkpoint.output == [{"nested": ["original"]}]


def test_identity_and_checkpoint_reprs_exclude_route_and_credential_secrets():
    sentinels = (
        "repr-user-sentinel",
        "repr-password-sentinel",
        "repr-path-sentinel",
        "repr-query-sentinel",
        "repr-fragment-sentinel",
        "repr-credential-scope-sentinel",
    )
    base_url = (
        f"https://{sentinels[0]}:{sentinels[1]}@api.example.com/{sentinels[2]}"
        f"?token={sentinels[3]}#{sentinels[4]}"
    )
    identity = _identity(base_url=base_url, credential_scope=sentinels[5])
    checkpoint = _checkpoint(identity=identity)

    for rendered in (repr(identity), repr(checkpoint)):
        assert base_url not in rendered
        assert all(sentinel not in rendered for sentinel in sentinels)
        assert "provider='openai'" in rendered
        assert "model='gpt-5'" in rendered


def test_redacted_metadata_contains_only_safe_operational_fields():
    checkpoint = _checkpoint()

    metadata = checkpoint.redacted_metadata(tail_item_count=2, elapsed_ms=7)

    assert metadata == {
        "strategy": "openai_native",
        "provider": "openai",
        "api_mode": "codex_responses",
        "model": "gpt-5",
        "base_url_host": "api.openai.com",
        "input_item_count": 1,
        "output_item_count": 3,
        "tail_item_count": 2,
        "generation": 1,
        "prefix_sha256": checkpoint.source_input_sha256[:12],
        "elapsed_ms": 7,
    }
    rendered = repr(metadata)
    assert "opaque-A" not in rendered
    assert "retained" not in rendered
    assert "output_json" not in rendered


def test_redacted_metadata_never_exposes_arbitrary_credential_scope_values():
    credential_scope = "credential-scope-raw-secret-sentinel"
    checkpoint = _checkpoint(identity=_identity(credential_scope=credential_scope))

    metadata = checkpoint.redacted_metadata()

    assert "credential_scope" not in metadata
    assert credential_scope not in repr(metadata)


def test_redacted_metadata_excludes_base_url_credentials_and_route_details():
    base_url = (
        "https://route-user:route-password@api.example.com/v1/compact"
        "?api_key=query-secret#private-fragment"
    )
    checkpoint = _checkpoint(identity=_identity(base_url=base_url))

    metadata = checkpoint.redacted_metadata()

    assert checkpoint.identity.base_url == base_url
    assert metadata["base_url_host"] == "api.example.com"
    rendered = repr(metadata)
    for secret in (
        "route-user",
        "route-password",
        "query-secret",
        "private-fragment",
        "/v1/compact",
    ):
        assert secret not in rendered


def _serialize_rows(messages):
    return [
        {"role": message["role"], "content": copy.deepcopy(message.get("content", ""))}
        for message in messages
        if isinstance(message, dict) and message.get("role") != "system"
    ]


def test_cut_retains_newest_real_user_turn_despite_newer_synthetic_scaffolding():
    from agent.native_openai_compaction import select_native_compaction_cut

    messages = [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "newest human request"},
        {"role": "assistant", "content": "working"},
        {
            "role": "user",
            "content": "synthetic runtime note",
            "_todo_snapshot_synthetic": True,
        },
        {"role": "assistant", "content": "still working"},
    ]

    cut = select_native_compaction_cut(
        messages, protect_last_n=1, serialize_input=_serialize_rows
    )

    assert cut is not None
    assert cut.message_count == 2
    assert cut.source_input == _serialize_rows(messages[:2])


def test_cut_keeps_multiple_tool_calls_and_results_atomic():
    from agent.native_openai_compaction import select_native_compaction_cut
    from agent.transports.codex import ResponsesApiTransport

    messages = [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "run both"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_a", "function": {"name": "a", "arguments": "{}"}},
                {"id": "call_b", "function": {"name": "b", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_a", "content": "A"},
        {"role": "tool", "tool_call_id": "call_b", "content": "B"},
        {"role": "assistant", "content": "both done"},
        {"role": "user", "content": "next request"},
    ]

    transport = ResponsesApiTransport()
    cut = select_native_compaction_cut(
        messages,
        protect_last_n=3,
        serialize_input=lambda rows: transport.build_input_items(
            rows, is_codex_backend=True
        ),
    )

    assert cut is not None
    assert cut.message_count == 3
    assert messages[cut.message_count]["role"] == "assistant"


def test_cut_refuses_malformed_detached_tool_history_when_no_safe_prefix_exists():
    from agent.native_openai_compaction import select_native_compaction_cut

    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "broken", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "unknown", "content": "secret-result"},
        {"role": "user", "content": "real request"},
        {"role": "assistant", "content": "answer"},
    ]

    assert (
        select_native_compaction_cut(
            messages, protect_last_n=1, serialize_input=_serialize_rows
        )
        is None
    )


@pytest.mark.parametrize(
    "function",
    [None, {}, "not-an-object", {"name": ""}, {"name": "   "}, {"name": 7}],
)
def test_cut_refuses_raw_tool_call_with_invalid_function_name(function):
    from agent.native_openai_compaction import select_native_compaction_cut
    from agent.transports.codex import ResponsesApiTransport

    messages = [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "function": function}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "private result"},
        {"role": "user", "content": "new request"},
        {"role": "assistant", "content": "new answer"},
    ]
    transport = ResponsesApiTransport()

    assert (
        select_native_compaction_cut(
            messages,
            protect_last_n=1,
            serialize_input=lambda rows: transport.build_input_items(
                rows, is_codex_backend=True
            ),
        )
        is None
    )


def test_cut_refuses_finalized_call_id_collision_across_completed_groups():
    from agent.native_openai_compaction import select_native_compaction_cut
    from agent.transports.codex import ResponsesApiTransport

    messages = [
        {"role": "user", "content": "first request"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_reused", "function": {"name": "a", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_reused", "content": "first"},
        {"role": "assistant", "content": "first done"},
        {"role": "user", "content": "second request"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": " call_reused ", "function": {"name": "b", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_reused", "content": "second"},
        {"role": "assistant", "content": "second done"},
        {"role": "user", "content": "newest request"},
        {"role": "assistant", "content": "newest answer"},
    ]
    transport = ResponsesApiTransport()

    assert (
        select_native_compaction_cut(
            messages,
            protect_last_n=1,
            serialize_input=lambda rows: transport.build_input_items(
                rows, is_codex_backend=True
            ),
        )
        is None
    )


def _select_cut_for_serialized_tool_graph(graph):
    from agent.native_openai_compaction import select_native_compaction_cut

    messages = [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new request"},
        {"role": "assistant", "content": "new answer"},
    ]

    def serialize(rows):
        items = copy.deepcopy(graph)
        if len(rows) > 2:
            items.append({"role": "user", "content": "tail"})
        return items

    return select_native_compaction_cut(
        messages, protect_last_n=1, serialize_input=serialize
    )


def test_cut_accepts_finalized_graph_with_additional_calls_while_calls_are_pending():
    graph = [
        {"type": "function_call", "call_id": "call_a", "name": "a", "arguments": "{}"},
        {"type": "function_call", "call_id": "call_b", "name": "b", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_b", "output": "B"},
        {"type": "function_call_output", "call_id": "call_a", "output": "A"},
    ]

    assert _select_cut_for_serialized_tool_graph(graph) is not None


def test_cut_accepts_finalized_no_tool_item_shapes():
    graph = [
        {"role": "user", "content": "question"},
        {"type": "reasoning", "encrypted_content": "opaque", "summary": []},
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "answer"}],
        },
        {"role": "assistant", "content": "answer"},
    ]

    assert _select_cut_for_serialized_tool_graph(graph) is not None


@pytest.mark.parametrize(
    "interleaved_item",
    [
        {"type": "future_item", "private": "unknown-payload"},
        {"role": "tool", "tool_call_id": "call_a", "content": "raw-result"},
    ],
)
def test_cut_refuses_unknown_or_raw_tool_item_while_finalized_call_is_pending(
    interleaved_item,
):
    graph = [
        {"type": "function_call", "call_id": "call_a", "name": "a", "arguments": "{}"},
        interleaved_item,
        {"type": "function_call_output", "call_id": "call_a", "output": "A"},
    ]

    assert _select_cut_for_serialized_tool_graph(graph) is None


@pytest.mark.parametrize("name", [None, "", "   ", 7])
def test_cut_refuses_finalized_function_call_with_invalid_name(name):
    graph = [
        {"type": "function_call", "call_id": "call_a", "name": name, "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_a", "output": "A"},
    ]

    assert _select_cut_for_serialized_tool_graph(graph) is None


@pytest.mark.parametrize(
    "unknown_item",
    [
        {"type": "future_item", "private": "unknown-payload"},
        {"role": "tool", "tool_call_id": "call_a", "content": "raw-result"},
        {"type": "message", "role": "user", "content": []},
        {},
    ],
)
def test_cut_refuses_unknown_finalized_item_when_no_call_is_pending(unknown_item):
    graph = [
        {"role": "user", "content": "before"},
        unknown_item,
        {"role": "assistant", "content": "after"},
    ]

    assert _select_cut_for_serialized_tool_graph(graph) is None


@pytest.mark.parametrize(
    "graph",
    [
        [{"type": "function_call_output", "call_id": "unknown", "output": "x"}],
        [
            {"type": "function_call", "call_id": "call_1", "name": "a", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "x"},
            {"type": "function_call_output", "call_id": "call_1", "output": "duplicate"},
        ],
        [
            {"type": "function_call", "call_id": "call_1", "name": "a", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "x"},
            {"type": "function_call", "call_id": "call_1", "name": "b", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "y"},
        ],
        [
            {"type": "function_call", "call_id": "call_1", "name": "a", "arguments": "{}"},
            {"role": "assistant", "content": "interleaved"},
            {"type": "function_call_output", "call_id": "call_1", "output": "x"},
        ],
        [{"type": "function_call", "call_id": "call_1", "name": "a", "arguments": "{}"}],
    ],
)
def test_cut_refuses_invalid_finalized_tool_graph(graph):
    assert _select_cut_for_serialized_tool_graph(graph) is None


def test_input_override_cannot_form_a_strict_ordinary_prefix_cut():
    from agent.native_openai_compaction import select_native_compaction_cut
    from agent.transports.codex import ResponsesApiTransport

    messages = [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new request"},
        {"role": "assistant", "content": "new answer"},
    ]
    override = {"input": [{"role": "user", "content": "fixed override"}]}
    transport = ResponsesApiTransport()

    assert (
        select_native_compaction_cut(
            messages,
            protect_last_n=1,
            serialize_input=lambda rows: transport.build_input_items(
                rows,
                is_codex_backend=True,
                request_overrides=override,
            ),
        )
        is None
    )


def test_cut_refuses_malformed_tool_group_in_retained_tail():
    from agent.native_openai_compaction import select_native_compaction_cut

    messages = [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new request"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "function": {"name": "terminal", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "wrong", "content": "result"},
    ]

    assert (
        select_native_compaction_cut(
            messages, protect_last_n=2, serialize_input=_serialize_rows
        )
        is None
    )


@pytest.mark.parametrize(
    "tool_calls",
    [{"id": "call_1"}, "call_1", "", 1, ("call_1",)],
)
def test_cut_refuses_non_list_assistant_tool_calls_in_retained_tail(tool_calls):
    from agent.native_openai_compaction import select_native_compaction_cut

    messages = [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new request"},
        {"role": "assistant", "content": "", "tool_calls": tool_calls},
    ]

    assert (
        select_native_compaction_cut(
            messages, protect_last_n=1, serialize_input=_serialize_rows
        )
        is None
    )


@pytest.mark.parametrize(
    "messages,protect_last_n",
    [
        ([{"role": "user", "content": "only turn"}], 1),
        (
            [
                {"role": "system", "content": "instructions"},
                {"role": "user", "content": "request"},
                {"role": "assistant", "content": "answer"},
            ],
            1,
        ),
    ],
)
def test_cut_refuses_too_short_or_no_item_prefix(messages, protect_last_n):
    from agent.native_openai_compaction import select_native_compaction_cut

    assert (
        select_native_compaction_cut(
            messages,
            protect_last_n=protect_last_n,
            serialize_input=_serialize_rows,
        )
        is None
    )


def test_repeated_cut_must_extend_strictly_beyond_previous_source_boundary():
    from agent.native_openai_compaction import select_native_compaction_cut

    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "one answer"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "two answer"},
        {"role": "user", "content": "three"},
    ]
    first = select_native_compaction_cut(
        messages, protect_last_n=1, serialize_input=_serialize_rows
    )

    assert first is not None
    assert (
        select_native_compaction_cut(
            messages,
            protect_last_n=1,
            serialize_input=_serialize_rows,
            previous_source_input_item_count=first.source_input_item_count,
        )
        is None
    )

    extended_messages = messages + [
        {"role": "assistant", "content": "three answer"},
        {"role": "user", "content": "four"},
        {"role": "assistant", "content": "four answer"},
    ]
    extended = select_native_compaction_cut(
        extended_messages,
        protect_last_n=1,
        serialize_input=_serialize_rows,
        previous_source_input_item_count=first.source_input_item_count,
    )

    assert extended is not None
    assert extended.source_input_item_count > first.source_input_item_count


def test_cut_source_is_exact_ordinary_prefix_hashed_and_deep_copy_safe():
    from agent.native_openai_compaction import select_native_compaction_cut
    from agent.transports.codex import ResponsesApiTransport

    transport = ResponsesApiTransport()
    serialize = lambda rows: transport.build_input_items(  # noqa: E731
        rows, is_codex_backend=True
    )
    messages = [
        {"role": "user", "content": "old"},
        {
            "role": "assistant",
            "content": "answer",
            "codex_reasoning_items": [
                {"type": "reasoning", "encrypted_content": "opaque"}
            ],
        },
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "new answer"},
    ]
    ordinary = serialize(messages)

    cut = select_native_compaction_cut(
        messages, protect_last_n=1, serialize_input=serialize
    )

    assert cut is not None
    assert cut.source_input == ordinary[: cut.source_input_item_count]
    assert cut.source_input_item_count == len(cut.source_input)
    assert cut.source_input_sha256 == canonical_input_sha256(cut.source_input)
    fetched = cut.source_input
    fetched[0]["content"] = "caller mutation"
    assert cut.source_input == ordinary[: cut.source_input_item_count]


def test_cut_repr_and_invalid_payload_errors_never_expose_payloads():
    from agent.native_openai_compaction import (
        NativeCompactionCut,
        select_native_compaction_cut,
    )

    sentinel = "private-transcript-tool-args-encrypted-sentinel"
    cut = NativeCompactionCut(
        message_count=1,
        source_input=[{"type": "reasoning", "encrypted_content": sentinel}],
        source_input_item_count=1,
        source_input_sha256=canonical_input_sha256(
            [{"type": "reasoning", "encrypted_content": sentinel}]
        ),
    )
    assert sentinel not in repr(cut)
    assert "source_input=" not in repr(cut)

    def invalid_serializer(_messages):
        return [{"arguments": {sentinel: object()}}]

    rendered = repr(
        select_native_compaction_cut(
            [
                {"role": "user", "content": "old"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "new"},
            ],
            protect_last_n=1,
            serialize_input=invalid_serializer,
        )
    )
    assert sentinel not in rendered
    assert rendered == "None"


@pytest.mark.parametrize(
    "bad_row",
    [
        {"role": "developer", "content": "hidden developer instruction"},
        {"role": "unknown", "content": "unknown role payload"},
        {"content": "missing role payload"},
        {"role": 7, "content": "non-string role payload"},
    ],
)
def test_cut_refuses_raw_rows_with_unserializable_roles_before_serialization(bad_row):
    from agent.native_openai_compaction import select_native_compaction_cut
    from agent.transports.codex import ResponsesApiTransport

    messages = [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        bad_row,
        {"role": "user", "content": "new request"},
        {"role": "assistant", "content": "new answer"},
    ]
    transport = ResponsesApiTransport()
    calls = 0

    def serialize(rows):
        nonlocal calls
        calls += 1
        return transport.build_input_items(rows, is_codex_backend=True)

    assert select_native_compaction_cut(messages, protect_last_n=1, serialize_input=serialize) is None
    assert calls == 0


def test_cut_refuses_non_plain_raw_row_and_role():
    from agent.native_openai_compaction import select_native_compaction_cut

    class Row(dict):
        pass

    class Role(str):
        pass

    for bad_row in (
        Row(role="assistant", content="not a plain row"),
        {"role": Role("assistant"), "content": "not a plain role"},
    ):
        messages = [
            {"role": "user", "content": "old"},
            bad_row,
            {"role": "user", "content": "new"},
        ]
        assert select_native_compaction_cut(
            messages, protect_last_n=1, serialize_input=_serialize_rows
        ) is None


@pytest.mark.parametrize("api_content", [{"private": "object"}, [], 7, False])
def test_cut_refuses_raw_non_string_api_content(api_content):
    from agent.native_openai_compaction import select_native_compaction_cut

    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "answer", "api_content": api_content},
        {"role": "user", "content": "new"},
    ]
    assert select_native_compaction_cut(
        messages, protect_last_n=1, serialize_input=_serialize_rows
    ) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("codex_reasoning_items", "not-a-list"),
        ("codex_reasoning_items", {}),
        ("codex_reasoning_items", ()),
        ("codex_message_items", "not-a-list"),
        ("codex_message_items", {}),
        ("codex_message_items", ()),
    ],
)
def test_cut_refuses_raw_non_list_wire_sidecar_containers(field, value):
    from agent.native_openai_compaction import select_native_compaction_cut

    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "tail", field: value},
    ]
    assert select_native_compaction_cut(
        messages, protect_last_n=1, serialize_input=_serialize_rows
    ) is None


@pytest.mark.parametrize(
    "graph",
    [
        [
            {"type": "function_call", "call_id": "call_1", "name": "tool", "arguments": {}},
            {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
        ],
        [
            {"type": "function_call", "call_id": "call_1", "name": "tool", "arguments": "{}", "extra": "x"},
            {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
        ],
        [
            {"type": "function_call", "call_id": "call_1", "name": "tool", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": {"private": "object"}},
        ],
        [
            {"type": "function_call", "call_id": "call_1", "name": "tool", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "ok", "extra": "x"},
        ],
        [
            {"type": "function_call", "call_id": "call_1", "name": "tool", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": [{"type": "input_text", "text": {"private": "object"}}]},
        ],
        [
            {"type": "function_call", "call_id": "call_1", "name": "tool", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": [{"type": "input_image", "image_url": ""}]},
        ],
        [{"type": "reasoning", "encrypted_content": {"private": "object"}, "summary": []}],
        [{"type": "reasoning", "encrypted_content": "opaque", "summary": {}}],
        [{"type": "reasoning", "encrypted_content": "opaque", "summary": [{"type": "summary_text", "text": "ok", "extra": "x"}]}],
        [{"type": "reasoning", "encrypted_content": "opaque", "summary": [], "extra": "x"}],
        [{"type": "message", "role": "assistant", "status": "completed", "content": {"private": "object"}}],
        [{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "ok", "extra": "x"}]}],
        [{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "ok"}], "extra": "x"}],
        [{"role": "user", "content": {"private": "object"}}],
        [{"role": "user", "content": [{"type": "output_text", "text": "wrong direction"}]}],
        [{"role": "assistant", "content": [{"type": "input_text", "text": "wrong direction"}]}],
        [{"role": "user", "content": [{"type": "input_image", "image_url": "", "detail": "auto"}]}],
        [{"role": "user", "content": "ok", "extra": "x"}],
    ],
)
def test_cut_refuses_malformed_exact_finalized_responses_schemas(graph):
    assert _select_cut_for_serialized_tool_graph(graph) is None


def test_cut_refuses_non_plain_finalized_item_and_nested_part():
    class Item(dict):
        pass

    assert _select_cut_for_serialized_tool_graph(
        [Item(role="user", content="not plain")]
    ) is None
    assert _select_cut_for_serialized_tool_graph(
        [{"role": "user", "content": [Item(type="input_text", text="not plain")]}]
    ) is None


def test_cut_accepts_all_exact_finalized_responses_schemas():
    graph = [
        {"role": "user", "content": [
            {"type": "input_text", "text": "question"},
            {"type": "input_image", "image_url": "https://example.test/image.png", "detail": "auto"},
        ]},
        {"type": "reasoning", "encrypted_content": "opaque", "summary": [
            {"type": "summary_text", "text": "summary"}
        ]},
        {"type": "message", "role": "assistant", "status": "completed", "id": "msg_1", "phase": "final_answer", "content": [
            {"type": "output_text", "text": "calling"}
        ]},
        {"type": "function_call", "call_id": "call_a", "name": "a", "arguments": "{}"},
        {"type": "function_call", "call_id": "call_b", "name": "b", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_b", "output": [
            {"type": "input_text", "text": "B"},
            {"type": "input_image", "image_url": "https://example.test/b.png"},
        ]},
        {"type": "function_call_output", "call_id": "call_a", "output": "A"},
        {"role": "assistant", "content": [
            {"type": "output_text", "text": "done"},
            {"type": "input_image", "image_url": "https://example.test/result.png", "detail": "low"},
        ]},
    ]

    assert _select_cut_for_serialized_tool_graph(graph) is not None



class _RequestAgent:
    def __init__(self, client=None, *, create_error=None, close_error=None):
        self.client = client
        self.create_error = create_error
        self.close_error = close_error
        self.create_calls = []
        self.close_calls = []

    def _create_request_openai_client(self, *, reason, api_kwargs):
        self.create_calls.append((reason, copy.deepcopy(api_kwargs)))
        if self.create_error is not None:
            raise self.create_error
        return self.client

    def _close_request_openai_client(self, client, *, reason):
        self.close_calls.append((client, reason))
        if self.close_error is not None:
            raise self.close_error


class _CompactRecorder:
    def __init__(self, response=None, *, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if self.error is not None:
            raise self.error
        return self.response


def _request_client(compact):
    return SimpleNamespace(responses=SimpleNamespace(compact=compact))


def _cut_for(items):
    return NativeCompactionCut(
        message_count=1,
        source_input=items,
        source_input_item_count=len(items),
        source_input_sha256=canonical_input_sha256(items),
    )


def _request(agent, cut, **overrides):
    values = {
        "model": "gpt-5",
        "cut": cut,
        "compact_instructions": "compact safely",
        "resolved_timeout": 12.5,
    }
    values.update(overrides)
    return request_native_compaction_candidate(agent, **values)


def test_request_calls_compact_with_exact_payload_and_request_client_lifecycle():
    source = [{"role": "user", "content": {"private": "source"}}]
    response = SimpleNamespace(
        output=[{"type": "compaction", "encrypted_content": "opaque"}],
        id="resp_1",
        created_at=42.5,
    )
    compact = _CompactRecorder(response)
    client = _request_client(compact)
    agent = _RequestAgent(client)

    result = _request(agent, _cut_for(source))

    assert isinstance(result, NativeCompactionCandidate)
    assert compact.calls == [{
        "model": "gpt-5", "input": source,
        "instructions": "compact safely", "timeout": 12.5,
    }]
    assert "previous_response_id" not in compact.calls[0]
    assert agent.create_calls == [("native_openai_compaction", {"model": "gpt-5"})]
    assert agent.close_calls == [(client, "native_openai_compaction")]
    assert vars(result) | {} == vars(result)  # frozen value object remains inspectable
    assert (result.source_input_item_count, result.input_item_count, result.output_item_count) == (1, 1, 1)
    assert result.compact_response_id == "resp_1"
    assert result.compact_created_at == 42.5


def test_predispatch_guard_runs_after_client_creation_and_before_compact():
    compact = _CompactRecorder(
        SimpleNamespace(output=[{"type": "compaction", "encrypted_content": "opaque"}])
    )
    client = _request_client(compact)
    agent = _RequestAgent(client)
    guard_calls = []

    result = _request(
        agent,
        _cut_for([{"role": "user", "content": "question"}]),
        pre_dispatch_check=lambda: guard_calls.append("checked") or False,
    )

    assert result == NativeCompactionFailure("client", False, True)
    assert guard_calls == ["checked"]
    assert compact.calls == []
    assert len(agent.create_calls) == 1
    assert agent.close_calls == [(client, "native_openai_compaction")]


def test_predispatch_guard_runs_after_request_payload_copy():
    source = [{"role": "user", "content": "question"}]
    compact = _CompactRecorder(
        SimpleNamespace(output=[{"type": "compaction", "encrypted_content": "opaque"}])
    )
    client = _request_client(compact)
    agent = _RequestAgent(client)
    dispatch_allowed = True
    real_deepcopy = copy.deepcopy

    def _copy_then_cancel(value):
        nonlocal dispatch_allowed
        copied = real_deepcopy(value)
        if value == source:
            dispatch_allowed = False
        return copied

    with patch(
        "agent.native_openai_compaction.copy.deepcopy",
        side_effect=_copy_then_cancel,
    ):
        result = _request(
            agent,
            _cut_for(source),
            pre_dispatch_check=lambda: dispatch_allowed,
        )

    assert result == NativeCompactionFailure("client", False, True)
    assert compact.calls == []
    assert agent.close_calls == [(client, "native_openai_compaction")]


def test_atomic_dispatch_fence_denies_compact_when_hard_cancel_wins_after_precheck():
    from agent.conversation_compression import CompressionCommitFence

    compact = _CompactRecorder(
        SimpleNamespace(
            output=[{"type": "compaction", "encrypted_content": "opaque"}]
        )
    )
    client = _request_client(compact)
    agent = _RequestAgent(client)
    fence = CompressionCommitFence()
    hard_cancel = threading.Event()

    def _check_then_cancel():
        assert fence.cancel_before_commit(hard_cancel) is True
        return True

    result = _request(
        agent,
        _cut_for([{"role": "user", "content": "question"}]),
        pre_dispatch_check=_check_then_cancel,
        dispatch_fence=fence,
        hard_cancel_event=hard_cancel,
    )

    assert result == NativeCompactionFailure("client", False, True)
    assert compact.calls == []
    assert agent.close_calls == [(client, "native_openai_compaction")]


def test_atomic_dispatch_fence_rechecks_lease_after_admission_before_compact():
    from agent.conversation_compression import CompressionCommitFence

    compact = _CompactRecorder(
        SimpleNamespace(
            output=[{"type": "compaction", "encrypted_content": "opaque"}]
        )
    )
    client = _request_client(compact)
    agent = _RequestAgent(client)
    checks = iter((True, False))

    result = _request(
        agent,
        _cut_for([{"role": "user", "content": "question"}]),
        pre_dispatch_check=lambda: next(checks),
        dispatch_fence=CompressionCommitFence(),
        hard_cancel_event=threading.Event(),
    )

    assert result == NativeCompactionFailure("client", False, True)
    assert compact.calls == []
    assert agent.close_calls == [(client, "native_openai_compaction")]


def test_post_admission_lease_check_baseexception_releases_dispatch_fence():
    from agent.conversation_compression import CompressionCommitFence

    compact = _CompactRecorder(
        SimpleNamespace(
            output=[{"type": "compaction", "encrypted_content": "opaque"}]
        )
    )
    client = _request_client(compact)
    agent = _RequestAgent(client)
    fence = CompressionCommitFence()
    checks = iter((True, KeyboardInterrupt("stop")))

    def _check():
        value = next(checks)
        if isinstance(value, BaseException):
            raise value
        return value

    with pytest.raises(KeyboardInterrupt, match="stop"):
        _request(
            agent,
            _cut_for([{"role": "user", "content": "question"}]),
            pre_dispatch_check=_check,
            dispatch_fence=fence,
            hard_cancel_event=threading.Event(),
        )

    assert compact.calls == []
    assert agent.close_calls == [(client, "native_openai_compaction")]
    assert fence.try_cancel_before_commit() is True


def test_repeated_request_uses_opaque_output_plus_only_new_tail_without_mutation():
    old_source = [{"role": "user", "content": {"nested": "old"}}]
    extended = old_source + [{"role": "assistant", "content": {"nested": "new"}}]
    checkpoint = _checkpoint(old_source)
    source_before = copy.deepcopy(extended)
    checkpoint_before = checkpoint.output
    compact = _CompactRecorder(SimpleNamespace(output=[{"type": "compaction", "future": 2}]))

    result = _request(_RequestAgent(_request_client(compact)), _cut_for(extended), previous_checkpoint=checkpoint)

    assert isinstance(result, NativeCompactionCandidate)
    assert compact.calls[0]["input"] == OPAQUE_OUTPUT + extended[1:]
    assert result.input_item_count == len(OPAQUE_OUTPUT) + 1
    assert extended == source_before
    assert checkpoint.output == checkpoint_before


@pytest.mark.parametrize("source", [[{"value": 1}], [{"value": 2}, {"value": 3}]])
def test_repeated_request_rejects_non_extension_or_prefix_mismatch_without_client(source):
    checkpoint = _checkpoint([{"value": 1}])
    agent = _RequestAgent(object())

    result = _request(agent, _cut_for(source), previous_checkpoint=checkpoint)

    assert result == NativeCompactionFailure("invalid_response", False, True)
    assert agent.create_calls == []
    assert agent.close_calls == []


def test_sdk_output_items_dump_once_and_preserve_unknown_fields_and_order():
    class Item:
        def __init__(self, value):
            self.value = value
            self.calls = []
        def model_dump(self, **kwargs):
            self.calls.append(kwargs)
            return copy.deepcopy(self.value)

    items = [Item({"type": "future_a", "unknown": 1}), Item({"type": "future_b", "unknown": 2})]
    compact = _CompactRecorder(SimpleNamespace(output=items))
    result = _request(_RequestAgent(_request_client(compact)), _cut_for([{"x": 1}]))

    assert result.output == [item.value for item in items]
    assert [item.calls for item in items] == [[{"mode": "json"}], [{"mode": "json"}]]


@pytest.mark.parametrize("response", [
    object(), SimpleNamespace(output=None), SimpleNamespace(output={}),
    SimpleNamespace(output=[]), SimpleNamespace(output=[object()]),
    SimpleNamespace(output=[{"bad": math.nan}]),
])
def test_invalid_compact_outputs_fail_safely_and_close_once(response):
    compact = _CompactRecorder(response)
    client = _request_client(compact)
    agent = _RequestAgent(client)

    result = _request(agent, _cut_for([{"x": 1}]))

    assert result.classification == "invalid_response"
    assert agent.close_calls == [(client, "native_openai_compaction")]


def test_compact_output_over_item_limit_fails_safely_and_closes_once():
    compact = _CompactRecorder(SimpleNamespace(output=[{"x": 1}] * 513))
    client = _request_client(compact)
    agent = _RequestAgent(client)

    result = _request(agent, _cut_for([{"x": 1}]))

    assert result == NativeCompactionFailure("invalid_response", False, True)
    assert agent.close_calls == [(client, "native_openai_compaction")]


def test_compact_output_over_depth_limit_fails_safely_and_closes_once():
    nested = "leaf"
    for _ in range(65):
        nested = {"nested": nested}
    compact = _CompactRecorder(SimpleNamespace(output=[nested]))
    client = _request_client(compact)
    agent = _RequestAgent(client)

    result = _request(agent, _cut_for([{"x": 1}]))

    assert result == NativeCompactionFailure("invalid_response", False, True)
    assert agent.close_calls == [(client, "native_openai_compaction")]


def test_compact_output_over_serialized_size_limit_fails_safely_and_closes_once():
    compact = _CompactRecorder(
        SimpleNamespace(output=[{"opaque": "x" * (4 * 1024 * 1024 + 1)}])
    )
    client = _request_client(compact)
    agent = _RequestAgent(client)

    result = _request(agent, _cut_for([{"x": 1}]))

    assert result == NativeCompactionFailure("invalid_response", False, True)
    assert agent.close_calls == [(client, "native_openai_compaction")]


def test_cyclic_compact_output_fails_safely():
    cyclic = []
    cyclic.append(cyclic)
    compact = _CompactRecorder(SimpleNamespace(output=cyclic))
    result = _request(_RequestAgent(_request_client(compact)), _cut_for([{"x": 1}]))
    assert result.classification == "invalid_response"


@pytest.mark.parametrize("error,classification,retryable", [
    (type("AuthFailure", (Exception,), {"status_code": 401})(), "auth", False),
    (type("PermissionFailure", (Exception,), {"status_code": 403})(), "auth", False),
    (type("UnsupportedFailure", (Exception,), {"status_code": 404})(), "unsupported", False),
    (TimeoutError(), "timeout", True),
    (ConnectionError(), "network", True),
])
def test_compact_errors_are_redacted_classified_failures(error, classification, retryable):
    compact = _CompactRecorder(error=error)
    client = _request_client(compact)
    agent = _RequestAgent(client)

    result = _request(agent, _cut_for([{"x": 1}]))

    assert result == NativeCompactionFailure(classification, retryable, True)
    assert agent.close_calls == [(client, "native_openai_compaction")]


def test_compact_interruption_propagates_after_closing_once():
    compact = _CompactRecorder(error=KeyboardInterrupt())
    client = _request_client(compact)
    agent = _RequestAgent(client)

    with pytest.raises(KeyboardInterrupt):
        _request(agent, _cut_for([{"x": 1}]))

    assert agent.close_calls == [(client, "native_openai_compaction")]


def test_close_failure_does_not_replace_compact_interruption():
    compact = _CompactRecorder(error=KeyboardInterrupt())
    client = _request_client(compact)
    agent = _RequestAgent(client, close_error=RuntimeError("close failed"))

    with pytest.raises(KeyboardInterrupt):
        _request(agent, _cut_for([{"x": 1}]))

    assert agent.close_calls == [(client, "native_openai_compaction")]


def test_creation_failure_and_missing_compact_capability_are_safe_failures():
    create_agent = _RequestAgent(create_error=RuntimeError("token-secret URL-secret payload-secret"))
    create_failure = _request(create_agent, _cut_for([{"private": "payload-secret"}]))
    missing_agent = _RequestAgent(SimpleNamespace(responses=SimpleNamespace()))
    missing = _request(missing_agent, _cut_for([{"private": "payload-secret"}]))

    assert create_failure.classification == "client"
    assert create_agent.close_calls == []
    assert missing.classification == "unsupported"
    assert len(missing_agent.close_calls) == 1
    assert "payload-secret" not in repr(create_failure) + repr(missing)


def test_call_failure_closes_once_and_close_failure_discards_candidate():
    call_client = _request_client(_CompactRecorder(error=RuntimeError("call payload")))
    call_agent = _RequestAgent(call_client)
    call_result = _request(call_agent, _cut_for([{"x": 1}]))
    close_client = _request_client(_CompactRecorder(SimpleNamespace(output=[{"private": "compact-output-secret"}])))
    close_agent = _RequestAgent(close_client, close_error=RuntimeError("close token-secret"))
    close_result = _request(close_agent, _cut_for([{"x": 1}]))

    assert call_result.classification == "client"
    assert call_agent.close_calls == [(call_client, "native_openai_compaction")]
    assert close_result.classification == "client"
    assert close_agent.close_calls == [(close_client, "native_openai_compaction")]
    assert "compact-output-secret" not in repr(close_result)


@pytest.mark.parametrize("overrides", [
    {"model": ""}, {"model": "   "}, {"model": type("Model", (str,), {})("gpt-5")},
    {"compact_instructions": 7},
    {"compact_instructions": type("Instructions", (str,), {})("compact")},
    {"resolved_timeout": 0}, {"resolved_timeout": -1},
    {"resolved_timeout": math.inf}, {"resolved_timeout": math.nan},
    {"resolved_timeout": True},
])
def test_invalid_request_arguments_fail_before_client_creation(overrides):
    agent = _RequestAgent(object())
    result = _request(agent, _cut_for([{"x": 1}]), **overrides)
    assert result.classification == "client"
    assert agent.create_calls == []


def test_response_metadata_is_accepted_only_when_plain_and_valid():
    class ID(str):
        pass
    compact = _CompactRecorder(SimpleNamespace(output=[{"x": 1}], id=ID("resp_secret"), created_at=math.inf))
    result = _request(_RequestAgent(_request_client(compact)), _cut_for([{"x": 1}]))
    assert result.compact_response_id is None
    assert result.compact_created_at is None


def test_candidate_output_is_deep_copy_safe_and_repr_never_contains_payload():
    sentinel = "transcript-tool-args-encrypted-output-sentinel"
    compact = _CompactRecorder(SimpleNamespace(output=[{"private": {"value": sentinel}}]))
    result = _request(_RequestAgent(_request_client(compact)), _cut_for([{"x": 1}]))

    fetched = result.output
    fetched[0]["private"]["value"] = "mutated"
    assert result.output == [{"private": {"value": sentinel}}]
    assert sentinel not in repr(result)
    assert "_output_json" not in repr(result)
    with pytest.raises(Exception):
        result.input_item_count = 99


def test_candidate_constructor_enforces_hash_counts_timestamp_and_strict_json():
    valid = dict(source_input_item_count=1, source_input_sha256="a" * 64,
                 compact_response_id=None, compact_created_at=None,
                 input_item_count=1, output_item_count=1, output=[{"x": 1}])
    for field, value in (
        ("source_input_item_count", -1), ("source_input_sha256", "not-a-hash"),
        ("compact_created_at", math.nan), ("input_item_count", -1),
        ("output_item_count", 2), ("output", []), ("output", [{"x": math.nan}]),
    ):
        kwargs = dict(valid)
        kwargs[field] = value
        with pytest.raises(ValueError):
            NativeCompactionCandidate(**kwargs)


def test_failure_is_immutable_narrow_and_payload_safe():
    failure = NativeCompactionFailure("network", True, True)
    with pytest.raises(Exception):
        failure.retryable = False
    assert vars(failure) == {"classification": "network", "retryable": True, "use_textual_fallback": True}
    with pytest.raises(ValueError):
        NativeCompactionFailure("payload-secret", False, True)


def test_failure_paths_do_not_log_or_render_payload_token_url_or_output(caplog):
    sentinels = (
        "payload-secret",
        "token-secret",
        "https://secret.test/path",
        "opaque-secret",
    )

    class RaisingResponses:
        @property
        def compact(self):
            raise RuntimeError(" ".join(sentinels))

    agent = _RequestAgent(SimpleNamespace(responses=RaisingResponses()))
    result = _request(agent, _cut_for([{"private": sentinels[0]}]))
    rendered = repr(result) + caplog.text
    assert isinstance(result, NativeCompactionFailure)
    assert all(sentinel not in rendered for sentinel in sentinels)


def test_raising_exception_status_property_is_redacted_and_client_still_closes():
    class RaisingStatusError(Exception):
        @property
        def status_code(self):
            raise RuntimeError("token-url-payload-secret")

    compact = _CompactRecorder(error=RaisingStatusError("opaque-secret"))
    client = _request_client(compact)
    agent = _RequestAgent(client)

    result = _request(agent, _cut_for([{"private": "payload-secret"}]))

    assert result == NativeCompactionFailure("client", False, True)
    assert agent.close_calls == [(client, "native_openai_compaction")]


class _ObservabilityDB:
    def __init__(self):
        self.holder = "holder-1"
        self.checkpoint = None

    def get_compression_lock_holder(self, _session_id):
        return self.holder

    def load_native_openai_checkpoint(self, _session_id):
        return None

    def upsert_native_openai_checkpoint(self, checkpoint, *, expected_lock_holder):
        assert expected_lock_holder == self.holder
        self.checkpoint = checkpoint
        return True


class _ObservabilityAgent:
    def __init__(self, *, eligible=True, status_error=None):
        self.native_compaction_policy = SimpleNamespace(
            is_eligible=lambda **_kwargs: eligible
        )
        self.client = _FakeCompactClient(compact=lambda **_: None)
        self.provider = "openai"
        self.api_mode = "codex_responses"
        self.base_url = "https://api.openai.com/v1"
        self.model = "gpt-5"
        self.session_id = "session-observability"
        self._session_db = _ObservabilityDB()
        self._active_compression_lock_holder = "holder-1"
        self.context_compressor = SimpleNamespace(
            protect_last_n=1,
            record_external_compaction=lambda **_kwargs: None,
        )
        self.statuses = []
        self._status_error = status_error

    def _emit_status(self, message):
        if self._status_error is not None:
            raise self._status_error
        self.statuses.append(message)

    def _build_api_kwargs(self, messages):
        return {
            "input": [
                {
                    "role": message["role"],
                    "content": message.get("api_content", message.get("content", "")),
                }
                for message in copy.deepcopy(messages)
            ]
        }

    def _get_transport(self):
        return SimpleNamespace(preflight_kwargs=lambda kwargs, **_options: kwargs)

    def _is_copilot_url(self):
        return False

    def _is_codex_backend(self):
        return False

    def _resolved_api_call_timeout(self):
        return 10.0


def _observability_messages(sentinel="ordinary-transcript-sentinel"):
    return [
        {"role": "user", "content": sentinel},
        {"role": "assistant", "content": "answer-one"},
        {"role": "user", "content": "question-two"},
        {"role": "assistant", "content": "answer-two"},
    ]


def test_native_lifecycle_observability_is_structured_and_payload_safe(caplog):
    from agent.conversation_compression import _try_native_openai_compaction

    sentinels = {
        "api_key": "api-key-sentinel",
        "authorization": "authorization-header-sentinel",
        "input": "compact-input-sentinel",
        "output": "encrypted-output-sentinel",
        "transcript": "ordinary-transcript-sentinel",
    }
    agent = _ObservabilityAgent()
    agent.api_key = sentinels["api_key"]
    agent.default_headers = {"Authorization": sentinels["authorization"]}
    messages = _observability_messages(sentinels["transcript"])
    messages[1]["api_content"] = sentinels["input"]

    def _candidate(_agent, *, cut, **_kwargs):
        return NativeCompactionCandidate(
            source_input_item_count=cut.source_input_item_count,
            source_input_sha256=cut.source_input_sha256,
            compact_response_id="response-id-omitted-by-policy",
            compact_created_at=123.0,
            input_item_count=cut.source_input_item_count,
            output_item_count=1,
            output=[{"type": "compaction", "encrypted_content": sentinels["output"]}],
        )

    caplog.set_level("DEBUG", logger="agent.conversation_compression")
    with (
        patch(
            "agent.chat_completion_helpers.native_openai_identity_for_agent",
            return_value=_identity(),
        ),
        patch(
            "agent.native_openai_compaction.request_native_compaction_candidate",
            side_effect=_candidate,
        ),
    ):
        outcome = _try_native_openai_compaction(
            agent, messages, commit_fence=None, hard_cancel_event=None
        )

    assert outcome == "success"
    assert agent.statuses == [
        "Context compacted with OpenAI native Responses projection "
        "(generation 1; readable history retained)."
    ]
    records = [record for record in caplog.records if hasattr(record, "native_compaction")]
    assert len(records) == 1
    metadata = records[0].native_compaction
    assert set(metadata) == {
        "strategy", "provider", "api_mode", "model", "base_url_host",
        "input_item_count", "output_item_count", "tail_item_count", "generation",
        "prefix_sha256", "elapsed_ms",
    }
    rendered = caplog.text + repr(metadata) + repr(agent.statuses)
    assert "response-id-omitted-by-policy" not in rendered
    assert all(sentinel not in rendered for sentinel in sentinels.values())


def test_enabled_native_failure_warns_once_per_turn_and_reset_allows_next_turn(caplog):
    from agent.conversation_compression import (
        _try_native_openai_compaction,
        reset_native_compaction_observability,
    )

    agent = _ObservabilityAgent()
    caplog.set_level("INFO", logger="agent.conversation_compression")
    failure = NativeCompactionFailure("network", True, True)
    with (
        patch(
            "agent.chat_completion_helpers.native_openai_identity_for_agent",
            return_value=_identity(),
        ),
        patch(
            "agent.native_openai_compaction.request_native_compaction_candidate",
            return_value=failure,
        ),
    ):
        assert _try_native_openai_compaction(
            agent, _observability_messages(), commit_fence=None, hard_cancel_event=None
        ) == "fallback"
        assert _try_native_openai_compaction(
            agent, _observability_messages(), commit_fence=None, hard_cancel_event=None
        ) == "fallback"
        reset_native_compaction_observability(agent)
        assert _try_native_openai_compaction(
            agent, _observability_messages(), commit_fence=None, hard_cancel_event=None
        ) == "fallback"

    warning = "OpenAI native compaction unavailable; using Hermes text compression."
    assert agent.statuses == [warning, warning]
    metadata = [
        record.native_compaction
        for record in caplog.records
        if hasattr(record, "native_compaction")
    ]
    assert len(metadata) == 3
    assert all(item["fallback_category"] == "network" for item in metadata)
    assert all("elapsed_ms" in item for item in metadata)


def test_disabled_native_policy_does_not_emit_fallback_status(caplog):
    from agent.conversation_compression import _try_native_openai_compaction

    agent = _ObservabilityAgent(eligible=False)
    assert _try_native_openai_compaction(
        agent, _observability_messages(), commit_fence=None, hard_cancel_event=None
    ) == "fallback"
    assert agent.statuses == []
    assert not any(hasattr(record, "native_compaction") for record in caplog.records)


def test_status_callback_error_cannot_change_success_or_fallback(caplog):
    from agent.conversation_compression import _try_native_openai_compaction

    status_error = RuntimeError("status-callback-secret")
    success_agent = _ObservabilityAgent(status_error=status_error)
    failure_agent = _ObservabilityAgent(status_error=status_error)

    def _candidate(_agent, *, cut, **_kwargs):
        return NativeCompactionCandidate(
            source_input_item_count=cut.source_input_item_count,
            source_input_sha256=cut.source_input_sha256,
            compact_response_id=None,
            compact_created_at=None,
            input_item_count=cut.source_input_item_count,
            output_item_count=1,
            output=[{"type": "compaction", "encrypted_content": "opaque"}],
        )

    with patch(
        "agent.chat_completion_helpers.native_openai_identity_for_agent",
        return_value=_identity(),
    ):
        with patch(
            "agent.native_openai_compaction.request_native_compaction_candidate",
            side_effect=_candidate,
        ):
            assert _try_native_openai_compaction(
                success_agent,
                _observability_messages(),
                commit_fence=None,
                hard_cancel_event=None,
            ) == "success"
        with patch(
            "agent.native_openai_compaction.request_native_compaction_candidate",
            return_value=NativeCompactionFailure("timeout", True, True),
        ):
            assert _try_native_openai_compaction(
                failure_agent,
                _observability_messages(),
                commit_fence=None,
                hard_cancel_event=None,
            ) == "fallback"

    assert "status-callback-secret" not in caplog.text


def test_malformed_native_result_property_fails_open_without_rendering(caplog):
    from agent.conversation_compression import _try_native_openai_compaction

    class MalformedResult:
        @property
        def classification(self):
            raise RuntimeError("malformed-result-secret")

    agent = _ObservabilityAgent()
    with (
        patch(
            "agent.chat_completion_helpers.native_openai_identity_for_agent",
            return_value=_identity(),
        ),
        patch(
            "agent.native_openai_compaction.request_native_compaction_candidate",
            return_value=MalformedResult(),
        ),
    ):
        assert _try_native_openai_compaction(
            agent,
            _observability_messages(),
            commit_fence=None,
            hard_cancel_event=None,
        ) == "fallback"

    assert "malformed-result-secret" not in caplog.text
