"""Read-only, telemetry-only detectors for Hermes failure-loop scouting.

This module deliberately lives outside the worker, gateway, hooks, and session
writer paths.  It reads a SQLite session store through a ``mode=ro`` URI and
emits bounded, redacted JSONL signals.  It is a Phase 0 shadow tool, not a
runtime guard or an intervention mechanism.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _datetime
import hashlib
import json
import math
import re
import shlex
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

HFL01_ID = "HFL-01"
HFL03_ID = "HFL-03"
HFL04_ID = "HFL-04"
DETECTOR_REVISION = "loop-risk-monitor-0.2"
DEFAULT_MIN_REPEATS = 3
MAX_STORED_TEXT = 131_072

_DETECTOR_IDS = (HFL01_ID, HFL03_ID, HFL04_ID)

# These are intentionally conservative.  A command is only classified as a
# native consumer when it is a known executable (or an explicit override is
# present); unknown commands produce no HFL-03 signal.
_NATIVE_EXECUTABLES = {
    "cmd",
    "cmd.exe",
    "ffmpeg",
    "ffmpeg.exe",
    "gh",
    "gh.exe",
    "git",
    "git.exe",
    "node",
    "node.exe",
    "npm",
    "npm.cmd",
    "npm.exe",
    "npx",
    "npx.cmd",
    "pnpm",
    "pnpm.cmd",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "python",
    "python.exe",
    "python3.exe",
    "py",
    "py.exe",
    "uv",
    "uv.exe",
}
_POSIX_EXECUTABLES = {
    "awk",
    "bash",
    "cat",
    "cut",
    "find",
    "grep",
    "head",
    "ls",
    "perl",
    "rsync",
    "sed",
    "sh",
    "sort",
    "tail",
    "tar",
    "tr",
    "uniq",
    "xargs",
    "zsh",
}

# Path regexes describe the dialect, not whether a path exists.  The matcher
# is deliberately limited to absolute roots so URLs and ordinary relative
# repository paths do not generate telemetry.
_MSYS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/[A-Za-z][\\/]")
_WSL_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])(?:/mnt/[A-Za-z][\\/]|//mnt/[A-Za-z][\\/])", re.IGNORECASE)
_WINDOWS_DRIVE_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]")
_WINDOWS_UNC_RE = re.compile(
    r"(?<![A-Za-z0-9_])\\\\[^\\/\s]+[\\/]|(?<![:A-Za-z0-9_])//(?!mnt/)[^/\s]+/",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://[^\s]+")
_POSIX_PATH_RE = re.compile(
    # Guard against treating the slash after a drive colon as POSIX.
    r"(?<![A-Za-z0-9_:\\])/(?:tmp|home|usr|var|opt|workspace|etc|root|run|srv|proc|dev)(?:[\\/]|$)",
    re.IGNORECASE,
)

_SECRET_KEY_RE = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|authorization|cookie|credential|private[_-]?key)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")

_MUTATING_TOOL_NAMES = {
    "apply_patch",
    "delete_file",
    "edit_file",
    "mkdir",
    "patch",
    "remove_file",
    "replace_file",
    "touch",
    "write_file",
}
_GIT_MUTATIONS = {
    "apply",
    "checkout",
    "cherry-pick",
    "clean",
    "commit",
    "merge",
    "pull",
    "push",
    "rebase",
    "reset",
    "restore",
    "revert",
    "switch",
}
_GIT_OPTIONS_WITH_VALUES = {
    # The parser lower-cases command basenames, so -C is represented by -c.
    "-c",
    "--config",
    "--exec-path",
    "--git-dir",
    "--namespace",
    "--work-tree",
}
_FILE_MUTATORS = {"cp", "install", "mkdir", "mv", "rm", "rmdir", "touch"}
_INSTALL_COMMANDS = {"npm", "pnpm", "pip", "uv", "yarn"}
_COMMAND_MUTATORS = {"add-content", "out-file", "set-content", "write_all_text", "writealltext"}


@dataclass(frozen=True)
class Signal:
    """A bounded detector result suitable for one JSONL line."""

    pattern_id: str
    detector: str
    session_id: str
    message_ids: Sequence[Any] = field(default_factory=tuple)
    evidence_lines: Sequence[str] = field(default_factory=tuple)
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "detector": self.detector,
            "session_id": self.session_id,
            "message_ids": [_json_safe(value) for value in self.message_ids],
            "evidence_lines": [str(line) for line in self.evidence_lines],
            "details": _json_safe(dict(self.details)),
        }


@dataclass
class SessionTrace:
    """The minimum in-memory projection consumed by the detectors."""

    session_id: str
    started_at: Any = None
    ended_at: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ScanResult:
    """Read-only database scan result."""

    stats: dict[str, Any]
    signals: list[Signal]
    traces: list[SessionTrace] = field(default_factory=list)


@dataclass
class _Invocation:
    message_id: Any
    call_id: str
    tool_name: str
    arguments: Any
    arguments_hash: str
    row_index: int
    result: Any = None
    result_hash: str | None = None
    result_is_failure: bool = False
    result_row_index: int | None = None
    result_message_id: Any = None


# ---------------------------------------------------------------------------
# General serialization, redaction, and timestamp helpers


def _json_safe(value: Any) -> Any:
    """Convert nested values to JSON-safe values without exposing raw paths."""

    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _redacted_for_fingerprint(value: Any, key: str | None = None) -> Any:
    """Redact credential-like fields before a value is hashed or reported."""

    if key is not None and _SECRET_KEY_RE.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redacted_for_fingerprint(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redacted_for_fingerprint(item, key) for item in value]
    if isinstance(value, str):
        return _BEARER_RE.sub("<redacted> token", value)
    return _json_safe(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _redacted_for_fingerprint(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalised_text(value: Any) -> str:
    text = str(value or "")
    if len(text) > MAX_STORED_TEXT:
        text = text[: MAX_STORED_TEXT // 2] + "\n<bounded>\n" + text[-MAX_STORED_TEXT // 2 :]
    return re.sub(r"\s+", " ", text).strip().lower()


def _parse_json(value: Any) -> Any:
    if isinstance(value, (Mapping, list, tuple, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _as_timestamp(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip()
    try:
        number = float(text)
        return number if math.isfinite(number) else None
    except ValueError:
        pass
    candidate = text
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = _datetime.datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
    return parsed.timestamp()


def _display_since(value: Any) -> str | float | None:
    timestamp = _as_timestamp(value)
    if timestamp is None:
        return None
    return timestamp


def _unique_ids(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        marker = _canonical_json(value)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


def _row_message_id(row: Mapping[str, Any]) -> Any:
    return row.get("id", row.get("message_id"))


def _row_role(row: Mapping[str, Any]) -> str:
    return str(row.get("role") or "").strip().lower()


def _row_timestamp(row: Mapping[str, Any]) -> float | None:
    return _as_timestamp(row.get("timestamp", row.get("created_at")))


# ---------------------------------------------------------------------------
# Tool-call projection and result classification


def _tool_call_entries(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = row.get("tool_calls")
    parsed = _parse_json(raw)
    if isinstance(parsed, Mapping):
        if isinstance(parsed.get("tool_calls"), list):
            parsed = parsed["tool_calls"]
        else:
            parsed = [parsed]
    if isinstance(parsed, (list, tuple)):
        return [item for item in parsed if isinstance(item, Mapping)]

    # A few historical fixtures store a single call in columns rather than in
    # the assistant JSON blob.  Supporting that projection keeps the scout
    # useful across schema revisions without importing production code.
    if row.get("tool_call_id") or row.get("tool_name"):
        return [
            {
                "id": row.get("tool_call_id"),
                "name": row.get("tool_name"),
                "arguments": row.get("arguments", row.get("tool_arguments")),
            }
        ]
    return []


def _call_name_and_arguments(call: Mapping[str, Any], row: Mapping[str, Any]) -> tuple[str, Any, str]:
    function = call.get("function")
    if not isinstance(function, Mapping):
        function = {}
    name = call.get("name", function.get("name", row.get("tool_name")))
    name = str(name or "unknown_tool")
    arguments = call.get("arguments", function.get("arguments", {}))
    parsed_arguments = _parse_json(arguments)
    if parsed_arguments is None:
        parsed_arguments = {}
    call_id = call.get("id", call.get("call_id", row.get("tool_call_id")))
    if call_id is None or str(call_id) == "":
        call_id = f"message-{_row_message_id(row)}-{name}"
    return name, parsed_arguments, str(call_id)


def _result_value(row: Mapping[str, Any]) -> Any:
    for key in ("content", "result", "tool_result", "output"):
        if key in row and row.get(key) is not None:
            return _parse_json(row.get(key))
    return None


def _result_is_failure(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, Mapping):
        lowered = {str(key).lower(): item for key, item in value.items()}
        for key in ("success", "ok"):
            if key in lowered and lowered[key] is False:
                return True
        if lowered.get("failed") is True or lowered.get("failure") is True:
            return True
        for key in ("exit_code", "returncode", "return_code", "status_code"):
            if key in lowered:
                try:
                    if int(lowered[key]) != 0:
                        return True
                except (TypeError, ValueError):
                    pass
        for key in ("error", "errors", "exception", "traceback"):
            item = lowered.get(key)
            if item not in (None, "", [], {}):
                return True
        status = str(lowered.get("status", "")).strip().lower()
        if status in {"error", "failed", "failure", "timeout", "timed_out", "cancelled"}:
            return True
        # A nested provider response often places the actual result under
        # output/result.  Inspect it only when it is itself structured.
        for key in ("output", "result", "response"):
            nested = lowered.get(key)
            if isinstance(nested, Mapping) and _result_is_failure(nested):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_result_is_failure(item) for item in value if isinstance(item, Mapping))
    text = _normalised_text(value)
    if not text:
        return False
    failure_phrases = (
        "error",
        "exception",
        "failed",
        "failure",
        "traceback",
        "no module named",
        "command not found",
        "permission denied",
        "cannot ",
        "could not",
        "enoent",
        "exit code",
        "non-zero",
        "timed out",
        "timeout",
    )
    return any(phrase in text for phrase in failure_phrases)


def _result_fingerprint(value: Any) -> str:
    if isinstance(value, str):
        return _fingerprint(_normalised_text(value))
    return _fingerprint(value)


def _build_invocations(trace: SessionTrace) -> list[_Invocation]:
    rows = list(trace.rows)
    result_rows: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for index, row in enumerate(rows):
        role = _row_role(row)
        if role not in {"tool", "tool_result", "function"}:
            continue
        call_id = row.get("tool_call_id", row.get("call_id"))
        if call_id is None:
            continue
        result_rows.setdefault(str(call_id), []).append((index, row))

    invocations: list[_Invocation] = []
    for row_index, row in enumerate(rows):
        if _row_role(row) not in {"assistant", "assistant_tool_call"}:
            continue
        for call in _tool_call_entries(row):
            name, arguments, call_id = _call_name_and_arguments(call, row)
            invocation = _Invocation(
                message_id=_row_message_id(row),
                call_id=call_id,
                tool_name=name,
                arguments=arguments,
                arguments_hash=_fingerprint(arguments),
                row_index=row_index,
            )
            # Historical fixture rows may carry the result on the call itself.
            direct_result = call.get("result", call.get("tool_result"))
            candidate_rows = result_rows.get(call_id, [])
            selected: tuple[int, Mapping[str, Any]] | None = None
            for candidate_index, candidate_row in candidate_rows:
                if candidate_index > row_index:
                    selected = (candidate_index, candidate_row)
                    break
            if selected is not None:
                result_index, result_row = selected
                result = _result_value(result_row)
                invocation.result = result
                invocation.result_hash = _result_fingerprint(result)
                invocation.result_is_failure = _result_is_failure(result)
                invocation.result_row_index = result_index
                invocation.result_message_id = _row_message_id(result_row)
            elif direct_result is not None:
                result = _parse_json(direct_result)
                invocation.result = result
                invocation.result_hash = _result_fingerprint(result)
                invocation.result_is_failure = _result_is_failure(result)
            invocations.append(invocation)
    return invocations


def _no_new_evidence(rows: Sequence[Mapping[str, Any]], previous_result_index: int | None, next_row_index: int) -> bool:
    if previous_result_index is None or previous_result_index >= next_row_index:
        return False
    # The empty interval is the strong form of the detector condition.  Any
    # row, including an unrelated tool result or a user/assistant note, is
    # treated as new evidence rather than trying to understand its semantics.
    return next_row_index == previous_result_index + 1


# ---------------------------------------------------------------------------
# HFL-01: unchanged-action retry


def detect_hfl01(trace: SessionTrace, min_repeats: int = DEFAULT_MIN_REPEATS) -> list[Signal]:
    """Detect a run of identical failed tool calls with no intervening rows."""

    if min_repeats < 2:
        raise ValueError("min_repeats must be at least 2")
    invocations = [
        invocation
        for invocation in _build_invocations(trace)
        if invocation.result_hash is not None and invocation.result_is_failure
    ]
    if not invocations:
        return []

    signals: list[Signal] = []
    current: list[_Invocation] = [invocations[0]]
    for invocation in invocations[1:] + [None]:  # type: ignore[list-item]
        previous = current[-1]
        same_call = (
            invocation is not None
            and invocation.tool_name == previous.tool_name
            and invocation.arguments_hash == previous.arguments_hash
            and invocation.result_hash == previous.result_hash
            and _no_new_evidence(trace.rows, previous.result_row_index, invocation.row_index)
        )
        if same_call:
            current.append(invocation)  # type: ignore[arg-type]
            continue
        if len(current) >= min_repeats:
            message_ids: list[Any] = []
            evidence_lines: list[str] = []
            for number, item in enumerate(current, start=1):
                message_ids.extend((item.message_id, item.result_message_id))
                evidence_lines.append(
                    f"repeat {number}: {item.tool_name} returned a stable failure fingerprint; "
                    "no intervening evidence"
                )
            signals.append(
                Signal(
                    pattern_id=HFL01_ID,
                    detector="deterministic-hfl01",
                    session_id=str(trace.session_id),
                    message_ids=_unique_ids(message_ids),
                    evidence_lines=evidence_lines,
                    details={
                        "detector_revision": DETECTOR_REVISION,
                        "tool_name": current[0].tool_name,
                        "repeat_count": len(current),
                        "arguments_sha256": current[0].arguments_hash,
                        "outcome_sha256": current[0].result_hash,
                        "no_new_evidence_between_retries": True,
                        "intervening_row_count": 0,
                    },
                )
            )
        if invocation is not None:
            current = [invocation]
    return signals


# ---------------------------------------------------------------------------
# HFL-03: Windows path dialect mismatch


def _command_value(arguments: Any) -> str | list[str] | None:
    if isinstance(arguments, Mapping):
        for key in ("command", "cmd", "script", "program"):
            if key in arguments and arguments[key] is not None:
                value = arguments[key]
                if isinstance(value, (str, list, tuple)):
                    return list(value) if isinstance(value, tuple) else value
        for key in ("argv", "args"):
            value = arguments.get(key)
            if isinstance(value, (str, list, tuple)):
                return list(value) if isinstance(value, tuple) else value
        return None
    if isinstance(arguments, (str, list, tuple)):
        return list(arguments) if isinstance(arguments, tuple) else arguments
    return None


def _command_text(arguments: Any) -> str:
    value = _command_value(arguments)
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value or "")


def _command_tokens(command: str) -> list[str]:
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        tokens = re.findall(r'''(?:[^\s"']+|"[^"]*"|'[^']*')''', command)
    return [token.strip("\"'") for token in tokens]


def _runtime_override(arguments: Any) -> str | None:
    if not isinstance(arguments, Mapping):
        return None
    for key in ("consumer_runtime", "runtime", "execution_runtime", "shell_runtime"):
        value = arguments.get(key)
        if value is None:
            continue
        text = str(value).strip().lower()
        if text in {"native", "windows", "win32", "windows-native", "cmd", "powershell", "pwsh"}:
            return "native"
        if text in {"posix", "unix", "msys", "git-bash", "git bash", "bash"}:
            return "posix"
        if text in {"wsl", "linux-wsl"}:
            return "wsl"
    return None


def _basename(token: str) -> str:
    return re.split(r"[\\/]", token)[-1].lower()


def _infer_consumer_runtime(tool_name: str, arguments: Any) -> str | None:
    override = _runtime_override(arguments)
    if override is not None:
        return override
    command = _command_text(arguments)
    tokens = _command_tokens(command)
    if not tokens:
        return None
    for token in tokens[:4]:
        base = _basename(token)
        if base in {"wsl", "wsl.exe"}:
            return "wsl"
        if base in {"bash", "sh", "zsh"}:
            return "posix"
        if base in _NATIVE_EXECUTABLES:
            return "native"
        if base in _POSIX_EXECUTABLES:
            return "posix"
    # Some callers put the consumer name in the tool name and the command in
    # a separate argument field.
    base = _basename(str(tool_name))
    if base in _NATIVE_EXECUTABLES:
        return "native"
    if base in _POSIX_EXECUTABLES:
        return "posix"
    return None


def _string_values(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _string_values(item)


def _path_dialects(arguments: Any) -> set[str]:
    dialects: set[str] = set()
    for raw_text in _string_values(arguments):
        # A URL path is not a local runtime path, even when its authority or
        # path happens to resemble an absolute POSIX or UNC root.
        text = _URL_RE.sub(" ", raw_text)
        if _WSL_PATH_RE.search(text):
            dialects.add("wsl")
        if _MSYS_PATH_RE.search(text):
            dialects.add("msys")
        if _WINDOWS_DRIVE_RE.search(text) or _WINDOWS_UNC_RE.search(text):
            dialects.add("windows")
        if _POSIX_PATH_RE.search(text):
            dialects.add("posix")
    return dialects


_SHELL_BOUNDARY_CHARS = ";|&\n"
_COMMON_CODE_ARGUMENT_FLAGS = {
    "-c",
    "-command",
    "-e",
    "-script",
}
_PERL_CODE_ARGUMENT_FLAGS = {"-n", "-ne", "-p", "-pe"}
_SHELL_REDIRECTION_RE = re.compile(r"^(?:\d*>>?|\d*<<?|&>)")
_VIRTUAL_POSIX_PATH_RE = re.compile(r"^/dev/(?:null|stdin|stdout|stderr|fd(?:/|$))", re.IGNORECASE)


def _split_shell_segments(command: str) -> list[str]:
    """Split a shell command without splitting quoted script arguments."""

    segments: list[str] = []
    start = 0
    quote: str | None = None
    index = 0
    while index < len(command):
        character = command[index]
        if quote is not None:
            if character == quote:
                quote = None
            elif character == "\\" and quote == '"':
                index += 1
        elif character in {"'", '"'}:
            quote = character
        elif character in _SHELL_BOUNDARY_CHARS:
            segment = command[start:index].strip()
            if segment:
                segments.append(segment)
            if character in "|&" and index + 1 < len(command) and command[index + 1] == character:
                index += 1
            start = index + 1
        index += 1
    segment = command[start:].strip()
    if segment:
        segments.append(segment)
    return segments


def _consumer_token_index(tokens: Sequence[str]) -> int | None:
    for index, token in enumerate(tokens[:4]):
        base = _basename(token.strip("\"'"))
        if base in _NATIVE_EXECUTABLES or base in _POSIX_EXECUTABLES:
            return index
    return None


def _is_redirection_token(token: str, previous: str | None = None) -> bool:
    stripped = token.strip("\"'")
    if _SHELL_REDIRECTION_RE.match(stripped):
        return True
    if previous is not None and _SHELL_REDIRECTION_RE.fullmatch(previous.strip("\"'")):
        return True
    return False


def _is_virtual_posix_path(token: str) -> bool:
    return bool(_VIRTUAL_POSIX_PATH_RE.match(token.rstrip(",;")))


def _is_embedded_code_flag(consumer: str, token: str) -> bool:
    if token in _COMMON_CODE_ARGUMENT_FLAGS:
        return True
    return consumer in {"perl", "perl.exe"} and token in _PERL_CODE_ARGUMENT_FLAGS


def _path_dialects_for_segment(segment: str) -> set[str]:
    """Return dialects in direct consumer arguments, excluding shell syntax."""

    tokens = _command_tokens(segment)
    consumer_index = _consumer_token_index(tokens)
    if consumer_index is None:
        return set()

    dialects: set[str] = set()
    consumer = _basename(tokens[consumer_index].strip("\"'"))
    embedded_code = False
    for index, token in enumerate(tokens[consumer_index + 1 :], start=consumer_index + 1):
        stripped = token.strip("\"'")
        lowered = stripped.lower()
        if _is_embedded_code_flag(consumer, lowered):
            embedded_code = True
            continue
        if embedded_code:
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", stripped):
            continue
        previous = tokens[index - 1] if index else None
        if _is_redirection_token(token, previous):
            continue
        if _is_virtual_posix_path(stripped):
            continue
        dialects.update(_path_dialects({"command": stripped}))
    return dialects


def _incompatible_dialects(runtime: str, dialects: set[str]) -> list[str]:
    if runtime == "native":
        return sorted(dialects.intersection({"msys", "posix", "wsl", "windows"}) - {"windows"})
    if runtime == "posix":
        return sorted(dialects.intersection({"windows"}))
    if runtime == "wsl":
        return sorted(dialects.intersection({"windows"}))
    return []


def detect_hfl03(trace: SessionTrace) -> list[Signal]:
    """Detect absolute paths whose dialect conflicts with their consumer."""

    signals: list[Signal] = []
    for invocation in _build_invocations(trace):
        runtime = _infer_consumer_runtime(invocation.tool_name, invocation.arguments)
        if runtime is None:
            continue
        command_value = _command_value(invocation.arguments)
        if command_value is None:
            continue
        if isinstance(command_value, (list, tuple)):
            command = " ".join(str(value) for value in command_value)
        else:
            command = str(command_value)
        segments = _split_shell_segments(command)
        observation: tuple[int, str, list[str]] | None = None
        for segment_index, segment in enumerate(segments):
            segment_runtime = _runtime_override(invocation.arguments) or _infer_consumer_runtime(
                invocation.tool_name, {"command": segment}
            )
            if segment_runtime is None:
                continue
            segment_dialects = _path_dialects_for_segment(segment)
            segment_incompatible = _incompatible_dialects(segment_runtime, segment_dialects)
            if segment_incompatible:
                observation = (segment_index, segment_runtime, segment_incompatible)
                break
        if observation is None:
            continue
        segment_index, runtime, incompatible = observation
        selected = incompatible[0]
        signals.append(
            Signal(
                pattern_id=HFL03_ID,
                detector="deterministic-hfl03",
                session_id=str(trace.session_id),
                message_ids=_unique_ids((invocation.message_id, invocation.result_message_id)),
                evidence_lines=[
                    f"{invocation.tool_name} uses a {selected} path with a {runtime} consumer; "
                    "path token redacted"
                ],
                details={
                    "detector_revision": DETECTOR_REVISION,
                    "tool_name": invocation.tool_name,
                    "consumer_runtime": runtime,
                    "path_dialect": selected,
                    "path_dialects": incompatible,
                    "command_segment_index": segment_index,
                    "invocation_message_id": invocation.message_id,
                    "invocation_call_id": invocation.call_id,
                },
            )
        )
    return signals


# ---------------------------------------------------------------------------
# HFL-04: concurrent duplicate execution / stale writer


def _metadata_layers(metadata: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    layers: list[Mapping[str, Any]] = [metadata]
    for key in ("model_config", "origin_json", "origin", "metadata", "session_metadata"):
        value = _parse_json(metadata.get(key))
        if isinstance(value, Mapping):
            layers.append(value)
    return layers


def _metadata_value(metadata: Mapping[str, Any], keys: Sequence[str]) -> Any:
    lowered_keys = {key.lower() for key in keys}
    for layer in _metadata_layers(metadata):
        for key, value in layer.items():
            if str(key).lower() in lowered_keys and value not in (None, ""):
                return value
    return None


def _workstream_keys(trace: SessionTrace) -> list[tuple[str, str]]:
    metadata = trace.metadata or {}
    keys: list[tuple[str, str]] = []
    branch = _metadata_value(metadata, ("git_branch", "branch", "branch_name"))
    if branch is not None:
        branch_text = str(branch).strip()
        match = re.search(r"(?:^|\s)(?:branch|workstream)\s*[:=]\s*([^\s,]+)", branch_text, re.IGNORECASE)
        if match:
            branch_text = match.group(1)
        if branch_text:
            keys.append(("branch", branch_text))

    task = _metadata_value(metadata, ("task_id", "task", "kanban_task", "work_item_id"))
    title = _metadata_value(metadata, ("title", "session_title"))
    task_text = str(task or "").strip()
    if not task_text and title:
        task_text = str(title)
    task_match = re.search(r"\bt_[0-9a-f]{6,}\b", task_text, re.IGNORECASE)
    if task_match:
        task_text = task_match.group(0)
    if task_text:
        keys.append(("task", task_text))

    workstream = _metadata_value(metadata, ("workstream_id", "workstream"))
    if workstream not in (None, ""):
        keys.append(("workstream", str(workstream).strip()))

    # Preserve priority and remove duplicate keys.  Branches are preferred
    # when both sessions expose them; task ids still provide a useful fallback.
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in keys:
        if item[1] and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _value_is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "writer", "mutating"}


def _git_subcommand(basenames: Sequence[str], git_index: int) -> str | None:
    """Return git's subcommand after options that consume a value."""

    index = git_index + 1
    while index < len(basenames):
        token = basenames[index]
        if token in _GIT_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if token.startswith("--") and "=" in token:
            option = token.split("=", 1)[0]
            if option in _GIT_OPTIONS_WITH_VALUES:
                index += 1
                continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return None


def _command_is_mutating(command: str) -> bool:
    tokens = [token.lower() for token in _command_tokens(command)]
    if not tokens:
        return False
    basenames = [_basename(token) for token in tokens]
    for index, token in enumerate(basenames):
        if token in {"git", "git.exe"}:
            if _git_subcommand(basenames, index) in _GIT_MUTATIONS:
                return True
        if token in _FILE_MUTATORS:
            return True
        if token in _INSTALL_COMMANDS and index + 1 < len(basenames):
            if basenames[index + 1] in {"install", "add", "sync", "remove", "uninstall"}:
                return True
    return any(token in _COMMAND_MUTATORS for token in basenames)


def _invocation_is_mutating(invocation: _Invocation) -> bool:
    name = _basename(invocation.tool_name)
    arguments = invocation.arguments
    if isinstance(arguments, Mapping):
        if _value_is_true(arguments.get("writer")) or _value_is_true(arguments.get("mutating")):
            return True
    if name in _MUTATING_TOOL_NAMES:
        return True
    if name in {"process", "terminal", "run", "exec", "execute"}:
        command = _command_text(arguments)
        return _command_is_mutating(command)
    return False


def _writer_identity(trace: SessionTrace) -> tuple[str, str | None, bool]:
    metadata = trace.metadata or {}
    explicit = _metadata_value(metadata, ("writer_id", "writer_identity", "process_id", "writer_pid", "pid"))
    marked_writer = _value_is_true(_metadata_value(metadata, ("writer", "is_writer", "mutating")))
    if explicit not in (None, ""):
        text = str(explicit).strip()
        # Keep ordinary PID/process labels useful, but never echo an explicit
        # path or credential-like value into a telemetry signal.
        sensitive = (
            len(text) > 128
            or _SECRET_KEY_RE.search(text) is not None
            or _BEARER_RE.search(text) is not None
            or _WINDOWS_DRIVE_RE.search(text) is not None
            or _WINDOWS_UNC_RE.search(text) is not None
            or _MSYS_PATH_RE.search(text) is not None
            or _POSIX_PATH_RE.search(text) is not None
        )
        if sensitive:
            return f"writer:{_fingerprint(text)}", None, marked_writer
        return f"pid:{text}", text, marked_writer
    return f"session:{trace.session_id}", None, marked_writer


def _writer_evidence(trace: SessionTrace) -> tuple[str, str | None, bool, list[Any]]:
    identity, pid, marked_writer = _writer_identity(trace)
    invocations = _build_invocations(trace)
    message_ids: list[Any] = []
    mutating = marked_writer
    for invocation in invocations:
        if _invocation_is_mutating(invocation):
            mutating = True
            message_ids.extend((invocation.message_id, invocation.result_message_id))
    if marked_writer and not message_ids:
        message_ids.extend(_row_message_id(row) for row in trace.rows if _row_role(row) == "assistant")
    return identity, pid, mutating, _unique_ids(message_ids)


def _trace_interval(trace: SessionTrace) -> tuple[float, float] | None:
    start = _as_timestamp(trace.started_at)
    end = _as_timestamp(trace.ended_at)
    row_times = [time for time in (_row_timestamp(row) for row in trace.rows) if time is not None]
    if start is None and row_times:
        start = min(row_times)
    if end is None and row_times:
        end = max(row_times)
    if start is None or end is None:
        return None
    if end < start:
        start, end = end, start
    return start, end


def detect_hfl04(traces: Sequence[SessionTrace]) -> list[Signal]:
    """Detect distinct writers with overlapping windows on one workstream."""

    records: list[dict[str, Any]] = []
    for trace in traces:
        keys = _workstream_keys(trace)
        interval = _trace_interval(trace)
        if not keys or interval is None:
            continue
        identity, pid, is_writer, message_ids = _writer_evidence(trace)
        if not is_writer:
            continue
        records.append(
            {
                "trace": trace,
                "keys": keys,
                "interval": interval,
                "identity": identity,
                "pid": pid,
                "message_ids": message_ids,
            }
        )

    # Group by every available key.  A pair sharing both a branch and a task
    # must still produce one signal, so pair keys are de-duplicated below.
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        for key in record["keys"]:
            groups.setdefault(key, []).append(record)

    signals: list[Signal] = []
    seen_pairs: set[tuple[str, str]] = set()
    for key, group in groups.items():
        group = sorted(group, key=lambda item: (item["interval"][0], str(item["trace"].session_id)))
        for left_index, left in enumerate(group):
            for right in group[left_index + 1 :]:
                left_id = str(left["trace"].session_id)
                right_id = str(right["trace"].session_id)
                if left_id == right_id or left["identity"] == right["identity"]:
                    continue
                pair = (min(left_id, right_id), max(left_id, right_id))
                if pair in seen_pairs:
                    continue
                overlap_start = max(left["interval"][0], right["interval"][0])
                overlap_end = min(left["interval"][1], right["interval"][1])
                if overlap_start >= overlap_end:
                    continue
                seen_pairs.add(pair)
                primary, related = left, right
                if (primary["interval"][0], str(primary["trace"].session_id)) > (
                    related["interval"][0],
                    str(related["trace"].session_id),
                ):
                    primary, related = related, primary
                message_ids = _unique_ids(primary["message_ids"] + related["message_ids"])
                related_id = str(related["trace"].session_id)
                primary_id = str(primary["trace"].session_id)
                signals.append(
                    Signal(
                        pattern_id=HFL04_ID,
                        detector="deterministic-hfl04",
                        session_id=primary_id,
                        message_ids=message_ids,
                        evidence_lines=[
                            f"writer {primary['identity']} has mutating evidence in an overlapping window",
                            f"writer {related['identity']} shares the same {key[0]} workstream; paths redacted",
                        ],
                        details={
                            "detector_revision": DETECTOR_REVISION,
                            "workstream_kind": key[0],
                            "workstream_sha256": _fingerprint(key[1]),
                            "related_session_ids": [related_id],
                            "primary_writer": primary["identity"],
                            "related_writer": related["identity"],
                            "writer_pids": [primary["pid"], related["pid"]],
                            "overlap_start": overlap_start,
                            "overlap_end": overlap_end,
                            "overlap_seconds": overlap_end - overlap_start,
                        },
                    )
                )
    signals.sort(key=lambda signal: (str(signal.session_id), signal.pattern_id, str(signal.message_ids)))
    return signals


# ---------------------------------------------------------------------------
# Read-only session-store projection


def _readonly_uri(db_path: str | Path) -> str:
    path = Path(db_path).expanduser().resolve()
    return path.as_uri() + "?mode=ro"


@contextlib.contextmanager
def open_read_only(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open a SQLite store without allowing any writes."""

    connection = sqlite3.connect(_readonly_uri(db_path), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if table not in {"sessions", "messages"}:
        raise ValueError(f"unsupported table: {table}")
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    columns = {str(row[1]) for row in rows}
    if not columns:
        raise RuntimeError(f"session store is missing the {table} table")
    return columns


def _quote_identifier(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise ValueError(f"unsafe SQL identifier: {identifier!r}")
    return f'"{identifier}"'


def _select_query(table: str, available: set[str], wanted: Sequence[str]) -> str:
    columns = [column for column in wanted if column in available]
    if not columns:
        raise RuntimeError(f"session store has none of the expected {table} columns")
    return ", ".join(_quote_identifier(column) for column in columns)


def _load_session_rows(connection: sqlite3.Connection, since: float | None) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    session_columns = _table_columns(connection, "sessions")
    message_columns = _table_columns(connection, "messages")
    wanted_sessions = (
        "id",
        "source",
        "model_config",
        "parent_session_id",
        "started_at",
        "ended_at",
        "title",
        "cwd",
        "git_branch",
        "git_repo_root",
        "origin_json",
        "metadata",
        "task_id",
        "workstream_id",
        "branch",
        "pid",
        "process_id",
        "writer_pid",
        "writer",
    )
    select_sessions = _select_query("sessions", session_columns, wanted_sessions)
    predicates: list[str] = []
    parameters: list[Any] = []
    if since is not None and "started_at" in session_columns:
        predicates.append(f'"started_at" >= ?')
        parameters.append(since)
    query = f"SELECT {select_sessions} FROM \"sessions\""
    if predicates:
        query += " WHERE " + " AND ".join(predicates)
    if "started_at" in session_columns:
        query += ' ORDER BY "started_at", "id"'
    elif "id" in session_columns:
        query += ' ORDER BY "id"'
    session_rows = [dict(row) for row in connection.execute(query, parameters).fetchall()]
    return session_rows, session_columns, message_columns


def _load_messages_for_ids(
    connection: sqlite3.Connection,
    session_ids: Sequence[Any],
    message_columns: set[str],
) -> dict[str, list[dict[str, Any]]]:
    if not session_ids:
        return {}
    wanted_messages = (
        "id",
        "session_id",
        "role",
        "content",
        "tool_call_id",
        "tool_calls",
        "tool_name",
        "timestamp",
        "active",
        "pid",
        "process_id",
        "writer_pid",
        "metadata",
        "created_at",
    )
    select_messages = _select_query("messages", message_columns, wanted_messages)
    result: dict[str, list[dict[str, Any]]] = {str(session_id): [] for session_id in session_ids}
    # SQLite's default parameter limit is commonly 999.  Batching also keeps
    # the generated SQL small for a several-thousand-session shadow scan.
    for offset in range(0, len(session_ids), 500):
        batch = session_ids[offset : offset + 500]
        placeholders = ",".join("?" for _ in batch)
        query = f"SELECT {select_messages} FROM \"messages\" WHERE \"session_id\" IN ({placeholders})"
        if "active" in message_columns:
            query += ' AND "active" = 1'
        if "id" in message_columns:
            query += ' ORDER BY "id"'
        for row in connection.execute(query, list(batch)).fetchall():
            item = dict(row)
            result.setdefault(str(item.get("session_id")), []).append(item)
    return result


def load_traces(db_path: str | Path, since: Any = None) -> list[SessionTrace]:
    """Load session/message projections through a read-only SQLite connection."""

    since_timestamp = _as_timestamp(since)
    with open_read_only(db_path) as connection:
        session_rows, _session_columns, message_columns = _load_session_rows(connection, since_timestamp)
        raw_ids = [row.get("id") for row in session_rows]
        messages = _load_messages_for_ids(connection, raw_ids, message_columns)
    traces: list[SessionTrace] = []
    for row in session_rows:
        session_id = str(row.get("id"))
        metadata = {key: value for key, value in row.items() if key != "id"}
        traces.append(
            SessionTrace(
                session_id=session_id,
                started_at=row.get("started_at"),
                ended_at=row.get("ended_at"),
                metadata=metadata,
                rows=messages.get(session_id, []),
            )
        )
    return traces


def scan_database(
    db_path: str | Path,
    since: Any = None,
    min_repeats: int = DEFAULT_MIN_REPEATS,
) -> ScanResult:
    """Run all Phase 0 detectors without writing to the session store."""

    traces = load_traces(db_path, since=since)
    signals: list[Signal] = []
    for trace in traces:
        signals.extend(detect_hfl01(trace, min_repeats=min_repeats))
        signals.extend(detect_hfl03(trace))
    signals.extend(detect_hfl04(traces))
    signals.sort(
        key=lambda signal: (
            str(signal.session_id),
            str(signal.pattern_id),
            str(signal.message_ids),
        )
    )
    by_pattern = {pattern_id: 0 for pattern_id in _DETECTOR_IDS}
    for signal in signals:
        by_pattern[signal.pattern_id] = by_pattern.get(signal.pattern_id, 0) + 1
    stats = {
        "sessions_scanned": len(traces),
        "message_rows_scanned": sum(len(trace.rows) for trace in traces),
        "signals_fired": len(signals),
        "signals_by_pattern": by_pattern,
        "read_only": True,
        "database_mode": "ro",
    }
    return ScanResult(stats=stats, signals=signals, traces=traces)


# ---------------------------------------------------------------------------
# Held-out evaluation and report generation


def _trace_from_json(value: Mapping[str, Any]) -> SessionTrace:
    return SessionTrace(
        session_id=str(value.get("session_id", "case-session")),
        started_at=value.get("started_at"),
        ended_at=value.get("ended_at"),
        metadata=dict(value.get("metadata") or {}),
        rows=[dict(row) for row in value.get("rows", [])],
    )


def _case_traces(case: Mapping[str, Any]) -> list[SessionTrace]:
    values = case.get("traces")
    if isinstance(values, list) and values:
        return [_trace_from_json(value) for value in values if isinstance(value, Mapping)]
    value = case.get("trace")
    if isinstance(value, Mapping):
        return [_trace_from_json(value)]
    return []


def _case_labels(case: Mapping[str, Any]) -> dict[str, bool]:
    raw = case.get("labels")
    if isinstance(raw, Mapping):
        return {pattern_id: bool(raw.get(pattern_id, False)) for pattern_id in _DETECTOR_IDS}
    raw = case.get("label")
    labels = {pattern_id: False for pattern_id in _DETECTOR_IDS}
    if isinstance(raw, Mapping):
        return {pattern_id: bool(raw.get(pattern_id, False)) for pattern_id in _DETECTOR_IDS}
    if isinstance(raw, str) and raw in labels:
        labels[raw] = True
    target = case.get("target_pattern")
    if isinstance(target, str) and target in labels:
        labels[target] = True
    return labels


def _case_predictions(traces: Sequence[SessionTrace]) -> dict[str, bool]:
    predictions = {pattern_id: False for pattern_id in _DETECTOR_IDS}
    for trace in traces:
        predictions[HFL01_ID] = predictions[HFL01_ID] or bool(detect_hfl01(trace))
        predictions[HFL03_ID] = predictions[HFL03_ID] or bool(detect_hfl03(trace))
    predictions[HFL04_ID] = bool(detect_hfl04(traces))
    return predictions


def _metric(tp: int, fp: int, tn: int, fn: int) -> dict[str, Any]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "support_positive": tp + fn,
        "support_negative": tn + fp,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
    }


def evaluate_cases(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Evaluate labelled JSON-compatible traces against all three detectors."""

    counts = {
        pattern_id: {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
        for pattern_id in _DETECTOR_IDS
    }
    case_results: list[dict[str, Any]] = []
    positive_cases = 0
    clean_cases = 0
    for case in cases:
        traces = _case_traces(case)
        labels = _case_labels(case)
        if any(labels.values()):
            positive_cases += 1
        else:
            clean_cases += 1
        predictions = _case_predictions(traces)
        for pattern_id in _DETECTOR_IDS:
            expected = labels[pattern_id]
            predicted = predictions[pattern_id]
            if expected and predicted:
                counts[pattern_id]["tp"] += 1
            elif not expected and predicted:
                counts[pattern_id]["fp"] += 1
            elif not expected and not predicted:
                counts[pattern_id]["tn"] += 1
            else:
                counts[pattern_id]["fn"] += 1
        case_results.append(
            {
                "case_id": str(case.get("case_id", f"case-{len(case_results) + 1}")),
                "source_sessions": [
                    str(item)
                    for item in case.get("source_sessions", [])
                    if item is not None
                ],
                "labels": labels,
                "predictions": predictions,
            }
        )
    by_detector = {
        pattern_id: _metric(**counts[pattern_id]) for pattern_id in _DETECTOR_IDS
    }
    return {
        "cases_total": len(cases),
        "positive_cases": positive_cases,
        "clean_cases": clean_cases,
        "equal_positive_and_clean": positive_cases == clean_cases,
        "by_detector": by_detector,
        "case_results": case_results,
    }


def _load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(value, Mapping):
                raise ValueError(f"JSONL record at {path}:{line_number} is not an object")
            records.append(dict(value))
    return records


def _write_jsonl(path: str | Path, records: Iterable[Mapping[str, Any]]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(_json_safe(dict(record)), ensure_ascii=True, sort_keys=True) + "\n")


def _load_review(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "status": "not_run",
            "signals_reviewed": 0,
            "false_positives": 0,
            "entries": [],
            "notes": "Manual review was not supplied.",
        }
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if isinstance(value, list):
        value = {"status": "complete", "entries": value}
    if not isinstance(value, Mapping):
        raise ValueError("review file must contain an object or an array")
    entries = [dict(entry) for entry in value.get("entries", []) if isinstance(entry, Mapping)]
    false_positives = sum(
        1
        for entry in entries
        if str(entry.get("classification", "")).lower() in {"false_positive", "false-positive"}
    )
    return {
        "status": str(value.get("status", "complete")),
        "signals_reviewed": int(value.get("signals_reviewed", len(entries))),
        "false_positives": int(value.get("false_positives", false_positives)),
        "entries": entries,
        "notes": str(value.get("notes", "")),
    }


def _recommendation(evaluation: Mapping[str, Any] | None, review: Mapping[str, Any]) -> dict[str, Any]:
    metrics = (evaluation or {}).get("by_detector", {}) if isinstance(evaluation, Mapping) else {}
    sufficient = True
    gate_results: dict[str, Any] = {}
    for pattern_id in _DETECTOR_IDS:
        metric = metrics.get(pattern_id, {}) if isinstance(metrics, Mapping) else {}
        precision = float(metric.get("precision", 0.0))
        recall = float(metric.get("recall", 0.0))
        positive_support = int(metric.get("support_positive", 0))
        negative_support = int(metric.get("support_negative", 0))
        passes = positive_support >= 2 and negative_support >= 2 and precision >= 0.8 and recall >= 0.8
        gate_results[pattern_id] = {
            "precision": precision,
            "recall": recall,
            "positive_support": positive_support,
            "negative_support": negative_support,
            "passes": passes,
        }
        sufficient = sufficient and passes
    review_complete = str(review.get("status", "")) == "complete"
    no_false_positives = int(review.get("false_positives", 0)) == 0
    decision = "PHASE_1_TYPE_I" if sufficient and review_complete and no_false_positives else "STOP"
    reasons: list[str] = []
    if not sufficient:
        reasons.append("held-out precision/recall and support gates are not all met")
    if not review_complete:
        reasons.append("the live shadow signals do not have a complete manual review")
    if not no_false_positives:
        reasons.append("manual review found false positives")
    if not reasons:
        reasons.append("all three deterministic detectors meet the held-out gates and live signals were reviewed")
    return {
        "decision": decision,
        "scope": "Type-I next-decision cases" if decision == "PHASE_1_TYPE_I" else "no Phase 1 promotion",
        "gate_results": gate_results,
        "review_complete": review_complete,
        "false_positives": int(review.get("false_positives", 0)),
        "rationale": "; ".join(reasons),
    }


def build_report(
    result: ScanResult,
    since: Any,
    evaluation: Mapping[str, Any] | None = None,
    review: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    review_value = dict(review or {
        "status": "not_run",
        "signals_reviewed": 0,
        "false_positives": 0,
        "entries": [],
        "notes": "Manual review was not supplied.",
    })
    report = {
        "schema_version": "loop-risk-monitor-shadow/v1",
        "mode": "shadow",
        "session_store": "local read-only state.db",
        "read_only": True,
        "database_mode": "ro",
        "since": _display_since(since),
        "scan": result.stats,
        "evaluation": dict(evaluation) if evaluation is not None else None,
        "false_positive_review": review_value,
        "recommendation": _recommendation(evaluation, review_value),
    }
    return _json_safe(report)


def _write_report(path: str | Path, report: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="SQLite session store to open read-only")
    parser.add_argument("--since", required=True, help="epoch seconds or ISO-8601 session start cutoff")
    parser.add_argument("--output", required=True, help="shadow report JSON path")
    parser.add_argument("--signals-out", help="optional fired-signal JSONL path")
    parser.add_argument("--heldout", help="optional held-out labelled JSONL path")
    parser.add_argument("--review-file", help="optional hand-review JSON path")
    parser.add_argument("--min-repeats", type=int, default=DEFAULT_MIN_REPEATS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_argument_parser()
    args = parser.parse_args(argv)
    since = _as_timestamp(args.since)
    if since is None:
        parser.error("--since must be epoch seconds or ISO-8601")
    result = scan_database(args.db, since=since, min_repeats=args.min_repeats)
    if args.signals_out:
        _write_jsonl(args.signals_out, (signal.as_dict() for signal in result.signals))
    evaluation = None
    if args.heldout:
        evaluation = evaluate_cases(_load_jsonl(args.heldout))
    review = _load_review(args.review_file)
    report = build_report(result, since=since, evaluation=evaluation, review=review)
    _write_report(args.output, report)
    print(json.dumps({
        "mode": "shadow",
        "sessions_scanned": result.stats["sessions_scanned"],
        "message_rows_scanned": result.stats["message_rows_scanned"],
        "signals_fired": result.stats["signals_fired"],
        "recommendation": report["recommendation"]["decision"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
