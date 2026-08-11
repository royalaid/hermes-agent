"""Explicit in-process state for one updater transaction."""

from dataclasses import dataclass
from pathlib import Path


@dataclass
class _UpdateTransaction:
    """Correlation and cleanup state that must never leak into parser args."""

    lease: dict[str, object] | None = None
    invocation_id: str | None = None
    handoff_owner_pid: int | None = None
    gateway_resume_plan: dict[str, object] | None = None
    deferred_gateway_plan_path: Path | None = None
