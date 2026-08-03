"""Regression: detect compression progress by tokens, rows, or native replay.

Issue #39548: preflight compression in the turn prologue was checking
``len(messages) >= _orig_len`` to decide "Cannot compress further". This
false-positives when a pass summarises message contents — reducing the
estimated request token count without removing any rows — and surfaces a
spurious ``Context length exceeded`` failure followed by an auto-reset of
an otherwise healthy session.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import Context
from types import SimpleNamespace

from agent.conversation_compression import (
    _begin_compression_attempt_outcome,
    _mark_native_compression_attempt_succeeded,
    capture_compression_attempt_outcome,
    compression_attempt_made_progress,
    conversation_history_after_compression,
)
from agent.turn_context import (
    _compression_made_progress,
    _compression_warrants_another_preflight_pass,
)
from tools.thread_context import propagate_context_to_thread


class TestCompressionMadeProgress:
    def test_rows_reduced_counts_as_progress(self):
        assert _compression_made_progress(
            orig_len=10, new_len=5, orig_tokens=1000, new_tokens=1000
        ) is True

    def test_neither_moved_means_no_progress(self):
        assert _compression_made_progress(
            orig_len=10, new_len=10, orig_tokens=1000, new_tokens=1000
        ) is False

    def test_sub_5pct_token_drop_is_not_progress(self):
        assert _compression_made_progress(
            orig_len=10, new_len=10, orig_tokens=1000, new_tokens=970
        ) is False
        assert _compression_made_progress(
            orig_len=10, new_len=10, orig_tokens=1000, new_tokens=940
        ) is True

    def test_native_success_counts_as_progress_without_transcript_changes(self):
        agent = SimpleNamespace(_last_native_compaction_succeeded=True)

        assert compression_attempt_made_progress(
            agent,
            before_count=4,
            after_count=4,
            before_tokens=1_000,
            after_tokens=1_000,
        )

    def test_failed_native_and_textual_noop_do_not_count_as_progress(self):
        agent = SimpleNamespace(_last_native_compaction_succeeded=False)

        assert not compression_attempt_made_progress(
            agent,
            before_count=4,
            after_count=4,
            before_tokens=1_000,
            after_tokens=950,
        )

    def test_valid_context_tier_reduction_counts_as_progress(self):
        assert compression_attempt_made_progress(
            SimpleNamespace(_last_native_compaction_succeeded=False),
            before_count=4,
            after_count=4,
            before_tokens=1_000,
            after_tokens=1_000,
            old_context_length=200_000,
            new_context_length=128_000,
        )

    def test_invalid_context_tier_values_do_not_count_as_progress(self):
        assert not compression_attempt_made_progress(
            SimpleNamespace(_last_native_compaction_succeeded=False),
            before_count=4,
            after_count=4,
            before_tokens=1_000,
            after_tokens=1_000,
            old_context_length=-1,
            new_context_length=-2,
        )

    def test_interleaved_attempts_use_context_local_native_outcome(self):
        agent = SimpleNamespace(_last_native_compaction_succeeded=False)
        native_context = Context()
        noop_context = Context()

        native_context.run(_begin_compression_attempt_outcome, agent)
        native_context.run(_mark_native_compression_attempt_succeeded, agent)
        native_outcome = native_context.run(
            capture_compression_attempt_outcome, agent
        )

        # A later no-op attempt overwrites the legacy shared signal before the
        # native caller evaluates progress, reproducing the reported race.
        noop_context.run(_begin_compression_attempt_outcome, agent)
        agent._last_native_compaction_succeeded = False
        noop_outcome = noop_context.run(capture_compression_attempt_outcome, agent)

        assert compression_attempt_made_progress(
            agent,
            before_count=4,
            after_count=4,
            before_tokens=1_000,
            after_tokens=1_000,
            attempt_outcome=native_outcome,
        )
        assert not compression_attempt_made_progress(
            agent,
            before_count=4,
            after_count=4,
            before_tokens=1_000,
            after_tokens=1_000,
            attempt_outcome=noop_outcome,
        )

    def test_native_outcome_crosses_progress_timeout_worker_boundary(self):
        agent = SimpleNamespace(_last_native_compaction_succeeded=False)
        _begin_compression_attempt_outcome(agent)

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(
                propagate_context_to_thread(
                    lambda: _mark_native_compression_attempt_succeeded(agent)
                )
            ).result()

        outcome = capture_compression_attempt_outcome(agent)
        agent._last_native_compaction_succeeded = False
        assert compression_attempt_made_progress(
            agent,
            before_count=4,
            after_count=4,
            before_tokens=1_000,
            after_tokens=1_000,
            attempt_outcome=outcome,
        )
        assert "_last_native_compaction_succeeded" not in repr(outcome)


class TestCompressionWarrantsAnotherPreflightPass:
    def test_material_reduction_above_threshold_allows_another_pass(self):
        assert _compression_warrants_another_preflight_pass(
            orig_tokens=400_000,
            new_tokens=350_000,
            threshold_tokens=272_000,
        ) is True

    def test_marginal_reduction_above_threshold_stops(self):
        assert _compression_warrants_another_preflight_pass(
            orig_tokens=350_000,
            new_tokens=345_000,
            threshold_tokens=272_000,
        ) is False


def test_native_only_success_preserves_previous_gateway_flush_baseline():
    previous = [{"role": "user", "content": "persisted"}]
    messages = list(previous)
    agent = SimpleNamespace(
        _last_native_compaction_succeeded=True,
        _last_compression_attempt_recorded=True,
        _last_compression_attempt_in_place=None,
        _last_compaction_in_place=False,
    )

    assert (
        conversation_history_after_compression(agent, messages, previous) is previous
    )
