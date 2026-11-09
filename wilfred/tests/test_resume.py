"""Durable resume — artifact-scan crash recovery (Phase 4).

The central guarantee under test: a run killed mid-flight resumes from the FIRST step whose
declared artifact is missing or invalid (truncated), re-running it and every downstream step,
while durably-complete steps are skipped. A truncated artifact is RE-RUN, never silently skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from hyperwilfred_core import engine, resume
from hyperwilfred_core.run_store import atomic_write_text
from hyperwilfred_core.schemas import (
    AcceptanceVerdict,
    BuildObjective,
    GateKind,
    GateStatus,
    HumanGate,
    Packet,
    Plan,
    QualityLoopState,
    RunStatus,
    Slice,
    StepStatus,
)


def _objective() -> BuildObjective:
    return BuildObjective(id="obj-1", title="Landing", objective="build a landing page")


def _build_completed_run(repo_root: Path, run_id: str) -> engine.RunPaths:
    """Create a BUILD run with EVERY step durably complete (valid on-disk evidence per kind)."""
    engine.plan(
        repo_root=repo_root, objective=_objective(), run_id=run_id, product_dir="products/x"
    )
    paths = engine.RunPaths(repo_root, run_id)
    rd = paths.run_dir

    # single / gate artifacts
    paths.artifact("frame.md").write_text("framed\n", encoding="utf-8")
    plan = Plan(
        objective_id="obj-1",
        product_dir="products/x",
        slices=[Slice(id="a", title="A", allowed_globs=["products/x/**"])],
    )
    atomic_write_text(paths.plan, plan.model_dump_json(indent=2) + "\n")
    paths.artifact("scaffold.md").write_text("scaffold\n", encoding="utf-8")
    paths.artifact("verify.md").write_text("verified\n", encoding="utf-8")

    from hyperwilfred_core.evaluators.verification import VerificationReport

    report = VerificationReport(task_id="obj-1", run_id=run_id, completed_at=datetime.now(UTC))
    atomic_write_text(paths.artifact("verification.json"), report.model_dump_json(indent=2) + "\n")

    # fanout: one accepted packet
    packet = Packet(
        packet_id="a", run_id=run_id, step_id="implement", slice_id="a", title="A",
        instruction="impl", allowed_globs=["products/x/**"], blocked_globs=[],
        max_files=10, max_changed_lines=100, max_hunk_span=80, full_file_ratio=0.9,
        worktree_hint=f".hw/{run_id}/worktrees/a", result_path=f".hw/{run_id}/results/a.json",
    )
    atomic_write_text(rd / "packets" / "a.json", packet.model_dump_json(indent=2) + "\n")
    verdict = AcceptanceVerdict(packet_id="a", worktree="wt", accepted=True)
    atomic_write_text(rd / "verdicts" / "a.json", verdict.model_dump_json(indent=2) + "\n")

    # human gate: approved
    gate = HumanGate(gate_id="g1", run_id=run_id, kind=GateKind.MERGE, status=GateStatus.APPROVED)
    atomic_write_text(rd / "gates" / "g1.json", gate.model_dump_json(indent=2) + "\n")

    # mark everything done + quality passed
    state = engine.load_state(paths)
    for step in state.steps:
        state.step_status[step.id] = StepStatus.DONE
    state.quality = QualityLoopState(passed=True)
    state.status = RunStatus.IN_PROGRESS
    engine.save_state(paths, state)
    return paths


def test_fully_complete_run_has_no_resume_cursor(tmp_path: Path) -> None:
    _build_completed_run(tmp_path, "r1")
    out = resume.resume(repo_root=tmp_path, run_id="r1")
    assert out["complete"] is True
    assert out["resume_cursor"] is None
    assert out["reset_steps"] == []
    assert engine.load_state(engine.RunPaths(tmp_path, "r1")).status is RunStatus.COMPLETED


def test_missing_artifact_on_done_step_is_re_run(tmp_path: Path) -> None:
    paths = _build_completed_run(tmp_path, "r2")
    # Step marked done, but its artifact was lost (e.g. the file never flushed before the crash).
    paths.artifact("scaffold.md").unlink()
    out = resume.resume(repo_root=tmp_path, run_id="r2")
    assert out["resume_cursor"] == "scaffold"
    # scaffold + every downstream step reset to pending; frame/decompose kept done.
    assert "scaffold" in out["reset_steps"]
    state = engine.load_state(paths)
    assert state.step_status["frame"] is StepStatus.DONE
    assert state.step_status["decompose"] is StepStatus.DONE
    assert state.step_status["scaffold"] is StepStatus.PENDING
    assert state.step_status["implement"] is StepStatus.PENDING
    assert state.step_status["ship"] is StepStatus.PENDING


def test_truncated_json_artifact_is_detected_and_re_run(tmp_path: Path) -> None:
    paths = _build_completed_run(tmp_path, "r3")
    # decompose is marked done, but plan.json was killed mid-write → truncated, fails validation.
    paths.plan.write_text('{"objective_id": "obj-1", "product_dir": "produ', encoding="utf-8")
    out = resume.resume(repo_root=tmp_path, run_id="r3")
    assert out["resume_cursor"] == "decompose", "a truncated artifact must be RE-RUN, not skipped"
    state = engine.load_state(paths)
    assert state.step_status["frame"] is StepStatus.DONE
    assert state.step_status["decompose"] is StepStatus.PENDING


def test_interrupted_running_step_is_re_run(tmp_path: Path) -> None:
    paths = _build_completed_run(tmp_path, "r4")
    state = engine.load_state(paths)
    # Simulate a crash with 'frame' still RUNNING (status set just before the adapter executed it).
    state.step_status["frame"] = StepStatus.RUNNING
    engine.save_state(paths, state)
    out = resume.resume(repo_root=tmp_path, run_id="r4")
    assert out["resume_cursor"] == "frame"
    assert "interrupted" in out["cursor_reason"]


def test_incomplete_fanout_is_the_cursor(tmp_path: Path) -> None:
    paths = _build_completed_run(tmp_path, "r5")
    # implement marked done but a packet's verdict was lost → fan-out is incomplete.
    (paths.run_dir / "verdicts" / "a.json").unlink()
    out = resume.resume(repo_root=tmp_path, run_id="r5")
    assert out["resume_cursor"] == "implement"


def test_dry_run_does_not_mutate_state(tmp_path: Path) -> None:
    paths = _build_completed_run(tmp_path, "r6")
    paths.artifact("scaffold.md").unlink()
    before = paths.state.read_text(encoding="utf-8")
    out = resume.resume(repo_root=tmp_path, run_id="r6", apply=False)
    assert out["applied"] is False
    assert out["resume_cursor"] == "scaffold"
    assert "scaffold" in out["reset_steps"]  # WOULD reset
    assert paths.state.read_text(encoding="utf-8") == before, "dry-run must not touch state.json"


def test_failed_run_is_not_resurrected(tmp_path: Path) -> None:
    paths = _build_completed_run(tmp_path, "r7")
    state = engine.load_state(paths)
    state.status = RunStatus.FAILED  # a human rejected an irreversible gate (Phase-3 invariant)
    engine.save_state(paths, state)
    out = resume.resume(repo_root=tmp_path, run_id="r7")
    assert out["halted"] is True
    assert out["applied"] is False
    # state untouched — still FAILED, not advanced.
    assert engine.load_state(paths).status is RunStatus.FAILED


def test_awaiting_human_gate_is_left_intact(tmp_path: Path) -> None:
    paths = _build_completed_run(tmp_path, "r8")
    # Roll back to: everything before ship done, ship pending + run awaiting a human approval.
    rd = paths.run_dir
    (rd / "gates" / "g1.json").unlink()
    pending = HumanGate(gate_id="g1", run_id="r8", kind=GateKind.MERGE, status=GateStatus.PENDING)
    atomic_write_text(rd / "gates" / "g1.json", pending.model_dump_json(indent=2) + "\n")
    state = engine.load_state(paths)
    state.step_status["ship"] = StepStatus.PENDING
    state.status = RunStatus.AWAITING_HUMAN
    engine.save_state(paths, state)
    out = resume.resume(repo_root=tmp_path, run_id="r8")
    assert out.get("awaiting_human") is True
    assert out["applied"] is False
    # The gate is legitimately waiting — resume must not "recover" it.
    assert engine.load_state(paths).status is RunStatus.AWAITING_HUMAN


def test_resume_is_idempotent_when_already_clean(tmp_path: Path) -> None:
    _build_completed_run(tmp_path, "r9")
    first = resume.resume(repo_root=tmp_path, run_id="r9")
    second = resume.resume(repo_root=tmp_path, run_id="r9")
    assert first["complete"] is second["complete"] is True
    assert second["reset_steps"] == []
