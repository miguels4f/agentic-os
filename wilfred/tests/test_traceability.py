"""L4 — requirement traceability (pilot replay (d)): a dropped shaping part is MISSING."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyperwilfred_core import engine, traceability
from hyperwilfred_core.schemas import (
    BuildObjective,
    ContextRef,
    Plan,
    Slice,
    StepStatus,
)

SHAPING = """# Shaping — pilot action cockpit

## Requirements
| ID | Requirement |
|----|-------------|
| R1 | Every action is gated by the engine |
| R2 | Privacy: names never reach the snapshot |

## Shape B
- **B1** Snapshot generator
- **B2** Per-action paste command
  - B2.1 Allowed tools listed per action (`--allowedTools`)
  - B2.2 Budget cap (`--max-budget-usd`)
"""


def _objective(criteria: list[str], **kw: object) -> BuildObjective:
    return BuildObjective(
        id="o", title="cockpit", objective="build shape B of the cockpit per the shaping doc",
        acceptance_criteria=criteria,
        context_refs=[ContextRef(kind="path", value="docs/shaping.md")],
        **kw,  # type: ignore[arg-type]
    )


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "shaping.md").write_text(SHAPING, encoding="utf-8")
    return tmp_path


def _status(report: traceability.TraceabilityReport) -> dict[str, str]:
    return {e.id: e.status for e in report.ids}


def test_defined_ids_reads_tables_lists_and_nested_bullets() -> None:
    assert traceability.defined_ids(SHAPING) == ["R1", "R2", "B1", "B2", "B2.1", "B2.2"]


def test_replay_d_shaping_part_absent_from_contract_is_missing(tmp_path: Path) -> None:
    """Shaping B2.1 lists allowed tools; the contract omits them → MISSING."""
    repo = _repo(tmp_path)
    obj = _objective([
        "R1: every account action is gated by the engine",
        "R2: names never appear in the snapshot",
        "B1: the snapshot generator emits the gate result",
        "B2.2: the paste command carries --max-budget-usd",
    ])
    report = traceability.analyse(run_id="r", objective=obj, plan=None, repo_root=repo)
    assert not report.ok
    assert report.missing == ["B2.1"]
    assert _status(report)["B2.1"] == "MISSING"
    assert _status(report)["B2.2"] == "covered"
    # the bare parent is accounted for only when every child is — B2.1 is not, so B2 is MISSING
    assert _status(report)["B2"] == "MISSING"
    assert "B2.1" in report.message and "out_of_scope" in report.message


def test_covered_by_plan_slice_and_out_of_scope_are_accounted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    obj = _objective(
        ["R1 gated", "R2 privacy", "B1 snapshot", "B2.2 budget cap"],
        out_of_scope=["B2.1"],
    )
    report = traceability.analyse(run_id="r", objective=obj, plan=None, repo_root=repo)
    assert report.ok, report.message
    assert _status(report)["B2.1"] == "out_of_scope"
    assert _status(report)["B2"] == "covered"  # parent: every child accounted for

    obj2 = _objective(["R1 gated", "R2 privacy", "B1 snapshot", "B2.2 budget"])
    plan = Plan(
        objective_id="o", product_dir="p",
        slices=[Slice(id="s", title="paste command", acceptance_criteria=["B2.1 allowed tools"])],
    )
    assert traceability.analyse(run_id="r", objective=obj2, plan=plan, repo_root=repo).ok


def test_out_of_scope_line_in_constraints_and_prefix_wildcard(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    obj = _objective(
        ["R1", "R2", "B1", "B2.2"],
        constraints=["Out of scope: B2.1 (deferred to V3)"],
    )
    report = traceability.analyse(run_id="r", objective=obj, plan=None, repo_root=repo)
    assert report.ok and _status(report)["B2.1"] == "out_of_scope"

    obj_w = _objective(["R1", "R2"], out_of_scope=["B*"])
    assert traceability.analyse(run_id="r", objective=obj_w, plan=None, repo_root=repo).ok


def test_out_of_scope_line_does_not_count_as_coverage_of_other_ids(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    obj = _objective(["R1", "R2", "B1", "B2.2", "Non-goal: B2.1"])
    report = traceability.analyse(run_id="r", objective=obj, plan=None, repo_root=repo)
    assert report.ok and _status(report)["B2.1"] == "out_of_scope"


def test_id_mentioned_only_in_objective_prose_must_be_in_the_contract(tmp_path: Path) -> None:
    obj = BuildObjective(id="o", title="t", objective="implement R7 and R8 exactly",
                         acceptance_criteria=["R7 works"])
    report = traceability.analyse(run_id="r", objective=obj, plan=None, repo_root=tmp_path)
    assert report.missing == ["R8"]


def test_unreadable_context_ref_fails_closed(tmp_path: Path) -> None:
    obj = _objective(["R1"])  # docs/shaping.md does not exist under tmp_path
    report = traceability.analyse(run_id="r", objective=obj, plan=None, repo_root=tmp_path)
    assert not report.ok and report.unreadable_refs == ["docs/shaping.md"]


def test_no_ids_no_refs_is_ok(tmp_path: Path) -> None:
    obj = BuildObjective(id="o", title="t", objective="do the thing")
    assert traceability.analyse(run_id="r", objective=obj, plan=None, repo_root=tmp_path).ok


def test_version_like_tokens_are_not_ids(tmp_path: Path) -> None:
    obj = BuildObjective(id="o", title="t", objective="uses B2B and v1.2 and X86 chips")
    report = traceability.analyse(run_id="r", objective=obj, plan=None, repo_root=tmp_path)
    assert [e.id for e in report.ids] == ["X86"]  # B2B / v1.2 are not requirement IDs


def _at_verify(repo: Path, obj: BuildObjective) -> engine.RunPaths:
    engine.plan(repo_root=repo, objective=obj, run_id="v", product_dir="product")
    paths = engine.RunPaths(repo, "v")
    state = engine.load_state(paths)
    for s in state.steps:
        if s.id == "verify":
            break
        state.step_status[s.id] = StepStatus.DONE
    engine.save_state(paths, state)
    paths.artifact("verify.md").write_text("ran it\n", encoding="utf-8")
    return paths


def test_verify_step_is_blocked_by_missing_and_names_the_id(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    paths = _at_verify(repo, _objective(["R1", "R2", "B1", "B2.2"]))
    with pytest.raises(engine.EngineError, match=r"verify step blocked.*B2\.1"):
        engine.complete_step(repo_root=repo, run_id="v", step_id="verify")
    assert engine.load_state(paths).step_status["verify"] is not StepStatus.DONE
    assert (paths.run_dir / "verify-checks.json").exists()


def test_verify_step_completes_when_traced(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _at_verify(repo, _objective(["R1", "R2", "B1", "B2.1 tools", "B2.2 budget"]))
    out = engine.complete_step(repo_root=repo, run_id="v", step_id="verify")
    assert out["verify_checks"]["ok"] is True
