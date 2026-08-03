"""Behavior contracts for native OpenAI compaction checkpoint primitives."""

from __future__ import annotations

import copy
import math
from types import SimpleNamespace

import pytest

from agent.context_compressor import ContextCompressor
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

    metadata = checkpoint.redacted_metadata()

    assert metadata == {
        "provider": "openai",
        "api_mode": "codex_responses",
        "model": "gpt-5",
        "base_url_host": "api.openai.com",
        "issuer_kind": "api_key",
        "credential_scope_present": True,
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


def test_redacted_metadata_never_exposes_arbitrary_credential_scope_values():
    credential_scope = "credential-scope-raw-secret-sentinel"
    checkpoint = _checkpoint(identity=_identity(credential_scope=credential_scope))

    metadata = checkpoint.redacted_metadata()

    assert metadata["credential_scope_present"] is True
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
