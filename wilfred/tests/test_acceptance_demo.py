"""Acceptance harness tests do not certify live reasoning or human authorization."""
import json

import pytest

from hyperwilfred_core import foundry_acceptance as demo
from hyperwilfred_core.cli import build_parser, main


def test_fixture_demo_executes_all_three_architectures_and_preserves_evidence(tmp_path):
    root = tmp_path / "proof"
    result = demo.run(root, "fixture")
    assert result["architectures"] == {"notes_to_actions": "workflow",
            "evidence_answer": "single_agent", "reviewed_brief": "multi_agent"}
    assert result["live_models_demonstrated"] is False
    single = json.loads((root / "evidence_answer-proof.json").read_text())
    assert single["input"]["question"] == (
        "How many days remain? Reply with only '<number> days remain.' using the calculated number."
    )
    assert not any(character.isdigit() for character in single["input"]["question"])
    assert single["execution"]["output"]["answer"] == "23 days remain."
    assert single["execution"]["evidence"]["model_evidence"] == "fixture"
    multi = json.loads((root / "reviewed_brief-proof.json").read_text())
    assert multi["execution"]["output"]["content_review"]["approved"]
    assert {call["role"] for call in multi["execution"]["evidence"]["model_calls"]} == {
        "writer", "reviewer"}
    approval = json.loads((root / "approval-proof.json").read_text())
    assert len(approval["absent_and_self_claim_blocked"]) == 2
    assert approval["signed_positive_is_test_fixture"] and approval["replay_blocked"]
    before = (root / "summary.json").read_bytes()
    with pytest.raises(RuntimeError, match="never overwritten"):
        demo.run(root, "fixture")
    assert (root / "summary.json").read_bytes() == before


def test_cli_executes_generated_task_and_requires_signed_token(tmp_path, capsys):
    project = tmp_path / "workflow"
    demo.prepare(project, "notes_to_actions")
    payload = tmp_path / "input.json"
    payload.write_text(json.dumps({"notes": "- Prepare summary"}))
    command = ["foundry", "execute", "--project", str(project), "--input-file", str(payload)]
    assert main(command) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["output"]["actions"] == [{"title": "Prepare summary", "source_line": 1}]
    parsed = build_parser().parse_args(["foundry", "create", "--project", "p", "--objective", "x",
                                       "--task-family", "evidence_answer"])
    assert parsed.task_family == "evidence_answer"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["gate-approve", "--run-id", "r", "--gate-id", "g",
                                   "--decision", "approve", "--by", "self"])


def test_cli_agent_without_provider_returns_actionable_error(tmp_path, capsys):
    project = tmp_path / "agent"
    demo.prepare(project, "evidence_answer")
    payload = tmp_path / "input.json"
    payload.write_text(json.dumps(demo.SYNTHESIS))
    assert main(["foundry", "execute", "--project", str(project),
                 "--input-file", str(payload)]) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["kind"] == "foundry_error" and "provider" in error["error"]
