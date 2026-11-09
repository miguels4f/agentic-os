"""L5 — plan lint (pilot replay (b)): cut by responsibility, not by file count."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyperwilfred_core import engine, plan_lint
from hyperwilfred_core.schemas import (
    BuildObjective,
    GeneratedArtifact,
    Plan,
    RiskLevel,
    Slice,
    StepStatus,
)


def _pilot_b_plan() -> Plan:
    """B's V2 plan shape: max_files 2 packets; the UI packet recomputes the engine's rule."""
    return Plan(
        objective_id="o", product_dir="site",
        slices=[
            Slice(id="slice-1", title="Action check engine",
                  description="Implement the allowed/blocked gate check per account action.",
                  allowed_globs=["engine/**"], max_files=2),
            Slice(id="slice-2", title="Snapshot generator",
                  description="Emit the per-account snapshot.",
                  allowed_globs=["scripts/snapshot/**", "site/data/**"], max_files=2,
                  generated_artifacts=[
                      GeneratedArtifact(path="site/data/accounts.json", command="make snapshot")
                  ]),
            Slice(id="slice-3", title="Account page actions",
                  description="Recompute the allowed/blocked state in TypeScript so the page can "
                  "grey out actions.",
                  allowed_globs=["site/src/**"], max_files=6),
        ],
    )


def _obj(risk: RiskLevel = RiskLevel.MEDIUM) -> BuildObjective:
    return BuildObjective(id="o", title="t", objective="x", risk_level=risk)


def test_replay_b_recompute_in_typescript_is_flagged() -> None:
    report = plan_lint.lint(_pilot_b_plan(), _obj())
    rules = {(f.rule, tuple(f.slice_ids)) for f in report.findings}
    assert ("reimplements-rule", ("slice-3",)) in rules
    assert report.blocking is False  # warn by default
    msg = next(f.message for f in report.findings if f.rule == "reimplements-rule")
    assert "twice" in msg


def test_rule_separated_from_its_publication_point_is_flagged() -> None:
    report = plan_lint.lint(_pilot_b_plan(), _obj())
    sep = [f for f in report.findings if f.rule == "rule-separated-from-publisher"]
    assert sep and sep[0].slice_ids == ["slice-1", "slice-2"]


@pytest.mark.parametrize(
    "text",
    [
        "Mirror the gate logic in the page",
        "Duplicate the rule client-side",
        "Re-implement the eligibility check in the UI",
        "Port the rule to JavaScript",
        "the permission logic in the client",
    ],
)
def test_reimplementation_phrasings(text: str) -> None:
    plan = Plan(objective_id="o", product_dir="p",
                slices=[Slice(id="s", title="t", description=text, allowed_globs=["p/**"])])
    assert any(f.rule == "reimplements-rule" for f in plan_lint.lint(plan, _obj()).findings)


def test_a_responsibility_cut_plan_is_clean() -> None:
    plan = Plan(
        objective_id="o", product_dir="p",
        slices=[
            Slice(id="engine-and-snapshot", title="Engine embeds its verdict in the snapshot",
                  description="Compute the gate result once in the engine and publish it in the "
                  "snapshot.", allowed_globs=["engine/**", "site/data/**"],
                  generated_artifacts=[
                      GeneratedArtifact(path="site/data/accounts.json", command="make snapshot")
                  ]),
            Slice(id="page", title="Account page", description="Display the published verdict.",
                  allowed_globs=["site/src/**"], depends_on=["engine-and-snapshot"]),
        ],
    )
    assert plan_lint.lint(plan, _obj()).findings == []


def test_high_and_critical_risk_are_a_hard_block() -> None:
    for risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
        report = plan_lint.lint(_pilot_b_plan(), _obj(risk))
        assert report.blocking and not report.ok
    clean = Plan(objective_id="o", product_dir="p", slices=[Slice(id="s", title="t")])
    assert plan_lint.lint(clean, _obj(RiskLevel.HIGH)).ok


def _at_decompose(repo: Path, risk: RiskLevel, plan: Plan) -> engine.RunPaths:
    obj = _obj(risk)
    engine.plan(repo_root=repo, objective=obj, run_id="l", product_dir="site")
    paths = engine.RunPaths(repo, "l")
    state = engine.load_state(paths)
    state.step_status["frame"] = StepStatus.DONE
    engine.save_state(paths, state)
    paths.artifact("plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    return paths


def test_decompose_completes_with_warnings_at_medium_risk(tmp_path: Path) -> None:
    _at_decompose(tmp_path, RiskLevel.MEDIUM, _pilot_b_plan())
    out = engine.complete_step(repo_root=tmp_path, run_id="l", step_id="decompose")
    assert {w["rule"] for w in out["plan_lint_warnings"]} >= {"reimplements-rule"}


def test_decompose_is_blocked_at_high_risk(tmp_path: Path) -> None:
    paths = _at_decompose(tmp_path, RiskLevel.HIGH, _pilot_b_plan())
    with pytest.raises(engine.EngineError, match="plan lint blocked"):
        engine.complete_step(repo_root=tmp_path, run_id="l", step_id="decompose")
    assert engine.load_state(paths).step_status["decompose"] is not StepStatus.DONE
