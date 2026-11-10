"""Helper modules the tests can import are witnessed whatever their directory is called.

A configured test root witnesses every Python file below it; without a configured root, every
Python file in a directory below the target that holds a test module or ``conftest.py`` is a
control file, package or not. Product modules at the target root stay product code; adding a
package marker or a test cannot move a witnessed helper out of the control set.
"""

from pathlib import Path

from hyperwilfred_core import regression
from hyperwilfred_core.evaluators.code import pytest_evaluator

REPO = Path(__file__).resolve().parents[1]


def _checks_suite(root: Path) -> Path:
    checks = root / "checks"
    (checks / "support").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\ntestpaths = checks\n")
    (root / "product.py").write_text("def value():\n    return 1\n")
    (checks / "helper.py").write_text("def expect(value):\n    assert value\n")
    (checks / "support" / "__init__.py").write_text("")
    (checks / "support" / "numbers.py").write_text("ONE = 1\n")
    (checks / "test_guard.py").write_text(
        "from helper import expect\nfrom product import value\n"
        "def test_guard():\n    expect(value() == 1)\n")
    return checks


def _controls(target: Path) -> set[str]:
    scope = pytest_evaluator.resolve_pytest_scope(target)
    return {path.relative_to(scope.target).as_posix() for path in scope.control_files}


def test_configured_root_witnesses_every_helper_module(tmp_path: Path) -> None:
    _checks_suite(tmp_path)
    assert _controls(tmp_path) == {
        "checks/helper.py", "checks/support/__init__.py", "checks/support/numbers.py",
        "checks/test_guard.py"}


def test_gutted_helper_in_configured_root_is_a_control_change(tmp_path: Path) -> None:
    checks = _checks_suite(tmp_path)
    baseline = regression.capture_baseline(tmp_path)
    assert baseline["passing_ids"] == ["checks/test_guard.py::test_guard"]
    (tmp_path / "product.py").write_text("def value():\n    return 2\n")
    (checks / "helper.py").write_text("def expect(value):\n    pass\n")
    result = regression.compare(tmp_path, baseline)
    assert not result.passed
    assert "control files changed" in (result.details or "")


def test_adding_a_package_marker_cannot_unwitness_a_helper(tmp_path: Path) -> None:
    checks = _checks_suite(tmp_path)
    baseline = regression.capture_baseline(tmp_path)
    (checks / "__init__.py").write_text("")
    assert "checks/helper.py" in _controls(tmp_path)
    (checks / "helper.py").write_text("def expect(value):\n    pass\n")
    result = regression.compare(tmp_path, baseline)
    assert not result.passed
    assert "control files changed" in (result.details or "")


def test_adding_a_test_cannot_turn_a_helper_package_into_product(tmp_path: Path) -> None:
    checks = _checks_suite(tmp_path)
    baseline = regression.capture_baseline(tmp_path)
    (checks / "support" / "test_dummy.py").write_text("def test_dummy(): assert True\n")
    (checks / "support" / "numbers.py").write_text("ONE = 2\n")
    result = regression.compare(tmp_path, baseline)
    assert not result.passed
    assert "control files changed" in (result.details or "")


def test_unconfigured_target_witnesses_helpers_next_to_tests(tmp_path: Path) -> None:
    checks = tmp_path / "checks"
    checks.mkdir()
    (tmp_path / "product.py").write_text("VALUE = 1\n")
    (tmp_path / "test_root.py").write_text("def test_root(): assert True\n")
    (checks / "helper.py").write_text("VALUE = 1\n")
    (checks / "test_guard.py").write_text("def test_guard(): assert True\n")
    (tmp_path / "library").mkdir()
    (tmp_path / "library" / "code.py").write_text("VALUE = 1\n")
    assert _controls(tmp_path) == {"checks/helper.py", "checks/test_guard.py", "test_root.py"}


def test_product_module_at_unconfigured_target_root_stays_product(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\ndef test_add():\n    assert add(1, 2) == 3\n")
    baseline = regression.capture_baseline(tmp_path)
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    assert regression.compare(tmp_path, baseline).passed


def test_repository_control_set_is_the_test_tree() -> None:
    expected = {path.relative_to(REPO).as_posix() for path in (REPO / "tests").rglob("*.py")}
    expected.add("conftest.py")  # the root conftest (policy-switch isolation) is a control file too
    assert _controls(REPO) == expected
