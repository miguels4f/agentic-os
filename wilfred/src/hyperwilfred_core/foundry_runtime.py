"""Deterministic local bundle adapter and optional bounded-decision lane."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import Field

from .foundry_design import result_schema, topology_for
from .foundry_families import FAMILIES, family_source
from .foundry_models import AgentSystemSpecification, CanonicalModel, DataContract
from .foundry_validation import ValidationReport, validate_specification

BUNDLE_SCHEMA = "local-python-agent-bundle/v2"
RUNTIME_VERSION = "bounded-families/2"
SUPPORTED_TASK_FAMILY = "notes_to_actions"


class CapabilitySupport(StrEnum):
    DIRECT = "direct"
    EMULATED = "emulated"
    UNSUPPORTED = "unsupported"


class CapabilityMapping(CanonicalModel):
    capability: str
    support: CapabilitySupport
    rationale: str
    implementation: str | None = None


class HumanCapabilityDecision(CanonicalModel):
    capability: str
    decision: Literal["defer", "exclude"]
    rationale: str


class BundleGenerationBlocked(ValueError):
    def __init__(self, message: str, *, capabilities: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.capabilities = tuple(sorted(set(capabilities)))


class LocalPythonBundle(CanonicalModel):
    schema_version: str = BUNDLE_SCHEMA
    bundle_id: str
    manifest: dict[str, Any]
    instructions: str
    config: dict[str, Any]
    contracts: list[dict[str, Any]]
    evaluations: list[dict[str, Any]]
    operating_runbook: list[str]
    capability_mapping: list[CapabilityMapping]
    human_decisions: list[HumanCapabilityDecision] = Field(default_factory=list)
    source_files: dict[str, str] = Field(default_factory=dict)

    def files(self) -> dict[str, str]:
        payloads: dict[str, Any] = {
            "manifest.json": self.manifest,
            "config.json": self.config,
            "contracts.json": self.contracts,
            "evaluations.json": self.evaluations,
            "capabilities.json": [x.model_dump(mode="json") for x in self.capability_mapping],
        }
        files = {
            name: json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            for name, value in payloads.items()
        }
        files["INSTRUCTIONS.md"] = self.instructions.rstrip() + "\n"
        files["RUNBOOK.md"] = "\n".join(self.operating_runbook).rstrip() + "\n"
        files["runner.py"] = self.source_files.get("runner.py", family_source(
            self.config["task_family"], _RUNNER_SOURCE, self.config.get("seeded_defect", False)
        ))
        expected = self.manifest.get("file_sha256", {})
        for name, digest in expected.items():
            if hashlib.sha256(files[name].encode()).hexdigest() != digest:
                raise ValueError(f"bundle file {name!r} no longer matches its manifest digest")
        return dict(sorted(files.items()))

def _cap(
    name: str, support: CapabilitySupport, why: str, how: str | None = None
) -> CapabilityMapping:
    return CapabilityMapping(
        capability=name, support=support, rationale=why, implementation=how
    )


_CAPABILITIES = {
    "instructions": _cap(
        "instructions", CapabilitySupport.DIRECT, "Markdown is consumed directly."
    ),
    "configuration": _cap(
        "configuration", CapabilitySupport.DIRECT, "Configuration is local JSON."
    ),
    "data_contracts": _cap(
        "data_contracts", CapabilitySupport.DIRECT, "Contracts remain JSON Schema."
    ),
    "evaluations": _cap("evaluations", CapabilitySupport.DIRECT, "Evaluations remain JSON."),
    "state_machine": _cap(
        "state_machine", CapabilitySupport.UNSUPPORTED,
        "Host lifecycle stages do not implement arbitrary generated state transitions.",
    ),
    "human_gates": _cap(
        "human_gates", CapabilitySupport.UNSUPPORTED,
        "Host approval APIs are separate; generated runners do not implement specification gates.",
    ),
    "tool_execution": _cap(
        "tool_execution",
        CapabilitySupport.UNSUPPORTED,
        "No arbitrary tool contracts are bound; bounded document lookup is a separate capability.",
    ),
    "bounded_document_lookup": _cap(
        "bounded_document_lookup", CapabilitySupport.UNSUPPORTED,
        "Only evidence-agent families implement lookup over supplied document ids and text.",
    ),
    "model_execution": _cap(
        "model_execution", CapabilitySupport.UNSUPPORTED,
        "Notes invoke no model; evidence families require an explicitly selected host provider."
    ),
    "persistent_memory": _cap(
        "persistent_memory", CapabilitySupport.UNSUPPORTED, "No durable memory backend is bundled."
    ),
    "external_side_effects": _cap(
        "external_side_effects",
        CapabilitySupport.UNSUPPORTED,
        "Runner never performs external actions.",
    ),
}
_BASE = {"configuration", "data_contracts", "evaluations", "instructions"}


def capability_mapping(required: Sequence[str] = ()) -> list[CapabilityMapping]:
    """Map every request; unknown capabilities are explicit and unsupported."""
    return [
        _CAPABILITIES.get(
            name,
            _cap(name, CapabilitySupport.UNSUPPORTED, "Capability is not declared."),
        )
        for name in sorted(set(required) | _BASE)
    ]


def _validate_family_contract(spec, blueprint, family: str) -> None:
    """Accept only the contracts and actors implemented by the bounded family runner."""
    topology = topology_for(family)
    model = family != SUPPORTED_TASK_FAMILY
    kind = "agent" if model else "service"
    units = {unit.id: unit for unit in topology.units}
    edges = {(edge.source, edge.target, edge.contract) for edge in topology.handoffs}
    schema = result_schema(family)

    def result_contracts(contracts):
        return len(contracts) == 1 and (
            contracts[0].name == "result" and contracts[0].required
            and contracts[0].media_type == "application/json"
            and contracts[0].json_schema == schema)

    compatible = (
        result_contracts(spec.contracts) and not spec.handoffs
        and [(state.id, state.terminal) for state in spec.states] == [("complete", True)]
        and {(node.id, node.kind.value) for node in spec.topology.nodes}
        == {(identifier, kind) for identifier in units}
        and len(spec.topology.edges) == len(edges)
        and {(edge.source, edge.target, edge.relation) for edge in spec.topology.edges} == edges
        and {component.id for component in spec.components} == set(units)
        and all(component.kind.value == kind and not component.inputs
                and result_contracts(component.outputs)
                and component.responsibilities == units[component.id].responsibilities
                for component in spec.components))
    if blueprint is not None:
        allowed = {"schema_version", "task_family", "topology_kind", "steps"}
        step_fields = {"id", "component_id", "model_call", "model_call_justification", "depends_on"}
        steps = blueprint.get("steps", [])
        compatible = compatible and (
            set(blueprint) <= allowed and blueprint.get("task_family") == family
            and blueprint.get("topology_kind") == topology.kind.value
            and isinstance(steps, list) and len(steps) == len(units)
            and all(isinstance(step, Mapping) for step in steps)
            and {step.get("id") for step in steps} == set(units)
            and all(set(step) <= step_fields and step.get("component_id") == step.get("id")
                    and step.get("model_call") is model
                    and isinstance(step.get("depends_on", []), list)
                    and sorted(step.get("depends_on", []))
                    == sorted(source for source, target, _ in edges if target == step["id"])
                    for step in steps))
    if not compatible:
        raise BundleGenerationBlocked(
            f"Why: specification or blueprint has an incompatible {family} executable contract "
            "or topology. Unlock: use the canonical family design; custom input/output "
            "contracts, components and handoffs require a reviewed runtime adapter.")


class LocalPythonManifestAdapter:
    runtime = "local-python"

    def build_bundle(
        self,
        specification: AgentSystemSpecification | Mapping[str, Any],
        *,
        blueprint: Mapping[str, Any] | None = None,
        required_capabilities: Sequence[str] = (),
        human_decisions: Sequence[HumanCapabilityDecision | Mapping[str, str]] = (),
        task_family: str = SUPPORTED_TASK_FAMILY,
        seeded_defect: bool = False,
    ) -> LocalPythonBundle:
        if task_family not in FAMILIES:
            raise BundleGenerationBlocked(
                f"Why: task family {task_family!r} has no bounded implementation. "
                f"Unlock: select one of {sorted(FAMILIES)} or install a reviewed runtime adapter."
            )
        report = validate_specification(specification, blueprint=blueprint)
        if not report.valid:
            raise BundleGenerationBlocked(
                "Why: " + _validation_message(report) +
                " Unlock: correct the reported specification paths and generate again."
            )
        spec = (
            specification if isinstance(specification, AgentSystemSpecification)
            else AgentSystemSpecification.model_validate(specification)
        )
        unimplemented = []
        if spec.transitions:
            unimplemented.append("state_machine")
        if spec.human_gates:
            unimplemented.append("human_gates")
        if any(component.tools for component in spec.components):
            unimplemented.append("tool_execution")
        if unimplemented:
            raise BundleGenerationBlocked(
                "Why: the specification requires unimplemented runtime behavior: "
                + ", ".join(unimplemented)
                + ". Unlock: remove those requirements or install a reviewed adapter; "
                "metadata and defer decisions cannot implement them.", capabilities=unimplemented,
            )
        _validate_family_contract(spec, blueprint, task_family)
        derived = set(required_capabilities)
        if task_family != SUPPORTED_TASK_FAMILY:
            derived.update({"model_execution", "bounded_document_lookup"})
        if spec.memory.retention or spec.memory.scopes or spec.memory.write_rules:
            derived.add("persistent_memory")
        if blueprint and any(step.get("model_call") for step in blueprint.get("steps", ())):
            derived.add("model_execution")
        read_only = {"get", "list", "read", "search", "view"}
        if any(action.strip().casefold().rsplit(":", 1)[-1] not in read_only
               for component in spec.components for permission in component.permissions
               if permission.effect.value != "deny" for action in permission.actions):
            derived.add("external_side_effects")
        mappings = capability_mapping(sorted(derived))
        if task_family != SUPPORTED_TASK_FAMILY:
            supported = {
                "model_execution": "Explicit trusted host provider; fresh bounded model contexts.",
                "bounded_document_lookup": "Only id/text lookup on supplied evidence; "
                "no custom tool binding or external effects.",
            }
            mappings = [_cap(x.capability, CapabilitySupport.DIRECT, supported[x.capability])
                        if x.capability in supported else x for x in mappings]
        decisions = [
            x if isinstance(x, HumanCapabilityDecision)
            else HumanCapabilityDecision.model_validate(x)
            for x in human_decisions
        ]
        decided = {x.capability: x for x in decisions}
        excluded = sorted(x.capability for x in decisions
                          if x.decision == "exclude" and x.capability in derived)
        if excluded:
            raise BundleGenerationBlocked(
                "Why: Excluded capabilities are still required by the design: "
                + ", ".join(excluded)
                + ". Unlock: remove the requirement or provide a reviewed implementation.",
                capabilities=excluded,
            )
        unsupported = [
            x.capability for x in mappings
            if x.support is CapabilitySupport.UNSUPPORTED and x.capability not in decided
        ]
        if unsupported:
            raise BundleGenerationBlocked(
                "Why: unsupported capabilities have no bounded local implementation: "
                + ", ".join(unsupported)
                + ". Unlock: record a defer decision or install a reviewed adapter.",
                capabilities=unsupported,
            )
        unknown = sorted(set(decided) - {x.capability for x in mappings})
        if unknown:
            raise BundleGenerationBlocked(
                "Why: decisions reference unrequested capabilities: " + ", ".join(unknown)
                + ". Unlock: remove those decisions or declare the capabilities as required."
            )
        config = {
            "runtime": self.runtime,
            "specification": spec.model_dump(mode="json"),
            "blueprint": dict(blueprint or {}),
            "deferred_capabilities": sorted(
                x.capability for x in decisions if x.decision == "defer"
            ),
            "execution": {"entrypoint": "runner.py", "network": False, "side_effects": False,
                          "isolation": "macos-sandbox-exec-required",
                          "provider_boundary": "trusted-host-stdio"},
            "task_family": task_family,
            "architecture": FAMILIES[task_family],
            "seeded_defect": bool(seeded_defect),
        }
        contracts = [x.model_dump(mode="json") for x in _all_contracts(spec)]
        evaluations = [x.model_dump(mode="json") for x in spec.evaluations]
        stable = json.dumps(
            {"config": config, "contracts": contracts, "evaluations": evaluations},
            ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        )
        bundle_id = hashlib.sha256(stable.encode()).hexdigest()[:20]
        names = [
            "INSTRUCTIONS.md", "RUNBOOK.md", "capabilities.json", "config.json",
            "contracts.json", "evaluations.json", "runner.py",
        ]
        instructions = _instructions(spec) if task_family == SUPPORTED_TASK_FAMILY else (
            f"# {spec.metadata.name}\n\n{FAMILIES[task_family]['rationale']}\n\n"
            "Execute through Wilfred with an explicit host model provider and enforced OS sandbox."
        )
        runbook = _runbook()
        if task_family != SUPPORTED_TASK_FAMILY:
            runbook[4] = "3. Supply question and documents to execute_bundle with a host provider."
        file_payloads = _render_files(
            config, contracts, evaluations, mappings, instructions, runbook
        )
        manifest = {
            "schema_version": BUNDLE_SCHEMA, "bundle_id": bundle_id,
            "name": spec.metadata.name, "specification_id": spec.metadata.specification_id,
            "specification_revision": spec.metadata.revision,
            "runtime": self.runtime, "runtime_version": RUNTIME_VERSION,
            "task_family": task_family, "files": sorted(names),
            "architecture": FAMILIES[task_family],
            "file_sha256": {
                name: hashlib.sha256(content.encode()).hexdigest()
                for name, content in sorted(file_payloads.items())
            },
        }
        return LocalPythonBundle(
            bundle_id=bundle_id, manifest=manifest, instructions=instructions, config=config,
            contracts=contracts, evaluations=evaluations, operating_runbook=runbook,
            capability_mapping=mappings,
            human_decisions=sorted(decisions, key=lambda x: x.capability),
            source_files={"runner.py": file_payloads["runner.py"]},
        )

    compile = build_bundle


def build_local_python_bundle(
    specification: AgentSystemSpecification | Mapping[str, Any], **kwargs: Any
) -> LocalPythonBundle:
    return LocalPythonManifestAdapter().build_bundle(specification, **kwargs)


def repair_bundle_source(bundle: LocalPythonBundle) -> LocalPythonBundle:
    """Replace only the recognized defective source guard; preserve all accepted components."""
    original = bundle.files()["runner.py"]
    marker = "if True:  # seeded_empty_defect"
    if original.count(marker) != 1:
        raise BundleGenerationBlocked("Why: no unique seeded source defect. "
                                      "Unlock: inspect the actual failing component before repair.")
    repaired = original.replace(marker, "if False:  # seeded_empty_defect", 1)
    manifest = json.loads(json.dumps(bundle.manifest))
    digest = hashlib.sha256(repaired.encode()).hexdigest()
    manifest["file_sha256"]["runner.py"] = digest
    manifest["bundle_id"] = hashlib.sha256((bundle.bundle_id + digest).encode()).hexdigest()[:20]
    return bundle.model_copy(update={"source_files": {"runner.py": repaired},
                                     "manifest": manifest, "bundle_id": manifest["bundle_id"]})


def _validation_message(report: ValidationReport) -> str:
    return "Specification validation failed: " + "; ".join(
        f"{x.path}: {x.code}" for x in report.issues
    )


def _all_contracts(spec: AgentSystemSpecification) -> list[DataContract]:
    found = {x.name: x for x in spec.contracts}
    for component in spec.components:
        for contract in (*component.inputs, *component.outputs):
            found.setdefault(contract.name, contract)
        for tool in component.tools:
            for tool_contract in (tool.input, tool.output):
                if tool_contract is not None:
                    found.setdefault(tool_contract.name, tool_contract)
    for handoff in spec.handoffs:
        found.setdefault(handoff.contract.name, handoff.contract)
    return [found[name] for name in sorted(found)]


def _instructions(spec: AgentSystemSpecification) -> str:
    components = [
        f"- `{x.id}`: " + "; ".join(x.responsibilities)
        for x in sorted(spec.components, key=lambda x: x.id)
    ]
    return "\n".join([
        f"# {spec.metadata.name}", "", spec.problem.statement, "", "## Components", "",
        *(components or ["- No components declared."]), "",
        "The runner converts workshop notes into deterministic JSON actions.",
        "It validates every action and never invokes tools, networks, models, or side effects.",
    ])


def _runbook() -> list[str]:
    return [
        "# Operating runbook", "",
        "1. Inspect `manifest.json`, `capabilities.json`, and `config.json`.",
        "2. Inspect `contracts.json` and `evaluations.json` before supplying data.",
        "3. Run: `echo '{\"notes\":\"- Ana: Draft brief by 2026-10-01\"}' | python runner.py`.",
        "4. Treat the returned actions as proposals; the runner performs no action itself.",
        "5. An error explains why execution stopped and how to unlock it.",
    ]


_RUNNER_SOURCE = '''#!/usr/bin/env python3
import json, pathlib, re, sys
root = pathlib.Path(__file__).resolve().parent
config = json.loads((root / "config.json").read_text(encoding="utf-8"))
def deny_network(event, args):
    if event.startswith("socket."):
        raise RuntimeError("network is disabled for this bounded runtime")
sys.addaudithook(deny_network)
def block(why, unlock):
    print(json.dumps({"accepted": False, "error": {"why": why, "unlock": unlock}},
                     ensure_ascii=False, sort_keys=True))
    raise SystemExit(2)
try:
    payload = json.loads(sys.stdin.readline())
except (json.JSONDecodeError, UnicodeDecodeError):
    block("input is not valid JSON", "send an object with a string 'notes' field")
if not isinstance(payload, dict):
    block("input must be a JSON object", "wrap workshop text as {\\\"notes\\\": \\\"...\\\"}")
notes = payload.get("notes")
if notes is None and isinstance(payload.get("input"), dict):
    notes = payload["input"].get("notes")
if not isinstance(notes, str):
    block("notes must be a string", "provide workshop notes in the 'notes' field")
actions = []
for number, raw in enumerate(notes.splitlines(), 1):
    line = re.sub(r"^\\s*(?:[-*]|\\d+[.)])\\s*", "", raw).strip()
    if not line or line.startswith("#"):
        continue
    due_match = re.search(r"\\s+by\\s+(\\d{4}-\\d{2}-\\d{2})$", line, re.I)
    due = due_match.group(1) if due_match else None
    body = line[:due_match.start()].strip() if due_match else line
    owner, separator, title = body.partition(":")
    if not separator:
        owner, title = "", body
    owner, title = owner.strip(), title.strip()
    if not title or len(title) > 240:
        block(f"line {number} is not a bounded action", "use a 1-240 character action line")
    action = {"title": title, "source_line": number}
    if owner: action["owner"] = owner
    if due: action["due_date"] = due
    actions.append(action)
if not actions:
    if False:  # seeded_empty_defect
        block("seeded edge-case defect rejected empty notes", "repair source; seeded_defect=false")
result = {"accepted": True, "schema_version": "workshop-actions/v1", "actions": actions}
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
'''


def _render_files(
    config: dict[str, Any], contracts: list[dict[str, Any]], evaluations: list[dict[str, Any]],
    mappings: list[CapabilityMapping], instructions: str, runbook: list[str],
) -> dict[str, str]:
    values: dict[str, Any] = {
        "config.json": config, "contracts.json": contracts, "evaluations.json": evaluations,
        "capabilities.json": [x.model_dump(mode="json") for x in mappings],
    }
    files = {name: json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
             for name, value in values.items()}
    files.update({"INSTRUCTIONS.md": instructions.rstrip() + "\n",
                  "RUNBOOK.md": "\n".join(runbook).rstrip() + "\n",
                  "runner.py": family_source(config["task_family"], _RUNNER_SOURCE,
                                              config.get("seeded_defect", False))})
    return files


class DecisionImpact(StrEnum):
    LOW = "low"
    HIGH = "high"
    IRREVERSIBLE = "irreversible"


class BoundedDecisionRequest(CanonicalModel):
    decision_id: str
    question: str
    options: list[str] = Field(min_length=1, max_length=8)
    baseline_option: str
    impact: DecisionImpact = DecisionImpact.LOW


class JevConfig(CanonicalModel):
    enabled: bool = False
    available: bool = False
    max_options: int = Field(default=8, ge=1, le=8)


class BoundedDecisionResult(CanonicalModel):
    decision_id: str
    selected_option: str
    lane: Literal["assist", "baseline"]
    rationale: str
    human_approval_required: bool = False
    authorizes_action: Literal[False] = False


class JevLane(Protocol):
    def decide(self, request: BoundedDecisionRequest) -> tuple[str, str]: ...


_HIGH_IMPACT = re.compile(r"\b(delete|deploy|publish|pay|purchase|irreversible|legal|medical)\b", re.I)  # noqa: E501


def decide_bounded(
    request: BoundedDecisionRequest, config: JevConfig, lane: JevLane | None = None
) -> BoundedDecisionResult:
    """Use Assist only for bounded low-impact choices; record fallback otherwise."""
    if request.baseline_option not in request.options:
        raise ValueError("baseline_option must be one of options")
    if request.impact is not DecisionImpact.LOW or _HIGH_IMPACT.search(request.question):
        return BoundedDecisionResult(
            decision_id=request.decision_id, selected_option=request.baseline_option,
            lane="baseline", rationale="Consequential decision requires human approval.",
            human_approval_required=True,
        )
    if not config.enabled or not config.available or lane is None:
        return BoundedDecisionResult(
            decision_id=request.decision_id, selected_option=request.baseline_option,
            lane="baseline", rationale="Assist disabled or unavailable; recorded baseline used.",
        )
    if len(request.options) > config.max_options:
        raise ValueError("decision exceeds configured option bound")
    try:
        selected, rationale = lane.decide(request)
    except Exception as exc:
        return BoundedDecisionResult(
            decision_id=request.decision_id, selected_option=request.baseline_option,
            lane="baseline", rationale=f"Assist failed ({type(exc).__name__}); baseline used.",
        )
    if selected not in request.options:
        raise ValueError("Assist selected an option outside the declared bound")
    return BoundedDecisionResult(
        decision_id=request.decision_id, selected_option=selected, lane="assist", rationale=rationale
    )


class BenchmarkObservation(CanonicalModel):
    lane: Literal["assist", "baseline"]
    quality: float = Field(ge=0, le=1)
    accuracy: float = Field(ge=0, le=1)
    approximate_cost: float = Field(ge=0)
    latency_ms: float = Field(ge=0)
    low_confidence: bool = False
    failed: bool = False


class BenchmarkThresholds(CanonicalModel):
    minimum_quality_gain: float = Field(default=0.0, ge=0)
    minimum_accuracy_gain: float = Field(default=0.0, ge=0)
    maximum_cost_ratio: float = Field(default=1.5, ge=0)
    maximum_latency_ratio: float = Field(default=1.5, ge=0)
    maximum_low_confidence_rate: float = Field(default=0.1, ge=0, le=1)
    maximum_failure_rate: float = Field(default=0.05, ge=0, le=1)


class BenchmarkReport(CanonicalModel):
    baseline: dict[str, float]
    assist: dict[str, float]
    thresholds_passed: bool
    recommend_activation: bool
    reasons: list[str]


def benchmark_assist(
    observations: Sequence[BenchmarkObservation], thresholds: BenchmarkThresholds | None = None
) -> BenchmarkReport:
    """Recommend Assist only when quality, cost, latency, and reliability all pass."""
    thresholds = thresholds or BenchmarkThresholds()
    grouped = {lane: [x for x in observations if x.lane == lane] for lane in ("baseline", "assist")}
    if not all(grouped.values()):
        raise ValueError("benchmark requires baseline and assist observations")
    metrics = {lane: _aggregate(items) for lane, items in grouped.items()}
    baseline, assist = metrics["baseline"], metrics["assist"]
    checks = {
        "quality": assist["quality"] >= baseline["quality"] + thresholds.minimum_quality_gain,
        "accuracy": assist["accuracy"] >= baseline["accuracy"] + thresholds.minimum_accuracy_gain,
        "cost": assist["approximate_cost"] <= _limit(baseline["approximate_cost"], thresholds.maximum_cost_ratio),  # noqa: E501
        "latency": assist["latency_ms"] <= _limit(baseline["latency_ms"], thresholds.maximum_latency_ratio),  # noqa: E501
        "low_confidence": assist["low_confidence_rate"] <= thresholds.maximum_low_confidence_rate,
        "failures": assist["failure_rate"] <= thresholds.maximum_failure_rate,
    }
    passed = all(checks.values())
    return BenchmarkReport(
        baseline=baseline, assist=assist, thresholds_passed=passed, recommend_activation=passed,
        reasons=[f"{name}: {'pass' if ok else 'fail'}" for name, ok in checks.items()],
    )


def _aggregate(items: Sequence[BenchmarkObservation]) -> dict[str, float]:
    count = len(items)
    return {
        "quality": sum(x.quality for x in items) / count,
        "accuracy": sum(x.accuracy for x in items) / count,
        "approximate_cost": sum(x.approximate_cost for x in items) / count,
        "latency_ms": sum(x.latency_ms for x in items) / count,
        "low_confidence_rate": sum(x.low_confidence for x in items) / count,
        "failure_rate": sum(x.failed for x in items) / count,
    }


def _limit(baseline: float, ratio: float) -> float:
    return baseline * ratio if baseline else 0.0


__all__ = [
    "BUNDLE_SCHEMA", "RUNTIME_VERSION", "SUPPORTED_TASK_FAMILY", "BenchmarkObservation",
    "BenchmarkReport", "BenchmarkThresholds", "BoundedDecisionRequest", "BoundedDecisionResult",
    "BundleGenerationBlocked", "CapabilityMapping", "CapabilitySupport", "DecisionImpact",
    "HumanCapabilityDecision", "JevConfig", "JevLane", "LocalPythonBundle",
    "LocalPythonManifestAdapter", "benchmark_assist", "build_local_python_bundle",
    "capability_mapping", "decide_bounded", "repair_bundle_source",
]
