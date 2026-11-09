"""Coverage evaluator (Blueprint §5.1, §6.6 — `evaluators/code/`).

Wraps ``coverage`` (coverage.py) so a pipeline runner can ask for "coverage
of the tests in this path against this source path, with this minimum
threshold". Internally it does two subprocess invocations:

1. ``coverage run --source=<source> -m pytest <target>``
2. ``coverage report --fail-under=<threshold>``

The single ``CheckResult`` returned reflects the combined outcome.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from hyperwilfred_core.evaluators.base import BaseEvaluator, run_subprocess
from hyperwilfred_core.evaluators.verification import CheckKind, CheckResult, Severity


class CoverageEvaluator(BaseEvaluator):
    """Run coverage.py over a pytest invocation.

    Configuration keys (all optional):

    * ``threshold`` (float, default 0.0) — passed to ``--fail-under``.
    * ``source`` (str | Path) — passed to ``--source``. Defaults to
      ``target_path``.
    * ``timeout_s`` (float) — overrides default timeout for *each* of the
      two subprocess invocations.
    """

    kind: ClassVar[CheckKind] = CheckKind.TEST
    name: ClassVar[str] = "coverage"
    binary: ClassVar[str] = "coverage"
    default_timeout_s: ClassVar[float] = 180.0
    max_lines_in_details: ClassVar[int] = 30

    def run(
        self,
        target_path: Path,
        *,
        config: dict[str, Any] | None = None,
    ) -> CheckResult:
        """Run coverage measurement on *target_path* and return a :class:`CheckResult`."""
        timeout_s = self._resolve_timeout(config)
        threshold = _coerce_threshold(config)
        source = _coerce_source(config, target_path)

        with tempfile.TemporaryDirectory(prefix="cov-") as tmpdir:
            env = os.environ.copy()
            env["COVERAGE_FILE"] = str(Path(tmpdir) / ".coverage")

            run_argv = [
                sys.executable,
                "-m",
                "coverage",
                "run",
                f"--source={source}",
                "-m",
                "pytest",
                "-q",
                "--no-header",
                str(target_path),
            ]
            try:
                run_outcome = run_subprocess(run_argv, timeout_s=timeout_s, env=env)
            except Exception as exc:  # noqa: BLE001
                return self._unexpected_error_result(exc)

            if run_outcome.tool_missing:
                return self._tool_missing_result(run_outcome)
            if run_outcome.timed_out:
                return self._timeout_result(run_outcome)
            # pytest collected nothing → still produce a coverage report (it
            # just won't have any data); but treat rc>=2 as infra error.
            if run_outcome.returncode >= 2 and run_outcome.returncode != 5:
                tail = _tail(run_outcome.stdout, self.max_lines_in_details) or run_outcome.stderr
                return CheckResult(
                    kind=self.kind,
                    name=self.name,
                    passed=False,
                    severity=Severity.ERROR,
                    details=f"coverage run failed (rc={run_outcome.returncode}):\n{tail}",
                    duration_ms=run_outcome.duration_ms,
                )

            report_argv = [
                sys.executable,
                "-m",
                "coverage",
                "report",
                f"--fail-under={threshold:.2f}",
            ]
            try:
                report_outcome = run_subprocess(report_argv, timeout_s=timeout_s, env=env)
            except Exception as exc:  # noqa: BLE001
                return self._unexpected_error_result(exc)

            total_ms = run_outcome.duration_ms + report_outcome.duration_ms

            if report_outcome.tool_missing:
                return self._tool_missing_result(report_outcome)
            if report_outcome.timed_out:
                return self._timeout_result(report_outcome)

            report_body = _tail(report_outcome.stdout, self.max_lines_in_details)
            tests_failed = run_outcome.returncode == 1
            below_threshold = report_outcome.returncode == 2  # coverage's signal

            if not tests_failed and report_outcome.returncode == 0:
                return CheckResult(
                    kind=self.kind,
                    name=self.name,
                    passed=True,
                    severity=Severity.INFO,
                    details=(
                        f"coverage: ≥ {threshold:.2f}% (threshold met)\n{report_body}"
                    ).strip(),
                    duration_ms=total_ms,
                )

            details_chunks: list[str] = []
            if tests_failed:
                details_chunks.append("tests failed during coverage run")
            if below_threshold:
                details_chunks.append(f"coverage below threshold ({threshold:.2f}%)")
            if report_outcome.returncode not in (0, 2):
                details_chunks.append(f"coverage rc={report_outcome.returncode}")

            details = "; ".join(details_chunks) or "coverage failed"
            return CheckResult(
                kind=self.kind,
                name=self.name,
                passed=False,
                severity=Severity.ERROR,
                details=f"coverage: {details}\n{report_body}".strip(),
                duration_ms=total_ms,
            )


def _coerce_threshold(config: dict[str, Any] | None) -> float:
    if not config:
        return 0.0
    raw = config.get("threshold", 0.0)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 100.0:
        return 100.0
    return value


def _coerce_source(config: dict[str, Any] | None, target_path: Path) -> str:
    if config and "source" in config and config["source"]:
        return str(config["source"])
    return str(target_path)


def _tail(text: str, limit: int) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text.strip()
    return "\n".join(lines[-limit:])


__all__ = ["CoverageEvaluator"]
