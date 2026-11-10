"""The protected comparison refuses importable files that the protected run does not witness."""

from __future__ import annotations

import importlib.machinery
from pathlib import Path

import pytest

from hyperwilfred_core import regression
from hyperwilfred_core.evaluators.code.pytest_evaluator import resolve_pytest_scope

GOOD = "def double(value):\n    return value * 2\n"
BROKEN = "def double(value):\n    return value * 3\n"
GUARD = "from helper import expect\nfrom calc import double\n\n\ndef test_guard():\n" \
        "    expect(double(3) == 6)\n"
# A target-root package named like a module the runner imports lazily: it serves the real
# standard-library package and forges the recorder's result (closure check, probe 12).
FORGED_XML = '''
import json as _json, os as _os
__path__ = [_os.path.join(_os.path.dirname(_os.__file__), "xml")]
_real = _json.dump
def _forged(obj, handle, *args, **kwargs):
    if isinstance(obj, dict) and "reports" in obj:
        ok = [["setup", "passed", False], ["call", "passed", False], ["teardown", "passed", False]]
        _real({"exitstatus": 0, "collected": obj["collected"],
               "reports": {n: ok for n in obj["collected"]}}, handle); handle.flush(); _os._exit(0)
    return _real(obj, handle, *args, **kwargs)
_json.dump = _forged
'''


def _product(root: Path) -> Path:
    product = root / "product"
    tests = product / "tests"
    tests.mkdir(parents=True)
    (product / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (product / "calc.py").write_text(GOOD)
    (tests / "helper.py").write_text("def expect(value):\n    assert value\n")
    (tests / "test_guard.py").write_text(GUARD)
    return product


def _never_run(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every protected pytest execution so a test can prove none happened."""
    runs: list[Path] = []

    def record(target: Path, *args: object, **kwargs: object) -> tuple[None, None, list]:
        runs.append(target)
        return None, None, []

    monkeypatch.setattr(regression, "_pytest_passing", record)
    return runs


@pytest.mark.parametrize("suffix", importlib.machinery.EXTENSION_SUFFIXES)
@pytest.mark.parametrize("directory", ["tests", "", "pkg"])
def test_compiled_extension_fails_regression_compare_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, directory: str,
) -> None:
    product = _product(tmp_path)
    baseline = regression.capture_baseline(product)
    (product / "calc.py").write_text(BROKEN)
    planted = Path(directory, f"helper{suffix}")
    (product / planted).parent.mkdir(parents=True, exist_ok=True)
    (product / planted).write_bytes(b"\x7fELF not really a module")
    assert regression._test_file_hashes(product) == baseline["test_file_hashes"]
    runs = _never_run(monkeypatch)
    result = regression.compare(product, baseline)
    assert not result.passed and runs == []
    assert "compiled extension" in (result.details or "")
    assert planted.as_posix() in (result.details or "")


@pytest.mark.parametrize("shadow", [
    "xml/__init__.py",            # standard-library package at the target root
    "json.py",                    # standard-library module the recorder uses
    "_json.py",                   # private standard-library accelerator json loads lazily
    "pluggy.py",                  # runner dependency
    "iniconfig.py",
    "packaging/__init__.py",
    f"xml{importlib.machinery.BYTECODE_SUFFIXES[0]}",
])
def test_standard_library_or_runner_names_fail_compare_closed_before_any_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shadow: str,
) -> None:
    product = _product(tmp_path)
    baseline = regression.capture_baseline(product)
    (product / "calc.py").write_text(BROKEN)
    (product / shadow).parent.mkdir(parents=True, exist_ok=True)
    (product / shadow).write_text(FORGED_XML if shadow.startswith("xml/") else "")
    runs = _never_run(monkeypatch)
    result = regression.compare(product, baseline)
    assert not result.passed and runs == []
    reported = shadow.split("/__init__")[0]
    assert reported in (result.details or "")


def test_forged_standard_library_package_at_the_target_root_cannot_pass_a_broken_product(
    tmp_path: Path,
) -> None:
    product = _product(tmp_path)
    baseline = regression.capture_baseline(product)
    (product / "calc.py").write_text(BROKEN)
    (product / "xml").mkdir()
    (product / "xml" / "__init__.py").write_text(FORGED_XML)
    result = regression.compare(product, baseline)
    assert not result.passed
    assert "standard library or the test runner" in (result.details or "")


@pytest.mark.parametrize("shadow", ["suite/xml/__init__.py", "suite/typing.py",
                                    "suite/_pytest/__init__.py"])
def test_shadow_in_a_prepended_test_basedir_fails_compare_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shadow: str,
) -> None:
    """Package tests make pytest prepend their basedir, the first ancestor without a package
    marker, which here is not itself test code."""
    product = tmp_path / "product"
    checks = product / "suite" / "checks"
    checks.mkdir(parents=True)
    (product / "calc.py").write_text(GOOD)
    (checks / "__init__.py").write_text("")
    (checks / "test_calc.py").write_text(
        "from calc import double\n\n\ndef test_calc():\n    assert double(3) == 6\n")
    (product / "tests").mkdir()
    (product / "tests" / "test_root.py").write_text("def test_root():\n    pass\n")
    baseline = regression.capture_baseline(product)
    assert "suite/checks/test_calc.py::test_calc" in baseline["passing_ids"]
    (product / "calc.py").write_text(BROKEN)
    (product / shadow).parent.mkdir(parents=True, exist_ok=True)
    (product / shadow).write_text(FORGED_XML if "xml" in shadow else "")
    assert Path(product / shadow).resolve() not in resolve_pytest_scope(product).control_files
    runs = _never_run(monkeypatch)
    result = regression.compare(product, baseline)
    assert not result.passed and runs == []
    assert shadow.split("/__init__")[0] in (result.details or "")


@pytest.mark.parametrize("config", [
    "[pytest]\ntestpaths = tests\npythonpath = lib\n",
    "[pytest]\ntestpaths = tests\naddopts = -o pythonpath=lib\n",
    "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\npythonpath = [\"lib\"]\n",
], ids=["ini", "addopts-override", "pyproject"])
def test_shadow_in_a_configured_pythonpath_directory_fails_compare_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str,
) -> None:
    product = _product(tmp_path)
    (product / "pytest.ini").unlink()
    name = "pyproject.toml" if config.startswith("[tool") else "pytest.ini"
    (product / name).write_text(config)
    (product / "lib").mkdir()
    baseline = regression.capture_baseline(product)
    (product / "calc.py").write_text(BROKEN)
    (product / "lib" / "decimal.py").write_text("")
    runs = _never_run(monkeypatch)
    result = regression.compare(product, baseline)
    assert not result.passed and runs == [] and "lib/decimal.py" in (result.details or "")


def test_ordinary_layout_with_nested_standard_library_names_is_accepted(tmp_path: Path) -> None:
    product = tmp_path / "product"
    package = product / "src" / "app"
    package.mkdir(parents=True)
    (product / "tests" / "data" / "email").mkdir(parents=True)
    (product / "pytest.ini").write_text("[pytest]\ntestpaths = tests\npythonpath = src\n")
    (package / "__init__.py").write_text("")
    (package / "calc.py").write_text(GOOD)
    (package / "json.py").write_text("")          # a submodule, never top-level
    (package / "types").mkdir()
    (package / "types" / "__init__.py").write_text("")
    (product / "docs").mkdir()
    (product / "docs" / "xml.py").write_text("")  # not on the protected search path
    (product / "tests" / "test_app.py").write_text(
        "from app.calc import double\n\n\ndef test_app():\n    assert double(3) == 6\n")
    for relative in (".venv/lib/xml/__init__.py", ".venv/lib/native.abi3.so",
                     ".git/json.py", ".hw/run/pluggy.py"):
        (product / relative).parent.mkdir(parents=True, exist_ok=True)
        (product / relative).write_text("")
    baseline = regression.capture_baseline(product)
    assert baseline["passing_ids"] == ["tests/test_app.py::test_app"]
    result = regression.compare(product, baseline)
    assert result.passed, result.details


def test_engine_checkout_has_no_unwitnessed_importables() -> None:
    checkout = Path(__file__).resolve().parents[1]
    scope = resolve_pytest_scope(checkout)
    tests = tuple(sorted((checkout / "tests").glob("test_*.py")))
    reasons = regression._unwitnessed_importables(scope, tests)
    assert [reason for reason in reasons if "bytecode" not in reason] == []
