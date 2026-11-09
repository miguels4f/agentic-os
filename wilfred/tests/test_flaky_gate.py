"""Flaky-test gate: tests the work touched must answer the same way on every run.

Regression for two September-2026 wilfred tests that passed once and flaked on main: a log
search that matched a duration by chance, and a daemon test racing its own poll loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyperwilfred_core.evaluators.flaky import (
    ChangedTestsError,
    FlakyTestEvaluator,
    changed_test_files,
)
from hyperwilfred_core.evaluators.verification import Severity

DETERMINISTIC = "def test_adds():\n    assert 1 + 1 == 2\n"
ALWAYS_FAILS = "def test_broken():\n    assert False\n"


def _alternating(counter: Path) -> str:
    """A test that fails on every second run — a stand-in for a timing race."""
    return (
        "from pathlib import Path\n"
        f"COUNTER = Path({str(counter)!r})\n"
        "def test_races():\n"
        "    n = int(COUNTER.read_text()) if COUNTER.exists() else 0\n"
        "    COUNTER.write_text(str(n + 1))\n"
        "    assert n % 2 == 0\n"
    )


@pytest.fixture
def py_project(git_repo: Path, git_helper) -> tuple[Path, str]:
    pkg = git_repo / "pkg"
    (pkg / "tests").mkdir(parents=True)
    (pkg / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    (pkg / "tests" / "test_existing.py").write_text(DETERMINISTIC, encoding="utf-8")
    git_helper(git_repo, "add", "-A")
    git_helper(git_repo, "commit", "-q", "-m", "project")
    base = git_helper(git_repo, "rev-parse", "HEAD").strip()
    return git_repo, base


def _run(repo: Path, base: str, runs: int = 5):
    return FlakyTestEvaluator().run(repo, config={"base_commit": base, "flaky_runs": runs})


def test_only_tests_changed_since_base_are_selected(py_project, tmp_path: Path) -> None:
    repo, base = py_project
    (repo / "pkg" / "tests" / "test_new.py").write_text(DETERMINISTIC, encoding="utf-8")
    (repo / "pkg" / "helper.py").write_text("X = 1\n", encoding="utf-8")
    (repo / "web").mkdir()
    (repo / "web" / "app.test.ts").write_text("", encoding="utf-8")
    top, files = changed_test_files(repo, base)  # type: ignore[misc]
    assert files == ["pkg/tests/test_new.py", "web/app.test.ts"]
    assert "pkg/tests/test_existing.py" not in files
    # restricted to the target directory
    assert changed_test_files(repo / "pkg", base)[1] == ["pkg/tests/test_new.py"]  # type: ignore[index]


def test_deterministic_new_test_passes(py_project) -> None:
    repo, base = py_project
    (repo / "pkg" / "tests" / "test_new.py").write_text(DETERMINISTIC, encoding="utf-8")
    result = _run(repo, base)
    assert result.passed is True
    assert result.severity is Severity.INFO
    assert "1 test(s)" in (result.details or "")


def test_non_deterministic_test_blocks(py_project, tmp_path: Path) -> None:
    repo, base = py_project
    counter = tmp_path / "counter"
    (repo / "pkg" / "tests" / "test_race.py").write_text(_alternating(counter), encoding="utf-8")
    result = _run(repo, base)
    assert result.passed is False
    assert result.severity is Severity.ERROR
    assert "passed 3 of 5 runs" in (result.details or "")
    assert int(counter.read_text()) == 5


def test_modified_committed_test_is_rechecked(py_project, git_helper, tmp_path: Path) -> None:
    repo, base = py_project
    counter = tmp_path / "counter"
    existing = repo / "pkg" / "tests" / "test_existing.py"
    existing.write_text(_alternating(counter), encoding="utf-8")
    git_helper(repo, "commit", "-q", "-am", "make it racy")
    assert _run(repo, base).passed is False


def test_untouched_flaky_test_is_not_this_runs_problem(
    py_project, git_helper, tmp_path: Path
) -> None:
    repo, _ = py_project
    (repo / "pkg" / "tests" / "test_old_race.py").write_text(
        _alternating(tmp_path / "counter"), encoding="utf-8"
    )
    git_helper(repo, "add", "-A")
    git_helper(repo, "commit", "-q", "-m", "pre-existing")
    new_base = git_helper(repo, "rev-parse", "HEAD").strip()
    result = _run(repo, new_base)
    assert result.passed is True
    assert "no test files added or changed" in (result.details or "")


def test_consistent_failure_is_left_to_the_test_gate(py_project) -> None:
    repo, base = py_project
    (repo / "pkg" / "tests" / "test_broken.py").write_text(ALWAYS_FAILS, encoding="utf-8")
    result = _run(repo, base, runs=3)
    assert result.passed is True
    assert result.severity is Severity.WARNING
    assert any(f.rule_id == "flaky-always-fails" for f in result.findings)


def test_uncollectable_file_is_reported(py_project) -> None:
    repo, base = py_project
    (repo / "pkg" / "tests" / "test_syntax.py").write_text("def broken(:\n", encoding="utf-8")
    result = _run(repo, base, runs=2)
    assert result.passed is True
    assert result.severity is Severity.WARNING


def test_js_test_without_runner_is_reported_not_ignored(py_project) -> None:
    repo, base = py_project
    (repo / "web").mkdir()
    (repo / "web" / "package.json").write_text('{"name": "web"}', encoding="utf-8")
    (repo / "web" / "app.test.ts").write_text("", encoding="utf-8")
    result = _run(repo, base)
    assert result.passed is True
    assert any(f.rule_id == "flaky-unsupported" for f in result.findings)


def test_outside_git_is_a_visible_warning_not_a_silent_pass(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    result = FlakyTestEvaluator().run(outside)
    assert result.passed is True
    assert result.severity is Severity.WARNING
    # inside a run that started from a repository, a target outside any repository blocks
    assert FlakyTestEvaluator().run(outside, config={"base_commit": "0" * 40}).passed is False


def test_a_broken_repository_blocks(py_project) -> None:
    repo, base = py_project
    (repo / ".git" / "HEAD").write_text("garbage\n")
    result = _run(repo, base)
    assert result.passed is False
    assert "git failed" in (result.details or "") or "cannot list" in (result.details or "")


def test_environment_variables_cannot_switch_the_gate_off(
    py_project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = py_project
    (repo / "pkg" / "tests" / "test_race.py").write_text(
        _alternating(tmp_path / "counter"), encoding="utf-8"
    )
    for name in ("HW_FLAKY_GATE", "HW_FLAKY_GATE_ACTIVE"):
        monkeypatch.setenv(name, "off")
    assert _run(repo, base).passed is False


def test_missing_base_commit_blocks(py_project) -> None:
    repo, _ = py_project
    for config in ({}, {"base_commit": None}):
        result = FlakyTestEvaluator().run(repo, config=config)
        assert result.passed is False
        assert "no base commit" in (result.details or "")


def test_unknown_base_commit_blocks_instead_of_reading_as_no_changes(py_project) -> None:
    repo, _ = py_project
    with pytest.raises(ChangedTestsError):
        changed_test_files(repo, "f" * 40)
    result = _run(repo, "f" * 40)
    assert result.passed is False
    assert "cannot list the changed tests" in (result.details or "")


def _declare(repo: Path, git_helper, gates: dict) -> str:
    import json

    (repo / ".hw-gates.json").write_text(json.dumps({"version": 1, "gates": gates}))
    git_helper(repo, "add", ".hw-gates.json")
    git_helper(repo, "commit", "-q", "-m", "gates")
    return git_helper(repo, "rev-parse", "HEAD").strip()


def test_off_committed_before_the_run_is_honoured_and_visible(
    py_project, git_helper, tmp_path: Path
) -> None:
    repo, _ = py_project
    base = _declare(repo, git_helper, {"flaky-tests": {"mode": "off", "reason": "lab only"}})
    (repo / "pkg" / "tests" / "test_race.py").write_text(
        _alternating(tmp_path / "counter"), encoding="utf-8"
    )
    result = _run(repo, base)
    assert result.passed is True
    assert result.severity is Severity.WARNING
    assert "turned off by .hw-gates.json" in (result.details or "")
    assert "lab only" in (result.details or "")


def test_off_written_during_the_run_has_no_effect(
    py_project, git_helper, tmp_path: Path
) -> None:
    repo, base = py_project
    _declare(repo, git_helper, {"flaky-tests": {"mode": "off", "reason": "worker says so"}})
    (repo / "pkg" / "tests" / "test_race.py").write_text(
        _alternating(tmp_path / "counter"), encoding="utf-8"
    )
    assert _run(repo, base).passed is False


def test_used_up_time_budget_blocks(py_project) -> None:
    repo, base = py_project
    (repo / "pkg" / "tests" / "test_new.py").write_text(DETERMINISTIC, encoding="utf-8")
    result = FlakyTestEvaluator().run(
        repo, config={"base_commit": base, "flaky_runs": 3, "flaky_budget_s": 1e-9}
    )
    assert result.passed is False
    assert any(f.rule_id == "flaky-budget" for f in result.findings)


def test_a_target_that_stops_being_a_repository_during_a_run_blocks(py_project) -> None:
    import shutil

    repo, base = py_project
    shutil.rmtree(repo / ".git")
    result = _run(repo, base)
    assert result.passed is False
