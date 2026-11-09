"""L8 — the full-file-replacement guardrail exempts DECLARED generated artifacts only."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from hyperwilfred_core import engine, guardrails
from hyperwilfred_core.schemas import (
    AcceptanceVerdict,
    BuildObjective,
    GeneratedArtifact,
    Plan,
    Slice,
    StepStatus,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit_snapshot(repo: Path, payload: dict) -> None:
    (repo / "snap").mkdir(exist_ok=True)
    (repo / "snap" / "accounts.json").write_text(json.dumps(payload), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "snapshot")


def _accept(repo: Path, **kw: object) -> AcceptanceVerdict:
    return guardrails.accept(
        packet_id="p", worktree=str(repo), allowed_globs=["**"], blocked_globs=[],
        result_file=None, max_changed_lines=400, max_hunk_span=120, full_file_ratio=0.9,
        do_rollback=False, **kw,  # type: ignore[arg-type]
    )


def test_single_line_json_snapshot_trips_the_rule_without_a_declaration(git_repo: Path) -> None:
    """The pilot gap: a regenerated one-line snapshot is 100% changed → rejected."""
    _commit_snapshot(git_repo, {"accounts": [1, 2, 3], "generated_at": "2026-09-17"})
    (git_repo / "snap" / "accounts.json").write_text(
        json.dumps({"accounts": [4, 5, 6, 7], "generated_at": "2026-09-18"}), encoding="utf-8"
    )
    verdict = _accept(git_repo)
    assert not verdict.accepted
    assert any(
        "full-file-replacement" in r for v in verdict.patch_violations for r in v.reasons
    )


def test_declared_generated_snapshot_is_exempt(git_repo: Path) -> None:
    _commit_snapshot(git_repo, {"accounts": [1, 2, 3], "generated_at": "2026-09-17"})
    (git_repo / "snap" / "accounts.json").write_text(
        json.dumps({"accounts": [4, 5, 6, 7], "generated_at": "2026-09-18"}), encoding="utf-8"
    )
    verdict = _accept(git_repo, generated_globs=["snap/*.json"])
    assert verdict.accepted, verdict.patch_violations


def test_source_files_keep_the_protection_even_when_a_snapshot_is_declared(
    git_repo: Path,
) -> None:
    """Other direction: regenerating the declared snapshot is fine; rewriting source is not."""
    _commit_snapshot(git_repo, {"a": 1})
    (git_repo / "snap" / "accounts.json").write_text('{"a": 2, "b": 3}', encoding="utf-8")
    (git_repo / "README.md").write_text("all\nnew\ntext\nhere\nnow\nyes\n", encoding="utf-8")
    verdict = _accept(git_repo, generated_globs=["snap/*.json"])
    assert not verdict.accepted
    flagged = {v.path for v in verdict.patch_violations}
    assert flagged == {"README.md"}  # the snapshot is exempt, the source file is not


def test_exemption_does_not_widen_the_allow_list(git_repo: Path) -> None:
    _commit_snapshot(git_repo, {"a": 1})
    (git_repo / "snap" / "accounts.json").write_text('{"a": 2}', encoding="utf-8")
    verdict = guardrails.accept(
        packet_id="p", worktree=str(git_repo), allowed_globs=["src/**"], blocked_globs=[],
        result_file=None, max_changed_lines=400, max_hunk_span=120, full_file_ratio=0.9,
        do_rollback=False, generated_globs=["snap/*.json"],
    )
    assert not verdict.accepted and "snap/accounts.json" in verdict.escapes


def test_regeneration_command_is_required() -> None:
    with pytest.raises(ValidationError, match="regeneration command"):
        GeneratedArtifact(path="snap/accounts.json", command="  ")


@pytest.mark.parametrize(
    "path", ["**", "*", "src/**/*", "src/app.py", "web/page.tsx", "lib/util.ts", "Makefile"]
)
def test_broad_or_source_paths_cannot_be_declared_generated(path: str) -> None:
    with pytest.raises(ValidationError, match="too broad or not a data file"):
        GeneratedArtifact(path=path, command="make snapshots")


def test_plan_rejects_a_generated_path_outside_the_slice_scope(tmp_path: Path) -> None:
    obj = BuildObjective(id="o", title="t", objective="x")
    engine.plan(repo_root=tmp_path, objective=obj, run_id="g", product_dir="product")
    paths = engine.RunPaths(tmp_path, "g")
    bad = Plan(
        objective_id="o", product_dir="product",
        slices=[Slice(
            id="s", title="t", allowed_globs=["product/src/**"],
            generated_artifacts=[GeneratedArtifact(path="other/x.json", command="make x")],
        )],
    )
    paths.plan.write_text(bad.model_dump_json(), encoding="utf-8")
    with pytest.raises(engine.EngineError, match="outside its allowed_globs"):
        engine._validate_plan(paths.plan)


def test_fanout_packet_carries_generated_artifacts_and_record_result_honours_them(
    git_repo: Path,
) -> None:
    repo = git_repo
    _commit_snapshot(repo, {"n": 1, "generated_at": "t0"})
    obj = BuildObjective(id="o", title="t", objective="x")
    engine.plan(repo_root=repo, objective=obj, run_id="g", product_dir="snap")
    paths = engine.RunPaths(repo, "g")
    state = engine.load_state(paths)
    for s in state.steps:
        if s.id == "implement":
            break
        state.step_status[s.id] = StepStatus.DONE
    engine.save_state(paths, state)
    plan = Plan(
        objective_id="o", product_dir="snap",
        slices=[Slice(
            id="gen", title="regenerate snapshot", allowed_globs=["snap/**"],
            generated_artifacts=[
                GeneratedArtifact(path="snap/accounts.json", command="make snapshots",
                                  volatile_fields=["generated_at"])
            ],
        )],
    )
    paths.plan.write_text(plan.model_dump_json(), encoding="utf-8")
    out = engine.fanout_plan(repo_root=repo, run_id="g")
    pkt = out["waves"][0][0]
    assert pkt["generated_artifacts"][0]["command"] == "make snapshots"

    (repo / "snap" / "accounts.json").write_text('{"n": 99, "generated_at": "t1"}', "utf-8")
    res = engine.record_result(
        repo_root=repo, run_id="g", packet_id="gen", worktree=str(repo), do_rollback=False
    )
    assert res["verdict"]["accepted"] is True
