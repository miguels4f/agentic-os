"""G1 — human-declared environment-only known failures (``known_failures.py``).

Two halves:

* the HAPPY path proves the pilot run-3 dead-end is gone: a repo whose only red tests are
  pre-declared, pre-existing, untouched environment-only failures passes the IMPROVE regression
  gate with a visible WARNING instead of hard-failing forever;
* the ADVERSARIAL half proves the list cannot be used as a bypass: every way an agent inside the
  run could try to widen it (uncommitted edit, mid-run commit, wildcard, new test in a declared
  dir, edited declared test, a regression, a collection error, a worker write) still hard-blocks.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from hyperwilfred_core import engine, guardrails, known_failures
from hyperwilfred_core.evaluators.code.pytest_evaluator import _failing_findings
from hyperwilfred_core.evaluators.verification import CheckKind, CheckResult, Finding, Severity
from hyperwilfred_core.schemas import (
    DEFAULT_BLOCKED_GLOBS,
    BuildMode,
    BuildObjective,
    RunStatus,
    StepStatus,
)

ENV_TEST = "pkg/test_env.py::test_reads_laptop_only_file"
ENV_TEST_SRC = (
    "from pathlib import Path\n\n\n"
    "def test_reads_laptop_only_file() -> None:\n"
    "    assert Path('/nonexistent/only-on-owners-mac.json').exists()\n"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _declare(repo: Path, entries: list[dict[str, str]], *, commit: bool = True) -> None:
    (repo / known_failures.DECLARATION_FILE).write_text(
        json.dumps({"version": 1, "entries": entries}), encoding="utf-8"
    )
    if commit:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "declare known env-only failures")


DECLARED = [{"id": ENV_TEST, "reason": "needs a file that only exists on the owner's Mac"}]


@pytest.fixture
def env_repo(tmp_path: Path) -> Path:
    """A repo whose suite is green EXCEPT one test that needs a laptop-only file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(repo, "config", "core.hooksPath", str(hooks))
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "gc.auto", "0")
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "calc.py").write_text("def double(n: int) -> int:\n    return n * 2\n", encoding="utf-8")
    (pkg / "test_calc.py").write_text(
        "from calc import double\n\n\ndef test_double() -> None:\n    assert double(3) == 6\n",
        encoding="utf-8",
    )
    (pkg / "test_env.py").write_text(ENV_TEST_SRC, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "suite with one environment-only failure")
    return repo


def _toolchain_ready() -> bool:
    from hyperwilfred_core.evaluators import tool_on_path

    return all(tool_on_path(t) for t in ("ruff", "mypy", "pytest"))


needs_toolchain = pytest.mark.skipif(not _toolchain_ready(), reason="toolchain not on PATH")


def _plan_improve(repo: Path) -> engine.RunPaths:
    engine.plan(
        repo_root=repo, objective=BuildObjective(id="o", title="t", objective="extend calc"),
        run_id="i", product_dir="pkg", mode=BuildMode.IMPROVE,
    )
    return engine.RunPaths(repo, "i")


def _improve_to_regression(repo: Path) -> engine.RunPaths:
    paths = _plan_improve(repo)
    engine.regression_baseline(repo_root=repo, run_id="i", target=repo / "pkg")
    state = engine.load_state(paths)
    for sid in ("comprehend", "locate", "implement"):
        state.step_status[sid] = StepStatus.DONE
    engine.save_state(paths, state)
    return paths


def _pytest_check(result: dict) -> dict:
    return next(c for c in result["report"]["checks"] if c["name"] == "pytest")


# --------------------------------------------------------------------------- happy path


@needs_toolchain
def test_declared_env_only_failure_no_longer_sticks_the_regression_gate(env_repo: Path) -> None:
    _declare(env_repo, DECLARED)
    paths = _improve_to_regression(env_repo)
    (env_repo / "pkg" / "calc.py").write_text(
        "def double(n: int) -> int:\n    return n * 2\n\n\n"
        "def triple(n: int) -> int:\n    return n * 3\n",
        encoding="utf-8",
    )
    out = engine.evaluate(repo_root=env_repo, run_id="i", target=env_repo / "pkg",
                          step_id="regression")
    assert out["passed"] is True
    check = _pytest_check(out)
    assert check["passed"] is True and check["severity"] == "warning"  # visible, not silent
    assert ENV_TEST in check["details"] and "the owner's Mac" in check["details"]
    assert engine.load_state(paths).step_status["regression"] is StepStatus.DONE


@needs_toolchain
def test_plan_records_the_base_commit(env_repo: Path) -> None:
    head = subprocess.run(["git", "-C", str(env_repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    paths = _plan_improve(env_repo)
    assert engine.load_state(paths).base_commit == head


# --------------------------------------------------------------------------- adversarial


def _assert_gate_blocks(repo: Path, paths: engine.RunPaths) -> str:
    with pytest.raises(engine.GateFailure) as exc:
        engine.evaluate(repo_root=repo, run_id="i", target=repo / "pkg", step_id="regression")
    state = engine.load_state(paths)
    assert state.status is RunStatus.GATE_FAILED
    assert state.step_status["regression"] is StepStatus.FAILED
    return str(exc.value)


@needs_toolchain
def test_without_a_declaration_the_gate_still_hard_fails(env_repo: Path) -> None:
    paths = _plan_improve(env_repo)
    with pytest.raises(engine.EngineError, match="verified pre-run environment declaration"):
        engine.regression_baseline(repo_root=env_repo, run_id="i", target=env_repo / "pkg")
    assert not paths.artifact("regression_baseline.json").exists()


@needs_toolchain
def test_uncommitted_declaration_is_ignored(env_repo: Path) -> None:
    paths = _plan_improve(env_repo)
    _declare(env_repo, DECLARED, commit=False)  # written in the working tree, never committed
    with pytest.raises(engine.EngineError, match="verified pre-run environment declaration"):
        engine.regression_baseline(repo_root=env_repo, run_id="i", target=env_repo / "pkg")
    assert not paths.artifact("regression_baseline.json").exists()


@needs_toolchain
def test_declaration_committed_during_the_run_is_ignored(env_repo: Path) -> None:
    paths = _plan_improve(env_repo)  # base commit recorded here, with no declaration
    _declare(env_repo, DECLARED)  # a mid-run commit cannot widen what the run tolerates
    with pytest.raises(engine.EngineError, match="verified pre-run environment declaration"):
        engine.regression_baseline(repo_root=env_repo, run_id="i", target=env_repo / "pkg")
    assert not paths.artifact("regression_baseline.json").exists()


@needs_toolchain
def test_editing_a_declared_test_file_voids_its_exemption(env_repo: Path) -> None:
    _declare(env_repo, DECLARED)
    paths = _improve_to_regression(env_repo)
    (env_repo / "pkg" / "test_env.py").write_text(
        ENV_TEST_SRC + "\n\ndef test_extra() -> None:\n    pass\n", encoding="utf-8"
    )
    msg = _assert_gate_blocks(env_repo, paths)
    assert "new or modified" in msg


@needs_toolchain
def test_new_failing_test_in_a_declared_directory_is_not_excused(env_repo: Path) -> None:
    _declare(env_repo, [{"id": "pkg/", "reason": "whole dir needs the laptop-only fixture"}])
    paths = _improve_to_regression(env_repo)
    (env_repo / "pkg" / "test_new.py").write_text(
        "def test_new_behaviour() -> None:\n    assert 1 == 2\n", encoding="utf-8"
    )
    msg = _assert_gate_blocks(env_repo, paths)
    assert "pkg/test_new.py::test_new_behaviour" in msg


@needs_toolchain
def test_an_undeclared_failure_alongside_a_declared_one_blocks(env_repo: Path) -> None:
    _declare(env_repo, DECLARED)
    paths = _improve_to_regression(env_repo)
    (env_repo / "pkg" / "calc.py").write_text(
        # a different SIZE than the original, so a same-second stale .pyc can never mask it
        "def double(n: int) -> int:\n    return n * 2 + 1\n", encoding="utf-8"
    )  # a real regression of test_calc.py::test_double
    msg = _assert_gate_blocks(env_repo, paths)
    assert "not declared" in msg


@needs_toolchain
def test_declaring_a_baseline_passing_test_cannot_hide_its_regression(env_repo: Path) -> None:
    _declare(env_repo, DECLARED + [
        {"id": "pkg/test_calc.py::test_double", "reason": "trying to excuse a real test"},
    ])
    paths = _improve_to_regression(env_repo)
    (env_repo / "pkg" / "calc.py").write_text(
        # a different SIZE than the original, so a same-second stale .pyc can never mask it
        "def double(n: int) -> int:\n    return n * 2 + 1\n", encoding="utf-8"
    )
    msg = _assert_gate_blocks(env_repo, paths)
    assert "regression" in msg


@needs_toolchain
def test_a_collection_error_is_never_excused(env_repo: Path) -> None:
    _declare(env_repo, [{"id": "pkg/", "reason": "blanket attempt"}])
    paths = _improve_to_regression(env_repo)
    (env_repo / "pkg" / "test_calc.py").write_text("def broken(:\n", encoding="utf-8")
    _assert_gate_blocks(env_repo, paths)


@needs_toolchain
def test_an_invalid_declaration_excuses_nothing(env_repo: Path) -> None:
    _declare(env_repo, [{"id": "**", "reason": "tolerate everything"}])
    paths = _plan_improve(env_repo)
    with pytest.raises(engine.EngineError, match="declaration ignored"):
        engine.regression_baseline(repo_root=env_repo, run_id="i", target=env_repo / "pkg")
    assert not paths.artifact("regression_baseline.json").exists()


def test_a_worker_can_never_write_the_declaration(git_repo: Path) -> None:
    assert "**/.hw-known-failures.json" in DEFAULT_BLOCKED_GLOBS
    (git_repo / known_failures.DECLARATION_FILE).write_text("{}", encoding="utf-8")
    verdict = guardrails.accept(
        packet_id="p", worktree=str(git_repo), allowed_globs=["**"],
        blocked_globs=list(DEFAULT_BLOCKED_GLOBS), result_file=None,
        max_changed_lines=400, max_hunk_span=120, full_file_ratio=0.9,
    )
    assert not verdict.accepted and verdict.rolled_back
    assert known_failures.DECLARATION_FILE in verdict.escapes


def test_apply_never_touches_other_checks_or_non_error_failures() -> None:
    ruff = CheckResult(kind=CheckKind.LINT, name="ruff", passed=False, severity=Severity.ERROR)
    crit = CheckResult(kind=CheckKind.TEST, name="pytest", passed=False,
                       severity=Severity.CRITICAL)
    for check in (ruff, crit):
        out = known_failures.apply(check, repo_root=Path("."), target=Path("."),
                                   base_commit="deadbeef", baseline_passing=None)
        assert out is check


def test_unparsed_failures_are_never_excused(env_repo: Path) -> None:
    _declare(env_repo, DECLARED)
    base = known_failures.head_commit(env_repo)
    check = CheckResult(
        kind=CheckKind.TEST, name="pytest", passed=False, severity=Severity.ERROR,
        findings=[Finding(file="<pytest>", message="x", severity=Severity.ERROR,
                          rule_id=known_failures.RULE_UNPARSED)],
    )
    out = known_failures.apply(check, repo_root=env_repo, target=env_repo / "pkg",
                               base_commit=base, baseline_passing=None)
    assert out.passed is False and out.severity is Severity.ERROR


# --------------------------------------------------------------------------- selector rules


@pytest.mark.parametrize(
    "selector",
    ["*", "**", "**/*", "tests/*.py", "tests/test_x.py::*", "tests/test_x.py::",
     "../outside/test_x.py", "/abs/test_x.py", "tests", "tests/[ab]/test_x.py",
     "tests/dir::test_x"],
)
def test_broad_or_malformed_selectors_are_rejected(selector: str) -> None:
    with pytest.raises(ValidationError):
        known_failures.KnownFailure(id=selector, reason="r")


@pytest.mark.parametrize(
    "selector",
    ["tests/test_x.py::test_y", "tests/test_x.py::test_y[param-1]", "tests/test_x.py",
     "tests/live/", "tests/test_x.py::test_live_*"],
)
def test_narrow_selectors_are_accepted(selector: str) -> None:
    assert known_failures.KnownFailure(id=selector, reason="needs a laptop-only file").id


def test_reason_is_mandatory_and_the_list_is_capped() -> None:
    with pytest.raises(ValidationError):
        known_failures.KnownFailure(id="tests/test_x.py", reason="  ")
    many = [{"id": f"tests/test_{i}.py", "reason": "r"} for i in range(26)]
    with pytest.raises(ValidationError):
        known_failures.KnownFailuresDeclaration.model_validate({"version": 1, "entries": many})


@pytest.mark.parametrize(
    ("selector", "node_id", "expected"),
    [
        ("a/test_x.py::test_y", "a/test_x.py::test_y", True),
        ("a/test_x.py::test_y", "a/test_x.py::test_y2", False),
        ("a/test_x.py::test_y[p1]", "a/test_x.py::test_y[p1]", True),
        ("a/test_x.py::test_y[p1]", "a/test_x.py::test_y1", False),  # brackets are literal
        ("a/test_x.py", "a/test_x.py::anything", True),
        ("a/test_x.py", "a/test_x2.py::t", False),
        ("a/", "a/b/test_x.py::t", True),
        ("a/", "ab/test_x.py::t", False),
        ("a/test_x.py::test_live_*", "a/test_x.py::test_live_feed", True),
        ("a/test_x.py::test_live_*", "a/test_x.py::test_other", False),
    ],
)
def test_entry_matches(selector: str, node_id: str, expected: bool) -> None:
    assert known_failures.entry_matches(selector, node_id) is expected


# --------------------------------------------------------------------------- evaluator parsing


def test_pytest_evaluator_parses_failing_ids_and_reconciles_the_count() -> None:
    out = (
        "..F.E\n"
        "=========================== short test summary info ===========================\n"
        "FAILED pkg/test_a.py::test_x - AssertionError: 2 failed\n"
        "ERROR pkg/test_b.py::test_y - FileNotFoundError\n"
        "1 failed, 3 passed, 1 error in 0.12s\n"
    )
    findings = _failing_findings(out)
    assert [(f.file, f.rule_id) for f in findings] == [
        ("pkg/test_a.py::test_x", "pytest-failed"),
        ("pkg/test_b.py::test_y", "pytest-error"),
    ]


def test_pytest_evaluator_fails_closed_when_ids_do_not_reconcile() -> None:
    out = "FAILED pkg/test_a.py::test_x - boom\n3 failed, 1 passed in 0.1s\n"
    findings = _failing_findings(out)
    assert len(findings) == 1 and findings[0].rule_id == "pytest-unparsed"
