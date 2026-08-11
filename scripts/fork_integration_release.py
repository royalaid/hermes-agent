#!/usr/bin/env python3
"""Fork integration release audit and isolated preparation entry point."""

from pathlib import Path
import sys


# Audit/dry-run must not create bytecode cache files inside the checkout.
sys.dont_write_bytecode = True


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from fork_integration.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
