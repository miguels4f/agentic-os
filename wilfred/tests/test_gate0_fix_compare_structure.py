"""The regression comparison is orchestration over named phases with unchanged verdicts.

The witness stability check exists once and runs before and after the protected execution;
the control authority is read once for reconciliation and once on each side of execution.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from hyperwilfred_core import regression
from hyperwilfred_core.evaluators.code import pytest_evaluator

HELPER = "tests/helpers.py"
BEFORE = ("regression vs baseline: regression evidence unavailable: "
          "pytest witnesses changed before regression execution")
DURING = "regression vs baseline: pytest witnesses changed during regression execution"


def _suite(root: Path) -> dict:
    tests = root / "tests"
    tests.mkdir()
    (root / "pytest.ini").write_text("[pytest]\ntestpaths=tests\n")
    (tests / "helpers.py").write_text("VALUE = 1\n")
    (tests / "test_guard.py").write_text(
        "from helpers import VALUE\n\n\ndef test_guard():\n    assert VALUE\n")
    return regression.capture_baseline(root)


def _compare_tree() -> ast.FunctionDef:
    source = textwrap.dedent(inspect.getsource(regression.compare))
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def test_compare_is_orchestration_without_closure_recursion_or_raise() -> None:
    tree = _compare_tree()
    nested = [node for node in ast.walk(tree) if node is not tree
              and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
    raises = [node for node in ast.walk(tree) if isinstance(node, ast.Raise)]
    calls = {node.func.id for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not nested and not raises and "compare" not in calls
    assert {"_reconcile_control_authority", "_run_protected_suite"} <= calls
    assert tree.end_lineno is not None and tree.end_lineno - tree.lineno < 90


def test_witness_stability_check_is_written_once() -> None:
    module = ast.parse(inspect.getsource(regression))
    rescans = [node for node in ast.walk(module) if isinstance(node, ast.Compare)
               and any(isinstance(side, ast.Call) and isinstance(side.func, ast.Name)
                       and side.func.id == "_control_file_hashes"
                       for side in (node.left, *node.comparators))]
    assert len(rescans) == 1
    assert callable(regression._witnesses_unchanged)


def test_witness_check_runs_before_and_after_execution(tmp_path: Path, monkeypatch) -> None:
    baseline = _suite(tmp_path)
    original = regression._witnesses_unchanged
    phases: list[bool] = []

    def spy(*args, **kwargs):
        phases.append(executed)
        return original(*args, **kwargs)

    executed = False
    runner = regression._pytest_passing

    def run(*args, **kwargs):
        nonlocal executed
        executed = True
        return runner(*args, **kwargs)

    monkeypatch.setattr(regression, "_witnesses_unchanged", spy)
    monkeypatch.setattr(regression, "_pytest_passing", run)
    assert regression.compare(tmp_path, baseline).passed
    assert phases == [False, True]


def test_witness_changed_before_execution_is_reported_and_never_runs(
        tmp_path: Path, monkeypatch) -> None:
    baseline = _suite(tmp_path)
    resolve = pytest_evaluator.resolve_pytest_scope
    calls: list[int] = []

    def mutate_on_recheck(target):
        calls.append(1)
        if len(calls) == 2:
            (tmp_path / "tests" / "test_guard.py").write_text("def test_guard(): pass\n")
        return resolve(target)

    monkeypatch.setattr(regression, "resolve_pytest_scope", mutate_on_recheck)
    monkeypatch.setattr(regression, "_pytest_passing",
                        lambda *a, **k: pytest.fail("changed witness reached execution"))
    result = regression.compare(tmp_path, baseline)
    assert not result.passed and result.details == BEFORE


def test_witness_changed_during_execution_is_reported(tmp_path: Path, monkeypatch) -> None:
    baseline = _suite(tmp_path)
    runner = regression._pytest_passing

    def mutate(*args, **kwargs):
        outcome = runner(*args, **kwargs)
        (tmp_path / "tests" / "helpers.py").write_text("VALUE = 2\n")
        return outcome

    monkeypatch.setattr(regression, "_pytest_passing", mutate)
    result = regression.compare(tmp_path, baseline)
    assert not result.passed and result.details == DURING


def _authorized(tmp_path: Path, monkeypatch, revoke_at: int | None):
    baseline = _suite(tmp_path)
    old = baseline["control_file_hashes"][HELPER]
    (tmp_path / "tests" / "helpers.py").write_text("VALUE = 2\n")
    new = regression._control_file_hashes(pytest_evaluator.resolve_pytest_scope(tmp_path))[HELPER]
    monkeypatch.setattr(regression, "legacy_control_witness",
                        lambda *_a: (None, dict(baseline["control_file_hashes"])))
    calls: list[int] = []

    def authority() -> dict[str, tuple[str, str]]:
        calls.append(1)
        return {} if len(calls) == revoke_at else {HELPER: (old, new)}

    return baseline, authority, calls


def test_control_authority_is_read_once_per_phase(tmp_path: Path, monkeypatch) -> None:
    baseline, authority, calls = _authorized(tmp_path, monkeypatch, None)
    result = regression.compare(tmp_path, baseline, base_commit="0" * 40,
                                control_authority=authority)
    assert result.passed and len(calls) == 3
    assert result.details == (
        "no regression: all 1 baseline-passing test(s) still pass, 1 pre-existing test file(s) "
        "unchanged; 0 exact signed semantic transition(s) approved; 1 verified executable "
        "control transition(s) approved")


@pytest.mark.parametrize(("revoke_at", "details", "executed"), [
    (2, "regression vs baseline: regression evidence unavailable: "
        "control authority changed before regression execution", False),
    (3, "regression vs baseline: control authority changed during regression execution", True),
])
def test_control_authority_revocation_reasons(
        tmp_path: Path, monkeypatch, revoke_at: int, details: str, executed: bool) -> None:
    baseline, authority, calls = _authorized(tmp_path, monkeypatch, revoke_at)
    runner, ran = regression._pytest_passing, []

    def run(*args, **kwargs):
        ran.append(1)
        return runner(*args, **kwargs)

    monkeypatch.setattr(regression, "_pytest_passing", run)
    result = regression.compare(tmp_path, baseline, base_commit="0" * 40,
                                control_authority=authority)
    assert not result.passed and result.details == details
    assert bool(ran) == executed and len(calls) == revoke_at


def test_relocated_witness_never_carries_the_authority(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_guard.py").write_text("def test_guard(): assert True\n")
    baseline = regression.capture_baseline(tests)
    baseline["passing_ids"] = [f"tests/{node}" for node in baseline["passing_ids"]]
    result = regression.compare(tmp_path, baseline, control_authority=lambda: pytest.fail(
        "authority read for a relocated scope"))
    assert result.passed and "0 verified executable control" in (result.details or "")
