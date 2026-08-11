"""``hermes update`` subcommand parser.

Extracted verbatim from ``hermes_cli/main.py:main()`` (god-file Phase 2).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

import argparse
import math
from typing import Callable


def _bounded_timeout(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(number) or not 0.1 <= number <= 120.0:
        raise argparse.ArgumentTypeError("must be between 0.1 and 120 seconds")
    return number


def build_update_parser(subparsers, *, cmd_update: Callable) -> None:
    """Attach the ``update`` subcommand to ``subparsers``."""
    # =========================================================================
    # update command
    # =========================================================================
    update_parser = subparsers.add_parser(
        "update",
        help="Update Hermes Agent to the latest version",
        description="Pull the latest changes from git and reinstall dependencies",
    )
    update_parser.add_argument(
        "--gateway",
        action="store_true",
        default=False,
        help="Gateway mode: use file-based IPC for prompts instead of stdin (used internally by /update)",
    )
    lifecycle_mode = update_parser.add_mutually_exclusive_group()
    lifecycle_mode.add_argument(
        "--check",
        action="store_true",
        default=False,
        help="Check whether an update is available without installing anything",
    )
    lifecycle_mode.add_argument(
        "--preflight",
        action="store_true",
        default=False,
        help="Inspect local update readiness without network access or mutation",
    )
    lifecycle_mode.add_argument(
        "--drain",
        action="store_true",
        default=False,
        help="Quiesce only verified Hermes MCP bridges (requires --yes)",
    )
    lifecycle_mode.add_argument(
        "--resume-deferred-gateway",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    update_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit one machine-readable JSON document for --preflight/--drain",
    )
    update_parser.add_argument(
        "--timeout-seconds",
        type=_bounded_timeout,
        default=12.0,
        metavar="N",
        help="Bounded wait for --drain (maximum 120 seconds)",
    )
    update_parser.add_argument(
        "--bridge-lease-id",
        default=None,
        help=argparse.SUPPRESS,
    )
    update_parser.add_argument(
        "--invocation-id",
        default=None,
        help=argparse.SUPPRESS,
    )
    update_parser.add_argument(
        "--root",
        dest="resume_root",
        default=None,
        help=argparse.SUPPRESS,
    )
    update_parser.add_argument(
        "--defer-gateway-resume",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    update_parser.add_argument(
        "--no-backup",
        action="store_true",
        default=False,
        help="Skip ALL pre-update backups for this run (both the quick state snapshot and the full zip; overrides updates.pre_update_backup)",
    )
    update_parser.add_argument(
        "--backup",
        action="store_true",
        default=False,
        help="Force a FULL pre-update backup (quick state snapshot + HERMES_HOME zip) for this run, regardless of updates.pre_update_backup",
    )
    update_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=False,
        help=(
            "Assume yes for interactive prompts (config migration, stash restore). "
            "On Windows this also authorizes interruption of only exact, verified "
            "Codex or Claude Hermes MCP bridges. API-key entry is skipped; run "
            "'hermes config migrate' separately for those."
        ),
    )
    update_parser.add_argument(
        "--branch",
        default=None,
        metavar="NAME",
        help=(
            "Update against this branch instead of the default (main). "
            "If the local checkout is on a different branch, hermes will "
            "switch to the requested branch first (auto-stashing any "
            "uncommitted changes)."
        ),
    )
    update_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Windows: proceed with the update even when another hermes.exe is detected. The concurrent process will likely cause WinError 32 warnings and may leave a reboot-deferred .exe replacement. Does NOT bypass the venv-process guard (see --force-venv).",
    )
    update_parser.add_argument(
        "--force-venv",
        action="store_true",
        default=False,
        help="Windows: mutate the venv even while other processes are running from its interpreter (desktop backend, gateway, terminals). Those processes keep native .pyd files locked, so the dependency sync will likely fail partway and strand the install half-updated. Use only if you know the detected holders are false positives.",
    )
    update_parser.set_defaults(func=cmd_update)
