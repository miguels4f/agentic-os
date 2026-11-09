"""Abstract base class for verification evaluators (forked, runtime-agnostic).

Evaluators are subprocess-based wrappers around external tools (ruff, mypy, pytest,
coverage). They take a target path and emit a typed
:class:`~hyperwilfred_core.evaluators.verification.CheckResult` regardless of whether the
underlying tool succeeds, fails, is missing, or times out. ``run_all`` aggregates them.

Design: subprocess-only (no library coupling to a tool's version), never raises (every error
path resolves to a ``passed=False`` CheckResult), and every invocation has a wall-clock
timeout. This is the strongest, cleanest asset forked from the old factory — the
objective-truth quality gate.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from hyperwilfred_core.evaluators.verification import CheckKind, CheckResult, Severity

DEFAULT_TIMEOUT_S: float = 60.0


@dataclass(frozen=True)
class SubprocessOutcome:
    """Result of a subprocess invocation, normalised for evaluator parsing."""

    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    tool_missing: bool = False
    error: str | None = None

    @property
    def ran(self) -> bool:
        """True iff the subprocess actually executed (regardless of exit code)."""
        return not self.tool_missing and not self.timed_out and self.error is None


def run_subprocess(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    env: dict[str, str] | None = None,
) -> SubprocessOutcome:
    """Execute *argv* and capture stdout/stderr without raising."""
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 — argv is fully constructed by us.
            argv,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return SubprocessOutcome(
            returncode=-1,
            stdout="",
            stderr=str(exc),
            duration_ms=elapsed_ms,
            tool_missing=True,
            error=f"tool not on PATH: {argv[0]}",
        )
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return SubprocessOutcome(
            returncode=-1,
            stdout=(exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""),
            stderr=(exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""),
            duration_ms=elapsed_ms,
            timed_out=True,
            error=f"timed out after {timeout_s:.1f}s",
        )
    except OSError as exc:  # pragma: no cover — defensive
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return SubprocessOutcome(
            returncode=-1,
            stdout="",
            stderr=str(exc),
            duration_ms=elapsed_ms,
            error=f"OS error invoking {argv[0]}: {exc}",
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return SubprocessOutcome(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_ms=elapsed_ms,
    )


def tool_on_path(binary: str) -> bool:
    """Cheap pre-flight check used by tests/diagnostics."""
    return shutil.which(binary) is not None


def _coerce_timeout(config: dict[str, Any] | None, fallback: float) -> float:
    if not config:
        return fallback
    raw = config.get("timeout_s")
    if raw is None:
        return fallback
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    if value <= 0:
        return fallback
    return value


class BaseEvaluator(ABC):
    """Abstract base for all verification evaluators. ``run`` MUST NOT raise."""

    kind: ClassVar[CheckKind]
    name: ClassVar[str]
    binary: ClassVar[str]
    default_timeout_s: ClassVar[float] = DEFAULT_TIMEOUT_S

    @abstractmethod
    def run(
        self,
        target_path: Path,
        *,
        config: dict[str, Any] | None = None,
    ) -> CheckResult:
        """Execute the wrapped tool against *target_path*."""

    def _resolve_timeout(self, config: dict[str, Any] | None) -> float:
        return _coerce_timeout(config, self.default_timeout_s)

    def _tool_missing_result(self, outcome: SubprocessOutcome) -> CheckResult:
        return CheckResult(
            kind=self.kind,
            name=self.name,
            passed=False,
            severity=Severity.ERROR,
            details=f"{self.name}: {outcome.error or 'tool not on PATH'}",
            duration_ms=outcome.duration_ms,
        )

    def _timeout_result(self, outcome: SubprocessOutcome) -> CheckResult:
        return CheckResult(
            kind=self.kind,
            name=self.name,
            passed=False,
            severity=Severity.ERROR,
            details=f"{self.name}: {outcome.error or 'timed out'}",
            duration_ms=outcome.duration_ms,
        )

    def _unexpected_error_result(self, exc: BaseException) -> CheckResult:
        return CheckResult(
            kind=self.kind,
            name=self.name,
            passed=False,
            severity=Severity.CRITICAL,
            details=f"{self.name}: unexpected error: {exc!r}",
            duration_ms=0,
        )


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "BaseEvaluator",
    "SubprocessOutcome",
    "run_subprocess",
    "tool_on_path",
]
