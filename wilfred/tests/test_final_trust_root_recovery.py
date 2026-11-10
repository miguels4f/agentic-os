"""Exact deterministic target and evaluated-byte integration recovery."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from hyperwilfred_core import engine, quality, regression
from hyperwilfred_core.cli import main
from hyperwilfred_core.evaluators.verification import (
    CheckKind,
    CheckResult,
    Severity,
    VerificationReport,
)
from hyperwilfred_core.schemas import (
    BuildMode,
    BuildObjective,
    Stack,
    StepStatus,
)


def _objective(*, stack: Stack = Stack.PYTHON) -> BuildObjective:
    return BuildObjective(id="o", title="Trust root", objective="Verify exact bytes", stack=stack)


def _report(*, passed: bool = True) -> VerificationReport:
    return VerificationReport(
        task_id="o",
        run_id="r",
        completed_at="2026-09-24T00:00:00Z",
        checks=[CheckResult(
            kind=CheckKind.TEST,
            name="fixture",
            passed=passed,
            severity=Severity.INFO if passed else Severity.ERROR,
            details=None if passed else "failure outside the decoy subtree",
        )],
        blocks_merge=not passed,
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _git_base(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.email", "fixture@example.test"), ("user.name", "Fixture")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)


def test_explicit_root_rejects_passing_decoy_and_checks_declared_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "test_ok.py").write_text("def test_ok(): assert True\n")
    (tmp_path / "test_failure_outside.py").write_text("def test_bad(): assert False\n")
    engine.plan(
        repo_root=tmp_path, run_id="r", product_dir=".", objective=_objective(),
    )
    calls: list[Path] = []

    def checks(target: Path, *_args, **_kwargs) -> VerificationReport:
        calls.append(target)
        return _report(passed=False)

    monkeypatch.setattr(engine, "run_all", checks)
    monkeypatch.setattr(quality, "product_snapshot_sha256", lambda *_: "a" * 64)
    before = _snapshot(tmp_path / ".hw")
    with pytest.raises(engine.EngineError, match="exactly match"):
        engine.evaluate(repo_root=tmp_path, run_id="r", target=decoy)
    assert _snapshot(tmp_path / ".hw") == before
    assert calls == []

    with pytest.raises(engine.GateFailure, match="failure outside"):
        engine.evaluate(repo_root=tmp_path, run_id="r", target=tmp_path)
    assert calls == [tmp_path.resolve()]


@pytest.mark.parametrize("surface", ["api", "cli"])
@pytest.mark.parametrize("location", ["descendant", "sibling", "external"])
def test_explicit_target_rejections_have_no_api_or_cli_effects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], surface: str, location: str,
) -> None:
    product = tmp_path / "product"
    product.mkdir()
    (product / "child").mkdir()
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    targets = {
        "descendant": product / "child",
        "sibling": sibling,
        "external": tmp_path.parent,
    }
    engine.plan(
        repo_root=tmp_path, run_id="r", product_dir="product", objective=_objective(),
    )
    before = _snapshot(tmp_path)
    if surface == "api":
        with pytest.raises(engine.EngineError):
            engine.evaluate(repo_root=tmp_path, run_id="r", target=targets[location])
    else:
        assert main([
            "--repo", str(tmp_path), "evaluate", "--run-id", "r",
            "--target", str(targets[location]),
        ]) == 2
        assert json.loads(capsys.readouterr().out)["kind"] == "engine_error"
    assert _snapshot(tmp_path) == before


def test_explicit_placeholder_is_not_treated_as_legacy(tmp_path: Path) -> None:
    declared = tmp_path / "products/r"
    child = declared / "decoy"
    child.mkdir(parents=True)
    engine.plan(
        repo_root=tmp_path, run_id="r", product_dir="products/r", objective=_objective(),
    )
    paths = engine.RunPaths(tmp_path, "r")
    assert engine.load_state(paths).deterministic_target == "products/r"
    before = _snapshot(tmp_path / ".hw")
    with pytest.raises(engine.EngineError, match="pinned target"):
        engine.evaluate(repo_root=tmp_path, run_id="r", target=child)
    assert _snapshot(tmp_path / ".hw") == before


@pytest.mark.parametrize("surface", ["api", "cli"])
def test_legacy_evaluate_pins_once_only_after_verified_precheck_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], surface: str,
) -> None:
    actual, other = tmp_path / "actual", tmp_path / "other"
    actual.mkdir()
    other.mkdir()
    engine.plan(repo_root=tmp_path, run_id="r", objective=_objective())
    snapshots: list[str] = []

    def digest(*_args) -> str:
        snapshots.append("pre" if not snapshots else "post")
        return "1" * 64

    monkeypatch.setattr(quality, "product_snapshot_sha256", digest)
    monkeypatch.setattr(engine, "run_all", lambda *_a, **_k: _report())
    argv = ["--repo", str(tmp_path), "evaluate", "--run-id", "r", "--target", str(actual)]
    if surface == "api":
        assert engine.evaluate(repo_root=tmp_path, run_id="r", target=actual)["passed"]
    else:
        assert main(argv) == 0
        capsys.readouterr()
    paths = engine.RunPaths(tmp_path, "r")
    state = engine.load_state(paths)
    assert snapshots == ["pre", "post"]
    assert state.deterministic_target == "actual"
    assert paths.artifact("deterministic-scope.sha256").read_text().strip() == "1" * 64

    before = _snapshot(tmp_path / ".hw")
    if surface == "api":
        with pytest.raises(engine.EngineError, match="pinned target"):
            engine.evaluate(repo_root=tmp_path, run_id="r", target=other)
    else:
        argv[-1] = str(other)
        assert main(argv) == 2
        capsys.readouterr()
    assert _snapshot(tmp_path / ".hw") == before


def test_regression_runs_before_post_snapshot_and_mutation_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = tmp_path / "product.py"
    product.write_text("VALUE = 1\n")
    _git_base(tmp_path)
    engine.plan(
        repo_root=tmp_path, run_id="r", product_dir=".", mode=BuildMode.IMPROVE,
        objective=_objective(),
    )
    paths = engine.RunPaths(tmp_path, "r")
    paths.artifact(regression.BASELINE_FILE).write_text("{}")
    order: list[str] = []

    def snapshot(*_args) -> str:
        order.append("snapshot")
        return hashlib.sha256(product.read_bytes()).hexdigest()

    def checks(*_args, **_kwargs) -> VerificationReport:
        order.append("checks")
        return _report()

    def compare(*_args, **_kwargs) -> CheckResult:
        order.append("regression")
        product.write_text("VALUE = 2\n")
        return _report().checks[0]

    monkeypatch.setattr(quality, "product_snapshot_sha256", snapshot)
    monkeypatch.setattr(engine, "run_all", checks)
    monkeypatch.setattr(regression, "checked_baseline", lambda *_: ({"passing_ids": []}, None))
    monkeypatch.setattr(regression, "verified_semantic_transitions", lambda *_a, **_k: [])
    monkeypatch.setattr(regression, "compare", compare)
    monkeypatch.setattr(engine.known_failures, "apply", lambda check, **_kwargs: check)

    with pytest.raises(engine.GateFailure, match="bytes changed"):
        engine.evaluate(
            repo_root=tmp_path, run_id="r", target=tmp_path, step_id="regression",
        )
    state = engine.load_state(paths)
    assert order == ["snapshot", "checks", "regression", "snapshot"]
    assert state.step_status["regression"] is StepStatus.FAILED
    assert not paths.artifact("deterministic-scope.sha256").exists()
    report = json.loads(paths.artifact("verification.json").read_text())
    assert report["checks"][-1]["name"] == "product-snapshot-stability"


@pytest.mark.parametrize("surface", ["api", "cli"])
def test_undeclared_improve_baseline_never_pins_a_caller_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], surface: str,
) -> None:
    actual, other = tmp_path / "actual", tmp_path / "other"
    actual.mkdir()
    other.mkdir()
    engine.plan(
        repo_root=tmp_path, run_id="r", mode=BuildMode.IMPROVE, objective=_objective(),
    )
    monkeypatch.setattr(
        regression, "baseline_candidate", lambda _target: pytest.fail("pytest ran"),
    )
    argv = [
        "--repo", str(tmp_path), "regression-baseline", "--run-id", "r",
        "--target", str(actual),
    ]
    paths = engine.RunPaths(tmp_path, "r")
    before = _snapshot(tmp_path / ".hw")
    for target in (actual, other):
        if surface == "api":
            with pytest.raises(engine.EngineError, match="declared at plan time"):
                engine.regression_baseline(repo_root=tmp_path, run_id="r", target=target)
        else:
            argv[-1] = str(target)
            assert main(argv) == 2
            assert "declared at plan time" in capsys.readouterr().out
    assert engine.load_state(paths).deterministic_target is None
    assert _snapshot(tmp_path / ".hw") == before
