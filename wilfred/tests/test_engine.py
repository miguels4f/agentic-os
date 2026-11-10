"""Engine orchestration — plan, step sequencing, fanout, acceptance, gate hard-block."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyperwilfred_core import engine
from hyperwilfred_core.schemas import (
    BuildObjective,
    EstimatedComplexity,
    Plan,
    RiskLevel,
    Slice,
    Tier,
)


def _objective(**kw) -> BuildObjective:
    base = dict(id="obj-1", title="Landing page", objective="build a landing page")
    base.update(kw)
    return BuildObjective(**base)


def test_plan_creates_run_state_and_artifacts(tmp_path: Path) -> None:
    state = engine.plan(repo_root=tmp_path, objective=_objective(), run_id="r1")
    assert state.run_id == "r1"
    paths = engine.RunPaths(tmp_path, "r1")
    assert paths.state.exists()
    assert paths.guardrails.exists()
    assert (paths.run_dir / "objective.json").exists()
    assert [s.id for s in state.steps][0] == "frame"


def test_tier_router_is_deterministic() -> None:
    assert engine.classify_tier(_objective(risk_level=RiskLevel.CRITICAL)) is Tier.FULL
    big = _objective(estimated_complexity=EstimatedComplexity.XL)
    assert engine.classify_tier(big) is Tier.FULL
    assert engine.classify_tier(_objective(objective="x " * 50)) is Tier.FULL
    assert engine.classify_tier(_objective()) is Tier.LIGHT


def test_next_step_advances_through_singles(tmp_path: Path) -> None:
    engine.plan(repo_root=tmp_path, objective=_objective(), run_id="r2")
    paths = engine.RunPaths(tmp_path, "r2")

    first = engine.next_step(repo_root=tmp_path, run_id="r2")
    assert first["step"]["id"] == "frame"

    # adapter writes the declared artifact, then completes the step
    paths.artifact("frame.md").write_text("framed\n", encoding="utf-8")
    out = engine.complete_step(repo_root=tmp_path, run_id="r2", step_id="frame")
    assert out["completed_step"] == "frame"
    assert out["next"]["step"]["id"] == "decompose"


def test_complete_step_requires_declared_artifact(tmp_path: Path) -> None:
    engine.plan(repo_root=tmp_path, objective=_objective(), run_id="r3")
    engine.next_step(repo_root=tmp_path, run_id="r3")
    with pytest.raises(engine.EngineError, match="frame.md"):
        engine.complete_step(repo_root=tmp_path, run_id="r3", step_id="frame")


def _write_plan(paths: engine.RunPaths, slices: list[Slice]) -> None:
    plan_obj = Plan(objective_id="obj-1", product_dir="products/x", slices=slices)
    paths.plan.write_text(plan_obj.model_dump_json(indent=2), encoding="utf-8")


def test_fanout_plan_emits_packets_in_waves(tmp_path: Path) -> None:
    engine.plan(repo_root=tmp_path, objective=_objective(), run_id="r4")
    paths = engine.RunPaths(tmp_path, "r4")
    _write_plan(
        paths,
        [
            Slice(id="a", title="A", allowed_globs=["products/x/a/**"]),
            Slice(id="b", title="B", allowed_globs=["products/x/b/**"]),
            Slice(id="c", title="C", allowed_globs=["products/x/a/**"], depends_on=["a"]),
        ],
    )
    out = engine.fanout_plan(repo_root=tmp_path, run_id="r4")
    waves = out["waves"]
    # a,b disjoint -> wave 1; c depends on a AND overlaps a's scope -> later wave
    assert len(waves) == 2
    assert {p["packet_id"] for p in waves[0]} == {"a", "b"}
    assert waves[1][0]["packet_id"] == "c"
    # packets carry merged blocked-globs (global + slice)
    assert any(".git/**" in p["blocked_globs"] for p in waves[0])
    assert (paths.packets_dir / "a.json").exists()


def test_record_result_accepts_in_scope(git_repo: Path, tmp_path: Path) -> None:
    # repo_root for .hw is the git repo itself; the worktree is also the git repo
    engine.plan(repo_root=git_repo, objective=_objective(), run_id="r5", product_dir="src")
    paths = engine.RunPaths(git_repo, "r5")
    _write_plan(paths, [Slice(id="a", title="A", allowed_globs=["src/**", ".hw/**"])])
    engine.fanout_plan(repo_root=git_repo, run_id="r5")
    engine.register_worktree(repo_root=git_repo, run_id="r5", packet_id="a", worktree=str(git_repo))
    paths.results_dir.mkdir(parents=True, exist_ok=True)
    (paths.results_dir / "a.json").write_text('{"status": "done"}\n')

    (git_repo / "src" / "new.py").write_text("v = 1\n", encoding="utf-8")
    out = engine.record_result(
        repo_root=git_repo, run_id="r5", packet_id="a", worktree=str(git_repo)
    )
    assert out["verdict"]["accepted"] is True


def test_record_result_rejects_and_rolls_back_escape(git_repo: Path) -> None:
    engine.plan(repo_root=git_repo, objective=_objective(), run_id="r6", product_dir="src")
    paths = engine.RunPaths(git_repo, "r6")
    _write_plan(paths, [Slice(id="a", title="A", allowed_globs=["src/**"])])
    engine.fanout_plan(repo_root=git_repo, run_id="r6")
    engine.register_worktree(repo_root=git_repo, run_id="r6", packet_id="a", worktree=str(git_repo))
    paths.results_dir.mkdir(parents=True, exist_ok=True)
    (paths.results_dir / "a.json").write_text('{"status": "done"}\n')

    (git_repo / "src" / "ok.py").write_text("v = 1\n", encoding="utf-8")
    (git_repo / "evil.py").write_text("danger\n", encoding="utf-8")
    out = engine.record_result(
        repo_root=git_repo, run_id="r6", packet_id="a", worktree=str(git_repo)
    )
    verdict = out["verdict"]
    assert verdict["accepted"] is False
    assert "evil.py" in verdict["escapes"]
    assert verdict["rolled_back"] is True
    assert not (git_repo / "evil.py").exists()
    assert not (git_repo / "src" / "ok.py").exists()


def test_evaluate_hard_blocks_on_failure(tmp_path: Path) -> None:
    # A product dir whose tests fail -> gate must HARD-BLOCK (raise GateFailure).
    engine.plan(repo_root=tmp_path, objective=_objective(), run_id="r7")
    product = tmp_path / "product"
    product.mkdir()
    # syntax error -> ruff/mypy fail
    (product / "broken.py").write_text("def f(:\n", encoding="utf-8")
    with pytest.raises(engine.GateFailure):
        engine.evaluate(repo_root=tmp_path, run_id="r7", target=product)
    state = engine.load_state(engine.RunPaths(tmp_path, "r7"))
    assert state.status.value == "gate_failed"


def test_events_jsonl_is_appended(tmp_path: Path) -> None:
    engine.plan(repo_root=tmp_path, objective=_objective(), run_id="r8")
    events_path = engine.RunPaths(tmp_path, "r8").run_dir / "events.jsonl"
    lines = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    assert lines and lines[0]["stage"] == "plan"
