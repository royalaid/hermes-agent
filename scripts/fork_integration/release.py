"""Import shim for the byte-pure-imported release script.

``hermes-integration-release-windows.py`` cannot be imported with a normal
``import`` statement because its filename is not a valid Python identifier
(it contains dashes). This shim loads it by path so the rest of the repo
(tests, CI) can do ``from scripts.fork_integration.release import mod`` and
reach every module-level name via ``mod.<name>``.

Import-time side effects in the loaded module (e.g. resolving
``MANIFEST_PATH``) are fine here: the module's ``SCRIPT_DIR`` resolves to
this package directory, where ``hermes-integration-manifest.json`` was
imported alongside it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT_PATH = Path(__file__).with_name("hermes-integration-release-windows.py")


def _load():
    spec = importlib.util.spec_from_file_location("fork_integration_release", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()

# Re-export every public module-level name so ``from
# scripts.fork_integration.release import <name>`` also works.
globals().update({name: getattr(mod, name) for name in dir(mod) if not name.startswith("__")})
