"""Agent internals -- extracted modules from run_agent.py.

These modules contain pure utility functions and self-contained classes
that were previously embedded in the 3,600-line run_agent.py. Extracting
them makes run_agent.py focused on the AIAgent orchestrator class.
"""

# This gate must run before ``jiter_preload``.  ``python -m agent.transports``
# imports this package before the target module, and jiter maps a native venv
# extension that an in-place Windows update cannot replace.  The gate is a
# top-level stdlib-only module so this check itself does not touch the venv.
from hermes_mcp_update_gate import should_quiesce_mcp_bridge

if should_quiesce_mcp_bridge():
    raise SystemExit(0)

from . import jiter_preload as _jiter_preload  # noqa: F401
