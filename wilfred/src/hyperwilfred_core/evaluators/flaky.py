"""Flaky-test gate — a test the work added or changed must give the same answer every time.

A test that passes once proves little when it depends on timing, ordering or chance. Two such
tests reached ``main`` in wilfred in September 2026: a privacy assertion that searched a log for
``"10000"`` and matched a duration by chance, and a daemon test that raced its own poll loop
(2 failures in 8 runs). Both passed the pytest gate on the run that introduced them.

This gate re-runs every test file the run added or modified (``git diff`` against the run's
``base_commit`` plus untracked files, limited to the target) :data:`DEFAULT_RUNS` times,
shuffling the file order on each run, and compares the per-test outcomes. Every other run
happens under CPU contention (one busy process per core, sharing nothing with the tests): CI
runners are small, slow machines, and timing races that never show on an idle laptop show
there — the daemon race above passed 5 of 5 idle runs and failed under load.

Outcomes:

* a test that both passed and failed across runs is **non-deterministic** → ERROR, hard block;
* a test that failed in every run is left to the test gate (which already blocks on it) and is
  reported here as a WARNING, since it may be a test the suite's default selection excludes;
* everything consistent → pass.

Runners: pytest (per-test outcomes from ``--junitxml``), jest (``--json``) and vitest
(``--reporter=json``). Test files are grouped by their nearest project config
(``pyproject.toml``/``pytest.ini``/``setup.cfg``/``tox.ini`` or ``package.json``) and run from
that directory, as CI runs them. Files no supported runner can execute are reported, not
ignored.

Fail-closed rules: the run's ``base_commit`` is required, and a git error while listing the
changed tests (unknown base, broken repository) blocks instead of reading as "nothing changed".
The whole re-run is bounded by a time budget (``flaky_budget_s``, default
:data:`DEFAULT_BUDGET_S`); running out of it blocks, because determinism was not shown. No
environment variable switches the gate off: only a human can, with an ``off`` entry in
``.hw-gates.json`` committed before the run (see :mod:`hyperwilfred_core.gate_declaration`).
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any, ClassVar

from hyperwilfred_core import gate_declaration
from hyperwilfred_core.evaluators.base import BaseEvaluator, run_subprocess
from hyperwilfred_core.evaluators.verification import (
    CheckKind,
    CheckResult,
    Finding,
    Severity,
)

DEFAULT_RUNS = 5
MAX_TEST_FILES = 40
RUN_TIMEOUT_S: float = 300.0
DEFAULT_BUDGET_S: float = 1800.0

_PY_TEST = re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$")
_JS_TEST = re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$")
_PY_CONFIGS = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")


class ChangedTestsError(RuntimeError):
    """git could not say which tests changed — the gate must block, not assume "none"."""


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        env=gate_declaration.git_env(),
    )


def changed_test_files(target: Path, base_commit: str) -> tuple[Path, list[str]] | None:
    """``(git toplevel, test files added/modified since base_commit under target)``.

    ``None`` when *target* is not inside a git repository. Any git failure after that (unknown
    base commit, unreadable index) raises :class:`ChangedTestsError`.
    """
    top_proc = _git(target, "rev-parse", "--show-toplevel")
    if top_proc.returncode != 0 or not top_proc.stdout.strip():
        return None
    top = Path(top_proc.stdout.strip())
    if _git(top, "cat-file", "-e", f"{base_commit}^{{commit}}").returncode != 0:
        raise ChangedTestsError(f"base commit {base_commit[:12]} is not in {top.name}")
    diff = _git(top, "diff", "--name-only", "--diff-filter=AMR", base_commit, "--")
    others = _git(top, "ls-files", "--others", "--exclude-standard")
    for proc, what in ((diff, "git diff"), (others, "git ls-files")):
        if proc.returncode != 0:
            raise ChangedTestsError(f"{what} failed: {proc.stderr.strip()[:300]}")
    changed, untracked = diff.stdout.splitlines(), others.stdout.splitlines()
    try:
        prefix = target.resolve().relative_to(top.resolve()).as_posix()
    except ValueError as exc:
        raise ChangedTestsError(f"{target} is not inside {top}") from exc
    prefix = "" if prefix == "." else prefix + "/"
    files = sorted(
        {
            f.strip()
            for f in [*changed, *untracked]
            if f.strip()
            and f.startswith(prefix)
            and (_PY_TEST.search(f) or _JS_TEST.search(f))
            and (top / f.strip()).is_file()
        }
    )
    return top, files


def _project_dir(top: Path, rel: str, markers: tuple[str, ...]) -> Path:
    """Nearest ancestor of *rel* (inside *top*) holding one of *markers*; else *top*."""
    here = (top / rel).parent
    while True:
        if any((here / m).exists() for m in markers):
            return here
        if here == top or top not in here.parents:
            return top
        here = here.parent


def _node_runner(project: Path) -> str | None:
    try:
        pkg = json.loads((project / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    script = str((pkg.get("scripts") or {}).get("test", ""))
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    for runner in ("vitest", "jest"):
        if runner in script or runner in deps:
            return runner
    return None


# --------------------------------------------------------------------------- one run


def _pytest_run(project: Path, files: list[str], seed: int, timeout: float, env: dict[str, str]):
    with tempfile.TemporaryDirectory(prefix="hw-flaky-") as tmp:
        report = Path(tmp) / "junit.xml"
        argv = [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            f"--junitxml={report}", *files,
        ]  # fmt: skip
        outcome = run_subprocess(argv, cwd=project, timeout_s=timeout, env=env)
        results: dict[str, bool] = {}
        if report.exists():
            try:
                root = ET.parse(report).getroot()
            except ET.ParseError:
                root = None
            for case in root.iter("testcase") if root is not None else []:
                if case.find("skipped") is not None:
                    continue
                failed = case.find("failure") is not None or case.find("error") is not None
                results[f"{case.get('classname', '')}::{case.get('name', '')}"] = not failed
        return outcome, results


def _node_run(
    project: Path, runner: str, files: list[str], seed: int, timeout: float, env: dict[str, str]
):
    with tempfile.TemporaryDirectory(prefix="hw-flaky-") as tmp:
        report = Path(tmp) / "report.json"
        if runner == "jest":
            argv = [
                "npx", "--no-install", "jest", "--json", f"--outputFile={report}",
                "--runInBand", "--randomize", f"--seed={seed}", *files,
            ]  # fmt: skip
        else:
            argv = [
                "npx", "--no-install", "vitest", "run", "--reporter=json",
                f"--outputFile={report}", "--sequence.shuffle", f"--sequence.seed={seed}",
                *files,
            ]  # fmt: skip
        outcome = run_subprocess(argv, cwd=project, timeout_s=timeout, env=env)
        results: dict[str, bool] = {}
        try:
            data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
        except (OSError, json.JSONDecodeError):
            data = {}
        for suite in data.get("testResults") or []:
            name = str(suite.get("name", ""))
            for case in suite.get("assertionResults") or []:
                status = case.get("status")
                if status in ("passed", "failed"):
                    key = f"{name}::{case.get('fullName') or case.get('title')}"
                    results[key] = status == "passed"
        return outcome, results


# --------------------------------------------------------------------------- the gate


class _CpuLoad:
    """One busy-looping process per core while active — perturbs timing, touches nothing."""

    def __init__(self) -> None:
        self.procs: list[subprocess.Popen[bytes]] = []

    def __enter__(self) -> _CpuLoad:
        for _ in range(os.cpu_count() or 2):
            self.procs.append(
                subprocess.Popen(  # noqa: S603 — fixed argv
                    [sys.executable, "-c", "while True: pass"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        return self

    def __exit__(self, *exc: object) -> None:
        for proc in self.procs:
            proc.kill()
        for proc in self.procs:
            proc.wait()


def check_determinism(
    top: Path,
    files: list[str],
    *,
    runs: int = DEFAULT_RUNS,
    timeout_s: float = RUN_TIMEOUT_S,
    budget_s: float = DEFAULT_BUDGET_S,
) -> tuple[list[Finding], list[Finding], int]:
    """Run *files* ``runs`` times within *budget_s*; return ``(errors, warnings, observed)``."""
    deadline = time.monotonic() + budget_s
    errors: list[Finding] = []
    warnings: list[Finding] = []
    groups: dict[tuple[Path, str], list[str]] = defaultdict(list)
    for rel in files:
        if _PY_TEST.search(rel):
            project = _project_dir(top, rel, _PY_CONFIGS)
            groups[(project, "pytest")].append(rel)
            continue
        project = _project_dir(top, rel, ("package.json",))
        runner = _node_runner(project)
        if runner is None:
            warnings.append(
                Finding(
                    file=rel,
                    message="no jest/vitest runner found for this test — determinism NOT checked",
                    severity=Severity.WARNING,
                    rule_id="flaky-unsupported",
                )
            )
            continue
        groups[(project, runner)].append(rel)

    env = dict(os.environ)
    observed = 0
    for (project, runner), rels in groups.items():
        local = [(top / r).relative_to(project).as_posix() for r in rels]
        outcomes: dict[str, list[bool]] = defaultdict(list)
        crashed = 0
        for n in range(runs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                errors.append(
                    Finding(
                        file=str(project.relative_to(top)) or ".",
                        message=f"time budget of {budget_s:.0f}s used up before {runner} run "
                        f"{n + 1}/{runs} — determinism NOT shown",
                        severity=Severity.ERROR,
                        rule_id="flaky-budget",
                    )
                )
                return errors, warnings, observed + len(outcomes)
            seed = 1000 + n
            order = list(local)
            random.Random(seed).shuffle(order)
            with _CpuLoad() if n % 2 == 1 else contextlib.nullcontext():
                step_timeout = min(timeout_s, remaining)
                if runner == "pytest":
                    outcome, results = _pytest_run(project, order, seed, step_timeout, env)
                else:
                    outcome, results = _node_run(project, runner, order, seed, step_timeout, env)
            if not results:
                crashed += 1
            for test_id, ok in results.items():
                outcomes[test_id].append(ok)
            if outcome.timed_out:
                warnings.append(
                    Finding(
                        file=str(project.relative_to(top)) or ".",
                        message=f"{runner} run {n + 1}/{runs} timed out after "
                        f"{step_timeout:.0f}s",
                        severity=Severity.WARNING,
                        rule_id="flaky-timeout",
                    )
                )
        if crashed == runs:
            warnings.append(
                Finding(
                    file=", ".join(rels[:3]) + ("…" if len(rels) > 3 else ""),
                    message=f"{runner} produced no per-test results in any run "
                    "(collection error or runner missing) — determinism NOT checked",
                    severity=Severity.WARNING,
                    rule_id="flaky-no-results",
                )
            )
        elif 0 < crashed:
            errors.append(
                Finding(
                    file=", ".join(rels[:3]),
                    message=f"{runner} produced results in only {runs - crashed}/{runs} runs "
                    "— the test session itself is non-deterministic",
                    severity=Severity.ERROR,
                    rule_id="flaky-session",
                )
            )
        observed += len(outcomes)
        for test_id, seen in sorted(outcomes.items()):
            passes = sum(seen)
            if 0 < passes < len(seen):
                errors.append(
                    Finding(
                        file=test_id.split("::", 1)[0],
                        message=f"{test_id}: passed {passes} of {len(seen)} runs — "
                        "non-deterministic (timing, ordering, shared state or randomness)",
                        severity=Severity.ERROR,
                        rule_id="flaky-test",
                    )
                )
            elif passes == 0 and len(seen) == runs:
                warnings.append(
                    Finding(
                        file=test_id.split("::", 1)[0],
                        message=f"{test_id}: failed in all {runs} runs here "
                        "(left to the test gate; check the environment if it passes there)",
                        severity=Severity.WARNING,
                        rule_id="flaky-always-fails",
                    )
                )
    return errors, warnings, observed


class FlakyTestEvaluator(BaseEvaluator):
    """Re-run the tests the work touched and block on any that do not answer consistently."""

    kind: ClassVar[CheckKind] = CheckKind.TEST
    name: ClassVar[str] = "flaky-tests"
    binary: ClassVar[str] = "git"
    default_timeout_s: ClassVar[float] = RUN_TIMEOUT_S

    def run(self, target_path: Path, *, config: dict[str, Any] | None = None) -> CheckResult:
        def _info(details: str, severity: Severity = Severity.INFO) -> CheckResult:
            return CheckResult(
                kind=self.kind, name=self.name, passed=True, severity=severity, details=details
            )

        def _block(details: str) -> CheckResult:
            return CheckResult(
                kind=self.kind, name=self.name, passed=False, severity=Severity.ERROR,
                details=details,
            )  # fmt: skip

        started = time.monotonic()
        cfg = config or {}
        if not target_path.exists():
            return _block(f"flaky-tests: target {target_path} does not exist")
        top, git_error = gate_declaration.git_top(target_path)
        base_commit = cfg.get("base_commit")
        if git_error:
            return _block(f"flaky-tests: git failed on the target ({git_error}) — blocked")
        if top is None and base_commit:
            return _block(
                "flaky-tests: the run started from a git repository but the target is no longer "
                "inside one — blocked"
            )
        if top is None:
            return _info(
                "flaky-tests: target is not inside a git repository — NOT checked",
                Severity.WARNING,
            )
        if not base_commit:
            return _block(
                "flaky-tests: the run has no base commit, so the tests it changed cannot be "
                "listed — blocked (commit the repository before `hw plan`)"
            )
        switch, switch_error = gate_declaration.read_switch(top, str(base_commit), self.name)
        if switch is not None and switch.mode == "off":
            return _info(f"flaky-tests: turned off by {switch.note()}", Severity.WARNING)
        try:
            found = changed_test_files(target_path, str(base_commit))
        except ChangedTestsError as exc:
            return _block(f"flaky-tests: cannot list the changed tests ({exc}) — blocked")
        if found is None:
            return _block("flaky-tests: target left the git repository during the check")
        top, files = found
        if not files:
            note = f" (declaration ignored — {switch_error})" if switch_error else ""
            return _info(f"flaky-tests: no test files added or changed — nothing to re-run{note}")
        dropped = len(files) - MAX_TEST_FILES
        files = files[:MAX_TEST_FILES]
        try:
            runs = max(2, int(cfg.get("flaky_runs") or DEFAULT_RUNS))
        except (TypeError, ValueError):
            runs = DEFAULT_RUNS
        try:
            budget = float(cfg.get("flaky_budget_s") or DEFAULT_BUDGET_S)
        except (TypeError, ValueError):
            budget = DEFAULT_BUDGET_S

        errors, warnings, observed = check_determinism(
            top, files, runs=runs, timeout_s=self._resolve_timeout(config), budget_s=budget
        )
        if switch_error:
            warnings.append(
                Finding(
                    file=gate_declaration.DECLARATION_FILE,
                    message=f"declaration ignored — {switch_error}",
                    severity=Severity.WARNING,
                    rule_id="flaky-declaration",
                )
            )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if dropped > 0:
            warnings.append(
                Finding(
                    file=".",
                    message=f"{dropped} more changed test file(s) beyond {MAX_TEST_FILES} "
                    "were NOT re-run",
                    severity=Severity.WARNING,
                    rule_id="flaky-truncated",
                )
            )
        summary = (
            f"flaky-tests: {len(files)} changed test file(s), {observed} test(s), "
            f"{runs} runs each (shuffled order, every other run under CPU load)"
        )
        findings = [*errors, *warnings]
        if errors:
            return CheckResult(
                kind=self.kind,
                name=self.name,
                passed=False,
                severity=Severity.ERROR,
                details=f"{summary}; {len(errors)} blocking finding(s). First: {errors[0].message}",
                duration_ms=elapsed_ms,
                findings=findings,
            )
        if warnings:
            return CheckResult(
                kind=self.kind,
                name=self.name,
                passed=True,
                severity=Severity.WARNING,
                details=f"{summary}; deterministic, {len(warnings)} note(s) — see findings",
                duration_ms=elapsed_ms,
                findings=findings,
            )
        result = _info(f"{summary}; all deterministic")
        return result.model_copy(update={"duration_ms": elapsed_ms})


__all__ = ["ChangedTestsError", "FlakyTestEvaluator", "changed_test_files", "check_determinism"]
