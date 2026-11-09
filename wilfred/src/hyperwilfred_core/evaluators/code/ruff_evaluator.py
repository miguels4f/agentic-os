"""Ruff lint evaluator (Blueprint §5.1, §6.6 — `evaluators/code/`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from hyperwilfred_core.evaluators.base import BaseEvaluator, run_subprocess
from hyperwilfred_core.evaluators.verification import CheckKind, CheckResult, Severity


class RuffEvaluator(BaseEvaluator):
    """Wrap ``ruff check`` and surface its findings as a ``CheckResult``.

    The evaluator invokes ``ruff check --output-format=json <target>`` so the
    output is machine-readable. The exit code semantics:

    * ``0`` → no findings, ``passed=True``
    * ``1`` → findings present, ``passed=False`` with first ~10 findings
      summarised in ``details``
    * other → unexpected ruff error, ``passed=False`` with stderr captured
    """

    kind: ClassVar[CheckKind] = CheckKind.LINT
    name: ClassVar[str] = "ruff"
    binary: ClassVar[str] = "ruff"
    max_findings_in_details: ClassVar[int] = 10

    def run(
        self,
        target_path: Path,
        *,
        config: dict[str, Any] | None = None,
    ) -> CheckResult:
        """Run ruff linting on *target_path* and return a :class:`CheckResult`."""
        timeout_s = self._resolve_timeout(config)
        argv = [
            self.binary,
            "check",
            "--output-format",
            "json",
            "--no-fix",
            str(target_path),
        ]
        try:
            outcome = run_subprocess(argv, timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001 — defensive: never raise out of run().
            return self._unexpected_error_result(exc)

        if outcome.tool_missing:
            return self._tool_missing_result(outcome)
        if outcome.timed_out:
            return self._timeout_result(outcome)

        findings = _parse_ruff_json(outcome.stdout)
        if outcome.returncode == 0 and not findings:
            return CheckResult(
                kind=self.kind,
                name=self.name,
                passed=True,
                severity=Severity.INFO,
                details="ruff: no findings",
                duration_ms=outcome.duration_ms,
            )

        if outcome.returncode in (0, 1):
            details = _summarise_findings(findings, self.max_findings_in_details)
            return CheckResult(
                kind=self.kind,
                name=self.name,
                passed=False,
                severity=Severity.ERROR,
                details=details,
                duration_ms=outcome.duration_ms,
            )

        # Unexpected ruff error (e.g., bad config).
        details = (outcome.stderr or outcome.stdout or "ruff failed").strip()
        return CheckResult(
            kind=self.kind,
            name=self.name,
            passed=False,
            severity=Severity.ERROR,
            details=f"ruff (rc={outcome.returncode}): {details[:1000]}",
            duration_ms=outcome.duration_ms,
        )


def _parse_ruff_json(stdout: str) -> list[dict[str, Any]]:
    if not stdout.strip():
        return []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _summarise_findings(findings: list[dict[str, Any]], limit: int) -> str:
    total = len(findings)
    header = f"ruff: {total} finding{'s' if total != 1 else ''}"
    lines: list[str] = []
    for item in findings[:limit]:
        location = item.get("location") or {}
        row = location.get("row")
        column = location.get("column")
        filename = item.get("filename", "<unknown>")
        code = item.get("code", "??")
        message = item.get("message", "")
        line_marker = f":{row}:{column}" if row is not None else ""
        lines.append(f"  {filename}{line_marker} {code} {message}")
    if total > limit:
        lines.append(f"  …and {total - limit} more")
    body = "\n".join(lines)
    return f"{header}\n{body}" if body else header


__all__ = ["RuffEvaluator"]
