from pathlib import Path

import pytest

from hyperwilfred_core import regression
from hyperwilfred_core.evaluators.code import pytest_evaluator as evaluator


def _suite(root: Path) -> Path:
    tests = root / "tests"
    tests.mkdir()
    (root / "pytest.ini").write_text("[pytest]\ntestpaths=tests\n")
    (tests / "test_guard.py").write_text("def test_guard(): assert True\n")
    return tests


def test_new_test_cannot_execute_inside_protected_regression(tmp_path: Path) -> None:
    tests = _suite(tmp_path)
    baseline = regression.capture_baseline(tmp_path)
    marker = tmp_path / "untrusted-executed"
    (tests / "test_new.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "def test_added(): assert False\n"
    )
    assert regression.compare(tmp_path, baseline).passed
    assert not marker.exists()
    assert not evaluator.PytestEvaluator().run(tmp_path).passed
    assert marker.exists()


@pytest.mark.parametrize("control", ["conftest.py", "helpers.py"])
def test_changed_controls_rejected_before_execution(tmp_path: Path, monkeypatch, control: str):
    tests = _suite(tmp_path)
    path = tests / control
    path.write_text("VALUE = 1\n")
    baseline = regression.capture_baseline(tmp_path)
    path.write_text("VALUE = 2\n")
    monkeypatch.setattr(regression, "_pytest_passing",
                        lambda *a, **k: pytest.fail("untrusted control executed"))
    result = regression.compare(tmp_path, baseline)
    assert not result.passed and "control files changed" in result.details


@pytest.mark.parametrize("kind", ["external", "dynamic", "broken", "chained"])
def test_unsafe_plugin_controls_fail_closed(tmp_path: Path, kind: str):
    tests = _suite(tmp_path)
    plugin = tmp_path / "local.py"
    (tests / "conftest.py").write_text("pytest_plugins = ['local']\n")
    if kind == "dynamic":
        (tests / "conftest.py").write_text("pytest_plugins = make_plugins()\n")
    elif kind == "broken":
        plugin.symlink_to(tmp_path / "missing")
    elif kind == "chained":
        plugin.write_text("pytest_plugins = ['second']\n")
        (tmp_path / "second.py").symlink_to(plugin)
    with pytest.raises(ValueError):
        evaluator.resolve_pytest_scope(tmp_path)


def test_original_capture_boundary_ignores_parent_controls(tmp_path: Path):
    tests = _suite(tmp_path)
    baseline = regression.capture_baseline(tests)
    baseline["passing_ids"] = [f"tests/{node}" for node in baseline["passing_ids"]]
    (tmp_path / "conftest.py").write_text("raise AssertionError('parent executed')\n")
    assert regression.compare(tmp_path, baseline).passed


def test_baseline_capture_rejects_control_mutation(tmp_path: Path, monkeypatch):
    tests = _suite(tmp_path)
    helper = tests / "helper.py"
    helper.write_text("VALUE = 1\n")
    def mutate(_target):
        helper.write_text("VALUE = 2\n")
        return {"tests/test_guard.py::test_guard"}, 0, []
    monkeypatch.setattr(regression, "_pytest_passing", mutate)
    with pytest.raises(regression.RegressionError, match="changed during"):
        regression.capture_baseline(tmp_path)


def test_regression_rejects_inflight_configuration_change(tmp_path: Path, monkeypatch):
    _suite(tmp_path)
    baseline = regression.capture_baseline(tmp_path)
    def mutate(_target, **kwargs):
        (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths=tests\naddopts=-q\n")
        return set(baseline["passing_ids"]), 0, []
    monkeypatch.setattr(regression, "_pytest_passing", mutate)
    assert not regression.compare(tmp_path, baseline).passed


def test_suite_timeout_allows_real_nested_engine_tests():
    assert evaluator.PytestEvaluator.default_timeout_s == 600
    assert regression._PYTEST_TIMEOUT_S == 600


@pytest.mark.parametrize("statement", ["import test_new", "__import__('test_new')"])
def test_new_test_imported_by_old_control_is_not_exempt(tmp_path: Path, statement: str):
    tests = _suite(tmp_path)
    (tests / "helpers.py").write_text(f"def load():\n    {statement}\n")
    baseline = regression.capture_baseline(tmp_path)
    (tests / "test_new.py").write_text("raise RuntimeError('must not execute')\n")
    assert not regression.compare(tmp_path, baseline).passed


@pytest.mark.parametrize("field", ["passing_ids", "test_file_hashes"])
def test_malformed_relocated_witness_fails_closed(tmp_path: Path, field: str):
    tests = _suite(tmp_path)
    baseline = regression.capture_baseline(tests)
    baseline[field] = 7
    assert not regression.compare(tmp_path, baseline).passed


def test_unreadable_config_returns_failed_result(tmp_path: Path, monkeypatch):
    _suite(tmp_path)
    baseline = regression.capture_baseline(tmp_path)
    def unreadable(_path):
        raise regression.RegressionError("witness replaced")
    monkeypatch.setattr(regression, "_config_file_sha256", unreadable)
    assert not regression.compare(tmp_path, baseline).passed
