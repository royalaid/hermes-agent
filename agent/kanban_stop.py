"""Turn-end guard for kanban workers.

Kanban workers must end with ``kanban_complete`` or ``kanban_block``. Models
(especially GLM / Qwen families) sometimes narrate the next step
("Let me write the report now") and stop with ``finish_reason=stop`` and no
tool calls. Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``.

This module owns the turn-end policy and the small lifecycle repair needed
when a tool guardrail halts a kanban worker before it can call a board tool.
The normal stop guard returns a bounded synthetic nudge; guardrail halts are
recorded as typed capability blocks.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Optional


_TERMINAL_KANBAN_TOOLS = frozenset({"kanban_complete", "kanban_block"})

_DEFAULT_MAX_ATTEMPTS = 2
logger = logging.getLogger(__name__)


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the kanban stop-guard is active for this process.

    On when ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), unless
    ``HERMES_KANBAN_STOP_NUDGE`` explicitly disables it.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return bool(task)


def _tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(tc.get("name") or "")
    fn = getattr(tc, "function", None)
    if fn is not None:
        return str(getattr(fn, "name", "") or "")
    return str(getattr(tc, "name", "") or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool."""
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS:
                    return True
        elif role == "tool":
            name = str(msg.get("name") or "")
            if name in _TERMINAL_KANBAN_TOOLS:
                return True
    return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire (not a kanban worker,
    already completed/blocked, or nudge budget exhausted).
    """
    if not kanban_stop_nudge_enabled():
        return None
    if attempts >= max_attempts:
        return None
    if session_called_kanban_terminal(messages):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running`. Ending now without a board tool "
        "causes a protocol violation (clean exit with no "
        "`kanban_complete` / `kanban_block`).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work "
        "is done, OR `kanban_block(reason=...)` if you are blocked.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


def record_kanban_guardrail_halt(decision: Any, task_id: Optional[str] = None) -> bool:
    """Record a tool guardrail halt as a capability block for this worker.

    Guardrail halts intentionally bypass the normal tool-call path, so a
    worker can otherwise exit with neither ``kanban_complete`` nor
    ``kanban_block``. Keep this lifecycle repair best-effort: a database or
    redaction failure must not turn the original, user-visible guardrail halt
    into another process crash.
    """
    env_task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    requested_task_id = (task_id or "").strip()
    if not env_task_id or (requested_task_id and requested_task_id != env_task_id):
        return False

    code = str(getattr(decision, "code", "tool_guardrail_halt") or "tool_guardrail_halt")
    tool_name = str(getattr(decision, "tool_name", "unknown_tool") or "unknown_tool")
    message = str(getattr(decision, "message", "") or "").strip()
    reason = f"Tool guardrail halted {tool_name}: {code}."
    if message:
        reason = f"{reason} {message}"
    try:
        from agent.redact import redact_sensitive_text

        reason = redact_sensitive_text(reason, force=True)
    except Exception:
        logger.debug("Could not redact guardrail halt reason", exc_info=True)

    raw_run_id = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    try:
        run_id = int(raw_run_id) if raw_run_id else None
    except (TypeError, ValueError):
        run_id = None

    try:
        from hermes_cli import kanban_db as kb

        with kb.connect_closing() as conn:
            return bool(
                kb.block_task(
                    conn,
                    env_task_id,
                    reason=reason,
                    kind="capability",
                    expected_run_id=run_id,
                )
            )
    except Exception:
        logger.warning(
            "Could not record kanban capability block for guardrail halt on %s",
            env_task_id,
            exc_info=True,
        )
        return False


__all__ = [
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "record_kanban_guardrail_halt",
    "session_called_kanban_terminal",
]
