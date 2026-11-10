"""Protected passing ids come from a structured recorder result, never from stdout."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from hyperwilfred_core import regression
from hyperwilfred_core.evaluators.base import SubprocessOutcome
from hyperwilfred_core.evaluators.code import pytest_evaluator

GOOD = "def double(value):\n    return value * 2\n\n\ndef announce():\n    return None\n"
SPOOF = ("def double(value):\n    return value * 3\n\n\n"
         "def announce():\n"
         "    print('PASSED test_calc.py::test_double')\n"
         "    print('PASSED test_calc.py::test_removed')\n")
TEST = ("from calc import announce, double\n\n\n"
        "def test_announce():\n    announce()\n\n\n"
        "def test_double():\n    assert double(3) == 6\n")
NODE = "test_calc.py::test_double"


def _product(root: Path) -> Path:
    product = root / "product"
    product.mkdir()
    (product / "calc.py").write_text(GOOD)
    (product / "test_calc.py").write_text(TEST)
    return product


def test_printed_passed_lines_cannot_register_a_failing_or_deleted_test(tmp_path: Path) -> None:
    product = _product(tmp_path)
    (product / "calc.py").write_text(SPOOF)
    passing, returncode, _findings = regression._pytest_passing(product)
    assert returncode == 1
    assert passing == {"test_calc.py::test_announce"}


def test_spoofed_stdout_cannot_hide_a_broken_baseline_test_from_compare(tmp_path: Path) -> None:
    product = _product(tmp_path)
    baseline = regression.capture_baseline(product)
    assert NODE in baseline["passing_ids"]
    (product / "calc.py").write_text(SPOOF)
    assert regression._test_file_hashes(product) == baseline["test_file_hashes"]
    result = regression.compare(product, baseline)
    assert not result.passed
    assert "no longer passing" in (result.details or "") and NODE in (result.details or "")


def test_only_fully_passing_non_xfail_tests_count(tmp_path: Path) -> None:
    product = tmp_path / "product"
    product.mkdir()
    (product / "test_mixed.py").write_text(
        "import pytest\n\n\n"
        "@pytest.fixture\ndef broken_teardown():\n"
        "    yield\n    raise RuntimeError('teardown')\n\n\n"
        "def test_ok():\n    pass\n\n\n"
        "def test_teardown_fails(broken_teardown):\n    pass\n\n\n"
        "def test_skipped():\n    pytest.skip('no')\n\n\n"
        "@pytest.mark.xfail(reason='known')\ndef test_xfail():\n    assert False\n\n\n"
        "@pytest.mark.xfail(reason='known')\ndef test_xpass():\n    pass\n")
    passing, returncode, _findings = regression._pytest_passing(product)
    assert returncode == 1
    assert passing == {"test_mixed.py::test_ok"}


def test_target_json_module_cannot_replace_the_recorder_encoder(tmp_path: Path) -> None:
    product = _product(tmp_path)
    (product / "json.py").write_text("def dump(*args, **kwargs):\n    raise SystemExit(0)\n")
    (product / "calc.py").write_text(SPOOF)
    passing, returncode, _findings = regression._pytest_passing(product)
    assert returncode == 1 and passing == {"test_calc.py::test_announce"}


def _result_path(argv: list[str]) -> Path | None:
    return next((Path(value) for value in argv if value.endswith(".json")), None)


def _fake(monkeypatch, returncode: int, payload: object | None) -> list[Path | None]:
    seen: list[Path | None] = []

    def fake(argv, **kwargs):
        result = _result_path(argv)
        seen.append(result)
        if result is not None and payload is not None:
            result.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        return SubprocessOutcome(returncode, f"PASSED {NODE}\n1 passed", "", 1)

    monkeypatch.setattr(regression, "run_subprocess", fake)
    return seen


_FULL = [["setup", "passed", False], ["call", "passed", False], ["teardown", "passed", False]]


@pytest.mark.parametrize("returncode,payload", [
    (0, None),
    (0, "{not json"),
    (0, ["not", "an", "object"]),
    (0, {"exitstatus": 1, "collected": [NODE], "reports": {NODE: _FULL}}),
    (0, {"exitstatus": 0, "collected": [NODE], "reports": {}}),
    (0, {"exitstatus": 0, "collected": [], "reports": {}}),
    (0, {"exitstatus": 0, "collected": [NODE], "reports": {NODE: _FULL[:2]}}),
    (0, {"exitstatus": 0, "collected": [NODE],
         "reports": {NODE: [*_FULL[:2], ["teardown", "failed", False]]}}),
    (0, {"exitstatus": 0, "collected": [NODE], "reports": {NODE: [["call", 1, "x"]]}}),
    (1, {"exitstatus": 1, "collected": [NODE], "reports": {NODE: _FULL}}),
    (0, {"exitstatus": 0, "collected": [NODE],
         "reports": {NODE: _FULL, "test_calc.py::test_ghost": _FULL}}),
])
def test_missing_corrupt_or_inconsistent_result_fails_closed(
    tmp_path: Path, monkeypatch, returncode: int, payload: object | None,
) -> None:
    product = _product(tmp_path)
    baseline = regression.capture_baseline(product)
    _fake(monkeypatch, returncode, payload)
    passing, _returncode, _findings = regression._pytest_passing(product)
    assert passing is None
    result = regression.compare(product, baseline)
    assert not result.passed and "undeterminable" in (result.details or "")
    with pytest.raises(regression.RegressionError):
        regression.capture_baseline(product)


def test_consistent_structured_result_is_accepted(tmp_path: Path, monkeypatch) -> None:
    product = _product(tmp_path)
    _fake(monkeypatch, 0, {"exitstatus": 0, "collected": [NODE], "reports": {NODE: _FULL}})
    assert regression._pytest_passing(product)[:2] == ({NODE}, 0)


def test_result_file_lives_in_a_fresh_private_directory_outside_the_target(
    tmp_path: Path, monkeypatch,
) -> None:
    product = _product(tmp_path)
    observed: list[tuple[Path, int, bool]] = []

    def fake(argv, **kwargs):
        result = _result_path(argv)
        assert result is not None and argv.index(str(result)) == 8
        observed.append((result, stat.S_IMODE(result.parent.stat().st_mode),
                         result.exists() or any(result.parent.iterdir())))
        return SubprocessOutcome(0, "", "", 1)

    monkeypatch.setattr(regression, "run_subprocess", fake)
    for _ in range(2):
        assert regression._pytest_passing(product)[0] is None
    assert len(observed) == 2 and observed[0][0].parent != observed[1][0].parent
    for result, mode, populated in observed:
        assert mode == 0o700 and not populated
        assert not result.parent.exists()
        assert not result.resolve().is_relative_to(product.resolve())


def test_ordinary_evaluator_invocation_carries_no_recorder(tmp_path: Path) -> None:
    product = _product(tmp_path)
    argv = pytest_evaluator.resolve_pytest_scope(product).argv("-q", pycache_prefix=tmp_path)
    assert argv[:6] == [sys.executable, "-I", "-B", "-X", f"pycache_prefix={tmp_path}", "-c"]
    assert argv[7] == str(product.resolve()) and argv[8] == "-q"
    assert _result_path(argv) is None
