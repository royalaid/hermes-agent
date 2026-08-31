"""Durable delivery-obligation ledger for gateway final responses.

A final agent response that was generated but not yet confirmed-delivered
to the messaging platform is the one artifact the gateway can lose without
a trace: the turn already burned its tokens, the text exists only in a
Python local, and a crash / planned restart between finalize and platform
ACK drops it silently (#58818, #41696, #63695).

This module records a small durable row per outbound final response in the
shared ``state.db`` (same file and conventions as
``tools.async_delegation`` — WAL, owner pid + process-start-time liveness,
bounded retention). The gateway writes three checkpoints around the send:

    record_obligation()   state='pending'     before any send attempt
    mark_attempting()     state='attempting'  immediately before the await
    mark_delivered() /    state='delivered'   only on SendResult.success
    mark_failed()         state='failed'      on a definitive rejection

On startup, ``sweep_recoverable()`` claims rows whose owning process is
dead and hands them to the gateway for redelivery. After a platform adapter
reconnects without a process restart, ``sweep_failed_for_runtime()`` may claim
only the same live process's explicitly allowlisted transient failures. Crash
semantics are explicit about ambiguity (the contract review of the earlier
delivery-outbox attempt, #61790, closed it for silently resending ambiguous
sends):

- ``pending``     — the send never started: redeliver plainly, no dup risk.
- ``attempting``  — crashed mid-await: the platform MAY already have the
  message. Redelivered WITH a visible recovered-reply marker so the
  contract is honest at-least-once, never a silent duplicate.
- ``failed``      — definitively rejected once; the restart is a natural
  retry boundary. Also carries the marker.
- ``delivered``   — nothing to do; retention prunes.

Poison rows cannot spin: attempts are capped, stale rows expire, and both
transition to ``abandoned`` (kept briefly for inspection, then pruned).

Ordinary response rows remain best-effort. Claim-bound completed results are
fail-closed: their durable ownership must exist before execution ownership ends.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DB_LOCK = threading.Lock()

# Redelivery policy knobs (module constants; deliberately not config — the
# ledger itself is gated by ``gateway.delivery_ledger`` and these bounds
# only matter in the rare recovery path).
MAX_ATTEMPTS = 3
STALE_AFTER_SECONDS = 24 * 60 * 60
_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_ROWS = 500
MAX_CONTENT_BYTES = 1_000_000


class DeliveryObligationConflict(RuntimeError):
    """The same durable result identity was presented with different bytes."""


class DeliveryObligationCapacityError(RuntimeError):
    """The bounded ledger cannot safely admit another owed result."""

# Visible prefix for redeliveries that might duplicate an already-received
# message (crash mid-send / post-rejection retry). Honest at-least-once.
RECOVERED_MARKER = (
    "♻️ Recovered reply — the gateway restarted during delivery, "
    "so this may be a duplicate:\n\n"
)

# Runtime recovery uses a distinct marker because no gateway restart occurred.
# Keep the ambiguity explicit: a network rejection normally means the platform
# did not accept the message, but an acknowledgement can be lost independently.
RECONNECTED_MARKER = (
    "♻️ Recovered reply — the messaging platform reconnected after the original "
    "delivery failed, so this may be a duplicate:\n\n"
)

# Runtime replay is deliberately fail-closed. Only errors whose send contract
# proves they are transient reconnect failures belong here; permanent rejects
# (blocked bot, bad auth, missing chat) must not be retried merely because an
# adapter reconnected.
_RUNTIME_RETRYABLE_ERRORS = frozenset({"send_path_degraded"})


def _db_path():
    return get_hermes_home() / "state.db"


def _connect(home: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(home) / "state.db" if home is not None else _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        _initialize_schema(conn)
    except Exception:
        # A PRAGMA/DDL failure after a successful connect() must not leak the
        # just-opened connection back to the caller.
        conn.close()
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="state.db (delivery_ledger)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS delivery_obligations (
            obligation_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            thread_id TEXT,
            content TEXT NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            last_error TEXT,
            adapter_profile TEXT,
            claim_id TEXT,
            claim_event_id TEXT,
            active_turn_token TEXT,
            raw_content TEXT,
            source_json TEXT,
            message_ref TEXT
        )"""
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(delivery_obligations)")
    }
    if "adapter_profile" not in columns:
        try:
            conn.execute(
                "ALTER TABLE delivery_obligations ADD COLUMN adapter_profile TEXT"
            )
        except sqlite3.OperationalError as exc:
            # Concurrent first-use connections can both observe the old schema.
            if "duplicate column" not in str(exc).lower():
                raise
    for column in (
        "claim_id",
        "claim_event_id",
        "active_turn_token",
        "raw_content",
        "source_json",
        "message_ref",
    ):
        if column not in columns:
            try:
                conn.execute(
                    f"ALTER TABLE delivery_obligations ADD COLUMN {column} TEXT"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS
               delivery_obligations_claim_event_uq
           ON delivery_obligations(claim_id, claim_event_id)
           WHERE claim_id IS NOT NULL AND claim_event_id IS NOT NULL"""
    )


@contextmanager
def _transaction(home: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, and ALWAYS close it.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back the
    transaction; they do not close the connection. Using ``with _connect()``
    alone therefore leaks a connection — and its WAL/SHM file descriptors — on
    every call, deferring the close to the garbage collector. On a long-running
    gateway that exhausts ``RLIMIT_NOFILE`` (the cron-ledger sibling of this
    bug was #69567 / PR #69594). ``record_obligation`` runs on every outbound
    final response, so this ledger is the highest-frequency leaker.
    """
    conn = _connect(home)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _owner_stamp() -> tuple[int, Optional[int]]:
    pid = os.getpid()
    try:
        from gateway.status import get_process_start_time

        return pid, get_process_start_time(pid)
    except Exception:
        return pid, None


def _owner_alive(pid: Any, started_at: Any) -> bool:
    """True when the recorded owning process still exists (pid + start time)."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        from gateway.status import get_process_start_time

        current_start = get_process_start_time(pid)
    except Exception:
        current_start = None
    if current_start is None:
        # No such process (or unreadable) — treat unreadable-but-extant
        # processes as alive only if the pid exists. Route through the
        # cross-platform probe: ``os.kill(pid, 0)`` on Windows is NOT a
        # no-op (bpo-14484 — CPython maps sig=0 to
        # ``GenerateConsoleCtrlEvent(0, pid)``), so a raw probe here could
        # Ctrl+C the gateway's own console group whenever psutil failed to
        # read the start time of a live pid. ``_pid_exists`` keeps the
        # EPERM-means-alive semantics (exists but owned by another user).
        try:
            from gateway.status import _pid_exists
        except Exception:
            if os.name == "nt":
                # Never fall back to a raw sig-0 probe on Windows.
                return False
            try:
                os.kill(pid, 0)  # windows-footgun: ok — POSIX-only fallback branch
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            except OSError:
                return False
            return True
        try:
            return bool(_pid_exists(pid))
        except Exception:
            return False
    if started_at is None:
        return True
    try:
        return int(current_start) == int(started_at)
    except (TypeError, ValueError):
        return True


def compute_obligation_id(session_key: str, message_ref: str, content: str) -> str:
    """Stable id: same turn + same content re-records idempotently, while
    distinct threads/topics on the same chat can never collide (the
    session_key carries platform, chat and thread; ``message_ref`` is the
    triggering inbound message id, distinguishing turns in one session)."""
    payload = f"{session_key}|{message_ref}|{content}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:24]


def compute_claimed_result_id(
    session_key: str, claim_id: str, claim_event_id: str
) -> str:
    """Stable, payload-free identity for one completed claimed execution."""
    payload = f"goal-continuation-result|{session_key}|{claim_id}|{claim_event_id}"
    return hashlib.sha256(payload.encode("utf-8", "strict")).hexdigest()[:24]


def _ensure_insert_capacity(conn: sqlite3.Connection) -> None:
    """Make one terminal-row slot without ever deleting owed output."""
    cutoff = time.time() - _RETENTION_SECONDS
    conn.execute(
        """DELETE FROM delivery_obligations
           WHERE state IN ('delivered', 'abandoned') AND updated_at < ?""",
        (cutoff,),
    )
    total = conn.execute(
        "SELECT COUNT(*) FROM delivery_obligations"
    ).fetchone()[0]
    needed = max(0, total - _MAX_ROWS + 1)
    if needed:
        conn.execute(
            """DELETE FROM delivery_obligations WHERE obligation_id IN (
                 SELECT obligation_id FROM delivery_obligations
                 WHERE state IN ('delivered', 'abandoned')
                 ORDER BY updated_at ASC LIMIT ?)""",
            (needed,),
        )
        total = conn.execute(
            "SELECT COUNT(*) FROM delivery_obligations"
        ).fetchone()[0]
    if total >= _MAX_ROWS:
        raise DeliveryObligationCapacityError(
            "delivery obligation capacity exhausted"
        )


def record_claimed_result(
    *,
    session_key: str,
    claim_id: str,
    claim_event_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
    content: str,
    adapter_profile: Optional[str] = None,
    active_turn_token: Optional[str] = None,
    raw_content: Optional[str] = None,
    source_json: Optional[str] = None,
    message_ref: Optional[str] = None,
    home: Optional[Path] = None,
) -> str:
    """Durably own a completed continuation result before claim retirement.

    The claim/event pair is the logical execution identity. Replaying the exact
    completed bytes is idempotent and never resets an already-attempted or
    delivered row. Different bytes or routing for that identity fail closed.
    """
    if not claim_id or not claim_event_id:
        raise ValueError("claim result identity is required")
    if not isinstance(content, str):
        raise TypeError("claim result content must be text")
    if len(content.encode("utf-8", "replace")) > MAX_CONTENT_BYTES:
        raise DeliveryObligationCapacityError("claim result exceeds durable limit")
    stored_raw_content = content if raw_content is None else raw_content
    if not isinstance(stored_raw_content, str) or len(
        stored_raw_content.encode("utf-8", "replace")
    ) > MAX_CONTENT_BYTES:
        raise DeliveryObligationCapacityError("claim replay payload exceeds durable limit")
    stored_source_json = source_json if source_json else None
    if stored_source_json is not None:
        try:
            source_payload = json.loads(stored_source_json)
        except (TypeError, ValueError) as exc:
            raise DeliveryObligationConflict(
                "claim replay source is invalid"
            ) from exc
        if not isinstance(source_payload, dict) or len(
            stored_source_json.encode("utf-8", "replace")
        ) > 64 * 1024:
            raise DeliveryObligationConflict("claim replay source is invalid")
    stored_message_ref = str(message_ref) if message_ref else None
    if stored_message_ref is not None and len(stored_message_ref) > 512:
        raise DeliveryObligationConflict("claim replay message identity is invalid")

    obligation_id = compute_claimed_result_id(
        session_key, claim_id, claim_event_id
    )
    now = time.time()
    stored_profile = str(adapter_profile).strip() if adapter_profile else "default"
    stored_thread = str(thread_id) if thread_id else None
    stored_turn_token = (
        str(active_turn_token).strip() if active_turn_token else None
    )
    expected = (
        obligation_id,
        session_key,
        platform,
        str(chat_id),
        stored_thread,
        content,
        claim_id,
        claim_event_id,
        stored_profile,
        stored_turn_token,
        stored_raw_content,
        stored_source_json,
        stored_message_ref,
    )
    pid, started = _owner_stamp()
    with _DB_LOCK, _transaction(home) as conn:
        existing = conn.execute(
            """SELECT obligation_id, session_key, platform, chat_id, thread_id,
                      content, claim_id, claim_event_id, adapter_profile,
                      active_turn_token, raw_content, source_json, message_ref
               FROM delivery_obligations
               WHERE claim_id=? AND claim_event_id=?""",
            (claim_id, claim_event_id),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != expected:
                raise DeliveryObligationConflict(
                    "completed result identity conflicts with durable ownership"
                )
            return obligation_id

        _ensure_insert_capacity(conn)
        try:
            conn.execute(
                """INSERT INTO delivery_obligations
                   (obligation_id, session_key, platform, chat_id, thread_id,
                    content, state, attempts, created_at, updated_at,
                    owner_pid, owner_started_at, adapter_profile,
                    claim_id, claim_event_id, active_turn_token, raw_content,
                    source_json, message_ref)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    obligation_id,
                    session_key,
                    platform,
                    str(chat_id),
                    stored_thread,
                    content,
                    now,
                    now,
                    pid,
                    started,
                    stored_profile,
                    claim_id,
                    claim_event_id,
                    stored_turn_token,
                    stored_raw_content,
                    stored_source_json,
                    stored_message_ref,
                ),
            )
        except sqlite3.IntegrityError:
            raced = conn.execute(
                """SELECT obligation_id, session_key, platform, chat_id,
                          thread_id, content, claim_id, claim_event_id,
                          adapter_profile, active_turn_token, raw_content,
                          source_json, message_ref
                   FROM delivery_obligations
                   WHERE claim_id=? AND claim_event_id=?""",
                (claim_id, claim_event_id),
            ).fetchone()
            if raced is None or tuple(raced) != expected:
                raise DeliveryObligationConflict(
                    "completed result identity conflicts with durable ownership"
                )
    return obligation_id


def get_claimed_result(
    claim_id: str,
    claim_event_id: str,
    *,
    home: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Return durable publication ownership for one claim event, if present."""
    with _DB_LOCK, _transaction(home) as conn:
        row = conn.execute(
            """SELECT obligation_id, session_key, platform, chat_id, thread_id,
                      content, state, attempts, adapter_profile, active_turn_token,
                      raw_content, source_json, message_ref
               FROM delivery_obligations
               WHERE claim_id=? AND claim_event_id=?""",
            (claim_id, claim_event_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "obligation_id": row[0],
        "session_key": row[1],
        "platform": row[2],
        "chat_id": row[3],
        "thread_id": row[4],
        "content": row[5],
        "state": row[6],
        "attempts": row[7],
        "profile": row[8],
        "active_turn_token": row[9],
        "raw_content": row[10],
        "source_json": row[11],
        "message_ref": row[12],
    }


def completed_active_turn_tokens(
    *, home: Optional[Path] = None
) -> Dict[str, set[str]]:
    """Return bounded turn tokens whose claimed results own publication."""
    with _DB_LOCK, _transaction(home) as conn:
        rows = conn.execute(
            """SELECT session_key, active_turn_token
               FROM delivery_obligations
               WHERE claim_id IS NOT NULL AND claim_event_id IS NOT NULL
                 AND active_turn_token IS NOT NULL
                 AND state != 'abandoned'
               LIMIT ?""",
            (_MAX_ROWS,),
        ).fetchall()
    tokens: Dict[str, set[str]] = {}
    for session_key, token in rows:
        if token:
            tokens.setdefault(str(session_key), set()).add(str(token))
    return tokens


def prepare_claimed_result_delivery(
    obligation_id: str,
    *,
    session_key: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
    content: str,
    adapter_profile: Optional[str],
    home: Optional[Path] = None,
) -> bool:
    """Bind staged output to final visible text before its first send.

    ``False`` means the exact obligation was already delivered. Any missing
    identity, route mismatch, or unexpected state fails closed so callers do
    not publish output without durable ownership.
    """
    if not obligation_id or not session_key or not platform or not chat_id:
        raise ValueError("claimed result delivery identity is required")
    if not isinstance(content, str):
        raise TypeError("claimed result delivery content must be text")
    if len(content.encode("utf-8", "replace")) > MAX_CONTENT_BYTES:
        raise DeliveryObligationCapacityError("claim result exceeds durable limit")

    now = time.time()
    stored_profile = str(adapter_profile).strip() if adapter_profile else "default"
    stored_thread = str(thread_id) if thread_id else None
    with _DB_LOCK, _transaction(home) as conn:
        row = conn.execute(
            """SELECT session_key, platform, chat_id, thread_id,
                      adapter_profile, state, claim_id, claim_event_id,
                      owner_pid, owner_started_at
               FROM delivery_obligations WHERE obligation_id=?""",
            (obligation_id,),
        ).fetchone()
        if row is None or not row[6] or not row[7]:
            raise DeliveryObligationConflict(
                "claimed-result delivery ownership is unavailable"
            )
        actual = {
            "session_key": row[0],
            "platform": row[1],
            "chat_id": row[2],
            "thread_id": row[3],
            "adapter_profile": row[4],
        }
        expected = {
            "session_key": session_key,
            "platform": platform,
            "chat_id": str(chat_id),
            "thread_id": stored_thread,
            "adapter_profile": stored_profile,
        }
        if actual != expected:
            raise DeliveryObligationConflict(
                "claimed-result delivery route conflicts with durable ownership"
            )
        state = row[5]
        if state == "delivered":
            return False
        owner_pid, owner_started_at = _owner_stamp()
        if (row[8], row[9]) != (owner_pid, owner_started_at):
            raise DeliveryObligationConflict(
                "claimed-result delivery is owned by another process"
            )
        if state not in {"pending", "failed", "attempting"}:
            raise DeliveryObligationConflict(
                "claimed-result delivery state is invalid"
            )
        conn.execute(
            """UPDATE delivery_obligations
               SET content=?, state='attempting', updated_at=?, last_error=NULL
               WHERE obligation_id=?""",
            (content, now, obligation_id),
        )
    return True


def record_obligation(
    *,
    obligation_id: str,
    session_key: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
    content: str,
    adapter_profile: Optional[str] = None,
) -> None:
    """Record a final response as owed to the platform (state='pending')."""
    now = time.time()
    stored_profile = str(adapter_profile).strip() if adapter_profile else "default"
    stored_thread = str(thread_id) if thread_id else None
    expected = (
        session_key,
        platform,
        str(chat_id),
        stored_thread,
        content,
        stored_profile,
    )
    pid, started = _owner_stamp()
    with _DB_LOCK, _transaction() as conn:
        existing = conn.execute(
            """SELECT session_key, platform, chat_id, thread_id, content,
                      adapter_profile, claim_id, claim_event_id
               FROM delivery_obligations WHERE obligation_id=?""",
            (obligation_id,),
        ).fetchone()
        if existing is not None:
            if tuple(existing[:6]) == expected and not existing[6] and not existing[7]:
                return
            raise DeliveryObligationConflict(
                "delivery obligation identity conflicts with existing ownership"
            )
        _ensure_insert_capacity(conn)
        conn.execute(
            """INSERT INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, thread_id,
                content, state, attempts, created_at, updated_at,
                owner_pid, owner_started_at, adapter_profile)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?)""",
            (
                obligation_id,
                session_key,
                platform,
                str(chat_id),
                stored_thread,
                content,
                now,
                now,
                pid,
                started,
                stored_profile,
            ),
        )


def mark_attempting(obligation_id: str) -> None:
    _update_state(obligation_id, "attempting")


def mark_delivered(obligation_id: str) -> None:
    _update_state(obligation_id, "delivered")


def mark_claimed_result_delivered(obligation_id: str) -> bool:
    """Mark one claimed result delivered only for its current process owner."""
    return _update_claimed_result_state(obligation_id, "delivered")


def mark_claimed_result_failed(
    obligation_id: str, error: str = "platform_delivery_failed"
) -> bool:
    """Return one claimed result to failed only for its current owner."""
    return _update_claimed_result_state(obligation_id, "failed", error=error)


def mark_failed(obligation_id: str, error: str = "") -> None:
    _update_state(obligation_id, "failed", error=error)


def release_runtime_claim(obligation_id: str, error: str = "") -> bool:
    """Return an unsent runtime claim to ``failed`` without spending an attempt.

    Runtime recovery claims before clearing ``resume_pending`` so that two
    reconnect paths cannot send the same row. If the session flag cannot be
    cleared, no platform send was attempted and the claim must not consume the
    bounded redelivery budget. Release is fail-closed to the exact current
    process instance and the ``attempting`` state.
    """
    pid, started = _owner_stamp()
    if started is None:
        return False
    with _DB_LOCK, _transaction() as conn:
        cursor = conn.execute(
            """UPDATE delivery_obligations
               SET state='failed', attempts=CASE
                       WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                   updated_at=?, last_error=?
               WHERE obligation_id=? AND state='attempting'
                 AND owner_pid IS ? AND owner_started_at IS ?""",
            (time.time(), error[:500] if error else None,
             obligation_id, pid, started),
        )
    return bool(cursor.rowcount)


def _update_state(obligation_id: str, state: str, error: str = "") -> None:
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """UPDATE delivery_obligations
               SET state=?, updated_at=?, last_error=?
               WHERE obligation_id=?""",
            (state, time.time(), error[:500] if error else None, obligation_id),
        )


def _update_claimed_result_state(
    obligation_id: str, state: str, error: str = ""
) -> bool:
    pid, started = _owner_stamp()
    if started is None:
        return False
    with _DB_LOCK, _transaction() as conn:
        cursor = conn.execute(
            """UPDATE delivery_obligations
               SET state=?, updated_at=?, last_error=?
               WHERE obligation_id=? AND claim_id IS NOT NULL
                 AND claim_event_id IS NOT NULL
                 AND owner_pid IS ? AND owner_started_at IS ?""",
            (
                state,
                time.time(),
                error[:500] if error else None,
                obligation_id,
                pid,
                started,
            ),
        )
    return bool(cursor.rowcount)


def sweep_recoverable(
    now: Optional[float] = None,
    *,
    deliverable_platforms: Optional[set] = None,
    deliverable_targets: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Claim undelivered rows owned by dead processes; return them for
    redelivery.

    Claiming atomically re-stamps the owner to THIS process and increments
    ``attempts``, so a second gateway racing the same sweep cannot
    double-claim (the UPDATE is guarded on the previous owner stamp).
    Rows over the attempts cap or older than the stale cutoff transition to
    'abandoned' instead of being returned.

    ``deliverable_platforms`` (platform value strings) restricts claiming to
    platforms the caller can actually send on this boot.  ``attempts`` is the
    redelivery budget, so it must only be spent on a real send: a platform
    that failed to connect would otherwise burn one attempt per boot and hit
    the cap having never been sent once.  Rows for absent platforms are left
    untouched for a later boot; the stale cutoff still bounds them.

    ``deliverable_targets`` further scopes multiplexed gateways by exact
    ``(platform, adapter_profile)`` identity, preventing one connected bot from
    spending another disconnected bot's retry budget.
    """
    now = now if now is not None else time.time()
    pid, started = _owner_stamp()
    claimed: List[Dict[str, Any]] = []
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, platform, chat_id, thread_id,
                      content, state, attempts, created_at,
                      owner_pid, owner_started_at, adapter_profile, claim_id,
                      claim_event_id, raw_content, source_json, message_ref
               FROM delivery_obligations
               WHERE state IN ('pending', 'attempting', 'failed')
               LIMIT ?""",
            (_MAX_ROWS + 1,),
        ).fetchall()
        for (oid, session_key, platform, chat_id, thread_id, content, state,
             attempts, created_at, owner_pid, owner_started_at,
             adapter_profile, claim_id, claim_event_id, raw_content,
             source_json, message_ref) in rows:
            if _owner_alive(owner_pid, owner_started_at):
                continue  # a live gateway still owns this row
            if attempts >= MAX_ATTEMPTS or (now - created_at) > STALE_AFTER_SECONDS:
                conn.execute(
                    """UPDATE delivery_obligations
                       SET state='abandoned', updated_at=? WHERE obligation_id=?""",
                    (now, oid),
                )
                continue
            if (
                deliverable_platforms is not None
                and platform not in deliverable_platforms
            ):
                # No adapter for this platform this boot — the caller cannot
                # send, so claiming would spend an attempt on a no-op.
                continue
            if (
                deliverable_targets is not None
                and (platform, adapter_profile) not in deliverable_targets
            ):
                continue
            cursor = conn.execute(
                """UPDATE delivery_obligations
                   SET owner_pid=?, owner_started_at=?, attempts=attempts+1,
                       updated_at=?
                   WHERE obligation_id=? AND (owner_pid IS ? OR owner_pid=?)""",
                (pid, started, now, oid, owner_pid, owner_pid),
            )
            if cursor.rowcount:
                claimed.append({
                    "obligation_id": oid,
                    "session_key": session_key,
                    "platform": platform,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "content": content,
                    # pending = send never started, redeliver plainly;
                    # attempting/failed = ambiguous or rejected, carry marker.
                    "needs_marker": state != "pending",
                    "profile": adapter_profile,
                    "claim_id": claim_id,
                    "claim_event_id": claim_event_id,
                    "raw_content": raw_content,
                    "source_json": source_json,
                    "message_ref": message_ref,
                    "attempts": attempts + 1,
                })
    return claimed


def sweep_failed_for_runtime(
    platform: str,
    now: Optional[float] = None,
    *,
    profile: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Claim this process's reconnect-retryable failed rows for one adapter.

    ``profile`` scopes multiplexed gateways to the bot identity that actually
    owned the failed send; ``None`` means the primary/default adapter. The
    persisted adapter owner is independent of the routed session namespace.

    Startup recovery intentionally ignores rows owned by a live gateway. That
    protects concurrent processes, but it also means a final response rejected
    with ``send_path_degraded`` remains stranded when only the platform adapter
    reconnects. This runtime sweep closes that gap without weakening ownership:

    - only rows stamped to this exact process instance are eligible;
    - only explicitly allowlisted transient errors are eligible;
    - attempts/staleness bounds match startup recovery;
    - every update is guarded by the prior owner stamp and ``failed`` state.

    Unowned rows and rows owned by another process are left untouched for the
    normal startup/dead-owner sweep. Claimed rows always carry the reconnect
    marker because the failed send's acknowledgement is not safe to infer.
    """
    now = now if now is not None else time.time()
    pid, started = _owner_stamp()
    if started is None:
        # PID equality alone cannot distinguish this process from a stale row
        # left by an earlier process incarnation after PID reuse. Runtime replay
        # is optional recovery, so fail closed when the process fingerprint is
        # unavailable; startup recovery remains the durable fallback.
        return []
    claimed: List[Dict[str, Any]] = []
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, platform, chat_id, thread_id,
                      content, attempts, created_at, owner_pid,
                      owner_started_at, last_error, adapter_profile, claim_id,
                      claim_event_id, raw_content, source_json, message_ref
               FROM delivery_obligations
               WHERE state='failed' AND platform=?
               LIMIT ?""",
            (platform, _MAX_ROWS + 1),
        ).fetchall()
        for (
            oid,
            session_key,
            row_platform,
            chat_id,
            thread_id,
            content,
            attempts,
            created_at,
            owner_pid,
            owner_started_at,
            last_error,
            adapter_profile,
            claim_id,
            claim_event_id,
            raw_content,
            source_json,
            message_ref,
        ) in rows:
            expected_profile = (
                "default" if not profile or profile == "default" else str(profile)
            )
            if adapter_profile != expected_profile:
                continue
            # Runtime reconnect recovery may act only on its own rows. Exact
            # process-start matching prevents PID reuse from stealing work.
            if owner_pid != pid or owner_started_at != started:
                continue
            if str(last_error or "").strip().lower() not in _RUNTIME_RETRYABLE_ERRORS:
                continue
            owner_guard = (oid, owner_pid, owner_started_at)
            if attempts >= MAX_ATTEMPTS or (now - created_at) > STALE_AFTER_SECONDS:
                conn.execute(
                    """UPDATE delivery_obligations
                       SET state='abandoned', updated_at=?
                       WHERE obligation_id=? AND state='failed'
                         AND owner_pid IS ? AND owner_started_at IS ?""",
                    (now, *owner_guard),
                )
                continue
            cursor = conn.execute(
                """UPDATE delivery_obligations
                   SET state='attempting', attempts=attempts+1, updated_at=?
                   WHERE obligation_id=? AND state='failed'
                     AND owner_pid IS ? AND owner_started_at IS ?""",
                (now, *owner_guard),
            )
            if cursor.rowcount:
                claimed.append({
                    "obligation_id": oid,
                    "session_key": session_key,
                    "platform": row_platform,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "content": content,
                    "needs_marker": True,
                    "marker": RECONNECTED_MARKER,
                    "profile": adapter_profile,
                    "claim_id": claim_id,
                    "claim_event_id": claim_event_id,
                    "raw_content": raw_content,
                    "source_json": source_json,
                    "message_ref": message_ref,
                    "runtime_recovery": True,
                    "attempts": attempts + 1,
                })
    return claimed


def _prune(now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    cutoff = now - _RETENTION_SECONDS
    try:
        with _transaction() as conn:
            conn.execute(
                """DELETE FROM delivery_obligations
                   WHERE state IN ('delivered', 'abandoned') AND updated_at < ?""",
                (cutoff,),
            )
            total = conn.execute(
                "SELECT COUNT(*) FROM delivery_obligations"
            ).fetchone()[0]
            excess = max(0, total - _MAX_ROWS)
            if excess:
                conn.execute(
                    """DELETE FROM delivery_obligations WHERE obligation_id IN (
                         SELECT obligation_id FROM delivery_obligations
                         WHERE state IN ('delivered', 'abandoned')
                         ORDER BY updated_at ASC
                         LIMIT ?)""",
                    (excess,),
                )
    except Exception:
        logger.debug("delivery ledger prune failed", exc_info=True)


def ledger_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """Read the ``gateway.delivery_ledger`` config gate (default on)."""
    try:
        if config is None:
            from hermes_cli.config import load_config

            config = load_config()
        gw = config.get("gateway") or {}
        value = gw.get("delivery_ledger", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "no", "off"}
        return bool(value)
    except Exception:
        return True


def debug_rows(limit: int = 20) -> str:
    """Human-readable dump for ad-hoc inspection (sqlite3-free path)."""
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, state, attempts,
                      created_at, updated_at, last_error
               FROM delivery_obligations
               ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return json.dumps(
        [
            {
                "id": r[0], "session": r[1], "state": r[2], "attempts": r[3],
                "created_at": r[4], "updated_at": r[5], "last_error": r[6],
            }
            for r in rows
        ],
        indent=2,
    )
