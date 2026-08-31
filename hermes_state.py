#!/usr/bin/env python3
"""SQLite state store for Hermes Agent: session metadata, message history, model
config, FTS5 search. WAL mode (concurrent readers + one writer); compression
splits sessions via parent_session_id chains; sessions are source-tagged
('cli', 'telegram', ...). Batch-runner / RL trajectories live elsewhere.
"""

import asyncio
import atexit
import hashlib
import json
import logging
import os
import queue
import random
import re
import sqlite3
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from agent.message_sanitization import _sanitize_surrogates
from agent.message_metadata import stamp_persisted_todo_snapshot
from hermes_constants import get_hermes_home
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple, TypeVar, cast

from hermes_state_common import escape_like as _escape_like, stat_db_file_identity as _stat_db_file_identity
from hermes_state_errors import (
    _DELETED_WAL_GENERATION_MSG, _DISK_IO_ERROR_MARKER, _STATE_DB_CORRUPT_MSG, _STATE_DB_GENERATION_KEY,
    _STATE_DB_REPLACED_MSG, CompressionMetadataConflictError, DeletedWalGenerationError,
    SessionCompressionInProgressError, StateDbCorruptError,
    StateDbReplacedError, _is_no_more_rows, classify_persistence_error, is_malformed_db_error,
    is_malformed_schema_error,
)
from hermes_state_guard import (
    _STATE_DB_GUARD_BYPASS_ENV, _in_test_context, _is_production_state_db, _real_platform_state_root,
    _set_last_init_error, get_last_init_error,
)
from hermes_state_readpool import _READ_POOL_MAX, _proc_fd_targets, _read_budget_for
from hermes_state_sessions import SessionSessionsMixin
from hermes_state_fts import SessionFtsSetupMixin, load_fts5_cjk_extension
from hermes_state_portability import SessionPortabilityMixin
from hermes_state_telegram import SessionTelegramTopicsMixin
from hermes_state_schema import SessionSchemaMixin
import hermes_state_holders as _state_holders
from hermes_state_dbfile import (
    _canonical_sqlite_path, _connect_tracked_db, _read_sqlite_application_id, _stat_sqlite_sidecar_identity,
    _watched_sqlite_sidecar_paths, is_zeroed_state_db, quarantine_cross_process_lock, quarantine_zeroed_state_db,
    refuse_deleted_wal_generation,
)
from hermes_state_messages import SessionMessagesMixin
from hermes_state_wal import _WAL_INCOMPAT_MARKERS, apply_database_pragmas, apply_wal_with_fallback
from hermes_state_repair import _claim_repair_attempt, preflight_db_writability, repair_state_db_schema
from hermes_state_titles import SessionTitlesMixin
from hermes_state_usage import SessionUsageMixin
from hermes_state_maintenance import SessionMaintenanceMixin
from hermes_state_gateway import SessionGatewayMixin
from hermes_state_compression import SessionCompressionMixin
from hermes_state_search import SessionSearchMixin

try:  # Hard dependency, but tolerate scaffold-phase imports before pip install.
    import psutil
except ImportError:  # pragma: no cover - stripped/scaffold installs only
    psutil = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_MAX_SAFE_MESSAGES = 20_000  # resume/export guard default

_TODO_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})
_TODO_STATUS_BY_MARKER = {" ": "pending", ">": "in_progress", "~": "cancelled", "x": "completed"}
_TODO_ITEM_RE = re.compile(
    r"^( *)- \[([x> ~])\] (.+?)\. (.+) \((pending|in_progress|completed|cancelled)\)$"
)
_TODO_ITEM_V2_RE = re.compile(r"^( *)- \[([x> ~])\] (\{.+\})$")
_TODO_SKILL_CALLS_RE = re.compile(
    r"skill_view\(name='[^'\r\n]+'\)(?:; skill_view\(name='[^'\r\n]+'\))*"
)
_TODO_SKILL_NOTICE_HEADER = (
    "[Skills pruned during compression — reload before acting on these tasks]"
)
_TODO_SKILL_NOTICE_PREFIX = (
    "The task list above crossed the compression boundary verbatim, but the skill "
    "instructions that governed it were pruned. Before executing any preserved task "
    "that depends on these skills, reload them first: "
)
_TODO_SKILL_NOTICE_SUFFIX = (
    ". After reloading, re-check that each pending task is still justified — findings "
    "recorded before the boundary may have invalidated it."
)


def _parse_todo_value(value: Any, depth: int = 0) -> Optional[List[Dict[str, Any]]]:
    """Match Desktop's bounded Todo JSON grammar; ``None`` means invalid."""
    from tools.todo_tool import MAX_TODO_RESULT_CHARS

    if depth > 2:
        return None
    try:
        encoded = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    if len(encoded) > MAX_TODO_RESULT_CHARS:
        return None
    if isinstance(value, list):
        parsed: List[Dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict) or item.get("status") not in _TODO_STATUSES:
                return None
            raw_id = item.get("id")
            raw_content = item.get("content")
            raw_parent = item.get("parent")
            if not isinstance(raw_id, str) or not isinstance(raw_content, str):
                return None
            if raw_parent is not None and not isinstance(raw_parent, str):
                return None
            item_id = raw_id.strip()
            content = raw_content.strip()
            parent = raw_parent.strip() if isinstance(raw_parent, str) else ""
            if not item_id or not content:
                return None
            parsed.append(
                {
                    "id": item_id,
                    "content": content,
                    "status": item["status"],
                    **({"parent": parent} if parent and parent != item_id else {}),
                }
            )
        return parsed
    if isinstance(value, str) and value.strip():
        try:
            return _parse_todo_value(json.loads(value), depth + 1)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(value, dict) and "todos" in value:
        return _parse_todo_value(value["todos"], depth + 1)
    return None


def _is_producer_todo_skill_notice(lines: List[str]) -> bool:
    if len(lines) != 3 or lines[0] != "" or lines[1] != _TODO_SKILL_NOTICE_HEADER:
        return False
    notice = lines[2]
    if not notice.startswith(_TODO_SKILL_NOTICE_PREFIX) or not notice.endswith(
        _TODO_SKILL_NOTICE_SUFFIX
    ):
        return False
    calls = notice[len(_TODO_SKILL_NOTICE_PREFIX) : -len(_TODO_SKILL_NOTICE_SUFFIX)]
    return _TODO_SKILL_CALLS_RE.fullmatch(calls) is not None


def _is_valid_legacy_todo_carrier(value: Any) -> bool:
    """Validate the exact V1/V2 standalone carrier grammar used by Desktop."""
    from tools.todo_tool import (
        MAX_TODO_RESULT_CHARS,
        TODO_INJECTION_FORMAT_V2,
        TODO_INJECTION_HEADER,
    )

    if not isinstance(value, str) or len(value) > MAX_TODO_RESULT_CHARS:
        return False
    if "\r" in value and "\r\n" not in value:
        return False
    lines = value.replace("\r\n", "\n").split("\n")
    if not lines or lines[0] != TODO_INJECTION_HEADER:
        return False
    versioned = len(lines) > 1 and lines[1] == TODO_INJECTION_FORMAT_V2
    index = 2 if versioned else 1
    todos: List[Dict[str, Any]] = []
    ancestors: List[str] = []
    ids: Set[str] = set()
    while index < len(lines):
        match = (_TODO_ITEM_V2_RE if versioned else _TODO_ITEM_RE).fullmatch(lines[index])
        if match is None:
            break
        indent, marker = match.group(1), match.group(2)
        if versioned:
            try:
                payload = json.loads(match.group(3))
            except (json.JSONDecodeError, TypeError):
                return False
            if (
                not isinstance(payload, dict)
                or set(payload) != {"id", "content", "status"}
                or not isinstance(payload["id"], str)
                or not isinstance(payload["content"], str)
                or payload["status"] not in _TODO_STATUSES
            ):
                return False
            item_id, content, status = payload["id"], payload["content"], payload["status"]
        else:
            item_id, content, status = match.group(3), match.group(4), match.group(5)
            if ". " in content:
                return False
        if (
            len(indent) % 2
            or item_id != item_id.strip()
            or content != content.strip()
            or not item_id
            or not content
            or item_id in ids
            or _TODO_STATUS_BY_MARKER.get(marker) != status
        ):
            return False
        depth = len(indent) // 2
        if depth > len(ancestors) or (depth > 0 and not ancestors[depth - 1]):
            return False
        parent = ancestors[depth - 1] if depth else None
        todos.append({"id": item_id, "content": content, "status": status, "parent": parent})
        ids.add(item_id)
        ancestors = ancestors[:depth]
        ancestors.append(item_id)
        index += 1
    if not todos:
        return False
    remainder = lines[index:]
    if remainder and not _is_producer_todo_skill_notice(remainder):
        return False
    retained_parents: Set[str] = set()
    for todo in reversed(todos):
        active = todo["status"] in {"pending", "in_progress"}
        if not active and todo["id"] not in retained_parents:
            return False
        if todo["parent"]:
            retained_parents.add(todo["parent"])
    return True


def _configured_transcript_limit(key: str, fallback: int = _MAX_SAFE_MESSAGES) -> int:
    """``sessions.<key>`` from config.yaml (lazy import: circular at load), else *fallback*; 0 disables."""
    try:
        from hermes_cli.config import load_config_readonly
        value = (load_config_readonly().get("sessions") or {}).get(key)
        if value is None:
            return fallback
        limit = int(value)
        return limit if limit >= 0 else fallback
    except Exception:
        return fallback


def resolved_max_resume_messages() -> int:
    return _configured_transcript_limit("max_resume_messages")


def resolved_max_export_messages() -> int:
    return _configured_transcript_limit("max_export_messages")


class SessionResumeTooLargeError(ValueError):
    def __init__(
        self, message_count: int, limit: int = _MAX_SAFE_MESSAGES, scope: str = "across its lineage",
    ):
        self.message_count, self.limit = message_count, limit
        super().__init__(
            f"session has at least {message_count} active messages {scope}; "
            f"safe resume limit is {limit}. Export the session instead, or set "
            "sessions.max_resume_messages: 0 in config.yaml to disable the guard."
        )


class SessionExportTooLargeError(ValueError):
    def __init__(self, session_id: str, message_count: int, limit: int = _MAX_SAFE_MESSAGES):
        self.session_id, self.message_count, self.limit = session_id, message_count, limit
        super().__init__(
            f"session '{session_id}' has at least {message_count} active messages; "
            f"safe in-memory export limit is {limit}"
        )


def _compression_lock_holder_process_is_dead(holder: str) -> bool:
    """True only when a ``pid=<n>`` lock holder's local PID is provably gone.
    Reclaim on kernel proof only: unstructured/same-process holders (another
    thread's live lease) and any probe doubt keep the lease until TTL expiry
    (PID reuse must never steal a live lease; a wrongly-kept one self-heals)."""
    match = re.search(r"(?:^|:)pid=(\d+)(?::|$)", holder or "")
    pid = int(match.group(1)) if match else 0
    if pid <= 0 or pid == os.getpid():
        return False
    if psutil is not None:
        try:
            return not psutil.pid_exists(pid)  # recycled PIDs read as alive (conservative)
        except Exception:
            return False
    # psutil-less fallback is POSIX-only: on Windows os.kill(pid, 0) maps sig=0 to
    # CTRL_C_EVENT and can kill the target's console group.
    if os.name == "nt":
        return False
    try:
        os.kill(pid, 0)  # windows-footgun: ok — nt early-returns just above
    except ProcessLookupError:
        return True
    except (OSError, OverflowError):  # PermissionError is an OSError: alive but foreign
        return False
    return False


def _scrub_surrogates(value: Any) -> Any:
    """Replace lone surrogates in text (sqlite3 raises UnicodeEncodeError, aborting the whole write)."""
    return _sanitize_surrogates(value) if isinstance(value, str) else value


# Billing buckets that aren't a routable provider identity: a session that persisted only
# one of these (never ran /model) falls back to the config default. Shared by
# session_gateway_runtime and tui_gateway.server so they cannot drift.
_BARE_BILLING_PROVIDERS = frozenset({"auto", "custom"})

T = TypeVar("T")

# Import-time snapshot lets _default_db_path() detect a re-pointed DEFAULT_DB_PATH
# (tests monkeypatch the constant directly).
DEFAULT_DB_PATH = _IMPORT_DEFAULT_DB_PATH = get_hermes_home() / "state.db"

# Back off from read-only opens after one fails: not per query, but short enough that
# transient fd pressure doesn't strand the read pool.
_READ_OPEN_RETRY_SECONDS = 60.0
# Transient SQLITE_IOERR retry budget for READ-ONLY opens (#100436): a WAL writer's checkpoint/
# reset/frame flush surfaces "disk I/O error" to a concurrent mode=ro reader for a millisecond-
# wide window — the ro connection cannot perform WAL recovery because recovery writes the -shm
# index, which mode=ro refuses. The writer closes the window on its own, so a few short retries
# make the open succeed instead of 500-ing the whole /api/sessions poll (or any other ro opener).
# Deliberately NOT for writable opens: a writer owns the transition, so an IOERR there is a real
# storage/fd problem. A persistent IOERR still exhausts the budget and propagates.
_READ_ONLY_IOERR_RETRY_ATTEMPTS, _READ_ONLY_IOERR_RETRY_BACKOFF_S = 3, 0.05


def _default_db_path() -> Path:
    """Default state DB path at CALL time: a re-pointed ``DEFAULT_DB_PATH`` wins, else
    ``get_hermes_home()`` is resolved fresh (a runtime HERMES_HOME redirect works regardless of import)."""
    return DEFAULT_DB_PATH if DEFAULT_DB_PATH != _IMPORT_DEFAULT_DB_PATH else get_hermes_home() / "state.db"


# Live-DB guard knobs live HERE (not in hermes_state_guard): the hermetic conftest monkeypatches
# ``hermes_state._STATE_DB_GUARD_BYPASS`` (``@pytest.mark.live_system_guard_bypass`` escape hatch)
# and ``_EXTRA_DENY_ROOTS`` (the pre-sandbox root, so custom-HERMES_HOME deployments are covered).
_STATE_DB_GUARD_BYPASS = False
_STATE_DB_GUARD_EXTRA_DENY_ROOTS: Tuple[Path, ...] = ()


def _ensure_test_isolation(db_path: Path) -> None:
    """Raise before any connection/mkdir/pragma/byte probe when a pytest-context process
    (env OR ancestry) resolves a production DB.

    Env alone is not enough: a child spawned with a rebuilt environment loses ``PYTEST_*`` and
    ``HERMES_HOME`` together, which is precisely the state in which it writes to production (#82770).
    """
    if _STATE_DB_GUARD_BYPASS or os.environ.get(_STATE_DB_GUARD_BYPASS_ENV) or not _in_test_context():
        return
    try:
        resolved = Path(db_path).expanduser().resolve()
    except Exception:
        return
    roots = [r for r in (_real_platform_state_root(),) if r is not None]
    for extra in _STATE_DB_GUARD_EXTRA_DENY_ROOTS:
        try:
            roots.append(Path(extra).expanduser().resolve())
        except Exception:
            continue
    for root in roots:
        if _is_production_state_db(resolved, root):
            raise RuntimeError(
                "live-system guard: test attempted to open production "
                f"state.db at {resolved} (under real Hermes root {root}). "
                "Tests must run against a temporary HERMES_HOME — pass an "
                "explicit tmp db_path or let the hermetic conftest redirect "
                "HERMES_HOME. If this test genuinely needs the live database, mark it with "
                "@pytest.mark.live_system_guard_bypass — or, for a spawned "
                f"child process, export {_STATE_DB_GUARD_BYPASS_ENV}=1 in "
                "its environment."
            )


# Openings of the background-review harness prompts (agent/background_review.py).
_REVIEW_HARNESS_PREFIXES = (
    "Review the conversation above and update the skill library",
    "Review the conversation above and consider saving to memory",
)


def _is_background_review_harness_message(msg: Dict[str, Any]) -> bool:
    """Persisted harness prompt (older builds wrote the forked curator's turns
    into real sessions; replaying them hijacks the session)."""
    if not isinstance(msg, dict) or msg.get("role") not in {"user", "system"}:
        return False
    content = msg.get("content")
    return isinstance(content, str) and content.lstrip().startswith(_REVIEW_HARNESS_PREFIXES)


def _strip_background_review_harness(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop harness messages and the curator-mode assistant reply that immediately followed each."""
    if not messages:
        return messages
    out: List[Dict[str, Any]] = []
    skip_next_assistant = False
    for msg in messages:
        if _is_background_review_harness_message(msg):
            skip_next_assistant = True
            continue
        if skip_next_assistant:
            skip_next_assistant = False
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                continue  # the curator-mode reply to the harness prompt
        out.append(msg)
    return out


# Matches a bare protocol/tool-name marker such as "[memory]" or "[skill_manage]".
_STALE_TOOL_CALL_MARKER_RE = re.compile(r"^\[[A-Za-z_][A-Za-z0-9_.-]*\]$")


def _is_stale_tool_call_marker_message(msg: Dict[str, Any]) -> bool:
    """Assistant tool-call turn whose content is a bare ``[marker]`` (an older
    conversation_loop persisted a local template's marker as the final response)."""
    if not isinstance(msg, dict) or msg.get("role") != "assistant" or not msg.get("tool_calls"):
        return False
    content = msg.get("content")
    return isinstance(content, str) and bool(_STALE_TOOL_CALL_MARKER_RE.fullmatch(content.strip()))


def _strip_stale_tool_call_markers(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Blank stale ``[marker]`` assistant content (replaying it teaches the model
    to keep emitting it); tool_call/result pairing stays intact."""
    repaired = 0
    for msg in filter(_is_stale_tool_call_marker_message, messages):
        msg["content"] = ""
        repaired += 1
    if repaired:
        logger.info(
            "Cleared %d stale tool-call marker message(s) while restoring session (#78148)", repaired,
        )
    return messages


def format_session_db_unavailable(prefix: str = "Session database not available") -> str:
    """User-facing message with the captured init cause (+ WAL-docs hint for NFS/SMB locking failures)."""
    cause = get_last_init_error()
    if not cause:
        return f"{prefix}."
    hint = " (state.db may be on NFS/SMB/FUSE/ZFS — see https://www.sqlite.org/wal.html)"
    return f"{prefix}: {cause}{hint if any(m in cause.lower() for m in _WAL_INCOMPAT_MARKERS) else ''}."


# Auto-repair at most once per DB path per process (no repair loops; serialises concurrent
# web_server / gateway opens on the same malformed file).
_repair_attempted_paths: set[str] = set()
_repair_attempt_lock = threading.Lock()
# Cross-process schema-surgery lock timeout (``_repair_attempt_lock`` covers one interpreter
# only); sized for the slowest legitimate holder (VACUUM, multi-GB DB).
_REPAIR_LOCK_TIMEOUT_SECONDS = 120.0
_IS_WINDOWS = sys.platform == "win32"


def divert_session_transcript_jsonl(session_id: str, messages) -> "Optional[Path]":
    """Append pending messages to HERMES_HOME/sessions/<id>.jsonl (state.db was replaced under a
    live process). Returns the path, or None if nothing to write."""
    sid = str(session_id or "").strip()
    if not sid or not messages:
        return None
    sessions_dir = get_hermes_home() / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{sid}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for msg in messages:
            if msg is not None:
                record = msg if isinstance(msg, dict) else {"content": str(msg)}
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return path


# Process-wide shared SessionDB registry: long-lived in-process callers share ONE writer
# connection per resolved path via hermes_state_registry.acquire(); one-shots use SessionDB() + close().
def _foreign_state_db_holders(db_path: Path) -> List[Tuple[int, str]]:
    """Compatibility delegate to the state-holder authority."""
    return _state_holders.foreign_state_db_holders(db_path)


# ── Process-wide shared SessionDB registry (#90837) ── lives in hermes_state_registry.py (acquire /
# release / close_all / release_or_close). Long-lived in-process callers (gateway, tui_gateway, cron,
# in-process tools) share ONE writer connection per resolved path via hermes_state_registry.acquire(); CLI
# one-shots, recovery flows, and read-only cross-profile opens use SessionDB() directly with their own close().


class SessionDB(
    SessionSessionsMixin, SessionFtsSetupMixin, SessionSearchMixin, SessionSchemaMixin,
    SessionPortabilityMixin, SessionTelegramTopicsMixin, SessionCompressionMixin,
    SessionGatewayMixin, SessionMaintenanceMixin, SessionUsageMixin, SessionTitlesMixin,
    SessionMessagesMixin,
):
    """SQLite-backed session storage with FTS5 search; many reader threads, one writer (WAL)."""

    # Only these state-owned producers join automatic stale-open reconciliation; messaging/UI
    # sources have their own lifecycle owners; unknown sources fail closed.
    # See #60609.
    _AUTO_PRUNE_STALE_OPEN_SOURCES: Tuple[str, ...] = (
        "cli", "cron", "kanban", "acp", "api_server", "subagent", "tool",
    )

    # ── Write-contention tuning ──
    # SQLite's deterministic busy handler convoys under many hermes processes: keep its
    # timeout short (1s) and retry with random jitter. Patience is TIME-based (a sibling
    # legitimately holds the lock for seconds: checkpoint at close, VACUUM, recovery, FTS
    # optimize); attempt-counted budgets destroyed turns on a healthy store. Transcript
    # writes (failure aborts the turn) get the long budget; observation-only activity
    # writes sit on the response-critical path and get a sub-second one.
    _WRITE_PATIENCE_S, _TRANSCRIPT_WRITE_PATIENCE_S, _ACTIVITY_WRITE_PATIENCE_S = 20.0, 60.0, 0.5
    # A live compression lock gets a short wait (compression publishes in seconds), but the lease
    # is a correctness boundary: a writer still locked out afterwards is refused.
    # Observation-only activity heartbeat/label writes (#76354 review S1): these run on (or adjacent to) the
    # response-critical path and must never wait out the full routine patience under contention. Sub-second
    # budget; a skipped write is retried naturally at the next heartbeat window.
    # A live compression lock gets its own, much shorter budget than the write lock. Compression publishes
    # in a couple of seconds, so a brief wait saves the overwhelming majority of concurrent turns (#75083).
    # It deliberately stays short: the lease is a correctness boundary, not just a busy signal (see
    # test_compression_lease_blocks_non_owner_but_allows_owner_flush), so a writer that is still locked out
    # after this budget must still be refused rather than allowed to land a stale turn in a session whose
    # compression is genuinely long-running or wedged.
    _COMPRESSION_BUSY_WAIT_S = 5.0
    _WRITE_RETRY_MIN_S, _WRITE_RETRY_MAX_S = 0.020, 0.150  # fast jitter for the first _SLOW_AFTER_S
    _WRITE_RETRY_SLOW_AFTER_S = 2.0
    _WRITE_RETRY_SLOW_MIN_S, _WRITE_RETRY_SLOW_MAX_S = 0.250, 1.000
    # PASSIVE WAL checkpoint every N successful writes.
    _CHECKPOINT_EVERY_N_WRITES = 50
    # Bounded FTS ``'merge'`` (ms of lock each) instead of ``'optimize'`` (9-18s per index on a 10GB
    # DB, longer than a writer's patience); up to _COMMANDS_PER_PASS per index, stopping on no-progress.
    _FTS_MERGE_EVERY_N_WRITES, _FTS_MERGE_MAX_PAGES_PER_INDEX, _FTS_MERGE_COMMANDS_PER_PASS = 1000, 500, 4
    # Imports cap lower than exports: an import holds one BEGIN IMMEDIATE.
    _IMPORT_MAX_SESSIONS, _IMPORT_MAX_MESSAGES_PER_SESSION, _IMPORT_MAX_TOTAL_MESSAGES = 500, 10_000, 50_000
    _IMPORT_MAX_SESSION_BYTES, _IMPORT_MAX_TOTAL_BYTES = 5 * 1024 * 1024, 25 * 1024 * 1024
    # Accounting workers retire when idle so a bound-method target can't keep an abandoned SessionDB alive.
    _TOKEN_WRITER_IDLE_SECONDS = 30.0

    @staticmethod
    def _store_system_prompt(conn, system_prompt: Optional[str]) -> Optional[str]:
        if system_prompt is None:
            return None
        prompt_hash = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT OR IGNORE INTO system_prompts (hash, prompt) VALUES (?, ?)",
            (prompt_hash, system_prompt),
        )
        return prompt_hash

    @staticmethod
    def _delete_unreferenced_system_prompts(conn) -> None:
        conn.execute(
            "DELETE FROM system_prompts WHERE NOT EXISTS ("
            "SELECT 1 FROM sessions WHERE sessions.system_prompt_hash = system_prompts.hash)"
        )

    @staticmethod
    def _session_row_dict(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        if "_system_prompt_resolved" in data:
            resolved = data.pop("_system_prompt_resolved")
            if "system_prompt" in data:
                data["system_prompt"] = resolved
        return data

    @staticmethod
    def _close_connection_quietly(conn: Optional[sqlite3.Connection]) -> None:
        """Close a partially initialized connection without masking its error."""
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            logger.debug("Could not close a SessionDB connection", exc_info=True)

    def _close_conn_logged(self, conn, label: str) -> None:
        """Close *conn*; a failing close leaks a tracked fd: logged at WARNING, never swallowed."""
        try:
            conn.close()
        except Exception as exc:
            logger.warning("%s close failed for %s: %s", label, self.db_path, exc)

    def __init__(self, db_path: Path = None, read_only: bool = False):
        self.db_path = db_path or _default_db_path()
        _ensure_test_isolation(self.db_path)  # before any connection/pragma/mkdir
        self.read_only = read_only
        self._lock = threading.Lock()
        # Read-path split (WAL only): reads borrow from a BOUNDED read-only pool so they
        # never queue behind writer flushes on self._lock (see _read_ctx); unbounded
        # per-thread connections pinned fds for the process lifetime and hit EMFILE.
        self._read_pool: "queue.LifoQueue[sqlite3.Connection]" = queue.LifoQueue(maxsize=_READ_POOL_MAX)
        # Permits bound PEAK descriptors (the pool bounds only the idle set), shared per
        # DATABASE PATH; acquired non-blocking so a permitless reader degrades to the writer lock.
        # One permit per live read connection, held from before the open in _get_read_conn() until after the
        # close in _close_read_conn(). See _READ_POOL_MAX. Acquired non-blocking on purpose: a reader that
        # cannot get a permit must degrade to the writer lock, not queue here — blocking would convert fd
        # exhaustion into a stall, which is the same outage with a different stack trace. Permits are shared
        # per DATABASE PATH, not per instance: the descriptors they ration belong to the file, and one
        # process holds several SessionDB objects on the same state.db (#98573). See _PathReadBudget.
        self._read_budget = _read_budget_for(self.db_path)
        self._read_budget.register(self)
        self._read_permits = self._read_budget.permits
        self._read_conns_lock = threading.Lock()
        # Set when close() begins; an in-flight reader then closes its own connection
        # instead of re-populating a pool nobody will drain again.
        self._read_conns_closed = False
        # Read-open failure backoff is a TIMESTAMP, not a sticky bool: the likeliest trigger
        # is transient EMFILE, and a permanent flag would demote every reader forever.
        self._read_open_failed_at = 0.0
        self._wal_active, self._write_count = False, 0
        # File identity of the opened state.db, compared on every write so an out-of-band
        # replace cannot limp through in-place surgery (inode: mv/new-file; application_id: cp).
        self._db_file_identity: Optional[tuple] = None
        self._db_file_application_id: int = 0
        self._db_sidecar_identity: Dict[str, tuple] = {}
        self._db_replaced = self._db_wal_generation_lost = False
        self._db_corrupt, self._db_corrupt_reason = False, ""  # sticky quarantine (StateDbCorruptError)
        self._fts_usermerge_floor_applied = False  # one-shot usermerge-floor write guard
        self._fts_enabled = self._fts_stale = self._trigram_available = False
        # _fts_cjk_loaded: tokenizer on the writer connection; _fts_cjk_available: messages_fts_cjk
        # is queryable AND not marked stale.
        self._fts_cjk_loaded = self._fts_cjk_available = self._fts_unavailable_warned = False
        self._conn = None
        # Async token accounting; distinct from self._lock so enqueue/flush never contends with writes.
        self._token_queue: deque = deque()
        self._token_queue_cond = threading.Condition(threading.Lock())
        self._token_writer_thread: Optional[threading.Thread] = None
        self._token_writer_stop = self._token_writer_busy = False
        self._token_atexit_hook: Optional[Callable[[], None]] = None
        # Opened via hermes_state_registry.acquire(): close() releases a refcount instead.
        # Set True when this instance is opened via hermes_state_registry.acquire(). Makes close() a no-op so the
        # registry (not individual callers) controls the connection lifecycle (#90837).
        self._shared_registry_owned = False
        initialization_complete = False
        try:
            if read_only:
                self._open_read_only()
            else:
                self._open_writer()
            self._record_db_file_identity()
            initialization_complete = True
        except Exception as exc:
            # Surface WHY via /resume and friends; callers keep their ``_session_db = None`` path.
            _set_last_init_error(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if not initialization_complete:
                conn, self._conn = self._conn, None
                self._close_connection_quietly(conn)

    def _open_writer(self) -> None:
        """Writable open: preflight, zero-byte quarantine, connect + schema (one in-place repair of a
        malformed sqlite_master), generation stamp."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Read-only file/sidecar preflight BEFORE the first connection: an actionable message
        # instead of an opaque "attempt to write a readonly database" from inside _init_schema.
        preflight_db_writability(self.db_path, db_label="state.db")
        try:
            # Serialize zero-byte check, quarantine, connect and schema commit so concurrent
            # openers don't race the absent-path -> schema-commit window.
            if not self.db_path.exists() or is_zeroed_state_db(self.db_path):
                with quarantine_cross_process_lock(self.db_path) as lock_acquired:
                    if not lock_acquired:
                        logger.warning(
                            "startup quarantine lock for %s not acquired within 5s; proceeding",
                            self.db_path,
                        )
                    self._handle_quarantine_if_zeroed(already_locked=lock_acquired)
                    self._connect_and_init_with_lock_patience()
            else:
                self._handle_quarantine_if_zeroed(already_locked=False)
                self._connect_and_init_with_lock_patience()
        except sqlite3.DatabaseError as exc:
            # A malformed schema fails on the very first statement (before _init_schema), so the
            # FTS-rebuild layer never sees it: repair sqlite_master in place (backup first), reopen once.
            if not is_malformed_schema_error(exc) or not _claim_repair_attempt(self.db_path):
                raise
            logger.error(
                "state.db schema is malformed (%s) — attempting automatic "
                "repair (a backup copy is made first).", exc,
            )
            self._close_connection_quietly(self._conn)
            if not repair_state_db_schema(self.db_path).get("repaired"):
                raise
            self._connect_and_init_with_lock_patience()
        # FTS optimization is OPT-IN (`hermes db optimize`); no background worker races session lifecycle.
        self._ensure_db_file_generation()

    def _open_read_only(self) -> None:
        """Read-only attach for cross-profile aggregation: no schema init, NO write
        lock (sidebar polling never contends with that profile's backend); the DB
        must exist. FTS flags are probed with SELECTs only, and the connection is
        closed on ANY probe failure (malformed schema raises DatabaseError) so a
        leaked tracked connection cannot block the forensic backup the writable heal takes next."""
        for attempt in range(_READ_ONLY_IOERR_RETRY_ATTEMPTS + 1):
            try:
                self._conn = conn = self._connect_read_only(timeout=1.0)
                try:
                    apply_database_pragmas(conn, db_label="state.db")
                    cursor = conn.cursor()
                    self._fts_enabled = self._fts_table_probe(cursor, "messages_fts") is True
                    if self._fts_enabled:
                        self._trigram_available = (
                            self._fts_table_probe(cursor, "messages_fts_trigram") is True
                        )
                except BaseException:
                    self._conn = None
                    self._close_connection_quietly(conn)
                    raise
                return
            except sqlite3.OperationalError as ioerr:
                # In-flight WAL checkpoint/reset/frame-flush on the writer side can surface
                # SQLITE_IOERR to a mode=ro reader (it can't do the -shm recovery the read
                # needs). Closes in milliseconds: retry a bounded number of times before
                # classifying the store as failed (#100436; see _READ_ONLY_IOERR_RETRY_ATTEMPTS).
                transient = _DISK_IO_ERROR_MARKER in str(ioerr).lower()
                if attempt >= _READ_ONLY_IOERR_RETRY_ATTEMPTS or not transient:
                    raise
                time.sleep(_READ_ONLY_IOERR_RETRY_BACKOFF_S)

    def _connect_read_only(self, timeout: float) -> sqlite3.Connection:
        """``mode=ro`` tracked connection with Row factory. check_same_thread=False: pooled connections
        are borrowed by whichever thread reads next; exclusive ownership is enforced by pool checkout."""
        conn = _connect_tracked_db(
            f"file:{self.db_path}?mode=ro", tracking_path=self.db_path, uri=True,
            check_same_thread=False, timeout=timeout, isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        return conn

    def _handle_quarantine_if_zeroed(self, already_locked: bool = False) -> None:
        """Quarantine a zero-byte/headerless state.db so a fresh one can open; if quarantine failed,
        raise the clear message instead of opening the zeroed file."""
        if not (self.db_path.exists() and is_zeroed_state_db(self.db_path)):
            return
        try:
            zsize = self.db_path.stat().st_size
        except OSError:
            zsize = -1
        qpath = quarantine_zeroed_state_db(self.db_path, already_locked=already_locked)
        msg = (
            f"state.db looks ZEROED ({zsize} bytes, no SQLite header). "
            f"Preserved at {qpath or '(quarantine failed — file left in place)'}. "
            f"Restore from {self.db_path.parent / 'state-snapshots'} via `hermes snapshot list` / "
            f"`hermes snapshot restore <id>` if available. "
            "Opening a fresh empty database so the agent can start."
        )
        logger.error(msg)
        _set_last_init_error(msg)
        if qpath is None and self.db_path.exists() and is_zeroed_state_db(self.db_path):
            raise sqlite3.DatabaseError(msg)

    def _open_writer_conn(self) -> sqlite3.Connection:
        """Connect + WAL/pragma/tokenizer setup for a writer connection (no schema init). Short timeout:
        jittered application-level retry handles contention, not SQLite's busy handler;
        isolation_level=None: explicit BEGIN IMMEDIATE."""
        conn = _connect_tracked_db(
            str(self.db_path), check_same_thread=False, timeout=1.0, isolation_level=None,
        )
        try:
            conn.row_factory = sqlite3.Row
            self._wal_active = apply_wal_with_fallback(conn, db_label="state.db") == "wal"
            apply_database_pragmas(conn, db_label="state.db")
            conn.execute("PRAGMA foreign_keys=ON")
            self._fts_cjk_loaded = load_fts5_cjk_extension(conn)
        except BaseException:
            self._close_connection_quietly(conn)
            raise
        return conn

    def _connect_and_init(self) -> None:
        # Refuse before sqlite3.connect (under the startup lock) so we cannot mint
        # a replacement WAL while a live writer still holds a deleted sidecar inode.
        refuse_deleted_wal_generation(self.db_path)
        self._conn = self._open_writer_conn()
        self._init_schema()

    def _connect_and_init_with_lock_patience(self) -> None:
        """Open + init, waiting out a sibling's write lock with jittered patience:
        _init_schema's DDL runs on a 1s-timeout connection, so a sibling's VACUUM
        or checkpoint used to fail the ENTIRE open and callers disabled
        persistence for the whole run. Non-lock errors propagate immediately."""
        # Lock contention during open: _init_schema's DDL/reconcile statements run on a 1s-timeout
        # connection with no retry, so a sibling process holding the write lock (VACUUM, TRUNCATE checkpoint
        # at close, a long FTS pass from an older still-running install) used to fail the ENTIRE open —
        # callers then disable persistence for the whole run ("Failed to initialize SessionDB ... database
        # is locked", #74478). The store is healthy; wait it out with the same jittered patience the write
        # path uses.
        deadline = time.monotonic() + self._WRITE_PATIENCE_S
        while True:
            try:
                self._connect_and_init()
                return
            except sqlite3.OperationalError as exc:
                err = str(exc).lower()
                if "locked" not in err and "busy" not in err:
                    raise
                self._close_connection_quietly(self._conn)
                now = time.monotonic()
                if now >= deadline:
                    raise
                jitter = random.uniform(self._WRITE_RETRY_SLOW_MIN_S, self._WRITE_RETRY_SLOW_MAX_S)
                time.sleep(min(jitter, max(deadline - now, 0.001)))

    # ── Read-path split ──

    def _get_read_conn(self) -> Optional[sqlite3.Connection]:
        """Open a fresh read-only connection, or None when unavailable (callers
        return it to self._read_pool). WAL only: WAL readers never block on the
        writer, so reads skip self._lock; under DELETE journal mode (NFS fallback)
        readers hit SQLITE_BUSY storms, so the legacy locked path stays. Autocommit
        reads see everything committed so far (read-your-writes for flush-then-search)."""
        if not self._wal_active or self.read_only:
            return None
        with self._read_conns_lock:
            failed_at = self._read_open_failed_at
            backing_off = failed_at and time.monotonic() - failed_at < _READ_OPEN_RETRY_SECONDS
            if self._read_conns_closed or backing_off:
                return None
        # Permit BEFORE the open: openers race for permits, not descriptors.
        if not self._read_budget.acquire(self):
            logger.debug(
                "read pool at capacity (%d) for %s; serving this read from the "
                "locked writer connection", _READ_POOL_MAX, self.db_path,
            )
            return None
        conn = None  # bound before the try so the handlers can close a half-open one
        try:
            conn = self._connect_read_only(timeout=5.0)
            apply_database_pragmas(conn, db_label="state.db")
            if self._fts_cjk_loaded:  # registers in the connection, not the file: ro is fine
                load_fts5_cjk_extension(conn)
        except BaseException as exc:
            # A half-open connection (open ok, extension load failed) is a live tracked descriptor,
            # the leak shape this pool exists to fix; a stranded permit would shrink the read
            # path by one slot forever. (Not _close_read_conn: callers release their own permit.)
            if conn is not None:
                self._close_conn_logged(conn, "partially-opened read conn")
            self._read_budget.release()
            if not isinstance(exc, sqlite3.Error):
                raise
            with self._read_conns_lock:
                self._read_open_failed_at = time.monotonic()
            logger.debug("read-only connection open failed for %s", self.db_path, exc_info=True)
            return None
        return conn

    def _evict_one_idle_read_conn(self) -> bool:
        """Close one idle pooled connection (a peer on the same file wants its permit); never a live one."""
        try:
            conn = self._read_pool.get_nowait()
        except queue.Empty:
            return False
        self._close_read_conn(conn)
        return True

    def _close_read_conn(self, conn) -> None:
        """Close a pooled read connection and release its permit even when the close fails (withholding
        it would narrow the read path forever). Over-releasing the BoundedSemaphore raises ValueError."""
        try:
            self._close_conn_logged(conn, "read-conn")
        finally:
            self._read_budget.release()

    def _checkout_read_conn(self) -> Optional[sqlite3.Connection]:
        """Borrow a read connection, opening on a miss; None when the read path is unavailable.
        A pool hit costs no permit (the connection already holds one)."""
        if not self._wal_active or self.read_only:
            return None
        try:
            return self._read_pool.get_nowait()
        except queue.Empty:
            return self._get_read_conn()

    @contextmanager
    def _read_ctx(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection for read-only statements: a pooled read-only
        connection with NO lock under WAL; otherwise (non-WAL, open failure,
        ceiling reached) the writer connection under self._lock — deliberate
        degradation: slower beats EMFILE, which the supervisor cannot see."""
        conn = self._checkout_read_conn()
        if conn is not None:
            try:
                yield conn
            finally:
                returned = False
                with self._read_conns_lock:
                    if not self._read_conns_closed:
                        try:
                            self._read_pool.put_nowait(conn)
                            returned = True
                        except queue.Full:
                            pass
                if not returned:
                    # close() drained the pool (or queue.Full: unreachable while
                    # permits == maxsize, load-bearing if they drift): surplus.
                    self._close_read_conn(conn)
            return
        with self._lock:
            if self._conn is None:  # close() raced a still-unwinding reader
                self._reopen_after_close_locked(context="read")
            yield cast(sqlite3.Connection, self._conn)

    def _reopen_after_close_locked(self, context: str = "write") -> None:
        """Reopen the writer after ``close()`` raced a live caller (a teardown owner
        set ``_conn = None`` while a worker still had a transcript flush to land).
        Loud (WARNING) and bounded (only after an explicit close()). Caller holds
        ``self._lock``. No _init_schema: no DDL races with siblings during teardown."""
        if self.read_only:
            raise sqlite3.ProgrammingError(
                f"SessionDB for {self.db_path} was closed (read-only handle); "
                f"cannot serve a {context} after close()"
            )
        # A reopen resolves the PATH again: a replaced file would be written through stale WAL/shm
        # assumptions; a quarantined handle must never hand a fresh connection to a damaged file.
        if self._db_corrupt and not (self._db_replaced or self._db_file_was_replaced()):
            raise self._corrupt_error(
                f"state.db connection for {self.db_path} is quarantined after "
                f"structural corruption; refusing to reopen for a {context} "
                "after close(). "
            )
        self._halt_if_db_generation_changed()
        logger.warning(
            "state.db connection for %s was closed while a %s was still in "
            "flight — reopening (teardown/worker race, #94736)", self.db_path, context,
        )
        try:
            self._conn = self._open_writer_conn()
        except Exception as exc:
            raise sqlite3.OperationalError(
                f"state.db connection was closed while a {context} was still "
                f"in flight (a session-teardown path called close() before "
                f"this worker finished — #94736) and the automatic reopen failed: {exc}"
            ) from exc

    def _execute_write(
        self, fn: Callable[[sqlite3.Connection], T], patience_s: Optional[float] = None,
    ) -> T:
        """Run *fn(conn)* inside BEGIN IMMEDIATE with jittered lock retry; commit
        is handled here (callers must not commit). Returns *fn*'s result.
        BEGIN IMMEDIATE takes the WAL write lock up front so contention surfaces
        immediately; on locked/busy the Python lock is released, a jitter slept,
        and the WHOLE callback retried — *fn* must stay idempotent under retry."""
        if patience_s is None:
            patience_s = self._WRITE_PATIENCE_S
        deadline = time.monotonic() + patience_s
        compression_deadline: Optional[float] = None  # set on the first compression-busy collision
        # One retry for SQLITE_IOERR raised by BEGIN IMMEDIATE itself (callback not run: nothing
        # replayed). Once fn has started, an IOERR leaves settlement unknown and must propagate.
        # The callback has not run at that point, so there is no durable effect to replay and the retry is
        # exactly-once safe (#99502's contract). Once the callback starts, an IOERR leaves the write's
        # settlement unknown and must propagate — this helper owns non-idempotent transcript/counter
        # mutations, not just idempotent UPSERTs.
        ioerr_begin_retried = False
        while True:
            self._raise_if_db_corrupt()
            self._raise_if_db_replaced()
            fn_started = False
            try:
                with self._lock:
                    if self._conn is None:  # close() raced this writer
                        self._reopen_after_close_locked(context="write")
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        fn_started = True
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                # Success — periodic best-effort checkpoint + FTS merge.
                self._write_count += 1
                if self._write_count % self._CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                if self._write_count % self._FTS_MERGE_EVERY_N_WRITES == 0:
                    self._try_incremental_merge_fts()
                return result
            except SessionCompressionInProgressError:
                # Transient (see _COMPRESSION_BUSY_WAIT_S): a steer landing mid-compression must not abort.
                # A live foreign compression lock is transient: the compressor publishes in a couple of
                # seconds. Without any wait, a steer that lands mid-compression aborts the user's turn as
                # session_persistence_failed and sends the operator hunting disk space that was never the
                # problem (#75083). The budget is _COMPRESSION_BUSY_WAIT_S, not the write-lock patience: the
                # lease is a correctness boundary, so a writer still locked out after a short wait must be
                # refused rather than left to land a stale turn once a long-running or wedged compression
                # finally lets go.
                if compression_deadline is None:
                    compression_deadline = min(time.monotonic() + self._COMPRESSION_BUSY_WAIT_S, deadline)
                if self._sleep_before_write_retry(
                    compression_deadline, self._COMPRESSION_BUSY_WAIT_S
                ):
                    continue
                raise
            except sqlite3.Error as exc:
                # 'no more rows' is a transient engine error on contended WAL appends (some builds
                # raise it as InterfaceError, a sibling of DatabaseError): retry like locked/busy.
                if _is_no_more_rows(exc) and self._sleep_before_write_retry(deadline, patience_s):
                    continue
                err_msg = str(exc).lower()
                if isinstance(exc, sqlite3.OperationalError):
                    if "locked" in err_msg or "busy" in err_msg:
                        if self._sleep_before_write_retry(deadline, patience_s):
                            continue
                        # Say what actually happened, not disk/permission damage.
                        raise sqlite3.OperationalError(
                            f"database is locked (another Hermes process held the "
                            f"state.db write lock for over {patience_s:.0f}s — "
                            "likely a long maintenance operation such as VACUUM, "
                            "a large WAL checkpoint, or an older pre-update "
                            "process; the database itself is healthy)"
                        ) from exc
                    if (
                        _DISK_IO_ERROR_MARKER in err_msg and not fn_started and not ioerr_begin_retried
                        and self._sleep_before_write_retry(deadline, patience_s)
                    ):
                        # Retry on the SAME connection: close()+reopen would cancel this process's
                        # POSIX locks for every sibling (howtocorrupt §2.2).
                        ioerr_begin_retried = True
                        continue
                    raise  # non-lock error, callback already ran, or patience exhausted
                if isinstance(exc, sqlite3.DatabaseError):
                    # An out-of-band replace surfaces as this same corruption class; in-file repair
                    # on a NEW generation amplifies the damage.
                    if (
                        "not a database" in err_msg or is_malformed_db_error(exc)
                        or self._is_fts_write_corruption_error(exc)
                    ):
                        self._raise_if_db_replaced()
                    # Corrupt FTS shadow tables fail every write via the sync triggers while canonical
                    # rows are intact: detach the derived indexes atomically and retry (never rebuild here).
                    if self._enter_fts_fail_open(exc):
                        continue
                    # What survives both checks is structural damage: quarantine.
                    if self._is_structural_corruption_error(exc):
                        self._halt_db_corrupt(exc)
                raise

    def _write_sql(
        self, sql: str, params: Any = (), *, many: bool = False, patience_s: Optional[float] = None,
    ) -> None:
        """Run one INSERT/UPDATE/DELETE through ``_execute_write``."""
        def _do(conn):
            (conn.executemany if many else conn.execute)(sql, params)
        self._execute_write(_do, patience_s=patience_s)

    def _write_rowcount(self, sql: str, params: Any = (), *, patience_s: Optional[float] = None) -> int:
        """Run one UPDATE/DELETE through ``_execute_write``; return rows changed
        (``SELECT changes()`` when the driver reports None / negative)."""
        def _do(conn):
            rowcount = conn.execute(sql, params).rowcount
            if rowcount is None or rowcount < 0:
                rowcount = conn.execute("SELECT changes()").fetchone()[0]
            return rowcount
        return self._execute_write(_do, patience_s=patience_s)

    def _read_one(self, sql: str, params: Any = ()) -> Optional[sqlite3.Row]:
        """``fetchone()`` of one read-only statement via ``_read_ctx``."""
        with self._read_ctx() as conn:
            return conn.execute(sql, params).fetchone()

    def _read_all(self, sql: str, params: Any = ()) -> List[sqlite3.Row]:
        """``fetchall()`` of one read-only statement via ``_read_ctx``."""
        with self._read_ctx() as conn:
            return conn.execute(sql, params).fetchall()

    def _ensure_db_file_generation(self) -> None:
        """Mint a once-per-file generation stamp (state_meta + application_id). First opener wins (INSERT
        OR IGNORE); application_id is written only while 0 so racers converge. PASSIVE checkpoint only.

        See #45383.
        """
        if self.read_only or self._conn is None:
            return
        token = uuid.uuid4().hex
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR IGNORE INTO state_meta (key, value) VALUES (?, ?)",
                    (_STATE_DB_GENERATION_KEY, token),
                )
                row = self._conn.execute(
                    "SELECT value FROM state_meta WHERE key = ?", (_STATE_DB_GENERATION_KEY,),
                ).fetchone()
                if row and row[0]:
                    token = str(row[0])
                pragma_row = self._conn.execute("PRAGMA application_id").fetchone()
                current = int(pragma_row[0] or 0) if pragma_row else 0
                if current == 0:
                    current = (int(token[:8], 16) & 0x7FFFFFFF) or 1
                    self._conn.execute(f"PRAGMA application_id={current}")
                self._db_file_application_id = current
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except sqlite3.Error:
                    pass
        except sqlite3.Error as exc:
            logger.debug("state.db generation stamp skipped: %s", exc)

    def _record_db_file_identity(self) -> None:
        """Snapshot inode plus the on-disk generation header when present."""
        self._db_file_identity = _stat_db_file_identity(self.db_path)
        self._db_sidecar_identity = _stat_sqlite_sidecar_identity(self.db_path)
        disk_id = _read_sqlite_application_id(self.db_path)
        if disk_id:
            self._db_file_application_id = disk_id
        elif self._conn is not None and not self._db_file_application_id:
            try:
                pragma_row = self._read_one("PRAGMA application_id")
            except sqlite3.Error:
                pragma_row = None
            if pragma_row and pragma_row[0]:
                self._db_file_application_id = int(pragma_row[0])

    def _db_file_was_replaced(self) -> bool:
        """True when the path no longer names the file this instance opened."""
        recorded = self._db_file_identity
        if recorded is not None and _stat_db_file_identity(self.db_path) != recorded:
            return True
        recorded_app = int(self._db_file_application_id or 0)
        if not recorded_app:
            return False
        # Header 0 = WAL not yet checkpointed, not a replace; a real replacement is nonzero.
        disk_app = _read_sqlite_application_id(self.db_path)
        return bool(disk_app and disk_app != recorded_app)

    def _wal_generation_was_lost(self) -> bool:
        """True when the WAL/SHM generation this handle opened is gone. Recorded
        generation: pure stat (no /proc walk on healthy writes). Empty identity
        (WAL appeared after open, or cleared by a clean close()): probe
        /proc/self/fd for deleted sidecars and adopt the current ones once clean."""
        recorded = self._db_sidecar_identity or {}
        base = os.fspath(self.db_path)
        if recorded:
            return any(
                _stat_db_file_identity(Path(base + suffix)) != ident for suffix, ident in recorded.items()
            )
        if not self._wal_active:  # no sidecar generation to lose; keep /proc off the hot path
            return False
        if sys.platform.startswith("linux"):
            watched = _watched_sqlite_sidecar_paths(self.db_path)
            try:
                for target in _proc_fd_targets(os.getpid()):
                    if " (deleted)" in target and _canonical_sqlite_path(target) in watched:
                        return True
            except OSError:
                return False
        # Probe clean (or unavailable): adopt the current sidecar generation.
        current_identity = _stat_sqlite_sidecar_identity(self.db_path)
        if current_identity:
            self._db_sidecar_identity = current_identity
        return False

    def _halt_if_db_generation_changed(self) -> None:
        """Stop writes (logging once) when the file was replaced or its WAL/SHM generation
        is gone: never run in-file repair on a new generation, never keep committing on a
        split WAL. Both flags are sticky."""
        # A reopen resolves the PATH again — if the file at that path is no longer the one this instance
        # originally opened (out-of-band restore/cp/mv), reconnecting would write into the new generation
        # through stale WAL/shm assumptions (#89332). Refuse instead.
        if self._db_replaced or self._db_file_was_replaced():
            self._db_replaced = True
            logger.error(_STATE_DB_REPLACED_MSG)
            raise StateDbReplacedError(_STATE_DB_REPLACED_MSG)
        if self._db_wal_generation_lost or self._wal_generation_was_lost():
            self._db_wal_generation_lost = True
            logger.error(_DELETED_WAL_GENERATION_MSG)
            raise DeletedWalGenerationError(_DELETED_WAL_GENERATION_MSG)

    def _raise_if_db_replaced(self) -> None:
        """Sticky-flag fast path (no log spam on every write), then the live probe."""
        if self._db_replaced:
            raise StateDbReplacedError(_STATE_DB_REPLACED_MSG)
        if self._db_wal_generation_lost:
            raise DeletedWalGenerationError(_DELETED_WAL_GENERATION_MSG)
        self._halt_if_db_generation_changed()

    @classmethod
    def _is_structural_corruption_error(cls, exc: BaseException) -> bool:
        """Bare SQLITE_CORRUPT/NOTADB with no FTS provenance: canonical B-tree/schema/freelist damage,
        never repairable from the live write path."""
        return (
            isinstance(exc, sqlite3.DatabaseError)
            and not isinstance(exc, StateDbCorruptError)
            and not cls._is_fts_write_corruption_error(exc)
            and classify_persistence_error(exc) == "corrupt"
        )

    def _corrupt_error(self, prefix: str = "") -> "StateDbCorruptError":
        """Build the quarantine error for this handle (message assembled once)."""
        return StateDbCorruptError(f"{prefix}{_STATE_DB_CORRUPT_MSG} (cause: {self._db_corrupt_reason})")

    def _halt_db_corrupt(self, exc: BaseException) -> None:
        """Quarantine this handle and raise; never run in-file repair here."""
        self._db_corrupt = True
        self._db_corrupt_reason = str(exc)
        self._disable_close_time_checkpoint()
        logger.error(
            "state.db %s reported structural corruption outside the FTS "
            "indexes (%s); quarantining this handle: no further writes, no "
            "automatic reopen, no explicit WAL checkpoint at close. Stop the "
            "gateway and run `hermes sessions recover --source %s --inspect-only`.", self.db_path, exc,
            self.db_path,
        )
        err = self._corrupt_error()
        for attr in ("sqlite_errorcode", "sqlite_errorname"):
            if getattr(exc, attr, None) is not None:
                setattr(err, attr, getattr(exc, attr))
        raise err from exc

    def _disable_close_time_checkpoint(self) -> None:
        """Best-effort SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE (Python 3.12+): sqlite3's
        close() otherwise runs the internal last-connection checkpoint that wrote
        the incident's pages under wrong page numbers (see StateDbCorruptError).
        <3.12 has no setconfig; the residual checkpoint only carries
        pre-quarantine committed frames, which is tolerable."""
        flag = getattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", None)
        conn = self._conn
        setconfig = getattr(conn, "setconfig", None)
        if flag is None or setconfig is None:
            return
        try:
            setconfig(flag, True)
        except Exception:
            logger.debug(
                "Could not disable SQLite's close-time checkpoint on the quarantined handle for %s",
                self.db_path, exc_info=True,
            )

    def _raise_if_db_corrupt(self) -> None:
        if self._db_corrupt:
            raise self._corrupt_error()

    def _sleep_before_write_retry(self, deadline: float, patience_s: float) -> bool:
        """Sleep one jitter interval if the budget allows; True = retry, False = deadline passed. Small
        jitter for the first _WRITE_RETRY_SLOW_AFTER_S, then slow; never overshoots the deadline."""
        now = time.monotonic()
        if now >= deadline:
            return False
        slow = now - (deadline - patience_s) >= self._WRITE_RETRY_SLOW_AFTER_S
        jitter = random.uniform(*(
            (self._WRITE_RETRY_SLOW_MIN_S, self._WRITE_RETRY_SLOW_MAX_S) if slow
            else (self._WRITE_RETRY_MIN_S, self._WRITE_RETRY_MAX_S)
        ))
        time.sleep(min(jitter, max(deadline - now, 0.001)))
        return True

    def _foreign_state_db_holders(self) -> List[Tuple[int, str]]:
        """Foreign processes holding this DB or its WAL sidecars (see hermes_state_holders)."""
        return _foreign_state_db_holders(self.db_path)

    def _try_wal_checkpoint(self) -> None:
        """Best-effort PASSIVE WAL checkpoint; never raises. PASSIVE never blocks writers;
        TRUNCATE corrupted B-trees on 65K+ page databases under exclusive-lock I/O pressure.

        Previous TRUNCATE strategy caused B-tree corruption on large databases (65K+ pages) due to the
        exclusive-lock I/O pressure from checkpointing thousands of frames at once (issue #45383).
        """
        if self._db_corrupt:
            return  # quarantined: never checkpoint over a damaged image
        try:
            with self._lock:
                result = self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                if result and result[1] > 0:
                    logger.debug("WAL checkpoint: %d/%d pages checkpointed", result[2], result[1])
        except Exception as exc:
            logger.warning("WAL checkpoint (PASSIVE) failed: %s", exc)

    def __enter__(self) -> "SessionDB":
        """``with SessionDB(path) as db:`` closes on exit; owners must release deterministically.

        Ownership of a SessionDB should be released explicitly. Historically an instance with a started
        token writer pinned ITSELF (bound-method writer target plus a strong ``atexit`` drain hook), so
        ``__del__`` never ran for exactly the instances that leaked descriptors (#88033). The writer now
        retires after an idle window and the atexit hook holds only a weak reference, so abandoned handles
        are eventually collectible — but "eventually, after the idle window and a GC cycle" is not a release
        policy. Call sites owning a handle are still expected to close it deterministically (see the
        ownership comments in ``run_agent.py`` and ``tui_gateway/methods_session.py``).
        """
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False  # never suppress the caller's exception

    def close(self):
        """Drain queued token deltas, then a PASSIVE checkpoint on writable handles
        (NOT TRUNCATE: a full WAL reset races the gateway's live writer, tearing
        B-tree pages). A registry-shared instance RELEASES one refcount instead.

        Drains queued token deltas first (the background writer needs the connection). Read-only connections
        never request a checkpoint. See #45383.
        When this instance is shared (opened via ``hermes_state_registry.acquire``), ``close()`` RELEASES one
        refcount instead of tearing down the connection: the registry owns the lifecycle and only closes on
        the final release (#90837). This prevents one caller's close from tearing down the writer connection
        that other callers in the same process are still using — while still letting legacy ``close()`` call
        sites return their reference instead of leaking it.
        """
        if self._shared_registry_owned:
            from hermes_state_registry import release
            release(self)
            return
        self._stop_token_writer()
        hook, self._token_atexit_hook = self._token_atexit_hook, None
        if hook is not None:
            atexit.unregister(hook)
        # Closed flag first: an in-flight reader then closes its own connection.
        with self._read_conns_lock:
            self._read_conns_closed = True
        while self._evict_one_idle_read_conn():
            pass
        with self._lock:
            if self._conn:
                if self._db_corrupt:  # quarantined: no checkpoint over a damaged image
                    logger.warning(
                        "Skipping the close-time WAL checkpoint for %s: this "
                        "handle observed structural corruption (%s). Take a "
                        "snapshot of state.db, -wal and -shm before restarting, "
                        "then run `hermes sessions recover --source %s --inspect-only`.", self.db_path,
                        self._db_corrupt_reason, self.db_path,
                    )
                elif not self.read_only:  # PASSIVE, not TRUNCATE (see docstring)
                    try:
                        # Every cron run_agent opens+closes a transient SessionDB, so a TRUNCATE here fires
                        # a full WAL reset many times/hour, racing the gateway's long-lived writer on large
                        # WAL databases and tearing hot B-tree pages -- the #45383 corruption this class's
                        # own periodic checkpoint was already made PASSIVE to avoid. TRUNCATE belongs only
                        # on a sole-opener/quiescent connection.
                        self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    except Exception as exc:
                        logger.debug("WAL checkpoint (PASSIVE) at close failed: %s", exc)
                conn, self._conn = self._conn, None
                self._close_connection_quietly(conn)
                # A clean close lets SQLite unlink the sidecars (a legitimate end of the
                # generation, not a split): a teardown-race reopen must re-adopt.
                self._db_sidecar_identity = {}

    def __del__(self) -> None:
        """Safety net: close() if the caller forgot. Attribute access stays
        guarded: module teardown order is undefined."""
        if self.__dict__.get("_conn") is not None:
            try:
                self.close()
            except Exception:
                pass

    # ── Async token accounting (SessionUsageMixin) ──
    # queue_token_counts() is a deque append; a single-writer thread applies deltas in
    # order, coalescing consecutive deltas whose route fields are EQUAL (so the merged
    # UPDATE equals applying them sequentially). Exact readers call flush_token_counts().
    _TOKEN_DELTA_SUM_FIELDS = (
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
        "api_call_count",
    )
    _TOKEN_DELTA_COST_FIELDS = ("estimated_cost_usd", "actual_cost_usd")
    _TOKEN_DELTA_ROUTE_FIELDS = (
        "model", "cost_status", "cost_source", "pricing_version", "billing_provider", "billing_base_url",
        "billing_mode",
    )

    MAX_TITLE_LENGTH = 100

    # Title provenance, lowest to highest authority: auto-titling may only replace a
    # strictly lower-authority title (``derived`` -> ``llm`` once; never a user-typed name).
    TITLE_SOURCE_DERIVED, TITLE_SOURCE_LLM, TITLE_SOURCE_USER = "derived", "llm", "user"
    _TITLE_SOURCE_RANK = {TITLE_SOURCE_DERIVED: 0, TITLE_SOURCE_LLM: 1, TITLE_SOURCE_USER: 2}

    # Bot Mode's canonical chat is resolved by exact-title lookup: the title IS the identity,
    # so _set_session_title refuses renames of a hidden row holding it.
    # Bot Mode's forever-chat registry: the session titled exactly this, on a bot's profile, IS the bot's
    # canonical chat — resolved by exact-title lookup on every open (no session-id pointer exists). See
    # #92473.
    CANONICAL_BOT_CHAT_TITLE = "Bot Chat"

    # ── Message storage constants (SessionMessagesMixin) ──
    # Prefix marking JSON-encoded structured content; NUL cannot collide with text.
    _CONTENT_JSON_PREFIX = "\x00json:"
    #: Reactions live inside ``display_metadata`` so they survive row rewrites.
    REACTIONS_METADATA_KEY = "reactions"
    # Columns every conversation projection decodes; ``active`` rides along so a display read
    # can split compaction-archived rows without a second query.
    _CONVERSATION_ROW_COLUMNS = (
        "id, role, content, tool_call_id, tool_calls, tool_name, effect_disposition, "
        "finish_reason, reasoning, reasoning_content, reasoning_details, "
        "codex_reasoning_items, codex_message_items, platform_message_id, observed, "
        "_compressed_summary, timestamp, active, api_content, display_kind, display_metadata"
    )

    # ── Meta key/value (scheduler bookkeeping) ──

    def get_meta(self, key: str) -> Optional[str]:
        """Read state_meta[key] on self._lock (not _read_ctx): fts_rebuild_step reads progress before its
        write transaction and a WAL reader would not see it."""
        with self._lock:
            row = self._conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str, *, cursor: Optional[sqlite3.Cursor] = None) -> None:
        """Upsert state_meta[key]; with ``cursor`` the write is inline (the caller already holds a
        transaction — nesting BEGIN IMMEDIATE would deadlock)."""
        sql = (
            "INSERT INTO state_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        if cursor is not None:
            cursor.execute(sql, (key, value))
        else:
            self._write_sql(sql, (key, value))

    def compare_and_set_meta(
        self, key: str, expected: Optional[str], replacement: str
    ) -> bool:
        """Atomically replace one meta value iff its exact value is unchanged."""
        return self.compare_and_set_meta_many([(key, expected, replacement)])

    def compare_and_set_meta_many(
        self, changes: List[Tuple[str, Optional[str], str]]
    ) -> bool:
        """Compare and replace meta rows in one authoritative transaction.

        All comparisons run after ``BEGIN IMMEDIATE`` owns SQLite's writer
        lock. No row changes unless every exact expected value still matches.
        """
        if not changes:
            return True

        def _do(conn):
            for key, expected, _replacement in changes:
                row = conn.execute(
                    "SELECT value FROM state_meta WHERE key = ?", (key,)
                ).fetchone()
                current = None if row is None else row[0]
                if current != expected:
                    return False
            for key, _expected, replacement in changes:
                conn.execute(
                    "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, replacement),
                )
            return True

        return bool(self._execute_write(_do))

    def retag_kanban_worker_sessions(self, workspaces_root: str) -> int:
        """Retag legacy kanban worker rows from ``cli`` to ``kanban`` by cwd under the board's workspaces
        root; gated once per root via state_meta. Returns rows retagged."""
        prefix = str(workspaces_root).rstrip("/\\")
        if not prefix:
            return 0
        gate = f"kanban_worker_source_retagged:{prefix}"
        if self.get_meta(gate) == "1":
            return 0
        def _do(conn):
            cursor = conn.execute(
                "UPDATE sessions SET source = 'kanban' "
                "WHERE source = 'cli' AND (cwd = ? OR cwd LIKE ? ESCAPE '\\')",
                (prefix, _escape_like(prefix) + "/%"),
            )
            # rowcount BEFORE set_meta reuses this cursor for its INSERT.
            retagged = cursor.rowcount or 0
            self.set_meta(gate, "1", cursor=cursor)
            return retagged
        return self._execute_write(_do)

    def list_meta_prefix(self, prefix: str) -> List[Tuple[str, str]]:
        """``[(key, value), ...]`` for state_meta keys starting with the literal
        ``prefix`` (LIKE wildcards escaped) — e.g. ``loop:<session_id>`` rows."""
        if not prefix:
            return []
        rows = self._read_all(
            "SELECT key, value FROM state_meta WHERE key LIKE ? ESCAPE '\\'", (_escape_like(prefix) + "%",),
        )
        return [(row[0], row[1]) for row in rows]


    _TODO_STATE_MAX_MATERIALIZED_BYTES = 1_000_000
    _TODO_MIGRATION_PAYLOAD_BYTES = 65_536
    _TODO_MIGRATION_SMALL_FIELD_BYTES = 4_096
    _TODO_MIGRATION_ROWS_PER_SLICE = 64
    _TODO_MIGRATION_MAX_SLICES_PER_READ = 9
    _TODO_MIGRATION_MAX_PENDING_RESULTS = 16
    _TODO_MIGRATION_MAX_DECODED_BYTES_PER_READ = 2_000_000
    _TODO_MIGRATION_MAX_PROGRESS_BYTES = 1_100_000
    _TODO_MIGRATION_MAX_SECONDS = 0.05
    # These are per-field read ceilings, not a global 64 KiB BLOB ceiling.
    # Legacy source payloads stay at 64 KiB; already-materialized authority and
    # durable migration-progress representations are larger but independently
    # capped. The decoded-byte budget below bounds their aggregate per attempt.
    _TODO_BLOB_READ_CAPS = {
        ("messages", "role"): 16,
        ("messages", "content"): _TODO_MIGRATION_PAYLOAD_BYTES,
        ("messages", "tool_call_id"): _TODO_MIGRATION_SMALL_FIELD_BYTES,
        ("messages", "tool_calls"): _TODO_MIGRATION_PAYLOAD_BYTES,
        ("messages", "tool_name"): _TODO_MIGRATION_SMALL_FIELD_BYTES,
        ("messages", "display_kind"): _TODO_MIGRATION_SMALL_FIELD_BYTES,
        ("messages", "display_metadata"): _TODO_MIGRATION_PAYLOAD_BYTES,
        ("messages", "todo_authority_json"): _TODO_STATE_MAX_MATERIALIZED_BYTES,
        ("todo_authorities", "authority_json"): _TODO_STATE_MAX_MATERIALIZED_BYTES,
        ("todo_pair_boundaries", "boundary_json"): _TODO_STATE_MAX_MATERIALIZED_BYTES,
        (
            "todo_authority_migrations",
            "pending_results_json",
        ): _TODO_MIGRATION_MAX_PROGRESS_BYTES,
        (
            "todo_authority_migrations",
            "deferred_pending_results_json",
        ): _TODO_MIGRATION_MAX_PROGRESS_BYTES,
        ("state_meta", "value"): 32,
    }
    # Same-host CPython tracemalloc regression ceiling for the adversarial
    # one-million-byte authority read+UTF-8 decode+JSON parse path. This is a
    # tested allocation ceiling, not a portable hard RSS limit.
    _TODO_NEAR_LIMIT_TRACEMALLOC_CEILING_BYTES = 6_000_000
    _TODO_STATE_LOOKUP_SQL = """
        SELECT message_id
        FROM todo_authorities INDEXED BY idx_todo_authorities_latest
        WHERE session_id = ? AND (active = 1 OR compacted = 1)
        ORDER BY message_id DESC
        LIMIT 1
    """
    _TODO_BLOB_COLUMNS = frozenset(_TODO_BLOB_READ_CAPS)

    @classmethod
    def _todo_blob_resource_contract_for_tests(cls) -> Dict[str, Any]:
        """Expose the executable Todo read/allocation contract to regressions."""
        if set(cls._TODO_BLOB_READ_CAPS) != set(cls._TODO_BLOB_COLUMNS):
            raise AssertionError("Todo BLOB source inventory is incomplete")
        return {
            "field_read_caps_bytes": {
                f"{table}.{column}": cap
                for (table, column), cap in sorted(cls._TODO_BLOB_READ_CAPS.items())
            },
            "single_result_source_bytes": cls._TODO_MIGRATION_PAYLOAD_BYTES,
            "materialized_authority_bytes": cls._TODO_STATE_MAX_MATERIALIZED_BYTES,
            "pending_result_count": cls._TODO_MIGRATION_MAX_PENDING_RESULTS,
            "pending_results_json_bytes": cls._TODO_MIGRATION_MAX_PROGRESS_BYTES,
            "attempt_decoded_bytes": cls._TODO_MIGRATION_MAX_DECODED_BYTES_PER_READ,
            "migration_rows_per_slice": cls._TODO_MIGRATION_ROWS_PER_SLICE,
            "migration_slices_per_attempt": cls._TODO_MIGRATION_MAX_SLICES_PER_READ,
            "cooperative_deadline_seconds": cls._TODO_MIGRATION_MAX_SECONDS,
            "deadline_kind": "cooperative",
            "hard_deadline_seconds": None,
            "deadline_checkpoints": [
                "before_each_slice_after_first",
                "before_each_row_after_first_progress",
            ],
            "non_preemptible_operations": [
                "sqlite_statement",
                "bounded_blob_read",
                "utf8_decode",
                "json_decode",
                "json_encode",
                "sqlite_write",
            ],
            "near_limit_tracemalloc_ceiling_bytes": cls._TODO_NEAR_LIMIT_TRACEMALLOC_CEILING_BYTES,
        }

    @classmethod
    def _read_bounded_blob_text(
        cls,
        conn,
        *,
        table: str,
        column: str,
        row_id: int,
        max_bytes: int,
        budget_bytes: Optional[int] = None,
    ) -> Tuple[str, Optional[str], int]:
        """Read TEXT incrementally without materializing a value before its cap."""
        declared_cap = cls._TODO_BLOB_READ_CAPS.get((table, column))
        if declared_cap is None:
            raise ValueError(f"Unsupported bounded BLOB source: {table}.{column}")
        if max_bytes > declared_cap:
            raise ValueError(
                f"Todo BLOB read cap {max_bytes} exceeds declared "
                f"{table}.{column} cap {declared_cap}"
            )
        try:
            blob = conn.blobopen(table, column, int(row_id), readonly=True)
        except sqlite3.Error:
            return "missing", None, 0
        with blob:
            size = len(blob)
            if size > max_bytes:
                return "oversized", None, size
            if budget_bytes is not None and size > max(0, budget_bytes):
                return "budget", None, size
            raw = blob.read(size)
        try:
            return "ok", raw.decode("utf-8"), size
        except UnicodeDecodeError:
            return "invalid", None, size

    def _todo_migration_stats_for_tests(self) -> Dict[str, Any]:
        """Return the most recent bounded migration-attempt counters."""
        return dict(
            getattr(
                self,
                "_todo_last_migration_stats",
                {
                    "rows": 0,
                    "processed_rows": 0,
                    "selects": 0,
                    "decoded_bytes": 0,
                    "blob_reads": 0,
                    "max_blob_read_bytes": 0,
                    "max_source_blob_bytes": 0,
                    "pending_results": 0,
                    "elapsed": 0.0,
                },
            )
        )

    def _validate_todo_authority_text(
        self,
        raw: str,
        session_id: str,
    ) -> Optional[List[Dict[str, Any]]]:
        """Validate a complete materialized authority before publication."""
        try:
            messages = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(messages, list) or not 1 <= len(messages) <= 2:
            return None
        if any(not isinstance(message, dict) for message in messages):
            return None
        if any(message.get("session_id") != session_id for message in messages):
            return None

        if len(messages) == 1:
            message = messages[0]
            if message.get("role") != "user":
                return None
            metadata = message.get("display_metadata")
            snapshot = metadata.get("todo_snapshot") if isinstance(metadata, dict) else None
            structured = (
                isinstance(snapshot, dict)
                and set(snapshot) == {"todos"}
                and _parse_todo_value(snapshot.get("todos")) is not None
            )
            legacy = (
                message.get("display_kind") is None
                and metadata is None
                and _is_valid_legacy_todo_carrier(message.get("content"))
            )
            return messages if structured or legacy else None

        assistant, result = messages
        if assistant.get("role") != "assistant" or result.get("role") != "tool":
            return None
        call_id = str(result.get("tool_call_id") or "").strip()
        calls = assistant.get("tool_calls")
        if not call_id or not isinstance(calls, list) or len(calls) != 1:
            return None
        call = calls[0]
        if not isinstance(call, dict):
            return None
        function = call.get("function")
        function = function if isinstance(function, dict) else call
        name = str(function.get("name") or call.get("name") or "").strip()
        candidate_id = str(call.get("id") or call.get("tool_call_id") or "").strip()
        if name != "todo" or candidate_id != call_id:
            return None
        if _parse_todo_value(result.get("content")) is None:
            return None
        return messages

    def _load_todo_authority(
        self,
        conn,
        session_id: str,
    ) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
        row = conn.execute(self._TODO_STATE_LOOKUP_SQL, (session_id,)).fetchone()
        if row is None:
            return "absent", None
        status, raw, _size = self._read_bounded_blob_text(
            conn,
            table="todo_authorities",
            column="authority_json",
            row_id=int(row["message_id"]),
            max_bytes=self._TODO_STATE_MAX_MATERIALIZED_BYTES,
        )
        if status != "ok" or raw is None:
            return "invalid", None
        messages = self._validate_todo_authority_text(raw, session_id)
        return ("found", messages) if messages is not None else ("invalid", None)

    def _store_todo_authority(
        self,
        conn,
        *,
        message_id: int,
        session_id: str,
        authority_json: str,
    ) -> None:
        """Publish one bounded authority into the sparse lookup table."""
        conn.execute(
            "UPDATE messages SET todo_authority_json = ? WHERE id = ?",
            (authority_json, message_id),
        )
        conn.execute(
            """INSERT INTO todo_authorities
                   (message_id, session_id, authority_json, active, compacted)
               SELECT id, session_id, ?, active, compacted
                 FROM messages WHERE id = ? AND session_id = ?
               ON CONFLICT(message_id) DO UPDATE SET
                   session_id = excluded.session_id,
                   authority_json = excluded.authority_json,
                   active = excluded.active,
                   compacted = excluded.compacted""",
            (authority_json, message_id, session_id),
        )

    def _record_todo_pair_boundary(
        self,
        conn,
        *,
        message_id: int,
        session_id: str,
        role: str,
        content: Any,
        tool_calls: Any,
        tool_name: Optional[str],
        display_kind: Optional[str],
        display_metadata: Any,
        timestamp: float,
    ) -> None:
        """Record the latest non-tool boundary for constant-work result pairing."""
        if role not in {"assistant", "user", "system"}:
            return

        boundary_json = None
        if role == "assistant" and isinstance(tool_calls, list):
            todo_calls: List[Dict[str, Any]] = []
            overflow = False
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                function = function if isinstance(function, dict) else call
                name = str(function.get("name") or call.get("name") or "").strip()
                call_id = str(call.get("id") or call.get("tool_call_id") or "").strip()
                if name != "todo" or not call_id:
                    continue
                todo_calls.append(call)
                if len(todo_calls) > self._TODO_MIGRATION_MAX_PENDING_RESULTS:
                    overflow = True
                    break
            if todo_calls and not overflow:
                projected = {
                    "id": message_id,
                    "session_id": session_id,
                    "role": role,
                    "content": "",
                    "tool_calls": todo_calls,
                    "tool_name": tool_name,
                    "display_kind": display_kind,
                    "display_metadata": display_metadata if isinstance(display_metadata, dict) else None,
                    "timestamp": timestamp,
                }
                encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
                if len(encoded.encode("utf-8")) <= self._TODO_STATE_MAX_MATERIALIZED_BYTES:
                    boundary_json = encoded

        conn.execute(
            "UPDATE messages SET todo_pair_boundary_json = ? WHERE id = ?",
            (boundary_json, message_id),
        )
        conn.execute(
            """INSERT INTO todo_pair_boundaries
                   (session_id, message_id, role, boundary_json)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   message_id = excluded.message_id,
                   role = excluded.role,
                   boundary_json = excluded.boundary_json
               WHERE excluded.message_id >= todo_pair_boundaries.message_id""",
            (session_id, message_id, role, boundary_json),
        )

    def _todo_carrier_authority_json(
        self,
        *,
        session_id: str,
        row_id: int,
        role: str,
        content: Any,
        tool_call_id: Optional[str],
        tool_name: Optional[str],
        display_kind: Optional[str],
        display_metadata: Any,
        timestamp: float,
    ) -> Optional[str]:
        """Materialize one trusted Todo carrier at the persistence boundary."""
        metadata = display_metadata if isinstance(display_metadata, dict) else None
        snapshot = metadata.get("todo_snapshot") if metadata is not None else None
        structured = (
            role == "user"
            and isinstance(snapshot, dict)
            and set(snapshot) == {"todos"}
            and _parse_todo_value(snapshot.get("todos")) is not None
        )
        legacy = (
            role == "user"
            and display_kind is None
            and display_metadata is None
            and _is_valid_legacy_todo_carrier(content)
        )
        if not structured and not legacy:
            return None

        projected = {
            "id": row_id,
            "session_id": session_id,
            "role": role,
            "content": "" if structured else content,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "display_kind": display_kind,
            "display_metadata": metadata,
            "timestamp": timestamp,
        }
        encoded = json.dumps([projected], ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > self._TODO_STATE_MAX_MATERIALIZED_BYTES:
            return None
        return encoded

    def _todo_tool_result_authority_json(
        self,
        conn,
        *,
        session_id: str,
        row_id: int,
        role: str,
        content: Any,
        tool_call_id: Optional[str],
        tool_name: Optional[str],
        display_kind: Optional[str],
        display_metadata: Any,
        timestamp: float,
    ) -> Optional[str]:
        """Materialize a Todo result only when its nearest call boundary matches."""
        call_id = str(tool_call_id or "").strip()
        if role != "tool" or not call_id:
            return None

        boundary = conn.execute(
            """SELECT rowid, message_id, role
                 FROM todo_pair_boundaries
                WHERE session_id = ?""",
            (session_id,),
        ).fetchone()
        if (
            boundary is None
            or boundary["role"] != "assistant"
            or boundary["message_id"] >= row_id
        ):
            return None
        boundary_status, boundary_raw, _boundary_size = self._read_bounded_blob_text(
            conn,
            table="todo_pair_boundaries",
            column="boundary_json",
            row_id=int(boundary["rowid"]),
            max_bytes=self._TODO_STATE_MAX_MATERIALIZED_BYTES,
        )
        if boundary_status != "ok" or boundary_raw is None:
            return None
        try:
            boundary_message = json.loads(boundary_raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(boundary_message, dict):
            return None
        calls = boundary_message.get("tool_calls")
        if not isinstance(calls, list):
            return None

        matching_call = None
        remaining_calls = []
        for call in calls:
            if isinstance(call, dict):
                function = call.get("function")
                function = function if isinstance(function, dict) else call
                name = str(function.get("name") or call.get("name") or "").strip()
                candidate_id = str(
                    call.get("id") or call.get("tool_call_id") or ""
                ).strip()
                if name == "todo" and candidate_id == call_id:
                    # A call ID is one-shot. Keep the last duplicate declaration,
                    # matching the renderer's Map semantics, but consume every
                    # declaration before validating the result payload.
                    matching_call = call
                    continue
            remaining_calls.append(call)
        if matching_call is None:
            return None

        boundary_message["tool_calls"] = remaining_calls
        consumed_boundary_json = json.dumps(
            boundary_message,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        conn.execute(
            "UPDATE messages SET todo_pair_boundary_json = ? "
            "WHERE id = ? AND session_id = ?",
            (consumed_boundary_json, int(boundary["message_id"]), session_id),
        )
        conn.execute(
            "UPDATE todo_pair_boundaries SET boundary_json = ? "
            "WHERE rowid = ? AND session_id = ? AND message_id = ?",
            (
                consumed_boundary_json,
                int(boundary["rowid"]),
                session_id,
                int(boundary["message_id"]),
            ),
        )

        if isinstance(content, str):
            if len(content) > self._TODO_STATE_MAX_MATERIALIZED_BYTES:
                return None
            if len(content.encode("utf-8")) > self._TODO_STATE_MAX_MATERIALIZED_BYTES:
                return None
        if _parse_todo_value(content) is None:
            return None

        result_metadata = display_metadata if isinstance(display_metadata, dict) else None
        evidence = [
            {
                "id": boundary_message.get("id"),
                "session_id": session_id,
                "role": "assistant",
                "content": "",
                "tool_call_id": None,
                "tool_name": boundary_message.get("tool_name"),
                "display_kind": boundary_message.get("display_kind"),
                "display_metadata": boundary_message.get("display_metadata"),
                "timestamp": boundary_message.get("timestamp"),
                "tool_calls": [matching_call],
            },
            {
                "id": row_id,
                "session_id": session_id,
                "role": "tool",
                "content": content,
                "tool_call_id": call_id,
                "tool_name": tool_name,
                "display_kind": display_kind,
                "display_metadata": result_metadata,
                "timestamp": timestamp,
            },
        ]
        encoded = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > self._TODO_STATE_MAX_MATERIALIZED_BYTES:
            return None
        return encoded

    def _advance_todo_authority_migration(self, conn, session_id: str) -> str:
        """Advance one bounded legacy scan; return found, complete, or pending."""
        started = time.perf_counter()
        stats: Dict[str, Any] = {
            "rows": 0,
            "processed_rows": 0,
            "selects": 0,
            "decoded_bytes": 0,
            "blob_reads": 0,
            "max_blob_read_bytes": 0,
            "max_source_blob_bytes": 0,
            "pending_results": 0,
            "elapsed": 0.0,
        }
        self._todo_last_migration_stats = stats
        pending_results: List[Dict[str, Any]] = []

        def finish(status: str) -> str:
            stats["pending_results"] = len(pending_results)
            stats["elapsed"] = time.perf_counter() - started
            return status

        stats["selects"] += 1
        if conn.execute(
            "SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,)
        ).fetchone() is None:
            stats["progress_action"] = "session_absent"
            return finish("absent")

        def read_accounted(
            *, table: str, column: str, row_id: int, max_bytes: int
        ) -> Tuple[str, Optional[str]]:
            remaining = (
                self._TODO_MIGRATION_MAX_DECODED_BYTES_PER_READ
                - int(stats["decoded_bytes"])
            )
            status, value, size = self._read_bounded_blob_text(
                conn,
                table=table,
                column=column,
                row_id=row_id,
                max_bytes=max_bytes,
                budget_bytes=remaining,
            )
            stats["blob_reads"] += 1
            stats["max_source_blob_bytes"] = max(
                int(stats["max_source_blob_bytes"]),
                size,
            )
            if status in {"ok", "invalid"}:
                stats["decoded_bytes"] += size
                stats["max_blob_read_bytes"] = max(
                    int(stats["max_blob_read_bytes"]),
                    size,
                )
            return status, value

        stats["selects"] += 1
        high_water_row = conn.execute(
            "SELECT rowid FROM state_meta "
            "WHERE key = 'todo_authority_legacy_high_water'"
        ).fetchone()
        high_water = 0
        if high_water_row is not None:
            status, high_water_text = read_accounted(
                table="state_meta",
                column="value",
                row_id=int(high_water_row["rowid"]),
                max_bytes=32,
            )
            if status == "ok" and high_water_text is not None:
                try:
                    high_water = max(0, int(high_water_text))
                except ValueError:
                    high_water = 0

        stats["selects"] += 1
        progress = conn.execute(
            """SELECT rowid, before_message_id, complete, phase,
                      authority_checked_message_id
                 FROM todo_authority_migrations WHERE session_id = ?""",
            (session_id,),
        ).fetchone()
        migration_phase = (
            str(progress["phase"] or "scan") if progress is not None else "scan"
        )
        if migration_phase not in {"scan", "authority_probe", "authority_checked"}:
            migration_phase = "scan"
        authority_checked_message_id = (
            int(progress["authority_checked_message_id"])
            if progress is not None
            and progress["authority_checked_message_id"] is not None
            else None
        )
        stats["phase"] = migration_phase
        if progress is not None and progress["complete"]:
            return finish("complete")

        before_message_id = (
            int(progress["before_message_id"])
            if progress is not None and progress["before_message_id"] is not None
            else high_water + 1
        )
        if progress is not None:
            status, pending_text = read_accounted(
                table="todo_authority_migrations",
                column="pending_results_json",
                row_id=int(progress["rowid"]),
                max_bytes=self._TODO_MIGRATION_MAX_PROGRESS_BYTES,
            )
            if status == "ok" and pending_text:
                try:
                    decoded_pending = json.loads(pending_text)
                except (json.JSONDecodeError, TypeError):
                    decoded_pending = None
                if isinstance(decoded_pending, list):
                    pending_results = [
                        value
                        for value in decoded_pending
                        if isinstance(value, dict)
                    ][: self._TODO_MIGRATION_MAX_PENDING_RESULTS]

        deadline = started + self._TODO_MIGRATION_MAX_SECONDS
        complete = high_water <= 0
        slices = 0
        stop_for_budget = False

        def read_field(
            row: sqlite3.Row,
            column: str,
            max_bytes: int,
        ) -> Tuple[str, Optional[str]]:
            value_type = row[f"{column}_type"]
            if value_type == "null":
                return "null", None
            if value_type not in {"text", "blob"}:
                return "invalid", None
            return read_accounted(
                table="messages",
                column=column,
                row_id=int(row["id"]),
                max_bytes=max_bytes,
            )

        def read_optional_fields(
            row: sqlite3.Row,
            columns: Tuple[str, ...],
        ) -> Tuple[str, Dict[str, Optional[str]]]:
            values: Dict[str, Optional[str]] = {}
            for column in columns:
                status, value = read_field(
                    row,
                    column,
                    self._TODO_MIGRATION_SMALL_FIELD_BYTES,
                )
                if status == "budget":
                    return status, {}
                if status not in {"ok", "null"}:
                    return "invalid", {}
                values[column] = value
            return "ok", values

        def consume(row_id: int) -> None:
            nonlocal before_message_id, migration_phase, authority_checked_message_id
            before_message_id = row_id
            stats["processed_rows"] += 1
            if authority_checked_message_id == row_id:
                migration_phase = "scan"
                authority_checked_message_id = None

        def remember_pending_result(candidate: Dict[str, Any]) -> None:
            """Keep the earliest bounded row for one call while scanning backward."""
            nonlocal pending_results
            call_id = str(candidate.get("tool_call_id") or "").strip()
            if not call_id:
                return
            replacement_index = next(
                (
                    index
                    for index, pending in enumerate(pending_results)
                    if str(pending.get("tool_call_id") or "").strip() == call_id
                ),
                None,
            )
            if (
                replacement_index is None
                and len(pending_results) >= self._TODO_MIGRATION_MAX_PENDING_RESULTS
            ):
                return

            tombstone = {"id": candidate.get("id"), "tool_call_id": call_id}
            variants = [candidate]
            if candidate != tombstone:
                variants.append(tombstone)
            for variant in variants:
                updated = list(pending_results)
                if replacement_index is None:
                    updated.append(variant)
                else:
                    updated[replacement_index] = variant
                encoded = json.dumps(
                    updated,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if len(encoded.encode("utf-8")) <= self._TODO_MIGRATION_MAX_PROGRESS_BYTES:
                    pending_results = updated
                    return

        while not complete and slices < self._TODO_MIGRATION_MAX_SLICES_PER_READ:
            if slices and time.perf_counter() >= deadline:
                break
            stats["selects"] += 1
            rows = conn.execute(
                """SELECT id,
                          CASE WHEN typeof(timestamp) IN ('integer', 'real')
                               THEN timestamp END AS timestamp,
                          typeof(role) AS role_type,
                          typeof(content) AS content_type,
                          typeof(tool_call_id) AS tool_call_id_type,
                          typeof(tool_calls) AS tool_calls_type,
                          typeof(tool_name) AS tool_name_type,
                          typeof(display_kind) AS display_kind_type,
                          typeof(display_metadata) AS display_metadata_type,
                          typeof(todo_authority_json) AS todo_authority_json_type
                     FROM messages INDEXED BY idx_messages_session_id
                    WHERE session_id = ? AND id < ? AND id <= ?
                      AND (active = 1 OR compacted = 1)
                    ORDER BY id DESC LIMIT ?""",
                (
                    session_id,
                    before_message_id,
                    high_water,
                    self._TODO_MIGRATION_ROWS_PER_SLICE,
                ),
            ).fetchall()
            slices += 1
            stats["rows"] += len(rows)
            if not rows:
                if migration_phase == "authority_probe" and progress is not None:
                    conn.execute(
                        """UPDATE todo_authority_migrations
                              SET pending_results_json = deferred_pending_results_json,
                                  deferred_pending_results_json = '[]',
                                  phase = 'scan', authority_checked_message_id = NULL
                            WHERE session_id = ?""",
                        (session_id,),
                    )
                    stats["progress_action"] = "restored_after_missing_probe_row"
                    return finish("pending")
                complete = True
                break

            for row in rows:
                if stats["processed_rows"] and time.perf_counter() >= deadline:
                    stop_for_budget = True
                    break
                if stats["decoded_bytes"] >= self._TODO_MIGRATION_MAX_DECODED_BYTES_PER_READ:
                    stop_for_budget = True
                    break

                row_id = int(row["id"])
                if (
                    migration_phase == "authority_probe"
                    and authority_checked_message_id != row_id
                ):
                    conn.execute(
                        """UPDATE todo_authority_migrations
                              SET pending_results_json = deferred_pending_results_json,
                                  deferred_pending_results_json = '[]',
                                  phase = 'scan', authority_checked_message_id = NULL
                            WHERE session_id = ?""",
                        (session_id,),
                    )
                    stats["progress_action"] = "restored_after_probe_row_changed"
                    return finish("pending")
                if (
                    migration_phase == "authority_checked"
                    and authority_checked_message_id != row_id
                ):
                    migration_phase = "scan"
                    authority_checked_message_id = None

                skip_authority = (
                    migration_phase == "authority_checked"
                    and authority_checked_message_id == row_id
                )
                authority_status = "skipped"
                authority_raw = None
                if not skip_authority:
                    authority_status, authority_raw = read_field(
                        row,
                        "todo_authority_json",
                        self._TODO_STATE_MAX_MATERIALIZED_BYTES,
                    )
                    if authority_status == "budget":
                        if pending_results and progress is not None:
                            conn.execute(
                                """UPDATE todo_authority_migrations
                                      SET deferred_pending_results_json = pending_results_json,
                                          pending_results_json = '[]',
                                          phase = 'authority_probe',
                                          authority_checked_message_id = ?
                                    WHERE session_id = ?""",
                                (row_id, session_id),
                            )
                            stats["progress_action"] = "deferred_pending_for_authority_probe"
                            return finish("pending")
                        stop_for_budget = True
                        break
                    if authority_status == "ok" and authority_raw is not None:
                        authority = self._validate_todo_authority_text(
                            authority_raw,
                            session_id,
                        )
                        if authority is not None:
                            self._store_todo_authority(
                                conn,
                                message_id=row_id,
                                session_id=session_id,
                                authority_json=authority_raw,
                            )
                            consume(row_id)
                            complete = True
                            migration_phase = "scan"
                            authority_checked_message_id = None
                            break
                    if migration_phase == "authority_probe" and progress is not None:
                        conn.execute(
                            """UPDATE todo_authority_migrations
                                  SET pending_results_json = deferred_pending_results_json,
                                      deferred_pending_results_json = '[]',
                                      phase = 'authority_checked',
                                      authority_checked_message_id = ?
                                WHERE session_id = ?""",
                            (row_id, session_id),
                        )
                        stats["progress_action"] = "authority_probe_completed"
                        return finish("pending")
                    if authority_status in {"ok", "invalid"}:
                        migration_phase = "authority_checked"
                        authority_checked_message_id = row_id
                        stats["progress_action"] = "authority_read_checkpointed"
                else:
                    migration_phase = "scan"
                    authority_checked_message_id = None

                role_status, role = read_field(row, "role", 16)
                if role_status == "budget":
                    stop_for_budget = True
                    break
                if role_status != "ok" or role not in {"assistant", "user", "system", "tool"}:
                    consume(row_id)
                    continue

                if role in {"assistant", "user", "system"}:
                    if role == "assistant" and pending_results:
                        calls_status, tool_calls_raw = read_field(
                            row,
                            "tool_calls",
                            self._TODO_MIGRATION_PAYLOAD_BYTES,
                        )
                        if calls_status == "budget":
                            stop_for_budget = True
                            break
                        calls = None
                        if calls_status == "ok" and tool_calls_raw:
                            try:
                                calls = json.loads(tool_calls_raw)
                            except (json.JSONDecodeError, TypeError):
                                calls = None
                        if isinstance(calls, list):
                            todo_calls_by_id: Dict[str, Dict[str, Any]] = {}
                            for call in calls:
                                if not isinstance(call, dict):
                                    continue
                                function = call.get("function")
                                function = function if isinstance(function, dict) else call
                                name = str(
                                    function.get("name") or call.get("name") or ""
                                ).strip()
                                candidate_id = str(
                                    call.get("id") or call.get("tool_call_id") or ""
                                ).strip()
                                if name == "todo" and candidate_id:
                                    # The renderer's Map keeps the last declaration
                                    # for a duplicate ID and consumes that ID once.
                                    todo_calls_by_id[candidate_id] = call

                            earliest_by_call_id: Dict[
                                str, Tuple[int, Dict[str, Any]]
                            ] = {}
                            for result in pending_results:
                                call_id = str(
                                    result.get("tool_call_id") or ""
                                ).strip()
                                if call_id not in todo_calls_by_id:
                                    continue
                                try:
                                    result_id = int(result.get("id"))
                                except (TypeError, ValueError):
                                    continue
                                current = earliest_by_call_id.get(call_id)
                                if current is None or result_id < current[0]:
                                    earliest_by_call_id[call_id] = (result_id, result)

                            matching_result = None
                            matching_call = None
                            matching_result_id = -1
                            for call_id, (result_id, result) in earliest_by_call_id.items():
                                # The first row consumes this call even when its
                                # payload is malformed. Only a valid first result
                                # may become authority; later duplicates are ignored.
                                if _parse_todo_value(result.get("content")) is None:
                                    continue
                                if result_id > matching_result_id:
                                    matching_result = result
                                    matching_call = todo_calls_by_id[call_id]
                                    matching_result_id = result_id
                            if matching_result is not None and matching_call is not None:
                                fields_status, fields = read_optional_fields(
                                    row,
                                    ("tool_name", "display_kind"),
                                )
                                metadata_status, metadata_raw = read_field(
                                    row,
                                    "display_metadata",
                                    self._TODO_MIGRATION_PAYLOAD_BYTES,
                                )
                                if fields_status == "budget" or metadata_status == "budget":
                                    stop_for_budget = True
                                    break
                                assistant_metadata = None
                                metadata_valid = metadata_status == "null"
                                if metadata_status == "ok" and metadata_raw is not None:
                                    try:
                                        decoded_metadata = json.loads(metadata_raw)
                                    except (json.JSONDecodeError, TypeError):
                                        decoded_metadata = None
                                    if isinstance(decoded_metadata, dict):
                                        assistant_metadata = decoded_metadata
                                        metadata_valid = True
                                if fields_status == "ok" and metadata_valid:
                                    evidence = [
                                        {
                                            "id": row_id,
                                            "session_id": session_id,
                                            "role": "assistant",
                                            "content": "",
                                            "tool_call_id": None,
                                            "tool_name": fields["tool_name"],
                                            "display_kind": fields["display_kind"],
                                            "display_metadata": assistant_metadata,
                                            "timestamp": row["timestamp"],
                                            "tool_calls": [matching_call],
                                        },
                                        matching_result,
                                    ]
                                    encoded = json.dumps(
                                        evidence,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    )
                                    if (
                                        len(encoded.encode("utf-8"))
                                        <= self._TODO_STATE_MAX_MATERIALIZED_BYTES
                                        and self._validate_todo_authority_text(
                                            encoded,
                                            session_id,
                                        )
                                        is not None
                                    ):
                                        self._store_todo_authority(
                                            conn,
                                            message_id=int(matching_result["id"]),
                                            session_id=session_id,
                                            authority_json=encoded,
                                        )
                                        consume(row_id)
                                        complete = True
                                        break
                    if stop_for_budget or complete:
                        break
                    pending_results = []
                    if role == "user":
                        metadata_status, metadata_raw = read_field(
                            row,
                            "display_metadata",
                            self._TODO_MIGRATION_PAYLOAD_BYTES,
                        )
                        if metadata_status == "budget":
                            stop_for_budget = True
                            break
                        metadata = None
                        metadata_valid = metadata_status == "null"
                        if metadata_status == "ok" and metadata_raw is not None:
                            metadata = self._decode_display_metadata(metadata_raw)
                            metadata_valid = isinstance(metadata, dict)

                        kind_status, display_kind = read_field(
                            row,
                            "display_kind",
                            self._TODO_MIGRATION_SMALL_FIELD_BYTES,
                        )
                        if kind_status == "budget":
                            stop_for_budget = True
                            break
                        kind_valid = kind_status in {"ok", "null"}
                        snapshot = (
                            metadata.get("todo_snapshot")
                            if isinstance(metadata, dict)
                            else None
                        )
                        structured = (
                            isinstance(snapshot, dict)
                            and set(snapshot) == {"todos"}
                            and _parse_todo_value(snapshot.get("todos")) is not None
                        )

                        content = "" if structured else None
                        content_status = "ok"
                        if not structured and metadata_status == "null" and kind_status == "null":
                            content_status, content_raw = read_field(
                                row,
                                "content",
                                self._TODO_MIGRATION_PAYLOAD_BYTES,
                            )
                            if content_status == "budget":
                                stop_for_budget = True
                                break
                            if content_status == "ok" and content_raw is not None:
                                content = self._decode_content(content_raw)

                        candidate_shape = structured or (
                            metadata_status == "null"
                            and kind_status == "null"
                            and content_status == "ok"
                            and _is_valid_legacy_todo_carrier(content)
                        )
                        if metadata_valid and kind_valid and candidate_shape:
                            fields_status, fields = read_optional_fields(
                                row,
                                ("tool_call_id", "tool_name"),
                            )
                            if fields_status == "budget":
                                stop_for_budget = True
                                break
                            if fields_status == "ok":
                                authority_json = self._todo_carrier_authority_json(
                                    session_id=session_id,
                                    row_id=row_id,
                                    role=role,
                                    content=content,
                                    tool_call_id=fields["tool_call_id"],
                                    tool_name=fields["tool_name"],
                                    display_kind=display_kind,
                                    display_metadata=metadata,
                                    timestamp=row["timestamp"],
                                )
                                if authority_json is not None:
                                    self._store_todo_authority(
                                        conn,
                                        message_id=row_id,
                                        session_id=session_id,
                                        authority_json=authority_json,
                                    )
                                    consume(row_id)
                                    complete = True
                                    break
                    if stop_for_budget or complete:
                        break
                    consume(row_id)
                elif role == "tool":
                    call_id_status, call_id_raw = read_field(
                        row,
                        "tool_call_id",
                        self._TODO_MIGRATION_SMALL_FIELD_BYTES,
                    )
                    fields_status, fields = read_optional_fields(
                        row,
                        ("tool_name", "display_kind"),
                    )
                    content_status, content_raw = read_field(
                        row,
                        "content",
                        self._TODO_MIGRATION_PAYLOAD_BYTES,
                    )
                    metadata_status, metadata_raw = read_field(
                        row,
                        "display_metadata",
                        self._TODO_MIGRATION_PAYLOAD_BYTES,
                    )
                    if (
                        call_id_status == "budget"
                        or fields_status == "budget"
                        or content_status == "budget"
                        or metadata_status == "budget"
                    ):
                        stop_for_budget = True
                        break
                    content = (
                        self._decode_content(content_raw)
                        if content_status == "ok" and content_raw is not None
                        else None
                    )
                    metadata = None
                    metadata_valid = metadata_status == "null"
                    if metadata_status == "ok" and metadata_raw is not None:
                        metadata = self._decode_display_metadata(metadata_raw)
                        metadata_valid = isinstance(metadata, dict)
                    call_id = (
                        str(call_id_raw or "").strip()
                        if call_id_status == "ok"
                        else ""
                    )
                    if call_id:
                        candidate: Dict[str, Any] = {
                            "id": row_id,
                            "tool_call_id": call_id,
                        }
                        if (
                            fields_status == "ok"
                            and content_status == "ok"
                            and metadata_valid
                            and _parse_todo_value(content) is not None
                        ):
                            candidate = {
                                "id": row_id,
                                "session_id": session_id,
                                "role": "tool",
                                "content": content,
                                "tool_call_id": call_id,
                                "tool_name": fields["tool_name"],
                                "display_kind": fields["display_kind"],
                                "display_metadata": metadata,
                                "timestamp": row["timestamp"],
                            }
                        # Backward scanning sees later duplicates first. Replacing
                        # by call ID retains the earliest row, including a compact
                        # tombstone when that consuming row is not a valid Todo.
                        remember_pending_result(candidate)
                    consume(row_id)

            if stop_for_budget:
                break
            if len(rows) < self._TODO_MIGRATION_ROWS_PER_SLICE:
                complete = True

        pending_json = json.dumps(
            pending_results,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(pending_json.encode("utf-8")) > self._TODO_MIGRATION_MAX_PROGRESS_BYTES:
            pending_results = []
            pending_json = "[]"
        persisted_phase = "scan" if complete else migration_phase
        persisted_checked_message_id = (
            None if complete else authority_checked_message_id
        )
        conn.execute(
            """INSERT INTO todo_authority_migrations
                   (session_id, before_message_id, pending_results_json,
                    deferred_pending_results_json, phase,
                    authority_checked_message_id, complete)
               VALUES (?, ?, ?, '[]', ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   before_message_id = excluded.before_message_id,
                   pending_results_json = excluded.pending_results_json,
                   deferred_pending_results_json = '[]',
                   phase = excluded.phase,
                   authority_checked_message_id = excluded.authority_checked_message_id,
                   complete = excluded.complete""",
            (
                session_id,
                before_message_id,
                pending_json,
                persisted_phase,
                persisted_checked_message_id,
                1 if complete else 0,
            ),
        )
        stats["selects"] += 1
        if conn.execute(self._TODO_STATE_LOOKUP_SQL, (session_id,)).fetchone() is not None:
            return finish("found")
        return finish("complete" if complete else "pending")

    def get_todo_state_messages(self, session_id: str) -> Optional[List[Dict[str, Any]]]:
        """Return at most one validated Todo evidence pair for a whole session.

        Materialized authorities are validated at the persistence boundary and
        read through one partial-index lookup. Rejected transcript rows are not
        visited, decoded, or retained by this method.
        """
        with self._read_ctx() as conn:
            state, messages = self._load_todo_authority(conn, session_id)
        if state == "found":
            return messages
        if state == "invalid":
            return []

        status = self._execute_write(
            lambda conn: self._advance_todo_authority_migration(conn, session_id),
            patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S,
        )
        if status == "pending":
            return None
        with self._read_ctx() as conn:
            state, messages = self._load_todo_authority(conn, session_id)
        return messages if state == "found" else []


class AsyncSessionDB:
    """Async door onto SessionDB: every call runs via asyncio.to_thread so a blocking SQLite call
    never freezes the event loop (no method returns a live cursor)."""

    def __init__(self, db: "SessionDB") -> None:
        self._db = db

    def __getattr__(self, name: str):
        attr = getattr(self._db, name)
        if not callable(attr):
            return attr
        async def _offloaded(*args, **kwargs):
            return await asyncio.to_thread(attr, *args, **kwargs)
        return _offloaded


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Set  # noqa: F401,E402
import contextlib  # noqa: F401,E402
import errno  # noqa: F401,E402
import struct  # noqa: F401,E402
import weakref  # noqa: F401,E402

MAX_SAFE_EXPORT_MESSAGES = 20_000

MAX_SAFE_RESUME_MESSAGES = 20_000


_PLUGIN_COMPAT_LAZY = {
    'AUTO_VACUUM_MIN_FREELIST_RATIO': ('hermes_state_common', 'AUTO_VACUUM_MIN_FREELIST_RATIO'),
    'ActivityProvenance': ('agent.session_activity', 'ActivityProvenance'),
    'CompressionSessionBusyError': ('hermes_state_errors', 'CompressionSessionBusyError'),
    'CompressionSessionClosedError': ('hermes_state_errors', 'CompressionSessionClosedError'),
    'DEFERRED_INDEX_SQL': ('hermes_state_common', 'DEFERRED_INDEX_SQL'),
    'FTS_CJK_STALE_KEY': ('hermes_state_common', 'FTS_CJK_STALE_KEY'),
    'FTS_CJK_TABLE_SQL': ('hermes_state_fts', 'FTS_CJK_TABLE_SQL'),
    'FTS_CJK_TRIGGER_SQL': ('hermes_state_fts', 'FTS_CJK_TRIGGER_SQL'),
    'FTS_REBUILD_DEFERRAL_KEY': ('hermes_state_common', 'FTS_REBUILD_DEFERRAL_KEY'),
    'FTS_SQL': ('hermes_state_common', 'FTS_SQL'),
    'FTS_STALE_KEY': ('hermes_state_common', 'FTS_STALE_KEY'),
    'FTS_STORAGE_VERSION': ('hermes_state_common', 'FTS_STORAGE_VERSION'),
    'FTS_TRIGRAM_SQL': ('hermes_state_common', 'FTS_TRIGRAM_SQL'),
    'LEGACY_FTS_SQL': ('hermes_state_common', 'LEGACY_FTS_SQL'),
    'LEGACY_FTS_TRIGRAM_SQL': ('hermes_state_common', 'LEGACY_FTS_TRIGRAM_SQL'),
    'MAX_FTS5_QUERY_CHARS': ('hermes_state_common', 'MAX_FTS5_QUERY_CHARS'),
    'PERSISTENCE_ERROR_CAUSES': ('hermes_state_errors', 'PERSISTENCE_ERROR_CAUSES'),
    'SCHEMA_SQL': ('hermes_state_common', 'SCHEMA_SQL'),
    'SCHEMA_VERSION': ('hermes_state_common', 'SCHEMA_VERSION'),
    'SESSION_STATUS_COMPLETE': ('hermes_state_sessions', 'SESSION_STATUS_COMPLETE'),
    'SESSION_STATUS_EMPTY': ('hermes_state_sessions', 'SESSION_STATUS_EMPTY'),
    'SESSION_STATUS_ERROR': ('hermes_state_sessions', 'SESSION_STATUS_ERROR'),
    'SESSION_STATUS_INTERRUPTED': ('hermes_state_sessions', 'SESSION_STATUS_INTERRUPTED'),
    'SKILL_EXCERPT_JOINT': ('agent.skill_commands', 'SKILL_EXCERPT_JOINT'),
    'SKILL_SCAFFOLD_SQL_LIKE': ('agent.skill_commands', 'SKILL_SCAFFOLD_SQL_LIKE'),
    'SessionTurnLeaseLostError': ('hermes_state_errors', 'SessionTurnLeaseLostError'),
    'WalUnsupportedError': ('hermes_state_wal', 'WalUnsupportedError'),
    'apply_durability_barriers': ('hermes_state_repair', 'apply_durability_barriers'),
    'classify_session_status': ('hermes_state_sessions', 'classify_session_status'),
    'collect_state_db_stats': ('hermes_state_dbfile', 'collect_state_db_stats'),
    'count_db_holders': ('hermes_state_dbfile', 'count_db_holders'),
    'describe_skill_invocation': ('agent.skill_commands', 'describe_skill_invocation'),
    'fts5_cjk_so_path': ('hermes_state_fts', 'fts5_cjk_so_path'),
    'is_advisory_lock_contention': ('hermes_state_common', 'is_advisory_lock_contention'),
    'is_automatic_end_reason': ('hermes_state_common', 'is_automatic_end_reason'),
    'is_disk_full_error': ('hermes_state_errors', 'is_disk_full_error'),
    'is_sqlite_wal_reset_vulnerable': ('hermes_state_wal', 'is_sqlite_wal_reset_vulnerable'),
    'is_transient_sqlite_error': ('hermes_state_errors', 'is_transient_sqlite_error'),
    'iter_deleted_sqlite_sidecar_holders': ('hermes_state_dbfile', 'iter_deleted_sqlite_sidecar_holders'),
    'release_or_close': ('hermes_state_registry', 'release_or_close'),
    'report_startup_progress': ('hermes_startup_watchdog', 'report_startup_progress'),
    'resolve_journal_mode': ('hermes_state_wal', 'resolve_journal_mode'),
    'resolve_synchronous_level': ('hermes_state_wal', 'resolve_synchronous_level'),
    'sanitize_context': ('agent.memory_manager', 'sanitize_context'),
    'sqlite_source_id': ('hermes_state_wal', 'sqlite_source_id'),
    'workspace_key': ('hermes_state_sessions', 'workspace_key'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
