"""Provider-neutral contracts for the Agent Foundry.

The models in this module describe an agent system, not a particular implementation.
Runtime adapters may compile these records into their own configuration, but vendor and
framework concepts do not belong in this canonical layer.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AGENT_SYSTEM_SPECIFICATION_SCHEMA = "agent-system-specification/v1"
PACKAGE_RECORD_SCHEMA = "agent-package/v1"


class CanonicalModel(BaseModel):
    """Strict base model with deterministic, transport-safe JSON serialization."""

    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, populate_by_name=True
    )

    def to_stable_json(self, *, indent: int | None = None) -> str:
        """Serialize deterministically for hashing, comparison, and persistence."""
        return json.dumps(
            self.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            indent=indent,
            separators=(",", ":") if indent is None else None,
            sort_keys=True,
        )

    def stable_json(self, *, indent: int | None = None) -> str:
        """Backward-friendly alias for :meth:`to_stable_json`."""
        return self.to_stable_json(indent=indent)


class VersionedModel(CanonicalModel):
    """Base for records whose schema must be explicitly compatible."""

    supported_schema: ClassVar[str]
    schema_version: str

    @model_validator(mode="before")
    @classmethod
    def reject_incompatible_schema(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        received = value.get("schema_version", cls.supported_schema)
        if received != cls.supported_schema:
            raise ValueError(
                f"unsupported schema_version {received!r}; expected {cls.supported_schema!r}. "
                "Migrate the record before loading it."
            )
        return value


class ProjectStage(StrEnum):
    DISCOVERY = "discovery"
    SPECIFICATION = "specification"
    DESIGN = "design"
    IMPLEMENTATION = "implementation"
    EVALUATION = "evaluation"
    PILOT = "pilot"
    PRODUCTION = "production"
    RETIRED = "retired"


class RecordStatus(StrEnum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class EvidenceKind(StrEnum):
    OBSERVATION = "observation"
    INTERVIEW = "interview"
    DOCUMENT = "document"
    DATA = "data"
    EXPERIMENT = "experiment"
    DECISION = "decision"


class ComponentKind(StrEnum):
    AGENT = "agent"
    HUMAN = "human"
    SERVICE = "service"
    DATA_STORE = "data_store"
    INTERFACE = "interface"


class PermissionEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class DecisionOutcome(StrEnum):
    REUSE = "reuse"
    ADAPT = "adapt"
    BUILD = "build"
    DEFER = "defer"
    REJECT = "reject"


class EvaluationKind(StrEnum):
    DETERMINISTIC = "deterministic"
    MODEL_BASED = "model_based"
    HUMAN = "human"
    OPERATIONAL = "operational"


class Metadata(CanonicalModel):
    specification_id: str
    name: str
    revision: str
    description: str = ""
    owners: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)


class Evidence(CanonicalModel):
    id: str
    kind: EvidenceKind
    summary: str
    status: RecordStatus = RecordStatus.PROPOSED
    source: str
    collected_at: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ProblemStatement(CanonicalModel):
    statement: str
    affected_parties: list[str] = Field(default_factory=list)
    desired_outcomes: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    status: RecordStatus = RecordStatus.PROPOSED


class Assumption(CanonicalModel):
    id: str
    statement: str
    status: RecordStatus = RecordStatus.PROPOSED
    evidence_ids: list[str] = Field(default_factory=list)
    consequence_if_false: str = ""


class Contradiction(CanonicalModel):
    id: str
    statement: str
    evidence_ids: list[str] = Field(default_factory=list)
    status: RecordStatus = RecordStatus.PROPOSED
    resolution: str | None = None


class TopologyNode(CanonicalModel):
    id: str
    kind: ComponentKind
    label: str


class TopologyEdge(CanonicalModel):
    source: str
    target: str
    relation: str
    description: str = ""


class Topology(CanonicalModel):
    nodes: list[TopologyNode] = Field(default_factory=list)
    edges: list[TopologyEdge] = Field(default_factory=list)


class DataContract(CanonicalModel):
    name: str
    description: str = ""
    media_type: str = "application/json"
    json_schema: dict[str, Any] = Field(
        default_factory=dict, validation_alias="schema", serialization_alias="schema"
    )
    required: bool = True


class ToolContract(CanonicalModel):
    name: str
    purpose: str
    input: DataContract | None = None
    output: DataContract | None = None


class Permission(CanonicalModel):
    resource: str
    actions: list[str] = Field(default_factory=list)
    effect: PermissionEffect = PermissionEffect.ALLOW
    reason: str = ""


class Component(CanonicalModel):
    id: str
    kind: ComponentKind
    description: str = ""
    responsibilities: list[str] = Field(default_factory=list)
    inputs: list[DataContract] = Field(default_factory=list)
    outputs: list[DataContract] = Field(default_factory=list)
    tools: list[ToolContract] = Field(default_factory=list)
    permissions: list[Permission] = Field(default_factory=list)


class ContextPolicy(CanonicalModel):
    sources: list[str] = Field(default_factory=list)
    selection_rules: list[str] = Field(default_factory=list)
    maximum_size: int | None = Field(default=None, ge=1)


class MemoryPolicy(CanonicalModel):
    scopes: list[str] = Field(default_factory=list)
    retention: str = ""
    write_rules: list[str] = Field(default_factory=list)
    read_rules: list[str] = Field(default_factory=list)
    deletion_rules: list[str] = Field(default_factory=list)


class State(CanonicalModel):
    id: str
    description: str = ""
    terminal: bool = False


class Transition(CanonicalModel):
    source: str
    target: str
    trigger: str
    condition: str | None = None
    actions: list[str] = Field(default_factory=list)


class Handoff(CanonicalModel):
    source_component: str
    target_component: str
    trigger: str
    contract: DataContract
    acceptance_conditions: list[str] = Field(default_factory=list)


class FailurePolicy(CanonicalModel):
    retry_limit: int = Field(default=0, ge=0)
    retry_strategy: str = "none"
    timeout_seconds: float | None = Field(default=None, gt=0)
    failure_conditions: list[str] = Field(default_factory=list)
    fallback: str | None = None
    escalation: str | None = None


class HumanGate(CanonicalModel):
    id: str
    trigger: str
    approvers: list[str] = Field(default_factory=list)
    decision_options: list[str] = Field(default_factory=lambda: ["approve", "reject"])
    timeout_seconds: float | None = Field(default=None, gt=0)
    on_timeout: str = "escalate"


class ExternalReuseDecision(CanonicalModel):
    candidate: str
    outcome: DecisionOutcome
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


class Threshold(CanonicalModel):
    metric: str
    operator: Literal[">", ">=", "<", "<=", "==", "!="]
    value: float | int | bool | str


class Evaluation(CanonicalModel):
    id: str
    kind: EvaluationKind
    description: str
    thresholds: list[Threshold] = Field(default_factory=list)
    required: bool = True
    evidence_required: list[str] = Field(default_factory=list)


class RuntimeTarget(CanonicalModel):
    environment: str
    execution_model: str
    constraints: list[str] = Field(default_factory=list)
    service_levels: dict[str, str] = Field(default_factory=dict)


class CapabilityGap(CanonicalModel):
    capability: str
    impact: str
    mitigation: str | None = None
    status: RecordStatus = RecordStatus.PROPOSED


class DecisionRecord(CanonicalModel):
    id: str
    question: str
    decision: str
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)
    assumption_ids: list[str] = Field(default_factory=list)
    supersedes: list[str] = Field(default_factory=list)


class AgentSystemSpecificationV1(VersionedModel):
    """Canonical, implementation-independent Agent System Specification v1."""

    supported_schema: ClassVar[str] = AGENT_SYSTEM_SPECIFICATION_SCHEMA
    schema_version: Literal["agent-system-specification/v1"] = "agent-system-specification/v1"
    metadata: Metadata
    stage: ProjectStage
    problem: ProblemStatement
    evidence: list[Evidence] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    topology: Topology
    components: list[Component] = Field(default_factory=list)
    contracts: list[DataContract] = Field(default_factory=list)
    context: ContextPolicy = Field(default_factory=ContextPolicy)
    memory: MemoryPolicy = Field(default_factory=MemoryPolicy)
    states: list[State] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)
    handoffs: list[Handoff] = Field(default_factory=list)
    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)
    human_gates: list[HumanGate] = Field(default_factory=list)
    external_reuse: list[ExternalReuseDecision] = Field(default_factory=list)
    evaluations: list[Evaluation] = Field(default_factory=list)
    runtime_target: RuntimeTarget
    capability_gaps: list[CapabilityGap] = Field(default_factory=list)
    decisions: list[DecisionRecord] = Field(default_factory=list)


class PackageArtifact(CanonicalModel):
    path: str
    media_type: str = "application/octet-stream"
    digest: str | None = None


class PackageRecordV1(VersionedModel):
    """Portable delivery record for a compiled agent-system package."""

    supported_schema: ClassVar[str] = PACKAGE_RECORD_SCHEMA
    schema_version: Literal["agent-package/v1"] = "agent-package/v1"
    package_id: str
    name: str
    version: str
    specification_id: str
    specification_revision: str
    stage: ProjectStage
    artifacts: list[PackageArtifact] = Field(default_factory=list)
    evaluation_results: dict[str, bool | int | float | str] = Field(default_factory=dict)
    provenance: list[str] = Field(default_factory=list)


# Concise public names for callers that target the current canonical version.
AgentSystemSpecification = AgentSystemSpecificationV1
PackageRecord = PackageRecordV1


__all__ = [name for name in globals() if not name.startswith("_")]
