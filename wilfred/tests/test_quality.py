"""The quality loop — the load-bearing Phase-2 claims.

Proves: the 0-5 evaluator gates on the MIN (not the average) and the engine recomputes it; the
deterministic gate is a hard wall the loop cannot pass while red (precedence rule); an open
blocker escalates to a human after the budget rather than being waived; a pure subjective
shortfall falls back to the best version; and the surgical patcher's acceptance gate rejects a
full-file rewrite while accepting a surgical edit, resetting the deterministic gate on accept.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from review_attestation import record_attested

from hyperwilfred_core import engine, quality
from hyperwilfred_core.schemas import (
    BuildMode,
    BuildObjective,
    CriticFinding,
    CriticSeverity,
    CritiqueReport,
    Plan,
    QualityLoopState,
    ReviewAuthor,
    RunStatus,
    Slice,
    StepStatus,
    SubjectiveCriterion,
    SubjectiveEvaluation,
    SubjectiveIssue,
)


def _run_at_quality(
    repo: Path,
    *,
    run_id: str = "q",
    mode: BuildMode = BuildMode.BUILD,
    det_green: bool = True,
    iteration: int = 0,
    max_iterations: int = 3,
    threshold: int = 4,
    product_dir: str = "product",
) -> engine.RunPaths:
    """Stand up a run parked at the quality step, with the quality sub-state pre-seeded."""
    obj = BuildObjective(id="o", title="t", objective="do the thing")
    engine.plan(repo_root=repo, objective=obj, run_id=run_id, product_dir=product_dir, mode=mode)
    paths = engine.RunPaths(repo, run_id)
    state = engine.load_state(paths)
    det = "regression" if mode is BuildMode.IMPROVE else "evaluate"
    for s in state.steps:
        if s.id == "quality":
            break
        state.step_status[s.id] = StepStatus.DONE
    state.step_status["quality"] = StepStatus.RUNNING
    state.status = RunStatus.IN_PROGRESS
    if not det_green:
        state.step_status[det] = StepStatus.PENDING
    state.quality = QualityLoopState(
        det_gate_step=det,
        iteration=iteration,
        max_iterations=max_iterations,
        subjective_threshold=threshold,
    )
    engine.save_state(paths, state)
    return paths


def _inputs_dir(repo: Path, run_id: str) -> Path:
    # Transient evaluator/critic inputs live under .hw/ so they never count as an out-of-scope
    # escape at patch-acceptance time (the engine exempts its own .hw/ state dir).
    d = repo / ".hw" / run_id / "_inputs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _record_subjective(
    repo: Path, run_id: str, scores: list[int], *, self_reported: int = 0,
    issues: list[SubjectiveIssue] | None = None,
) -> dict:
    ev = SubjectiveEvaluation(
        evaluator="t",
        criteria=[SubjectiveCriterion(name=f"c{i}", score=s) for i, s in enumerate(scores)],
        global_score=self_reported,
        issues=issues or [],
        author=ReviewAuthor.INDEPENDENT,
    )
    inp = _inputs_dir(repo, run_id) / "subj_in.json"
    inp.write_text(ev.model_dump_json(), encoding="utf-8")
    return record_attested(
        quality.record_subjective, repo=repo,
        repo_root=repo, run_id=run_id, result_file=str(inp),
    )


def _add_critique(repo: Path, run_id: str, lens: str, findings: list[CriticFinding]) -> None:
    state = engine.load_state(engine.RunPaths(repo, run_id))
    iteration = state.quality.iteration if state.quality else 0
    packet_dir = repo / ".hw" / run_id / "quality" / f"iter-{iteration}" / "critic-packets"
    if not packet_dir.exists():
        quality.critique_plan(repo_root=repo, run_id=run_id)
    report = CritiqueReport(
        critic="c", lens=lens, findings=findings, author=ReviewAuthor.INDEPENDENT
    )
    inp = repo / ".hw" / run_id / "quality" / f"iter-{iteration}" / "critics" / f"{lens}.json"
    inp.parent.mkdir(parents=True, exist_ok=True)
    inp.write_text(report.model_dump_json(), encoding="utf-8")
    record_attested(
        quality.record_critique, repo=repo, repo_root=repo, run_id=run_id,
        packet_id=f"critic-{iteration}-{lens}", result_file=str(inp),
    )


def _independent_panel(repo: Path, run_id: str) -> None:
    """A full independent critic panel with no findings (every required lens present)."""
    for lens in quality.CRITIC_LENSES:
        _add_critique(repo, run_id, lens, [])


# --------------------------------------------------------------------------- the MIN gate


def test_global_score_is_min_not_average_and_engine_recomputes(tmp_path: Path) -> None:
    _run_at_quality(tmp_path, threshold=4)
    # criteria average to 4.4 but the MIN is 2; evaluator self-reports a flattering 5.
    out = _record_subjective(tmp_path, "q", [5, 5, 5, 2, 5], self_reported=5)
    assert out["aggregate_rule"] == "min"
    assert out["global_score"] == 2  # MIN, not avg 4.4, not the self-reported 5
    assert out["passes_threshold"] is False
    # the authoritative persisted copy carries the recomputed MIN, not the self-reported value
    persisted = SubjectiveEvaluation.model_validate_json(
        (tmp_path / ".hw/q/quality/iter-0/subjective.json").read_text(encoding="utf-8")
    )
    assert persisted.global_score == 2


# --------------------------------------------------------------------------- gate decisions


def test_clean_pass(tmp_path: Path) -> None:
    paths = _run_at_quality(tmp_path, threshold=4)
    _record_subjective(tmp_path, "q", [5, 5, 5, 5, 5])
    _independent_panel(tmp_path, "q")
    g = quality.quality_gate(repo_root=tmp_path, run_id="q")
    assert g["action"] == "pass"
    state = engine.load_state(paths)
    assert state.step_status["quality"] is StepStatus.DONE
    assert state.quality is not None and state.quality.passed is True


def test_below_threshold_iterates_to_patch_with_budget(tmp_path: Path) -> None:
    paths = _run_at_quality(tmp_path, threshold=4, iteration=0, max_iterations=3)
    _record_subjective(tmp_path, "q", [5, 5, 3, 5, 5])  # MIN 3 < 4
    g = quality.quality_gate(repo_root=tmp_path, run_id="q")
    assert g["action"] == "patch"
    state = engine.load_state(paths)
    assert state.quality is not None and state.quality.phase.value == "patch"
    assert state.step_status["quality"] is StepStatus.RUNNING  # not done


def test_precedence_deterministic_not_green_blocks_pass(tmp_path: Path) -> None:
    paths = _run_at_quality(tmp_path, det_green=False, threshold=4)
    _record_subjective(tmp_path, "q", [5, 5, 5, 5, 5])  # subjectively perfect…
    g = quality.quality_gate(repo_root=tmp_path, run_id="q")
    # …but the deterministic gate is red, so the loop CANNOT pass — precedence rule.
    assert g["action"] == "deterministic_gate_required"
    assert g["det_gate_step"] == "evaluate"
    state = engine.load_state(paths)
    assert state.step_status["quality"] is StepStatus.RUNNING


def test_open_blocker_iterates_when_budget_remains(tmp_path: Path) -> None:
    _run_at_quality(tmp_path, threshold=4, iteration=0, max_iterations=3)
    _record_subjective(tmp_path, "q", [5, 5, 5, 5, 5])
    _add_critique(
        tmp_path, "q", "security",
        [CriticFinding(id="s1", lens="security", severity=CriticSeverity.BLOCKER,
                       title="hardcoded secret")],
    )
    g = quality.quality_gate(repo_root=tmp_path, run_id="q")
    assert g["action"] == "patch"
    assert g["open_blocker"] == 1


def test_open_blocker_escalates_to_human_after_budget(tmp_path: Path) -> None:
    # iteration 2 of max 3 → no budget left (2+1 >= 3).
    paths = _run_at_quality(tmp_path, threshold=4, iteration=2, max_iterations=3)
    _record_subjective(tmp_path, "q", [5, 5, 5, 5, 5])  # subjectively fine
    _add_critique(
        tmp_path, "q", "correctness",
        [CriticFinding(id="b1", lens="correctness", severity=CriticSeverity.BLOCKER,
                       title="data loss path")],
    )
    g = quality.quality_gate(repo_root=tmp_path, run_id="q")
    assert g["action"] == "escalate"  # a blocker is NEVER silently waived
    state = engine.load_state(paths)
    assert state.status is RunStatus.AWAITING_HUMAN
    assert state.step_status["quality"] is StepStatus.FAILED
    # and next-step surfaces the human gate rather than advancing
    nxt = engine.next_step(repo_root=tmp_path, run_id="q")
    assert nxt.get("awaiting_human") is True


def test_subjective_shortfall_falls_back_to_best_after_budget(tmp_path: Path) -> None:
    """Legacy ID retained; a subjective shortfall now remains incomplete."""
    paths = _run_at_quality(tmp_path, threshold=4, iteration=2, max_iterations=3)
    _record_subjective(tmp_path, "q", [5, 5, 3, 5, 5])  # MIN 3 < 4, NO blocker
    _independent_panel(tmp_path, "q")
    g = quality.quality_gate(repo_root=tmp_path, run_id="q")
    assert g["action"] == "escalate"
    assert g["passed"] is False
    state = engine.load_state(paths)
    assert state.step_status["quality"] is StepStatus.FAILED
    assert state.quality is not None
    assert state.quality.used_fallback is False and state.quality.passed is False


def test_open_major_cannot_pass_on_high_subjective_score(tmp_path: Path) -> None:
    _run_at_quality(tmp_path, threshold=4, iteration=0, max_iterations=3)
    _record_subjective(
        tmp_path, "q", [5, 5, 5, 5, 5],
        issues=[SubjectiveIssue(severity=CriticSeverity.MAJOR, criterion="correctness",
                                description="material defect", suggestion="repair it")],
    )
    assert quality.quality_gate(repo_root=tmp_path, run_id="q")["action"] == "patch"


def test_fallback_never_fires_while_deterministic_red(tmp_path: Path) -> None:
    # Budget exhausted AND subjective shortfall — but the deterministic gate is red, so the
    # fallback must NOT fire; the hard wall wins over the best-version fallback.
    _run_at_quality(tmp_path, det_green=False, threshold=4, iteration=2, max_iterations=3)
    _record_subjective(tmp_path, "q", [5, 5, 3, 5, 5])
    g = quality.quality_gate(repo_root=tmp_path, run_id="q")
    assert g["action"] == "deterministic_gate_required"


# --------------------------------------------------------------------------- the surgical patch


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _to_patch_phase(repo: Path, run_id: str) -> None:
    """Drive the loop into the PATCH phase via a major subjective issue."""
    _record_subjective(
        repo, run_id, [5, 5, 3, 5, 5],
        issues=[SubjectiveIssue(severity=CriticSeverity.MAJOR, criterion="c2",
                                description="weak", suggestion="tighten it")],
    )
    g = quality.quality_gate(repo_root=repo, run_id=run_id)
    assert g["action"] == "patch", g


def test_patch_accept_increments_iter_and_resets_det_gate(git_repo: Path) -> None:
    repo = git_repo
    paths = _run_at_quality(repo, run_id="p", product_dir="src")
    # plan so the patcher's allowed scope is src/**
    paths.plan.write_text(
        Plan(objective_id="o", product_dir="src",
             slices=[Slice(id="s", title="t", allowed_globs=["src/**"])]).model_dump_json(),
        encoding="utf-8",
    )
    _to_patch_phase(repo, "p")
    pp = quality.patch_plan(repo_root=repo, run_id="p")
    assert pp["packet"]["allowed_globs"] == ["src/**"]
    assert pp["packet"]["max_changed_lines"] <= 80  # tight surgical budget

    # surgical patcher: change ONE line of an existing file, write its result
    app = repo / "src" / "app.py"
    app.write_text("def add(a, b):\n    return a + b  # sum\n", encoding="utf-8")
    (repo / pp["packet"]["result_path"]).parent.mkdir(parents=True, exist_ok=True)
    (repo / pp["packet"]["result_path"]).write_text(
        '{"patched": ["x"], "skipped": [], "files_touched": ["src/app.py"]}\n', encoding="utf-8"
    )
    out = quality.record_patch(
        repo_root=repo, run_id="p", packet_id="patch-0", worktree=str(repo)
    )
    assert out["verdict"]["accepted"] is True, out["verdict"]
    state = engine.load_state(paths)
    assert state.quality is not None and state.quality.iteration == 1  # bumped
    # deterministic gate reset to PENDING → adapter MUST re-run it (a patch can break a test)
    assert state.step_status["evaluate"] is StepStatus.PENDING


def test_patch_rejects_full_file_rewrite_and_rolls_back(git_repo: Path) -> None:
    repo = git_repo
    # a sizeable tracked file the patcher could be tempted to regenerate wholesale
    big = "\n".join(f"line_{i} = {i}" for i in range(60)) + "\n"
    (repo / "product").mkdir()
    (repo / "product" / "mod.py").write_text(big, encoding="utf-8")
    _git(repo, "add", "product/mod.py")
    _git(repo, "commit", "-q", "-m", "seed product")

    paths = _run_at_quality(repo, run_id="p", product_dir="product")
    paths.plan.write_text(
        Plan(objective_id="o", product_dir="product",
             slices=[Slice(id="s", title="t", allowed_globs=["product/**"])]).model_dump_json(),
        encoding="utf-8",
    )
    _to_patch_phase(repo, "p")
    pp = quality.patch_plan(repo_root=repo, run_id="p")

    # the "patcher" REGENERATES the whole file (60 brand-new lines) — must be rejected.
    (repo / "product" / "mod.py").write_text(
        "\n".join(f"renamed_{i} = {i * 2}" for i in range(60)) + "\n", encoding="utf-8"
    )
    (repo / pp["packet"]["result_path"]).parent.mkdir(parents=True, exist_ok=True)
    (repo / pp["packet"]["result_path"]).write_text('{"patched": ["x"]}\n', encoding="utf-8")
    out = quality.record_patch(
        repo_root=repo, run_id="p", packet_id="patch-0", worktree=str(repo)
    )
    v = out["verdict"]
    assert v["accepted"] is False
    assert v["rolled_back"] is True
    assert v["patch_violations"]  # the full-file-rewrite tripped the guard
    # the wholesale rewrite was rolled back byte-clean
    assert (repo / "product" / "mod.py").read_text(encoding="utf-8") == big
    state = engine.load_state(paths)
    assert state.quality is not None and state.quality.iteration == 0  # NOT advanced
