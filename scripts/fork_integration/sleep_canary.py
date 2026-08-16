"""Disposable no_agent test fixture: a live NDJSON progress source (U8/KTD8).

Prints a small, fixed sequence of NDJSON stage lines to stdout, flushed
immediately, at a configurable interval, then (unless disabled) one final
JSON result line whose shape does NOT carry a ``"stage"`` key. This is the
live-stream source KTD8's scheduler mechanics (``cron.scheduler._run_job_script``
``on_line`` callback + reader threads, ``run_job``'s no_agent branch splitting
stage lines from the final result line) are built and tested against, and the
script the later manual Desktop proof points a real ``no_agent`` cron job at.

The real release script's own NDJSON stage-emitter is a LATER unit (KTD8's
``scripts/fork_integration/release.py`` integration) -- this fixture is
deliberately standalone and does not import it, ``release.py``, or anything
else from this package.

Stage line shape (KTD8): ``{"ts": <iso8601>, "stage": <name>, "ok": <bool>,
"detail": <str>}``. Final result line shape: ``{"ts": <iso8601>, "ok": <bool>,
"result": <str>, "stages_completed": <int>}`` -- no ``"stage"`` key, so the
scheduler's classifier (``cron.scheduler._classify_ndjson_stage_line``) never
mistakes it for a stage line.

CLI flags exist only to drive specific test scenarios; the zero-argument
invocation is the realistic "watch it run" shape for the manual Desktop proof:

    python sleep_canary.py --interval 1.0

Every flag also has a ``SLEEP_CANARY_*`` environment-variable fallback
(env wins only when the flag is omitted -- explicit CLI args always win).
This exists because cron job scripts run as ``python <script_path>`` with
no argv (``cron.scheduler._run_job_script`` never appends CLI args), so a
test driving this fixture through a REAL no_agent cron job -- as opposed to
invoking the file directly -- has no other way to configure a run:
``SLEEP_CANARY_STAGES``, ``SLEEP_CANARY_INTERVAL``, ``SLEEP_CANARY_SECRET``,
``SLEEP_CANARY_SECRET_AT_STAGE``, ``SLEEP_CANARY_SPLIT_SECRET_WRITE`` (``1``/
``true``), ``SLEEP_CANARY_OMIT_FINAL`` (``1``/``true``), ``SLEEP_CANARY_EXIT_CODE``.

Exit code is 0 on the normal path (so ``run_job``'s no_agent branch reaches
its success path and evaluates ``_parse_wake_gate``/delivery), or the value
of ``--exit-code``/``SLEEP_CANARY_EXIT_CODE`` when explicitly overridden.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

DEFAULT_STAGE_NAMES = ["warmup", "probe", "verify", "cooldown"]


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(obj: dict) -> None:
    """Write one NDJSON line and flush immediately.

    Flushing per line (not relying on Python's default block-buffering for a
    non-tty pipe) is what makes this a LIVE stream: the scheduler's reader
    threads are reading this process's stdout pipe via ``readline()`` as it
    runs, not after it exits.
    """
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stages", type=int,
        default=_env_int("SLEEP_CANARY_STAGES", len(DEFAULT_STAGE_NAMES)),
        help="Number of stage lines to emit (default: %(default)s).",
    )
    parser.add_argument(
        "--interval", type=float,
        default=_env_float("SLEEP_CANARY_INTERVAL", 1.0),
        help="Seconds to sleep between stage lines (default: %(default)s).",
    )
    parser.add_argument(
        "--secret", default=os.environ.get("SLEEP_CANARY_SECRET") or None,
        help=(
            "A synthetic secret-shaped string to embed in one stage line's "
            "detail field (redaction test fixture). Embedded in the LAST "
            "stage by default, or --secret-at-stage to pick a 1-based index."
        ),
    )
    parser.add_argument(
        "--secret-at-stage", type=int,
        default=_env_int("SLEEP_CANARY_SECRET_AT_STAGE", 0) or None,
        help="1-based stage index to embed --secret in (default: the last stage).",
    )
    parser.add_argument(
        "--split-secret-write", action="store_true",
        default=_env_bool("SLEEP_CANARY_SPLIT_SECRET_WRITE"),
        help=(
            "Write the secret-bearing stage line in two separate "
            "write()+flush() calls, split in the middle of the secret, with "
            "a brief pause between them. Simulates a line split across two "
            "OS-level read chunks -- readline()-based reading must still "
            "deliver it as one complete, correctly-redacted line, never a "
            "half-written fragment."
        ),
    )
    parser.add_argument(
        "--omit-final", action="store_true",
        default=_env_bool("SLEEP_CANARY_OMIT_FINAL"),
        help=(
            "Emit the stage lines, then exit WITHOUT the final result line "
            "(simulates a run that stops between a stage line and its "
            "conclusion, e.g. an unhandled bug in the real script)."
        ),
    )
    parser.add_argument(
        "--exit-code", type=int,
        default=_env_int("SLEEP_CANARY_EXIT_CODE", 0),
        help="Process exit code (default: %(default)s).",
    )
    args = parser.parse_args(argv)

    stage_count = max(0, args.stages)
    names = [
        DEFAULT_STAGE_NAMES[i % len(DEFAULT_STAGE_NAMES)] for i in range(stage_count)
    ]
    secret_index = (
        args.secret_at_stage if args.secret_at_stage is not None else stage_count
    )

    for i, name in enumerate(names, start=1):
        detail = f"sleep-canary stage {i}/{stage_count} ok"
        if args.secret is not None and i == secret_index:
            detail = f"{detail}; token={args.secret}"

        line = {"ts": _now_iso(), "stage": name, "ok": True, "detail": detail}

        if args.secret is not None and i == secret_index and args.split_secret_write:
            # Split the raw JSON text mid-secret across two flushed writes,
            # with no trailing newline on the first chunk -- a reader using
            # readline() must not see (or redact-and-emit) anything until
            # the second write lands the trailing "\n".
            raw = json.dumps(line)
            secret_pos = raw.find(args.secret)
            split_at = secret_pos + len(args.secret) // 2 if secret_pos >= 0 else len(raw) // 2
            sys.stdout.write(raw[:split_at])
            sys.stdout.flush()
            time.sleep(0.05)
            sys.stdout.write(raw[split_at:] + "\n")
            sys.stdout.flush()
        else:
            _emit(line)

        if i < stage_count:
            time.sleep(max(0.0, args.interval))

    if not args.omit_final:
        _emit({
            "ts": _now_iso(),
            "ok": True,
            "result": f"sleep-canary completed {stage_count}/{stage_count} stages",
            "stages_completed": stage_count,
        })

    return args.exit_code


if __name__ == "__main__":
    sys.exit(main())
