"""Per-stack deterministic gate registry (Phase 2).

Phase 1 hard-coded the deterministic floor to python (ruff/mypy/pytest). Phase 2 generalises
it: the gate chain is selected by the product's declared :class:`~hyperwilfred_core.schemas.Stack`
rather than baked in, so a node product runs ``build + lint + test`` while a python product
keeps the Phase-1 floor. After the tests, every chain re-runs the tests the work touched to
prove they are deterministic (see ``flaky``), and ends with the CI-parity gate, which runs the
target repository's own pull-request CI (see ``ci_parity``). New stacks register here without
touching the engine.

The engine reads ``state.objective.stack`` and asks this registry for the chain — it still
never calls an LLM and never spawns anything; it just selects which subprocess-isolated
evaluators run.
"""

from __future__ import annotations

from collections.abc import Callable

from hyperwilfred_core.evaluators.base import BaseEvaluator
from hyperwilfred_core.evaluators.ci_parity import CiParityEvaluator
from hyperwilfred_core.evaluators.code import (
    MypyEvaluator,
    NodeBuildEvaluator,
    NodeLintEvaluator,
    NodeTestEvaluator,
    PytestEvaluator,
    RuffEvaluator,
)
from hyperwilfred_core.evaluators.flaky import FlakyTestEvaluator
from hyperwilfred_core.schemas import Stack


def _python_chain() -> list[BaseEvaluator]:
    """The python floor (lint + type + test), changed-test determinism, the repo's PR CI."""
    return [
        RuffEvaluator(),
        MypyEvaluator(),
        PytestEvaluator(),
        FlakyTestEvaluator(),
        CiParityEvaluator(),
    ]


def _node_chain() -> list[BaseEvaluator]:
    """The node stack: build + lint + test via ``npm run``, determinism, the repo's PR CI."""
    return [
        NodeBuildEvaluator(),
        NodeLintEvaluator(),
        NodeTestEvaluator(),
        FlakyTestEvaluator(),
        CiParityEvaluator(),
    ]


# The registry: a stack name → a factory that builds a fresh evaluator chain. Factories
# (not instances) so each run gets its own evaluator objects with no shared state.
GATE_REGISTRY: dict[Stack, Callable[[], list[BaseEvaluator]]] = {
    Stack.PYTHON: _python_chain,
    Stack.NODE: _node_chain,
}


def evaluators_for_stack(stack: Stack | str) -> list[BaseEvaluator]:
    """Return the deterministic gate chain for *stack*.

    Accepts a :class:`Stack` or its string value. An unknown stack is an explicit error
    rather than a silent empty chain (an empty chain would pass every gate — a dangerous
    "advance with best effort" the architecture forbids).
    """
    key = stack if isinstance(stack, Stack) else Stack(stack)
    factory = GATE_REGISTRY.get(key)
    if factory is None:  # pragma: no cover — Stack() already rejects unknown values
        raise ValueError(f"no deterministic gate chain registered for stack {key!r}")
    return factory()


def registered_stacks() -> list[str]:
    """The stack names that have a registered gate chain (for diagnostics/CLI)."""
    return sorted(s.value for s in GATE_REGISTRY)


__all__ = [
    "GATE_REGISTRY",
    "evaluators_for_stack",
    "registered_stacks",
]
