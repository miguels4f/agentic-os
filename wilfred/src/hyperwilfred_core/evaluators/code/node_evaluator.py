"""Node/npm deterministic gate evaluators (Phase 2 — per-stack gate registry).

The Phase-1 floor was python-only (ruff/mypy/pytest). Phase 2 generalises the deterministic
gate chain to a per-stack registry; this module is the ``node`` stack: ``build`` + ``lint`` +
``test`` driven through ``npm run <script>``.

Same contract as the python evaluators (``BaseEvaluator``): subprocess-only (no coupling to a
toolchain version), never raises (every error path resolves to a ``passed=False``
``CheckResult``), wall-clock timeout. A missing ``package.json`` or a missing *mandatory*
script (build/test) is an ERROR (a node product must build and test); a missing ``lint``
script is non-blocking (INFO, recorded as skipped) since not every node project lints.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from hyperwilfred_core.evaluators.base import BaseEvaluator, run_subprocess
from hyperwilfred_core.evaluators.verification import CheckKind, CheckResult, Severity

NODE_DEFAULT_TIMEOUT_S: float = 180.0  # node builds/tests are slower than python lint


def _read_scripts(target_path: Path) -> dict[str, str] | None:
    """Return ``package.json``'s ``scripts`` map, or ``None`` if absent/invalid."""
    pkg = target_path / "package.json"
    if not pkg.exists():
        return None
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {str(k): str(v) for k, v in scripts.items()}


class _NodeScriptEvaluator(BaseEvaluator):
    """Shared base: run ``npm run <script>`` in the target and map exit code → CheckResult."""

    script: ClassVar[str]
    mandatory: ClassVar[bool] = True  # mandatory scripts ERROR when absent; optional ones skip
    binary: ClassVar[str] = "npm"
    default_timeout_s: ClassVar[float] = NODE_DEFAULT_TIMEOUT_S
    max_output_chars: ClassVar[int] = 1500

    def run(
        self,
        target_path: Path,
        *,
        config: dict[str, Any] | None = None,
    ) -> CheckResult:
        timeout_s = self._resolve_timeout(config)
        scripts = _read_scripts(target_path)
        if scripts is None:
            return CheckResult(
                kind=self.kind,
                name=self.name,
                passed=False,
                severity=Severity.ERROR,
                details=f"{self.name}: no package.json at {target_path} — cannot gate node stack",
                duration_ms=0,
            )
        if self.script not in scripts:
            if self.mandatory:
                return CheckResult(
                    kind=self.kind,
                    name=self.name,
                    passed=False,
                    severity=Severity.ERROR,
                    details=f"{self.name}: package.json has no '{self.script}' script (required)",
                    duration_ms=0,
                )
            return CheckResult(
                kind=self.kind,
                name=self.name,
                passed=True,
                severity=Severity.INFO,
                details=f"{self.name}: no '{self.script}' script — skipped",
                duration_ms=0,
            )

        argv = [self.binary, "run", self.script]
        try:
            outcome = run_subprocess(argv, cwd=target_path, timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001 — defensive: never raise out of run().
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
                details=f"{self.name}: npm run {self.script} passed",
                duration_ms=outcome.duration_ms,
            )

        tail = (outcome.stderr or outcome.stdout or "").strip()[-self.max_output_chars :]
        return CheckResult(
            kind=self.kind,
            name=self.name,
            passed=False,
            severity=Severity.ERROR,
            details=f"{self.name} (rc={outcome.returncode}): {tail}",
            duration_ms=outcome.duration_ms,
        )


class NodeBuildEvaluator(_NodeScriptEvaluator):
    """Run ``npm run build`` — the node compile/bundle gate."""

    kind: ClassVar[CheckKind] = CheckKind.LINT  # closest existing kind for a build step
    name: ClassVar[str] = "npm-build"
    script: ClassVar[str] = "build"
    mandatory: ClassVar[bool] = True


class NodeLintEvaluator(_NodeScriptEvaluator):
    """Run ``npm run lint`` — non-blocking when no lint script is defined."""

    kind: ClassVar[CheckKind] = CheckKind.LINT
    name: ClassVar[str] = "npm-lint"
    script: ClassVar[str] = "lint"
    mandatory: ClassVar[bool] = False


class NodeTestEvaluator(_NodeScriptEvaluator):
    """Run ``npm run test`` — the node test gate."""

    kind: ClassVar[CheckKind] = CheckKind.TEST
    name: ClassVar[str] = "npm-test"
    script: ClassVar[str] = "test"
    mandatory: ClassVar[bool] = True


__all__ = [
    "NodeBuildEvaluator",
    "NodeLintEvaluator",
    "NodeTestEvaluator",
]
