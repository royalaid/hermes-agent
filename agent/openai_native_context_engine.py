"""Built-in OpenAI native Responses compaction context engine.

The engine inherits Hermes' textual compressor as its fail-open fallback while
selecting provider-native opaque compaction as the primary strategy on the
strictly supported first-party OpenAI Responses routes.
"""

from __future__ import annotations

from agent.context_compressor import ContextCompressor


DEFAULT_OPENAI_NATIVE_KEEP_RECENT_TOKENS = 20_000


def validate_native_keep_recent_tokens(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("native_keep_recent_tokens must be a positive integer")
    return value


class OpenAINativeContextEngine(ContextCompressor):
    """Native-first OpenAI compaction with inherited textual fallback."""

    def __init__(
        self,
        *args,
        native_keep_recent_tokens: int = DEFAULT_OPENAI_NATIVE_KEEP_RECENT_TOKENS,
        **kwargs,
    ) -> None:
        native_keep_recent_tokens = validate_native_keep_recent_tokens(
            native_keep_recent_tokens
        )
        super().__init__(*args, **kwargs)
        self.native_keep_recent_tokens = native_keep_recent_tokens

    @property
    def name(self) -> str:
        return "openai-native"
