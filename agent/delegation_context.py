"""Context-local state for delegate_task child execution.

A Hermes process may itself be a Kanban dispatcher worker with HERMES_KANBAN_* in
os.environ. In-process delegate_task children and cron jobs fired via
``cronjob(action="run")`` are NOT dispatcher-owned, so identity gates must fail
closed for them without mutating the process-global environment.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Mapping, MutableMapping, Sequence

_DELEGATED_CHILD_CONTEXT: ContextVar[bool] = ContextVar("hermes_delegated_child_context", default=False)
# Any in-process execution that is NOT the dispatcher-owned worker (cron jobs). Kept separate
# so delegate_task-specific behaviour (subprocess env scrubbing, its error strings) is unchanged.
_NON_DISPATCHER_OWNED_CONTEXT: ContextVar[bool] = ContextVar("hermes_non_dispatcher_owned_context", default=False)

DELEGATED_CHILD_ENV_MARKER = "HERMES_DELEGATED_CHILD_CONTEXT"

KANBAN_ENV_KEYS: tuple[str, ...] = (
    "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB",
)


@contextmanager
def delegated_child_context(session_id: str | None = None) -> Iterator[None]:
    """Mark child execution and isolate its task-local session identity. Even a context
    entered without an id must restore the parent's session ContextVar (child
    construction calls ``set_current_session_id``)."""
    token = _DELEGATED_CHILD_CONTEXT.set(True)
    try:
        from gateway.session_context import scoped_current_session_id  # lazy: it calls is_delegated_child_context()

        with scoped_current_session_id(session_id):
            yield
    finally:
        _DELEGATED_CHILD_CONTEXT.reset(token)


def is_delegated_child_context() -> bool:
    """Return True while code is running for a delegate_task child."""
    return bool(_DELEGATED_CHILD_CONTEXT.get())


def enter_non_dispatcher_owned_context() -> Token[bool]:
    """Token form of :func:`non_dispatcher_owned_context` for long try/finally scopes."""
    return _NON_DISPATCHER_OWNED_CONTEXT.set(True)


def exit_non_dispatcher_owned_context(token: Token[bool]) -> None:
    """Restore the flag saved by :func:`enter_non_dispatcher_owned_context`."""
    _NON_DISPATCHER_OWNED_CONTEXT.reset(token)


@contextmanager
def non_dispatcher_owned_context() -> Iterator[None]:
    """Mark in-process execution that does NOT own the dispatcher's Kanban task; without it
    a cron agent run inside a worker is misread as that worker (kanban toolset force-added,
    ``kanban_complete`` defaulting to its task). ContextVar-scoped rather than clearing
    os.environ, which the worker's claim heartbeat and concurrent readers share."""
    token = enter_non_dispatcher_owned_context()
    try:
        yield
    finally:
        exit_non_dispatcher_owned_context(token)


def is_dispatcher_owned_worker_context() -> bool:
    """The single predicate every ``HERMES_KANBAN_*`` identity gate should use."""
    return not (_DELEGATED_CHILD_CONTEXT.get() or _NON_DISPATCHER_OWNED_CONTEXT.get())


def is_delegated_child_process_context() -> bool:
    """Return True in this process or a subprocess spawned by a child."""
    return bool(_DELEGATED_CHILD_CONTEXT.get()) or bool(os.environ.get(DELEGATED_CHILD_ENV_MARKER))


# Entrypoints that start a long-lived Hermes *server* rather than doing agent
# work.  ``gateway`` is deliberately absent — only ``gateway run`` qualifies,
# which :func:`is_server_role_argv` special-cases; ``gateway restart|status``
# are ordinary short-lived CLI calls.
SERVER_ROLE_COMMANDS: frozenset[str] = frozenset({
    "serve",
    "dashboard",
    "gui",
    "desktop",
})


def is_server_role_argv(argv: Sequence[str]) -> bool:
    """Return True when *argv* starts a long-lived Hermes server process.

    *argv* is the argument list WITHOUT the program name (``sys.argv[1:]``).
    Only the leading positional words matter, so flags anywhere are ignored.
    """
    positionals = [arg for arg in argv if not arg.startswith("-")]
    if not positionals:
        return False
    command = positionals[0]
    if command == "gateway":
        return len(positionals) > 1 and positionals[1] == "run"
    return command in SERVER_ROLE_COMMANDS


def clear_delegated_child_marker_for_server_role() -> bool:
    """Drop an inherited lineage marker in a server-role process. Returns True if one was cleared.

    A long-lived Hermes server is a fresh root of trust, not delegate_task
    child work — it serves the user's browser and desktop app, not the agent
    that happened to launch it.  Such a server is routinely started *from* an
    agent: a delegated child that runs ``hermes serve`` or launches the desktop
    app through the terminal tool spawns it with
    :data:`DELEGATED_CHILD_ENV_MARKER` in its environment, because
    :func:`delegated_child_subprocess_env` stamps the marker into every child
    env so the lineage survives a fork.  Nothing ever unsets it, so the marker
    outlives the delegated child by hours and permanently poisons the server's
    Kanban path: ``kanban_db.connect()`` runs its first-open migration pass
    through ``write_txn``, so the delegated-child guard fails the whole *read*
    path, not just mutations, and the dashboard event stream errors on every
    reconnect.

    ``tests/conftest.py`` drops the marker for exactly this reason (pytest is
    routinely launched from a delegated worker).  This is the same fix for the
    other long-lived process that is not agent work.

    Deliberately narrow: only the server entrypoints in
    :func:`is_server_role_argv` call this.  Everything an agent actually
    delegates — a ``hermes kanban`` CLI call, a python subprocess importing
    ``kanban_db``, a grandchild of either — keeps the marker and stays rejected
    by the guard.
    """
    import os

    return os.environ.pop(DELEGATED_CHILD_ENV_MARKER, None) is not None


def scrub_kanban_env(env: Mapping[str, str] | MutableMapping[str, str]) -> dict[str, str]:
    """Return *env* with dispatcher-only Kanban variables removed and the lineage marker set."""
    cleaned = {k: v for k, v in env.items() if k not in KANBAN_ENV_KEYS}
    cleaned[DELEGATED_CHILD_ENV_MARKER] = "1"
    return cleaned


def delegated_child_subprocess_env(
    env: Mapping[str, str] | MutableMapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Env override only when delegated-child lineage must cross fork: preserves ``env=None``
    inherit semantics for non-delegated calls; in a child, a scrubbed env carrying the marker."""
    if not is_delegated_child_process_context():
        return None if env is None else dict(env)
    return scrub_kanban_env(os.environ if env is None else env)
