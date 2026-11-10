"""The ``hw`` command-line interface — the engine's only public surface.

Subcommands map one-to-one onto the engine functions:

    hw plan          create a run from an objective (intake + tier; no decomposition)
    hw next-step     return the next step packet + fresh instruction (resume-safe)
    hw complete-step mark a single/fanout step done (validates declared artifact)
    hw fanout-plan   emit N self-contained sub-agent packets in dependency-safe waves
    hw record-result THE ACCEPTANCE GATE — accept or hard-reject+rollback one worktree
    hw evaluate      run deterministic gates (HARD-BLOCK on failure)
    hw rollback      roll a worktree back to a byte-clean HEAD

Phase 3 — real-time guardrails + human gates:

    hw compile-guardrails  emit the PreToolUse hook + registry + settings + Codex artifacts
    hw guardrails-install  install the compiled Claude hook with a run-scoped backup
    hw guardrails-remove   restore/remove Claude settings after the protected worker
    hw gate-open           open a disk-file HITL gate for an irreversible action (BLOCKS)
    hw gate-status         resolve any gate whose human approval file appeared (exit 1=blocking)
    hw gate-approve        record a HUMAN decision (approve/reject/modify-scope)
    hw classify-paths      classify paths into sensitive categories + a recommended gate kind
    hw policy-status       report whether the owner's human-approvals policy switch is on

Phase 4 — durability + the Codex adapter:

    hw resume              scan disk artifacts + resume from the first missing/invalid artifact
    hw status              report run status + the read-only artifact-scan reconciliation
    hw codex-prepare       wire one Codex worker (install git hook + emit sandbox manifest + TOML)
    hw codex-batches       split the fan-out into dependency waves + concurrency-capped sub-batches

Phase 5 — the self-improving autonomy loop + observability (ALL review-gated):

    hw learn               mine .hw/*/ run history → emit review-only proposals (IMPROVE objectives)
    hw propose             list the proposal pack; with --id, open its human-review gate (BLOCKS)
    hw promote             promote an APPROVED proposal to an IMPROVE run (REFUSES without a human
                           approval file — the factory never auto-applies a change to itself)
    hw dashboard           compute the observability dashboard (JSON + a text view) from disk
    hw anomalies           detect code anomalies from disk + route high-severity alerts to the sink
    hw escalations         list escalations from the runtime-neutral sink (one run or all)

Every subcommand prints a single JSON object to stdout. The engine never calls an LLM or
spawns a sub-agent — these commands only compute, persist, and report. (The PreToolUse hook the
compiler emits is a SEPARATE harness-invoked process; the engine only GENERATES it as text.)

Exit codes: 0 = ok, 1 = a gate/acceptance rejection (an expected "no" the adapter handles),
2 = an engine precondition error (bad usage / missing prerequisite).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hyperwilfred_core import (
    codex,
    compiler,
    control_evolution,
    cost,
    engine,
    escalation,
    foundry,
    gates,
    guardrails,
    learning,
    observability,
    quality,
    resume,
    review,
)
from hyperwilfred_core.command_center import snapshot as command_center_snapshot
from hyperwilfred_core.foundry_agents import CodexCLIProvider
from hyperwilfred_core.foundry_execution import FoundryExecutionBlocked, execute_bundle
from hyperwilfred_core.foundry_families import FAMILIES
from hyperwilfred_core.foundry_scout import CandidateDossier, InMemorySearchProvider
from hyperwilfred_core.schemas import (
    BuildMode,
    BuildObjective,
    GateDecision,
    GateKind,
    ReviewTargetKind,
    RiskLevel,
    Stack,
    Tier,
)
from hyperwilfred_core.verify_checks import VerifyCheckError


def _emit(obj: object) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _repo_root(args: argparse.Namespace) -> Path:
    return Path(args.repo).resolve()


class FoundryCLIError(ValueError):
    """An invalid local input to a Foundry CLI command."""


def _read_foundry_json(path: str, *, label: str) -> object:
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except OSError as exc:
        raise FoundryCLIError(f"cannot read {label} at {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FoundryCLIError(
            f"invalid JSON in {label} at {path}: line {exc.lineno}, column {exc.colno}"
        ) from exc


def _foundry_value(args: argparse.Namespace) -> object:
    if args.value_file:
        return _read_foundry_json(args.value_file, label="answer file")
    try:
        return json.loads(args.value)
    except json.JSONDecodeError:
        return args.value


def _foundry_scout_providers(path: str | None) -> tuple[InMemorySearchProvider, ...]:
    if not path:
        return ()
    payload = _read_foundry_json(path, label="scout fixture")
    raw_candidates = payload.get("candidates") if isinstance(payload, dict) else payload
    if not isinstance(raw_candidates, list):
        raise FoundryCLIError("scout fixture must be a JSON list or an object with 'candidates'")
    try:
        candidates = [CandidateDossier.model_validate(item) for item in raw_candidates]
    except (TypeError, ValueError) as exc:
        raise FoundryCLIError(f"invalid scout fixture: {exc}") from exc
    return (InMemorySearchProvider(candidates),)


def _cmd_foundry_create(args: argparse.Namespace) -> int:
    if args.objective_file:
        try:
            objective = Path(args.objective_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise FoundryCLIError(
                f"cannot read objective file at {args.objective_file}: {exc}"
            ) from exc
    else:
        objective = args.objective
    if not objective:
        raise FoundryCLIError("provide --objective TEXT or --objective-file PATH")
    _emit(foundry.create(args.project, objective, mode=args.mode, task_family=args.task_family))
    return 0


def _cmd_foundry_answer(args: argparse.Namespace) -> int:
    _emit(foundry.answer(args.project, _foundry_value(args), field=args.field))
    return 0


def _cmd_foundry_next(args: argparse.Namespace) -> int:
    providers = _foundry_scout_providers(args.scout_fixture)
    _emit(foundry.next_action(args.project, providers=providers))
    return 0


def _cmd_foundry_status(args: argparse.Namespace) -> int:
    _emit(foundry.status(args.project))
    return 0


def _cmd_foundry_validate(args: argparse.Namespace) -> int:
    _emit(foundry.validate(args.project))
    return 0


def _cmd_foundry_generate(args: argparse.Namespace) -> int:
    decisions = [
        {"capability": capability, "decision": "defer", "rationale": "Explicit CLI deferral"}
        for capability in args.defer_capability
    ]
    _emit(foundry.generate(
        args.project, human_decisions=decisions, seeded_defect=args.seeded_defect
    ))
    return 0


def _cmd_foundry_evaluate(args: argparse.Namespace) -> int:
    _emit(foundry.evaluate(args.project, provider=_model_provider(args)))
    return 0


def _cmd_foundry_repair(args: argparse.Namespace) -> int:
    _emit(foundry.repair(args.project, args.component, provider=_model_provider(args)))
    return 0


def _model_provider(args: argparse.Namespace):
    return CodexCLIProvider() if args.provider == "codex" else None


def _cmd_foundry_execute(args: argparse.Namespace) -> int:
    payload = _read_foundry_json(args.input_file, label="execution input")
    if not isinstance(payload, dict):
        raise ValueError("execution input must be a JSON object")
    root, state = foundry._load(args.project)
    build = foundry._read_artifact(root, state, "build")
    result = execute_bundle(root / build["directory"], payload,
                            expected_manifest_sha256=build["manifest_sha256"],
                            provider=_model_provider(args))
    _emit(result.model_dump(mode="json"))
    return 0 if result.accepted else 1


def _cmd_foundry_package(args: argparse.Namespace) -> int:
    _emit(foundry.package(args.project))
    return 0


def _cmd_foundry_resume(args: argparse.Namespace) -> int:
    _emit(foundry.resume(args.project))
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    if args.objective_file:
        objective = BuildObjective.model_validate_json(
            Path(args.objective_file).read_text(encoding="utf-8")
        )
    else:
        if not args.objective:
            raise engine.EngineError("provide --objective TEXT or --objective-file PATH")
        objective = BuildObjective(
            id=args.id or "obj-" + (args.run_id or "auto"),
            title=args.title or args.objective[:60],
            objective=args.objective,
            acceptance_criteria=list(args.acceptance or []),
            constraints=list(args.constraint or []),
            risk_level=RiskLevel(args.risk),
            stack=Stack(args.stack),
        )
    state = engine.plan(
        repo_root=_repo_root(args),
        objective=objective,
        run_id=args.run_id,
        product_dir=args.product_dir,
        mode=BuildMode(args.mode),
        tier=Tier(args.tier) if args.tier else None,
    )
    _emit(
        {
            "run_id": state.run_id,
            "mode": state.mode.value,
            "tier": state.tier.value,
            "product_dir": state.product_dir,
            "steps": [s.id for s in state.steps],
            "first_step": engine.next_step(repo_root=_repo_root(args), run_id=state.run_id),
        }
    )
    return 0


def _cmd_next_step(args: argparse.Namespace) -> int:
    out = engine.next_step(repo_root=_repo_root(args), run_id=args.run_id)
    _emit(out)
    # A terminally-halted run (a human rejected an irreversible gate, status=FAILED) cannot
    # advance — signal it with exit 1, consistent with `hw resume` / `hw gate-status`, so a driver
    # looping on the exit code stops rather than treating the {halted:true} body as advanceable.
    return 1 if out.get("halted") else 0


def _cmd_complete_step(args: argparse.Namespace) -> int:
    _emit(engine.complete_step(repo_root=_repo_root(args), run_id=args.run_id, step_id=args.step))
    return 0


def _cmd_fanout_plan(args: argparse.Namespace) -> int:
    _emit(engine.fanout_plan(repo_root=_repo_root(args), run_id=args.run_id, step_id=args.step))
    return 0


def _cmd_register_worktree(args: argparse.Namespace) -> int:
    _emit(engine.register_worktree(
        repo_root=_repo_root(args), run_id=args.run_id, packet_id=args.packet_id,
        worktree=args.worktree, kind=args.kind,
    ))
    return 0


def _cmd_record_result(args: argparse.Namespace) -> int:
    out = engine.record_result(
        repo_root=_repo_root(args),
        run_id=args.run_id,
        packet_id=args.packet_id,
        worktree=args.worktree,
        result_file=args.result_file,
        do_rollback=not args.no_rollback,
    )
    _emit(out)
    return 0 if out["verdict"]["accepted"] else 1


def _cmd_evaluate(args: argparse.Namespace) -> int:
    try:
        out = engine.evaluate(
            repo_root=_repo_root(args),
            run_id=args.run_id,
            target=Path(args.target).resolve(),
            step_id=args.step,
        )
    except engine.GateFailure as exc:
        _emit({"run_id": args.run_id, "passed": False, "reason": str(exc)})
        return 1
    _emit(out)
    return 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    _emit(engine.rollback(worktree=args.worktree))
    return 0


def _cmd_regression_baseline(args: argparse.Namespace) -> int:
    _emit(
        engine.regression_baseline(
            repo_root=_repo_root(args), run_id=args.run_id, target=Path(args.target).resolve()
        )
    )
    return 0


def _cmd_regression_baseline_inherit(args: argparse.Namespace) -> int:
    _emit(
        engine.inherit_regression_baseline(
            repo_root=_repo_root(args), run_id=args.run_id,
            source_run_id=args.source_run, source_file=Path(args.source_file),
        )
    )
    return 0


def _cmd_record_control_review(args: argparse.Namespace) -> int:
    _emit(control_evolution.record_control_review(
        repo_root=_repo_root(args), run_id=args.run_id, target=Path(args.target),
        report_file=Path(args.report_file), dispatch_file=Path(args.dispatch_file),
        attestation_file=Path(args.attestation_file) if args.attestation_file else None,
    ))
    return 0


def _cmd_record_usage(args: argparse.Namespace) -> int:
    _emit(
        cost.hw_record_usage(
            repo_root=_repo_root(args),
            run_id=args.run_id,
            transcripts=[Path(t).expanduser().resolve() for t in args.transcript or []],
            session_ids=list(args.session_id or []),
            projects_dir=Path(args.projects_dir).expanduser() if args.projects_dir else None,
        )
    )
    return 0


def _cmd_cost_report(args: argparse.Namespace) -> int:
    _emit(cost.hw_cost_report(repo_root=_repo_root(args), run_id=args.run_id))
    return 0


# --------------------------------------------------------------------------- quality loop


def _cmd_quality_status(args: argparse.Namespace) -> int:
    _emit(quality.quality_status(repo_root=_repo_root(args), run_id=args.run_id))
    return 0


def _cmd_record_subjective(args: argparse.Namespace) -> int:
    if args.attestation_file and args.author != "independent":
        raise quality.QualityError("--attestation-file requires --author independent")
    out = quality.record_subjective(
        repo_root=_repo_root(args), run_id=args.run_id, result_file=args.result_file,
        author=args.author, attestation_file=args.attestation_file,
    )
    _emit(out)
    return 0


def _cmd_critique_plan(args: argparse.Namespace) -> int:
    _emit(quality.critique_plan(repo_root=_repo_root(args), run_id=args.run_id))
    return 0


def _cmd_record_critique(args: argparse.Namespace) -> int:
    if args.attestation_file and args.author != "independent":
        raise quality.QualityError("--attestation-file requires --author independent")
    out = quality.record_critique(
        repo_root=_repo_root(args),
        run_id=args.run_id,
        packet_id=args.packet_id,
        result_file=args.result_file,
        author=args.author,
        attestation_file=args.attestation_file,
    )
    _emit(out)
    return 0


def _cmd_trace_check(args: argparse.Namespace) -> int:
    from hyperwilfred_core import traceability

    report = traceability.check(repo_root=_repo_root(args), run_id=args.run_id)
    _emit(report.model_dump(mode="json"))
    return 0 if report.ok else 1


def _cmd_verify_override(args: argparse.Namespace) -> int:
    from hyperwilfred_core import verify_checks

    _emit(
        verify_checks.record_override(
            repo_root=_repo_root(args), run_id=args.run_id, check=args.check,
            by=args.by, reason=args.reason,
        )
    )
    return 0


def _cmd_plan_lint(args: argparse.Namespace) -> int:
    from hyperwilfred_core import plan_lint

    report = plan_lint.lint_run(repo_root=_repo_root(args), run_id=args.run_id)
    _emit({**report.model_dump(mode="json"), "ok": report.ok})
    return 0 if report.ok else 1


def _cmd_quality_override(args: argparse.Namespace) -> int:
    _emit(
        quality.record_independent_override(
            repo_root=_repo_root(args), run_id=args.run_id, by=args.by, reason=args.reason
        )
    )
    return 0


def _cmd_quality_gate(args: argparse.Namespace) -> int:
    # Always exit 0: the `action` field tells the adapter what to do next (pass / patch /
    # escalate / fallback / deterministic_gate_required). It is a decision, not a pass/fail.
    _emit(quality.quality_gate(repo_root=_repo_root(args), run_id=args.run_id))
    return 0


def _cmd_patch_plan(args: argparse.Namespace) -> int:
    _emit(quality.patch_plan(repo_root=_repo_root(args), run_id=args.run_id))
    return 0


def _cmd_record_patch(args: argparse.Namespace) -> int:
    out = quality.record_patch(
        repo_root=_repo_root(args),
        run_id=args.run_id,
        packet_id=args.packet_id,
        worktree=args.worktree,
        result_file=args.result_file,
        do_rollback=not args.no_rollback,
    )
    _emit(out)
    return 0 if out["verdict"]["accepted"] else 1


# ----------------------------------------------------------------- Phase 3: guardrail compiler


def _cmd_compile_guardrails(args: argparse.Namespace) -> int:
    _emit(
        compiler.compile_guardrails(
            repo_root=_repo_root(args), run_id=args.run_id, packet_id=args.packet_id
        )
    )
    return 0


def _cmd_guardrails_install(args: argparse.Namespace) -> int:
    _emit(
        compiler.install_guardrails(
            repo_root=_repo_root(args), run_id=args.run_id, packet_id=args.packet_id
        )
    )
    return 0


def _cmd_guardrails_remove(args: argparse.Namespace) -> int:
    _emit(compiler.remove_guardrails(repo_root=_repo_root(args), run_id=args.run_id))
    return 0


# --------------------------------------------------------------------------- Phase 3: HITL gates


def _cmd_gate_open(args: argparse.Namespace) -> int:
    out = gates.gate_open(
        repo_root=_repo_root(args),
        run_id=args.run_id,
        kind=GateKind(args.kind) if args.kind else None,
        step_id=args.step,
        action=args.action or "",
        paths=list(args.path or []),
        evidence=list(args.evidence or []),
    )
    _emit(out)
    return 0


def _cmd_gate_status(args: argparse.Namespace) -> int:
    out = gates.gate_status(
        repo_root=_repo_root(args), run_id=args.run_id, gate_id=args.gate_id
    )
    _emit(out)
    # Exit 1 while any gate is still BLOCKING (an unapproved irreversible gate hard-blocks); the
    # adapter loops on this until exit 0 (every gate decided).
    return 1 if out["blocked"] else 0


def _cmd_gate_approve(args: argparse.Namespace) -> int:
    out = gates.gate_approve(
        repo_root=_repo_root(args),
        run_id=args.run_id,
        gate_id=args.gate_id,
        decision=GateDecision(args.decision),
        decided_by=args.by or "human",
        note=args.note or "",
        token_file=args.token_file,
    )
    _emit(out)
    return 0


def _cmd_classify_paths(args: argparse.Namespace) -> int:
    _emit(gates.classify_paths(list(args.path or [])))
    return 0


def _cmd_policy_status(args: argparse.Namespace) -> int:
    from hyperwilfred_core import approvals

    policy = approvals.active_policy()
    _emit({
        "human_approvals": policy["human_approvals"] if policy else "signature-required",
        "policy_file": str(approvals.DEFAULT_POLICY_PATH),
        "active": policy is not None,
        "policy": policy,
        "always_signed": ["every run gate (ship, merge, deploy, deps, secrets, migration)",
                          "learning-proposal promotion", "independent-review attestation"],
        "auto_approved_when_active": ["quality-review override", "verify-finding override",
                                      "semantic test-evolution review"],
    })
    return 0


# ----------------------------------------------------------------- Phase 4: durable resume


def _cmd_resume(args: argparse.Namespace) -> int:
    out = resume.resume(
        repo_root=_repo_root(args), run_id=args.run_id, apply=not args.dry_run
    )
    _emit(out)
    # Exit 1 when the run is terminally halted (a human reject) — a clear "no" the driver handles.
    return 1 if out.get("halted") else 0


def _cmd_status(args: argparse.Namespace) -> int:
    paths = engine.RunPaths(_repo_root(args), args.run_id)
    state = engine.load_state(paths)
    _emit({"status": state.status.value, **resume.scan(repo_root=_repo_root(args), state=state)})
    return 0


# ----------------------------------------------------------------- Phase 4: Codex adapter wiring


def _cmd_codex_prepare(args: argparse.Namespace) -> int:
    _emit(
        codex.prepare_codex_worker(
            repo_root=_repo_root(args),
            run_id=args.run_id,
            packet_id=args.packet_id,
            worktree=Path(args.worktree),
            install_hook=not args.no_hook,
        )
    )
    return 0


def _cmd_codex_batches(args: argparse.Namespace) -> int:
    _emit(
        codex.codex_batches(
            repo_root=_repo_root(args), run_id=args.run_id, step_id=args.step, cap=args.cap
        )
    )
    return 0


# ----------------------------------------------------------------- Phase 5: learning + autonomy


def _cmd_learn(args: argparse.Namespace) -> int:
    _emit(learning.learn(repo_root=_repo_root(args)))
    return 0


def _cmd_propose(args: argparse.Namespace) -> int:
    if args.id:
        # Open the disk-file human-review gate for one proposal — BLOCKS until a human decides.
        _emit(learning.open_review_gate(repo_root=_repo_root(args), proposal_id=args.id))
        return 0
    # No id → emit the current proposal pack (read-only listing).
    _emit(learning.proposals_view(repo_root=_repo_root(args)))
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    out = learning.promote(
        repo_root=_repo_root(args), proposal_id=args.id, run_id=args.run_id
    )
    _emit(out)
    # Exit 1 when promotion is REFUSED (no human approval file) — a clear "no" the driver handles.
    return 0 if out.get("promoted") else 1


# ----------------------------------------------------------------- Phase 5: observability


def _cmd_dashboard(args: argparse.Namespace) -> int:
    dashboard = observability.build_dashboard(repo_root=_repo_root(args))
    _emit(dashboard.model_dump(mode="json"))
    return 0


def _cmd_anomalies(args: argparse.Namespace) -> int:
    _emit(
        observability.anomalies_report(repo_root=_repo_root(args), alert=not args.no_alert)
    )
    return 0


def _cmd_escalations(args: argparse.Namespace) -> int:
    items = escalation.list_escalations(repo_root=_repo_root(args), run_id=args.run_id)
    _emit(
        {
            "run_id": args.run_id,
            "escalations": [e.model_dump(mode="json") for e in items],
            "count": len(items),
            "notifications": str(escalation.notifications_path(_repo_root(args))),
        }
    )
    return 0


# -------------------------------------------------------------- Phase 6: REVIEW (read-only audit)


def _cmd_review(args: argparse.Namespace) -> int:
    objective = BuildObjective(
        id=args.id or "review-" + (args.run_id or "auto"),
        title=args.title or (args.objective or "Standalone code review")[:60],
        objective=args.objective or "Read-only audit of the declared target via the critic panel.",
        acceptance_criteria=list(args.acceptance or []),
        constraints=list(args.constraint or []),
        risk_level=RiskLevel(args.risk),
        stack=Stack(args.stack),
    )
    out = review.create_review(
        repo_root=_repo_root(args),
        objective=objective,
        kind=ReviewTargetKind(args.target_kind),
        run_id=args.run_id,
        root=args.root,
        paths=list(args.path or []),
        diff_range=args.diff_range or "",
        tier=Tier(args.tier) if args.tier else None,
        max_files=args.max_files,
        lenses=list(args.lens or []),
    )
    _emit(out)
    return 0


def _cmd_review_report(args: argparse.Namespace) -> int:
    _emit(review.review_report(repo_root=_repo_root(args), run_id=args.run_id))
    return 0


def _absolute_path(value: str) -> Path:
    """argparse type for ``--ops-bin``: a relative path would depend on the working directory."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise argparse.ArgumentTypeError(f"must be an absolute path, got {value!r}")
    return path


def _cmd_command_center_snapshot(args: argparse.Namespace) -> int:
    try:
        result = command_center_snapshot.run_snapshot(
            workspace=Path(args.workspace),
            out_dir=Path(args.out_dir) if args.out_dir else None,
            memory_dir=Path(args.memory) if args.memory else None,
            ops_bin=args.ops_bin,
        )
    except command_center_snapshot.CommandCenterError as exc:
        _emit({"error": str(exc), "kind": "command_center_error"})
        return 2
    _emit(result)
    return 0


def _cmd_command_center_render(args: argparse.Namespace) -> int:
    try:
        result = command_center_snapshot.run_render(
            out_dir=Path(args.out_dir) if args.out_dir else None
        )
    except command_center_snapshot.CommandCenterError as exc:
        _emit({"error": str(exc), "kind": "command_center_error"})
        return 2
    _emit(result)
    return 0


def _cmd_command_center_build(args: argparse.Namespace) -> int:
    try:
        result = command_center_snapshot.run_build(
            workspace=Path(args.workspace),
            out_dir=Path(args.out_dir) if args.out_dir else None,
            memory_dir=Path(args.memory) if args.memory else None,
            ops_bin=args.ops_bin,
        )
    except command_center_snapshot.CommandCenterError as exc:
        _emit({"error": str(exc), "kind": "command_center_error"})
        return 2
    _emit(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wilfred",
                                description="Wilfred engineering and Agent Foundry.")
    p.add_argument("--repo", default=".", help="repo root that holds .hw/ (default: cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    # Agent Foundry is namespaced so its lifecycle verbs do not collide with the existing
    # HyperWilfred run lifecycle (notably status, evaluate, and resume).
    foundry_parser = sub.add_parser(
        "foundry", help="create and advance a resumable Agent Foundry project"
    )
    foundry_sub = foundry_parser.add_subparsers(dest="foundry_cmd", required=True)

    sp = foundry_sub.add_parser("create", help="create a project from an objective")
    sp.add_argument("--project", required=True, help="Foundry project directory")
    objective = sp.add_mutually_exclusive_group(required=True)
    objective.add_argument("--objective", help="plain-language outcome to build")
    objective.add_argument("--objective-file", help="UTF-8 file containing the objective")
    sp.add_argument("--mode", choices=("guided", "autopilot"), default="guided")
    sp.add_argument("--task-family", choices=tuple(FAMILIES), help="explicit bounded task contract")
    sp.set_defaults(func=_cmd_foundry_create)

    sp = foundry_sub.add_parser("answer", help="answer the current guided-intake question")
    sp.add_argument("--project", required=True, help="Foundry project directory")
    answer = sp.add_mutually_exclusive_group(required=True)
    answer.add_argument("--value", help="answer text or a JSON value")
    answer.add_argument("--value-file", help="JSON file containing the answer value")
    sp.add_argument("--field", help="optional intake field; must match the current question")
    sp.set_defaults(func=_cmd_foundry_answer)

    sp = foundry_sub.add_parser(
        "next-action", aliases=["next"], help="advance one deterministic stage or ask one question"
    )
    sp.add_argument("--project", required=True, help="Foundry project directory")
    sp.add_argument(
        "--scout-fixture",
        help="optional provider-neutral JSON candidate fixture for the scout stage",
    )
    sp.set_defaults(func=_cmd_foundry_next)

    for command, help_text, handler in (
        ("status", "report project stage and next action", _cmd_foundry_status),
        ("validate", "validate the generated specification and contracts", _cmd_foundry_validate),
        ("generate", "generate the runtime-neutral local bundle", _cmd_foundry_generate),
        ("package", "package a successfully evaluated project", _cmd_foundry_package),
        ("resume", "verify artifacts and return the safe continuation point", _cmd_foundry_resume),
    ):
        sp = foundry_sub.add_parser(command, help=help_text)
        sp.add_argument("--project", required=True, help="Foundry project directory")
        if command == "generate":
            sp.add_argument("--defer-capability", action="append", default=[],
                            help="explicitly defer one unsupported runtime capability")
            sp.add_argument("--seeded-defect", action="store_true", help=argparse.SUPPRESS)
        sp.set_defaults(func=handler)

    sp = foundry_sub.add_parser("evaluate", help="execute all required evaluation scenarios")
    sp.add_argument("--project", required=True, help="Foundry project directory")
    sp.add_argument("--provider", choices=("codex",), help="explicit authenticated model provider")
    sp.set_defaults(func=_cmd_foundry_evaluate)

    sp = foundry_sub.add_parser("repair", help="patch defective source and execute its regression")
    sp.add_argument("--project", required=True, help="Foundry project directory")
    sp.add_argument("--component", required=True, help="failed scenario/component to replace")
    sp.add_argument("--provider", choices=("codex",), help="explicit authenticated model provider")
    sp.set_defaults(func=_cmd_foundry_repair)

    sp = foundry_sub.add_parser("execute", help="perform the generated task on a JSON input")
    sp.add_argument("--project", required=True)
    sp.add_argument("--input-file", required=True)
    sp.add_argument("--provider", choices=("codex",), help="explicit authenticated model provider")
    sp.set_defaults(func=_cmd_foundry_execute)

    sp = sub.add_parser("plan", help="create a run from an objective")
    sp.add_argument("--objective", help="the founder's one-line objective")
    sp.add_argument("--objective-file", help="path to a BuildObjective JSON file")
    sp.add_argument("--id", help="objective id")
    sp.add_argument("--title")
    sp.add_argument("--acceptance", action="append", help="repeatable acceptance criterion")
    sp.add_argument("--constraint", action="append", help="repeatable constraint")
    sp.add_argument("--risk", default="medium", choices=[r.value for r in RiskLevel])
    sp.add_argument(
        "--stack",
        default="python",
        choices=[s.value for s in Stack],
        help="product stack — selects the deterministic gate chain",
    )
    sp.add_argument("--run-id")
    sp.add_argument("--product-dir")
    sp.add_argument("--mode", default="build", choices=[m.value for m in BuildMode])
    sp.add_argument("--tier", choices=[t.value for t in Tier], help="override the tier router")
    sp.set_defaults(func=_cmd_plan)

    sp = sub.add_parser("next-step", help="return the next step packet")
    sp.add_argument("--run-id", required=True)
    sp.set_defaults(func=_cmd_next_step)

    sp = sub.add_parser("complete-step", help="mark a single/fanout step done")
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--step", required=True)
    sp.set_defaults(func=_cmd_complete_step)

    sp = sub.add_parser("fanout-plan", help="emit sub-agent packets in dependency-safe waves")
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--step", default="implement")
    sp.set_defaults(func=_cmd_fanout_plan)

    sp = sub.add_parser(
        "register-worktree", help="bind a clean worktree to one packet before dispatch"
    )
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--packet-id", required=True)
    sp.add_argument("--worktree", required=True)
    sp.add_argument("--kind", choices=["build", "patch"], default="build")
    sp.set_defaults(func=_cmd_register_worktree)

    sp = sub.add_parser("record-result", help="acceptance gate for one worktree")
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--packet-id", required=True)
    sp.add_argument("--worktree", required=True)
    sp.add_argument("--result-file", help="override the packet's result_path")
    sp.add_argument("--no-rollback", action="store_true", help="do not rollback on reject")
    sp.set_defaults(func=_cmd_record_result)

    sp = sub.add_parser("evaluate", help="run deterministic gates (hard-block on failure)")
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--target", required=True, help="path the evaluators check")
    sp.add_argument("--step", default="evaluate")
    sp.set_defaults(func=_cmd_evaluate)

    sp = sub.add_parser("rollback", help="roll a worktree back to a clean HEAD")
    sp.add_argument("--worktree", required=True)
    sp.set_defaults(func=_cmd_rollback)

    sp = sub.add_parser(
        "regression-baseline",
        help="snapshot the green baseline test set (IMPROVE comprehend step)",
    )
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--target", required=True, help="the existing repo/target to baseline")
    sp.set_defaults(func=_cmd_regression_baseline)

    sp = sub.add_parser(
        "regression-baseline-inherit", aliases=["regression-baseline-import"],
        help="inherit exact protected baseline bytes from an earlier run (never runs pytest)",
    )
    sp.add_argument("--run-id", required=True, help="destination IMPROVE run")
    sp.add_argument("--source-run", required=True, help="run that already pins the baseline")
    sp.add_argument("--source-file", required=True, help="source run baseline file")
    sp.set_defaults(func=_cmd_regression_baseline_inherit)

    sp = sub.add_parser(
        "record-control-review", help="prepare or record an exact signed pytest control review",
    )
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--target", required=True)
    sp.add_argument("--report-file", required=True)
    sp.add_argument("--dispatch-file", required=True)
    sp.add_argument("--attestation-file")
    sp.set_defaults(func=_cmd_record_control_review)

    # --- cost accounting ---
    sp = sub.add_parser(
        "record-usage",
        help="record the run's token usage from the runtime transcript(s) incl. sub-agents",
    )
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--transcript", action="append", help="a session JSONL path (repeatable)")
    sp.add_argument(
        "--session-id", action="append", help="a Claude Code session id (repeatable)"
    )
    sp.add_argument("--projects-dir", help="override ~/.claude/projects")
    sp.set_defaults(func=_cmd_record_usage)

    sp = sub.add_parser("cost-report", help="wall-clock + recorded tokens for one run")
    sp.add_argument("--run-id", required=True)
    sp.set_defaults(func=_cmd_cost_report)

    # --- quality loop (Phase 2) ---
    sp = sub.add_parser("quality-status", help="report the quality loop phase + next action")
    sp.add_argument("--run-id", required=True)
    sp.set_defaults(func=_cmd_quality_status)

    sp = sub.add_parser(
        "record-subjective", help="ingest the 0-5 evaluation (engine recomputes the MIN gate)"
    )
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--result-file", required=True, help="path to the SubjectiveEvaluation JSON")
    sp.add_argument(
        "--author", choices=["independent", "adapter"],
        help="request external attestation (independent) or record as adapter (default); "
        "a separate reviewer remains adapter until the exact challenge is signed externally",
    )
    sp.add_argument(
        "--attestation-file",
        help="external signer token for the second call; requires --author independent",
    )
    sp.set_defaults(func=_cmd_record_subjective)

    sp = sub.add_parser("critique-plan", help="emit one read-only critic packet per lens")
    sp.add_argument("--run-id", required=True)
    sp.set_defaults(func=_cmd_critique_plan)

    sp = sub.add_parser("record-critique", help="ingest one critic's findings JSON")
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--packet-id", required=True)
    sp.add_argument("--result-file", required=True, help="path to the CritiqueReport JSON")
    sp.add_argument(
        "--author", choices=["independent", "adapter"],
        help="request external attestation (independent) or record as adapter (default); "
        "a separate reviewer remains adapter until the exact challenge is signed externally",
    )
    sp.add_argument(
        "--attestation-file",
        help="external signer token for the second call; requires --author independent",
    )
    sp.set_defaults(func=_cmd_record_critique)

    sp = sub.add_parser(
        "trace-check",
        help="requirement traceability: shaping/requirement IDs vs the contract (exit 1=MISSING)",
    )
    sp.add_argument("--run-id", required=True)
    sp.set_defaults(func=_cmd_trace_check)

    sp = sub.add_parser(
        "verify-override",
        help="HUMAN override of a verify finding (live_demo only); recorded + shown in output",
    )
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--check", required=True, choices=["live_demo"])
    sp.add_argument("--by", required=True)
    sp.add_argument("--reason", required=True)
    sp.set_defaults(func=_cmd_verify_override)

    sp = sub.add_parser(
        "plan-lint",
        help="lint plan.json: cut by responsibility, not file count (exit 1 = blocking, HIGH risk)",
    )
    sp.add_argument("--run-id", required=True)
    sp.set_defaults(func=_cmd_plan_lint)

    sp = sub.add_parser(
        "quality-override",
        help="HUMAN override: accept non-independent (self-review) quality for this iteration",
    )
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--by", required=True, help="who is accepting the risk")
    sp.add_argument("--reason", required=True, help="why (shown in verify output + ship gate)")
    sp.set_defaults(func=_cmd_quality_override)

    sp = sub.add_parser("quality-gate", help="the quality decision (pass/patch/escalate/fallback)")
    sp.add_argument("--run-id", required=True)
    sp.set_defaults(func=_cmd_quality_gate)

    sp = sub.add_parser("patch-plan", help="emit ONE surgical patcher packet from open findings")
    sp.add_argument("--run-id", required=True)
    sp.set_defaults(func=_cmd_patch_plan)

    sp = sub.add_parser("record-patch", help="acceptance gate for the surgical patch")
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--packet-id", required=True)
    sp.add_argument("--worktree", required=True)
    sp.add_argument("--result-file", help="override the patch packet's result_path")
    sp.add_argument("--no-rollback", action="store_true", help="do not rollback on reject")
    sp.set_defaults(func=_cmd_record_patch)

    # --- Phase 3: guardrail compiler (real-time write-time enforcement artifacts) ---
    sp = sub.add_parser(
        "compile-guardrails",
        help="emit the PreToolUse hook + registry + settings + Codex sandbox/git-hook artifacts",
    )
    sp.add_argument("--run-id", required=True)
    sp.add_argument(
        "--packet-id",
        help="scope the registry to one packet (the active worker); omit for the whole wave",
    )
    sp.set_defaults(func=_cmd_compile_guardrails)

    for command, help_text, handler in (
        ("guardrails-install", "install compiled Claude hook with run-scoped backup",
         _cmd_guardrails_install),
        ("guardrails-remove", "restore the pre-run Claude settings", _cmd_guardrails_remove),
    ):
        sp = sub.add_parser(command, help=help_text)
        sp.add_argument("--run-id", required=True)
        if command == "guardrails-install":
            sp.add_argument(
                "--packet-id",
                help="verify the installed policy is scoped to this exact packet",
            )
        sp.set_defaults(func=handler)

    # --- Phase 3: disk-file HITL gates for irreversible actions ---
    sp = sub.add_parser("gate-open", help="open a HITL gate and BLOCK until a human approves")
    sp.add_argument("--run-id", required=True)
    sp.add_argument(
        "--kind",
        choices=[k.value for k in GateKind],
        help="the irreversible action class; omit to derive from --path via protected-paths",
    )
    sp.add_argument("--step", help="the pipeline step this gate attaches to (e.g. ship)")
    sp.add_argument("--action", help="human-readable description of the irreversible action")
    sp.add_argument("--path", action="append", help="repeatable sensitive path that triggers it")
    sp.add_argument("--evidence", action="append", help="repeatable artifact for human review")
    sp.set_defaults(func=_cmd_gate_open)

    sp = sub.add_parser(
        "gate-status", help="report gates and verify signed human decisions"
    )
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--gate-id", help="limit to one gate")
    sp.set_defaults(func=_cmd_gate_status)

    sp = sub.add_parser("gate-approve", help="record a HUMAN decision (approve/reject/modify)")
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--gate-id", required=True)
    sp.add_argument(
        "--decision", required=True, choices=[d.value for d in GateDecision]
    )
    sp.add_argument("--by", help="who decided (recorded in the run store)")
    sp.add_argument("--note", help="reason / scope note")
    sp.add_argument("--token-file", required=True,
                    help="externally signed challenge-bound decision")
    sp.set_defaults(func=_cmd_gate_approve)

    sp = sub.add_parser(
        "classify-paths", help="classify paths into sensitive categories + a recommended gate kind"
    )
    sp.add_argument("--path", action="append", required=True, help="repeatable path to classify")
    sp.set_defaults(func=_cmd_classify_paths)

    sp = sub.add_parser(
        "policy-status",
        help="report whether the human-approvals policy switch is on (read-only)",
    )
    sp.set_defaults(func=_cmd_policy_status)

    # --- Phase 4: durable resume (artifact-scan crash recovery) ---
    sp = sub.add_parser(
        "resume",
        help="scan disk artifacts + resume from the first missing/invalid artifact (crash-safe)",
    )
    sp.add_argument("--run-id", required=True)
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="report the resume cursor + which steps would reset, without mutating state",
    )
    sp.set_defaults(func=_cmd_resume)

    sp = sub.add_parser(
        "status", help="report run status + the read-only artifact-scan reconciliation"
    )
    sp.add_argument("--run-id", required=True)
    sp.set_defaults(func=_cmd_status)

    # --- Phase 4: Codex adapter wiring (wire the Phase-3 passive Codex artifacts) ---
    sp = sub.add_parser(
        "codex-prepare",
        help="wire one Codex worker: install the git hook + emit its sandbox manifest + TOML agent",
    )
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--packet-id", required=True)
    sp.add_argument("--worktree", required=True, help="the worker's git worktree (--cd target)")
    sp.add_argument(
        "--no-hook", action="store_true", help="emit the manifest but do not install the git hook"
    )
    sp.set_defaults(func=_cmd_codex_prepare)

    sp = sub.add_parser(
        "codex-batches",
        help="split the fan-out into dependency waves + concurrency-capped sub-batches (cap ~6)",
    )
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--step", default="implement")
    sp.add_argument(
        "--cap",
        type=int,
        default=codex.CODEX_DEFAULT_CONCURRENCY,
        help=f"max concurrent workers per sub-batch (default {codex.CODEX_DEFAULT_CONCURRENCY})",
    )
    sp.set_defaults(func=_cmd_codex_batches)

    # --- Phase 5: learning analyzer + review-gated autonomy loop ---
    sp = sub.add_parser(
        "learn",
        help="mine .hw/*/ run history → emit review-only proposals (IMPROVE objectives)",
    )
    sp.set_defaults(func=_cmd_learn)

    sp = sub.add_parser(
        "propose",
        help="list the proposal pack; with --id, open its human-review gate (BLOCKS)",
    )
    sp.add_argument("--id", help="open the disk-file review gate for this proposal id")
    sp.set_defaults(func=_cmd_propose)

    sp = sub.add_parser(
        "promote",
        help="promote an APPROVED proposal to an IMPROVE run (REFUSES without a human approval)",
    )
    sp.add_argument("--id", required=True, help="the proposal id to promote")
    sp.add_argument("--run-id", help="override the IMPROVE run id (default imp-<proposal-id>)")
    sp.set_defaults(func=_cmd_promote)

    # --- Phase 5: observability (anomaly / alert / dashboard) ---
    sp = sub.add_parser(
        "dashboard", help="compute the observability dashboard (JSON + a text view) from disk"
    )
    sp.set_defaults(func=_cmd_dashboard)

    sp = sub.add_parser(
        "anomalies",
        help="detect code anomalies from disk + route high-severity alerts to the escalation sink",
    )
    sp.add_argument(
        "--no-alert", action="store_true", help="detect only; do not route alerts to the sink"
    )
    sp.set_defaults(func=_cmd_anomalies)

    sp = sub.add_parser(
        "escalations", help="list escalations from the runtime-neutral sink (one run or all)"
    )
    sp.add_argument("--run-id", help="limit to one run; omit for every run")
    sp.set_defaults(func=_cmd_escalations)

    # --- Phase 6: REVIEW (standalone read-only audit reusing the critic panel) ---
    sp = sub.add_parser(
        "review",
        help="create a REVIEW run over a target (path subset / whole repo / git diff range) — "
        "read-only, writes nothing to the reviewed repo",
    )
    sp.add_argument("--objective", help="what to look for (free text); optional")
    sp.add_argument("--title")
    sp.add_argument("--id", help="objective id")
    sp.add_argument(
        "--target-kind",
        default="paths",
        choices=[k.value for k in ReviewTargetKind],
        help="paths (globs/dirs/files under --root; none ⇒ whole repo) | diff (a git diff range)",
    )
    sp.add_argument("--root", default=".", help="repo-relative root the target resolves against")
    sp.add_argument(
        "--path", action="append", help="repeatable path/glob to review (paths kind; omit ⇒ repo)"
    )
    sp.add_argument("--diff-range", help="git diff range for --target-kind diff (e.g. main...HEAD)")
    sp.add_argument("--acceptance", action="append", help="repeatable review focus / criterion")
    sp.add_argument("--constraint", action="append", help="repeatable constraint")
    sp.add_argument("--risk", default="medium", choices=[r.value for r in RiskLevel])
    sp.add_argument("--stack", default="python", choices=[s.value for s in Stack])
    sp.add_argument("--run-id")
    sp.add_argument("--tier", choices=[t.value for t in Tier], help="override the tier router")
    sp.add_argument(
        "--max-files",
        type=int,
        default=review.DEFAULT_MAX_FILES,
        help=f"cap the file list handed to the critics (default {review.DEFAULT_MAX_FILES})",
    )
    sp.add_argument(
        "--lens",
        action="append",
        choices=sorted(quality.REVIEW_OPTIONAL_LENSES),
        help="repeatable opt-in critic lens added to the four standard lenses (default: none). "
        "cross-slice-drift reads only the target files: use --target-kind paths over the "
        "modules of the flow",
    )
    sp.set_defaults(func=_cmd_review)

    sp = sub.add_parser(
        "review-report",
        help="synthesize the recorded critic findings into the ReviewReport (JSON + markdown)",
    )
    sp.add_argument("--run-id", required=True)
    sp.set_defaults(func=_cmd_review_report)

    # --- Command center: a read-only snapshot of a workspace (never writes under it) ---
    command_center_parser = sub.add_parser(
        "command-center", help="read-only view of the projects and runs of a workspace"
    )
    command_center_sub = command_center_parser.add_subparsers(
        dest="command_center_cmd", required=True
    )
    sp = command_center_sub.add_parser(
        "snapshot", help="write snapshot.json (projects, worktrees, runs) outside the workspace"
    )
    sp.add_argument("--workspace", required=True, help="directory that holds the projects")
    sp.add_argument("--memory", help="shared memory directory (default: ~/.agents/memory)")
    sp.add_argument(
        "--ops-bin",
        type=_absolute_path,
        help="absolute path of the ops command, run only when the workspace holds an ops "
        "folder (default: ~/.local/bin/ops)",
    )
    sp.add_argument(
        "--out-dir",
        help="output directory, outside the workspace "
        "(default: ~/Library/Application Support/Wilfred/command-center)",
    )
    sp.set_defaults(func=_cmd_command_center_snapshot)
    sp = command_center_sub.add_parser(
        "render", help="write index.html from the snapshot.json in the output directory"
    )
    sp.add_argument(
        "--out-dir",
        help="output directory that holds snapshot.json "
        "(default: ~/Library/Application Support/Wilfred/command-center)",
    )
    sp.set_defaults(func=_cmd_command_center_render)
    sp = command_center_sub.add_parser(
        "build", help="snapshot then render: write snapshot.json and index.html"
    )
    sp.add_argument("--workspace", required=True, help="directory that holds the projects")
    sp.add_argument("--memory", help="shared memory directory (default: ~/.agents/memory)")
    sp.add_argument(
        "--ops-bin",
        type=_absolute_path,
        help="absolute path of the ops command, run only when the workspace holds an ops "
        "folder (default: ~/.local/bin/ops)",
    )
    sp.add_argument(
        "--out-dir",
        help="output directory, outside the workspace "
        "(default: ~/Library/Application Support/Wilfred/command-center)",
    )
    sp.set_defaults(func=_cmd_command_center_build)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except engine.EngineError as exc:
        _emit({"error": str(exc), "kind": "engine_error"})
        return 2
    except control_evolution.ControlEvolutionError as exc:
        _emit({"error": str(exc), "kind": "control_evolution_error"})
        return 2
    except quality.QualityError as exc:
        _emit({"error": str(exc), "kind": "quality_error"})
        return 2
    except gates.GateError as exc:
        _emit({"error": str(exc), "kind": "gate_error"})
        return 2
    except guardrails.GuardrailError as exc:
        _emit({"error": str(exc), "kind": "guardrail_error"})
        return 2
    except compiler.CompilerError as exc:
        _emit({"error": str(exc), "kind": "compiler_error"})
        return 2
    except codex.CodexError as exc:
        _emit({"error": str(exc), "kind": "codex_error"})
        return 2
    except learning.LearningError as exc:
        _emit({"error": str(exc), "kind": "learning_error"})
        return 2
    except review.ReviewError as exc:
        _emit({"error": str(exc), "kind": "review_error"})
        return 2
    except VerifyCheckError as exc:
        _emit({"error": str(exc), "kind": "verify_check_error"})
        return 2
    except cost.CostError as exc:
        _emit({"error": str(exc), "kind": "cost_error"})
        return 2
    except FoundryCLIError as exc:
        _emit({"error": str(exc), "kind": "foundry_input_error"})
        return 2
    except (foundry.FoundryBlocked, FoundryExecutionBlocked, FileExistsError) as exc:
        _emit({"error": str(exc), "kind": "foundry_error"})
        return 2
    except ValueError as exc:
        # Identifier and path validation raise ValueError before any I/O; report it like every
        # other precondition failure instead of a traceback.
        kind = "foundry_input_error" if getattr(args, "cmd", None) == "foundry" else "input_error"
        _emit({"error": str(exc), "kind": kind})
        return 2
    except NotImplementedError as exc:
        # e.g. `hw plan --mode improve` in Phase 1 — surface a clean message, not a traceback.
        _emit({"error": str(exc), "kind": "not_implemented"})
        return 2


if __name__ == "__main__":
    sys.exit(main())
