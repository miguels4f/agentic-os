"""Contract tests for the provider-neutral Agent Foundry core."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_foundry_contract_hardening import runtime_design

from hyperwilfred_core.foundry_intake import (
    Ambiguity,
    EvidenceStatus,
    IntakeEvidence,
    IntakeField,
    IntakeMode,
    IntakeState,
    TopologyKind,
    TopologyProposal,
    WorkHandoff,
    WorkUnit,
    apply_autopilot_defaults,
    frame_stop,
    validate_topology,
)
from hyperwilfred_core.foundry_models import AgentSystemSpecification
from hyperwilfred_core.foundry_runtime import (
    BenchmarkObservation,
    BenchmarkThresholds,
    BoundedDecisionRequest,
    BundleGenerationBlocked,
    CapabilitySupport,
    HumanCapabilityDecision,
    JevConfig,
    LocalPythonManifestAdapter,
    benchmark_assist,
    capability_mapping,
    decide_bounded,
)
from hyperwilfred_core.foundry_scout import (
    CandidateChecks,
    CandidateDossier,
    InMemorySearchProvider,
    ReuseClass,
    ScoutRequest,
    decide_reuse,
    expand_queries,
    scout,
)
from hyperwilfred_core.foundry_validation import validate_specification

FIXTURES = Path(__file__).parent / "fixtures" / "foundry"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def valid_specification() -> dict:
    return load_fixture("valid.json")["specification"]


def runtime_specification(family="notes_to_actions") -> dict:
    return runtime_design(family)[0]


def test_canonical_v1_serialization_is_stable_and_rejects_other_versions() -> None:
    payload = valid_specification()
    first = AgentSystemSpecification.model_validate(payload)
    reordered = AgentSystemSpecification.model_validate(dict(reversed(payload.items())))

    assert first.to_stable_json() == reordered.to_stable_json()
    assert first.to_stable_json().startswith('{"assumptions":')
    with pytest.raises(ValidationError, match="unsupported schema_version"):
        AgentSystemSpecification.model_validate({**payload, "schema_version": "v2"})


@pytest.mark.parametrize(
    ("status", "value", "alternatives"),
    [
        (EvidenceStatus.GROUNDED, "known", []),
        (EvidenceStatus.ASSUMED, "tentative", []),
        (EvidenceStatus.CONTRADICTORY, None, ["a", "b"]),
        (EvidenceStatus.MISSING, None, []),
    ],
)
def test_all_intake_evidence_statuses(
    status: EvidenceStatus, value: str | None, alternatives: list[str]
) -> None:
    evidence = IntakeEvidence(
        field=IntakeField.OUTCOME,
        status=status,
        value=value,
        alternatives=alternatives,
    )
    assert evidence.status is status


def test_guided_intake_asks_one_ranked_question_without_reasking() -> None:
    state = IntakeState(mode=IntakeMode.GUIDED)
    first = state.next_question()
    assert first is not None and first.decision_implication

    state.update({first.field.value: "grounded answer"})
    second = state.next_question()
    assert second is not None and second.field is not first.field
    assert state.evidence[first.field].status is EvidenceStatus.GROUNDED


def test_autopilot_defaults_low_impact_and_blocks_material_ambiguity() -> None:
    state = IntakeState(mode=IntakeMode.AUTOPILOT)
    apply_autopilot_defaults(
        state,
        [
            Ambiguity(subject="non_goals", detail="May remain empty"),
            Ambiguity(subject="autonomy", detail="May publish without approval"),
        ],
    )

    assert state.evidence[IntakeField.NON_GOALS].status is EvidenceStatus.ASSUMED
    assert state.defaults[0].value == []
    assert state.evidence[IntakeField.AUTONOMY].status is EvidenceStatus.MISSING
    assert state.blockers == ["Unresolved high-impact ambiguity: autonomy"]


def test_frame_stop_requires_resolved_intake_and_valid_topology() -> None:
    unit = WorkUnit(
        id="worker", responsibilities=["transform"], kind=TopologyKind.DETERMINISTIC
    )
    topology = TopologyProposal(kind=TopologyKind.DETERMINISTIC, units=[unit])
    state = IntakeState().update({field.value: "known" for field in IntakeField})
    assert frame_stop(state, topology).passed

    blocked = IntakeState()
    result = frame_stop(blocked, topology)
    assert not result.passed
    assert not result.checks["all_intake_fields_resolved"]


def test_topology_rejects_redundancy_overlap_and_uncontracted_handoff() -> None:
    proposal = TopologyProposal(
        kind=TopologyKind.MULTI_AGENT,
        units=[
            WorkUnit(
                id="same",
                responsibilities=["Review", " review "],
                kind=TopologyKind.REASONING,
                model_call_justification="needs judgment",
            ),
            WorkUnit(
                id="same",
                responsibilities=["review"],
                kind=TopologyKind.REASONING,
                model_call_justification="needs judgment",
            ),
            WorkUnit(
                id="other",
                responsibilities=["review"],
                kind=TopologyKind.REASONING,
                model_call_justification="needs judgment",
            ),
        ],
        handoffs=[WorkHandoff(source="same", target="missing")],
    )
    errors = validate_topology(proposal).errors
    assert any("redundant unit ids" in error for error in errors)
    assert any("overlapping responsibility" in error for error in errors)
    assert any("has no contract" in error for error in errors)


def complete_candidate(**changes: object) -> CandidateDossier:
    data = {
        "candidate_id": "candidate-1",
        "name": "Local parser",
        "url": "https://example.invalid/parser",
        "provenance": ["catalog:fixture", "review:fixture"],
        "version_or_commit": "abc123",
        "license": "MIT",
        "summary": "Offline Python parser",
        "tags": ["python", "parser"],
        "preferred_class": ReuseClass.EXTERNAL_DEPENDENCY,
        "provider": "memory",
        "checks": CandidateChecks(
            provenance=True,
            maintained=True,
            security=True,
            tests=True,
            compatible=True,
            primary_source_correct=True,
        ),
    }
    data.update(changes)
    return CandidateDossier.model_validate(data)


def test_query_expansion_and_in_memory_scout_are_deterministic() -> None:
    request = ScoutRequest(
        problem="parse records",
        capabilities=["Parser", "parser"],
        languages=["Python"],
        tags=["offline"],
    )
    queries = expand_queries(request)
    report = scout(request, [InMemorySearchProvider([complete_candidate()])])

    assert [(query.dimension, query.value) for query in queries] == [
        ("problem", "parse records"),
        ("capability", "Parser"),
        ("language", "Python"),
        ("tag", "offline"),
    ]
    assert [item.candidate_id for item in report.dossiers] == ["candidate-1"]
    assert report.decisions[0].classification is ReuseClass.EXTERNAL_DEPENDENCY


def test_reuse_blocks_missing_license_and_preserves_complete_provenance() -> None:
    provenance = ["catalog:fixture", "review:fixture"]
    rejected = decide_reuse(complete_candidate(license=None))
    accepted = decide_reuse(complete_candidate(provenance=provenance))

    assert rejected.classification is ReuseClass.REJECTED
    assert "missing license" in rejected.rationale
    assert accepted.provenance == provenance
    assert accepted.classification is ReuseClass.EXTERNAL_DEPENDENCY


def test_spec_blueprint_and_contract_validators_cover_design_defects() -> None:
    fixture = load_fixture("invalid.json")
    report = validate_specification(**fixture)
    codes = {issue.code for issue in report.issues}

    assert not report.valid
    assert {
        "excessive_permission",
        "missing_contract_schema",
        "nonexistent_component_reference",
        "nonexistent_evidence_reference",
        "nonexistent_step_reference",
        "nonexistent_topology_reference",
        "orphan_component",
        "orphan_step",
        "overlapping_ownership",
        "undeclared_sequence_cycle",
    } <= codes


def test_valid_fixture_builds_bundle_with_every_required_file() -> None:
    fixture = load_fixture("valid.json")
    report = validate_specification(**fixture)
    assert report.valid, report.to_stable_json(indent=2)

    bundle = LocalPythonManifestAdapter().build_bundle(
        runtime_specification(), blueprint=runtime_design()[1]
    )
    unsupported = {**fixture["specification"], "transitions": [],
                   "states": [{"id": "complete", "terminal": True}]}
    with pytest.raises(BundleGenerationBlocked, match="incompatible.*executable contract"):
        LocalPythonManifestAdapter().build_bundle(unsupported, blueprint=fixture["blueprint"])
    assert set(bundle.files()) == {
        "INSTRUCTIONS.md",
        "RUNBOOK.md",
        "capabilities.json",
        "config.json",
        "contracts.json",
        "evaluations.json",
        "manifest.json",
        "runner.py",
    }
    assert bundle.manifest["files"] == sorted(set(bundle.files()) - {"manifest.json"})


def test_unsupported_runtime_capability_blocks_until_human_decision() -> None:
    adapter = LocalPythonManifestAdapter()
    with pytest.raises(BundleGenerationBlocked) as blocked:
        adapter.build_bundle(runtime_specification(), required_capabilities=["tool_execution"])
    assert blocked.value.capabilities == ("tool_execution",)

    with pytest.raises(BundleGenerationBlocked, match="still required"):
        adapter.build_bundle(
            runtime_specification(),
            required_capabilities=["tool_execution"],
            human_decisions=[
                HumanCapabilityDecision(
                    capability="tool_execution", decision="exclude", rationale="Stay offline"
                )
            ],
        )

    bundle = adapter.build_bundle(
        runtime_specification(),
        required_capabilities=["tool_execution"],
        human_decisions=[
            HumanCapabilityDecision(
                capability="tool_execution", decision="defer", rationale="Decide before execution"
            )
        ],
    )
    assert bundle.config["deferred_capabilities"] == ["tool_execution"]


def test_runtime_capability_is_derived_from_spec_without_caller_hint() -> None:
    specification = runtime_specification()
    specification["memory"] = {
        "retention": "project lifetime",
        "write_rules": ["retain accepted results"],
    }

    with pytest.raises(BundleGenerationBlocked) as blocked:
        LocalPythonManifestAdapter().build_bundle(specification)

    assert blocked.value.capabilities == ("persistent_memory",)


@pytest.mark.parametrize(
    "config", [JevConfig(enabled=False, available=True), JevConfig(enabled=True, available=False)]
)
def test_assist_disabled_or_unavailable_records_baseline_fallback(config: JevConfig) -> None:
    result = decide_bounded(
        BoundedDecisionRequest(
            decision_id="format", question="Choose format", options=["json", "yaml"],
            baseline_option="json",
        ),
        config,
    )
    assert result.lane == "baseline"
    assert result.selected_option == "json"
    assert "disabled or unavailable" in result.rationale
    assert not result.authorizes_action


def test_benchmark_activation_requires_every_threshold() -> None:
    thresholds = BenchmarkThresholds(
        minimum_quality_gain=0.05,
        minimum_accuracy_gain=0.05,
        maximum_cost_ratio=1.2,
        maximum_latency_ratio=1.2,
        maximum_low_confidence_rate=0.1,
        maximum_failure_rate=0.1,
    )
    baseline = BenchmarkObservation(
        lane="baseline", quality=0.8, accuracy=0.8, approximate_cost=10, latency_ms=100
    )
    passing = BenchmarkObservation(
        lane="assist", quality=0.9, accuracy=0.9, approximate_cost=11, latency_ms=110
    )
    failing = passing.model_copy(update={"latency_ms": 121})

    assert benchmark_assist([baseline, passing], thresholds).recommend_activation
    failed = benchmark_assist([baseline, failing], thresholds)
    assert not failed.recommend_activation
    assert "latency: fail" in failed.reasons


@pytest.mark.parametrize("family", ["notes_to_actions", "evidence_answer", "reviewed_brief"])
@pytest.mark.parametrize("requirement", ["transition", "human_gate", "shell_tool", "lookup_tool"])
@pytest.mark.parametrize("defer", [False, True])
def test_unimplemented_spec_runtime_behavior_blocks_even_when_deferred(family, requirement, defer):
    specification = runtime_specification(family)
    capability = "tool_execution"
    if requirement == "transition":
        specification = valid_specification()
        capability = "state_machine"
    elif requirement == "human_gate":
        specification["human_gates"] = [{"id": "approve-send", "trigger": "send requested",
                                           "approvers": ["operator"]}]
        capability = "human_gates"
    else:
        specification["components"][0]["tools"] = [
            {"name": "lookup" if requirement == "lookup_tool" else "shell",
             "purpose": "Execute this explicit caller-supplied tool contract"}]
    # Structurally valid does not mean executable by this adapter.
    assert validate_specification(specification).valid
    decisions = [{"capability": capability, "decision": "defer", "rationale": "test fixture"}]
    with pytest.raises(BundleGenerationBlocked, match="unimplemented runtime behavior") as blocked:
        LocalPythonManifestAdapter().build_bundle(
            specification, task_family=family, human_decisions=decisions if defer else [])
    assert blocked.value.capabilities == (capability,)


def test_capability_map_distinguishes_host_lifecycle_from_generated_execution():
    requested = capability_mapping(["state_machine", "human_gates", "tool_execution"])
    assert all(item.support is CapabilitySupport.UNSUPPORTED for item in requested
               if item.capability in {"state_machine", "human_gates", "tool_execution"})
    for family in ("notes_to_actions", "evidence_answer", "reviewed_brief"):
        generated = LocalPythonManifestAdapter().build_bundle(
            runtime_specification(family), task_family=family)
        mappings = {item.capability: item for item in generated.capability_mapping}
        assert not ({"state_machine", "human_gates", "tool_execution"} & mappings.keys())
        if family != "notes_to_actions":
            assert mappings["bounded_document_lookup"].support is CapabilitySupport.DIRECT
            assert mappings["model_execution"].support is CapabilitySupport.DIRECT
        with pytest.raises(BundleGenerationBlocked, match="unsupported capabilities"):
            LocalPythonManifestAdapter().build_bundle(
                runtime_specification(family), task_family=family,
                required_capabilities=["state_machine"])
