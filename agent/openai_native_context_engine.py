"""Built-in OpenAI native Responses compaction context engine.

The engine inherits Hermes' textual compressor as its fail-open fallback while
selecting provider-native opaque compaction as the primary strategy on the
strictly supported first-party OpenAI Responses routes.
"""

from __future__ import annotations

from agent.context_compressor import ContextCompressor


DEFAULT_OPENAI_NATIVE_KEEP_RECENT_TOKENS = 20_000


class OpenAINativeContextEngine(ContextCompressor):
    """Native-first OpenAI compaction with inherited textual fallback."""

    def __init__(
        self,
        *args,
        native_keep_recent_tokens: int = DEFAULT_OPENAI_NATIVE_KEEP_RECENT_TOKENS,
        **kwargs,
    ) -> None:
        if (
            type(native_keep_recent_tokens) is not int
            or native_keep_recent_tokens <= 0
        ):
            raise ValueError("native_keep_recent_tokens must be a positive integer")
        super().__init__(*args, **kwargs)
        self.native_keep_recent_tokens = native_keep_recent_tokens

    @property
    def name(self) -> str:
        return "openai-native"
