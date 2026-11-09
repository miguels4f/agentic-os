"""Pytest evaluator (Blueprint §5.1, §6.6 — `evaluators/code/`).

Wraps ``pytest`` invocation as a subprocess and surfaces the result as a
``CheckResult``. The evaluator does *not* import pytest as a library: that
would couple our process to the version of pytest the test target itself
uses, which can drift between repos.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, ClassVar

from hyperwilfred_core.evaluators.base import BaseEvaluator, run_subprocess
from hyperwilfred_core.evaluators.verification import CheckKind, CheckResult, Finding, Severity

# ``-rfE`` short-summary lines: ``FAILED path::name - msg`` / ``ERROR path::name - msg``.
_SUMMARY_LINE = re.compile(r"^(FAILED|ERROR) (\S+)")
_COUNT = re.compile(r"(\d+) (failed|errors?)\b")


class PytestEvaluator(BaseEvaluator):
    """Run pytest against ``target_path`` and capture pass/fail.

    Pytest exit codes (https://docs.pytest.org/en/stable/reference/exit-codes.html):

    * ``0`` → all tests passed
    * ``1`` → some tests failed
    * ``2`` → test execution interrupted by user
    * ``3`` → internal pytest error
    * ``4`` → pytest CLI usage error
    * ``5`` → no tests collected

    We treat ``0`` as ``passed=True`` and everything else as ``passed=False``.
    No-tests-collected (``5``) is reported as a WARNING rather than ERROR
    since "no tests" may be intentional for the path under check.
    """

    kind: ClassVar[CheckKind] = CheckKind.TEST
    name: ClassVar[str] = "pytest"
    binary: ClassVar[str] = "pytest"
    max_lines_in_details: ClassVar[int] = 30
    default_timeout_s: ClassVar[float] = 120.0

    def run(
        self,
        target_path: Path,
        *,
        config: dict[str, Any] | None = None,
    ) -> CheckResult:
        """Run pytest on *target_path* and return a :class:`CheckResult`."""
        timeout_s = self._resolve_timeout(config)
        argv = self._build_argv(target_path, config)

        try:
            outcome = run_subprocess(argv, timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected_error_result(exc)

        if outcome.tool_missing:
            return self._tool_missing_result(outcome)
        if outcome.timed_out:
            return self._timeout_result(outcome)

        rc = outcome.returncode
        details_body = _tail(outcome.stdout, self.max_lines_in_details)

        if rc == 0:
            return CheckResult(
                kind=self.kind,
                name=self.name,
                passed=True,
                severity=Severity.INFO,
                details=f"pytest: passed\n{details_body}".strip(),
                duration_ms=outcome.duration_ms,
            )
        if rc == 5:
            return CheckResult(
                kind=self.kind,
                name=self.name,
                passed=False,
                severity=Severity.WARNING,
                details="pytest: no tests collected",
                duration_ms=outcome.duration_ms,
            )

        severity = Severity.ERROR if rc == 1 else Severity.CRITICAL
        prefix = f"pytest (rc={rc})"
        body = details_body or (outcome.stderr or "").strip() or "test run failed"
        return CheckResult(
            kind=self.kind,
            name=self.name,
            passed=False,
            severity=severity,
            details=f"{prefix}:\n{body}",
            duration_ms=outcome.duration_ms,
            findings=_failing_findings(outcome.stdout) if rc == 1 else [],
        )

    def _build_argv(self, target_path: Path, config: dict[str, Any] | None) -> list[str]:
        # Use ``python -m pytest`` so we always invoke the pytest in the
        # current interpreter's environment (works under uv-managed venvs).
        argv: list[str] = [
            sys.executable,
            "-m",
            "pytest",
            "--tb=short",
            "-q",
            "-rfE",
            "--no-header",
        ]
        if config and config.get("markers"):
            argv.extend(["-m", str(config["markers"])])
        argv.append(str(target_path))
        return argv


def _failing_findings(stdout: str) -> list[Finding]:
    """One finding per failing/erroring node id, parsed from the ``-rfE`` short summary.

    Fail-closed: if the parsed ids do not reconcile with pytest's own ``N failed, M errors``
    count, a single ``pytest-unparsed`` finding is returned instead, so no downstream consumer
    (the known-failures tolerance) can act on a partial list.
    """
    findings: list[Finding] = []
    seen: set[str] = set()
    for raw in (stdout or "").splitlines():
        m = _SUMMARY_LINE.match(raw.strip())
        if m and m.group(2) not in seen:
            seen.add(m.group(2))
            kind = "failed" if m.group(1) == "FAILED" else "error"
            findings.append(
                Finding(
                    file=m.group(2),
                    message=f"test {kind}",
                    severity=Severity.ERROR,
                    rule_id=f"pytest-{kind}",
                )
            )
    expected: int | None = None
    for raw in reversed((stdout or "").splitlines()):
        counts = _COUNT.findall(raw)
        if counts:
            expected = sum(int(n) for n, _ in counts)
            break
    if expected is None or expected != len(findings):
        return [
            Finding(
                file="<pytest>",
                message=f"could not reconcile failing ids ({len(findings)} parsed, "
                f"{expected} reported)",
                severity=Severity.ERROR,
                rule_id="pytest-unparsed",
            )
        ]
    return findings


def _tail(text: str, limit: int) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text.strip()
    return "\n".join(lines[-limit:])


__all__ = ["PytestEvaluator"]
