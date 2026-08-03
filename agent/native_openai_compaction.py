"""Opaque native OpenAI compaction checkpoint primitives.

This module owns only immutable identity/checkpoint data and pure prefix
projection. Transport, persistence, and orchestration belong elsewhere.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from agent.backend_identity import BackendIdentity
from hermes_cli.route_identity import normalize_route_base_url


def _validate_json_value(value: Any, active_containers: set[int] | None = None) -> None:
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if math.isfinite(value):
            return
        raise ValueError("value must contain only JSON-compatible data")
    if type(value) not in (list, dict):
        raise ValueError("value must contain only JSON-compatible data")

    active_containers = active_containers or set()
    container_id = id(value)
    if container_id in active_containers:
        raise ValueError("value must contain only JSON-compatible data")
    active_containers.add(container_id)
    try:
        if type(value) is list:
            for item in value:
                _validate_json_value(item, active_containers)
        else:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("value must contain only JSON-compatible data")
                _validate_json_value(item, active_containers)
    finally:
        active_containers.remove(container_id)


def _canonical_json_bytes(value: Any) -> bytes:
    _validate_json_value(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ValueError("value must contain only JSON-compatible data") from None
    return encoded.encode("utf-8")


def canonical_input_sha256(items: list[dict]) -> str:
    """Hash canonical UTF-8 JSON while preserving input-list order."""
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError("input must be a JSON list of objects")
    return hashlib.sha256(_canonical_json_bytes(items)).hexdigest()


@dataclass(frozen=True, init=False)
class NativeCompactionCut:
    """Payload-safe immutable description of an ordinary-input prefix."""

    message_count: int
    source_input_item_count: int
    source_input_sha256: str
    _source_input_json: str = field(repr=False, compare=True)

    def __init__(
        self,
        *,
        message_count: int,
        source_input: list[dict],
        source_input_item_count: int,
        source_input_sha256: str,
    ) -> None:
        if (
            not isinstance(message_count, int)
            or isinstance(message_count, bool)
            or message_count <= 0
        ):
            raise ValueError("message_count must be a positive integer")
        if not isinstance(source_input, list) or not source_input:
            raise ValueError("source_input must be a non-empty JSON list of objects")
        if (
            not isinstance(source_input_item_count, int)
            or isinstance(source_input_item_count, bool)
            or source_input_item_count != len(source_input)
        ):
            raise ValueError("source_input_item_count must match source input length")
        try:
            source_input_json = _canonical_json_bytes(source_input).decode("utf-8")
            actual_sha256 = canonical_input_sha256(source_input)
        except ValueError:
            raise ValueError(
                "source_input must be a non-empty JSON list of objects"
            ) from None
        if not all(isinstance(item, dict) for item in source_input):
            raise ValueError("source_input must be a non-empty JSON list of objects")
        if source_input_sha256 != actual_sha256:
            raise ValueError("source_input_sha256 must match source input")

        object.__setattr__(self, "message_count", message_count)
        object.__setattr__(
            self, "source_input_item_count", source_input_item_count
        )
        object.__setattr__(self, "source_input_sha256", source_input_sha256)
        object.__setattr__(self, "_source_input_json", source_input_json)

    @property
    def source_input(self) -> list[dict]:
        """Return a fresh decode so callers cannot mutate the held prefix."""
        return json.loads(self._source_input_json)


def _tool_atomic_prefix(messages: list[dict]) -> bool:
    """Return whether a message prefix contains only completed tool groups."""
    pending: set[str] = set()
    for message in messages:
        role = message.get("role")
        tool_calls = message.get("tool_calls") if role == "assistant" else None
        if isinstance(tool_calls, list) and tool_calls:
            if pending:
                return False
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    return False
                call_id = tool_call.get("call_id")
                if not isinstance(call_id, str) or not call_id.strip():
                    call_id = tool_call.get("id")
                if not isinstance(call_id, str) or not call_id.strip():
                    return False
                call_id = call_id.strip()
                if call_id in pending:
                    return False
                pending.add(call_id)
            continue

        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id.strip() not in pending:
                return False
            pending.remove(call_id.strip())
        elif pending:
            return False
    return not pending


def select_native_compaction_cut(
    messages: list[dict],
    *,
    protect_last_n: int,
    serialize_input: Callable[[list[dict]], list[dict]],
    previous_source_input_item_count: int = 0,
) -> NativeCompactionCut | None:
    """Select the latest protected, tool-atomic ordinary-input prefix."""
    if (
        not isinstance(messages, list)
        or not messages
        or not all(isinstance(message, dict) for message in messages)
        or not isinstance(protect_last_n, int)
        or isinstance(protect_last_n, bool)
        or protect_last_n < 0
        or not isinstance(previous_source_input_item_count, int)
        or isinstance(previous_source_input_item_count, bool)
        or previous_source_input_item_count < 0
        or not callable(serialize_input)
    ):
        return None

    try:
        from agent.conversation_compression import _is_real_user_message

        newest_real_user_index = next(
            index
            for index in range(len(messages) - 1, -1, -1)
            if _is_real_user_message(messages[index])
        )
        max_cut = min(len(messages) - protect_last_n, newest_real_user_index)
        if max_cut <= 0:
            return None

        full_input = serialize_input(copy.deepcopy(messages))
        if (
            not isinstance(full_input, list)
            or not full_input
            or not all(isinstance(item, dict) for item in full_input)
        ):
            return None
        canonical_input_sha256(full_input)

        for message_count in range(max_cut, 0, -1):
            prefix_messages = messages[:message_count]
            if not _tool_atomic_prefix(prefix_messages):
                continue
            if messages[message_count].get("role") == "tool":
                continue

            source_input = serialize_input(copy.deepcopy(prefix_messages))
            if (
                not isinstance(source_input, list)
                or not source_input
                or not all(isinstance(item, dict) for item in source_input)
            ):
                continue
            source_input_item_count = len(source_input)
            if source_input_item_count <= previous_source_input_item_count:
                continue
            canonical_input_sha256(source_input)
            if (
                source_input_item_count >= len(full_input)
                or full_input[:source_input_item_count] != source_input
            ):
                continue

            return NativeCompactionCut(
                message_count=message_count,
                source_input=source_input,
                source_input_item_count=source_input_item_count,
                source_input_sha256=canonical_input_sha256(source_input),
            )
    except Exception:
        return None
    return None


def _normalize_label(value: str | None) -> str:
    return (value or "").strip().lower()


def _safe_base_url_host(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        parsed.port
    except (TypeError, ValueError):
        return ""
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or not host
        or any(character.isspace() or ord(character) < 32 for character in host)
    ):
        return ""
    return host


@dataclass(frozen=True)
class NativeCompactionPolicy:
    """Payload-safe, fail-closed eligibility gates for native compaction.

    The policy stores only booleans established at agent initialization.  The
    client and effective route are supplied to each check so provider fallback
    or model switching cannot leave eligibility pinned to stale route state.
    """

    feature_enabled: bool = False
    built_in_compressor: bool = False
    has_session_state: bool = False

    @classmethod
    def from_runtime(
        cls,
        *,
        feature_enabled: Any,
        context_compressor: Any,
        session_db: Any,
        session_id: Any,
        session_state_bound: Any,
    ) -> "NativeCompactionPolicy":
        from agent.context_compressor import ContextCompressor

        try:
            checkpoint_capable = all(
                callable(getattr(session_db, method, None))
                for method in (
                    "load_native_openai_checkpoint",
                    "upsert_native_openai_checkpoint",
                    "delete_native_openai_checkpoint",
                )
            )
        except Exception:
            checkpoint_capable = False

        return cls(
            feature_enabled=feature_enabled is True,
            built_in_compressor=type(context_compressor) is ContextCompressor,
            has_session_state=(
                session_state_bound is True
                and checkpoint_capable
                and type(session_id) is str
                and bool(session_id.strip())
            ),
        )

    def is_eligible(
        self,
        *,
        client: Any,
        provider: Any,
        api_mode: Any,
        base_url: Any,
    ) -> bool:
        """Evaluate the current client and effective route without retaining them."""
        if not (
            self.feature_enabled
            and self.built_in_compressor
            and self.has_session_state
        ):
            return False
        if not all(type(value) is str for value in (provider, api_mode, base_url)):
            return False

        try:
            if api_mode.strip().lower() != "codex_responses":
                return False
            compact = getattr(getattr(client, "responses", None), "compact", None)
            if not callable(compact):
                return False

            parsed = urlsplit(base_url)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                return False
            route = normalize_route_base_url(base_url)
            provider_id = provider.strip().lower()
        except Exception:
            return False

        return (provider_id, route) in {
            ("openai", "https://api.openai.com/v1"),
            ("openai-codex", "https://chatgpt.com/backend-api/codex"),
        }


@dataclass(frozen=True)
class NativeCompactionIdentity:
    """Normalized route identity that constrains opaque checkpoint replay."""

    provider: str = ""
    api_mode: str = ""
    model: str = ""
    base_url: str = field(default="", repr=False)
    issuer_kind: str = ""
    credential_scope: str = field(default="", repr=False)
    replay_encrypted_reasoning: bool = False

    def __post_init__(self) -> None:
        backend = BackendIdentity.build(
            provider=self.provider,
            model=self.model,
            base_url=self.base_url,
        )
        object.__setattr__(self, "provider", backend.provider)
        object.__setattr__(self, "model", backend.model)
        object.__setattr__(self, "base_url", backend.base_url)
        object.__setattr__(self, "api_mode", _normalize_label(self.api_mode))
        object.__setattr__(self, "issuer_kind", _normalize_label(self.issuer_kind))
        object.__setattr__(
            self, "credential_scope", _normalize_label(self.credential_scope)
        )
        object.__setattr__(
            self,
            "replay_encrypted_reasoning",
            bool(self.replay_encrypted_reasoning),
        )


@dataclass(frozen=True, init=False)
class NativeCompactionCheckpoint:
    """Immutable, payload-safe snapshot of one native compaction generation."""

    session_id: str
    identity: NativeCompactionIdentity
    source_input_item_count: int
    source_input_sha256: str
    compact_response_id: str | None
    compact_created_at: float | None
    input_item_count: int
    output_item_count: int
    generation: int
    created_at: float
    updated_at: float
    _output_json: str = field(repr=False, compare=True)

    def __init__(
        self,
        *,
        session_id: str,
        identity: NativeCompactionIdentity,
        source_input_item_count: int,
        source_input_sha256: str,
        output: list[dict],
        compact_response_id: str | None,
        compact_created_at: float | None,
        input_item_count: int,
        output_item_count: int,
        generation: int,
        created_at: float,
        updated_at: float,
    ) -> None:
        for name, value in (
            ("source_input_item_count", source_input_item_count),
            ("input_item_count", input_item_count),
            ("output_item_count", output_item_count),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
            raise ValueError("generation must be a positive integer")
        if compact_created_at is not None and (
            type(compact_created_at) not in (int, float)
            or not math.isfinite(compact_created_at)
            or compact_created_at < 0
        ):
            raise ValueError(
                "compact_created_at must be a non-negative finite timestamp"
            )
        for name, value in (("created_at", created_at), ("updated_at", updated_at)):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative finite timestamp")
        if not isinstance(output, list) or not output:
            raise ValueError("output must be a non-empty JSON list")
        try:
            output_json = _canonical_json_bytes(output).decode("utf-8")
        except ValueError:
            raise ValueError("output must be a non-empty JSON list") from None
        if output_item_count != len(output):
            raise ValueError("output_item_count must match output length")

        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "identity", identity)
        object.__setattr__(
            self, "source_input_item_count", source_input_item_count
        )
        object.__setattr__(self, "source_input_sha256", source_input_sha256)
        object.__setattr__(self, "compact_response_id", compact_response_id)
        object.__setattr__(self, "compact_created_at", compact_created_at)
        object.__setattr__(self, "input_item_count", input_item_count)
        object.__setattr__(self, "output_item_count", output_item_count)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "_output_json", output_json)

    @property
    def output(self) -> list[dict]:
        """Return a fresh decode so cached state cannot be mutated by callers."""
        return json.loads(self._output_json)

    @property
    def output_json(self) -> str:
        """Return the validated canonical JSON representation for persistence."""
        return self._output_json

    def redacted_metadata(self) -> dict[str, Any]:
        """Return operational metadata without opaque or transcript payloads."""
        return {
            "provider": self.identity.provider,
            "api_mode": self.identity.api_mode,
            "model": self.identity.model,
            "base_url_host": _safe_base_url_host(self.identity.base_url),
            "issuer_kind": self.identity.issuer_kind,
            "credential_scope_present": bool(self.identity.credential_scope),
            "replay_encrypted_reasoning": self.identity.replay_encrypted_reasoning,
            "source_input_item_count": self.source_input_item_count,
            "source_input_sha256": self.source_input_sha256[:12],
            "input_item_count": self.input_item_count,
            "output_item_count": self.output_item_count,
            "generation": self.generation,
            "compact_response_id": self.compact_response_id,
            "compact_created_at": self.compact_created_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def checkpoint_matches(
    identity: NativeCompactionIdentity,
    checkpoint: NativeCompactionCheckpoint,
    ordinary_input: list[dict],
) -> bool:
    """Return whether route identity and ordinary source prefix still match."""
    if identity != checkpoint.identity:
        return False
    count = checkpoint.source_input_item_count
    if len(ordinary_input) < count:
        return False
    try:
        return (
            canonical_input_sha256(ordinary_input[:count])
            == checkpoint.source_input_sha256
        )
    except ValueError:
        return False


def apply_checkpoint(
    checkpoint: NativeCompactionCheckpoint, ordinary_input: list[dict]
) -> list[dict]:
    """Project opaque output plus an isolated copy of the ordinary live tail."""
    count = checkpoint.source_input_item_count
    if len(ordinary_input) < count:
        raise ValueError("ordinary input does not match checkpoint prefix")
    if canonical_input_sha256(ordinary_input[:count]) != checkpoint.source_input_sha256:
        raise ValueError("ordinary input does not match checkpoint prefix")
    return checkpoint.output + copy.deepcopy(ordinary_input[count:])
