"""Per-stack deterministic gate registry (Phase 2).

Proves the gate chain is selected by the product's stack (not hard-coded), that the node chain
build/lint/test behaves correctly (pass, fail, missing package.json, optional lint), and that
the engine's ``evaluate`` HARD-BLOCKS on a node test failure exactly as it does for python.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyperwilfred_core import engine
from hyperwilfred_core.evaluators import evaluators_for_stack, registered_stacks
from hyperwilfred_core.evaluators.base import tool_on_path
from hyperwilfred_core.evaluators.code import (
    NodeBuildEvaluator,
    NodeLintEvaluator,
    NodeTestEvaluator,
)
from hyperwilfred_core.evaluators.verification import Severity
from hyperwilfred_core.schemas import BuildObjective, Stack

_NPM = pytest.mark.skipif(not tool_on_path("npm"), reason="npm not on PATH")


def test_registry_selects_chain_by_stack() -> None:
    py = [e.name for e in evaluators_for_stack(Stack.PYTHON)]
    node = [e.name for e in evaluators_for_stack(Stack.NODE)]
    assert py == ["ruff", "mypy", "pytest", "flaky-tests", "ci-parity"]
    assert node == ["npm-build", "npm-lint", "npm-test", "flaky-tests", "ci-parity"]
    assert set(registered_stacks()) == {"python", "node"}


def test_registry_accepts_string_stack() -> None:
    assert [e.name for e in evaluators_for_stack("node")] == [
        "npm-build",
        "npm-lint",
        "npm-test",
        "flaky-tests",
        "ci-parity",
    ]


def _write_pkg(target: Path, scripts: dict[str, str]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / "package.json").write_text(
        json.dumps({"name": "p", "version": "1.0.0", "scripts": scripts}), encoding="utf-8"
    )


def test_node_missing_package_json_is_error(tmp_path: Path) -> None:
    res = NodeBuildEvaluator().run(tmp_path)
    assert res.passed is False
    assert res.severity is Severity.ERROR
    assert "no package.json" in (res.details or "")


@_NPM
def test_node_build_and_test_pass(tmp_path: Path) -> None:
    prod = tmp_path / "prod"
    _write_pkg(prod, {"build": 'node -e "process.exit(0)"', "test": 'node -e "process.exit(0)"'})
    assert NodeBuildEvaluator().run(prod).passed is True
    assert NodeTestEvaluator().run(prod).passed is True


@_NPM
def test_node_test_failure_is_blocking(tmp_path: Path) -> None:
    prod = tmp_path / "prod"
    _write_pkg(prod, {"test": 'node -e "process.exit(1)"'})
    res = NodeTestEvaluator().run(prod)
    assert res.passed is False
    assert res.severity is Severity.ERROR


def test_node_missing_lint_script_is_skipped_not_blocking(tmp_path: Path) -> None:
    prod = tmp_path / "prod"
    _write_pkg(prod, {"build": "true", "test": "true"})  # no lint script
    res = NodeLintEvaluator().run(prod)
    assert res.passed is True
    assert res.severity is Severity.INFO
    assert "skipped" in (res.details or "")


def test_node_missing_mandatory_test_script_is_error(tmp_path: Path) -> None:
    prod = tmp_path / "prod"
    _write_pkg(prod, {"build": "true"})  # no test script
    res = NodeTestEvaluator().run(prod)
    assert res.passed is False
    assert res.severity is Severity.ERROR


@_NPM
def test_engine_evaluate_hard_blocks_on_node_test_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    prod = repo / "product"
    _write_pkg(prod, {"build": 'node -e "process.exit(0)"', "test": 'node -e "process.exit(1)"'})
    obj = BuildObjective(id="o", title="node app", objective="build a node app", stack=Stack.NODE)
    engine.plan(repo_root=repo, objective=obj, run_id="n", product_dir="product")
    # advance to the evaluate gate without walking every step
    paths = engine.RunPaths(repo, "n")
    with pytest.raises(engine.GateFailure):
        engine.evaluate(repo_root=repo, run_id="n", target=prod)
    state = engine.load_state(paths)
    assert state.status.value == "gate_failed"
