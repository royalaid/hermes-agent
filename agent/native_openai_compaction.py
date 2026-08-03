"""Opaque native OpenAI compaction identities, projection, and requests.

This module owns immutable checkpoint data, strict prefix/output validation,
route policy, and the isolated request-client lifecycle. Durable persistence
and transcript orchestration remain in their existing state/compression seams.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from agent.backend_identity import BackendIdentity
from hermes_cli.route_identity import normalize_route_base_url


MAX_NATIVE_COMPACTION_OUTPUT_ITEMS = 512
MAX_NATIVE_COMPACTION_OUTPUT_DEPTH = 64
MAX_NATIVE_COMPACTION_OUTPUT_JSON_BYTES = 4 * 1024 * 1024


def _validate_json_value(
    value: Any,
    active_containers: set[int] | None = None,
    *,
    depth: int = 0,
    max_depth: int | None = None,
) -> None:
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if math.isfinite(value):
            return
        raise ValueError("value must contain only JSON-compatible data")
    if type(value) not in (list, dict):
        raise ValueError("value must contain only JSON-compatible data")
    if max_depth is not None and depth > max_depth:
        raise ValueError("value must contain only JSON-compatible data")

    if active_containers is None:
        active_containers = set()
    container_id = id(value)
    if container_id in active_containers:
        raise ValueError("value must contain only JSON-compatible data")
    active_containers.add(container_id)
    try:
        if type(value) is list:
            for item in value:
                _validate_json_value(
                    item,
                    active_containers,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
        else:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("value must contain only JSON-compatible data")
                _validate_json_value(
                    item,
                    active_containers,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
    finally:
        active_containers.remove(container_id)


def _canonical_json_bytes(value: Any, *, max_depth: int | None = None) -> bytes:
    _validate_json_value(value, max_depth=max_depth)
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


def _bounded_compaction_output_json(output: Any) -> str:
    if (
        type(output) is not list
        or not output
        or len(output) > MAX_NATIVE_COMPACTION_OUTPUT_ITEMS
    ):
        raise ValueError("output must be a bounded non-empty JSON list")
    encoded = _canonical_json_bytes(
        output,
        max_depth=MAX_NATIVE_COMPACTION_OUTPUT_DEPTH,
    )
    if len(encoded) > MAX_NATIVE_COMPACTION_OUTPUT_JSON_BYTES:
        raise ValueError("output must be a bounded non-empty JSON list")
    return encoded.decode("utf-8")


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


@dataclass(frozen=True, init=False)
class NativeCompactionCandidate:
    """Validated native compact output awaiting durable checkpoint creation."""

    source_input_item_count: int
    source_input_sha256: str
    compact_response_id: str | None
    compact_created_at: float | None
    input_item_count: int
    output_item_count: int
    _output_json: str = field(repr=False, compare=True)

    def __init__(
        self,
        *,
        source_input_item_count: int,
        source_input_sha256: str,
        compact_response_id: str | None,
        compact_created_at: float | None,
        input_item_count: int,
        output_item_count: int,
        output: list[Any],
    ) -> None:
        for name, value in (
            ("source_input_item_count", source_input_item_count),
            ("input_item_count", input_item_count),
            ("output_item_count", output_item_count),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if (
            type(source_input_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", source_input_sha256) is None
        ):
            raise ValueError("source_input_sha256 must be a lowercase SHA-256 digest")
        if compact_response_id is not None and (
            type(compact_response_id) is not str or not compact_response_id.strip()
        ):
            raise ValueError("compact_response_id must be a non-empty plain string")
        if compact_created_at is not None and (
            type(compact_created_at) not in (int, float)
            or not math.isfinite(compact_created_at)
            or compact_created_at < 0
        ):
            raise ValueError(
                "compact_created_at must be a non-negative finite timestamp"
            )
        try:
            output_json = _bounded_compaction_output_json(output)
        except ValueError:
            raise ValueError("output must be a bounded non-empty JSON list") from None
        if output_item_count != len(output):
            raise ValueError("output_item_count must match output length")

        object.__setattr__(self, "source_input_item_count", source_input_item_count)
        object.__setattr__(self, "source_input_sha256", source_input_sha256)
        object.__setattr__(self, "compact_response_id", compact_response_id)
        object.__setattr__(self, "compact_created_at", compact_created_at)
        object.__setattr__(self, "input_item_count", input_item_count)
        object.__setattr__(self, "output_item_count", output_item_count)
        object.__setattr__(self, "_output_json", output_json)

    @property
    def output(self) -> list[Any]:
        """Return a fresh decode so callers cannot mutate candidate output."""
        return json.loads(self._output_json)


@dataclass(frozen=True)
class NativeCompactionFailure:
    """Payload-free classification directing the caller to textual fallback."""

    classification: str
    retryable: bool
    use_textual_fallback: bool

    def __post_init__(self) -> None:
        if self.classification not in {
            "auth",
            "unsupported",
            "timeout",
            "network",
            "invalid_response",
            "client",
        }:
            raise ValueError("classification must be a supported stable category")
        if type(self.retryable) is not bool:
            raise ValueError("retryable must be a boolean")
        if self.use_textual_fallback is not True:
            raise ValueError("use_textual_fallback must be true")


def _tool_atomic_prefix(messages: list[dict]) -> bool:
    """Return whether a message prefix contains only completed tool groups."""
    pending: set[str] = set()
    for message in messages:
        if type(message) is not dict:
            return False
        role = message.get("role")
        if type(role) is not str or role not in {"system", "user", "assistant", "tool"}:
            return False
        if "api_content" in message and message["api_content"] is not None:
            if type(message["api_content"]) is not str:
                return False
        if role == "assistant":
            for sidecar in ("codex_reasoning_items", "codex_message_items"):
                if sidecar in message and message[sidecar] is not None:
                    if type(message[sidecar]) is not list:
                        return False
        tool_calls = message.get("tool_calls") if role == "assistant" else None
        if tool_calls is not None and not isinstance(tool_calls, list):
            return False
        if tool_calls:
            if pending:
                return False
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    return False
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    return False
                function_name = function.get("name")
                if type(function_name) is not str or not function_name.strip():
                    return False
                call_id = tool_call.get("call_id")
                if type(call_id) is not str or not call_id.strip():
                    call_id = tool_call.get("id")
                if type(call_id) is not str or not call_id.strip():
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


def _content_part_is_valid(part: Any, *, text_type: str) -> bool:
    if type(part) is not dict:
        return False
    part_type = part.get("type")
    if part_type == text_type:
        return set(part) == {"type", "text"} and type(part.get("text")) is str
    if part_type == "input_image":
        if set(part) not in (
            {"type", "image_url"},
            {"type", "image_url", "detail"},
        ):
            return False
        image_url = part.get("image_url")
        if type(image_url) is not str or not image_url.strip():
            return False
        return "detail" not in part or type(part["detail"]) is str
    return False


def _content_is_valid(content: Any, *, text_type: str) -> bool:
    if type(content) is str:
        return True
    return type(content) is list and all(
        _content_part_is_valid(part, text_type=text_type) for part in content
    )


def _finalized_item_schema_is_valid(item: Any) -> bool:
    if type(item) is not dict:
        return False

    item_type = item.get("type")
    if item_type == "function_call":
        return (
            set(item) == {"type", "call_id", "name", "arguments"}
            and type(item.get("call_id")) is str
            and bool(item["call_id"].strip())
            and type(item.get("name")) is str
            and bool(item["name"].strip())
            and type(item.get("arguments")) is str
        )
    if item_type == "function_call_output":
        return (
            set(item) == {"type", "call_id", "output"}
            and type(item.get("call_id")) is str
            and bool(item["call_id"].strip())
            and _content_is_valid(item.get("output"), text_type="input_text")
        )
    if item_type == "reasoning":
        summary = item.get("summary")
        return (
            set(item) == {"type", "encrypted_content", "summary"}
            and type(item.get("encrypted_content")) is str
            and bool(item["encrypted_content"].strip())
            and type(summary) is list
            and all(
                type(entry) is dict
                and set(entry) == {"type", "text"}
                and entry.get("type") == "summary_text"
                and type(entry.get("text")) is str
                for entry in summary
            )
        )
    if item_type == "message":
        allowed_keys = {"type", "role", "status", "content", "id", "phase"}
        required_keys = {"type", "role", "status", "content"}
        content = item.get("content")
        return (
            required_keys <= set(item) <= allowed_keys
            and item.get("role") == "assistant"
            and type(item.get("role")) is str
            and type(item.get("status")) is str
            and bool(item["status"].strip())
            and type(content) is list
            and bool(content)
            and all(
                type(part) is dict
                and set(part) == {"type", "text"}
                and part.get("type") == "output_text"
                and type(part.get("text")) is str
                for part in content
            )
            and all(
                key not in item
                or (type(item[key]) is str and bool(item[key].strip()))
                for key in ("id", "phase")
            )
        )
    if "type" in item:
        return False

    role = item.get("role")
    if type(role) is not str or role not in {"user", "assistant"}:
        return False
    return set(item) == {"role", "content"} and _content_is_valid(
        item.get("content"),
        text_type="input_text" if role == "user" else "output_text",
    )


def _finalized_tool_graph_is_valid(items: list[dict]) -> bool:
    """Validate exact finalized Responses schemas and completed call relationships."""
    pending: set[str] = set()
    seen_calls: set[str] = set()
    for item in items:
        if not _finalized_item_schema_is_valid(item):
            return False
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = item["call_id"].strip()
            if call_id in seen_calls:
                return False
            seen_calls.add(call_id)
            pending.add(call_id)
            continue

        if item_type == "function_call_output":
            call_id = item["call_id"].strip()
            if call_id not in pending:
                return False
            pending.remove(call_id)
            continue

        if pending:
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
        or not all(type(message) is dict for message in messages)
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
        if not _tool_atomic_prefix(messages):
            return None

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
        if not _finalized_tool_graph_is_valid(full_input):
            return None

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
            if not _finalized_tool_graph_is_valid(source_input):
                continue
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
        try:
            output_json = _bounded_compaction_output_json(output)
        except ValueError:
            raise ValueError("output must be a bounded non-empty JSON list") from None
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

    def redacted_metadata(
        self,
        *,
        tail_item_count: int | None = None,
        elapsed_ms: int | None = None,
    ) -> dict[str, Any]:
        """Return the narrow payload-free lifecycle metadata contract."""
        metadata: dict[str, Any] = {
            "strategy": "openai_native",
            "provider": self.identity.provider,
            "api_mode": self.identity.api_mode,
            "model": self.identity.model,
            "base_url_host": _safe_base_url_host(self.identity.base_url),
            "input_item_count": self.input_item_count,
            "output_item_count": self.output_item_count,
            "generation": self.generation,
            "prefix_sha256": self.source_input_sha256[:12],
        }
        if type(tail_item_count) is int and tail_item_count >= 0:
            metadata["tail_item_count"] = tail_item_count
        if type(elapsed_ms) is int and elapsed_ms >= 0:
            metadata["elapsed_ms"] = elapsed_ms
        return metadata


def checkpoint_from_candidate(
    *,
    candidate: NativeCompactionCandidate,
    session_id: str,
    identity: NativeCompactionIdentity,
    previous_checkpoint: NativeCompactionCheckpoint | None,
    now: float,
) -> NativeCompactionCheckpoint:
    """Freeze a validated candidate as the next immutable checkpoint generation."""
    if type(candidate) is not NativeCompactionCandidate:
        raise ValueError("candidate must be a native compaction candidate")
    if type(identity) is not NativeCompactionIdentity:
        raise ValueError("identity must be a native compaction identity")
    matching_previous = (
        type(previous_checkpoint) is NativeCompactionCheckpoint
        and previous_checkpoint.session_id == session_id
        and previous_checkpoint.identity == identity
    )
    generation = previous_checkpoint.generation + 1 if matching_previous else 1
    created_at = previous_checkpoint.created_at if matching_previous else now
    return NativeCompactionCheckpoint(
        session_id=session_id,
        identity=identity,
        source_input_item_count=candidate.source_input_item_count,
        source_input_sha256=candidate.source_input_sha256,
        output=candidate.output,
        compact_response_id=candidate.compact_response_id,
        compact_created_at=candidate.compact_created_at,
        input_item_count=candidate.input_item_count,
        output_item_count=candidate.output_item_count,
        generation=generation,
        created_at=created_at,
        updated_at=now,
    )


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


def _native_compaction_failure(
    classification: str, *, retryable: bool = False
) -> NativeCompactionFailure:
    return NativeCompactionFailure(classification, retryable, True)


def _classify_native_compaction_exception(exc: Exception) -> NativeCompactionFailure:
    """Classify without formatting or retaining the exception."""
    try:
        status_code = getattr(exc, "status_code", None)
    except Exception:
        status_code = None
    if type(status_code) is int:
        if status_code in (401, 403):
            return _native_compaction_failure("auth")
        if status_code in (404, 405, 501):
            return _native_compaction_failure("unsupported")

    try:
        import openai

        auth_types = tuple(
            exception_type
            for exception_type in (
                getattr(openai, "AuthenticationError", None),
                getattr(openai, "PermissionDeniedError", None),
            )
            if isinstance(exception_type, type)
        )
        timeout_types = tuple(
            exception_type
            for exception_type in (getattr(openai, "APITimeoutError", None),)
            if isinstance(exception_type, type)
        )
        network_types = tuple(
            exception_type
            for exception_type in (getattr(openai, "APIConnectionError", None),)
            if isinstance(exception_type, type)
        )
    except Exception:
        auth_types = timeout_types = network_types = ()

    if auth_types and isinstance(exc, auth_types):
        return _native_compaction_failure("auth")
    if isinstance(exc, TimeoutError) or (
        timeout_types and isinstance(exc, timeout_types)
    ):
        return _native_compaction_failure("timeout", retryable=True)
    if isinstance(exc, ConnectionError) or (
        network_types and isinstance(exc, network_types)
    ):
        return _native_compaction_failure("network", retryable=True)

    exception_name = type(exc).__name__.lower()
    if "unsupported" in exception_name or "notfound" in exception_name:
        return _native_compaction_failure("unsupported")
    return _native_compaction_failure("client")


def request_native_compaction_candidate(
    agent: Any,
    *,
    model: str,
    cut: NativeCompactionCut,
    compact_instructions: str,
    resolved_timeout: float | None,
    previous_checkpoint: NativeCompactionCheckpoint | None = None,
    pre_dispatch_check: Callable[[], bool] | None = None,
    dispatch_fence: Any = None,
    hard_cancel_event: Any = None,
) -> NativeCompactionCandidate | NativeCompactionFailure:
    """Request one isolated native compact generation without mutating state."""
    if (
        type(model) is not str
        or not model.strip()
        or type(compact_instructions) is not str
        or (
            resolved_timeout is not None
            and (
                type(resolved_timeout) not in (int, float)
                or not math.isfinite(resolved_timeout)
                or resolved_timeout <= 0
            )
        )
        or type(cut) is not NativeCompactionCut
        or (pre_dispatch_check is not None and not callable(pre_dispatch_check))
        or (
            dispatch_fence is not None
            and (
                not callable(getattr(type(dispatch_fence), "begin_dispatch", None))
                or not callable(
                    getattr(type(dispatch_fence), "finish_dispatch", None)
                )
            )
        )
        or (
            previous_checkpoint is not None
            and type(previous_checkpoint) is not NativeCompactionCheckpoint
        )
    ):
        return _native_compaction_failure("client")

    source_input = cut.source_input
    if previous_checkpoint is None:
        effective_input = source_input
    else:
        old_count = previous_checkpoint.source_input_item_count
        if old_count >= cut.source_input_item_count:
            return _native_compaction_failure("invalid_response")
        try:
            prefix_matches = (
                canonical_input_sha256(source_input[:old_count])
                == previous_checkpoint.source_input_sha256
            )
        except ValueError:
            prefix_matches = False
        if not prefix_matches:
            return _native_compaction_failure("invalid_response")
        effective_input = previous_checkpoint.output + copy.deepcopy(
            source_input[old_count:]
        )

    try:
        client = agent._create_request_openai_client(
            reason="native_openai_compaction", api_kwargs={"model": model}
        )
    except Exception as exc:
        return _classify_native_compaction_exception(exc)

    propagating_base_exception = False
    close_failed = False
    try:
        try:
            try:
                compact = getattr(
                    getattr(client, "responses", None), "compact", None
                )
            except Exception:
                result: NativeCompactionCandidate | NativeCompactionFailure = (
                    _native_compaction_failure("unsupported")
                )
            else:
                if not callable(compact):
                    result = _native_compaction_failure("unsupported")
                else:
                    try:
                        request_input = copy.deepcopy(effective_input)
                    except Exception:
                        result = _native_compaction_failure("invalid_response")
                    else:
                        if pre_dispatch_check is not None:
                            try:
                                dispatch_allowed = pre_dispatch_check() is True
                            except Exception:
                                dispatch_allowed = False
                        else:
                            dispatch_allowed = True
                        dispatch_entered = False
                        if dispatch_allowed and dispatch_fence is not None:
                            try:
                                dispatch_entered = (
                                    type(dispatch_fence).begin_dispatch(
                                        dispatch_fence, hard_cancel_event
                                    )
                                    is True
                                )
                            except Exception:
                                dispatch_entered = False
                            dispatch_allowed = dispatch_entered
                            if dispatch_entered and pre_dispatch_check is not None:
                                try:
                                    dispatch_allowed = pre_dispatch_check() is True
                                except Exception:
                                    dispatch_allowed = False
                                if not dispatch_allowed:
                                    try:
                                        type(dispatch_fence).finish_dispatch(
                                            dispatch_fence
                                        )
                                    except Exception:
                                        pass
                                    dispatch_entered = False
                        if not dispatch_allowed:
                            result = _native_compaction_failure("client")
                        else:
                            try:
                                try:
                                    response = compact(
                                        model=model,
                                        input=request_input,
                                        instructions=compact_instructions,
                                        timeout=resolved_timeout,
                                    )
                                except Exception as exc:
                                    result = _classify_native_compaction_exception(exc)
                                else:
                                    result = _candidate_from_compact_response(
                                        response,
                                        cut=cut,
                                        input_item_count=len(effective_input),
                                    )
                            finally:
                                if dispatch_entered:
                                    try:
                                        type(dispatch_fence).finish_dispatch(
                                            dispatch_fence
                                        )
                                    except Exception:
                                        result = _native_compaction_failure("client")
        except Exception:
            result = _native_compaction_failure("invalid_response")
    except BaseException:
        propagating_base_exception = True
        raise
    finally:
        try:
            agent._close_request_openai_client(
                client, reason="native_openai_compaction"
            )
        except Exception:
            if not propagating_base_exception:
                close_failed = True

    if close_failed:
        return _native_compaction_failure("client")
    return result


def _candidate_from_compact_response(
    response: Any,
    *,
    cut: NativeCompactionCut,
    input_item_count: int,
) -> NativeCompactionCandidate | NativeCompactionFailure:
    try:
        output = response.output
        if (
            not isinstance(output, list)
            or not output
            or len(output) > MAX_NATIVE_COMPACTION_OUTPUT_ITEMS
        ):
            return _native_compaction_failure("invalid_response")

        serialized_output: list[Any] = []
        for item in output:
            model_dump = getattr(item, "model_dump", None)
            if callable(model_dump):
                serialized_output.append(model_dump(mode="json"))
            else:
                serialized_output.append(copy.deepcopy(item))

        try:
            response_id = response.id
        except Exception:
            response_id = None
        if type(response_id) is not str or not response_id.strip():
            response_id = None

        try:
            created_at = response.created_at
        except Exception:
            created_at = None
        if (
            type(created_at) not in (int, float)
            or not math.isfinite(created_at)
            or created_at < 0
        ):
            created_at = None

        return NativeCompactionCandidate(
            source_input_item_count=cut.source_input_item_count,
            source_input_sha256=cut.source_input_sha256,
            compact_response_id=response_id,
            compact_created_at=created_at,
            input_item_count=input_item_count,
            output_item_count=len(serialized_output),
            output=serialized_output,
        )
    except Exception:
        return _native_compaction_failure("invalid_response")
