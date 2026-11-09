"""CI-parity gate: the target repo's own pull-request CI, run locally before the pull request.

Each failure mode here is one that reached GitHub in September 2026 because the per-stack chain
never saw it: a lockfile path that did not exist, a repo-specific checker script, a job pinned
to another OS.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from hyperwilfred_core.evaluators.ci_parity import (
    NESTED_ENV,
    CiParityEvaluator,
    Unresolvable,
    evaluate_condition,
    evaluate_expression,
    expand_matrix,
    interpolate,
    job_workspace,
    run_parity,
)
from hyperwilfred_core.evaluators.verification import Severity

LOCAL_RUNNER = {"linux": "ubuntu-latest", "darwin": "macos-latest"}.get(
    sys.platform, "ubuntu-latest"
)
OTHER_RUNNER = "macos-latest" if LOCAL_RUNNER == "ubuntu-latest" else "ubuntu-latest"


def _workflow(repo: Path, name: str, body: str) -> None:
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / name).write_text(
        textwrap.dedent(body).replace("LOCAL_RUNNER", LOCAL_RUNNER), encoding="utf-8"
    )


def _messages(findings) -> str:
    return "\n".join(f.message for f in findings)


@pytest.fixture(autouse=True)
def _not_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    # The suite itself may run under a CI-parity gate; tests must exercise the real path.
    monkeypatch.delenv(NESTED_ENV, raising=False)


# --------------------------------------------------------------------------- expressions

CTX = {
    "github": {"event_name": "pull_request", "ref": "refs/pull/0/merge", "base_ref": "main"},
    "matrix": {"project": "frontend", "os": "ubuntu-latest"},
    "env": {"DIR": "CTO/MVP"},
}


def test_guardrails_style_if_is_true_on_a_pull_request() -> None:
    expr = "github.event_name == 'pull_request' || github.ref != 'refs/heads/main'"
    assert evaluate_expression(expr, CTX) is True
    assert evaluate_condition("${{ github.event_name == 'push' }}", CTX) is False
    assert evaluate_condition("!(github.base_ref == 'main')", CTX) is False


def test_functions_and_literals() -> None:
    assert evaluate_expression("contains(matrix.os, 'UBUNTU')", CTX) is True
    assert evaluate_expression("startsWith(github.ref, 'refs/pull/')", CTX) is True
    assert evaluate_expression("format('{0}/{1}', env.DIR, matrix.project)", CTX) == (
        "CTO/MVP/frontend"
    )
    assert evaluate_condition("always()", CTX) is True
    assert evaluate_condition("failure()", CTX) is False
    assert evaluate_expression("matrix['project'] == 'frontend' && true", CTX) is True


def test_interpolation() -> None:
    assert (
        interpolate("${{ env.DIR }}/${{ matrix.project }}/package-lock.json", CTX)
        == "CTO/MVP/frontend/package-lock.json"
    )


@pytest.mark.parametrize(
    "expr", ["secrets.NPM_TOKEN", "steps.build.outputs.dir", "hashFiles('**/x')", "vars.X"]
)
def test_unknowable_values_are_unresolvable(expr: str) -> None:
    with pytest.raises(Unresolvable):
        evaluate_expression(expr, CTX)


def test_matrix_expansion_with_include_and_exclude() -> None:
    combos = expand_matrix(
        {
            "matrix": {
                "project": ["backend", "frontend"],
                "node": [18, 20],
                "exclude": [{"project": "backend", "node": 18}],
                "include": [{"project": "frontend", "extra": "yes"}, {"project": "docs"}],
            }
        }
    )
    assert {"project": "backend", "node": 18} not in combos
    assert {"project": "frontend", "node": 20, "extra": "yes"} in combos
    assert {"project": "docs"} in combos
    assert len(combos) == 4
    assert expand_matrix(None) == [{}]


# --------------------------------------------------------------------------- execution
#
# Steps run in a throwaway checkout, so tests observe them through marker files written to an
# absolute directory outside the repository.


@pytest.fixture
def marks(tmp_path: Path) -> Path:
    d = tmp_path / "marks"
    d.mkdir()
    return d


def test_passing_pull_request_ci(git_repo: Path) -> None:
    (git_repo / "backend").mkdir()
    (git_repo / "backend" / "package-lock.json").write_text("{}", encoding="utf-8")
    _workflow(
        git_repo,
        "ci.yml",
        """
        name: CI
        on: [pull_request]
        env:
          ROOT_DIR: backend
        jobs:
          test:
            runs-on: LOCAL_RUNNER
            strategy:
              matrix:
                project: [backend]
            steps:
              - uses: actions/checkout@v4
              - uses: actions/cache@v4
                with:
                  cache-dependency-path: ${{ matrix.project }}/package-lock.json
              - name: export
                run: echo "GREETING=hello" >> "$GITHUB_ENV"
              - name: check
                working-directory: ${{ env.ROOT_DIR }}
                run: |
                  test "$GREETING" = hello
                  test "$CI" = true
                  test -f package-lock.json
        """,
    )
    report = run_parity(git_repo)
    assert report.errors == [], _messages(report.errors)
    assert report.warnings == [], _messages(report.warnings)
    assert report.executed == 2


def test_missing_lockfile_path_blocks(git_repo: Path) -> None:
    """app: setup-node cached on CTO/MVP/frontend/package-lock.json, which did not exist."""
    _workflow(
        git_repo,
        "guardrails.yml",
        """
        on:
          pull_request:
        jobs:
          lint:
            runs-on: LOCAL_RUNNER
            strategy:
              matrix:
                project: [frontend]
            steps:
              - uses: actions/cache@v4
                with:
                  cache-dependency-path: app/${{ matrix.project }}/package-lock.json
        """,
    )
    report = run_parity(git_repo)
    assert len(report.errors) == 1
    assert "app/frontend/package-lock.json" in report.errors[0].message


def test_failing_repo_check_blocks_and_stops_the_job(git_repo: Path, marks: Path) -> None:
    """app: a repo-specific checker script that only CI ran."""
    _workflow(
        git_repo,
        "guardrails.yml",
        f"""
        on: pull_request
        jobs:
          scope:
            runs-on: LOCAL_RUNNER
            steps:
              - name: Task Contract scope
                run: |
                  echo "FAIL Changed file is explicitly denied: .github/workflows/deploy.yml"
                  exit 1
              - name: never reached
                run: touch {marks}/reached
              - name: upload logs
                if: always()
                run: touch {marks}/always-ran
              - name: report failure
                if: failure()
                run: touch {marks}/failure-ran
          other:
            runs-on: LOCAL_RUNNER
            steps:
              - run: touch {marks}/other-ran
        """,
    )
    report = run_parity(git_repo)
    assert len(report.errors) == 1
    assert "Task Contract scope" in report.errors[0].message
    assert "explicitly denied" in report.errors[0].message
    assert not (marks / "reached").exists()
    assert (marks / "always-ran").exists()
    assert (marks / "failure-ran").exists()
    assert (marks / "other-ran").exists()  # other jobs still run, as on GitHub


def test_needs_follows_github_semantics(git_repo: Path, marks: Path) -> None:
    _workflow(
        git_repo,
        "ci.yml",
        f"""
        on: pull_request
        jobs:
          after-failure:
            needs: build
            runs-on: LOCAL_RUNNER
            steps:
              - run: touch {marks}/after-failure
          cleanup:
            needs: build
            if: always()
            runs-on: LOCAL_RUNNER
            steps:
              - run: touch {marks}/cleanup
          notify:
            needs: build
            if: failure()
            runs-on: LOCAL_RUNNER
            steps:
              - run: touch {marks}/notify
          build:
            runs-on: LOCAL_RUNNER
            steps:
              - run: exit 3
          optional:
            if: github.event_name == 'push'
            runs-on: LOCAL_RUNNER
            steps:
              - run: exit 1
          after-skip:
            needs: optional
            runs-on: LOCAL_RUNNER
            steps:
              - run: exit 1
          other-os:
            runs-on: {OTHER_RUNNER}
            steps:
              - run: exit 1
          after-unverified:
            needs: other-os
            runs-on: LOCAL_RUNNER
            steps:
              - run: exit 1
        """,
    )
    report = run_parity(git_repo)
    assert len(report.errors) == 1, _messages(report.errors)  # only build itself
    assert not (marks / "after-failure").exists()
    assert (marks / "cleanup").exists()
    assert (marks / "notify").exists()
    warnings = _messages(report.warnings)
    assert "after-failure: skipped because a needed job failed" in warnings
    assert "after-unverified: skipped because a needed job was skipped or NOT verified" in warnings
    assert "after-skip" in warnings


def test_each_job_gets_a_fresh_checkout_and_the_worktree_is_untouched(
    git_repo: Path, git_helper
) -> None:
    (git_repo / "README.md").write_text("# edited, not committed\n", encoding="utf-8")
    (git_repo / "new_file.txt").write_text("untracked work\n", encoding="utf-8")
    (git_repo / "node_modules").mkdir()
    (git_repo / "node_modules" / "cached.js").write_text("", encoding="utf-8")
    (git_repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    _workflow(
        git_repo,
        "ci.yml",
        """
        on: pull_request
        jobs:
          a:
            runs-on: LOCAL_RUNNER
            steps:
              - run: |
                  grep -q "edited, not committed" README.md
                  test -f new_file.txt
                  test ! -e node_modules
                  touch leaked.txt
                  echo "overwritten" > src/app.py
          b:
            needs: a
            runs-on: LOCAL_RUNNER
            steps:
              - run: |
                  test ! -e leaked.txt
                  grep -q "return a + b" src/app.py
        """,
    )
    before = git_helper(git_repo, "status", "--porcelain")
    report = run_parity(git_repo)
    assert report.errors == [], _messages(report.errors)
    assert report.executed == 2
    assert git_helper(git_repo, "status", "--porcelain") == before
    assert not (git_repo / "leaked.txt").exists()
    assert "return a + b" in (git_repo / "src" / "app.py").read_text(encoding="utf-8")
    assert git_helper(git_repo, "worktree", "list").count("\n") == 1


def test_steps_do_not_see_host_credentials(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "do-not-leak")
    monkeypatch.setenv("MY_TOOL_HOME", "/opt/tool")
    monkeypatch.setenv("HW_CI_PARITY_ENV_PASS", "MY_TOOL_HOME")
    _workflow(
        git_repo,
        "ci.yml",
        """
        on: pull_request
        env:
          FROM_WORKFLOW: from-workflow
        jobs:
          t:
            runs-on: LOCAL_RUNNER
            steps:
              - run: |
                  test -z "${AWS_SECRET_ACCESS_KEY:-}"
                  test -z "${ANTHROPIC_API_KEY:-}"
                  test "$MY_TOOL_HOME" = /opt/tool
                  test "$FROM_WORKFLOW" = from-workflow
                  test -n "$PATH" && test -n "$HOME"
        """,
    )
    report = run_parity(git_repo)
    assert report.errors == [], _messages(report.errors)
    assert report.executed == 1


def test_missing_or_unknown_toolchain_makes_the_job_unverified(
    git_repo: Path, marks: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hyperwilfred_core.evaluators.ci_parity as cp

    real_which = cp.shutil.which
    monkeypatch.setattr(cp.shutil, "which", lambda b: None if b == "pnpm" else real_which(b))
    _workflow(
        git_repo,
        "ci.yml",
        f"""
        on: pull_request
        jobs:
          node:
            runs-on: LOCAL_RUNNER
            steps:
              - uses: pnpm/action-setup@v4
              - run: pnpm test
          haskell:
            runs-on: LOCAL_RUNNER
            steps:
              - uses: haskell-actions/setup@v2
              - run: touch {marks}/haskell-ran
        """,
    )
    report = run_parity(git_repo)
    assert report.errors == []
    warnings = _messages(report.warnings)
    assert "'pnpm', which is not installed here" in warnings
    assert "haskell-actions/setup@v2" in warnings
    assert not (marks / "haskell-ran").exists()


def test_toolchain_version_mismatch_is_not_conclusive(git_repo: Path) -> None:
    if sys.platform.startswith("win"):
        pytest.skip("bash workflow steps")
    _workflow(
        git_repo,
        "ci.yml",
        """
        on: pull_request
        jobs:
          py:
            runs-on: LOCAL_RUNNER
            steps:
              - uses: actions/setup-python@v5
                with:
                  python-version: "2.7"
              - run: exit 1
        """,
    )
    report = run_parity(git_repo)
    assert report.errors == []
    assert "asks for python3 2.7" in _messages(report.warnings)
    assert "NOT conclusive" in _messages(report.warnings)


def test_push_only_workflows_are_never_executed(git_repo: Path, marks: Path) -> None:
    _workflow(
        git_repo,
        "deploy.yml",
        f"""
        on:
          push:
            branches: [main]
        jobs:
          deploy:
            runs-on: LOCAL_RUNNER
            steps:
              - run: touch {marks}/deployed
              - working-directory: infra/missing
                run: terraform apply
        """,
    )
    report = run_parity(git_repo)
    assert not (marks / "deployed").exists()
    assert report.executed == 0
    assert report.errors == []
    assert "infra/missing" in _messages(report.warnings)


def test_unreproducible_parts_are_reported_not_passed_silently(git_repo: Path) -> None:
    """wilfred-product: the quality job ran on macOS only."""
    _workflow(
        git_repo,
        "ci.yml",
        f"""
        on: pull_request
        jobs:
          other-os:
            runs-on: {OTHER_RUNNER}
            steps:
              - run: exit 1
          needs-secret:
            runs-on: LOCAL_RUNNER
            steps:
              - run: echo "${{{{ secrets.TOKEN }}}}"
              - uses: gitleaks/gitleaks-action@v2
          db:
            runs-on: LOCAL_RUNNER
            services:
              postgres:
                image: postgres:16
            steps:
              - run: exit 1
          only-on-push:
            if: github.event_name == 'push'
            runs-on: LOCAL_RUNNER
            steps:
              - run: exit 1
        """,
    )
    report = run_parity(git_repo)
    assert report.errors == []
    warnings = _messages(report.warnings)
    assert "NOT verified locally" in warnings  # other OS + services
    assert "secrets" in warnings
    assert "gitleaks/gitleaks-action@v2" in warnings
    assert "only-on-push" not in warnings  # correctly evaluated as not triggered


def test_continue_on_error_literal_and_expression(git_repo: Path, marks: Path) -> None:
    _workflow(
        git_repo,
        "ci.yml",
        f"""
        on: pull_request
        jobs:
          lint:
            runs-on: LOCAL_RUNNER
            steps:
              - run: exit 1
                continue-on-error: true
              - run: touch {marks}/after
          experimental:
            runs-on: LOCAL_RUNNER
            continue-on-error: ${{{{ matrix.experimental }}}}
            strategy:
              matrix:
                experimental: [true]
            steps:
              - run: exit 1
          step-expr:
            runs-on: LOCAL_RUNNER
            strategy:
              matrix:
                allow: [true]
            steps:
              - run: exit 1
                continue-on-error: ${{{{ matrix.allow }}}}
              - run: touch {marks}/step-expr-after
        """,
    )
    report = run_parity(git_repo)
    assert report.errors == [], _messages(report.errors)
    assert (marks / "after").exists()
    assert (marks / "step-expr-after").exists()
    assert "continue-on-error" in _messages(report.warnings)


def test_invalid_workflow_yaml_blocks(git_repo: Path) -> None:
    _workflow(git_repo, "broken.yml", "on: [pull_request\njobs: {")
    report = run_parity(git_repo)
    assert len(report.errors) == 1


# --------------------------------------------------------------------------- evaluator


def test_evaluator_maps_the_report_to_a_check(git_repo: Path) -> None:
    _workflow(
        git_repo,
        "ci.yml",
        """
        on: pull_request
        jobs:
          t:
            runs-on: LOCAL_RUNNER
            steps:
              - run: exit 1
        """,
    )
    result = CiParityEvaluator().run(git_repo / "src")
    assert result.passed is False
    assert result.severity is Severity.ERROR
    assert result.findings


def test_evaluator_blocks_when_no_pull_request_job_could_run(git_repo: Path) -> None:
    _workflow(
        git_repo,
        "ci.yml",
        f"""
        on: pull_request
        jobs:
          t:
            runs-on: {OTHER_RUNNER}
            steps:
              - run: exit 1
        """,
    )
    result = CiParityEvaluator().run(git_repo)
    assert result.passed is False
    assert result.severity is Severity.ERROR
    assert any(f.rule_id == "ci-parity-none" for f in result.findings)


def test_evaluator_warns_but_passes_when_only_some_jobs_are_unverified(git_repo: Path) -> None:
    _workflow(
        git_repo,
        "ci.yml",
        f"""
        on: pull_request
        jobs:
          other:
            runs-on: {OTHER_RUNNER}
            steps:
              - run: exit 1
          here:
            runs-on: LOCAL_RUNNER
            steps:
              - run: "true"
        """,
    )
    result = CiParityEvaluator().run(git_repo)
    assert result.passed is True
    assert result.severity is Severity.WARNING
    assert "NOT verified locally" in (result.details or "")


def test_evaluator_is_a_no_op_without_git_or_workflows(tmp_path: Path, git_repo: Path) -> None:
    assert CiParityEvaluator().run(tmp_path).passed is True
    assert CiParityEvaluator().run(git_repo).passed is True


def _commit(repo: Path, git_helper) -> str:
    git_helper(repo, "add", "-A")
    git_helper(repo, "commit", "-q", "-m", "base")
    return git_helper(repo, "rev-parse", "HEAD").strip()


FAILING = """
on: pull_request
jobs:
  t:
    runs-on: LOCAL_RUNNER
    steps:
      - run: exit 1
"""


def test_environment_variables_cannot_switch_the_gate_off(
    git_repo: Path, git_helper, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workflow(git_repo, "ci.yml", FAILING)
    base = _commit(git_repo, git_helper)
    for name, value in (("HW_CI_PARITY", "off"), (NESTED_ENV, "1"), (NESTED_ENV, str(git_repo))):
        monkeypatch.setenv(name, value)
        assert CiParityEvaluator().run(git_repo, config={"base_commit": base}).passed is False
        monkeypatch.delenv(name)


def test_nested_run_inside_a_real_job_clone_is_not_repeated(
    git_repo: Path, git_helper, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workflow(git_repo, "ci.yml", FAILING)
    _commit(git_repo, git_helper)
    with job_workspace(git_repo) as ws:
        monkeypatch.setenv(NESTED_ENV, str(ws))
        result = CiParityEvaluator().run(ws)
        assert result.passed is True
        assert result.severity is Severity.WARNING
        assert "nested" in (result.details or "")
        # the marker does not cover a target outside that clone
        assert CiParityEvaluator().run(git_repo).passed is False


def _declare(repo: Path, gates: dict) -> None:
    (repo / ".hw-gates.json").write_text(json.dumps({"version": 1, "gates": gates}))


def test_off_committed_before_the_run_is_honoured_and_visible(
    git_repo: Path, git_helper
) -> None:
    _workflow(git_repo, "ci.yml", FAILING)
    _declare(git_repo, {"ci-parity": {"mode": "off", "reason": "CI needs the lab"}})
    base = _commit(git_repo, git_helper)
    result = CiParityEvaluator().run(git_repo, config={"base_commit": base})
    assert result.passed is True
    assert result.severity is Severity.WARNING
    assert "CI needs the lab" in (result.details or "")


def test_declaration_written_during_the_run_has_no_effect(git_repo: Path, git_helper) -> None:
    _workflow(git_repo, "ci.yml", FAILING)
    base = _commit(git_repo, git_helper)
    _declare(git_repo, {"ci-parity": {"mode": "off", "reason": "worker says so"}})
    _commit(git_repo, git_helper)
    assert CiParityEvaluator().run(git_repo, config={"base_commit": base}).passed is False


def test_allow_unverified_turns_nothing_ran_into_a_warning(git_repo: Path, git_helper) -> None:
    _workflow(
        git_repo,
        "ci.yml",
        f"""
        on: pull_request
        jobs:
          t:
            runs-on: {OTHER_RUNNER}
            steps:
              - run: exit 1
        """,
    )
    _declare(git_repo, {"ci-parity": {"mode": "allow-unverified", "reason": "Mac only"}})
    base = _commit(git_repo, git_helper)
    result = CiParityEvaluator().run(git_repo, config={"base_commit": base})
    assert result.passed is True
    assert result.severity is Severity.WARNING
    assert "Mac only" in _messages(result.findings)


def test_the_run_cannot_delete_or_weaken_its_own_ci(git_repo: Path, git_helper) -> None:
    _workflow(git_repo, "ci.yml", FAILING)
    base = _commit(git_repo, git_helper)
    (git_repo / ".github" / "workflows" / "ci.yml").write_text(
        FAILING.replace("exit 1", "'true'").replace("LOCAL_RUNNER", LOCAL_RUNNER)
    )
    weakened = CiParityEvaluator().run(git_repo, config={"base_commit": base})
    assert weakened.passed is False
    assert any(f.rule_id == "ci-parity-workflow-changed" for f in weakened.findings)
    (git_repo / ".github" / "workflows" / "ci.yml").unlink()
    _commit(git_repo, git_helper)
    assert CiParityEvaluator().run(git_repo, config={"base_commit": base}).passed is False


def test_missing_or_unknown_base_commit_blocks(git_repo: Path) -> None:
    _workflow(git_repo, "ci.yml", FAILING.replace("exit 1", "'true'"))
    for base in (None, "f" * 40):
        assert CiParityEvaluator().run(git_repo, config={"base_commit": base}).passed is False


def test_steps_get_a_throwaway_home_and_a_clone_without_remote(
    git_repo: Path, git_helper
) -> None:
    git_helper(git_repo, "remote", "add", "origin", "https://example.invalid/repo.git")
    real_home = os.environ.get("HOME", "")
    _workflow(
        git_repo,
        "ci.yml",
        f"""
        on: pull_request
        jobs:
          t:
            runs-on: LOCAL_RUNNER
            steps:
              - run: test "$HOME" != "{real_home}"
              - run: test -z "$(git config --global --list 2>/dev/null)"
              - run: test -z "$(git remote)"
              - run: git rev-parse --verify --quiet "$GITHUB_BASE_REF"
              - run: test "$GIT_TERMINAL_PROMPT" = 0
        """,
    )
    base = _commit(git_repo, git_helper)
    report = run_parity(git_repo, base_commit=base)
    assert report.errors == [], _messages(report.errors)
    assert report.executed == 5


def test_used_up_time_budget_blocks(git_repo: Path, git_helper) -> None:
    _workflow(git_repo, "ci.yml", FAILING.replace("exit 1", "'true'"))
    base = _commit(git_repo, git_helper)
    result = CiParityEvaluator().run(
        git_repo, config={"base_commit": base, "ci_parity_budget_s": 1e-9}
    )
    assert result.passed is False
    assert any(f.rule_id == "ci-parity-budget" for f in result.findings)


def test_boolean_env_values_read_like_on_the_runner(git_repo: Path) -> None:
    _workflow(
        git_repo,
        "ci.yml",
        """
        on: pull_request
        env:
          FLAG: yes
          DISABLED: false
        jobs:
          t:
            runs-on: LOCAL_RUNNER
            steps:
              - run: test "$FLAG" = true && test "$DISABLED" = false
        """,
    )
    assert run_parity(git_repo).errors == []


def test_declared_environment_failures_excuse_only_the_same_ci_pytest_failures(
    git_repo: Path,
) -> None:
    from hyperwilfred_core.engine import _excuse_ci_parity_pytest
    from hyperwilfred_core.evaluators.ci_parity import PYTEST_IDS_MARKER
    from hyperwilfred_core.evaluators.verification import CheckKind, CheckResult, Finding

    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_env.py").write_text("def test_lab(): pass\n")
    (git_repo / "tests" / "test_other.py").write_text("def test_real(): pass\n")

    def parity(*findings: tuple[str, str]) -> CheckResult:
        return CheckResult(
            kind=CheckKind.CONSISTENCY, name="ci-parity", passed=False, severity=Severity.ERROR,
            findings=[
                Finding(file="ci.yml", message=m, severity=Severity.ERROR, rule_id=r)
                for r, m in findings
            ],
        )  # fmt: skip

    def step(*ids: str) -> tuple[str, str]:
        message = f"t / test: failed\n{PYTEST_IDS_MARKER}.] " + " ".join(ids)
        return ("ci-parity-pytest-failed", message)

    excused = CheckResult(
        kind=CheckKind.TEST, name="pytest", passed=True, severity=Severity.WARNING,
        details="pytest: 1 failing test(s) tolerated as human-declared environment-only failures",
        findings=[
            Finding(file="tests/test_env.py::test_lab", message="x", severity=Severity.ERROR,
                    rule_id="pytest-failed"),
        ],
    )  # fmt: skip
    not_excused = excused.model_copy(update={"passed": False, "severity": Severity.ERROR})

    def verdict(pytest_check: CheckResult, ci: CheckResult) -> bool:
        return _excuse_ci_parity_pytest([pytest_check, ci], git_repo)[1].passed

    assert verdict(excused, parity(step("tests/test_env.py::test_lab"))) is True
    # a different failing test, an extra failing step, an unparsed pytest failure: still blocks
    assert verdict(excused, parity(step("tests/test_other.py::test_real"))) is False
    assert verdict(
        excused,
        parity(step("tests/test_env.py::test_lab"), ("ci-parity-step-failed", "mypy failed")),
    ) is False
    assert verdict(excused, parity(("ci-parity-pytest-failed", "failed, no ids"))) is False
    assert verdict(not_excused, parity(step("tests/test_env.py::test_lab"))) is False


def test_only_a_plain_pytest_command_is_a_pytest_step() -> None:
    from hyperwilfred_core.evaluators.ci_parity import is_pytest_step

    oks = ("pytest", "python -m pytest -q tests/", "uv run pytest -q", "uv run --frozen pytest")
    for ok in oks:
        assert is_pytest_step(ok), ok
    for bad in ("pip install pytest && mypy .", "pytest; ruff check .", "pytest | tee log",
                "echo pytest", "pytest $(cat args)", "pytest tests\n./scripts/check.sh",
                "pytest -x", "pytest -qx", "pytest --maxfail=1", "pytest --cov-fail-under=90",
                "uv run pytest --lf"):  # fmt: skip
        assert not is_pytest_step(bad), bad


def test_a_failing_pytest_step_records_its_failing_ids(git_repo: Path) -> None:
    from hyperwilfred_core.evaluators.ci_parity import pytest_failing_ids

    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_bad.py").write_text("def test_no():\n    assert False\n")
    command = "python -m pytest -q -p no:cacheprovider tests"
    _workflow(git_repo, "ci.yml", FAILING.replace("exit 1", command))
    report = run_parity(git_repo)
    assert [f.rule_id for f in report.errors] == ["ci-parity-pytest-failed"]
    assert pytest_failing_ids(report.errors[0]) == ["tests/test_bad.py::test_no"]
    _workflow(git_repo, "ci.yml", FAILING.replace("exit 1", "python -m pytest -q tests && true"))
    assert [f.rule_id for f in run_parity(git_repo).errors] == ["ci-parity-step-failed"]


def test_env_pass_cannot_bring_back_the_real_home(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_home = os.environ.get("HOME", "")
    monkeypatch.setenv("HW_CI_PARITY_ENV_PASS", "HOME,GIT_SSH_COMMAND,SSH_AUTH_SOCK")
    _workflow(git_repo, "ci.yml", FAILING.replace("exit 1", f'test "$HOME" != "{real_home}"'))
    assert run_parity(git_repo).errors == []


def test_a_job_whose_if_cannot_be_evaluated_counts_as_not_verified(git_repo: Path) -> None:
    _workflow(
        git_repo,
        "ci.yml",
        """
        on: pull_request
        jobs:
          t:
            runs-on: LOCAL_RUNNER
            if: ${{ secrets.TOKEN != '' }}
            steps:
              - run: exit 1
        """,
    )
    result = CiParityEvaluator().run(git_repo)
    assert result.passed is False
    assert any(f.rule_id == "ci-parity-none" for f in result.findings)


def test_a_target_that_stops_being_a_repository_during_a_run_blocks(
    git_repo: Path, git_helper, tmp_path: Path
) -> None:
    import shutil

    _workflow(git_repo, "ci.yml", FAILING)
    base = _commit(git_repo, git_helper)
    shutil.rmtree(git_repo / ".git")
    assert CiParityEvaluator().run(git_repo, config={"base_commit": base}).passed is False


def test_reads_of_the_base_ignore_git_replace_refs(git_repo: Path, git_helper) -> None:
    _workflow(git_repo, "ci.yml", FAILING)
    base = _commit(git_repo, git_helper)
    # forge a replacement tree for the base commit whose CI always passes
    good = FAILING.replace("exit 1", "'true'").replace("LOCAL_RUNNER", LOCAL_RUNNER)
    (git_repo / ".github" / "workflows" / "ci.yml").write_text(good)
    forged = _commit(git_repo, git_helper)
    git_helper(git_repo, "replace", "-f", base, forged)
    git_helper(git_repo, "reset", "-q", "--hard", forged)
    result = CiParityEvaluator().run(git_repo, config={"base_commit": base})
    assert result.passed is False


def test_spoofed_failing_ids_are_not_trusted(git_repo: Path) -> None:
    """Code under test that fakes a FAILED line for a tolerated test cannot get its step excused."""
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_env.py").write_text("def test_lab():\n    pass\n")
    (git_repo / "tests" / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    print('\\nFAILED tests/test_env.py::test_lab')\n"
        "    session.exitstatus = 1\n"
    )
    command = "python -m pytest -q -p no:cacheprovider tests"
    _workflow(git_repo, "ci.yml", FAILING.replace("exit 1", command))
    report = run_parity(git_repo)
    assert [f.rule_id for f in report.errors] == ["ci-parity-step-failed"]
