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

import functools
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from hyperwilfred_core import gates, guardrails, known_failures, regression
from hyperwilfred_core.evaluators import evaluators_for_stack, run_all
from hyperwilfred_core.evaluators.verification import (
    CheckKind,
    CheckResult,
    Severity,
    VerificationReport,
)
from hyperwilfred_core.foundry_storage import fault
from hyperwilfred_core.path_safety import (
    UnstableFileError,
    contained_path,
    stable_regular_bytes,
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
        # Distinguish an explicit ``products/<run_id>`` declaration from the identical
        # placeholder assigned to legacy/default callers.  Explicit targets are exact at birth.
        deterministic_target=resolved_product_dir if product_dir is not None else None,
        steps=steps,
        step_status={s.id: StepStatus.PENDING for s in steps},
        base_commit=known_failures.head_commit(repo_root),
    )
    collection: bytes | None = None
    if mode is BuildMode.IMPROVE:
        # A declared target through a link is refused.
        declared_target = _plan_declared_target(repo_root, state)
        if (declared_target is not None and state.base_commit is not None
                and objective.stack is Stack.PYTHON):
            collection = regression.base_collection_record(
                paths.repo_root / declared_target, state.base_commit, declared_target)
            state.base_collection_sha256 = hashlib.sha256(collection).hexdigest()

    # Run state lives inside the host repo; keep it out of that repo's commits. Done before the
    # run folder exists so a failure here never leaves a run root without state.
    guardrails.ensure_engine_state_excluded(repo_root)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    if collection is not None:
        atomic_write_text(paths.artifact(regression.COLLECTION_FILE), collection.decode("utf-8"))
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
        require_accepted_head(repo_root=repo_root, run_id=run_id, stage=f"step {step_id}")

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


# -------------------------------------------------------------------------- packet worktree binding


def _worktree_slot(packet_id: str, *, kind: str, iteration: int | None = None) -> str:
    return f"{kind}:{iteration}:{packet_id}" if kind == "patch" else f"build:{packet_id}"


def _registration_accepted(paths: RunPaths, slot: str) -> bool:
    if slot.startswith("build:"):
        previous = paths.verdicts_dir / f"{slot.removeprefix('build:')}.json"
    elif slot.startswith("patch:"):
        _, old_iteration, _ = slot.split(":", 2)
        previous = paths.run_dir / "quality" / f"iter-{old_iteration}" / "patch-verdict.json"
    else:
        raise EngineError("worktree registration has an unknown packet kind")
    try:
        return json.loads(previous.read_text(encoding="utf-8"))["accepted"] is True
    except (OSError, ValueError, KeyError, TypeError):
        return False


_PIN_KEYS = ("head", "ignore_rules_sha256", "hidden_index_sha256")
_PIN_REFUSALS = {
    "head": "worktree has commits made after registration; acceptance inspects uncommitted "
            "work only",
    "ignore_rules_sha256": "worktree's local ignore rules changed after registration",
    "hidden_index_sha256": "worktree hides tracked files from status since registration",
}


# Recorded after a packet is accepted: what its worktree changed relative to its pinned HEAD,
# as JSON ``{path: [mode, object id] | null}`` (null: deleted). See ``_require_expected_head``.
_ACCEPTED_CHANGES = "accepted_changes"


def _tree_of(registration: dict) -> dict:
    """The location part of a registration (which checkout), without its acceptance pins."""
    return {key: value for key, value in registration.items()
            if key not in _PIN_KEYS and key != _ACCEPTED_CHANGES}


def _unexplained_head_paths(paths: RunPaths, state: RunState, worktree: str) -> list[str]:
    """Paths where *worktree*'s HEAD differs from the base commit and no accepted packet left it.

    Every path where HEAD differs from the run's base commit must hold exactly what an accepted
    packet of this run left there (however the adapter committed or merged it).
    """
    if not state.base_commit:
        return []
    try:
        head = guardrails.tree_entries(worktree, "HEAD")
        base = guardrails.tree_entries(worktree, state.base_commit)
    except (OSError, ValueError) as exc:
        raise EngineError(str(exc)) from exc
    accepted: dict[str, set[tuple[str, str] | None]] = {}
    for slot, registered in state.packet_worktrees.items():
        recorded = registered.get(_ACCEPTED_CHANGES)
        if not isinstance(recorded, str) or not _registration_accepted(paths, slot):
            continue
        for path, entry in json.loads(recorded).items():
            accepted.setdefault(path, set()).add(tuple(entry) if entry else None)
    return sorted(
        path for path in set(head) | set(base)
        if head.get(path) != base.get(path) and head.get(path) not in accepted.get(path, set())
    )


def _require_expected_head(paths: RunPaths, state: RunState, worktree: str) -> None:
    """Refuse a worktree whose HEAD holds work this run never accepted.

    Acceptance inspects uncommitted work against the HEAD pinned at registration, so a commit
    the worker made BEFORE registration would never be inspected.
    """
    unexplained = _unexplained_head_paths(paths, state, worktree)
    if unexplained:
        raise EngineError(
            "worktree HEAD holds changes since the run's base commit that no accepted packet "
            f"made ({unexplained[:5]}); commits made before registration are never inspected "
            "by the acceptance gate"
        )


def require_accepted_head(*, repo_root: Path, run_id: str, stage: str = "") -> None:
    """Refuse when the run's product checkout HEAD holds work no accepted packet made.

    Registration checks HEAD only when a worktree is bound to the NEXT packet, so the commit the
    adapter makes after the LAST accepted packet (or any commit added later) is checked here,
    with the same rule, before the implement step completes, the quality loop passes and the
    ship gate opens or is approved.
    """
    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)
    unexplained = _unexplained_head_paths(paths, state, str(paths.repo_root))
    if unexplained:
        where = f" at {stage}" if stage else ""
        raise EngineError(
            f"refused{where}: the run's checkout HEAD holds changes since the base commit that "
            f"no accepted packet made ({unexplained[:5]}); commit exactly the accepted work, "
            "and escalate to the owner instead of rewriting history to get past this"
        )


def record_accepted_changes(
    paths: RunPaths, state: RunState, slot: str, changes: dict[str, list[str] | None],
) -> None:
    """Remember what an accepted packet changed, so its commit can carry the next packet.

    *changes* come from the same read of the worktree the gate judged
    (``guardrails.accept(accepted_changes_out=...)``). The state is re-read just before the
    write so packets of one wave recorded one after another do not drop each other's record.
    """
    recorded = json.dumps(changes, sort_keys=True)
    if slot in state.packet_worktrees:
        state.packet_worktrees[slot][_ACCEPTED_CHANGES] = recorded
    fresh = load_state(paths)
    registration = fresh.packet_worktrees.get(slot)
    if registration is None:
        raise EngineError("packet worktree registration disappeared while recording acceptance")
    registration[_ACCEPTED_CHANGES] = recorded
    save_state(paths, fresh)


def register_worktree(
    *, repo_root: Path, run_id: str, packet_id: str, worktree: str,
    kind: str = "build",
) -> dict:
    """Bind a clean, live Git worktree to one emitted packet before dispatch."""
    validate_identifier(packet_id, label="packet_id")
    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)
    if kind == "build":
        packet_path = paths.packets_dir / f"{packet_id}.json"
        if not packet_path.is_file():
            raise EngineError("no build packet to register")
        packet = Packet.model_validate_json(packet_path.read_text(encoding="utf-8"))
        if packet.packet_id != packet_id or packet.run_id != run_id:
            raise EngineError("packet identity does not match this run")
        iteration = None
    elif kind == "patch":
        if state.quality is None:
            raise EngineError("no quality loop for a patch packet")
        iteration = state.quality.iteration
        packet_path = paths.run_dir / "quality" / f"iter-{iteration}" / "patch-packet.json"
        if not packet_path.is_file():
            raise EngineError("no patch packet to register")
        from hyperwilfred_core.schemas import PatchPacket
        patch_packet = PatchPacket.model_validate_json(packet_path.read_text(encoding="utf-8"))
        if (patch_packet.packet_id != packet_id or patch_packet.run_id != run_id
                or patch_packet.iteration != iteration):
            raise EngineError("patch packet identity does not match this iteration")
    else:
        raise EngineError("worktree registration kind must be build or patch")
    try:
        identity = guardrails.worktree_identity(paths.repo_root, worktree, state.base_commit)
        dirty = [path for path in guardrails.changed_paths(str(identity["path"]))
                 if not path.startswith(".hw/") and path != ".hw"]
    except (OSError, ValueError) as exc:
        raise EngineError(str(exc)) from exc
    if dirty:
        raise EngineError("packet worktree must be clean before registration")
    slot = _worktree_slot(packet_id, kind=kind, iteration=iteration)
    existing = state.packet_worktrees.get(slot)
    if existing is not None and _tree_of(existing) != identity:
        raise EngineError("packet worktree registration cannot be replaced")
    for other, registered in state.packet_worktrees.items():
        if (other != slot and _tree_of(registered) == identity
                and not _registration_accepted(paths, other)):
            raise EngineError("worktree is already registered to another active packet")
    for run_dir in (paths.repo_root / ".hw").iterdir():
        if run_dir.name == run_id or run_dir.is_symlink() or not (run_dir / "state.json").is_file():
            continue
        try:
            other_paths = RunPaths(paths.repo_root, run_dir.name)
            other_state = load_state(other_paths)
        except (OSError, ValueError) as exc:
            raise EngineError("another run's worktree registrations cannot be verified") from exc
        for other, registered in other_state.packet_worktrees.items():
            if (_tree_of(registered) == identity
                    and not _registration_accepted(other_paths, other)):
                raise EngineError("worktree is already registered to another active packet")
    if existing is None:
        _require_expected_head(paths, state, str(identity["path"]))
        # Pinned once: registering again never moves the pins to a later commit.
        try:
            pins = guardrails.worktree_pins(str(identity["path"]))
        except (OSError, ValueError) as exc:
            raise EngineError(str(exc)) from exc
        state.packet_worktrees[slot] = {**identity, **pins}
        save_state(paths, state)
    return {"run_id": run_id, "packet_id": packet_id, "kind": kind,
            "worktree": identity["path"], "registered": True}


def require_packet_worktree(
    *, repo_root: Path, state: RunState, packet_id: str, worktree: str,
    kind: str = "build", iteration: int | None = None,
) -> str:
    """Reject another packet's worktree or a stale/replaced Git registration."""
    slot = _worktree_slot(packet_id, kind=kind, iteration=iteration)
    try:
        current = guardrails.worktree_identity(repo_root, worktree, state.base_commit)
    except (OSError, ValueError) as exc:
        raise EngineError(str(exc)) from exc
    expected = state.packet_worktrees.get(slot)
    if expected is None:
        raise EngineError("packet has no registered worktree")
    if current != _tree_of(expected):
        raise EngineError("worktree is not bound to this packet or its registration is stale")
    try:
        pins = guardrails.worktree_pins(str(current["path"]))
    except (OSError, ValueError) as exc:
        raise EngineError(str(exc)) from exc
    for key in _PIN_KEYS:
        if pins[key] != expected.get(key):
            raise EngineError(_PIN_REFUSALS[key])
    return str(current["path"])


def require_packet_result(path: Path, root: Path) -> bytes:
    """Require a stable, nonempty regular result at this packet's known disk path."""
    try:
        payload = stable_regular_bytes(path, root=root, label="packet result")
    except (OSError, UnstableFileError) as exc:
        raise EngineError(f"packet result file is missing or unsafe: {exc}") from exc
    if not payload:
        raise EngineError("packet result file is empty")
    return payload


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
    state = load_state(paths)  # ensures the run exists
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

    if result_file is not None:
        requested = Path(result_file)
        if not requested.is_absolute():
            requested = paths.repo_root / requested
        if requested.absolute() != expected_result.absolute():
            raise EngineError("result file must match this packet's result_path")
    # Walk from the run root: a results directory replaced by a link is refused too.
    result_payload = require_packet_result(expected_result, paths.repo_root)
    bound_worktree = require_packet_worktree(
        repo_root=paths.repo_root, state=state, packet_id=packet_id, worktree=worktree,
    )
    accepted_changes: dict[str, list[str] | None] = {}
    verdict = guardrails.accept(
        packet_id=packet_id,
        worktree=bound_worktree,
        accepted_changes_out=accepted_changes,
        allowed_globs=packet.allowed_globs,
        blocked_globs=packet.blocked_globs,
        result_file=None,
        max_changed_lines=packet.max_changed_lines,
        max_hunk_span=packet.max_hunk_span,
        full_file_ratio=packet.full_file_ratio,
        do_rollback=do_rollback,
        generated_globs=guardrails.effective_generated_globs(
            [g.path for g in packet.generated_artifacts], packet.allowed_globs
        ),
    )

    verdict.result_read_from_disk = True
    verdict.result_bytes = len(result_payload)
    if verdict.accepted:
        record_accepted_changes(paths, state, _worktree_slot(packet_id, kind="build"),
                                accepted_changes)
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
    target, target_pin = _declared_product_target(repo_root, state, target)

    # Freeze the bytes before loading a baseline or starting any evaluator.  The deterministic
    # result is publishable only if the same digest is observed after the complete chain.
    from hyperwilfred_core import quality

    def reject_unstable_snapshot(reason: str) -> None:
        state.step_status[step_id] = StepStatus.FAILED
        state.status = RunStatus.GATE_FAILED
        save_state(paths, state)
        raise GateFailure(f"deterministic gate rejected: {reason}")

    try:
        pre_check_snapshot = quality.product_snapshot_sha256(repo_root, state)
    except quality.QualityError as exc:
        reject_unstable_snapshot(f"pre-check product snapshot is unavailable: {exc}")

    baseline = None
    baseline_error = None
    if step_id == "regression" and state.objective.stack is Stack.PYTHON:
        from hyperwilfred_core import regression

        baseline_path = paths.artifact(regression.BASELINE_FILE)
        if baseline_path.exists() or state.regression_baseline_sha256 is not None:
            baseline, baseline_error = regression.checked_baseline(
                baseline_path, target, state.regression_baseline_sha256,
            )
    report = (VerificationReport(
        task_id=state.objective.id, run_id=run_id, completed_at=datetime.now(UTC),
        checks=[baseline_error], blocks_merge=True,
    ) if baseline_error else run_all(
        target, evaluators_for_stack(state.objective.stack), task_id=state.objective.id,
        run_id=run_id,
        # base_commit lets the flaky-test gate find the tests this run added or changed.
        config={**(config or {}), "base_commit": state.base_commit},
    ))

    # IMPROVE regression guard: in addition to the chain (which catches a FAILING pre-existing
    # test), compare against the green baseline captured at comprehend so a DELETED/SKIPPED/HIDDEN
    # pre-existing test cannot neutralise the gate. Python baselines fail closed when missing;
    # Node retains its native build/lint/test chain until per-test baselines are supported.
    if step_id == "regression" and state.objective.stack is Stack.PYTHON:
        from hyperwilfred_core import control_evolution, regression

        if baseline_error is None and baseline is None:
            report.checks.append(regression.missing_baseline_check())
        elif baseline is not None:
            transitions = regression.verified_semantic_transitions(
                paths.run_dir, run_id=run_id, target=target, baseline=baseline,
                public_key_path=review_public_key_path, repo_root=repo_root,
            )
            # Only the engine knows both modules: it binds the control authority to this run.
            authority = functools.partial(
                control_evolution.verified_control_transition, repo_root=repo_root,
                run_id=run_id, target=target, baseline=baseline, base_commit=state.base_commit,
                baseline_sha256=state.regression_baseline_sha256,
                review_public_key_path=review_public_key_path,
            )
            unsigned = regression.policy_approved_paths(paths.run_dir) & set(transitions)
            report.checks.append(
                regression.compare(
                    target, baseline, approved_transitions=transitions,
                    base_commit=state.base_commit, control_authority=authority,
                    policy_approved=unsigned or None,
                )
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

    snapshot_failure = None
    try:
        post_check_snapshot = quality.product_snapshot_sha256(repo_root, state)
        if post_check_snapshot != pre_check_snapshot:
            snapshot_failure = "product bytes changed while checks were running"
    except quality.QualityError as exc:
        snapshot_failure = f"post-check product snapshot is unavailable: {exc}"
    if snapshot_failure is not None:
        report.checks.append(CheckResult(
            kind=CheckKind.CONSISTENCY,
            name="product-snapshot-stability",
            passed=False,
            severity=Severity.CRITICAL,
            details=snapshot_failure,
        ))
        report.blocks_merge = True

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

    atomic_write_text(
        paths.artifact("deterministic-scope.sha256"),
        pre_check_snapshot + "\n",
    )
    if target_pin is not None:
        state.deterministic_target = target_pin
    state.deterministic_evidence_emitted = True
    state.deterministic_scope_sha256 = pre_check_snapshot
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


_BASELINE_TRANSACTION_FILE = "regression_baseline.transaction.json"


def _baseline_transaction(paths: RunPaths) -> dict | None:
    transaction_path = paths.artifact(_BASELINE_TRANSACTION_FILE)
    if not transaction_path.exists():
        return None
    if transaction_path.is_symlink() or not transaction_path.is_file():
        raise EngineError("regression baseline transaction is not a regular file")
    try:
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EngineError("regression baseline transaction is unreadable") from exc
    if not isinstance(transaction, dict):
        raise EngineError("regression baseline transaction is invalid")
    return transaction


def _baseline_request(
    *, operation: str, run_id: str, source_run_id: str, source_file: str,
    request_target: str, payload: bytes, recorded_at: str | None = None,
) -> dict:
    return {
        "schema_version": "wilfred-regression-baseline-transaction/v1",
        "status": "prepared",
        "operation": operation,
        "run_id": run_id,
        "source_run_id": source_run_id,
        "source_file": source_file,
        "request_target": request_target,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "recorded_at": recorded_at or datetime.now(UTC).isoformat(),
        "payload": payload.decode("utf-8"),
    }


def _finish_baseline_transaction(
    *, paths: RunPaths, state: RunState, request: dict, existing: dict | None,
) -> tuple[dict, bool]:
    """Prepare or recover one exact baseline publication without re-running its producer."""
    transaction = existing or request
    identity = ("schema_version", "operation", "run_id", "source_run_id", "source_file",
                "request_target", "sha256", "recorded_at", "payload")
    if existing is not None and (
            existing.get("status") not in {"prepared", "finalized"}
            or any(existing.get(key) != request.get(key) for key in identity)):
        raise EngineError("regression baseline recovery conflicts with the prepared operation")
    try:
        payload = transaction["payload"].encode("utf-8")
        baseline = json.loads(payload)
        recorded_at = datetime.fromisoformat(transaction["recorded_at"])
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EngineError("regression baseline transaction evidence is invalid") from exc
    if (not isinstance(baseline, dict)
            or hashlib.sha256(payload).hexdigest() != transaction.get("sha256")):
        raise EngineError("regression baseline transaction bytes do not match their SHA-256")

    if existing is None:
        atomic_write_text(
            paths.artifact(_BASELINE_TRANSACTION_FILE),
            json.dumps(transaction, indent=2) + "\n",
        )
    fault("before_baseline_write")
    baseline_path = paths.artifact(regression.BASELINE_FILE)
    if baseline_path.is_symlink():
        raise EngineError("existing regression baseline must not be a symbolic link")
    if baseline_path.exists():
        if not baseline_path.is_file() or baseline_path.read_bytes() != payload:
            raise EngineError("existing regression baseline conflicts with prepared bytes")
    else:
        atomic_write_text(baseline_path, payload.decode("utf-8"))
    fault("after_baseline_write")
    fault("before_baseline_pin")

    source_run_id = transaction["source_run_id"]
    source_file = transaction["source_file"]
    provenance = (
        state.regression_baseline_source_run_id,
        state.regression_baseline_source_file,
        state.regression_baseline_recorded_at,
    )
    expected = (source_run_id, source_file, recorded_at)
    newly_pinned = state.regression_baseline_sha256 is None
    if state.regression_baseline_sha256 is not None:
        if state.regression_baseline_sha256 != transaction["sha256"] or provenance != expected:
            raise EngineError("run state conflicts with prepared regression baseline evidence")
    elif any(value is not None for value in provenance):
        raise EngineError("run state contains partial regression baseline provenance")
    else:
        state.regression_baseline_sha256 = transaction["sha256"]
        state.regression_baseline_source_run_id = source_run_id
        state.regression_baseline_source_file = source_file
        state.regression_baseline_recorded_at = recorded_at
        save_state(paths, state)
    fault("after_baseline_pin")
    if transaction.get("status") != "finalized":
        transaction = {**transaction, "status": "finalized"}
        atomic_write_text(
            paths.artifact(_BASELINE_TRANSACTION_FILE),
            json.dumps(transaction, indent=2) + "\n",
        )
    return baseline, newly_pinned


def _baseline_destination(paths: RunPaths, state: RunState) -> None:
    if state.mode is not BuildMode.IMPROVE:
        raise EngineError("regression baseline requires an IMPROVE run")
    comprehend = state.step_status.get("comprehend", StepStatus.PENDING)
    comprehend_index = next(i for i, step in enumerate(state.steps) if step.id == "comprehend")
    later_advanced = any(
        state.step_status.get(step.id, StepStatus.PENDING) is not StepStatus.PENDING
        for step in state.steps[comprehend_index + 1:]
    )
    if comprehend not in {StepStatus.PENDING, StepStatus.RUNNING} or later_advanced:
        raise EngineError("regression baseline is allowed only before implementation")


def regression_baseline(*, repo_root: Path, run_id: str, target: Path) -> dict:
    """Snapshot the green Python test set for the IMPROVE regression guard (at comprehend).

    Records the collected test node ids + passing count under ``target`` so the regression gate
    can later prove the SAME tests still run and pass — closing the delete/skip/hide bypass.
    """
    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)  # ensures the run exists and fixes the declaration trust point
    _baseline_destination(paths, state)
    if state.objective.stack is not Stack.PYTHON:
        raise EngineError(
            "per-test regression baselines are only supported for Python; for Node, run "
            "`npm run build`, `npm run lint` (if defined), and `npm run test` before editing, "
            "record the results in comprehension.md, then use `hw evaluate --step regression` "
            "after editing"
        )
    request_target = _plan_declared_target(repo_root, state)
    if request_target is None:
        raise EngineError(_UNDECLARED_BASELINE_TARGET)
    target, _ = _declared_product_target(repo_root, state, target)
    source_file = paths.artifact(regression.BASELINE_FILE).relative_to(
        repo_root.resolve()
    ).as_posix()
    existing = _baseline_transaction(paths)
    if existing is not None:
        prepared = str(existing.get("payload", "")).encode("utf-8")
        if state.regression_baseline_sha256 is None:
            # Recovery would compare the transaction with itself: nothing pinned vouches for it.
            baseline_path = paths.artifact(regression.BASELINE_FILE)
            if baseline_path.is_symlink() or (baseline_path.exists() and (
                    not baseline_path.is_file() or baseline_path.read_bytes() != prepared)):
                raise EngineError("existing regression baseline conflicts with prepared bytes")
            raise EngineError(
                "regression baseline capture stopped before its SHA-256 was pinned in run "
                "state, so the prepared bytes cannot be trusted without running pytest again; "
                f"a human must remove {_BASELINE_TRANSACTION_FILE} and "
                f"{regression.BASELINE_FILE} from {paths.run_dir} and capture again"
            )
        request = _baseline_request(
            operation="capture", run_id=run_id, source_run_id=run_id,
            source_file=source_file, request_target=request_target,
            payload=prepared, recorded_at=existing.get("recorded_at"),
        )
        baseline, _ = _finish_baseline_transaction(
            paths=paths, state=state, request=request, existing=existing,
        )
        return {"run_id": run_id, "baseline": baseline, "recovered": True}
    if (paths.artifact(regression.BASELINE_FILE).exists()
            or state.regression_baseline_sha256 is not None):
        raise EngineError("regression baseline is immutable after its first capture")
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
    try:
        # After the known-failure decision, so an undeclared failure is reported as such.
        # Also refuses a test that did not pass unless it is declared at the base commit, and
        # a test collected from the base commit at plan time that is not collected now.
        declared = regression.require_base_commit_witness(
            target, state.base_commit, baseline, repo_root,
            functools.partial(_planned_collection, paths, state, request_target))
    except regression.RegressionError as exc:
        raise EngineError(str(exc)) from exc
    if "vanished_since_plan" in baseline:
        baseline["base_collection_sha256"] = state.base_collection_sha256
    if declared is not None:
        baseline["declared_not_passing"] = declared
    payload = (json.dumps(baseline, indent=2) + "\n").encode("utf-8")
    request = _baseline_request(
        operation="capture", run_id=run_id, source_run_id=run_id,
        source_file=source_file, request_target=request_target, payload=payload,
    )
    baseline, newly_pinned = _finish_baseline_transaction(
        paths=paths, state=state, request=request, existing=None,
    )
    if newly_pinned:
        RunStore(paths.run_dir).append_event(
            run_id=run_id,
            stage="comprehend",
            status=RunEventStatus.running,
            message=f"Regression baseline captured: {baseline['passing']} passing test(s), "
            f"{len(baseline['test_file_hashes'])} test file(s), "
            f"{len(baseline.get('declared_not_passing') or [])} declared not-passing test(s)",
        )
    return {"run_id": run_id, "baseline": baseline}


_REPLAN = ("plan a new run (`hw plan`) from a clean checkout of the base commit and capture "
           "its baseline there")


def _planned_collection(paths: RunPaths, state: RunState, declared_target: str) -> list[str]:
    """The ids the base commit collected when the run was planned, from the record whose
    SHA-256 ``hw plan`` pinned in run state; a missing, unpinned, changed or unavailable
    record is a refusal (fail closed)."""
    record = paths.artifact(regression.COLLECTION_FILE)
    refused = "regression baseline capture refused: "
    if state.base_collection_sha256 is None:
        raise EngineError(f"{refused}this run has no pinned record of the tests its base "
                          f"commit collected at plan time; {_REPLAN}")
    try:
        if record.is_symlink() or not record.is_file():
            raise OSError("not a regular file")
        payload = record.read_bytes()
    except OSError as exc:
        raise EngineError(f"{refused}the plan-time collection record {record.name} is missing "
                          f"or unreadable; {_REPLAN}") from exc
    if hashlib.sha256(payload).hexdigest() != state.base_collection_sha256:
        raise EngineError(f"{refused}the plan-time collection record {record.name} changed "
                          f"since planning; {_REPLAN}")
    assert state.base_commit is not None
    try:
        return regression.read_base_collection(payload, state.base_commit, declared_target)
    except regression.RegressionError as exc:
        raise EngineError(f"{refused}{exc}; {_REPLAN}") from exc


def inherit_regression_baseline(
    *, repo_root: Path, run_id: str, source_run_id: str, source_file: Path,
) -> dict:
    """Import the exact pinned baseline bytes of an earlier run; never executes pytest."""
    paths = RunPaths(repo_root, run_id)
    source_paths = RunPaths(repo_root, source_run_id)
    state, source_state = load_state(paths), load_state(source_paths)
    _baseline_destination(paths, state)
    if (state.objective.stack is not Stack.PYTHON
            or source_state.objective.stack is not Stack.PYTHON):
        raise EngineError("per-test regression baselines are only supported for Python")
    if source_state.mode is not BuildMode.IMPROVE:
        raise EngineError("baseline source must be an IMPROVE run")
    if not state.base_commit or state.base_commit != source_state.base_commit:
        raise EngineError("baseline source and destination must have the same base commit")
    declared_target = _plan_declared_target(repo_root, state)
    if declared_target is None:
        raise EngineError(_UNDECLARED_BASELINE_TARGET)
    if _plan_declared_target(repo_root, source_state) != declared_target:
        raise EngineError("baseline source and destination must declare the same product target")
    supplied_source = Path(source_file)
    if not supplied_source.is_absolute():
        supplied_source = repo_root / supplied_source
    repo = repo_root.resolve()
    state_root = contained_path(repo, ".hw", label="baseline source root")
    try:
        supplied_relative = supplied_source.absolute().relative_to(state_root.absolute())
        trusted_source = contained_path(
            state_root, *supplied_relative.parts, label="baseline source file",
        )
    except ValueError as exc:
        raise EngineError(
            "baseline source file must be contained under this repository's .hw"
        ) from exc
    if supplied_source.is_symlink() or trusted_source.is_symlink():
        raise EngineError("baseline source must not be a symbolic link")
    try:
        if supplied_source.resolve(strict=True) != trusted_source.resolve(strict=True):
            raise EngineError("baseline source file is not the contained regular file requested")
    except OSError as exc:
        raise EngineError("baseline source file does not exist") from exc
    if not trusted_source.is_file():
        raise EngineError("baseline source must be a regular file")
    payload = trusted_source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if source_state.regression_baseline_sha256 != digest:
        raise EngineError("baseline source bytes do not match the source run's pinned SHA-256")
    try:
        baseline = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EngineError("baseline source is not valid UTF-8 JSON") from exc
    if not isinstance(baseline, dict):
        raise EngineError("baseline source payload must be an object")
    not_passing = baseline.get("not_passing_ids")
    declared = baseline.get("declared_not_passing")
    declared_ids = {item.get("id") for item in declared if isinstance(item, dict)} if (
        isinstance(declared, list)) else set()
    if isinstance(not_passing, list) and not set(not_passing) <= declared_ids:
        # A donor captured before the declaration rule may protect less than it shows.
        raise EngineError(
            "baseline source records test(s) that did not pass at capture and are not declared "
            f"in its declared_not_passing list: {sorted(set(not_passing) - declared_ids)[:5]}; "
            "capture a new baseline instead"
        )
    # The S0.2q comparison applies to inherited bytes too: every test the base commit collected
    # when THIS run was planned must be collected in the donor (passing, or not passing and
    # declared above). A donor captured before that comparison may protect less (F3). ``hw
    # plan`` pins that record for every IMPROVE Python run with a declared target in git; a
    # destination without a pin (planned by an older engine, or outside git) has nothing to
    # compare with, as before.
    vanished: list[str] = []
    if state.base_collection_sha256 is not None:
        planned = _planned_collection(paths, state, declared_target)
        try:
            vanished = regression.vanished_since_plan(baseline, planned)
        except (KeyError, TypeError) as exc:
            raise EngineError(
                "baseline source lacks its passing and not-passing test ids") from exc
    if vanished:
        raise EngineError(
            f"baseline source refused: {len(vanished)} test(s) were collected from base_commit "
            "when this run was planned but are not collected in the baseline source: "
            f"{sorted(vanished)[:5]} (file metadata that a commit does not store, such as a "
            "permission bit or an ACL, can deselect a test or shrink a parametrized list); "
            "capture a new baseline instead"
        )

    source_transaction = _baseline_transaction(source_paths)
    if source_transaction is None:
        # Every baseline published through a transaction records its provenance in run state;
        # such a source must still hold the transaction that binds its bytes to a target.
        if any(value is not None for value in (
                source_state.regression_baseline_source_run_id,
                source_state.regression_baseline_source_file,
                source_state.regression_baseline_recorded_at)):
            raise EngineError(
                "baseline source run has no baseline transaction "
                f"({_BASELINE_TRANSACTION_FILE}), so nothing binds its bytes to a declared "
                "product target"
            )
    else:
        # A capture names its target; an inheritance recorded before targets were bound has "".
        bound = {"capture": {declared_target}, "inherit": {declared_target, ""}}
        if (source_transaction.get("sha256") != digest
                or source_transaction.get("request_target")
                not in bound.get(str(source_transaction.get("operation")), set())):
            raise EngineError(
                "baseline source transaction does not bind these bytes to the destination's "
                "declared product target"
            )
    source_relative = trusted_source.relative_to(repo).as_posix()
    existing = _baseline_transaction(paths)
    # A destination finalized before targets were bound keeps its recorded shape on a re-call.
    unbound = (existing is not None and existing.get("operation") == "inherit"
               and existing.get("request_target") == "")
    request = _baseline_request(
        operation="inherit", run_id=run_id, source_run_id=source_run_id,
        source_file=source_relative, request_target="" if unbound else declared_target,
        payload=payload,
        recorded_at=existing.get("recorded_at") if existing else None,
    )
    if existing is None and (paths.artifact(regression.BASELINE_FILE).exists()
                             or state.regression_baseline_sha256 is not None):
        raise EngineError("destination already has a regression baseline")
    inherited, newly_pinned = _finish_baseline_transaction(
        paths=paths, state=state, request=request, existing=existing,
    )
    if newly_pinned:
        RunStore(paths.run_dir).append_event(
            run_id=run_id, stage="comprehend", status=RunEventStatus.running,
            message=f"Regression baseline inherited from {source_run_id}: {digest}",
            artifacts=[source_relative],
        )
    return {
        "run_id": run_id, "source_run_id": source_run_id,
        "source_file": source_relative, "sha256": digest,
        "recorded_at": state.regression_baseline_recorded_at,
        "baseline": inherited, "recovered": existing is not None,
    }


import_regression_baseline = inherit_regression_baseline
regression_baseline_inherit = inherit_regression_baseline


# --------------------------------------------------------------------------- helpers


def _declared_product_target(
    repo_root: Path, state: RunState, target: Path,
) -> tuple[Path, str | None]:
    """Resolve and authorise a deterministic target, pinning legacy auto-targets once.

    Returns the resolved target and a legacy pin for the caller to persist only after success.
    This helper is deliberately side-effect free: rejected or failed calls cannot claim a pin.
    """
    root = repo_root.resolve()
    resolved_target = target.resolve()
    if not resolved_target.is_relative_to(root):
        raise EngineError("deterministic target must be inside the repository")

    auto_placeholder = f"products/{state.run_id}"
    if state.product_dir != auto_placeholder:
        declared = (root / state.product_dir).resolve()
        if not declared.is_relative_to(root) or resolved_target != declared:
            raise EngineError(
                "deterministic target must exactly match the declared product directory"
            )
        return resolved_target, None

    normalized_target = resolved_target.relative_to(root).as_posix()
    if state.deterministic_target is not None:
        declared = (root / state.deterministic_target).resolve()
        if not declared.is_relative_to(root) or resolved_target != declared:
            raise EngineError("deterministic target must exactly match its pinned target")
        return resolved_target, None

    return resolved_target, normalized_target


_UNDECLARED_BASELINE_TARGET = (
    "regression baseline requires a product directory declared at plan time "
    "(`hw plan --product-dir`); this run declared none, so a caller-chosen target is refused"
)


def _plan_declared_target(repo_root: Path, state: RunState) -> str | None:
    """Repo-relative target the run declared when it was planned; never a caller argument.

    A run planned without a product directory holds the ``products/<run_id>`` placeholder and
    no target of its own; a target pinned later by a first deterministic call is not a
    declaration and yields ``None``.

    The declaration is compared as written: it is normalised by its own components and never
    resolved, and a symbolic link at any component below the repository root is refused, so
    re-pointing a link after planning cannot make one declaration stand for another directory.
    Links above the repository root belong to the host and are not declared components.
    """
    if (state.product_dir == f"products/{state.run_id}"
            and state.deterministic_target != state.product_dir):
        return None
    root = repo_root.resolve()
    escapes = EngineError("declared product directory escapes the repository")
    declared = Path(state.product_dir)
    if declared.is_absolute():
        base = next((candidate for candidate in (root, repo_root.absolute())
                     if declared.is_relative_to(candidate)), None)
        if base is None:
            raise escapes
        declared = declared.relative_to(base)
    kept: list[str] = []
    for part in declared.parts:
        if part == "..":
            if not kept:
                raise escapes
            kept.pop()
            continue
        kept.append(part)
        if root.joinpath(*kept).is_symlink():
            raise EngineError(
                "declared product directory must not pass through a symbolic link: "
                f"{'/'.join(kept)}"
            )
    return Path(*kept).as_posix()


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
    "require_accepted_head",
    "evaluate",
    "fanout_plan",
    "load_state",
    "next_step",
    "plan",
    "register_worktree",
    "record_result",
    "regression_baseline",
    "inherit_regression_baseline",
    "import_regression_baseline",
    "regression_baseline_inherit",
    "rollback",
]
