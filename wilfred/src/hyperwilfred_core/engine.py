"""The HyperWilfred portable engine — orchestration logic for the ``hw`` CLI.

CRUCIAL INVARIANT: the engine NEVER calls an LLM and NEVER spawns a sub-agent. It computes
"what should happen next", persists it to disk, and hands packets to whatever adapter is
driving it. Because it is literally the same process on both runtimes, its decisions are
byte-identical across Claude Code and (later) Codex.

All run state lives under ``<repo>/.hw/<run_id>/``. Every state write is atomic
(temp-then-rename) so a process killed mid-write leaves the previous good file intact —
the durability property that lets a run resume (and, in Phase 4, move between runtimes).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from hyperwilfred_core import gates, guardrails, known_failures
from hyperwilfred_core.evaluators import evaluators_for_stack, run_all
from hyperwilfred_core.evaluators.verification import CheckKind, CheckResult, Severity
from hyperwilfred_core.path_safety import (
    contained_path,
    validate_identifier,
    validate_relative_path,
)
from hyperwilfred_core.pipeline import load_instruction, steps_for
from hyperwilfred_core.run_store import RunEventStatus, RunStore, atomic_write_text
from hyperwilfred_core.schemas import (
    BuildMode,
    BuildObjective,
    EstimatedComplexity,
    GuardrailConfig,
    Packet,
    Plan,
    RiskLevel,
    RunState,
    RunStatus,
    Stack,
    StepKind,
    StepStatus,
    Tier,
)
from hyperwilfred_core.wave_planner import plan_waves


class EngineError(RuntimeError):
    """Raised on an engine precondition violation (surfaces as CLI exit 2)."""


class GateFailure(RuntimeError):
    """Raised when a deterministic gate HARD-BLOCKS. CLI maps this to a non-zero exit."""


# --------------------------------------------------------------------------- run layout


class RunPaths:
    """Resolve the on-disk paths for one run, rooted at ``<repo>/.hw/<run_id>/``."""

    def __init__(self, repo_root: Path, run_id: str) -> None:
        self.repo_root = repo_root.resolve()
        self.run_id = validate_identifier(run_id, label="run_id")
        state_root = contained_path(self.repo_root, ".hw", label="state root")
        self.run_dir = contained_path(state_root, self.run_id, label="run directory")

    @property
    def state(self) -> Path:
        return self.run_dir / "state.json"

    @property
    def guardrails(self) -> Path:
        return self.run_dir / "guardrails.json"

    @property
    def plan(self) -> Path:
        return self.run_dir / "plan.json"

    @property
    def packets_dir(self) -> Path:
        return self.run_dir / "packets"

    @property
    def results_dir(self) -> Path:
        return self.run_dir / "results"

    @property
    def verdicts_dir(self) -> Path:
        return self.run_dir / "verdicts"

    def artifact(self, name: str) -> Path:
        return self.run_dir / name


# --------------------------------------------------------------------------- tier router


def classify_tier(objective: BuildObjective) -> Tier:
    """Deterministic tier router (NO LLM). Mirrors hyperresearch light/full.

    A build goes FULL when it is large or risky enough to need the deeper path; otherwise
    LIGHT. Pure function of the objective's declared shape — no model judgment.
    """
    words = len(re.findall(r"\w+", objective.objective))
    if objective.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
        return Tier.FULL
    if objective.estimated_complexity in (EstimatedComplexity.L, EstimatedComplexity.XL):
        return Tier.FULL
    if words > 40 or len(objective.acceptance_criteria) > 3:
        return Tier.FULL
    return Tier.LIGHT


# --------------------------------------------------------------------------- state I/O


def load_state(paths: RunPaths) -> RunState:
    if not paths.state.exists():
        raise EngineError(f"no run found at {paths.run_dir}")
    return RunState.model_validate_json(paths.state.read_text(encoding="utf-8"))


def save_state(paths: RunPaths, state: RunState) -> None:
    state.updated_at = datetime.now(UTC)
    atomic_write_text(paths.state, state.model_dump_json(indent=2) + "\n")


def load_guardrails(paths: RunPaths) -> GuardrailConfig:
    return GuardrailConfig.model_validate_json(paths.guardrails.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- hw plan


def plan(
    *,
    repo_root: Path,
    objective: BuildObjective,
    run_id: str | None = None,
    product_dir: str | None = None,
    mode: BuildMode = BuildMode.BUILD,
    tier: Tier | None = None,
) -> RunState:
    """Create a run: write state, guardrails, objective. Returns the initial RunState.

    Does NOT decompose the objective into slices — that is the AI ``decompose`` STEP,
    executed by the adapter, never the engine. ``hw plan`` only stands up the run skeleton
    and the step graph.
    """
    resolved_id = run_id or uuid4().hex[:12]
    paths = RunPaths(repo_root, resolved_id)
    if paths.state.exists():
        raise EngineError(f"run {resolved_id!r} already exists at {paths.run_dir}")

    resolved_tier = tier or classify_tier(objective)
    resolved_product_dir = product_dir or f"products/{resolved_id}"
    steps = steps_for(mode)
    state = RunState(
        run_id=resolved_id,
        mode=mode,
        tier=resolved_tier,
        status=RunStatus.CREATED,
        objective=objective,
        product_dir=resolved_product_dir,
        steps=steps,
        step_status={s.id: StepStatus.PENDING for s in steps},
        base_commit=known_failures.head_commit(repo_root),
    )

    # Run state lives inside the host repo; keep it out of that repo's commits. Done before the
    # run folder exists so a failure here never leaves a run root without state.
    guardrails.ensure_engine_state_excluded(repo_root)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(paths.artifact("objective.json"), objective.model_dump_json(indent=2) + "\n")
    guardrail = GuardrailConfig(run_id=resolved_id)
    atomic_write_text(paths.guardrails, guardrail.model_dump_json(indent=2) + "\n")
    save_state(paths, state)

    store = RunStore(paths.run_dir)
    store.init()
    store.append_event(
        run_id=resolved_id,
        stage="plan",
        status=RunEventStatus.running,
        message=f"Run created (mode={mode.value}, tier={resolved_tier.value}, "
        f"base_commit={(state.base_commit or 'none')[:12]})",
    )
    return state


# --------------------------------------------------------------------------- hw next-step


def next_step(*, repo_root: Path, run_id: str) -> dict:
    """Return the next pending step packet (and mark it RUNNING). Resume-safe.

    The cursor is DERIVED from step_status, never stored — so a resume after a crash picks
    up at the first not-done step. Returns ``{"done": True}`` when all steps are done, or
    ``{"blocked": True, ...}`` if a gate has failed and not been cleared.
    """
    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)

    if state.status is RunStatus.FAILED:
        return {
            "halted": True,
            "reason": "the run was FAILED (a human rejected an irreversible HITL gate) — it does "
            "not advance; open a new run if the change should proceed",
            "run_id": run_id,
        }

    if state.status is RunStatus.GATE_FAILED:
        return {
            "blocked": True,
            "reason": "a deterministic gate failed; fix the product and re-run `hw evaluate`",
            "run_id": run_id,
        }

    if state.status is RunStatus.AWAITING_HUMAN or gates.run_blocked(repo_root, run_id, state):
        return {
            "awaiting_human": True,
            "reason": "the run is blocked on a human gate (a quality escalation or an "
            "irreversible-action HITL gate) — resolve it with `hw gate-status` after a human "
            "approves, or address the escalation",
            "run_id": run_id,
        }

    step = state.next_pending_step()
    if step is None:
        state.status = RunStatus.COMPLETED
        save_state(paths, state)
        return {"done": True, "run_id": run_id}

    state.step_status[step.id] = StepStatus.RUNNING
    state.status = RunStatus.IN_PROGRESS
    save_state(paths, state)

    produces_path = (
        str(paths.artifact(step.produces).relative_to(repo_root)) if step.produces else None
    )
    available = sorted(
        str(p.relative_to(repo_root))
        for p in paths.run_dir.glob("*.json")
        if p.name not in {"state.json"}
    )
    from hyperwilfred_core import learning

    return {
        "run_id": run_id,
        "step": step.model_dump(mode="json"),
        "lessons": learning.lessons_for_step(step.id),
        "instruction": load_instruction(step.instruction_file),
        "kind": step.kind.value,
        "product_dir": state.product_dir,
        "objective": state.objective.model_dump(mode="json"),
        "produces_path": produces_path,
        "available_inputs": available,
        "guardrails_path": str(paths.guardrails.relative_to(repo_root)),
    }


# --------------------------------------------------------------------------- hw complete-step


def complete_step(*, repo_root: Path, run_id: str, step_id: str) -> dict:
    """Mark a SINGLE/FANOUT step done after the adapter executed it.

    For a step that declares ``produces``, the named artifact MUST exist on disk (and, when
    it is a known JSON contract, validate) before the step is accepted as done — a half-done
    step cannot be silently skipped.
    """
    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)
    step = _find_step(state, step_id)

    if step.kind is StepKind.GATE:
        raise EngineError(f"step {step_id!r} is a gate — complete it via `hw evaluate`")
    if step.kind is StepKind.QUALITY:
        raise EngineError(
            f"step {step_id!r} is the quality loop — it completes via `hw quality-gate` "
            "(pass), not `hw complete-step`"
        )
    if step.kind is StepKind.HUMAN_GATE:
        raise EngineError(
            f"step {step_id!r} is a human gate — open it with `hw gate-open` and it completes "
            "via `hw gate-status` once a human approves, not `hw complete-step`"
        )

    if step.kind is StepKind.REVIEW:
        # The REVIEW critique step is read-only (no code-acceptance gate). Completion is gated on
        # the critic panel having actually run — at least one critic findings JSON recorded.
        from hyperwilfred_core import review as review_mod

        review_mod.assert_critics_recorded(repo_root=repo_root, run_id=run_id)

    plan_lint_report = None
    if step.produces:
        artifact = paths.artifact(step.produces)
        if not artifact.exists():
            raise EngineError(
                f"step {step_id!r} declares it produces {step.produces!r} but that file "
                f"does not exist at {artifact}"
            )
        if step.produces == "plan.json":
            _validate_plan(artifact)  # validate the decompose contract
            from hyperwilfred_core import plan_lint

            lint_report = plan_lint.lint_run(repo_root=repo_root, run_id=run_id)
            if lint_report.blocking:
                raise EngineError(
                    "plan lint blocked this HIGH/CRITICAL-risk plan — "
                    + " | ".join(f.message for f in lint_report.findings)
                    + " — re-cut the slices by responsibility (see the decompose step)"
                )
            plan_lint_report = lint_report

    if step.kind is StepKind.FANOUT:
        _assert_all_packets_accepted(paths)

    verify_report: dict | None = None
    if step_id == "verify":
        # Deterministic verify-step checks (requirement traceability, …) — a blocking result
        # refuses completion with the reason, exactly like a missing artifact.
        from hyperwilfred_core import verify_checks

        verify_report = verify_checks.run(repo_root=repo_root, run_id=run_id)
        if not verify_report["ok"]:
            raise EngineError("verify step blocked — " + " | ".join(verify_report["blocking"]))

    state.step_status[step_id] = StepStatus.DONE
    save_state(paths, state)
    RunStore(paths.run_dir).append_event(
        run_id=run_id,
        stage=step_id,
        status=RunEventStatus.completed,
        message=f"Step {step_id} completed",
        artifacts=[step.produces] if step.produces else [],
    )
    result: dict = {
        "run_id": run_id,
        "completed_step": step_id,
        "next": next_step(repo_root=repo_root, run_id=run_id),
    }
    if verify_report is not None:
        result["verify_checks"] = verify_report
    if plan_lint_report is not None and plan_lint_report.findings:
        result["plan_lint_warnings"] = [
            f.model_dump(mode="json") for f in plan_lint_report.findings
        ]
    return result


# --------------------------------------------------------------------------- hw fanout-plan


def fanout_plan(*, repo_root: Path, run_id: str, step_id: str = "implement") -> dict:
    """Emit one self-contained Packet per slice, grouped into dependency-safe waves.

    Reads the slices the ``decompose`` step wrote to ``plan.json``. Each packet carries its
    own allow/blocked scope (the slice's declared scope + the run's global blocked globs) and
    a known result_path the worker writes to. The engine writes every packet to disk; the
    adapter dispatches them. The engine does NOT spawn anything.
    """
    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)
    step = _find_step(state, step_id)
    if step.kind is not StepKind.FANOUT:
        raise EngineError(f"step {step_id!r} is not a fanout step")
    if not paths.plan.exists():
        raise EngineError("no plan.json — the decompose step must run before fanout")

    plan_obj = _validate_plan(paths.plan)
    guardrail = load_guardrails(paths)
    waves = plan_waves(plan_obj.slices)

    paths.packets_dir.mkdir(parents=True, exist_ok=True)
    instruction = load_instruction(step.instruction_file)
    wave_payload: list[list[dict]] = []
    for wave in waves:
        packets: list[dict] = []
        for slice_ in wave:
            packet_id = f"{slice_.id}"
            result_path = str((paths.results_dir / f"{packet_id}.json").relative_to(repo_root))
            worktree_hint = f".hw/{run_id}/worktrees/{packet_id}"
            packet = Packet(
                packet_id=packet_id,
                run_id=run_id,
                step_id=step_id,
                slice_id=slice_.id,
                title=slice_.title,
                instruction=instruction,
                allowed_globs=slice_.allowed_globs,
                blocked_globs=[*guardrail.global_blocked_globs, *slice_.blocked_globs],
                max_files=slice_.max_files,
                max_changed_lines=min(slice_.max_changed_lines, guardrail.max_changed_lines),
                max_hunk_span=guardrail.max_hunk_span,
                full_file_ratio=guardrail.full_file_ratio,
                worktree_hint=worktree_hint,
                result_path=result_path,
                generated_artifacts=slice_.generated_artifacts,
            )
            atomic_write_text(
                paths.packets_dir / f"{packet_id}.json",
                packet.model_dump_json(indent=2) + "\n",
            )
            packets.append(packet.model_dump(mode="json"))
        wave_payload.append(packets)

    RunStore(paths.run_dir).append_event(
        run_id=run_id,
        stage=step_id,
        status=RunEventStatus.running,
        message=f"Fan-out planned: {sum(len(w) for w in wave_payload)} packets "
        f"in {len(wave_payload)} wave(s)",
    )
    return {"run_id": run_id, "step_id": step_id, "waves": wave_payload}


# --------------------------------------------------------------------------- hw record-result


def record_result(
    *,
    repo_root: Path,
    run_id: str,
    packet_id: str,
    worktree: str,
    result_file: str | None = None,
    do_rollback: bool = True,
) -> dict:
    """THE ACCEPTANCE GATE. Read the worker's result from disk, run the guardrail, accept
    or hard-reject + rollback. This is the engine-level guardrail floor (S42-S43 as real
    engine code, not a prompt).
    """
    validate_identifier(packet_id, label="packet_id")
    paths = RunPaths(repo_root, run_id)
    load_state(paths)  # ensures the run exists
    packet_path = paths.packets_dir / f"{packet_id}.json"
    if not packet_path.exists():
        raise EngineError(f"no packet {packet_id!r} — run `hw fanout-plan` first")
    packet = Packet.model_validate_json(packet_path.read_text(encoding="utf-8"))
    if packet.packet_id != packet_id or packet.run_id != run_id:
        raise EngineError("packet identity does not match the requested run and packet")
    expected_result = paths.results_dir / f"{packet_id}.json"
    supplied_result = Path(packet.result_path)
    if not supplied_result.is_absolute():
        supplied_result = paths.repo_root / supplied_result
    if supplied_result.resolve(strict=False) != expected_result.resolve(strict=False):
        raise EngineError("packet result_path is outside the run results directory")

    resolved_result = result_file or packet.result_path
    verdict = guardrails.accept(
        packet_id=packet_id,
        worktree=worktree,
        allowed_globs=packet.allowed_globs,
        blocked_globs=packet.blocked_globs,
        result_file=resolved_result,
        max_changed_lines=packet.max_changed_lines,
        max_hunk_span=packet.max_hunk_span,
        full_file_ratio=packet.full_file_ratio,
        do_rollback=do_rollback,
        generated_globs=guardrails.effective_generated_globs(
            [g.path for g in packet.generated_artifacts], packet.allowed_globs
        ),
    )

    paths.verdicts_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        paths.verdicts_dir / f"{packet_id}.json", verdict.model_dump_json(indent=2) + "\n"
    )
    RunStore(paths.run_dir).append_event(
        run_id=run_id,
        stage=packet.step_id,
        status=RunEventStatus.accepted if verdict.accepted else RunEventStatus.rejected,
        message=(
            f"Packet {packet_id} accepted"
            if verdict.accepted
            else f"Packet {packet_id} REJECTED "
            f"(escapes={verdict.escapes}, violations={len(verdict.patch_violations)})"
        ),
        metadata={"accepted": str(verdict.accepted), "rolled_back": str(verdict.rolled_back)},
    )
    return {"run_id": run_id, "verdict": verdict.model_dump(mode="json")}


# --------------------------------------------------------------------------- hw evaluate


def evaluate(
    *,
    repo_root: Path,
    run_id: str,
    target: Path,
    step_id: str = "evaluate",
    config: dict | None = None,
    review_public_key_path: Path | None = None,
) -> dict:
    """Run the deterministic gates over *target*. A failing gate HARD-BLOCKS — no advance.

    The 0-5 'best version after N iterations' fallback NEVER applies to build/lint/type/test
    (precedence rule, spec §2). On failure the engine sets status GATE_FAILED and raises
    GateFailure so the CLI exits non-zero; the human/adapter must fix and re-run.
    """
    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)
    declared_gates = {s.id for s in steps_for(state.mode) if s.kind is StepKind.GATE}
    if step_id not in declared_gates:
        allowed = ", ".join(f"--step {sid}" for sid in sorted(declared_gates)) or "none"
        raise EngineError(
            f"hw evaluate only accepts the mode-declared deterministic gate "
            f"({state.mode.value}: {allowed}); step {step_id!r} must use its own workflow. "
            "Run `hw next-step` for the required instructions"
        )
    if _find_step(state, step_id).kind is not StepKind.GATE:
        raise EngineError(
            f"step {step_id!r} is not recorded as a deterministic gate; "
            "restore the run's declared step graph before evaluating"
        )

    report = run_all(
        target,
        evaluators_for_stack(state.objective.stack),
        task_id=state.objective.id,
        run_id=run_id,
        # base_commit lets the flaky-test gate find the tests this run added or changed.
        config={**(config or {}), "base_commit": state.base_commit},
    )

    # IMPROVE regression guard: in addition to the chain (which catches a FAILING pre-existing
    # test), compare against the green baseline captured at comprehend so a DELETED/SKIPPED/HIDDEN
    # pre-existing test cannot neutralise the gate. Python baselines fail closed when missing;
    # Node retains its native build/lint/test chain until per-test baselines are supported.
    baseline = None
    if step_id == "regression" and state.objective.stack is Stack.PYTHON:
        from hyperwilfred_core import regression

        baseline = regression.load_baseline(paths.artifact(regression.BASELINE_FILE))
        if baseline is None:
            report.checks.append(regression.missing_baseline_check())
        else:
            transitions = regression.verified_semantic_transitions(
                paths.run_dir, run_id=run_id, target=target, baseline=baseline,
                public_key_path=review_public_key_path,
            )
            unsigned = regression.policy_approved_paths(paths.run_dir) & set(transitions)
            report.checks.append(
                regression.compare(target, baseline, approved_transitions=transitions,
                                   policy_approved=unsigned)
                if unsigned else
                regression.compare(target, baseline, approved_transitions=transitions)
            )

    report.checks.append(_engine_state_check(repo_root, target))

    # Human-declared environment-only failures (committed before the run) may turn the plain
    # pytest ERROR into a WARNING — never a regression, never a new/edited test, never any other
    # check. The regression comparison above is untouched by this.
    report.checks = [
        known_failures.apply(
            c,
            repo_root=repo_root,
            target=target,
            base_commit=state.base_commit,
            baseline_passing=set(baseline.get("passing_ids", [])) if baseline else None,
        )
        for c in report.checks
    ]
    report.checks = _excuse_ci_parity_pytest(report.checks, target)
    report.blocks_merge = any(
        (not c.passed) and c.severity in (Severity.ERROR, Severity.CRITICAL) for c in report.checks
    )

    store = RunStore(paths.run_dir)
    evidence = store.write_evidence("verification.json", report.model_dump_json(indent=2) + "\n")
    # mirror to the step's declared artifact name for the resume scan
    atomic_write_text(paths.artifact("verification.json"), report.model_dump_json(indent=2) + "\n")

    if report.blocks_merge:
        state.step_status[step_id] = StepStatus.FAILED
        state.status = RunStatus.GATE_FAILED
        save_state(paths, state)
        store.append_event(
            run_id=run_id,
            stage=step_id,
            status=RunEventStatus.gate_failed,
            message="Deterministic gate FAILED — hard block",
            artifacts=[str(evidence)],
        )
        raise GateFailure(
            "deterministic gate failed: "
            + "; ".join(f"{c.name}={c.details or 'failed'}" for c in report.checks if not c.passed)
        )

    state.step_status[step_id] = StepStatus.DONE
    if state.status is RunStatus.GATE_FAILED:
        state.status = RunStatus.IN_PROGRESS
    save_state(paths, state)
    store.append_event(
        run_id=run_id,
        stage=step_id,
        status=RunEventStatus.gate_passed,
        message="Deterministic gates passed",
        artifacts=[str(evidence)],
    )
    return {"run_id": run_id, "report": report.model_dump(mode="json"), "passed": True}


def _excuse_ci_parity_pytest(checks: list[CheckResult], target: Path) -> list[CheckResult]:
    """Carry an accepted known-failures declaration over to the CI-parity pytest steps.

    When the pytest gate failed only on human-declared environment-only failures (and
    ``known_failures.apply`` therefore turned it into a WARNING), the same tests make the CI
    replay's pytest step fail too. A CI-parity check is excused only when every blocking finding
    is a single-command pytest step whose failing test ids were parsed and are all among the
    ids the pytest gate tolerated. Anything else — another failing step, an unparsed failure, a
    different failing test — keeps it blocking.
    """
    from hyperwilfred_core.evaluators.ci_parity import pytest_failing_ids

    pytest_check = next((c for c in checks if c.name == "pytest"), None)
    if not (
        pytest_check is not None
        and pytest_check.passed
        and pytest_check.severity is Severity.WARNING
        and "tolerated as human-declared" in (pytest_check.details or "")
    ):
        return checks
    top = known_failures._toplevel(Path(target))
    if top is None:
        return checks
    tolerated = {
        c
        for f in pytest_check.findings
        if f.rule_id in (known_failures.RULE_FAILED, known_failures.RULE_ERROR)
        for c in [known_failures.canonical_node_id(f.file, Path(target).resolve(), top)]
        if c
    }
    out: list[CheckResult] = []
    for c in checks:
        blocking = [f for f in c.findings if f.severity in (Severity.ERROR, Severity.CRITICAL)]
        if c.name == "ci-parity" and not c.passed and blocking:
            ids_per_step = [pytest_failing_ids(f) for f in blocking]
            if all(ids is not None and set(ids) <= tolerated for ids in ids_per_step):
                c = c.model_copy(
                    update={
                        "passed": True,
                        "severity": Severity.WARNING,
                        "details": "ci-parity: the failing CI pytest step(s) fail only on tests "
                        "the pytest gate tolerated as human-declared environment-only failures "
                        f"({known_failures.DECLARATION_FILE})\n{c.details or ''}",
                    }
                )
        out.append(c)
    return out


def _engine_state_check(repo_root: Path, target: Path) -> CheckResult:
    """Fail when git tracks ``.hw/`` run state in the engine repo or the product's repo.

    Committed run artifacts turn the host repo's own CI red (its scope checks deny them) and
    leak run ledgers into history, so a tracked ``.hw/`` blocks like any failing gate.
    """
    tracked: list[str] = []
    for root in (repo_root, target):
        if not root.exists():
            continue
        try:
            found = guardrails.tracked_engine_state(root)
        except guardrails.GuardrailError as exc:
            return CheckResult(
                kind=CheckKind.CONSISTENCY,
                name="engine-state-untracked",
                passed=False,
                severity=Severity.ERROR,
                details=f"engine-state-untracked: cannot list tracked files ({exc}); "
                "repair the repository and re-run",
            )
        tracked += [t for t in found if t not in tracked]
    if not tracked:
        return CheckResult(
            kind=CheckKind.CONSISTENCY,
            name="engine-state-untracked",
            passed=True,
            details="engine-state-untracked: no .hw/ files are tracked by git",
        )
    return CheckResult(
        kind=CheckKind.CONSISTENCY,
        name="engine-state-untracked",
        passed=False,
        severity=Severity.ERROR,
        details="engine-state-untracked: git tracks HyperWilfred run state ("
        + ", ".join(tracked[:5])
        + (", …" if len(tracked) > 5 else "")
        + "). A human must remove it from the index outside any work packet "
        "(`git rm -r --cached .hw`; the acceptance gate rejects that change from a worker) "
        "— it stays on disk.",
    )


# --------------------------------------------------------------------------- hw rollback


def rollback(*, worktree: str) -> dict:
    """Explicitly roll a worktree back to a byte-clean HEAD."""
    guardrails.rollback(worktree)
    return {"worktree": worktree, "clean": guardrails.is_clean(worktree)}


# --------------------------------------------------------------------------- hw regression-baseline


def regression_baseline(*, repo_root: Path, run_id: str, target: Path) -> dict:
    """Snapshot the green Python test set for the IMPROVE regression guard (at comprehend).

    Records the collected test node ids + passing count under ``target`` so the regression gate
    can later prove the SAME tests still run and pass — closing the delete/skip/hide bypass.
    """
    from hyperwilfred_core import regression

    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)  # ensures the run exists and fixes the declaration trust point
    if state.objective.stack is not Stack.PYTHON:
        raise EngineError(
            "per-test regression baselines are only supported for Python; for Node, run "
            "`npm run build`, `npm run lint` (if defined), and `npm run test` before editing, "
            "record the results in comprehension.md, then use `hw evaluate --step regression` "
            "after editing"
        )
    try:
        baseline, pytest_check = regression.baseline_candidate(target)
    except regression.RegressionError as exc:
        raise EngineError(str(exc)) from exc
    if not pytest_check.passed:
        tolerated = known_failures.apply(
            pytest_check,
            repo_root=repo_root,
            target=target,
            base_commit=state.base_commit,
            baseline_passing=set(baseline["passing_ids"]),
        )
        if not tolerated.passed or tolerated.severity is not Severity.WARNING:
            raise EngineError(
                "cannot capture regression baseline: pytest did not pass cleanly and every "
                f"failure was not a verified pre-run environment declaration; {tolerated.details}"
            )
        baseline["tolerated_known_failures"] = sorted(
            finding.file for finding in pytest_check.findings
        )
    atomic_write_text(
        paths.artifact(regression.BASELINE_FILE), json.dumps(baseline, indent=2) + "\n"
    )
    RunStore(paths.run_dir).append_event(
        run_id=run_id,
        stage="comprehend",
        status=RunEventStatus.running,
        message=f"Regression baseline captured: {baseline['passing']} passing test(s), "
        f"{len(baseline['test_file_hashes'])} test file(s)",
    )
    return {"run_id": run_id, "baseline": baseline}


# --------------------------------------------------------------------------- helpers


def _find_step(state: RunState, step_id: str):
    for step in state.steps:
        if step.id == step_id:
            return step
    raise EngineError(f"unknown step {step_id!r}")


def _validate_plan(plan_path: Path) -> Plan:
    try:
        plan_obj = Plan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise EngineError(f"plan.json is invalid: {exc}") from exc
    if plan_obj.product_dir != ".":
        validate_relative_path(plan_obj.product_dir, label="product_dir")
    slice_ids = {validate_identifier(item.id, label="slice id") for item in plan_obj.slices}
    if len(slice_ids) != len(plan_obj.slices):
        raise EngineError("plan.json is invalid: slice ids must be unique")
    for slice_ in plan_obj.slices:
        for dependency in slice_.depends_on:
            validate_identifier(dependency, label="slice dependency")
            if dependency not in slice_ids:
                raise EngineError(f"plan.json is invalid: unknown dependency {dependency!r}")
        for pattern in (*slice_.allowed_globs, *slice_.blocked_globs):
            validate_relative_path(pattern, label="slice path pattern", allow_glob=True)
        for gen in slice_.generated_artifacts:
            # A generated-artifact exemption can never widen a slice's write scope.
            if not any(guardrails.matches(gen.path, g) for g in slice_.allowed_globs):
                raise EngineError(
                    f"plan.json is invalid: slice {slice_.id!r} declares generated artifact "
                    f"{gen.path!r} outside its allowed_globs {slice_.allowed_globs}"
                )
    return plan_obj


def _assert_all_packets_accepted(paths: RunPaths) -> None:
    packet_files = sorted(paths.packets_dir.glob("*.json")) if paths.packets_dir.exists() else []
    if not packet_files:
        raise EngineError("fanout step has no packets — run `hw fanout-plan` first")
    from hyperwilfred_core.schemas import AcceptanceVerdict

    for pf in packet_files:
        pid = pf.stem
        vf = paths.verdicts_dir / f"{pid}.json"
        if not vf.exists():
            raise EngineError(f"packet {pid!r} has no verdict yet — run `hw record-result`")
        verdict = AcceptanceVerdict.model_validate_json(vf.read_text(encoding="utf-8"))
        if not verdict.accepted:
            raise EngineError(f"packet {pid!r} was rejected — cannot complete the fanout step")


__all__ = [
    "EngineError",
    "GateFailure",
    "RunPaths",
    "classify_tier",
    "complete_step",
    "evaluate",
    "fanout_plan",
    "load_state",
    "next_step",
    "plan",
    "record_result",
    "regression_baseline",
    "rollback",
]
