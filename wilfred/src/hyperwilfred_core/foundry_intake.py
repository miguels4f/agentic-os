"""Deterministic, provider-neutral intake for Agent Foundry specifications."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from .foundry_models import CanonicalModel


class EvidenceStatus(StrEnum):
    GROUNDED = "grounded"
    ASSUMED = "assumed"
    CONTRADICTORY = "contradictory"
    MISSING = "missing"


class IntakeMode(StrEnum):
    GUIDED = "guided"
    AUTOPILOT = "autopilot"


class IntakeField(StrEnum):
    OUTCOME = "outcome"
    ACTORS = "actors"
    TRIGGER_FREQUENCY = "trigger_frequency"
    INPUTS = "inputs"
    OUTPUTS = "outputs"
    CURRENT_REALITY = "current_reality"
    DECISIONS = "decisions"
    CONSTRAINTS = "constraints"
    FAILURE_IMPACT = "failure_impact"
    AUTONOMY = "autonomy"
    SUCCESS_EVIDENCE = "success_evidence"
    NON_GOALS = "non_goals"
REQUIRED_FIELDS = tuple(IntakeField)

_ALIASES = {
    "goal": IntakeField.OUTCOME,
    "objective": IntakeField.OUTCOME,
    "desired_outcome": IntakeField.OUTCOME,
    "users": IntakeField.ACTORS,
    "people": IntakeField.ACTORS,
    "trigger": IntakeField.TRIGGER_FREQUENCY,
    "frequency": IntakeField.TRIGGER_FREQUENCY,
    "trigger/frequency": IntakeField.TRIGGER_FREQUENCY,
    "current_state": IntakeField.CURRENT_REALITY,
    "as_is": IntakeField.CURRENT_REALITY,
    "failure": IntakeField.FAILURE_IMPACT,
    "risk": IntakeField.FAILURE_IMPACT,
    "permissions": IntakeField.AUTONOMY,
    "proof": IntakeField.SUCCESS_EVIDENCE,
    "success": IntakeField.SUCCESS_EVIDENCE,
    "out_of_scope": IntakeField.NON_GOALS,
    "non-goals": IntakeField.NON_GOALS,
}
class IntakeEvidence(CanonicalModel):
    field: IntakeField
    value: Any | None = None
    status: EvidenceStatus = EvidenceStatus.MISSING
    sources: list[str] = Field(default_factory=list)
    alternatives: list[Any] = Field(default_factory=list)

    @model_validator(mode="after")
    def status_matches_value(self) -> IntakeEvidence:
        if self.status is EvidenceStatus.GROUNDED and _empty(self.value):
            raise ValueError("grounded evidence must have a value")
        if self.status is EvidenceStatus.MISSING and not _empty(self.value):
            raise ValueError("missing evidence cannot have a value")
        if self.status is EvidenceStatus.CONTRADICTORY and len(self.alternatives) < 2:
            raise ValueError("contradictory evidence needs at least two alternatives")
        return self
class IntakeQuestion(CanonicalModel):
    field: IntakeField
    prompt: str
    architectural_impact: int = Field(ge=0, le=5)
    risk: int = Field(ge=0, le=5)
    information_gain: int = Field(ge=0, le=5)
    decision_implication: str | None = None

    @property
    def rank(self) -> tuple[int, int, int]:
        return (self.architectural_impact, self.risk, self.information_gain)
class RecordedDefault(CanonicalModel):
    field: IntakeField
    value: Any
    rationale: str
    impact: str = "low"
class Ambiguity(CanonicalModel):
    subject: str
    impact: str = "low"
    detail: str = ""
class IntakeState(CanonicalModel):
    mode: IntakeMode = IntakeMode.GUIDED
    evidence: dict[IntakeField, IntakeEvidence] = Field(default_factory=dict)
    defaults: list[RecordedDefault] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def fill_missing_fields(self) -> IntakeState:
        for field in REQUIRED_FIELDS:
            self.evidence.setdefault(field, IntakeEvidence(field=field))
        return self

    def update(self, payload: Mapping[str, Any] | str, *, source: str = "user") -> IntakeState:
        for candidate in extract_evidence(payload, source=source):
            current = self.evidence[candidate.field]
            if candidate.status is EvidenceStatus.MISSING:
                continue
            if current.status in (EvidenceStatus.MISSING, EvidenceStatus.ASSUMED):
                self.evidence[candidate.field] = candidate
            elif _same(current.value, candidate.value):
                current.sources = sorted(set(current.sources + candidate.sources))
                if candidate.status is EvidenceStatus.GROUNDED:
                    current.status = EvidenceStatus.GROUNDED
            else:
                alternatives = _unique(
                    [current.value, *current.alternatives, candidate.value, *candidate.alternatives]
                )
                self.evidence[candidate.field] = IntakeEvidence(
                    field=candidate.field,
                    value=None,
                    status=EvidenceStatus.CONTRADICTORY,
                    sources=sorted(set(current.sources + candidate.sources)),
                    alternatives=alternatives,
                )
        return self

    def next_question(self) -> IntakeQuestion | None:
        return next_question(self)
def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}
def _same(left: Any, right: Any) -> bool:
    return _normal(left) == _normal(right)
def _normal(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.casefold().split())
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _normal(item)) for key, item in value.items()))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_normal(item) for item in value)
    return value
def _unique(values: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if not _empty(value) and not any(_same(value, seen) for seen in result):
            result.append(value)
    return result
def _field(name: str) -> IntakeField | None:
    key = re.sub(r"[\s-]+", "_", name.strip().casefold())
    try:
        return IntakeField(key)
    except ValueError:
        return _ALIASES.get(key) or _ALIASES.get(name.strip().casefold())
def extract_evidence(
    payload: Mapping[str, Any] | str, *, source: str = "user"
) -> list[IntakeEvidence]:
    """Extract only explicit evidence from structured data or ``label: value`` lines."""
    if isinstance(payload, str):
        pairs: list[tuple[str, Any]] = []
        for line in payload.splitlines():
            match = re.match(r"^\s*[-*]?\s*([^:]+):\s*(.*?)\s*$", line)
            if match and match.group(2):
                pairs.append((match.group(1), match.group(2)))
    else:
        pairs = sorted(payload.items(), key=lambda pair: str(pair[0]))

    extracted: list[IntakeEvidence] = []
    for name, raw in pairs:
        field = _field(str(name))
        if field is None:
            continue
        if isinstance(raw, IntakeEvidence):
            candidate = raw.model_copy(deep=True)
            candidate.field = field
            if source not in candidate.sources:
                candidate.sources.append(source)
        elif isinstance(raw, Mapping) and ("value" in raw or "status" in raw):
            candidate = IntakeEvidence(
                field=field,
                value=raw.get("value"),
                status=raw.get("status", EvidenceStatus.GROUNDED),
                sources=_unique([*raw.get("sources", []), source]),
                alternatives=list(raw.get("alternatives", [])),
            )
        else:
            status = EvidenceStatus.MISSING if _empty(raw) else EvidenceStatus.GROUNDED
            candidate = IntakeEvidence(field=field, value=raw, status=status, sources=[source])
        extracted.append(candidate)
    return sorted(extracted, key=lambda item: item.field.value)
_QUESTION_DATA: dict[IntakeField, tuple[str, int, int, int, str]] = {
    IntakeField.OUTCOME: ("What outcome must this system produce?", 5, 4, 5,
                          "This sets the system boundary and what it optimizes for."),
    IntakeField.ACTORS: ("Who uses, reviews, or is affected by it?", 4, 4, 4,
                         "This decides access, review, and accountability."),
    IntakeField.TRIGGER_FREQUENCY: ("What starts it, and how often?", 4, 3, 4,
                                    "This determines its operating pattern."),
    IntakeField.INPUTS: ("What information enters the system?", 5, 4, 5,
                         "This defines required integrations and data contracts."),
    IntakeField.OUTPUTS: ("What must it produce, and for whom?", 5, 4, 5,
                          "This defines delivery contracts and dependencies."),
    IntakeField.CURRENT_REALITY: ("How is this handled today?", 3, 2, 4,
                                  "This reveals what can be reused or must change."),
    IntakeField.DECISIONS: ("Which decisions must the system make?", 5, 5, 5,
                            "This separates rules, reasoning, and human judgment."),
    IntakeField.CONSTRAINTS: ("Which limits or rules cannot be violated?", 5, 5, 5,
                              "This can require permission boundaries or approval."),
    IntakeField.FAILURE_IMPACT: ("What happens if it is wrong or unavailable?", 5, 5, 5,
                                 "This sets safeguards, fallbacks, and review depth."),
    IntakeField.AUTONOMY: ("What may it do without approval?", 5, 5, 5,
                           "This determines which actions must stop for a person."),
    IntakeField.SUCCESS_EVIDENCE: ("What observable evidence proves it works?", 5, 4, 5,
                                   "This becomes the acceptance and evaluation plan."),
    IntakeField.NON_GOALS: ("What is explicitly outside scope?", 3, 3, 3,
                            "This prevents accidental expansion of responsibility."),
}
def next_question(state: IntakeState) -> IntakeQuestion | None:
    """Return exactly one highest-value unresolved question, never grounded data."""
    candidates: list[tuple[tuple[int, int, int, int, str], IntakeQuestion]] = []
    for field in REQUIRED_FIELDS:
        evidence = state.evidence[field]
        if evidence.status is EvidenceStatus.GROUNDED:
            continue
        prompt, architecture, risk, gain, implication = _QUESTION_DATA[field]
        if evidence.status is EvidenceStatus.CONTRADICTORY:
            prompt = f"Which is correct for {field.value}: {evidence.alternatives!r}?"
            architecture = risk = gain = 5
        question = IntakeQuestion(
            field=field,
            prompt=prompt,
            architectural_impact=architecture,
            risk=risk,
            information_gain=gain,
            decision_implication=implication if state.mode is IntakeMode.GUIDED else None,
        )
        status_priority = 1 if evidence.status is EvidenceStatus.CONTRADICTORY else 0
        candidates.append(((status_priority, *question.rank, field.value), question))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None
_HIGH_IMPACT = re.compile(
    r"\b(permission|publish|public|send|email|message|spend|payment|purchase|delete|"
    r"irreversible|production|credential|legal|medical|financial)\b",
    re.IGNORECASE,
)
def apply_autopilot_defaults(
    state: IntakeState, ambiguities: Sequence[Ambiguity | Mapping[str, str]]
) -> IntakeState:
    """Record reversible low-impact defaults and block consequential ambiguity."""
    if state.mode is not IntakeMode.AUTOPILOT:
        return state
    for raw in ambiguities:
        ambiguity = raw if isinstance(raw, Ambiguity) else Ambiguity.model_validate(raw)
        field = _field(ambiguity.subject)
        high = ambiguity.impact.casefold() != "low" or bool(
            _HIGH_IMPACT.search(f"{ambiguity.subject} {ambiguity.detail}")
        )
        if high:
            blocker = f"Unresolved high-impact ambiguity: {ambiguity.subject}"
            if blocker not in state.blockers:
                state.blockers.append(blocker)
        elif field and state.evidence[field].status is EvidenceStatus.MISSING:
            default = [] if field is IntakeField.NON_GOALS else "use current reversible process"
            state.defaults.append(
                RecordedDefault(
                    field=field,
                    value=default,
                    rationale=f"Low-impact reversible default for: {ambiguity.subject}",
                )
            )
            state.evidence[field] = IntakeEvidence(
                field=field, value=default, status=EvidenceStatus.ASSUMED, sources=["autopilot"]
            )
    state.blockers.sort()
    return state
class TopologyKind(StrEnum):
    DETERMINISTIC = "deterministic"
    REASONING = "reasoning"
    HUMAN_REVIEW = "human_review"
    WORKFLOW = "workflow"
    SINGLE_AGENT = "single_agent"
    MULTI_AGENT = "multi_agent"
class WorkUnit(CanonicalModel):
    id: str
    responsibilities: list[str]
    kind: TopologyKind
    model_call_justification: str | None = None


class WorkHandoff(CanonicalModel):
    source: str
    target: str
    contract: str = ""


class TopologyProposal(CanonicalModel):
    kind: TopologyKind
    units: list[WorkUnit]
    handoffs: list[WorkHandoff] = Field(default_factory=list)


class TopologyValidation(CanonicalModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)


def classify_topology(
    units: Sequence[WorkUnit], handoffs: Sequence[WorkHandoff] = ()
) -> TopologyKind:
    """Choose the smallest topology that represents the declared work."""
    kinds = {unit.kind for unit in units}
    if TopologyKind.HUMAN_REVIEW in kinds:
        return TopologyKind.HUMAN_REVIEW if len(units) == 1 else TopologyKind.WORKFLOW
    reasoning = [
        unit
        for unit in units
        if unit.kind in {TopologyKind.REASONING, TopologyKind.SINGLE_AGENT}
    ]
    if len(reasoning) > 1:
        return TopologyKind.MULTI_AGENT
    if handoffs or len(units) > 1:
        return TopologyKind.WORKFLOW
    if reasoning:
        return TopologyKind.SINGLE_AGENT
    return TopologyKind.DETERMINISTIC


def validate_topology(proposal: TopologyProposal) -> TopologyValidation:
    """Reject unjustified model calls, redundant roles, overlaps, and unsafe handoffs."""
    errors: list[str] = []
    ids = [unit.id for unit in proposal.units]
    if len(ids) != len(set(ids)):
        errors.append("redundant unit ids")
    owners: dict[str, str] = {}
    for unit in proposal.units:
        if unit.kind in {
            TopologyKind.REASONING,
            TopologyKind.SINGLE_AGENT,
            TopologyKind.MULTI_AGENT,
        }:
            if not unit.model_call_justification or not unit.model_call_justification.strip():
                errors.append(f"model call for {unit.id!r} is not justified")
        elif unit.model_call_justification:
            errors.append(f"{unit.id!r} declares a model call for deterministic/human work")
        normalized = {_normal(item) for item in unit.responsibilities}
        if len(normalized) != len(unit.responsibilities):
            errors.append(f"{unit.id!r} has redundant responsibilities")
        for responsibility in normalized:
            previous = owners.get(responsibility)
            if previous and previous != unit.id:
                errors.append(
                    f"overlapping responsibility {responsibility!r}: {previous!r} and {unit.id!r}"
                )
            owners[responsibility] = unit.id
    known = set(ids)
    for handoff in proposal.handoffs:
        if handoff.source not in known or handoff.target not in known:
            errors.append(f"handoff {handoff.source!r}->{handoff.target!r} references unknown unit")
        if not handoff.contract.strip():
            errors.append(f"handoff {handoff.source!r}->{handoff.target!r} has no contract")
    actual = classify_topology(proposal.units, proposal.handoffs)
    if proposal.kind is not actual:
        errors.append(f"declared topology {proposal.kind.value!r} should be {actual.value!r}")
    return TopologyValidation(valid=not errors, errors=sorted(set(errors)))


class FrameStop(CanonicalModel):
    passed: bool
    checks: dict[str, bool]
    blockers: list[str]


def frame_stop(state: IntakeState, topology: TopologyProposal) -> FrameStop:
    """Produce an auditable stop decision; no truthy summary can bypass failed checks."""
    unresolved = [
        field.value
        for field, evidence in state.evidence.items()
        if evidence.status in {EvidenceStatus.MISSING, EvidenceStatus.CONTRADICTORY}
    ]
    topology_result = validate_topology(topology)
    success = state.evidence[IntakeField.SUCCESS_EVIDENCE]
    checks = {
        "all_intake_fields_resolved": not unresolved,
        "success_evidence_grounded": success.status is EvidenceStatus.GROUNDED,
        "topology_valid": topology_result.valid,
        "no_autonomy_blockers": not state.blockers,
    }
    blockers = [*state.blockers, *topology_result.errors]
    blockers.extend(f"unresolved intake field: {field}" for field in sorted(unresolved))
    if not checks["success_evidence_grounded"]:
        blockers.append("success evidence is not grounded")
    return FrameStop(passed=all(checks.values()), checks=checks, blockers=sorted(set(blockers)))


AdaptiveIntake = IntakeState

__all__ = [name for name in globals() if not name.startswith("_")]
