"""Co-located tests make the modules next to them control files: the strict rule fails closed.

Rule: every Python file below a configured test root, and every Python file below a directory
under the target that is named ``test``, ``tests`` or ``testing`` or holds a test module or
``conftest.py``, is a control file, package or not. No heuristic tells a co-located product
package apart from a test-only package or a newly planted one, so an edit to a co-located product
module is reported as a control change. Projects point ``testpaths`` at a dedicated test directory.
"""

from pathlib import Path

from hyperwilfred_core import regression
from hyperwilfred_core.evaluators.code import pytest_evaluator


def _colocated(root: Path) -> Path:
    package = root / "src" / "pkg"
    package.mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\ntestpaths = src\n")
    (package / "__init__.py").write_text("")
    (package / "calc.py").write_text("def double(v):\n    return v * 2\n")
    (package / "test_calc.py").write_text(
        "from pkg.calc import double\n\n\ndef test_double():\n    assert double(3) == 6\n")
    return package


def _controls(target: Path) -> set[str]:
    scope = pytest_evaluator.resolve_pytest_scope(target)
    return {path.relative_to(scope.target).as_posix() for path in scope.control_files}


def test_colocated_product_modules_are_controls(tmp_path: Path) -> None:
    package = _colocated(tmp_path)
    (package / "sub").mkdir()
    (package / "sub" / "__init__.py").write_text("")
    (package / "sub" / "util.py").write_text("VALUE = 1\n")
    assert _controls(tmp_path) == {
        "src/pkg/__init__.py", "src/pkg/calc.py", "src/pkg/sub/__init__.py",
        "src/pkg/sub/util.py", "src/pkg/test_calc.py"}


def test_colocated_product_edit_is_a_control_change(tmp_path: Path) -> None:
    package = _colocated(tmp_path)
    baseline = regression.capture_baseline(tmp_path)
    assert baseline["passing_ids"] == ["src/pkg/test_calc.py::test_double"]
    (package / "calc.py").write_text("def double(v):\n    return v + v  # refactor\n")
    result = regression.compare(tmp_path, baseline)
    assert not result.passed
    assert "control files changed" in (result.details or "")


def test_unconfigured_colocated_package_is_control(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "calc.py").write_text("VALUE = 1\n")
    (package / "test_calc.py").write_text("def test_calc(): assert True\n")
    (package / "conftest.py").write_text("")
    assert _controls(tmp_path) == {
        "pkg/__init__.py", "pkg/calc.py", "pkg/conftest.py", "pkg/test_calc.py"}


def test_helpers_next_to_colocated_packages_stay_controls(tmp_path: Path) -> None:
    package = _colocated(tmp_path)
    (tmp_path / "src" / "support").mkdir()
    (tmp_path / "src" / "support" / "helper.py").write_text("VALUE = 1\n")
    (tmp_path / "src" / "fixtures").mkdir()
    (tmp_path / "src" / "fixtures" / "__init__.py").write_text("")
    (tmp_path / "src" / "fixtures" / "data.py").write_text("VALUE = 1\n")
    (package / "tests").mkdir()
    (package / "tests" / "__init__.py").write_text("")
    (package / "tests" / "helper.py").write_text("VALUE = 1\n")
    (package / "tests" / "test_more.py").write_text("def test_more(): assert True\n")
    assert _controls(tmp_path) == {
        "src/fixtures/__init__.py", "src/fixtures/data.py", "src/support/helper.py",
        "src/pkg/__init__.py", "src/pkg/calc.py", "src/pkg/test_calc.py",
        "src/pkg/tests/__init__.py", "src/pkg/tests/helper.py", "src/pkg/tests/test_more.py"}


def test_gutted_plain_helper_in_colocated_root_is_a_control_change(tmp_path: Path) -> None:
    package = _colocated(tmp_path)
    support = tmp_path / "src" / "support"
    support.mkdir()
    (support / "helper.py").write_text("def expect(value):\n    assert value\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = src\npythonpath = src/support\n")
    (package / "test_calc.py").write_text(
        "from helper import expect\nfrom pkg.calc import double\n\n\n"
        "def test_double():\n    expect(double(3) == 6)\n")
    baseline = regression.capture_baseline(tmp_path)
    assert baseline["passing_ids"] == ["src/pkg/test_calc.py::test_double"]
    (package / "calc.py").write_text("def double(v):\n    return v * 3\n")
    (support / "helper.py").write_text("def expect(value):\n    pass\n")
    result = regression.compare(tmp_path, baseline)
    assert not result.passed
    assert "control files changed" in (result.details or "")



def _package_root_suite(root: Path) -> Path:
    checks = root / "checks"
    checks.mkdir()
    (root / "pytest.ini").write_text("[pytest]\ntestpaths = checks\n")
    (root / "calc.py").write_text("def double(v):\n    return v * 2\n")
    (checks / "helper.py").write_text("def expect(value):\n    assert value\n")
    return checks


def test_package_shadowing_a_witnessed_helper_fails_closed(tmp_path: Path) -> None:
    checks = _package_root_suite(tmp_path)
    (checks / "test_guard.py").write_text(
        "from helper import expect\nfrom calc import double\n\n\n"
        "def test_guard():\n    expect(double(3) == 6)\n")
    baseline = regression.capture_baseline(tmp_path)
    assert baseline["passing_ids"] == ["checks/test_guard.py::test_guard"]
    (tmp_path / "calc.py").write_text("def double(v):\n    return v * 3\n")
    shadow = checks / "helper"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("def expect(value):\n    pass\n")
    (shadow / "test_helper_pkg.py").write_text("def test_placeholder():\n    assert True\n")
    assert {"checks/helper.py", "checks/helper/__init__.py"} <= _controls(tmp_path)
    result = regression.compare(tmp_path, baseline)
    assert not result.passed
    assert "control files changed" in (result.details or "")


def test_helper_of_a_package_test_root_is_a_control_change(tmp_path: Path) -> None:
    checks = _package_root_suite(tmp_path)
    (checks / "__init__.py").write_text("")
    (checks / "test_guard.py").write_text(
        "from checks.helper import expect\nfrom calc import double\n\n\n"
        "def test_guard():\n    expect(double(3) == 6)\n")
    assert _controls(tmp_path) == {
        "checks/__init__.py", "checks/helper.py", "checks/test_guard.py"}
    baseline = regression.capture_baseline(tmp_path)
    assert baseline["passing_ids"] == ["checks/test_guard.py::test_guard"]
    (tmp_path / "calc.py").write_text("def double(v):\n    return v * 3\n")
    (checks / "helper.py").write_text("def expect(value):\n    pass\n")
    result = regression.compare(tmp_path, baseline)
    assert not result.passed
    assert "control files changed" in (result.details or "")
