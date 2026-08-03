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
from typing import Any
from urllib.parse import urlsplit

from agent.backend_identity import BackendIdentity


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
            "credential_scope": self.identity.credential_scope,
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
