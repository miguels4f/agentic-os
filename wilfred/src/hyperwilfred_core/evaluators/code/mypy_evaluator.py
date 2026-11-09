"""Mypy typecheck evaluator (Blueprint §5.1, §6.6 — `evaluators/code/`)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, ClassVar

from hyperwilfred_core.evaluators.base import BaseEvaluator, run_subprocess
from hyperwilfred_core.evaluators.verification import CheckKind, CheckResult, Severity


class MypyEvaluator(BaseEvaluator):
    """Wrap ``mypy`` against a target file or directory.

    Mypy exit codes:

    * ``0`` → success (no errors), ``passed=True``
    * ``1`` → errors found, ``passed=False`` with first ~20 lines of stdout
      captured in ``details``
    * ``2`` → fatal config / invocation error, ``passed=False`` with stderr
    """

    kind: ClassVar[CheckKind] = CheckKind.TYPECHECK
    name: ClassVar[str] = "mypy"
    binary: ClassVar[str] = "mypy"
    max_lines_in_details: ClassVar[int] = 20

    def run(
        self,
        target_path: Path,
        *,
        config: dict[str, Any] | None = None,
    ) -> CheckResult:
        """Run mypy type-checking on *target_path* and return a :class:`CheckResult`."""
        timeout_s = self._resolve_timeout(config)

        # Mypy keeps a sqlite-backed cache. If the cwd's ``.mypy_cache``
        # is read-only or on a flaky filesystem, mypy aborts with an
        # internal "disk I/O error" before doing any analysis. Always
        # point ``--cache-dir`` at a fresh tmp dir, isolated per
        # invocation, and disable incremental mode.
        with tempfile.TemporaryDirectory(prefix="mypy-cache-") as cache_dir:
            argv = [
                self.binary,
                "--no-error-summary",
                "--show-column-numbers",
                "--hide-error-context",
                "--no-pretty",
                "--no-incremental",
                "--cache-dir",
                cache_dir,
                str(target_path),
            ]
            if config and config.get("strict"):
                argv.insert(1, "--strict")

            try:
                outcome = run_subprocess(argv, timeout_s=timeout_s)
            except Exception as exc:  # noqa: BLE001
                return self._unexpected_error_result(exc)

        if outcome.tool_missing:
            return self._tool_missing_result(outcome)
        if outcome.timed_out:
            return self._timeout_result(outcome)

        if outcome.returncode == 0:
            return CheckResult(
                kind=self.kind,
                name=self.name,
                passed=True,
                severity=Severity.INFO,
                details="mypy: no errors",
                duration_ms=outcome.duration_ms,
            )

        body = _truncate_lines(outcome.stdout or outcome.stderr, self.max_lines_in_details)
        severity = Severity.ERROR if outcome.returncode == 1 else Severity.CRITICAL
        prefix = "mypy" if outcome.returncode == 1 else f"mypy (fatal rc={outcome.returncode})"
        return CheckResult(
            kind=self.kind,
            name=self.name,
            passed=False,
            severity=severity,
            details=f"{prefix}:\n{body}" if body else f"{prefix}: failed",
            duration_ms=outcome.duration_ms,
        )


def _truncate_lines(text: str, limit: int) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text.strip()
    head = "\n".join(lines[:limit])
    return f"{head}\n  …and {len(lines) - limit} more line(s)"


__all__ = ["MypyEvaluator"]
