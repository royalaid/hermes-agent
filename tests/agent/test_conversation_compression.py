"""Task 8 native-first compression orchestration behavior contracts."""

from __future__ import annotations

import copy
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.conversation_compression import (
    CompressionCommitFence,
    capture_compression_attempt_outcome,
    compress_context,
)
from agent.native_openai_compaction import (
    NativeCompactionCandidate,
    NativeCompactionFailure,
    NativeCompactionPolicy,
)


class _Policy:
    def __init__(self, eligible: bool = True):
        self.eligible = eligible

    def is_eligible(self, **_kwargs):
        return self.eligible


class _DB:
    def __init__(self):
        self.checkpoint = None
        self.holder = None
        self.events = []
        self.fail_upsert = False
        self.acquire = True

    def try_acquire_compression_lock(self, session_id, holder, *, ttl_seconds):
        if not self.acquire:
            return False
        self.holder = holder
        return True

    def refresh_compression_lock(self, session_id, holder, *, ttl_seconds):
        return self.holder == holder

    def release_compression_lock(self, session_id, holder):
        if self.holder == holder:
            self.holder = None
        return True

    def get_compression_lock_holder(self, session_id):
        return self.holder

    def load_native_openai_checkpoint(self, session_id):
        return self.checkpoint

    def upsert_native_openai_checkpoint(
        self, checkpoint, *, expected_lock_holder=None
    ):
        if self.fail_upsert:
            raise RuntimeError("SECRET_UPSERT_FAILURE")
        if expected_lock_holder != self.holder:
            return False
        self.events.append("upsert")
        self.checkpoint = checkpoint
        return True


class _Compressor:
    protect_last_n = 1
    compression_count = 0

    def __init__(self):
        self.text_calls = 0
        self.record_calls = []
        self._last_compress_aborted = False
        self._last_summary_error = None
        self._last_compression_made_progress = False
        self._last_summary_fallback_used = False
        self._last_feasibility_skip = False
        self.raise_on_text = True
        self.raise_on_record = False
        self.events = []

    def compress(self, messages, **_kwargs):
        self.text_calls += 1
        if self.raise_on_text:
            raise AssertionError("text compressor must be skipped on native success")
        return messages

    def record_external_compaction(self, **kwargs):
        self.events.append("record")
        self.record_calls.append(kwargs)
        if self.raise_on_record:
            raise RuntimeError("SECRET_RECORD_FAILURE")


class _Agent:
    def __init__(self):
        self.context_compressor = _Compressor()
        self.native_compaction_policy = _Policy()
        self.client = SimpleNamespace(responses=SimpleNamespace(compact=lambda **_: None))
        self.provider = "openai"
        self.api_mode = "codex_responses"
        self.base_url = "https://api.openai.com/v1"
        self.model = "gpt-5"
        self.session_id = "session-native"
        self.platform = "cli"
        self.tools = []
        self._session_db = _DB()
        self.context_compressor.events = self._session_db.events
        self._memory_manager = None
        self._todo_store = SimpleNamespace(format_for_injection=lambda: "")
        self._compression_feasibility_checked = True
        self.compression_in_place = True
        self._cached_system_prompt = "current prompt"
        self._hard_interrupt_requested = SimpleNamespace(is_set=lambda: False)
        self._codex_reasoning_replay_enabled = True
        self._base_url_hostname = "api.openai.com"
        self._base_url_lower = self.base_url.lower()
        self._last_compaction_in_place = True
        self._last_flushed_db_idx = 7
        self._flushed_db_message_ids = {11, 12}
        self._flushed_db_message_session_id = self.session_id
        self.statuses = []
        self.touches = []

    def _emit_status(self, message):
        self.statuses.append(message)

    def _emit_warning(self, message):
        self.statuses.append(message)

    def _build_system_prompt(self, system_message):
        return f"built:{system_message}"

    def _build_api_kwargs(self, candidate_messages):
        return {"input": copy.deepcopy(candidate_messages)}

    def _get_transport(self):
        return SimpleNamespace(preflight_kwargs=lambda kwargs, **_options: kwargs)

    def _is_copilot_url(self):
        return False

    def _is_codex_backend(self):
        return False

    def _resolved_api_call_timeout(self):
        return 19.0

    def _touch_activity(self, description, **_kwargs):
        self.touches.append(description)


class _Event:
    def __init__(self):
        self.value = False

    def is_set(self):
        return self.value

    def set(self):
        self.value = True


def _messages():
    return [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]


def test_eligible_native_success_keeps_readable_transcript_and_skips_text_boundary():
    agent = _Agent()
    messages = _messages()
    original = copy.deepcopy(messages)

    def _candidate(_agent, *, cut, **_kwargs):
        return NativeCompactionCandidate(
            source_input_item_count=cut.source_input_item_count,
            source_input_sha256=cut.source_input_sha256,
            compact_response_id="resp-1",
            compact_created_at=123.0,
            input_item_count=cut.source_input_item_count,
            output_item_count=1,
            output=[{"type": "compaction", "encrypted_content": "OPAQUE"}],
        )

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate",
        side_effect=_candidate,
    ):
        returned, prompt = compress_context(
            agent,
            messages,
            "incoming prompt",
            approx_tokens=100_000,
            force=True,
            commit_fence=CompressionCommitFence(),
        )

    assert returned is messages
    assert messages == original
    assert prompt == "current prompt"
    assert agent.context_compressor.text_calls == 0
    assert agent.context_compressor.record_calls == [
        {"strategy": "openai_native", "source_items": 2, "output_items": 1}
    ]
    assert agent._last_native_compaction_succeeded is True
    outcome = capture_compression_attempt_outcome(agent)
    assert outcome is not None and outcome.native_succeeded is True
    assert agent._last_compression_attempt_in_place is None
    assert agent._last_compaction_in_place is False
    assert agent.session_id == "session-native"
    assert agent._last_flushed_db_idx == 7
    assert agent._flushed_db_message_ids == {11, 12}
    assert agent._flushed_db_message_session_id == "session-native"
    assert agent._session_db.events == ["upsert", "record"]
    checkpoint = agent._session_db.checkpoint
    assert agent._native_openai_checkpoint is checkpoint
    assert agent._native_openai_checkpoint_session_id == "session-native"
    assert checkpoint.session_id == "session-native"
    assert checkpoint.generation == 1
    assert checkpoint.identity.provider == "openai"
    assert checkpoint.identity.api_mode == "codex_responses"
    assert checkpoint.identity.model == "gpt-5"
    assert checkpoint.identity.credential_scope == ""
    assert checkpoint.identity.replay_encrypted_reasoning is True
    assert checkpoint.output == [
        {"type": "compaction", "encrypted_content": "OPAQUE"}
    ]


def test_native_candidate_cannot_commit_after_compression_lease_ownership_is_lost():
    agent = _Agent()
    messages = _messages()
    original = copy.deepcopy(messages)

    def _candidate(_agent, *, cut, **_kwargs):
        agent._session_db.holder = "replacement-holder"
        return NativeCompactionCandidate(
            source_input_item_count=cut.source_input_item_count,
            source_input_sha256=cut.source_input_sha256,
            compact_response_id=None,
            compact_created_at=None,
            input_item_count=cut.source_input_item_count,
            output_item_count=1,
            output=[{"type": "compaction", "encrypted_content": "OPAQUE"}],
        )

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate",
        side_effect=_candidate,
    ):
        returned, prompt = compress_context(
            agent,
            messages,
            "incoming prompt",
            force=True,
            commit_fence=CompressionCommitFence(),
        )

    assert returned is messages
    assert messages == original
    assert prompt == "current prompt"
    assert agent._session_db.events == []
    assert agent.context_compressor.record_calls == []
    assert agent.context_compressor.text_calls == 0
    assert agent._last_native_compaction_succeeded is False


def test_native_upsert_failure_uses_text_fallback_once_in_the_same_attempt():
    agent = _Agent()
    agent.context_compressor.raise_on_text = False
    agent._session_db.fail_upsert = True
    messages = _messages()

    def _candidate(_agent, *, cut, **_kwargs):
        return NativeCompactionCandidate(
            source_input_item_count=cut.source_input_item_count,
            source_input_sha256=cut.source_input_sha256,
            compact_response_id=None,
            compact_created_at=None,
            input_item_count=cut.source_input_item_count,
            output_item_count=1,
            output=[{"type": "compaction", "encrypted_content": "SECRET_OPAQUE"}],
        )

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate",
        side_effect=_candidate,
    ):
        returned, _ = compress_context(
            agent,
            messages,
            "sys",
            force=True,
            commit_fence=CompressionCommitFence(),
        )

    assert returned is messages
    assert agent.context_compressor.text_calls == 1
    assert agent.context_compressor.record_calls == []
    assert agent._last_native_compaction_succeeded is False


def test_native_typed_endpoint_failure_uses_text_fallback_once():
    agent = _Agent()
    agent.context_compressor.raise_on_text = False
    messages = _messages()

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate",
        return_value=NativeCompactionFailure("timeout", True, True),
    ):
        returned, _ = compress_context(agent, messages, "sys", force=True)

    assert returned is messages
    assert agent.context_compressor.text_calls == 1
    assert agent._session_db.events == []


def test_ineligible_native_path_preserves_text_compressor_behavior():
    agent = _Agent()
    agent.native_compaction_policy = _Policy(False)
    agent.context_compressor.raise_on_text = False
    messages = _messages()

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate"
    ) as endpoint:
        returned, _ = compress_context(agent, messages, "sys", force=True)

    assert returned is messages
    assert agent.context_compressor.text_calls == 1
    endpoint.assert_not_called()


@pytest.mark.parametrize("middleware_kind", ["llm_request", "llm_execution"])
def test_native_compaction_fails_closed_when_llm_middleware_can_rewrite_wire_input(
    middleware_kind,
):
    agent = _Agent()
    agent.context_compressor.raise_on_text = False
    messages = _messages()

    def _has_middleware(kind):
        return kind == middleware_kind

    with (
        patch("hermes_cli.plugins.has_middleware", side_effect=_has_middleware),
        patch(
            "agent.native_openai_compaction.request_native_compaction_candidate"
        ) as endpoint,
    ):
        returned, _ = compress_context(agent, messages, "sys", force=True)

    assert returned is messages
    assert agent.context_compressor.text_calls == 1
    assert agent._last_native_compaction_succeeded is False
    endpoint.assert_not_called()


def test_native_compaction_aborts_if_cancelled_during_middleware_lookup():
    agent = _Agent()
    agent.context_compressor.raise_on_text = False
    cancel_event = threading.Event()
    agent._hard_interrupt_requested = cancel_event
    messages = _messages()

    def _has_middleware(_kind):
        cancel_event.set()
        return True

    with (
        patch("hermes_cli.plugins.has_middleware", side_effect=_has_middleware),
        patch(
            "agent.native_openai_compaction.request_native_compaction_candidate"
        ) as endpoint,
    ):
        returned, _ = compress_context(agent, messages, "sys", force=True)

    assert returned is messages
    assert agent.context_compressor.text_calls == 0
    assert agent._last_native_compaction_succeeded is False
    endpoint.assert_not_called()


def test_lock_loser_never_calls_native_endpoint_or_text_compressor():
    agent = _Agent()
    agent._session_db.acquire = False
    messages = _messages()

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate"
    ) as endpoint:
        returned, _ = compress_context(agent, messages, "sys", force=True)

    assert returned is messages
    assert agent.context_compressor.text_calls == 0
    endpoint.assert_not_called()


def test_native_endpoint_runs_under_lease_heartbeat_and_one_memory_prehook():
    agent = _Agent()
    prehook_calls = []
    agent._memory_manager = SimpleNamespace(
        on_pre_compress=lambda seen: prehook_calls.append(seen) or "memory insight"
    )
    messages = _messages()

    def _candidate(_agent, *, cut, compact_instructions, resolved_timeout, **_kwargs):
        assert agent._session_db.holder == agent._active_compression_lock_holder
        assert agent.touches[0] == "context compression started"
        assert compact_instructions
        assert "memory insight" not in compact_instructions
        assert resolved_timeout == 19.0
        assert prehook_calls == [messages]
        assert agent.context_compressor._compression_cancelled_check() is False
        return NativeCompactionCandidate(
            source_input_item_count=cut.source_input_item_count,
            source_input_sha256=cut.source_input_sha256,
            compact_response_id=None,
            compact_created_at=None,
            input_item_count=cut.source_input_item_count,
            output_item_count=1,
            output=[{"type": "compaction", "encrypted_content": "OPAQUE"}],
        )

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate",
        side_effect=_candidate,
    ):
        compress_context(agent, messages, "sys", force=True)

    assert len(prehook_calls) == 1
    assert agent.touches[-1] == "context compression completed"
    assert agent.context_compressor._compression_cancelled_check is None


def test_hard_cancellation_after_native_endpoint_blocks_all_commit_and_fallback():
    agent = _Agent()
    event = _Event()
    agent._hard_interrupt_requested = event
    messages = _messages()

    def _candidate(_agent, *, cut, **_kwargs):
        event.set()
        return NativeCompactionCandidate(
            source_input_item_count=cut.source_input_item_count,
            source_input_sha256=cut.source_input_sha256,
            compact_response_id=None,
            compact_created_at=None,
            input_item_count=cut.source_input_item_count,
            output_item_count=1,
            output=[{"type": "compaction", "encrypted_content": "OPAQUE"}],
        )

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate",
        side_effect=_candidate,
    ):
        returned, _ = compress_context(
            agent,
            messages,
            "sys",
            force=True,
            commit_fence=CompressionCommitFence(),
        )

    assert returned is messages
    assert agent._session_db.events == []
    assert agent.context_compressor.text_calls == 0
    assert agent.context_compressor.record_calls == []


def test_matching_checkpoint_is_reused_and_extended_to_next_generation():
    agent = _Agent()
    messages = _messages()
    previous_arguments = []

    def _candidate(_agent, *, cut, previous_checkpoint, **_kwargs):
        previous_arguments.append(previous_checkpoint)
        return NativeCompactionCandidate(
            source_input_item_count=cut.source_input_item_count,
            source_input_sha256=cut.source_input_sha256,
            compact_response_id=None,
            compact_created_at=None,
            input_item_count=cut.source_input_item_count,
            output_item_count=1,
            output=[
                {
                    "type": "compaction",
                    "encrypted_content": f"OPAQUE-{len(previous_arguments)}",
                }
            ],
        )

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate",
        side_effect=_candidate,
    ):
        compress_context(agent, messages, "sys", force=True)
        first = agent._session_db.checkpoint
        messages.extend(
            [
                {"role": "user", "content": "u3"},
                {"role": "assistant", "content": "a3"},
            ]
        )
        compress_context(agent, messages, "sys", force=True)

    second = agent._session_db.checkpoint
    assert previous_arguments == [None, first]
    assert second.generation == 2
    assert second.created_at == first.created_at
    assert second.output == [
        {"type": "compaction", "encrypted_content": "OPAQUE-2"}
    ]


def test_identity_mismatch_never_reuses_prior_opaque_checkpoint():
    agent = _Agent()
    messages = _messages()
    previous_arguments = []

    def _candidate(_agent, *, cut, previous_checkpoint, **_kwargs):
        previous_arguments.append(previous_checkpoint)
        return NativeCompactionCandidate(
            source_input_item_count=cut.source_input_item_count,
            source_input_sha256=cut.source_input_sha256,
            compact_response_id=None,
            compact_created_at=None,
            input_item_count=cut.source_input_item_count,
            output_item_count=1,
            output=[{"type": "compaction", "encrypted_content": "SECRET_OPAQUE"}],
        )

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate",
        side_effect=_candidate,
    ):
        compress_context(agent, messages, "sys", force=True)
        agent.model = "gpt-5-new-identity"
        compress_context(agent, messages, "sys", force=True)

    assert previous_arguments == [None, None]
    assert agent._session_db.checkpoint.generation == 1


def test_custom_context_engine_is_not_bypassed_by_native_endpoint():
    agent = _Agent()
    agent.context_compressor.raise_on_text = False
    agent.native_compaction_policy = NativeCompactionPolicy(
        feature_enabled=True,
        built_in_compressor=False,
        has_session_state=True,
    )
    messages = _messages()

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate"
    ) as endpoint:
        returned, _ = compress_context(agent, messages, "sys", force=True)

    assert returned is messages
    assert agent.context_compressor.text_calls == 1
    endpoint.assert_not_called()


def test_commit_fence_cancellation_after_endpoint_denies_native_and_text_commit():
    agent = _Agent()
    fence = CompressionCommitFence()
    messages = _messages()

    def _candidate(_agent, *, cut, **_kwargs):
        assert fence.cancel_before_commit()
        return NativeCompactionCandidate(
            source_input_item_count=cut.source_input_item_count,
            source_input_sha256=cut.source_input_sha256,
            compact_response_id=None,
            compact_created_at=None,
            input_item_count=cut.source_input_item_count,
            output_item_count=1,
            output=[{"type": "compaction", "encrypted_content": "OPAQUE"}],
        )

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate",
        side_effect=_candidate,
    ):
        returned, _ = compress_context(
            agent,
            messages,
            "sys",
            force=True,
            commit_fence=fence,
        )

    assert returned is messages
    assert agent._session_db.events == []
    assert agent.context_compressor.text_calls == 0


def test_hard_cancel_during_native_dispatch_returns_without_waiting_and_defers_lease():
    fence = CompressionCommitFence()
    hard_cancel = threading.Event()
    releases = []
    fence.register_cancelled_lock_release(lambda: releases.append("released"))

    assert fence.begin_dispatch(hard_cancel) is True
    assert fence.cancel_before_commit(hard_cancel) is True
    assert hard_cancel.is_set()
    assert fence.is_cancelled is True
    fence.release_cancelled_compression_lock()
    assert releases == []

    fence.finish_dispatch()
    assert releases == ["released"]


def test_hard_cancel_rechecks_when_dispatch_wins_after_initial_phase_probe():
    fence = CompressionCommitFence()
    hard_cancel = threading.Event()
    original_try_cancel = fence.try_cancel_before_commit
    probes = 0

    def race_dispatch_after_first_probe():
        nonlocal probes
        probes += 1
        if probes == 1:
            assert fence.begin_dispatch(hard_cancel) is True
            return None
        return original_try_cancel()

    fence.try_cancel_before_commit = race_dispatch_after_first_probe
    try:
        assert fence.cancel_before_commit() is True
        assert hard_cancel.is_set()
        assert probes == 2
    finally:
        fence.finish_dispatch()


def test_dispatch_admission_rechecks_revocation_after_publishing_cancel_event():
    fence = CompressionCommitFence()
    hard_cancel = threading.Event()

    class RevokeDuringFirstAdmissionCheck:
        def __bool__(self):
            fence.revoke_commit_admission()
            return False

    fence._admission_revoked = RevokeDuringFirstAdmissionCheck()
    admitted = fence.begin_dispatch(hard_cancel)
    try:
        assert admitted is False
        assert hard_cancel.is_set()
        assert not fence._dispatch_phase.is_set()
    finally:
        if admitted:
            fence.finish_dispatch()


def test_native_failures_never_emit_payload_credentials_or_opaque_output(caplog):
    agent = _Agent()
    agent.context_compressor.raise_on_text = False
    agent.api_key = "SECRET_CREDENTIAL"
    agent._session_db.fail_upsert = True
    messages = [
        {"role": "user", "content": "SECRET_PAYLOAD"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]

    def _candidate(_agent, *, cut, **_kwargs):
        return NativeCompactionCandidate(
            source_input_item_count=cut.source_input_item_count,
            source_input_sha256=cut.source_input_sha256,
            compact_response_id=None,
            compact_created_at=None,
            input_item_count=cut.source_input_item_count,
            output_item_count=1,
            output=[
                {"type": "compaction", "encrypted_content": "SECRET_OPAQUE"}
            ],
        )

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate",
        side_effect=_candidate,
    ):
        compress_context(agent, messages, "sys", force=True)

    emitted = "\n".join(record.getMessage() for record in caplog.records)
    emitted += "\n" + "\n".join(agent.statuses)
    assert "SECRET_PAYLOAD" not in emitted
    assert "SECRET_CREDENTIAL" not in emitted
    assert "SECRET_OPAQUE" not in emitted
    assert "SECRET_UPSERT_FAILURE" not in emitted


def test_native_cut_uses_finalized_ordinary_responses_wire_input():
    agent = _Agent()
    messages = _messages()
    seen_source_inputs = []

    class _Transport:
        def preflight_kwargs(self, kwargs, **options):
            assert options == {
                "allow_stream": False,
                "is_github_responses": False,
                "sanitize_harmony_tokens": False,
            }
            finalized = copy.deepcopy(kwargs)
            finalized["input"] = [
                {**item, "content": f"preflight:{item['content']}"}
                for item in finalized["input"]
            ]
            return finalized

    agent._get_transport = lambda: _Transport()
    agent._is_copilot_url = lambda: False
    agent._is_codex_backend = lambda: False

    def _candidate(_agent, *, cut, **_kwargs):
        seen_source_inputs.append(cut.source_input)
        return NativeCompactionCandidate(
            source_input_item_count=cut.source_input_item_count,
            source_input_sha256=cut.source_input_sha256,
            compact_response_id=None,
            compact_created_at=None,
            input_item_count=cut.source_input_item_count,
            output_item_count=1,
            output=[{"type": "compaction", "encrypted_content": "OPAQUE"}],
        )

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate",
        side_effect=_candidate,
    ):
        compress_context(agent, messages, "sys", force=True)

    assert seen_source_inputs
    assert all(
        item["content"].startswith("preflight:") for item in seen_source_inputs[0]
    )


def test_hard_cancel_before_native_dispatch_skips_endpoint_and_text_fallback():
    agent = _Agent()
    event = _Event()
    event.set()
    agent._hard_interrupt_requested = event

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate"
    ) as endpoint:
        returned, _ = compress_context(agent, _messages(), "sys", force=True)

    endpoint.assert_not_called()
    assert returned == _messages()
    assert agent.context_compressor.text_calls == 0
    assert agent._session_db.events == []


def test_lease_loss_during_failed_native_request_aborts_without_text_fallback():
    agent = _Agent()

    def _failed_request(*_args, **_kwargs):
        agent._session_db.holder = "replacement-holder"
        return NativeCompactionFailure("network", True, True)

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate",
        side_effect=_failed_request,
    ):
        returned, _ = compress_context(agent, _messages(), "sys", force=True)

    assert returned == _messages()
    assert agent.context_compressor.text_calls == 0
    assert agent._session_db.events == []


def test_lease_loss_during_failed_checkpoint_upsert_aborts_without_text_fallback():
    agent = _Agent()
    agent.context_compressor.raise_on_text = False

    def _failed_upsert(_checkpoint, *, expected_lock_holder):
        assert expected_lock_holder == agent._active_compression_lock_holder
        agent._session_db.holder = "replacement-holder"
        raise RuntimeError("SECRET_UPSERT_FAILURE")

    agent._session_db.upsert_native_openai_checkpoint = _failed_upsert

    def _candidate(_agent, *, cut, **_kwargs):
        return NativeCompactionCandidate(
            source_input_item_count=cut.source_input_item_count,
            source_input_sha256=cut.source_input_sha256,
            compact_response_id=None,
            compact_created_at=None,
            input_item_count=cut.source_input_item_count,
            output_item_count=1,
            output=[{"type": "compaction", "encrypted_content": "OPAQUE"}],
        )

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate",
        side_effect=_candidate,
    ):
        returned, _ = compress_context(agent, _messages(), "sys", force=True)

    assert returned == _messages()
    assert agent.context_compressor.text_calls == 0
    assert agent.context_compressor.record_calls == []
    assert agent._last_native_compaction_succeeded is False


def test_cancellation_during_native_eligibility_aborts_without_text_fallback():
    agent = _Agent()
    agent.context_compressor.raise_on_text = False
    fence = CompressionCommitFence()

    class _CancellingPolicy:
        def is_eligible(self, **_kwargs):
            assert fence.cancel_before_commit() is True
            agent._active_compression_lock_holder = None
            return True

    agent.native_compaction_policy = _CancellingPolicy()

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate"
    ) as endpoint:
        returned, _ = compress_context(
            agent,
            _messages(),
            "sys",
            force=True,
            commit_fence=fence,
        )

    assert returned == _messages()
    assert fence.is_cancelled is True
    assert agent.context_compressor.text_calls == 0
    assert agent.context_compressor.record_calls == []
    assert agent._last_native_compaction_succeeded is False
    endpoint.assert_not_called()


def test_cancellation_during_ineligible_policy_aborts_without_text_fallback():
    agent = _Agent()
    agent.context_compressor.raise_on_text = False
    fence = CompressionCommitFence()

    class _CancellingPolicy:
        def is_eligible(self, **_kwargs):
            assert fence.cancel_before_commit() is True
            return False

    agent.native_compaction_policy = _CancellingPolicy()

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate"
    ) as endpoint:
        compress_context(
            agent,
            _messages(),
            "sys",
            force=True,
            commit_fence=fence,
        )

    assert agent.context_compressor.text_calls == 0
    assert agent.context_compressor.record_calls == []
    endpoint.assert_not_called()


def _assert_cancellation_during_preflight_aborts(*, raise_after_cancel):
    agent = _Agent()
    agent.context_compressor.raise_on_text = False
    fence = CompressionCommitFence()

    class _CancellingTransport:
        def preflight_kwargs(self, kwargs, **_options):
            assert fence.cancel_before_commit() is True
            if raise_after_cancel:
                raise RuntimeError("SECRET_PREFLIGHT_FAILURE")
            return kwargs

    agent._get_transport = lambda: _CancellingTransport()

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate"
    ) as endpoint:
        compress_context(
            agent,
            _messages(),
            "sys",
            force=True,
            commit_fence=fence,
        )

    assert agent.context_compressor.text_calls == 0
    assert agent.context_compressor.record_calls == []
    assert agent._last_native_compaction_succeeded is False
    endpoint.assert_not_called()


def test_cancellation_during_successful_preflight_skips_endpoint_and_text_fallback():
    _assert_cancellation_during_preflight_aborts(raise_after_cancel=False)


def test_cancellation_during_failed_preflight_skips_endpoint_and_text_fallback():
    _assert_cancellation_during_preflight_aborts(raise_after_cancel=True)


def test_lease_loss_during_ineligible_policy_aborts_without_text_fallback():
    agent = _Agent()
    agent.context_compressor.raise_on_text = False

    class _LeaseLosingPolicy:
        def is_eligible(self, **_kwargs):
            agent._session_db.holder = "replacement-holder"
            return False

    agent.native_compaction_policy = _LeaseLosingPolicy()

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate"
    ) as endpoint:
        compress_context(agent, _messages(), "sys", force=True)

    assert agent.context_compressor.text_calls == 0
    assert agent.context_compressor.record_calls == []
    endpoint.assert_not_called()


def test_identity_construction_exception_uses_owned_text_fallback_once():
    agent = _Agent()
    agent.context_compressor.raise_on_text = False

    with (
        patch(
            "agent.codex_responses_adapter._classify_responses_issuer",
            side_effect=RuntimeError("SECRET_IDENTITY_FAILURE"),
        ),
        patch(
            "agent.native_openai_compaction.request_native_compaction_candidate"
        ) as endpoint,
    ):
        compress_context(agent, _messages(), "sys", force=True)

    assert agent.context_compressor.text_calls == 1
    assert agent.context_compressor.record_calls == []
    endpoint.assert_not_called()


def test_endpoint_helper_exception_uses_owned_text_fallback_once():
    agent = _Agent()
    agent.context_compressor.raise_on_text = False

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate",
        side_effect=RuntimeError("SECRET_ENDPOINT_HELPER_FAILURE"),
    ):
        compress_context(agent, _messages(), "sys", force=True)

    assert agent.context_compressor.text_calls == 1
    assert agent.context_compressor.record_calls == []


def test_checkpoint_construction_exception_uses_owned_text_fallback_once():
    agent = _Agent()
    agent.context_compressor.raise_on_text = False

    def _candidate(_agent, *, cut, **_kwargs):
        return NativeCompactionCandidate(
            source_input_item_count=cut.source_input_item_count,
            source_input_sha256=cut.source_input_sha256,
            compact_response_id=None,
            compact_created_at=None,
            input_item_count=cut.source_input_item_count,
            output_item_count=1,
            output=[{"type": "compaction", "encrypted_content": "OPAQUE"}],
        )

    with (
        patch(
            "agent.native_openai_compaction.request_native_compaction_candidate",
            side_effect=_candidate,
        ),
        patch(
            "agent.native_openai_compaction.checkpoint_from_candidate",
            side_effect=RuntimeError("SECRET_CHECKPOINT_FAILURE"),
        ),
    ):
        compress_context(agent, _messages(), "sys", force=True)

    assert agent.context_compressor.text_calls == 1
    assert agent.context_compressor.record_calls == []
    assert agent._session_db.events == []


def test_cancellation_during_request_client_creation_skips_endpoint_and_text():
    agent = _Agent()
    agent.context_compressor.raise_on_text = False
    fence = CompressionCommitFence()

    def _guarded_request(_agent, *, pre_dispatch_check, **_kwargs):
        assert fence.cancel_before_commit() is True
        assert pre_dispatch_check() is False
        return NativeCompactionFailure("client", False, True)

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate",
        side_effect=_guarded_request,
    ):
        compress_context(
            agent,
            _messages(),
            "sys",
            force=True,
            commit_fence=fence,
        )

    assert agent.context_compressor.text_calls == 0
    assert agent.context_compressor.record_calls == []
    assert agent._session_db.events == []
    assert agent._last_native_compaction_succeeded is False


def test_external_progress_failure_after_durable_checkpoint_remains_native_success(caplog):
    agent = _Agent()
    caplog.set_level("DEBUG", logger="agent.conversation_compression")
    secret_error = type("SECRET_RECORD_EXCEPTION_CLASS", (RuntimeError,), {})

    def _failed_record(**_kwargs):
        agent._session_db.events.append("record")
        raise secret_error()

    agent.context_compressor.record_external_compaction = _failed_record

    def _candidate(_agent, *, cut, **_kwargs):
        return NativeCompactionCandidate(
            source_input_item_count=cut.source_input_item_count,
            source_input_sha256=cut.source_input_sha256,
            compact_response_id=None,
            compact_created_at=None,
            input_item_count=cut.source_input_item_count,
            output_item_count=1,
            output=[{"type": "compaction", "encrypted_content": "OPAQUE"}],
        )

    with patch(
        "agent.native_openai_compaction.request_native_compaction_candidate",
        side_effect=_candidate,
    ):
        returned, _ = compress_context(agent, _messages(), "sys", force=True)

    assert returned == _messages()
    assert agent._session_db.checkpoint is not None
    assert agent._session_db.events == ["upsert", "record"]
    assert agent.context_compressor.text_calls == 0
    assert agent._last_native_compaction_succeeded is True
    assert "SECRET_RECORD_EXCEPTION_CLASS" not in "\n".join(
        record.getMessage() for record in caplog.records
    )
