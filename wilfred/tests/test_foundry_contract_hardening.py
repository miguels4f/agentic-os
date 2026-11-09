"""Executable contracts, secret rejection, and completion must agree with the runtime."""
import json

import pytest

from hyperwilfred_core import foundry
from hyperwilfred_core.foundry_acceptance import ANSWERS, OBJECTIVES, prepare
from hyperwilfred_core.foundry_design import topology_for
from hyperwilfred_core.foundry_intake import IntakeState
from hyperwilfred_core.foundry_runtime import BundleGenerationBlocked, LocalPythonManifestAdapter


def runtime_design(family="notes_to_actions"):
    intake = IntakeState().update({"outcome": OBJECTIVES[family], **ANSWERS})
    specification, blueprint = foundry._design(
        {"project_id": "runtime-fixture", "objective": OBJECTIVES[family], "task_family": family,
         "intake": intake.model_dump(mode="json")},
        topology_for(family).model_dump(mode="json"), {})
    return specification.model_dump(mode="json"), blueprint


@pytest.mark.parametrize("family", OBJECTIVES)
@pytest.mark.parametrize("change", [
    "contract", "input", "output", "component", "topology", "handoff",
    "blueprint_input", "blueprint_output", "blueprint_topology", "blueprint_handoff",
])
def test_incompatible_executable_requirements_are_rejected(family, change):
    spec, blueprint = runtime_design(family)
    custom = {"name": "payload", "json_schema": {
        "type": "object", "required": ["value"], "properties": {"value": {"type": "string"}}}}
    if change == "contract":
        spec["contracts"] = [custom]
    elif change in {"input", "output"}:
        spec["components"][0][change + "s"] = [custom]
    elif change == "component":
        spec["components"][0]["responsibilities"] = ["Run an unimplemented custom task"]
    elif change == "topology":
        spec["topology"]["nodes"][0]["kind"] = "workflow"
    elif change == "handoff":
        spec["handoffs"] = [{"source_component": spec["components"][0]["id"],
            "target_component": spec["components"][-1]["id"], "trigger": "custom",
            "contract": custom}]
    elif change == "blueprint_topology":
        blueprint["topology_kind"] = "unsupported"
    elif change == "blueprint_handoff":
        blueprint["handoffs"] = [{"contract": custom}]
    else:
        blueprint["steps"][0][change.removeprefix("blueprint_") + "s"] = [custom]
    with pytest.raises(BundleGenerationBlocked, match=r"Why:.*Unlock:"):
        LocalPythonManifestAdapter().build_bundle(spec, blueprint=blueprint, task_family=family)


@pytest.mark.parametrize("change", ["extra_step", "dependencies"])
def test_malformed_blueprint_is_blocked_with_actionable_error(change):
    spec, blueprint = runtime_design("evidence_answer")
    if change == "extra_step":
        blueprint["steps"].append(None)
    else:
        blueprint["steps"][0]["depends_on"] = None
    with pytest.raises(BundleGenerationBlocked, match=r"Why:.*Unlock:"):
        LocalPythonManifestAdapter().build_bundle(
            spec, blueprint=blueprint, task_family="evidence_answer")


@pytest.mark.parametrize("value", [
    {"api_key": 987654321}, {"password": 0}, {"session": True}, {"session": False},
    {"nested": [{"password": 987654321}]}, {"sk-" + "SYNTHETIC" * 4: None},
    "bearer " + "SYNTHETIC" * 4, "basic " + "SYNTHETIC" * 4,
    "github_pat_" + "SYNTHETIC" * 4,
])
def test_secret_variants_never_reach_persistent_intake(tmp_path, value):
    project = tmp_path / "secret-test"
    foundry.create(project, OBJECTIVES["notes_to_actions"])
    before = {path: path.read_bytes() for path in project.rglob("*") if path.is_file()}
    with pytest.raises(foundry.FoundryBlocked, match="secret") as blocked:
        foundry.answer(project, value)
    assert "SYNTHETIC" not in str(blocked.value) and "987654321" not in str(blocked.value)
    assert before == {path: path.read_bytes() for path in project.rglob("*") if path.is_file()}


@pytest.mark.parametrize("empty", [None, "", "  ", [], {}])
def test_empty_credential_references_remain_allowed(empty):
    foundry._reject_secrets({"password": empty, "nested": {"api_key": empty}})


@pytest.mark.parametrize("target", ["runner", "artifact", "linked_artifact"])
def test_terminal_status_checks_the_same_integrity_as_resume_and_package(tmp_path, target):
    project = tmp_path / "complete"
    prepare(project, "notes_to_actions")
    assert foundry.evaluate(project)["accepted"]
    foundry.package(project)
    assert foundry.status(project)["next_action"] == "complete"
    root, state = foundry._load(project)
    artifact = root / state["artifacts"]["evaluation"]["path"]
    if target == "runner":
        build = foundry._read_artifact(root, state, "build")
        runner = root / build["directory"] / "runner.py"
        runner.write_text(runner.read_text() + "\n# changed\n")
    elif target == "artifact":
        artifact.write_text(json.dumps({"accepted": True}))
    else:
        copy = tmp_path / "same-bytes.json"
        copy.write_bytes(artifact.read_bytes())
        artifact.unlink()
        artifact.symlink_to(copy)
    before = {path: path.read_bytes() for path in project.rglob("*") if path.is_file()}
    for operation in (foundry.status, foundry.resume, foundry.package):
        with pytest.raises(foundry.FoundryBlocked):
            operation(project)
    assert before == {path: path.read_bytes() for path in project.rglob("*") if path.is_file()}
