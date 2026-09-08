"""Agent internals extracted from run_agent.py so it stays focused on AIAgent."""

from hermes_mcp_update_gate import should_quiesce_mcp_bridge

if should_quiesce_mcp_bridge():
    raise SystemExit(0)

from . import jiter_preload as _jiter_preload  # noqa: F401
