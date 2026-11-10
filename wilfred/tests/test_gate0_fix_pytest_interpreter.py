"""The witnessed pytest interpreter is isolated from planted bytecode and runner shadows."""

from __future__ import annotations

import importlib.util
import marshal
import os
import py_compile
import sys
from pathlib import Path

import pytest
from _pytest.assertion.rewrite import PYC_TAIL

from hyperwilfred_core import regression
from hyperwilfred_core.evaluators.base import SubprocessOutcome
from hyperwilfred_core.evaluators.code import pytest_evaluator

GOOD = "def double(value):\n    return value * 2\n"
BROKEN = "def double(value):\n    return value * 3\n"  # same size as GOOD
TEST = "from calc import double\n\n\ndef test_double():\n    assert double(3) == 6\n"
NODE = "test_calc.py::test_double"
FAKE_RUNNER = ("import os, sys\n"
               "sys.stdout.write('PASSED test_guard.py::test_guard\\n1 passed\\n')\n"
               "sys.stdout.flush()\n"
               "os._exit(0)\n")


def _product(root: Path) -> Path:
    product = root / "product"
    product.mkdir()
    (product / "calc.py").write_text(GOOD)
    (product / "test_calc.py").write_text(TEST)
    return product


def _plant_bytecode(product: Path, kind: str) -> None:
    """Break the product while planting header-matching bytecode that hides the break."""
    calc, test = product / "calc.py", product / "test_calc.py"
    cache = product / "__pycache__"
    cache.mkdir(exist_ok=True)
    if kind == "module":
        stamp = calc.stat()
        py_compile.compile(str(calc), cfile=str(cache / f"calc.{sys.implementation.cache_tag}.pyc"),
                           doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
        calc.write_text(BROKEN)
        os.utime(calc, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        return
    calc.write_text(BROKEN)
    stamp = test.stat()
    code = compile("def test_double():\n    pass\n", str(test), "exec")
    header = (importlib.util.MAGIC_NUMBER + b"\0\0\0\0"
              + (int(stamp.st_mtime) & 0xFFFFFFFF).to_bytes(4, "little")
              + (stamp.st_size & 0xFFFFFFFF).to_bytes(4, "little"))
    (cache / f"test_calc{PYC_TAIL}").write_bytes(header + marshal.dumps(code))


@pytest.mark.parametrize("kind", ["module", "test"])
def test_planted_bytecode_fails_regression_compare_closed(tmp_path: Path, kind: str) -> None:
    product = _product(tmp_path)
    baseline = regression.capture_baseline(product)
    assert NODE in baseline["passing_ids"]
    _plant_bytecode(product, kind)
    assert regression._test_file_hashes(product) == baseline["test_file_hashes"]
    result = regression.compare(product, baseline)
    assert not result.passed
    assert "bytecode" in (result.details or "")


@pytest.mark.parametrize("kind", ["module", "test"])
def test_protected_run_never_reads_bytecode_next_to_source(tmp_path: Path, kind: str) -> None:
    product = _product(tmp_path)
    _plant_bytecode(product, kind)
    passing, returncode, _findings = regression._pytest_passing(product)
    assert passing is not None and NODE not in passing and returncode == 1
    assert not pytest_evaluator.PytestEvaluator().run(product).passed


def test_bytecode_scan_skips_tool_directories_and_catches_the_rest(tmp_path: Path) -> None:
    product = _product(tmp_path)
    baseline = regression.capture_baseline(product)
    for relative in (".venv/lib/cached.pyc", ".git/objects/x.pyc",
                     ".hw/run/__pycache__/cached.pyc"):
        (product / relative).parent.mkdir(parents=True, exist_ok=True)
        (product / relative).write_bytes(b"")
    assert regression.compare(product, baseline).passed
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    for relative in ("pkg/__pycache__", "pkg/legacy.pyo", "calc.pyc"):
        planted = product / relative
        planted.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith("__pycache__"):
            planted.symlink_to(outside, target_is_directory=True)
        else:
            planted.write_bytes(b"")
        result = regression.compare(product, baseline)
        assert not result.passed and relative in (result.details or "")
        planted.unlink()
    assert regression.compare(product, baseline).passed


def _failing_product(root: Path) -> Path:
    product = root / "product"
    product.mkdir()
    (product / "test_guard.py").write_text("def test_guard():\n    assert False\n")
    return product


@pytest.mark.parametrize("shadow", ["pytest.py", "_pytest/__init__.py", "pluggy.py",
                                    "iniconfig.py"])
def test_runner_names_at_target_root_cannot_replace_the_runner(
    tmp_path: Path, shadow: str,
) -> None:
    product = _failing_product(tmp_path)
    (product / shadow).parent.mkdir(parents=True, exist_ok=True)
    (product / shadow).write_text(FAKE_RUNNER)
    passing, returncode, _findings = regression._pytest_passing(product)
    assert passing == set() and returncode == 1
    assert not pytest_evaluator.PytestEvaluator().run(product).passed


def test_target_root_stays_importable_once_the_runner_is_loaded(tmp_path: Path) -> None:
    product = tmp_path / "product"
    (product / "tests").mkdir(parents=True)
    (product / "pytest.py").write_text(FAKE_RUNNER)
    (product / "app.py").write_text(GOOD)
    (product / "tests" / "test_app.py").write_text(
        "import pytest\nfrom app import double\n\n\ndef test_app():\n"
        "    assert double(3) == 6 and hasattr(pytest, 'raises')\n")
    passing, returncode, _findings = regression._pytest_passing(product)
    assert passing == {"tests/test_app.py::test_app"} and returncode == 0
    assert pytest_evaluator.PytestEvaluator().run(product).passed


def test_inherited_pythonpath_cannot_replace_the_runner(tmp_path: Path, monkeypatch) -> None:
    product = _failing_product(tmp_path)
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "pytest.py").write_text(FAKE_RUNNER)
    monkeypatch.setenv("PYTHONPATH", str(shadow))
    passing, returncode, _findings = regression._pytest_passing(product)
    assert passing == set() and returncode == 1
    assert not pytest_evaluator.PytestEvaluator().run(product).passed


def test_environment_drops_every_inherited_python_variable(tmp_path: Path, monkeypatch) -> None:
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONSAFEPATH",
                 "PYTHONSTARTUP", "PYTHONPYCACHEPREFIX", "PYTHONINSPECT"):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "")
    env = pytest_evaluator.pytest_environment()
    assert {name for name in env if name.startswith("PYTHON")} == {"PYTHONDONTWRITEBYTECODE"}
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def _observe(monkeypatch, module) -> list[tuple[list[str], Path, bool]]:
    seen: list[tuple[list[str], Path, bool]] = []

    def fake(argv, **kwargs):
        prefix = Path(argv[argv.index("-X") + 1].removeprefix("pycache_prefix="))
        seen.append((argv, prefix, prefix.is_dir() and not any(prefix.iterdir())))
        return SubprocessOutcome(0, "PASSED test_calc.py::test_double\n1 passed", "", 1)

    monkeypatch.setattr(module, "run_subprocess", fake)
    return seen


@pytest.mark.parametrize("gate", ["regression", "evaluator"])
def test_every_witnessed_invocation_uses_a_fresh_isolated_interpreter(
    tmp_path: Path, monkeypatch, gate: str,
) -> None:
    product = _product(tmp_path)
    seen = _observe(monkeypatch, regression if gate == "regression" else pytest_evaluator)
    for _ in range(2):
        if gate == "regression":
            regression._pytest_passing(product)
        else:
            assert pytest_evaluator.PytestEvaluator().run(product).passed
    assert len(seen) == 2 and seen[0][1] != seen[1][1]
    for argv, prefix, fresh in seen:
        assert argv[:6] == [sys.executable, "-I", "-B", "-X", f"pycache_prefix={prefix}", "-c"]
        assert "import sys, pytest;" in argv[6] and argv[7] == str(product.resolve())
        assert fresh and not prefix.exists()
        assert not prefix.resolve().is_relative_to(product.resolve())
