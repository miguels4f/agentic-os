"""The IMPROVE branch — net-new front (comprehend → locate → scoped-diff → regression guard),
sharing the same evaluator + gate + patcher core as BUILD.

The load-bearing claim: the regression guard HARD-BLOCKS when a pre-existing test breaks. It is
the deterministic hard wall for IMPROVE, and the quality loop treats it as such.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hyperwilfred_core import engine
from hyperwilfred_core.pipeline import steps_for
from hyperwilfred_core.schemas import BuildMode, BuildObjective, RunStatus, Stack, StepStatus


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_steps_for_improve_has_netnew_front_and_shared_core() -> None:
    ids = [s.id for s in steps_for(BuildMode.IMPROVE)]
    assert ids == ["comprehend", "locate", "implement", "regression", "quality", "verify", "ship"]
    # locate emits the same Plan/Slice contract as BUILD's decompose → fan-out is reused
    locate = next(s for s in steps_for(BuildMode.IMPROVE) if s.id == "locate")
    assert locate.produces == "plan.json"
    # regression is the deterministic GATE; quality is the shared loop
    kinds = {s.id: s.kind.value for s in steps_for(BuildMode.IMPROVE)}
    assert kinds["regression"] == "gate"
    assert kinds["quality"] == "quality"
    assert kinds["implement"] == "fanout"


def test_improve_plan_walks_the_improve_graph(tmp_path: Path) -> None:
    obj = BuildObjective(id="o", title="t", objective="add a flag to the parser")
    state = engine.plan(
        repo_root=tmp_path, objective=obj, run_id="i", product_dir="repo", mode=BuildMode.IMPROVE
    )
    assert state.mode is BuildMode.IMPROVE
    first = engine.next_step(repo_root=tmp_path, run_id="i")
    assert first["step"]["id"] == "comprehend"


@pytest.fixture
def improve_repo(tmp_path: Path) -> Path:
    """A small *working* repo with a green test suite — the IMPROVE starting point."""
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
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "calc.py").write_text("def double(n):\n    return n * 2\n", encoding="utf-8")
    (pkg / "test_calc.py").write_text(
        "from calc import double\n\n\ndef test_double():\n    assert double(3) == 6\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial green suite")
    return repo


def _toolchain_ready() -> bool:
    from hyperwilfred_core.evaluators import tool_on_path

    return all(tool_on_path(t) for t in ("ruff", "mypy", "pytest"))


def test_python_regression_requires_baseline_even_when_native_checks_pass(
    improve_repo: Path,
) -> None:
    if not _toolchain_ready():
        pytest.skip("toolchain not on PATH")
    engine.plan(
        repo_root=improve_repo, run_id="i", mode=BuildMode.IMPROVE,
        objective=BuildObjective(id="o", title="t", objective="change calc"),
    )
    with pytest.raises(engine.GateFailure, match="baseline is missing"):
        engine.evaluate(
            repo_root=improve_repo, run_id="i", target=improve_repo / "pkg", step_id="regression",
        )
    paths = engine.RunPaths(improve_repo, "i")
    report = json.loads(paths.artifact("verification.json").read_text())
    checks = {check["name"]: check for check in report["checks"]}
    assert all(checks[name]["passed"] for name in ("ruff", "mypy", "pytest"))
    assert checks["regression-baseline"]["passed"] is False
    assert engine.load_state(paths).status is RunStatus.GATE_FAILED


def test_regression_guard_passes_when_change_keeps_tests_green(improve_repo: Path) -> None:
    if not _toolchain_ready():
        pytest.skip("toolchain not on PATH")
    repo = improve_repo
    pkg = repo / "pkg"
    engine.plan(
        repo_root=repo, objective=BuildObjective(id="o", title="t", objective="extend calc"),
        run_id="i", product_dir="pkg", mode=BuildMode.IMPROVE,
    )
    engine.regression_baseline(repo_root=repo, run_id="i", target=pkg)
    paths = engine.RunPaths(repo, "i")
    state = engine.load_state(paths)
    # fast-forward to the regression gate (front steps proven elsewhere)
    for sid in ("comprehend", "locate", "implement"):
        state.step_status[sid] = StepStatus.DONE
    engine.save_state(paths, state)
    # a backwards-compatible change: existing test still holds
    (pkg / "calc.py").write_text(
        "def double(n):\n    return n * 2\n\n\ndef triple(n):\n    return n * 3\n",
        encoding="utf-8",
    )
    out = engine.evaluate(repo_root=repo, run_id="i", target=pkg, step_id="regression")
    assert out["passed"] is True
    state = engine.load_state(paths)
    assert state.step_status["regression"] is StepStatus.DONE


def test_regression_guard_hard_blocks_when_preexisting_test_breaks(improve_repo: Path) -> None:
    if not _toolchain_ready():
        pytest.skip("toolchain not on PATH")
    repo = improve_repo
    pkg = repo / "pkg"
    engine.plan(
        repo_root=repo, objective=BuildObjective(id="o", title="t", objective="change calc"),
        run_id="i", product_dir="pkg", mode=BuildMode.IMPROVE,
    )
    paths = engine.RunPaths(repo, "i")
    state = engine.load_state(paths)
    for sid in ("comprehend", "locate", "implement"):
        state.step_status[sid] = StepStatus.DONE
    engine.save_state(paths, state)
    # a REGRESSION: change double() so the pre-existing test (double(3)==6) now fails
    (pkg / "calc.py").write_text("def double(n):\n    return n * 3\n", encoding="utf-8")
    with pytest.raises(engine.GateFailure):
        engine.evaluate(repo_root=repo, run_id="i", target=pkg, step_id="regression")
    state = engine.load_state(paths)
    assert state.status is RunStatus.GATE_FAILED  # hard block, not "advance anyway"
    assert state.step_status["regression"] is StepStatus.FAILED


def _improve_run_with_baseline(repo: Path, pkg: Path) -> engine.RunPaths:
    """Plan an IMPROVE run, capture the green baseline, fast-forward to the regression gate."""
    engine.plan(
        repo_root=repo, objective=BuildObjective(id="o", title="t", objective="change calc"),
        run_id="i", product_dir="pkg", mode=BuildMode.IMPROVE,
    )
    engine.regression_baseline(repo_root=repo, run_id="i", target=pkg)  # snapshot green baseline
    paths = engine.RunPaths(repo, "i")
    state = engine.load_state(paths)
    for sid in ("comprehend", "locate", "implement"):
        state.step_status[sid] = StepStatus.DONE
    engine.save_state(paths, state)
    return paths


def test_regression_guard_blocks_when_preexisting_test_is_deleted(improve_repo: Path) -> None:
    # The verifier's vector A: delete the test so the suite is "green" (no tests) — must BLOCK.
    if not _toolchain_ready():
        pytest.skip("toolchain not on PATH")
    repo = improve_repo
    pkg = repo / "pkg"
    paths = _improve_run_with_baseline(repo, pkg)
    (pkg / "test_calc.py").unlink()  # remove the proving test; code can now drift unnoticed
    (pkg / "calc.py").write_text("def double(n):\n    return n * 99\n", encoding="utf-8")
    with pytest.raises(engine.GateFailure):
        engine.evaluate(repo_root=repo, run_id="i", target=pkg, step_id="regression")
    assert engine.load_state(paths).status is RunStatus.GATE_FAILED


def test_regression_guard_blocks_when_preexisting_test_is_skipped(improve_repo: Path) -> None:
    # The verifier's vector C: @pytest.mark.skip the proving test — suite "green" — must BLOCK.
    if not _toolchain_ready():
        pytest.skip("toolchain not on PATH")
    repo = improve_repo
    pkg = repo / "pkg"
    paths = _improve_run_with_baseline(repo, pkg)
    (pkg / "test_calc.py").write_text(
        "import pytest\nfrom calc import double\n\n\n"
        '@pytest.mark.skip(reason="neutralised")\n'
        "def test_double():\n    assert double(3) == 6\n",
        encoding="utf-8",
    )
    (pkg / "calc.py").write_text("def double(n):\n    return n * 99\n", encoding="utf-8")
    with pytest.raises(engine.GateFailure):
        engine.evaluate(repo_root=repo, run_id="i", target=pkg, step_id="regression")
    assert engine.load_state(paths).status is RunStatus.GATE_FAILED


def test_regression_guard_blocks_skip_plus_count_inflation(improve_repo: Path) -> None:
    # Re-verifier slip #1: skip the proving test AND add trivial passing tests so the aggregate
    # count stays up. A per-id passing-set check must still block (the baseline test stopped
    # passing); an aggregate count check would be fooled.
    if not _toolchain_ready():
        pytest.skip("toolchain not on PATH")
    repo = improve_repo
    pkg = repo / "pkg"
    paths = _improve_run_with_baseline(repo, pkg)
    (pkg / "test_calc.py").write_text(
        "import pytest\nfrom calc import double\n\n\n"
        '@pytest.mark.skip(reason="neutralised")\n'
        "def test_double():\n    assert double(3) == 6\n\n\n"
        "def test_pad_a():\n    assert True\n\n\ndef test_pad_b():\n    assert True\n",
        encoding="utf-8",
    )
    (pkg / "calc.py").write_text("def double(n):\n    return n * 99\n", encoding="utf-8")
    with pytest.raises(engine.GateFailure):
        engine.evaluate(repo_root=repo, run_id="i", target=pkg, step_id="regression")
    assert engine.load_state(paths).status is RunStatus.GATE_FAILED


def test_regression_guard_blocks_gutted_assertion(improve_repo: Path) -> None:
    # Re-verifier slip #3/#4: keep the test node-id collected+passing but gut its assertion so it
    # no longer proves anything, while the real code is broken. The test-file integrity check
    # (the file was modified) must block.
    if not _toolchain_ready():
        pytest.skip("toolchain not on PATH")
    repo = improve_repo
    pkg = repo / "pkg"
    paths = _improve_run_with_baseline(repo, pkg)
    (pkg / "test_calc.py").write_text(
        "from calc import double\n\n\ndef test_double():\n    double(3)\n    assert True\n",
        encoding="utf-8",
    )
    (pkg / "calc.py").write_text("def double(n):\n    return n * 99\n", encoding="utf-8")
    with pytest.raises(engine.GateFailure):
        engine.evaluate(repo_root=repo, run_id="i", target=pkg, step_id="regression")
    assert engine.load_state(paths).status is RunStatus.GATE_FAILED


def test_regression_baseline_passes_additive_change_with_baseline(improve_repo: Path) -> None:
    # A genuinely backwards-compatible change still passes WITH a baseline in place.
    if not _toolchain_ready():
        pytest.skip("toolchain not on PATH")
    repo = improve_repo
    pkg = repo / "pkg"
    _improve_run_with_baseline(repo, pkg)
    (pkg / "calc.py").write_text(
        "def double(n):\n    return n * 2\n\n\ndef triple(n):\n    return n * 3\n",
        encoding="utf-8",
    )
    out = engine.evaluate(repo_root=repo, run_id="i", target=pkg, step_id="regression")
    assert out["passed"] is True


@pytest.fixture
def native_node_repo(tmp_path: Path) -> Path:
    from hyperwilfred_core.evaluators import tool_on_path

    if not all(tool_on_path(tool) for tool in ("npm", "node")):
        pytest.skip("Node toolchain not on PATH")
    target = tmp_path / "node-product"
    target.mkdir()
    (target / "package.json").write_text(json.dumps({
        "scripts": {"build": "node --check calc.cjs", "lint": "node --check calc.test.cjs",
                    "test": "node --test calc.test.cjs"},
    }), encoding="utf-8")
    (target / "calc.cjs").write_text("exports.double = n => n * 2;\n", encoding="utf-8")
    (target / "calc.test.cjs").write_text(
        "const {test} = require('node:test');\n"
        "const assert = require('node:assert/strict');\n"
        "const {double} = require('./calc.cjs');\n"
        "test('double', () => assert.equal(double(3), 6));\n", encoding="utf-8",
    )
    return target


@pytest.mark.parametrize("mode", [BuildMode.BUILD, BuildMode.IMPROVE])
def test_native_node_gate_passes_without_python_baseline(
    tmp_path: Path, native_node_repo: Path, mode: BuildMode,
) -> None:
    engine.plan(
        repo_root=tmp_path, run_id="n", mode=mode,
        objective=BuildObjective(id="o", title="t", objective="calc", stack=Stack.NODE),
    )
    step_id = "evaluate" if mode is BuildMode.BUILD else "regression"
    result = engine.evaluate(
        repo_root=tmp_path, run_id="n", target=native_node_repo, step_id=step_id,
    )
    assert result["passed"] is True
    checks = result["report"]["checks"]
    assert [check["name"] for check in checks] == [
        "npm-build", "npm-lint", "npm-test", "flaky-tests", "ci-parity", "engine-state-untracked",
    ]
    assert all(check["passed"] for check in checks)
    assert all("passed" in check["details"] for check in checks[:3])
    paths = engine.RunPaths(tmp_path, "n")
    assert not paths.artifact("regression_baseline.json").exists()
    state = engine.load_state(paths)
    assert state.step_status[step_id] is StepStatus.DONE
    assert state.step_status["ship"] is StepStatus.PENDING


def test_native_node_regression_blocks_real_test_failure(
    tmp_path: Path, native_node_repo: Path,
) -> None:
    engine.plan(
        repo_root=tmp_path, run_id="n", mode=BuildMode.IMPROVE,
        objective=BuildObjective(id="o", title="t", objective="calc", stack=Stack.NODE),
    )
    (native_node_repo / "calc.cjs").write_text("exports.double = n => n * 3;\n", encoding="utf-8")
    with pytest.raises(engine.GateFailure, match="npm-test"):
        engine.evaluate(
            repo_root=tmp_path, run_id="n", target=native_node_repo, step_id="regression",
        )
    paths = engine.RunPaths(tmp_path, "n")
    checks = json.loads(paths.artifact("verification.json").read_text())["checks"]
    assert [(check["name"], check["passed"]) for check in checks] == [
        ("npm-build", True), ("npm-lint", True), ("npm-test", False),
        ("flaky-tests", True), ("ci-parity", True), ("engine-state-untracked", True),
    ]
    state = engine.load_state(paths)
    assert state.status is RunStatus.GATE_FAILED
    assert state.step_status["regression"] is StepStatus.FAILED
    assert engine.next_step(repo_root=tmp_path, run_id="n")["blocked"] is True
