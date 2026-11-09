"""Verification evaluators — the objective-truth quality gate (forked, cleaned).

Exposes the four code evaluators wrapping ``ruff``, ``mypy``, ``pytest`` and ``coverage``,
plus :func:`run_all`, the sequential aggregator that turns a list of evaluators into a single
:class:`VerificationReport`.

The doc-consistency evaluator and PR-quality evaluators are deferred to Phase 2 (the spec's
per-stack gate registry). Phase 1's deterministic floor is build/lint/type/test.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hyperwilfred_core.evaluators.base import (
    DEFAULT_TIMEOUT_S,
    BaseEvaluator,
    SubprocessOutcome,
    run_subprocess,
    tool_on_path,
)
from hyperwilfred_core.evaluators.code import (
    CoverageEvaluator,
    MypyEvaluator,
    NodeBuildEvaluator,
    NodeLintEvaluator,
    NodeTestEvaluator,
    PytestEvaluator,
    RuffEvaluator,
)
from hyperwilfred_core.evaluators.registry import (
    GATE_REGISTRY,
    evaluators_for_stack,
    registered_stacks,
)
from hyperwilfred_core.evaluators.verification import (
    CheckResult,
    Severity,
    VerificationReport,
)


def run_all(
    target_path: Path,
    evaluators: Iterable[BaseEvaluator],
    *,
    task_id: str,
    run_id: str | None = None,
    config: dict[str, Any] | None = None,
) -> VerificationReport:
    """Run *evaluators* sequentially over *target_path* and aggregate.

    ``blocks_merge`` is True iff at least one ``CheckResult`` has ``passed=False`` AND
    ``severity ∈ {ERROR, CRITICAL}``. Warnings alone do not block. A misbehaving evaluator
    surfaces as a CRITICAL failing check, never an exception.
    """
    if run_id is None:
        run_id = f"run-{uuid.uuid4().hex[:12]}"

    checks: list[CheckResult] = []
    for evaluator in evaluators:
        try:
            result = evaluator.run(target_path, config=config)
        except Exception as exc:  # noqa: BLE001 — last-line defence.
            result = CheckResult(
                kind=evaluator.kind,
                name=evaluator.name,
                passed=False,
                severity=Severity.CRITICAL,
                details=(f"{evaluator.name}: evaluator raised unexpected exception: {exc!r}"),
                duration_ms=0,
            )
        checks.append(result)

    blocks_merge = any(
        (not check.passed) and check.severity in (Severity.ERROR, Severity.CRITICAL)
        for check in checks
    )

    return VerificationReport(
        task_id=task_id,
        run_id=run_id,
        completed_at=datetime.now(UTC),
        checks=checks,
        requires_adr=False,
        blocks_merge=blocks_merge,
    )


def default_evaluators() -> list[BaseEvaluator]:
    """The python deterministic floor: lint + type + test (the Phase-1 default chain).

    Kept for back-compat; new callers select a chain by stack via ``evaluators_for_stack``.
    """
    from hyperwilfred_core.schemas import Stack

    return evaluators_for_stack(Stack.PYTHON)


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "GATE_REGISTRY",
    "BaseEvaluator",
    "CoverageEvaluator",
    "MypyEvaluator",
    "NodeBuildEvaluator",
    "NodeLintEvaluator",
    "NodeTestEvaluator",
    "PytestEvaluator",
    "RuffEvaluator",
    "SubprocessOutcome",
    "default_evaluators",
    "evaluators_for_stack",
    "registered_stacks",
    "run_all",
    "run_subprocess",
    "tool_on_path",
]
