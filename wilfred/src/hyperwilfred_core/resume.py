"""Durable resume — artifact-scan crash recovery (engine-side, LLM-free + spawn-free).

The architecture's durability rule (spec §1): *durability is a property of HyperWilfred's
disk state, not of any runtime's session log.* Every step writes a named artifact to disk
atomically (temp-then-rename); on restart the router SCANS ``.hw/<run_id>/`` for the first step
whose declared artifact is **missing** or **fails its L1-schema validation** and resumes there.

Why a scan and not just ``step_status``: ``step_status`` is the engine's *intent*, the on-disk
artifacts are the *truth*. A step killed mid-write leaves a truncated file. If we trusted
``step_status`` alone, a step marked ``running`` (set just before the adapter executed it) that
crashed, or a step marked ``done`` whose artifact was lost/truncated, could be silently skipped.
The scan makes the artifacts authoritative (the disk-as-source-of-truth discipline) so a
truncated artifact is RE-RUN, never skipped.

This module, like the rest of the engine, calls NO LLM and spawns NOTHING. It reconciles
``step_status`` against the artifacts on disk and reports/positions the resume cursor.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from hyperwilfred_core.evaluators.verification import VerificationReport
from hyperwilfred_core.schemas import (
    AcceptanceVerdict,
    Plan,
    RunState,
    RunStatus,
    StepKind,
    StepStatus,
)


class ArtifactState(StrEnum):
    """The on-disk verdict for one step's declared artifact(s)."""

    VALID = "valid"  # the artifact exists and validates against its L1 schema
    MISSING = "missing"  # the step is done-on-paper but its artifact is absent
    INVALID = "invalid"  # the artifact exists but is truncated / fails schema validation
    INCOMPLETE = "incomplete"  # a fan-out / gate whose disk evidence is not all present
    NOT_PRODUCED = "not_produced"  # the step has not run yet (nothing expected on disk)


# Steps whose declared ``produces`` is a JSON contract validate against a pydantic model.
_JSON_CONTRACTS: dict[str, type] = {
    "plan.json": Plan,
    "verification.json": VerificationReport,
}


def _validate_json_artifact(path: Path, model: type) -> ArtifactState:
    if not path.exists():
        return ArtifactState.MISSING
    try:
        model.model_validate_json(path.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    except (ValueError, OSError):
        # A truncated mid-write file or a schema mismatch — must be re-run, never skipped.
        return ArtifactState.INVALID
    return ArtifactState.VALID


def _validate_text_artifact(path: Path) -> ArtifactState:
    if not path.exists():
        return ArtifactState.MISSING
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return ArtifactState.INVALID
    # An empty / whitespace-only markdown artifact is the truncated-write signature.
    return ArtifactState.VALID if content.strip() else ArtifactState.INVALID


def _fanout_state(run_dir: Path) -> ArtifactState:
    """A fan-out step is complete iff every emitted packet has an ACCEPTED verdict on disk."""
    packets_dir = run_dir / "packets"
    verdicts_dir = run_dir / "verdicts"
    packet_files = sorted(packets_dir.glob("*.json")) if packets_dir.exists() else []
    if not packet_files:
        return ArtifactState.NOT_PRODUCED  # fanout-plan never ran
    for pf in packet_files:
        vf = verdicts_dir / f"{pf.stem}.json"
        if not vf.exists():
            return ArtifactState.INCOMPLETE
        try:
            verdict = AcceptanceVerdict.model_validate_json(vf.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return ArtifactState.INVALID
        if not verdict.accepted:
            return ArtifactState.INCOMPLETE
    return ArtifactState.VALID


def _human_gate_state(run_dir: Path) -> ArtifactState:
    """A human gate is complete iff a gate JSON records a human APPROVED decision on disk."""
    from hyperwilfred_core.schemas import GateStatus, HumanGate

    gates_dir = run_dir / "gates"
    if not gates_dir.exists():
        return ArtifactState.NOT_PRODUCED
    for gf in sorted(gates_dir.glob("*.json")):
        try:
            gate = HumanGate.model_validate_json(gf.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if gate.status is GateStatus.APPROVED:
            return ArtifactState.VALID
    return ArtifactState.INCOMPLETE


def _quality_state(state: RunState) -> ArtifactState:
    """The quality loop is complete iff its on-disk state shows pass or the subjective fallback."""
    q = state.quality
    if q is None:
        return ArtifactState.NOT_PRODUCED
    return ArtifactState.VALID if (q.passed or q.used_fallback) else ArtifactState.INCOMPLETE


def _step_artifact_state(state: RunState, run_dir: Path, step) -> ArtifactState:  # noqa: ANN001
    """The on-disk completion evidence for one step, by kind."""
    if step.kind is StepKind.FANOUT:
        return _fanout_state(run_dir)
    if step.kind is StepKind.HUMAN_GATE:
        return _human_gate_state(run_dir)
    if step.kind is StepKind.QUALITY:
        return _quality_state(state)
    if step.produces:
        path = run_dir / step.produces
        model = _JSON_CONTRACTS.get(step.produces)
        return _validate_json_artifact(path, model) if model else _validate_text_artifact(path)
    return ArtifactState.NOT_PRODUCED  # a single step with no declared artifact (none today)


def scan(*, repo_root: Path, state: RunState) -> dict:
    """Reconcile ``step_status`` against the on-disk artifacts. Pure read — never mutates.

    Returns a per-step report plus the resume cursor: the FIRST step that is either marked done
    but whose artifact is missing/invalid, marked running (interrupted), or simply not yet done.
    Everything from the cursor onward must be re-run; everything before it is durably complete.
    """
    run_dir = repo_root / ".hw" / state.run_id
    steps_report: list[dict] = []
    cursor_id: str | None = None
    cursor_reason: str | None = None

    for step in state.steps:
        recorded = state.step_status.get(step.id, StepStatus.PENDING)
        art = _step_artifact_state(state, run_dir, step)
        # A step is "durably done" only if intent says done AND the disk evidence validates.
        durably_done = recorded is StepStatus.DONE and art is ArtifactState.VALID

        reason: str | None = None
        if cursor_id is None and not durably_done:
            cursor_id = step.id
            if recorded is StepStatus.DONE and art is not ArtifactState.VALID:
                reason = f"marked done but artifact {art.value} (truncated/lost mid-write) — re-run"
            elif recorded is StepStatus.RUNNING:
                reason = "interrupted mid-execution (status=running) — re-run"
            elif recorded is StepStatus.FAILED:
                reason = "previously failed — re-run after the cause is addressed"
            else:
                reason = "next pending step"
            cursor_reason = reason

        steps_report.append(
            {
                "step_id": step.id,
                "kind": step.kind.value,
                "recorded_status": recorded.value,
                "artifact_state": art.value,
                "durably_done": durably_done,
                "is_resume_cursor": cursor_id == step.id and reason is not None,
            }
        )

    return {
        "run_id": state.run_id,
        "resume_cursor": cursor_id,
        "cursor_reason": cursor_reason,
        "complete": cursor_id is None,
        "steps": steps_report,
    }


def resume(*, repo_root: Path, run_id: str, apply: bool = True) -> dict:
    """Scan for the resume cursor and (when ``apply``) reset the cursor + every downstream step.

    Reset means: from the resume cursor onward, every step returns to ``pending`` so the driver
    re-runs them; durably-complete steps before the cursor are left ``done`` and are skipped. A
    truncated artifact (its step marked done but failing validation) is reset and RE-RUN, never
    skipped — the central durability guarantee.

    Refuses to resurrect a human-REJECTED run (``status=failed``) — the Phase-3 invariant that a
    human reject is terminal. An ``awaiting_human`` run with a still-pending gate is left intact
    (the gate is legitimately waiting, not a crash to recover from).
    """
    from hyperwilfred_core.engine import RunPaths, load_state, save_state
    from hyperwilfred_core.run_store import RunEventStatus, RunStore

    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)

    if state.status is RunStatus.FAILED:
        return {
            "run_id": run_id,
            "halted": True,
            "applied": False,
            "reason": "the run was FAILED (a human rejected an irreversible gate) — it is "
            "terminal and does not resume; open a new run if the change should proceed",
        }

    report = scan(repo_root=repo_root, state=state)
    cursor_id = report["resume_cursor"]

    if cursor_id is None:
        if apply and state.status is not RunStatus.COMPLETED:
            state.status = RunStatus.COMPLETED
            save_state(paths, state)
        return {**report, "applied": apply, "reset_steps": []}

    # An awaiting-human run whose cursor IS the human gate is genuinely waiting — do not "recover".
    cursor_step = next(s for s in state.steps if s.id == cursor_id)
    if (
        state.status is RunStatus.AWAITING_HUMAN
        and cursor_step.kind is StepKind.HUMAN_GATE
    ):
        return {
            **report,
            "applied": False,
            "reset_steps": [],
            "awaiting_human": True,
            "reason": "the resume cursor is an open human gate — it is legitimately waiting for "
            "a human approval file, not a crash to recover from",
        }

    if not apply:
        # Dry run: report which steps WOULD reset without mutating disk.
        seen = False
        would_reset: list[str] = []
        for step in state.steps:
            if step.id == cursor_id:
                seen = True
            if seen:
                would_reset.append(step.id)
        return {**report, "applied": False, "reset_steps": would_reset}

    # Apply: reset the cursor + every downstream step to pending (re-run from the cursor).
    seen = False
    reset_steps: list[str] = []
    for step in state.steps:
        if step.id == cursor_id:
            seen = True
        if seen and state.step_status.get(step.id) is not StepStatus.PENDING:
            state.step_status[step.id] = StepStatus.PENDING
            reset_steps.append(step.id)
        elif seen:
            reset_steps.append(step.id)

    any_done = any(v is StepStatus.DONE for v in state.step_status.values())
    state.status = RunStatus.IN_PROGRESS if any_done else RunStatus.CREATED
    save_state(paths, state)
    RunStore(paths.run_dir).append_event(
        run_id=run_id,
        stage="resume",
        status=RunEventStatus.running,
        message=f"Resumed at {cursor_id!r} ({report['cursor_reason']}); reset "
        f"{len(reset_steps)} step(s) to pending",
    )
    return {**report, "applied": True, "reset_steps": reset_steps}


__all__ = ["ArtifactState", "resume", "scan"]
