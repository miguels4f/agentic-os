# ruff: noqa: E501, E701, I001
from __future__ import annotations
import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from .foundry_intake import (
    Ambiguity,
    EvidenceStatus,
    IntakeEvidence,
    IntakeField,
    IntakeMode,
    IntakeState,
    TopologyKind,
    TopologyProposal,
    apply_autopilot_defaults,
    frame_stop,
)
from .foundry_models import (
    AgentSystemSpecification,
    Component,
    ComponentKind,
    ContextPolicy,
    DataContract,
    DecisionOutcome,
    Evaluation,
    EvaluationKind,
    Evidence,
    EvidenceKind,
    ExternalReuseDecision,
    FailurePolicy,
    MemoryPolicy,
    Metadata,
    PackageArtifact,
    PackageRecord,
    ProblemStatement,
    ProjectStage,
    RecordStatus,
    RuntimeTarget,
    State,
    Threshold,
    Topology,
    TopologyEdge,
    TopologyNode,
)
from .foundry_execution import FoundryExecutionBlocked, materialize_bundle, run_scenarios
from .foundry_design import choose_family, result_model, result_schema, topology_for
from .foundry_runtime import LocalPythonManifestAdapter
from .foundry_storage import atomic, fault, immutable, publish_build, publish_state
from .foundry_scout import ScoutRequest, SearchProvider, scout
from .foundry_validation import validate_specification
class FoundryStage(StrEnum):
    FRAME = "frame"
    CLASSIFY = "classify"
    SCOUT = "scout"
    BLUEPRINT = "blueprint"
    CONTRACT = "contract"
    GENERATE = "generate"
    EVALUATE = "evaluate"
    REPAIR = "repair"
    PACKAGE = "package"
STAGES = tuple(FoundryStage)
_PACKAGE_INPUTS = ("topology", "scout", "specification", "blueprint", "contracts",
                   "validation", "bundle", "build", "evaluation")
_TERMINAL_ARTIFACTS = (*_PACKAGE_INPUTS, "package")
_MATERIAL = {
    IntakeField.INPUTS, IntakeField.OUTPUTS, IntakeField.DECISIONS,
    IntakeField.CONSTRAINTS, IntakeField.FAILURE_IMPACT, IntakeField.AUTONOMY,
    IntakeField.SUCCESS_EVIDENCE,
}
_SECRET_KEY = re.compile(
    r"(secret|password|passwd|token|api.?key|private.?key|credential|authorization|"
    r"(?:^|[_-])auth(?:entication)?(?:$|[_-])|cookie|session)", re.I
)
_SECRET_VALUE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|gh[opusr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{16,}|AKIA[0-9A-Z]{16}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|Bearer\s+[A-Za-z0-9._~-]{16,}|"
    r"Basic\s+[A-Za-z0-9+/=]{8,})", re.I
)
class FoundryBlocked(ValueError):
    """A lifecycle gate refused an incomplete or unsafe transition."""
def _root(project: str | Path) -> Path:
    selected = Path(project).expanduser()
    if selected.is_symlink():
        raise FoundryBlocked("foundry project path must not be a symbolic link")
    return selected.resolve()
def _now() -> str:
    return datetime.now(UTC).isoformat()
def _json(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value)).hexdigest()
def _reject_secrets(value: Any, path: str = "$", *, key_sensitive: bool = False) -> None:
    if isinstance(value, Mapping):
        for index, (key, item) in enumerate(value.items()):
            if _SECRET_VALUE.search(str(key)):
                raise FoundryBlocked("likely secret rejected in an object key; provide a reference")
            _reject_secrets(item, f"{path}.field[{index}]",
                            key_sensitive=key_sensitive or bool(_SECRET_KEY.search(str(key))))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secrets(item, f"{path}[{index}]", key_sensitive=key_sensitive)
    elif ((key_sensitive and value is not None
           and not (isinstance(value, str) and not value.strip()))
          or (isinstance(value, str) and _SECRET_VALUE.search(value))):
        raise FoundryBlocked(f"likely secret rejected at {path}; provide a reference, not the value")
def _atomic(path: Path, value: Any) -> None:
    data = value if isinstance(value, bytes) else _json(value)
    atomic(path, data)
def _load(project: str | Path) -> tuple[Path, dict[str, Any]]:
    root = _root(project)
    try:
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FoundryBlocked(f"cannot resume project at {root}: {type(exc).__name__}") from exc
    if state.get("schema_version") != "agent-foundry-project/v1":
        raise FoundryBlocked("unsupported or missing project-state schema_version; migration required")
    return root, state
def _save(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    state["updated_at"] = _now()
    _reject_secrets(state)
    try:
        publish_state(root, _json(state))
    except ValueError as exc:
        raise FoundryBlocked(str(exc)) from exc
    return state
def _event(root: Path, event: str, **details: Any) -> None:
    _reject_secrets(details)
    record = (json.dumps({"at": _now(), "event": event, **details}, ensure_ascii=False,
                         separators=(",", ":"), sort_keys=True) + "\n").encode()
    path = root / "history.jsonl"
    if path.is_symlink():
        raise FoundryBlocked("history path must not be a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, record.rstrip(b"\n") + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
def _artifact(root: Path, state: dict[str, Any], name: str, value: Any) -> str:
    _reject_secrets(value)
    if (root / "artifacts").is_symlink():
        raise FoundryBlocked("artifact directory must not be a symbolic link")
    digest = _digest(value)
    relative = f"artifacts/{name}-{digest}.json"
    try:
        immutable(root / relative, _json(value))
    except ValueError as exc:
        raise FoundryBlocked(str(exc)) from exc
    fault("artifact_published")
    state.setdefault("artifacts", {})[name] = {"path": relative, "sha256": digest}
    return digest
def _read_artifact(root: Path, state: Mapping[str, Any], name: str) -> Any:
    try:
        artifacts_root = root / "artifacts"
        if artifacts_root.is_symlink():
            raise FoundryBlocked("artifact directory must not be a symbolic link")
        path = root / state["artifacts"][name]["path"]
        if path.is_symlink():
            raise FoundryBlocked(f"required artifact {name!r} must not be a symbolic link")
        path = path.resolve()
        if path.parent != artifacts_root:
            raise FoundryBlocked(f"required artifact {name!r} escapes the project artifact directory")
        encoded = path.read_bytes()
        value = json.loads(encoded)
    except (KeyError, OSError, json.JSONDecodeError) as exc:
        raise FoundryBlocked(f"required artifact {name!r} is missing or corrupt") from exc
    if hashlib.sha256(encoded).hexdigest() != state["artifacts"][name]["sha256"]:
        raise FoundryBlocked(f"required artifact {name!r} failed its integrity check")
    return value
def _advance(root: Path, state: dict[str, Any], completed: FoundryStage) -> None:
    if state["stage"] != completed.value:
        raise FoundryBlocked(f"expected {completed.value}, found {state['stage']}")
    index = STAGES.index(completed)
    state.setdefault("completed", []).append(completed.value)
    if index + 1 < len(STAGES):
        state["stage"] = STAGES[index + 1].value
    _event(root, "transition", completed=completed.value, next=state["stage"])
def create(project: str | Path, objective: str, mode: str | IntakeMode = "guided", *,
           task_family: str | None = None) -> dict[str, Any]:
    """Create a resumable project from an ambiguous objective without guessing facts."""
    root = _root(project)
    if (root / "state.json").exists():
        raise FileExistsError(f"foundry project already exists: {root}")
    _reject_secrets(objective)
    selected = IntakeMode(mode)
    intake = IntakeState(mode=selected)
    intake.evidence[IntakeField.OUTCOME] = IntakeEvidence(
        field=IntakeField.OUTCOME, value=objective.strip(), status=EvidenceStatus.GROUNDED,
        sources=["objective"],
    )
    if selected is IntakeMode.AUTOPILOT:
        ambiguities = [
            Ambiguity(subject=field.value, impact="high" if field in _MATERIAL else "low")
            for field, evidence in intake.evidence.items() if evidence.status is EvidenceStatus.MISSING
        ]
        apply_autopilot_defaults(intake, ambiguities)
    state: dict[str, Any] = {
        "schema_version": "agent-foundry-project/v1", "project_id": root.name,
        "objective": objective.strip(), "mode": selected.value, "stage": FoundryStage.FRAME.value,
        "completed": [], "intake": intake.model_dump(mode="json"), "artifacts": {},
        "created_at": _now(), "updated_at": _now(),
    }
    if task_family is not None:
        try:
            state["task_family"] = choose_family(objective, task_family)
        except ValueError as exc:
            raise FoundryBlocked(str(exc)) from exc
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    _save(root, state)
    _event(root, "created", stage=state["stage"], mode=selected.value)
    return status(root)
def answer(project: str | Path, value: Any, *, field: str | IntakeField | None = None) -> dict[str, Any]:
    """Record one answer to the current intake question; never persists secrets."""
    root, state = _load(project)
    if state["stage"] != FoundryStage.FRAME:
        raise FoundryBlocked("intake answers are only accepted during frame")
    _reject_secrets(value)
    intake = IntakeState.model_validate(state["intake"])
    question = intake.next_question()
    if question is None:
        raise FoundryBlocked("frame has no unanswered question")
    selected = IntakeField(field) if field is not None else question.field
    if selected is not question.field:
        raise FoundryBlocked(f"answer the current question first: {question.field.value}")
    intake.update({selected.value: value})
    intake.blockers = [x for x in intake.blockers if not x.endswith(f": {selected.value}")]
    state["intake"] = intake.model_dump(mode="json")
    _event(root, "intake_answered", field=selected.value)
    return _save(root, state) and status(root)
def _topology(intake: IntakeState, task_family: str | None = None) -> TopologyProposal:
    try:
        return topology_for(choose_family(str(intake.evidence[IntakeField.OUTCOME].value), task_family))
    except ValueError as exc:
        raise FoundryBlocked(str(exc)) from exc
def next_action(project: str | Path, providers: Iterable[SearchProvider] = ()) -> dict[str, Any]:
    """Perform the next deterministic stage action, or return the one intake question."""
    root, state = _load(project)
    stage = FoundryStage(state["stage"])
    if stage is FoundryStage.FRAME:
        intake = IntakeState.model_validate(state["intake"])
        question = intake.next_question()
        if question is not None:
            return {"action": "answer", "stage": stage.value, "question": question.model_dump(mode="json")}
        topology = _topology(intake, state.get("task_family"))
        stop = frame_stop(intake, topology)
        if not stop.passed:
            raise FoundryBlocked("frame incomplete: " + "; ".join(stop.blockers))
        _artifact(root, state, "topology", topology)
        _advance(root, state, stage)
    elif stage is FoundryStage.CLASSIFY:
        _read_artifact(root, state, "topology")
        _advance(root, state, stage)
    elif stage is FoundryStage.SCOUT:
        report = scout(ScoutRequest(problem=state["objective"]), providers)
        _artifact(root, state, "scout", report)
        _advance(root, state, stage)
    elif stage is FoundryStage.BLUEPRINT:
        spec, blueprint = _design(state, _read_artifact(root, state, "topology"),
                                  _read_artifact(root, state, "scout"))
        _artifact(root, state, "specification", spec)
        _artifact(root, state, "blueprint", blueprint)
        _artifact(root, state, "contracts", [x.model_dump(mode="json") for x in spec.contracts])
        _advance(root, state, stage)
    else:
        return {"action": _action_for(stage), **status(root)}
    _save(root, state)
    return status(root)
def _design(state: Mapping[str, Any], topology_data: Mapping[str, Any],
            scout_data: Mapping[str, Any]) -> tuple[AgentSystemSpecification, dict[str, Any]]:
    intake = IntakeState.model_validate(state["intake"])
    topology_proposal = TopologyProposal.model_validate(topology_data)
    family = choose_family(state["objective"], state.get("task_family"))
    uses_model = topology_proposal.kind in {TopologyKind.SINGLE_AGENT, TopologyKind.MULTI_AGENT}
    component_kind = ComponentKind.AGENT if uses_model else ComponentKind.SERVICE
    success = str(intake.evidence[IntakeField.SUCCESS_EVIDENCE].value)
    intake_evidence = [Evidence(
        id=f"intake-{field.value}", kind=EvidenceKind.DECISION,
        summary=value.value if isinstance(value.value, str) else json.dumps(
            value.value, ensure_ascii=False, sort_keys=True),
        source=f"intake:{field.value}", status=RecordStatus.VALIDATED, confidence=1.0,
    ) for field, value in intake.evidence.items() if value.status is EvidenceStatus.GROUNDED]
    reuse_outcomes = {
        "reference_only": DecisionOutcome.DEFER,
        "external_dependency": DecisionOutcome.REUSE,
        "licensed_adaptation": DecisionOutcome.ADAPT,
        "rejected": DecisionOutcome.REJECT,
    }
    scout_evidence: list[Evidence] = []
    reuse_decisions: list[ExternalReuseDecision] = []
    dossiers = {item["candidate_id"]: item for item in scout_data.get("dossiers", [])}
    for index, decision in enumerate(scout_data.get("decisions", [])):
        evidence_id = f"scout-{index}"
        dossier = dossiers.get(decision["candidate_id"], {})
        scout_evidence.append(Evidence(
            id=evidence_id, kind=EvidenceKind.DOCUMENT,
            summary=json.dumps({"dossier": dossier, "decision": decision},
                               ensure_ascii=False, sort_keys=True),
            source=decision.get("candidate_url") or "scout",
            status=RecordStatus.VALIDATED, confidence=1.0,
        ))
        reuse_decisions.append(ExternalReuseDecision(
            candidate=decision["candidate_id"], outcome=reuse_outcomes[decision["classification"]],
            rationale="; ".join(decision.get("rationale", [])), evidence_ids=[evidence_id],
            constraints=[item for item in (dossier.get("version_or_commit"), dossier.get("license"))
                         if item],
        ))
    contract = DataContract(
        name="result", description=f"Declared {family} result", json_schema=result_schema(family)
    )
    evaluations = [Evaluation(
        id=name, kind=EvaluationKind.DETERMINISTIC, description=f"{name} scenario: {success}",
        thresholds=[Threshold(metric="score", operator=">=", value=1.0)],
        evidence_required=["result"],
    ) for name in ("normal", "edge", "adversarial", "recovery")]
    if family == "reviewed_brief":
        evaluations.append(Evaluation(
            id="model_evaluation", kind=EvaluationKind.MODEL_BASED,
            description="Independent reviewer approves the final draft when evidence is nonempty.",
            thresholds=[Threshold(metric="approved", operator="==", value=1.0)],
            evidence_required=["result"],
        ))
    spec = AgentSystemSpecification(
        metadata=Metadata(specification_id=state["project_id"], name=state["project_id"], revision="1"),
        stage=ProjectStage.DESIGN, problem=ProblemStatement(statement=state["objective"],
            desired_outcomes=[str(intake.evidence[IntakeField.OUTPUTS].value)],
            evidence_ids=[item.id for item in intake_evidence], status=RecordStatus.VALIDATED),
        evidence=[*intake_evidence, *scout_evidence], external_reuse=reuse_decisions,
        topology=Topology(nodes=[TopologyNode(id=unit.id, kind=component_kind, label=unit.id)
                                 for unit in topology_proposal.units],
            edges=[TopologyEdge(source=edge.source, target=edge.target, relation=edge.contract)
                   for edge in topology_proposal.handoffs]),
        components=[Component(id=unit.id, kind=component_kind,
            description="; ".join(unit.responsibilities),
            responsibilities=unit.responsibilities, outputs=[contract])
            for unit in topology_proposal.units],
        contracts=[contract], context=ContextPolicy(sources=["declared inputs"]),
        memory=MemoryPolicy(),
        states=[State(id="complete", description="Result accepted", terminal=True)],
        failure_policy=FailurePolicy(retry_limit=1, retry_strategy="bounded",
            timeout_seconds=180, fallback="stop safely", escalation="request human review"),
        evaluations=evaluations,
        runtime_target=RuntimeTarget(environment="local", execution_model="single-run"),
    )
    blueprint = {"schema_version": "agent-blueprint/v1", "task_family": family,
        "topology_kind": topology_proposal.kind.value,
        "steps": [{"id": unit.id, "component_id": unit.id, "model_call": uses_model,
                   "model_call_justification": unit.model_call_justification,
                   "depends_on": [edge.source for edge in topology_proposal.handoffs
                                  if edge.target == unit.id]}
                  for unit in topology_proposal.units]}
    return spec, blueprint
def validate(project: str | Path) -> dict[str, Any]:
    root, state = _load(project)
    if state["stage"] != FoundryStage.CONTRACT:
        raise FoundryBlocked("validation is only available at contract")
    report = validate_specification(_read_artifact(root, state, "specification"),
                                    blueprint=_read_artifact(root, state, "blueprint"))
    _artifact(root, state, "validation", report)
    if not report.valid:
        _save(root, state)
        raise FoundryBlocked("contract validation failed")
    _advance(root, state, FoundryStage.CONTRACT)
    _save(root, state)
    return report.model_dump(mode="json")
def generate(project: str | Path, *, adapter: Any | None = None,
             human_decisions: Sequence[Mapping[str, str]] = (),
             seeded_defect: bool = False, task_family: str | None = None) -> dict[str, Any]:
    root, state = _load(project)
    if state["stage"] != FoundryStage.GENERATE:
        raise FoundryBlocked("generation is only available after an accepted contract")
    blueprint = _read_artifact(root, state, "blueprint")
    selected = blueprint.get("task_family")
    if not selected or (task_family is not None and task_family != selected):
        raise FoundryBlocked("generation task_family must match the accepted blueprint contract")
    bundle = (adapter or LocalPythonManifestAdapter()).build_bundle(
        _read_artifact(root, state, "specification"), blueprint=blueprint,
        human_decisions=human_decisions, seeded_defect=seeded_defect, task_family=selected)
    try:
        evidence = publish_build(root, bundle, materialize_bundle)
    except (FoundryExecutionBlocked, ValueError) as exc:
        raise FoundryBlocked(str(exc)) from exc
    _artifact(root, state, "bundle", bundle)
    _artifact(root, state, "build", evidence)
    _advance(root, state, FoundryStage.GENERATE)
    _save(root, state)
    return bundle.model_dump(mode="json")
def _verify_build(root: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    build = _read_artifact(root, state, "build")
    directory = root / build.get("directory", "")
    if directory.is_symlink() or directory.resolve().parent != root or not directory.is_dir():
        raise FoundryBlocked("generated build directory is missing or unsafe")
    if any(path.is_symlink() or not path.is_file() for path in directory.iterdir()):
        raise FoundryBlocked("generated build failed its integrity check")
    actual = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(directory.iterdir()) if path.is_file()}
    if actual != build.get("file_sha256"):
        raise FoundryBlocked("generated build failed its integrity check")
    return build
def _executed_evaluation(root: Path, state: Mapping[str, Any], *, provider: Any = None) -> dict[str, Any]:
    build = _verify_build(root, state)
    try:
        executed = run_scenarios(root / build["directory"],
                                 expected_manifest_sha256=build["manifest_sha256"], provider=provider)
    except FoundryExecutionBlocked as exc:
        raise FoundryBlocked(str(exc)) from exc
    scenarios = {name: result.model_dump(mode="json") for name, result in sorted(executed.items())}
    family = _read_artifact(root, state, "blueprint")["task_family"]
    for scenario in scenarios.values():
        for execution in scenario["executions"]:
            if execution["accepted"]:
                try:
                    result_model(family).model_validate(execution["output"], strict=True)
                except ValueError as exc:
                    raise FoundryBlocked("executed output violates its declared result contract") from exc
    accepted = all(item["passed"] for item in scenarios.values())
    calls = [call for scenario in scenarios.values() for execution in scenario["executions"]
             for call in execution["evidence"].get("model_calls", [])]
    model_status = ("fixture" if any(call["is_fixture"] for call in calls) else "live") if calls else "not_run"
    reviewed_runs = [execution for scenario in scenarios.values() for execution in scenario["executions"]
                     if family == "reviewed_brief" and any(
                         call["role"] == "reviewer" for call in execution["evidence"].get("model_calls", []))]
    review_calls = [call for execution in reviewed_runs
                    for call in execution["evidence"]["model_calls"] if call["role"] == "reviewer"]
    review_status = ("fixture" if any(call["is_fixture"] for call in review_calls) else "live") if review_calls else "not_run"
    return {"schema_version": "foundry-evaluation/v2", "accepted": accepted,
            "scenarios": scenarios,
            "records": {
                "deterministic": {"kind": "deterministic", "source": "executed_subprocess",
                                  "accepted": accepted, "scenarios": sorted(scenarios)},
                "model": {"kind": "model_execution", "status": model_status,
                          "accepted": accepted if calls else None, "calls": len(calls),
                          "scope": "model execution and result-contract checks"},
                "model_evaluation": {"kind": "model_based", "status": review_status,
                          "accepted": all(execution["output"].get("content_review", {}).get("approved") is True
                                          for execution in reviewed_runs) if review_calls else None,
                          "calls": len(review_calls), "scope": "independent reviewer final content approval"},
                "human": {"kind": "human", "status": "not_requested", "accepted": None}},
            "bundle_sha256": state["artifacts"]["bundle"]["sha256"],
            "build_sha256": state["artifacts"]["build"]["sha256"]}
def evaluate(project: str | Path, *, provider: Any = None) -> dict[str, Any]:
    root, state = _load(project)
    if state["stage"] != FoundryStage.EVALUATE:
        raise FoundryBlocked("evaluation requires a generated or repaired bundle")
    record = _executed_evaluation(root, state, provider=provider)
    _artifact(root, state, "evaluation", record)
    if record["accepted"]:
        if FoundryStage.EVALUATE.value not in state["completed"]:
            state["completed"].append(FoundryStage.EVALUATE.value)
        state["stage"] = FoundryStage.PACKAGE.value
        _event(root, "transition", completed="evaluate", next="package")
    else:
        state["stage"] = FoundryStage.REPAIR.value
        _event(root, "evaluation_failed", failed=[
            name for name, item in record["scenarios"].items() if not item["passed"]
        ])
    _save(root, state)
    return record
def repair(project: str | Path, component: str, *, provider: Any = None) -> dict[str, Any]:
    from .foundry_lifecycle import repair as repair_revision
    return repair_revision(project, component, provider=provider)
def package(project: str | Path) -> dict[str, Any]:  # noqa: A001
    root, state = _load(project)
    if state["stage"] != FoundryStage.PACKAGE:
        raise FoundryBlocked("packaging is only available after evaluation")
    if FoundryStage.PACKAGE.value in state.get("completed", []):
        _verify_terminal(root, state)
        raise FoundryBlocked("project is already packaged")
    evaluation = _read_artifact(root, state, "evaluation")
    if evaluation.get("accepted") is not True:
        raise FoundryBlocked("packaging requires an accepted evaluation")
    if evaluation.get("bundle_sha256") != state.get("artifacts", {}).get("bundle", {}).get("sha256"):
        raise FoundryBlocked("accepted evaluation does not match the current bundle")
    if evaluation.get("build_sha256") != state.get("artifacts", {}).get("build", {}).get("sha256"):
        raise FoundryBlocked("accepted evaluation does not match the generated files")
    build = _verify_build(root, state)
    artifacts = []
    for name in _PACKAGE_INPUTS:
        _read_artifact(root, state, name)
        item = state["artifacts"][name]
        artifacts.append(PackageArtifact(path=item["path"], media_type="application/json", digest=item["sha256"]))
    artifacts.extend(PackageArtifact(path=f"{build['directory']}/{name}", media_type=(
        "text/x-python" if name.endswith(".py") else "application/json" if name.endswith(".json")
        else "text/markdown"), digest=digest)
        for name, digest in sorted(build["file_sha256"].items()))
    spec = AgentSystemSpecification.model_validate(_read_artifact(root, state, "specification"))
    record = PackageRecord(package_id=f"{state['project_id']}-1", name=state["project_id"], version="1",
        specification_id=spec.metadata.specification_id, specification_revision=spec.metadata.revision,
        stage=ProjectStage.PRODUCTION, artifacts=artifacts,
        evaluation_results={name: item["passed"] for name, item in evaluation["scenarios"].items()},
        provenance=["history.jsonl"])
    _artifact(root, state, "package", record)
    _advance(root, state, FoundryStage.PACKAGE)
    _save(root, state)
    return record.model_dump(mode="json")
def _action_for(stage: FoundryStage) -> str:
    return {FoundryStage.CONTRACT: "validate", FoundryStage.GENERATE: "generate",
            FoundryStage.EVALUATE: "evaluate", FoundryStage.REPAIR: "repair",
            FoundryStage.PACKAGE: "package"}.get(stage, "next")
def status(project: str | Path) -> dict[str, Any]:
    root, state = _load(project)
    stage = FoundryStage(state["stage"])
    done = stage is FoundryStage.PACKAGE and stage.value in state["completed"]
    result = {"project_id": state["project_id"], "stage": stage.value,
              "completed": list(state["completed"]), "next_action": _action_for(stage),
              "artifacts": dict(state["artifacts"]), "updated_at": state["updated_at"]}
    if done:
        _verify_terminal(root, state)
        result["next_action"] = "complete"
    if stage is FoundryStage.FRAME:
        question = IntakeState.model_validate(state["intake"]).next_question()
        result["question"] = question.model_dump(mode="json") if question else None
    return result
def resume(project: str | Path) -> dict[str, Any]:
    """Verify persisted artifacts, then return the exact safe continuation point."""
    root, state = _load(project)
    # A build published (``generated-<digest>/``) before its "build" artifact was recorded is
    # not an error: ``publish_build`` is idempotent and a re-run of ``generate`` verifies the
    # same bytes and records them (``test_foundry_recovery`` at the ``build_published`` point).
    if "build" in state.get("artifacts", {}):
        _verify_build(root, state)
    if FoundryStage.PACKAGE.value in state.get("completed", []):
        _verify_terminal(root, state)
    else:
        for name in state.get("artifacts", {}):
            _read_artifact(root, state, name)
    return status(root)
def _verify_terminal(root: Path, state: Mapping[str, Any]) -> None:
    if state.get("stage") != FoundryStage.PACKAGE.value:
        raise FoundryBlocked("completed package has an inconsistent terminal stage")
    values = {name: _read_artifact(root, state, name) for name in _TERMINAL_ARTIFACTS}
    if values["evaluation"].get("accepted") is not True:
        raise FoundryBlocked("completed package references an unaccepted evaluation")
    if values["evaluation"].get("bundle_sha256") != state["artifacts"]["bundle"]["sha256"]:
        raise FoundryBlocked("completed package evaluation does not match its bundle")
    if values["evaluation"].get("build_sha256") != state["artifacts"]["build"]["sha256"]:
        raise FoundryBlocked("completed package evaluation does not match its generated files")
    _verify_build(root, state)
    try:
        record = PackageRecord.model_validate(values["package"])
    except (TypeError, ValueError) as exc:
        raise FoundryBlocked("completed package record is invalid") from exc
    expected = {(state["artifacts"][name]["path"], state["artifacts"][name]["sha256"])
                for name in _PACKAGE_INPUTS}
    actual = {(item.path, item.digest) for item in record.artifacts}
    if not expected.issubset(actual):
        raise FoundryBlocked("completed package record omits required artifacts")
get_status = status
advance = next_action
create_project = create
answer_intake = answer
__all__ = ["FoundryBlocked", "FoundryStage", "STAGES", "advance", "answer", "answer_intake",
           "create", "create_project", "evaluate", "generate", "get_status", "next_action", "package",
           "repair", "resume", "status", "validate"]
