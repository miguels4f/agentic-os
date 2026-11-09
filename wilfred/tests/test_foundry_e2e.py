"""Network-free end-to-end contract for the generic Agent Foundry lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_foundry_contract_hardening import runtime_design

from hyperwilfred_core import foundry
from hyperwilfred_core.cli import build_parser, main
from hyperwilfred_core.foundry_intake import (
    TopologyKind,
    TopologyProposal,
    WorkUnit,
    validate_topology,
)
from hyperwilfred_core.foundry_runtime import (
    BundleGenerationBlocked,
    LocalPythonManifestAdapter,
)
from hyperwilfred_core.foundry_scout import (
    CandidateChecks,
    CandidateDossier,
    InMemorySearchProvider,
    ReuseClass,
    decide_reuse,
)

OBJECTIVE = "Turn workshop notes into a concise action list"
ANSWERS = {
    "actors": "A workshop facilitator reviews the result",
    "trigger_frequency": "After each workshop",
    "inputs": "Plain-text workshop notes",
    "outputs": "A JSON action list for the facilitator",
    "current_reality": "The facilitator extracts actions manually",
    "decisions": "Extract explicit action owners and dates using fixed rules",
    "constraints": "Offline, read-only input, and deterministic JSON output",
    "failure_impact": "The facilitator corrects an incomplete draft before use",
    "autonomy": "Draft locally; never publish or message anyone",
    "success_evidence": "Every explicit action has text, owner, and due date",
    "non_goals": "No publishing, reminders, or calendar updates",
}
SCENARIOS = {
    name: {
        "passed": True,
        "score": 1.0,
        "evidence": f"fixture:{name}",
        "kind": "subjective" if name == "adversarial" else "deterministic",
    }
    for name in ("normal", "edge", "adversarial", "recovery")
}


def _candidate(*, license_name: str | None = "MIT") -> CandidateDossier:
    return CandidateDossier(
        candidate_id="notes-parser-v1",
        name="Workshop notes parser",
        url="https://example.invalid/notes-parser",
        provenance=["catalog:fixture"],
        version_or_commit="fixture-commit-1",
        license=license_name,
        summary=OBJECTIVE,
        tags=["offline", "notes"],
        preferred_class=ReuseClass.EXTERNAL_DEPENDENCY,
        provider="fixture",
        checks=CandidateChecks(
            provenance=True,
            maintained=True,
            security=True,
            tests=True,
            compatible=True,
            primary_source_correct=True,
        ),
    )


def _answer_every_question(project: Path) -> list[str]:
    asked: list[str] = []
    while True:
        question = foundry.status(project).get("question")
        if question is None:
            return asked
        field = question["field"]
        asked.append(field)
        foundry.answer(project, ANSWERS[field], field=field)


def _stored(project: Path) -> dict:
    return json.loads((project / "state.json").read_text(encoding="utf-8"))


def _artifact(project: Path, name: str) -> object:
    state = _stored(project)
    return json.loads((project / state["artifacts"][name]["path"]).read_text(encoding="utf-8"))


def _build_directory(project: Path) -> Path:
    return project / _artifact(project, "build")["directory"]


def _artifact_digest(value: object) -> str:
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    return hashlib.sha256(encoded).hexdigest()


def _advance_to_evaluate(
    project: Path, providers: tuple[InMemorySearchProvider, ...] = (), *,
    seeded_defect: bool = False,
) -> None:
    foundry.create(project, OBJECTIVE)
    _answer_every_question(project)
    assert foundry.next_action(project)["stage"] == "classify"
    assert foundry.next_action(project)["stage"] == "scout"
    assert foundry.next_action(project, providers=providers)["stage"] == "blueprint"
    assert foundry.next_action(project)["stage"] == "contract"
    assert foundry.validate(project)["valid"]
    foundry.generate(
        project,
        seeded_defect=seeded_defect,
    )
    assert foundry.status(project)["stage"] == "evaluate"


def _run_lifecycle(project: Path, mode: str, *, already_created: bool = False) -> dict[str, str]:
    created = (
        foundry.status(project)
        if already_created
        else foundry.create(project, OBJECTIVE, mode=mode)
    )
    assert created["stage"] == "frame"
    assert set(created["question"]) == {
        "field",
        "prompt",
        "architectural_impact",
        "risk",
        "information_gain",
        "decision_implication",
    }

    first = foundry.next_action(project)
    first_field = first["question"]["field"]
    foundry.answer(project, ANSWERS[first_field], field=first_field)
    second = foundry.next_action(project)
    assert second["action"] == "answer"
    assert second["question"]["field"] != first_field
    asked = [first_field, *_answer_every_question(project)]
    assert len(asked) == len(ANSWERS)
    assert len(set(asked)) == len(asked)

    assert foundry.next_action(project)["stage"] == "classify"
    topology = _artifact(project, "topology")
    assert topology["kind"] == "workflow"
    assert len(topology["units"]) == 2 and topology["handoffs"]
    assert foundry.next_action(project)["stage"] == "scout"
    provider = InMemorySearchProvider([_candidate()])
    assert foundry.next_action(project, providers=[provider])["stage"] == "blueprint"
    scout = _artifact(project, "scout")
    assert scout["decisions"][0]["classification"] == "external_dependency"
    assert foundry.next_action(project)["stage"] == "contract"

    report = foundry.validate(project)
    assert report["valid"] is True
    bundle = foundry.generate(project, task_family="notes_to_actions", seeded_defect=True)
    assert bundle["manifest"]["runtime"] == "local-python"
    accepted_hashes = {
        name: _stored(project)["artifacts"][name]["sha256"]
        for name in ("specification", "blueprint", "contracts", "validation")
    }
    unrelated_files = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in _build_directory(project).iterdir()
        if path.name not in {"runner.py", "manifest.json"}
    }

    before_crash = (project / "state.json").read_bytes()
    crashed = subprocess.run(
        [sys.executable, "-c", "import sys; from hyperwilfred_core import foundry; "
         "foundry.evaluate(sys.argv[1])", str(project)],
        env={**os.environ, "FOUNDRY_CRASH_AT": "artifact_published"},
        capture_output=True, timeout=30, check=False,
    )
    assert crashed.returncode == 86, crashed.stderr.decode()
    assert (project / "state.json").read_bytes() == before_crash
    resumed = foundry.resume(project)
    assert resumed["stage"] == "evaluate"
    assert resumed == foundry.status(project)

    evaluation = foundry.evaluate(project)
    assert not evaluation["accepted"]
    assert [name for name, value in evaluation["scenarios"].items() if not value["passed"]] == [
        "edge"
    ]
    with pytest.raises(foundry.FoundryBlocked, match="evaluation requires"):
        foundry.evaluate(project)
    assert foundry.status(project)["stage"] == "repair"
    repair = foundry.repair(project, "edge")
    assert repair["before_sha256"] != repair["after_sha256"]
    assert repair["rerun"]["accepted"] is True
    assert repair["changes"]["runner.py"]["diff"]
    assert set(repair["changes"]) == {"runner.py", "manifest.json"}
    after_repair = _stored(project)
    assert after_repair["repairs"] == [repair]
    assert {
        name: after_repair["artifacts"][name]["sha256"] for name in accepted_hashes
    } == accepted_hashes
    assert unrelated_files == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in _build_directory(project).iterdir()
        if path.name not in {"runner.py", "manifest.json"}
    }

    accepted = foundry.evaluate(project)
    assert accepted["accepted"]
    package = foundry.package(project)
    assert package["evaluation_results"] == {name: True for name in sorted(SCENARIOS)}
    assert any(item["path"] == f"{_build_directory(project).name}/runner.py"
               for item in package["artifacts"])
    assert foundry.resume(project)["next_action"] == "complete"

    persisted = _stored(project)
    for name, item in persisted["artifacts"].items():
        assert _artifact_digest(_artifact(project, name)) == item["sha256"]
    return {
        name: persisted["artifacts"][name]["sha256"]
        for name in (
            "topology",
            "scout",
            "specification",
            "blueprint",
            "contracts",
            "validation",
            "bundle",
            "evaluation",
            "package",
        )
    }


def test_guided_and_autopilot_complete_the_same_reproducible_lifecycle(tmp_path: Path) -> None:
    guided = tmp_path / "guided" / "fixture"
    autopilot = tmp_path / "autopilot" / "fixture"

    guided_hashes = _run_lifecycle(guided, "guided")
    auto_created = foundry.create(autopilot, OBJECTIVE, mode="autopilot")
    auto_state = _stored(autopilot)
    assert auto_created["question"] is not None
    assert auto_state["intake"]["blockers"]
    assert all("high-impact ambiguity" in item for item in auto_state["intake"]["blockers"])
    defaults = auto_state["intake"]["defaults"]
    assert defaults
    assert {item["impact"] for item in defaults} == {"low"}
    assert {item["field"] for item in defaults} >= {"actors", "non_goals"}

    # Continue the already-created autopilot project through the same helper contract.
    autopilot_hashes = _run_lifecycle(autopilot, "autopilot", already_created=True)
    assert autopilot_hashes == guided_hashes


def test_cli_create_status_next_resume_and_existing_plan_parser(tmp_path: Path, capsys) -> None:
    project = tmp_path / "cli-fixture"
    commands = [
        ["foundry", "create", "--project", str(project), "--objective", OBJECTIVE],
        ["foundry", "status", "--project", str(project)],
        ["foundry", "next", "--project", str(project)],
        ["foundry", "resume", "--project", str(project)],
    ]
    for command in commands:
        assert main(command) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["stage"] == "frame"
    parsed = build_parser().parse_args(["plan", "--objective", "existing command"])
    assert parsed.cmd == "plan"


def test_invalid_topology_is_rejected() -> None:
    invalid = TopologyProposal(
        kind=TopologyKind.DETERMINISTIC,
        units=[
            WorkUnit(
                id="reasoner",
                responsibilities=["infer owners"],
                kind=TopologyKind.REASONING,
            )
        ],
    )
    result = validate_topology(invalid)
    assert not result.valid
    assert any("not justified" in error for error in result.errors)
    assert any("should be 'single_agent'" in error for error in result.errors)


def test_unlicensed_scout_candidate_is_rejected() -> None:
    decision = decide_reuse(_candidate(license_name=None))
    assert decision.classification is ReuseClass.REJECTED
    assert "missing license" in decision.rationale


def test_unsupported_adapter_capability_is_rejected() -> None:
    specification = runtime_design()[0]
    with pytest.raises(BundleGenerationBlocked) as blocked:
        LocalPythonManifestAdapter().build_bundle(
            specification, required_capabilities=["external_side_effects"]
        )
    assert blocked.value.capabilities == ("external_side_effects",)


def test_malformed_nested_evaluation_is_a_clean_foundry_error(tmp_path: Path) -> None:
    project = tmp_path / "malformed-evaluation"
    _advance_to_evaluate(project)
    result = foundry.evaluate(project)

    assert result["accepted"] is True
    assert result["records"]["deterministic"]["source"] == "executed_subprocess"
    assert result["records"]["model"]["status"] == "not_run"
    assert result["records"]["human"]["status"] == "not_requested"


def test_resume_rejects_forged_complete_state_without_artifacts(tmp_path: Path) -> None:
    project = tmp_path / "forged-complete"
    foundry.create(project, OBJECTIVE)
    state = _stored(project)
    state["stage"] = "package"
    state["completed"] = ["package"]
    state["artifacts"] = {}
    (project / "state.json").write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(foundry.FoundryBlocked):
        foundry.resume(project)


@pytest.mark.parametrize("target", ["build", "evaluation", "repair"])
def test_resume_rejects_tampered_executable_and_evidence(
    tmp_path: Path, target: str
) -> None:
    project = tmp_path / target
    _advance_to_evaluate(project, seeded_defect=True)
    if target == "build":
        path = _build_directory(project) / "runner.py"
    else:
        foundry.evaluate(project)
        if target == "evaluation":
            path = project / _stored(project)["artifacts"]["evaluation"]["path"]
        else:
            foundry.repair(project, "edge")
            path = project / _stored(project)["artifacts"]["repair-1"]["path"]
    path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(foundry.FoundryBlocked, match="integrity"):
        foundry.resume(project)


def test_external_reuse_decision_survives_specification_and_package(tmp_path: Path) -> None:
    project = tmp_path / "reuse-provenance"
    _advance_to_evaluate(project, providers=(InMemorySearchProvider([_candidate()]),))
    specification = _artifact(project, "specification")
    reuse = specification["external_reuse"]
    assert reuse
    assert "notes-parser-v1" in json.dumps(reuse, sort_keys=True)

    assert foundry.evaluate(project)["accepted"]
    package = foundry.package(project)
    specification_entry = next(
        item for item in package["artifacts"]
        if item["path"] == _stored(project)["artifacts"]["specification"]["path"]
    )
    assert specification_entry["digest"] == _stored(project)["artifacts"]["specification"]["sha256"]
    assert _artifact(project, "specification")["external_reuse"] == reuse


@pytest.mark.parametrize(("family", "topology", "actors"), [
    ("notes_to_actions", "workflow", {"parser", "validate"}),
    ("evidence_answer", "single_agent", {"answerer"}),
    ("reviewed_brief", "multi_agent", {"writer", "reviewer"}),
])
def test_explicit_family_survives_topology_design_and_generation(
    tmp_path: Path, family: str, topology: str, actors: set[str],
) -> None:
    from hyperwilfred_core.foundry_agents import ScriptedProvider

    project = tmp_path / family
    foundry.create(project, "A bounded explicitly selected objective", task_family=family)
    _answer_every_question(project)
    for _ in range(4):
        foundry.next_action(project)
    specification, blueprint = _artifact(project, "specification"), _artifact(project, "blueprint")
    assert _artifact(project, "topology")["kind"] == topology
    assert {node["id"] for node in specification["topology"]["nodes"]} == actors
    assert {step["component_id"] for step in blueprint["steps"]} == actors
    assert blueprint["task_family"] == family and blueprint["topology_kind"] == topology
    expected_kinds = {name: "deterministic" for name in SCENARIOS}
    if family == "reviewed_brief":
        expected_kinds["model_evaluation"] = "model_based"
    assert {item["id"]: item["kind"] for item in specification["evaluations"]} == expected_kinds
    assert specification["failure_policy"]["timeout_seconds"] == 180
    assert foundry.validate(project)["valid"]
    with pytest.raises(foundry.FoundryBlocked, match="task_family"):
        foundry.generate(project, task_family="unsupported")
    assert foundry.status(project)["stage"] == "generate"
    generated = foundry.generate(project, task_family=family)
    assert generated["manifest"]["task_family"] == family
    assert foundry.resume(project)["stage"] == "evaluate"
    evaluation = foundry.evaluate(project, provider=ScriptedProvider())
    assert evaluation["accepted"]
    execution = evaluation["records"]["model"]
    judgment = evaluation["records"]["model_evaluation"]
    assert execution["kind"] == "model_execution"
    assert judgment["kind"] == "model_based"
    assert execution["status"] == ("not_run" if family == "notes_to_actions" else "fixture")
    assert execution["accepted"] is (None if family == "notes_to_actions" else True)
    assert (execution["calls"] > 0) is (family != "notes_to_actions")
    assert judgment["status"] == ("fixture" if family == "reviewed_brief" else "not_run")
    assert judgment["accepted"] is (True if family == "reviewed_brief" else None)
    assert (judgment["calls"] > 0) is (family == "reviewed_brief")
    assert judgment["calls"] <= execution["calls"]


def test_unsupported_goal_cannot_silently_become_notes_parser(tmp_path: Path) -> None:
    project = tmp_path / "unsupported-goal"
    foundry.create(project, "Forecast warehouse demand and reorder stock")
    _answer_every_question(project)
    with pytest.raises(foundry.FoundryBlocked, match="Unsupported or ambiguous objective"):
        foundry.next_action(project)
    assert foundry.resume(project)["stage"] == "frame"
    assert not _stored(project)["artifacts"]


def test_execution_output_is_checked_against_the_generated_contract(tmp_path: Path) -> None:
    from hyperwilfred_core.foundry_design import result_model, result_schema

    project = tmp_path / "result-contract"
    _advance_to_evaluate(project)
    evaluation = foundry.evaluate(project)
    declared = _artifact(project, "specification")["contracts"][0]
    assert declared["json_schema"] == result_schema("notes_to_actions")
    for scenario in evaluation["scenarios"].values():
        for execution in scenario["executions"]:
            if execution["accepted"]:
                result_model("notes_to_actions").model_validate(execution["output"], strict=True)
    with pytest.raises(ValueError):
        result_model("notes_to_actions").model_validate({"actions": [{"text": "wrong field"}]})
