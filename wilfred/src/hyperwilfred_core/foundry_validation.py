# ruff: noqa: E501
"""Deterministic validation for Agent Foundry specifications.

This module deliberately contains no model calls.  It turns structural and policy
violations into a stable report that lifecycle code can persist or gate on.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .foundry_models import (
    AGENT_SYSTEM_SPECIFICATION_SCHEMA,
    AgentSystemSpecification,
    DataContract,
    DecisionOutcome,
    PermissionEffect,
    RecordStatus,
)


class ValidationIssue(BaseModel):
    """One actionable, machine-readable specification defect."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    code: str
    path: str
    message: str
    remediation: str
    severity: Literal['error', 'warning'] = 'error'

class ValidationReport(BaseModel):
    """Stable validation result consumed by the Foundry lifecycle."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal['foundry-validation-report/v1'] = 'foundry-validation-report/v1'
    valid: bool
    issues: tuple[ValidationIssue, ...] = Field(default_factory=tuple)

    def to_stable_json(self, *, indent: int | None=None) -> str:
        """Serialize deterministically for lifecycle persistence and comparison."""
        return json.dumps(self.model_dump(mode='json'), ensure_ascii=False, indent=indent, separators=(',', ':') if indent is None else None, sort_keys=True)

    stable_json = to_stable_json

    @classmethod
    def from_issues(cls, issues: Iterable[ValidationIssue]) -> ValidationReport:
        ordered = tuple(sorted(issues, key=lambda item: (item.path, item.code, item.message)))
        return cls(valid=not any(item.severity == 'error' for item in ordered), issues=ordered)

def _issue(code: str, path: str, message: str, remediation: str) -> ValidationIssue:
    return ValidationIssue(code=code, path=path, message=message, remediation=remediation)

def _duplicates(values: Iterable[str]) -> set[str]:
    return {value for value, count in Counter(values).items() if value and count > 1}

def _schema_present(contract: DataContract, path: str, issues: list[ValidationIssue]) -> None:
    allowed_types = {'null', 'boolean', 'object', 'array', 'number', 'string', 'integer'}
    if contract.required and (not contract.json_schema):
        issues.append(_issue('missing_contract_schema', f'{path}.schema', 'Required contract has no schema.', 'Define the input/output fields and their types.'))
    elif contract.json_schema and 'type' in contract.json_schema:
        declared = contract.json_schema['type']
        valid = declared in allowed_types if isinstance(declared, str) else (
            isinstance(declared, list) and bool(declared)
            and all(isinstance(item, str) and item in allowed_types for item in declared)
        )
        if not valid:
            issues.append(_issue('invalid_contract_schema', f'{path}.schema.type', 'Schema type is not a supported JSON Schema type.', "Use null, boolean, object, array, number, string, or integer."))

def _graph_cycle(nodes: set[str], edges: list[tuple[str, str]]) -> bool:
    adjacency: dict[str, list[str]] = defaultdict(list)
    indegree = dict.fromkeys(nodes, 0)
    for source, target in edges:
        if source in nodes and target in nodes:
            adjacency[source].append(target)
            indegree[target] += 1
    queue = deque(sorted((node for node, degree in indegree.items() if degree == 0)))
    visited = 0
    while queue:
        source = queue.popleft()
        visited += 1
        for target in sorted(adjacency[source]):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    return visited != len(nodes)

def _validate_blueprint(blueprint: Mapping[str, Any], component_ids: set[str]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    version = blueprint.get('schema_version')
    if version != 'agent-blueprint/v1':
        issues.append(_issue('incompatible_schema_version', 'blueprint.schema_version', f'Blueprint schema {version!r} is unsupported.', "Use 'agent-blueprint/v1' or migrate the blueprint."))
    raw_steps = blueprint.get('steps', [])
    steps = [step for step in raw_steps if isinstance(step, Mapping)] if isinstance(raw_steps, Sequence) else []
    step_ids = {str(step.get('id', '')) for step in steps if step.get('id')}
    edges: list[tuple[str, str]] = []
    referenced: set[str] = set()
    for index, step in enumerate(steps):
        path = f'blueprint.steps[{index}]'
        step_id = str(step.get('id', ''))
        if not step_id:
            issues.append(_issue('missing_step_id', f'{path}.id', 'Step has no identifier.', 'Add a unique step id.'))
        owner = step.get('component_id', step.get('owner'))
        if not owner:
            issues.append(_issue('missing_step_owner', f'{path}.component_id', 'Step has no owning component.', 'Assign exactly one declared component.'))
        elif str(owner) not in component_ids:
            issues.append(_issue('nonexistent_component_reference', f'{path}.component_id', f'Step references unknown component {owner!r}.', 'Use a declared component id.'))
        else:
            referenced.add(str(owner))
        previous = step.get('after', step.get('depends_on', []))
        if isinstance(previous, str):
            previous = [previous]
        if isinstance(previous, Sequence):
            for dependency in previous:
                edges.append((str(dependency), step_id))
                if str(dependency) not in step_ids:
                    issues.append(_issue('nonexistent_step_reference', f'{path}.depends_on', f'Step references unknown predecessor {dependency!r}.', 'Reference an existing step id.'))
        model_call = step.get('model_call') or step.get('uses_model')
        if model_call and (not str(step.get('model_call_justification', step.get('justification', ''))).strip()):
            issues.append(_issue('missing_model_call_justification', path, 'Model call is not justified.', 'Explain why deterministic code cannot perform this step.'))
    for duplicate in sorted(_duplicates(str(step.get('id', '')) for step in steps)):
        issues.append(_issue('duplicate_step_id', 'blueprint.steps', f'Step id {duplicate!r} is duplicated.', 'Give every step a unique id.'))
    allowed_cycles = blueprint.get('allowed_cycles', blueprint.get('declared_cycles', []))
    if _graph_cycle(step_ids, edges) and (not allowed_cycles):
        issues.append(_issue('undeclared_sequence_cycle', 'blueprint.steps', 'Step sequence contains a cycle.', 'Remove the cycle or declare and bound the intentional loop.'))
    sequenced = {node for edge in edges for node in edge}
    if len(step_ids) > 1:
        for step_id in sorted(step_ids - sequenced):
            issues.append(_issue('orphan_step', 'blueprint.steps', f'Step {step_id!r} is outside the sequence.', 'Connect it through depends_on/after or remove it.'))
    for component_id in sorted(component_ids - referenced):
        issues.append(_issue('orphan_component', 'blueprint.steps', f'Component {component_id!r} owns no step.', 'Assign it a step or remove it from this blueprint.'))
    return issues

def _coerce_contracts(contracts: Sequence[DataContract | Mapping[str, Any]] | None) -> tuple[list[DataContract], list[ValidationIssue]]:
    parsed: list[DataContract] = []
    issues: list[ValidationIssue] = []
    for index, contract in enumerate(contracts or ()):
        try:
            parsed.append(contract if isinstance(contract, DataContract) else DataContract.model_validate(contract))
        except ValidationError as exc:
            for error in exc.errors():
                suffix = '.'.join(str(part) for part in error['loc'])
                path = f'contracts[{index}]' + (f'.{suffix}' if suffix else '')
                issues.append(_issue('invalid_contract', path, error['msg'], 'Provide a valid data contract.'))
    return (parsed, issues)

def validate_specification(specification: AgentSystemSpecification | Mapping[str, Any], blueprint: Mapping[str, Any] | None=None, contracts: Sequence[DataContract | Mapping[str, Any]] | None=None) -> ValidationReport:
    """Validate a canonical spec and optional separately stored design artifacts."""
    if isinstance(specification, Mapping):
        version = specification.get('schema_version', AGENT_SYSTEM_SPECIFICATION_SCHEMA)
        if version != AGENT_SYSTEM_SPECIFICATION_SCHEMA:
            return ValidationReport.from_issues([_issue('incompatible_schema_version', 'schema_version', f'Schema {version!r} is unsupported; expected {AGENT_SYSTEM_SPECIFICATION_SCHEMA!r}.', 'Migrate the specification before validation.')])
        try:
            spec = AgentSystemSpecification.model_validate(specification)
        except ValidationError as exc:
            return ValidationReport.from_issues(_issue('invalid_specification', '.'.join(str(part) for part in error['loc']) or '$', error['msg'], 'Correct the field to match the canonical schema.') for error in exc.errors())
    elif isinstance(specification, AgentSystemSpecification):
        spec = specification
    else:
        return ValidationReport.from_issues([_issue('invalid_specification', '$', 'Specification must be a mapping.', 'Pass an AgentSystemSpecification or dictionary.')])
    issues: list[ValidationIssue] = []
    component_ids = {component.id for component in spec.components}
    node_ids = {node.id for node in spec.topology.nodes}
    state_ids = {state.id for state in spec.states}
    evidence_ids = {evidence.id for evidence in spec.evidence}
    evidence_references: list[tuple[str, list[str]]] = [('problem.evidence_ids', spec.problem.evidence_ids), *((f'assumptions[{index}].evidence_ids', item.evidence_ids) for index, item in enumerate(spec.assumptions)), *((f'contradictions[{index}].evidence_ids', item.evidence_ids) for index, item in enumerate(spec.contradictions)), *((f'decisions[{index}].evidence_ids', item.evidence_ids) for index, item in enumerate(spec.decisions))]
    for path, references in evidence_references:
        for evidence_id in references:
            if evidence_id not in evidence_ids:
                issues.append(_issue('nonexistent_evidence_reference', path, f'Evidence {evidence_id!r} does not exist.', 'Reference recorded evidence or remove the id.'))
    for duplicate in sorted(_duplicates(component.id for component in spec.components)):
        issues.append(_issue('duplicate_component_id', 'components', f'Component id {duplicate!r} is duplicated.', 'Give every component a unique id.'))
    for duplicate in sorted(_duplicates(node.id for node in spec.topology.nodes)):
        issues.append(_issue('duplicate_topology_node', 'topology.nodes', f'Node id {duplicate!r} is duplicated.', 'Give every topology node a unique id.'))
    for index, component in enumerate(spec.components):
        path = f'components[{index}]'
        if not component.responsibilities:
            issues.append(_issue('missing_responsibility', f'{path}.responsibilities', 'Component has no responsibility.', 'State at least one specific responsibility.'))
        normalized = [value.strip().casefold() for value in component.responsibilities]
        for other_id, other in ((item.id, item) for item in spec.components[index + 1:]):
            overlap = set(normalized) & {value.strip().casefold() for value in other.responsibilities}
            if overlap:
                issues.append(_issue('overlapping_ownership', f'{path}.responsibilities', f'Responsibilities overlap with component {other_id!r}: {sorted(overlap)!r}.', 'Assign each responsibility to one component or document a handoff.'))
        for contract_index, contract in enumerate(component.inputs):
            _schema_present(contract, f'{path}.inputs[{contract_index}]', issues)
        for contract_index, contract in enumerate(component.outputs):
            _schema_present(contract, f'{path}.outputs[{contract_index}]', issues)
        for tool_index, tool in enumerate(component.tools):
            tool_path = f'{path}.tools[{tool_index}]'
            if not tool.purpose.strip():
                issues.append(_issue('missing_tool_justification', f'{tool_path}.purpose', 'Tool has no purpose.', 'Explain why the component needs this tool.'))
            if tool.input:
                _schema_present(tool.input, f'{tool_path}.input', issues)
            if tool.output:
                _schema_present(tool.output, f'{tool_path}.output', issues)
        for permission_index, permission in enumerate(component.permissions):
            permission_path = f'{path}.permissions[{permission_index}]'
            wild = not permission.resource.strip() or not permission.actions or permission.resource.strip() in {'*', '/'} or '*' in permission.actions
            if wild or (permission.effect == PermissionEffect.ALLOW and (not permission.reason.strip())):
                issues.append(_issue('excessive_permission', permission_path, 'Permission is broad or unjustified.', 'Grant only named resources/actions and explain the minimum need.'))
    for index, edge in enumerate(spec.topology.edges):
        for field, value in (('source', edge.source), ('target', edge.target)):
            if value not in node_ids:
                issues.append(_issue('nonexistent_topology_reference', f'topology.edges[{index}].{field}', f'Topology node {value!r} does not exist.', 'Reference a declared topology node.'))
    connected = {edge.source for edge in spec.topology.edges} | {edge.target for edge in spec.topology.edges}
    for node_id in sorted(node_ids - connected) if len(node_ids) > 1 else ():
        issues.append(_issue('orphan_topology_node', 'topology.nodes', f'Node {node_id!r} has no edge.', 'Connect it to the system flow or remove it.'))
    for component_id in sorted(component_ids - node_ids):
        issues.append(_issue('component_missing_from_topology', 'topology.nodes', f'Component {component_id!r} is absent.', 'Add a topology node for the component.'))
    transition_edges: list[tuple[str, str]] = []
    for index, transition in enumerate(spec.transitions):
        transition_edges.append((transition.source, transition.target))
        for field, value in (('source', transition.source), ('target', transition.target)):
            if value not in state_ids:
                issues.append(_issue('nonexistent_state_reference', f'transitions[{index}].{field}', f'State {value!r} does not exist.', 'Reference a declared state.'))
    if state_ids and (not any(state.terminal for state in spec.states)):
        issues.append(_issue('missing_terminal_state', 'states', 'State machine has no terminal state.', 'Mark at least one completion or failure state as terminal.'))
    sequenced_states = {state for edge in transition_edges for state in edge}
    for state_id in sorted(state_ids - sequenced_states) if len(state_ids) > 1 else ():
        issues.append(_issue('orphan_state', 'states', f'State {state_id!r} is outside the sequence.', 'Connect it with a transition or remove it.'))
    if _graph_cycle(state_ids, transition_edges) and (not any('cycle' in decision.question.casefold() for decision in spec.decisions)):
        issues.append(_issue('undeclared_state_cycle', 'transitions', 'State machine contains an undeclared cycle.', 'Remove it or add a decision explaining and bounding the loop.'))
    for index, handoff in enumerate(spec.handoffs):
        path = f'handoffs[{index}]'
        for field, value in (('source_component', handoff.source_component), ('target_component', handoff.target_component)):
            if value not in component_ids:
                issues.append(_issue('nonexistent_handoff_component', f'{path}.{field}', f'Handoff component {value!r} does not exist.', 'Reference a declared component.'))
        _schema_present(handoff.contract, f'{path}.contract', issues)
        source = next((component for component in spec.components if component.id == handoff.source_component), None)
        target = next((component for component in spec.components if component.id == handoff.target_component), None)
        if source and handoff.contract.name not in {contract.name for contract in source.outputs}:
            issues.append(_issue('missing_handoff_output', f'{path}.contract', f'Source does not declare output {handoff.contract.name!r}.', 'Add the contract to the source outputs.'))
        if target and handoff.contract.name not in {contract.name for contract in target.inputs}:
            issues.append(_issue('missing_handoff_input', f'{path}.contract', f'Target does not declare input {handoff.contract.name!r}.', 'Add the contract to the target inputs.'))
        if not handoff.acceptance_conditions:
            issues.append(_issue('missing_handoff_acceptance', f'{path}.acceptance_conditions', 'Handoff has no acceptance conditions.', 'Define how the receiver verifies the payload.'))
    policy = spec.failure_policy
    if policy.retry_limit < 1 or policy.retry_strategy.strip().casefold() == 'none':
        issues.append(_issue('missing_retry_strategy', 'failure_policy.retry_strategy', 'No bounded retry is defined.', 'Set a positive retry limit and a bounded retry strategy.'))
    if policy.timeout_seconds is None:
        issues.append(_issue('missing_timeout', 'failure_policy.timeout_seconds', 'No timeout is defined.', 'Set a finite timeout for stalled work.'))
    for field in ('fallback', 'escalation'):
        if not getattr(policy, field):
            issues.append(_issue(f'missing_{field}', f'failure_policy.{field}', f'No {field} is defined.', f'Define the {field} behavior after failure.'))
    for index, decision in enumerate(spec.external_reuse):
        if decision.outcome in {DecisionOutcome.REUSE, DecisionOutcome.ADAPT} and (not decision.evidence_ids):
            issues.append(_issue('invalid_reuse_evidence', f'external_reuse[{index}].evidence_ids', 'Reuse/adapt decision has no evidence.', 'Cite due-diligence evidence for the candidate.'))
        for evidence_id in decision.evidence_ids:
            if evidence_id not in evidence_ids:
                issues.append(_issue('nonexistent_evidence_reference', f'external_reuse[{index}].evidence_ids', f'Evidence {evidence_id!r} does not exist.', 'Reference recorded evidence.'))
            elif next(item for item in spec.evidence if item.id == evidence_id).status != RecordStatus.VALIDATED:
                issues.append(_issue('invalid_reuse_evidence', f'external_reuse[{index}].evidence_ids', f'Evidence {evidence_id!r} is not validated.', 'Validate the due-diligence evidence before reuse.'))
    for index, gap in enumerate(spec.capability_gaps):
        if gap.status not in {RecordStatus.REJECTED, RecordStatus.SUPERSEDED} and (not gap.mitigation):
            issues.append(_issue('unmet_capability_gap', f'capability_gaps[{index}].mitigation', f'Capability gap {gap.capability!r} is unresolved.', 'Add a mitigation or defer the design.'))
    separate_contracts, contract_issues = _coerce_contracts(contracts)
    issues.extend(contract_issues)
    all_contracts = [*spec.contracts, *separate_contracts]
    for duplicate in sorted(_duplicates(contract.name for contract in all_contracts)):
        issues.append(_issue('duplicate_contract', 'contracts', f'Contract {duplicate!r} is duplicated.', 'Keep one authoritative contract definition.'))
    for index, contract in enumerate(all_contracts):
        _schema_present(contract, f'contracts[{index}]', issues)
    if blueprint is not None:
        issues.extend(_validate_blueprint(blueprint, component_ids))
    return ValidationReport.from_issues(issues)
validate_agent_system_specification = validate_specification

class SpecificationValidator:
    """Small injectable facade for lifecycle services."""

    def validate(self, specification: AgentSystemSpecification | Mapping[str, Any], **artifacts: Any) -> ValidationReport:
        return validate_specification(specification, **artifacts)
__all__ = ['SpecificationValidator', 'ValidationIssue', 'ValidationReport', 'validate_agent_system_specification', 'validate_specification']
