"""Data files inside the test directories are part of the regression witness."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from hyperwilfred_core import regression

GOOD = "def add(a, b):\n    return a + b\n"
BROKEN = "def add(a, b):\n    return a * b\n"
GOLDEN = "[[1, 2, 3], [2, 2, 4]]"
FORGED_GOLDEN = "[[1, 2, 2], [2, 2, 4]]"
TEST = (
    "import json, pathlib\nfrom calc import add\n\n\ndef test_add_golden():\n"
    "    rows = json.loads((pathlib.Path(__file__).parent / 'golden.json').read_text())\n"
    "    for a, b, want in rows:\n        assert add(a, b) == want\n"
)
NODE = "tests/test_calc.py::test_add_golden"


def _product(root: Path) -> Path:
    product = root / "product"
    (product / "tests").mkdir(parents=True)
    (product / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (product / "calc.py").write_text(GOOD)
    (product / "tests" / "golden.json").write_text(GOLDEN)
    (product / "tests" / "test_calc.py").write_text(TEST)
    return product


def _commit(product: Path) -> str:
    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(product), "-c", "user.name=t", "-c", "user.email=t@example.test",
             *arguments], check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("add", "calc.py", "pytest.ini", "tests")
    git("commit", "-qm", "base")
    return git("rev-parse", "HEAD")


def _without(baseline: dict, *keys: str) -> dict:
    return {key: value for key, value in baseline.items() if key not in keys}


def _legacy(baseline: dict) -> dict:
    return _without(baseline, "control_file_hashes", "pytest_config_file_sha256",
                    "data_file_hashes")


def _never_run(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    runs: list[Path] = []

    def record(target: Path, *args: object, **kwargs: object) -> tuple[None, None, list]:
        runs.append(target)
        return None, None, []

    monkeypatch.setattr(regression, "_pytest_passing", record)
    return runs


def test_baseline_pins_data_files_and_an_unchanged_product_keeps_passing(tmp_path: Path) -> None:
    product = _product(tmp_path)
    baseline = regression.capture_baseline(product)
    assert baseline["schema_version"] == "wilfred-regression-baseline/v2"
    assert baseline["data_file_hashes"] == {
        "tests/golden.json": hashlib.sha256(GOLDEN.encode()).hexdigest()}
    # The runner's own cache at the target root and desktop metadata are not test inputs.
    (product / "tests" / ".DS_Store").write_bytes(b"\0")
    for _ in range(2):
        result = regression.compare(product, baseline)
        assert result.passed, result.details


@pytest.mark.parametrize("legacy", [False, True])
def test_golden_file_attack_fails_before_the_suite_runs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool) -> None:
    product = _product(tmp_path)
    head = _commit(product)
    baseline = regression.capture_baseline(product)
    if legacy:
        baseline = _legacy(baseline)
        result = regression.compare(product, baseline, base_commit=head)
        assert result.passed, result.details
    # The review's attack: break the product and rewrite the expected values to match.
    (product / "calc.py").write_text(BROKEN)
    (product / "tests" / "golden.json").write_text(FORGED_GOLDEN)
    runs = _never_run(monkeypatch)
    result = regression.compare(product, baseline, base_commit=head)
    assert not result.passed and runs == []
    assert "1 test data file(s) modified (regression-witness integrity): tests/golden.json" in (
        result.details or "")


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(("change", "reason"), [
    ("remove", "1 test data file(s) removed: tests/golden.json"),
    ("add", "1 test data file(s) added: tests/extra/case.json"),
])
def test_removed_or_added_data_file_fails_closed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool, change: str,
        reason: str) -> None:
    product = _product(tmp_path)
    head = _commit(product)
    baseline = regression.capture_baseline(product)
    baseline = _legacy(baseline) if legacy else baseline
    if change == "remove":
        (product / "tests" / "golden.json").unlink()
    else:
        (product / "tests" / "extra").mkdir()
        (product / "tests" / "extra" / "case.json").write_text("[]")
    runs = _never_run(monkeypatch)
    result = regression.compare(product, baseline, base_commit=head)
    assert not result.passed and runs == []
    assert reason in (result.details or "")


def test_new_test_module_stays_allowed_but_its_data_file_does_not(tmp_path: Path) -> None:
    product = _product(tmp_path)
    baseline = regression.capture_baseline(product)
    (product / "tests" / "test_new.py").write_text("def test_new():\n    assert True\n")
    assert regression.compare(product, baseline).passed
    (product / "tests" / "new_golden.json").write_text("[]")
    result = regression.compare(product, baseline)
    assert not result.passed
    assert "1 test data file(s) added: tests/new_golden.json" in (result.details or "")


def test_baseline_without_data_witness_needs_the_base_commit(tmp_path: Path) -> None:
    product = _product(tmp_path)
    head = _commit(product)
    captured = regression.capture_baseline(product)
    assert "data_file_hashes" in captured
    earlier = _without(captured, "data_file_hashes")
    result = regression.compare(product, earlier)
    assert not result.passed
    assert result.details == (
        "regression evidence unavailable: legacy baseline requires a full base_commit")
    assert regression.compare(product, earlier, base_commit=head).passed
    # A file that git did not track at the base commit counts as added.
    (product / "tests" / "untracked.json").write_text("{}")
    result = regression.compare(product, earlier, base_commit=head)
    assert not result.passed and "test data file(s) added: tests/untracked.json" in (
        result.details or "")


@pytest.mark.parametrize(("kind", "wording"), [
    ("symlink", "pytest witness must not be a symlink"),
    ("fifo", "pytest witness is not a regular file"),
])
def test_links_and_special_files_in_a_test_directory_are_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, wording: str) -> None:
    product = _product(tmp_path)
    baseline = regression.capture_baseline(product)
    planted = product / "tests" / "planted.json"
    if kind == "symlink":
        os.symlink(product / "tests" / "golden.json", planted)
    else:
        os.mkfifo(planted)
    with pytest.raises(regression.RegressionError, match=wording):
        regression.capture_baseline(product)
    runs = _never_run(monkeypatch)
    result = regression.compare(product, baseline)
    assert not result.passed and runs == []
    assert (result.details or "").startswith(f"regression evidence unavailable: {wording}")


def test_data_changed_just_before_the_protected_run_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    product = _product(tmp_path)
    baseline = regression.capture_baseline(product)
    original = regression._test_file_reasons

    def swap_then_report(*args: object) -> tuple[list[str], int]:
        (product / "tests" / "golden.json").write_text(FORGED_GOLDEN)
        return original(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(regression, "_test_file_reasons", swap_then_report)
    runs = _never_run(monkeypatch)
    result = regression.compare(product, baseline)
    assert not result.passed and runs == []
    assert "pytest witnesses changed before regression execution" in (result.details or "")


def test_data_changed_during_the_protected_run_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    product = _product(tmp_path)
    baseline = regression.capture_baseline(product)

    def swap_while_running(target: Path, *args: object, **kwargs: object):
        (product / "tests" / "golden.json").write_text(FORGED_GOLDEN)
        return {NODE}, 0, []

    monkeypatch.setattr(regression, "_pytest_passing", swap_while_running)
    result = regression.compare(product, baseline)
    assert not result.passed
    assert "pytest witnesses changed during regression execution" in (result.details or "")


def test_data_changed_during_baseline_capture_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    product = _product(tmp_path)

    def swap_while_running(_target: Path):
        (product / "tests" / "golden.json").write_text(FORGED_GOLDEN)
        return {NODE}, 0, []

    monkeypatch.setattr(regression, "_pytest_passing", swap_while_running)
    with pytest.raises(regression.RegressionError,
                       match="pytest witness changed during baseline capture"):
        regression.capture_baseline(product)


@pytest.mark.parametrize("forged", [["tests/golden.json"], {"tests/golden.json": "abc"},
                                    {"": "0" * 64}])
def test_malformed_data_witness_is_rejected(tmp_path: Path, forged: object) -> None:
    product = _product(tmp_path)
    baseline = {**regression.capture_baseline(product), "data_file_hashes": forged}
    path = tmp_path / "regression_baseline.json"
    path.write_text(json.dumps(baseline))
    loaded, check = regression.checked_baseline(path, product, regression.baseline_sha256(path))
    assert loaded is None and check is not None
    assert "data-file witness is invalid" in (check.details or "")
