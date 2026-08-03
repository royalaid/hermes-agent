"""Regression: detect compression progress by tokens, rows, or native replay.

Issue #39548: preflight compression in the turn prologue was checking
``len(messages) >= _orig_len`` to decide "Cannot compress further". This
false-positives when a pass summarises message contents — reducing the
estimated request token count without removing any rows — and surfaces a
spurious ``Context length exceeded`` failure followed by an auto-reset of
an otherwise healthy session.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.conversation_compression import (
    compression_attempt_made_progress,
    conversation_history_after_compression,
)
from agent.turn_context import (
    _compression_made_progress,
    _compression_warrants_another_preflight_pass,
)


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
