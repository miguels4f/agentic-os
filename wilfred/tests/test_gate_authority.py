"""Deterministic checks cannot stand in for other workflow authorities."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyperwilfred_core import engine
from hyperwilfred_core.cli import main
from hyperwilfred_core.pipeline import steps_for
from hyperwilfred_core.schemas import BuildMode, BuildObjective, Stack, StepKind


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("surface", ["api", "cli"])
@pytest.mark.parametrize(
    ("mode", "step_id"),
    [(mode, step.id) for mode in BuildMode for step in steps_for(mode)
     if step.kind is not StepKind.GATE]
    + [(BuildMode.BUILD, "regression"), (BuildMode.IMPROVE, "evaluate"),
       (BuildMode.REVIEW, "evaluate"), (BuildMode.BUILD, "unknown")],
)
def test_evaluate_rejects_other_authorities_without_effects(
    tmp_path: Path, capsys, surface: str, mode: BuildMode, step_id: str,
) -> None:
    # A real executable sentinel catches evaluator execution as well as state/evidence writes.
    target = tmp_path / "product"
    target.mkdir()
    (target / "probe.cjs").write_text(
        "require('node:fs').writeFileSync('evaluator-ran', 'unexpected');\n", encoding="utf-8"
    )
    (target / "package.json").write_text(json.dumps({
        "scripts": {name: "node probe.cjs" for name in ("build", "lint", "test")},
    }), encoding="utf-8")
    engine.plan(
        repo_root=tmp_path, run_id="r", mode=mode,
        objective=BuildObjective(id="o", title="t", objective="guard", stack=Stack.NODE),
    )
    before = _snapshot(tmp_path)
    if surface == "api":
        with pytest.raises(engine.EngineError, match="mode-declared deterministic gate"):
            engine.evaluate(repo_root=tmp_path, run_id="r", target=target, step_id=step_id)
    else:
        assert main([
            "--repo", str(tmp_path), "evaluate", "--run-id", "r", "--step", step_id,
            "--target", str(target),
        ]) == 2
        error = json.loads(capsys.readouterr().out)
        assert error["kind"] == "engine_error"
        assert "hw next-step" in error["error"]
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("mode", [BuildMode.BUILD, BuildMode.IMPROVE])
@pytest.mark.parametrize("alteration", ["promote_ship", "demote_gate"])
def test_evaluate_requires_both_canonical_and_recorded_gate_kind(
    tmp_path: Path, mode: BuildMode, alteration: str,
) -> None:
    engine.plan(
        repo_root=tmp_path, run_id="r", mode=mode,
        objective=BuildObjective(id="o", title="t", objective="guard"),
    )
    paths = engine.RunPaths(tmp_path, "r")
    state = engine.load_state(paths)
    step_id = "evaluate" if mode is BuildMode.BUILD else "regression"
    if alteration == "promote_ship":
        step_id = "ship"
    step = next(s for s in state.steps if s.id == step_id)
    step.kind = StepKind.GATE if alteration == "promote_ship" else StepKind.SINGLE
    engine.save_state(paths, state)
    before = _snapshot(tmp_path)
    with pytest.raises(engine.EngineError, match="deterministic gate"):
        engine.evaluate(repo_root=tmp_path, run_id="r", target=tmp_path, step_id=step_id)
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("surface", ["api", "cli"])
def test_node_baseline_request_explains_native_checks_without_writes(
    tmp_path: Path, capsys, surface: str,
) -> None:
    engine.plan(
        repo_root=tmp_path, run_id="r", mode=BuildMode.IMPROVE,
        objective=BuildObjective(id="o", title="t", objective="guard", stack=Stack.NODE),
    )
    before = _snapshot(tmp_path)
    if surface == "api":
        with pytest.raises(engine.EngineError, match="npm run test"):
            engine.regression_baseline(repo_root=tmp_path, run_id="r", target=tmp_path)
    else:
        assert main([
            "--repo", str(tmp_path), "regression-baseline", "--run-id", "r",
            "--target", str(tmp_path),
        ]) == 2
        error = json.loads(capsys.readouterr().out)
        assert "only supported for Python" in error["error"]
        assert "npm run test" in error["error"]
    assert _snapshot(tmp_path) == before
