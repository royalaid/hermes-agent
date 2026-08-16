"""Import shim for the byte-pure-imported failure-investigator script.

Same pattern as ``release.py``: ``hermes-release-failure-investigator.py``
is loaded by path (its filename is not a valid Python identifier) and
re-exported so callers can do
``from scripts.fork_integration.investigator import mod``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT_PATH = Path(__file__).with_name("hermes-release-failure-investigator.py")


def _load():
    spec = importlib.util.spec_from_file_location("fork_integration_investigator", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()

globals().update({name: getattr(mod, name) for name in dir(mod) if not name.startswith("__")})
