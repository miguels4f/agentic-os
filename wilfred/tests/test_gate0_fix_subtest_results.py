"""A test that uses the ``subtests`` fixture passes only when every one of its reports passes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyperwilfred_core import regression
from hyperwilfred_core.evaluators.base import SubprocessOutcome

GOOD = "def double(value):\n    return value * 2\n"
BROKEN = "def double(value):\n    return value * 3\n"
GUARD = ("from calc import double\n\n\n"
         "def test_plain():\n    assert True\n\n\n"
         "def test_table(subtests):\n"
         "    for value in (1, 2, 3):\n"
         "        with subtests.test(value=value):\n"
         "            assert double(value) == value + value\n")
PLAIN = "tests/test_guard.py::test_plain"
TABLE = "tests/test_guard.py::test_table"
NODE = "test_calc.py::test_double"


def _product(root: Path) -> Path:
    product = root / "product"
    (product / "tests").mkdir(parents=True)
    (product / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (product / "calc.py").write_text(GOOD)
    (product / "tests" / "test_guard.py").write_text(GUARD)
    return product


def test_passing_subtests_count_as_passing(tmp_path: Path) -> None:
    passing, returncode, _findings = regression._pytest_passing(_product(tmp_path))
    assert returncode == 0
    assert passing == {PLAIN, TABLE}


def test_a_failure_only_inside_a_subtest_turns_the_comparison_red(tmp_path: Path) -> None:
    product = _product(tmp_path)
    baseline = regression.capture_baseline(product)
    assert set(baseline["passing_ids"]) == {PLAIN, TABLE}
    (product / "calc.py").write_text(BROKEN)
    passing, returncode, _findings = regression._pytest_passing(product)
    assert returncode == 1 and passing == {PLAIN}
    result = regression.compare(product, baseline)
    assert not result.passed
    assert "no longer passing" in (result.details or "") and TABLE in (result.details or "")


def test_skipped_xfailed_or_failing_subtests_keep_the_test_out(tmp_path: Path) -> None:
    product = tmp_path / "product"
    product.mkdir()
    (product / "test_sub.py").write_text(
        "import pytest\n\n\n"
        "def test_all_pass(subtests):\n"
        "    for value in (1, 2):\n"
        "        with subtests.test(value=value):\n"
        "            assert value\n\n\n"
        "def test_unused_fixture(subtests):\n    pass\n\n\n"
        "def test_one_fails(subtests):\n"
        "    with subtests.test(value=1):\n        pass\n"
        "    with subtests.test(value=2):\n        assert False\n\n\n"
        "def test_one_skipped(subtests):\n"
        "    with subtests.test(value=1):\n        pytest.skip('no')\n\n\n"
        "def test_one_xfails(subtests):\n"
        "    with subtests.test(value=1):\n        pytest.xfail('known')\n\n\n"
        "@pytest.mark.xfail(reason='known')\n"
        "def test_marked_xfail(subtests):\n"
        "    with subtests.test(value=1):\n        assert False\n")
    passing, returncode, _findings = regression._pytest_passing(product)
    assert returncode == 1
    assert passing == {"test_sub.py::test_all_pass", "test_sub.py::test_unused_fixture"}


def _recorded(monkeypatch, tmp_path: Path, returncode: int, entries: list) -> set[str] | None:
    product = tmp_path / "product"
    product.mkdir()
    (product / "test_calc.py").write_text("def test_double():\n    pass\n")
    payload = {"exitstatus": returncode, "collected": [NODE], "reports": {NODE: entries}}

    def fake(argv, **kwargs):
        result = next(Path(value) for value in argv if value.endswith(".json"))
        result.write_text(json.dumps(payload))
        return SubprocessOutcome(returncode, "", "", 1)

    monkeypatch.setattr(regression, "run_subprocess", fake)
    return regression._pytest_passing(product)[0]


SETUP = ["setup", "passed", False]
CALL = ["call", "passed", False]
TEARDOWN = ["teardown", "passed", False]


def test_recorded_subtest_calls_that_all_pass_are_accepted(monkeypatch, tmp_path: Path) -> None:
    assert _recorded(monkeypatch, tmp_path, 0, [SETUP, CALL, CALL, CALL, TEARDOWN]) == {NODE}


@pytest.mark.parametrize("returncode,entries", [
    (1, [SETUP, ["call", "failed", False], CALL, TEARDOWN]),
    (1, [SETUP, CALL, ["call", "failed", False], TEARDOWN]),
    (0, [SETUP, ["call", "skipped", False], CALL, TEARDOWN]),
    (0, [SETUP, ["call", "skipped", True], CALL, TEARDOWN]),
    (0, [SETUP, ["call", "passed", True], CALL, TEARDOWN]),
    (0, [SETUP, CALL, ["teardown", "passed", True]]),
    (0, [SETUP, CALL, ["teardown", "skipped", False]]),
    (0, [SETUP, TEARDOWN]),
    (0, [SETUP, CALL, SETUP, CALL, TEARDOWN]),
    (0, [SETUP, CALL, TEARDOWN, CALL, TEARDOWN]),
    (0, [CALL, SETUP, CALL, TEARDOWN]),
    (0, [SETUP, SETUP, CALL, TEARDOWN]),
])
def test_recorded_sequences_with_any_unclean_report_do_not_pass(
    monkeypatch, tmp_path: Path, returncode: int, entries: list,
) -> None:
    assert _recorded(monkeypatch, tmp_path, returncode, entries) == set()
