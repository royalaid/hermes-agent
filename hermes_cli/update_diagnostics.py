"""Bounded, non-sensitive diagnostics for the Desktop update transaction."""

from __future__ import annotations

import logging
import subprocess
from typing import NoReturn


_CODE_STAGES = {
    "HDU101": "provider-proof",
    "HDU201": "candidate-staging",
    "HDU301": "source-route",
    "HDU302": "source-recovery",
    "HDA101": "current-lease",
    "HDA102": "stale-owner",
    "HDA201": "publication-cleanup",
    "HDU401": "heartbeat-probe",
    "HDU402": "heartbeat-renewal",
    "HDU403": "emergency-gate",
    "HDU999": "command-boundary",
}
_KINDS = frozenset({"io", "timeout", "protocol", "interrupted", "internal"})


def _kind(error: BaseException | None, explicit: str | None) -> str:
    if explicit in _KINDS:
        return explicit
    if isinstance(error, (subprocess.TimeoutExpired, TimeoutError)):
        return "timeout"
    if isinstance(error, (KeyboardInterrupt, InterruptedError, SystemExit)):
        return "interrupted"
    if isinstance(error, OSError):
        return "io"
    if isinstance(error, (KeyError, TypeError, ValueError)):
        return "protocol"
    return "internal"


def _os_code(error: BaseException | None) -> int | None:
    if error is None:
        return None
    for name in ("winerror", "errno"):
        try:
            value = getattr(error, name, None)
        except BaseException:
            continue
        if type(value) is int and -(1 << 31) <= value <= (1 << 31) - 1:
            return value
    return None


def failure_record(
    *,
    code: str,
    stage: str,
    error: BaseException | None = None,
    kind: str | None = None,
    os_code: int | None = None,
) -> str:
    """Return the one permitted updater failure record schema."""
    if _CODE_STAGES.get(code) != stage:
        raise ValueError("unsupported updater diagnostic identity")
    classified = _kind(error, kind)
    bounded_os_code = _os_code(error) if os_code is None else os_code
    if type(bounded_os_code) is not int or not (
        -(1 << 31) <= bounded_os_code <= (1 << 31) - 1
    ):
        bounded_os_code = None
    line = (
        "schema=1 event=desktop-update-failure "
        f"code={code} stage={stage} kind={classified} "
        f"os_code={bounded_os_code if bounded_os_code is not None else 'null'}"
    )
    encoded = line.encode("ascii", errors="strict")
    if len(encoded) > 192 or "\n" in line or "\r" in line:
        raise AssertionError("updater diagnostic schema exceeded its fixed bound")
    return line


def log_update_failure(
    logger: logging.Logger,
    *,
    code: str,
    stage: str,
    error: BaseException | None = None,
    kind: str | None = None,
    os_code: int | None = None,
    level: int = logging.ERROR,
) -> str:
    line = failure_record(
        code=code,
        stage=stage,
        error=error,
        kind=kind,
        os_code=os_code,
    )
    logger.log(level, line)
    return line


def receipt_reason(code: str, stage: str) -> str:
    if _CODE_STAGES.get(code) != stage:
        return "HDU999:command-boundary"
    return f"{code}:{stage}"


def sanitize_receipt_reason(value: object) -> str:
    if isinstance(value, str) and ":" in value:
        code, stage = value.split(":", 1)
        if _CODE_STAGES.get(code) == stage:
            return value
    return "HDU999:command-boundary"


class UpdateDiagnosticError(RuntimeError):
    """An updater failure retaining only the bounded diagnostic fields."""

    def __init__(
        self,
        *,
        code: str,
        stage: str,
        error: BaseException | None = None,
        kind: str | None = None,
        emitted: bool = False,
    ) -> None:
        self.code = code
        self.stage = stage
        self.kind = _kind(error, kind)
        self.os_code = _os_code(error)
        self.emitted = emitted
        super().__init__(failure_record(
            code=code,
            stage=stage,
            kind=self.kind,
            os_code=self.os_code,
        ))

    @property
    def reason(self) -> str:
        return receipt_reason(self.code, self.stage)

    def log(self, logger: logging.Logger, *, level: int = logging.ERROR) -> str:
        line = log_update_failure(
            logger,
            code=self.code,
            stage=self.stage,
            kind=self.kind,
            os_code=self.os_code,
            level=level,
        )
        self.emitted = True
        return line

    def raise_without_private_cause(self) -> NoReturn:
        raise self from None


def diagnostic_error(
    error: BaseException,
    *,
    code: str,
    stage: str,
    kind: str | None = None,
) -> UpdateDiagnosticError:
    if isinstance(error, UpdateDiagnosticError):
        return error
    return UpdateDiagnosticError(
        code=code,
        stage=stage,
        error=error,
        kind=kind,
    )
