"""Baseline capture has one calling convention for the pytest runner, with no test seam branch."""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from hyperwilfred_core import regression

GUARD = "tests/test_guard.py::test_guard"


def _suite(root: Path) -> Path:
    tests = root / "tests"
    tests.mkdir()
    (root / "pytest.ini").write_text("[pytest]\ntestpaths=tests\n")
    (tests / "test_guard.py").write_text("def test_guard(): assert True\n")
    other = root / "other"
    other.mkdir()
    (other / "test_other.py").write_text("def test_other(): assert True\n")
    return tests


def _names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def test_production_has_no_branch_on_a_replaced_runner() -> None:
    assert not hasattr(regression, "_DEFAULT_PYTEST_PASSING")
    tree = ast.parse(textwrap.dedent(inspect.getsource(regression.baseline_candidate)))
    identity = [node for node in ast.walk(tree) if isinstance(node, ast.Compare)
                and any(isinstance(op, (ast.Is, ast.IsNot)) for op in node.ops)
                and "_pytest_passing" in _names(node)]
    assert identity == []
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "_pytest_passing"]
    assert len(calls) == 1


def test_one_argument_runner_is_called_with_the_target_only(tmp_path: Path, monkeypatch) -> None:
    _suite(tmp_path)
    seen: list[Path] = []
    def runner(target):
        seen.append(target)
        return {GUARD}, 0, []
    monkeypatch.setattr(regression, "_pytest_passing", runner)
    baseline = regression.capture_baseline(tmp_path)
    assert seen == [tmp_path.resolve()]
    assert baseline["passing_ids"] == [GUARD]


def test_real_runner_resolves_the_configured_scope(tmp_path: Path) -> None:
    _suite(tmp_path)
    baseline = regression.capture_baseline(tmp_path)
    assert baseline["passing_ids"] == [GUARD]
    assert baseline["test_roots"] == ["tests"]


def test_scope_change_by_a_replaced_runner_is_refused(tmp_path: Path, monkeypatch) -> None:
    _suite(tmp_path)
    real = regression._pytest_passing
    def redirect(target):
        (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths=other\n")
        return real(target)
    monkeypatch.setattr(regression, "_pytest_passing", redirect)
    with pytest.raises(regression.RegressionError, match="changed during"):
        regression.capture_baseline(tmp_path)


def test_scope_change_inside_the_real_run_is_refused(tmp_path: Path) -> None:
    tests = _suite(tmp_path)
    (tests / "test_guard.py").write_text(
        "from pathlib import Path\n\n\n"
        "def test_guard():\n"
        f"    Path({str(tmp_path / 'pytest.ini')!r}).write_text("
        "'[pytest]\\ntestpaths=other\\n')\n"
    )
    with pytest.raises(regression.RegressionError, match="changed during"):
        regression.capture_baseline(tmp_path)


def test_scope_made_unresolvable_during_the_run_is_a_regression_error(
        tmp_path: Path, monkeypatch) -> None:
    _suite(tmp_path)
    def ambiguous(target):
        (tmp_path / "tox.ini").write_text("[pytest]\ntestpaths=other\n")
        return {GUARD}, 0, []
    monkeypatch.setattr(regression, "_pytest_passing", ambiguous)
    with pytest.raises(regression.RegressionError, match="cannot capture"):
        regression.capture_baseline(tmp_path)
