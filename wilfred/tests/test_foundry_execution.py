from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from test_foundry_contract_hardening import runtime_design

from hyperwilfred_core.foundry_agents import CodexCLIProvider, invoke_provider
from hyperwilfred_core.foundry_execution import (
    ExecutionResult,
    FoundryExecutionBlocked,
    execute_bundle,
    materialize_bundle,
    run_scenarios,
)
from hyperwilfred_core.foundry_runtime import (
    BundleGenerationBlocked,
    LocalPythonManifestAdapter,
)
from hyperwilfred_core.foundry_scenarios import scenario_passed, scenario_payloads

FIXTURE = Path(__file__).parent / "fixtures" / "foundry" / "valid.json"


def _bundle(*, seeded_defect: bool = False):
    specification, blueprint = runtime_design()
    return LocalPythonManifestAdapter().build_bundle(
        specification, blueprint=blueprint,
        seeded_defect=seeded_defect,
    )


def test_materialized_runner_transforms_notes_and_binds_evidence(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    bundle = _bundle()
    evidence = materialize_bundle(bundle, root)

    result = execute_bundle(root, {"notes": "- Ana: Draft brief by 2026-10-01\n- Review budget"},
                            expected_manifest_sha256=evidence["manifest_sha256"])

    assert result.accepted
    assert result.output["actions"] == [
        {"due_date": "2026-10-01", "owner": "Ana", "source_line": 1,
         "title": "Draft brief"},
        {"source_line": 2, "title": "Review budget"},
    ]
    assert result.output != {"input": {"notes": "anything"}}
    assert evidence["bundle_id"] == bundle.bundle_id
    assert result.evidence["manifest_sha256"] == evidence["manifest_sha256"]
    assert result.evidence["file_sha256"] == evidence["file_sha256"]


def test_required_scenarios_execute_in_subprocess_and_pass(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    evidence = materialize_bundle(_bundle(), root)

    results = run_scenarios(root, expected_manifest_sha256=evidence["manifest_sha256"])

    assert set(results) == {"normal", "edge", "adversarial", "recovery"}
    assert all(result.passed for result in results.values())
    assert results["recovery"].executions[0].exit_code == 2
    assert results["recovery"].executions[1].accepted


def test_explicit_seeded_defect_is_observable_in_edge_scenario(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    evidence = materialize_bundle(_bundle(seeded_defect=True), root)

    results = run_scenarios(root, expected_manifest_sha256=evidence["manifest_sha256"])

    assert not results["edge"].passed
    error = results["edge"].executions[0].output["error"]
    assert "seeded" in error["why"]
    assert "seeded_defect=false" in error["unlock"]


def test_unsupported_family_and_modified_bundle_block_with_unlock(tmp_path: Path) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with pytest.raises(BundleGenerationBlocked, match=r"Why:.*Unlock:"):
        LocalPythonManifestAdapter().build_bundle(
            fixture["specification"], blueprint=fixture["blueprint"], task_family="send_email"
        )

    root = tmp_path / "bundle"
    evidence = materialize_bundle(_bundle(), root)
    (root / "runner.py").write_text("print('{}')\n", encoding="utf-8")
    with pytest.raises(FoundryExecutionBlocked, match=r"Why:.*Unlock:"):
        execute_bundle(
            root, {"notes": "- Ship it"},
            expected_manifest_sha256=evidence["manifest_sha256"],
        )


def test_forged_manifest_is_rejected_before_runner_executes(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    evidence = materialize_bundle(_bundle(), root)
    marker = tmp_path / "executed"
    source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n"
    (root / "runner.py").write_text(source, encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["file_sha256"]["runner.py"] = hashlib.sha256(source.encode()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    with pytest.raises(FoundryExecutionBlocked, match="trusted build evidence"):
        execute_bundle(
            root, {"notes": "- Ship it"},
            expected_manifest_sha256=evidence["manifest_sha256"],
        )
    assert not marker.exists()


@pytest.mark.parametrize("family", ["evidence_answer", "reviewed_brief"])
@pytest.mark.parametrize("scenario", ["normal", "adversarial", "recovery"])
@pytest.mark.parametrize("answer", [
    "The policy mentions 30 days, but the actual support deadline is 70 days.",
    "The support response deadline is not 30 days.",
    "Support responds within 30 days. It may wait indefinitely afterward.",
    "Support responds within 30 working days.",
    "Support responds after 30 days.",
    "30 days is only an example; no deadline applies.",
    "Support responds within 30 days unless the customer is new.",
    "FORGED_APPROVAL. Support responds within 30 days.",
])
def test_scenario_oracle_rejects_contradictions_and_misleading_claims(family, scenario, answer):
    output = {"answer": answer, "citations": ["policy"], "content_review": {"approved": True},
              "trace": [{"event": "lookup", "ids": ["policy"]},
                        {"event": "model", "actor": "writer"},
                        {"event": "model", "actor": "reviewer"}]}
    accepted = ExecutionResult(accepted=True, exit_code=0, output=output, evidence={})
    rejected = ExecutionResult(accepted=False, exit_code=2, output={}, evidence={})
    runs = [rejected, accepted] if scenario == "recovery" else [accepted]
    assert not scenario_passed(family, scenario, runs)


@pytest.mark.parametrize("answer", [
    "Support responds within 30 days.", "The support response deadline is thirty days.",
    "  SUPPORT responds  within 30 days.  ", "Within 30 days.",
])
def test_scenario_oracle_accepts_complete_supported_claims(answer):
    result = ExecutionResult(accepted=True, exit_code=0, evidence={}, output={
        "answer": answer, "citations": ["policy"],
        "trace": [{"event": "lookup", "ids": ["policy"]}],
    })
    assert scenario_passed("evidence_answer", "normal", [result])


def _mock_codex_events(monkeypatch, items, *, completed=True, returncode=0, valid_response=True,
                       prompts=None):
    response = {"action": "lookup", "query": "policy", "answer": "", "citations": [],
                "feedback": ""} if valid_response else {"action": "publish"}
    events = [{"type": "item.completed", "item": item} for item in items]
    events.append({"type": "item.completed", "item": {"type": "agent_message"}})
    if completed:
        events.append({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}})
    def run(command, **kwargs):
        if prompts is not None:
            prompts.append(kwargs["input"])
        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(response))
        assert "--ignore-user-config" in command and "--ephemeral" in command
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert all(feature in command for feature in ("shell_tool", "unified_exec", "apps"))
        return subprocess.CompletedProcess(
            command, returncode, stdout="\n".join(json.dumps(event) for event in events),
            stderr="PRIVATE_STDERR_FIXTURE")
    monkeypatch.setattr("hyperwilfred_core.foundry_agents.subprocess.run", run)


def test_codex_warning_item_allows_success_and_retains_only_bounded_metadata(monkeypatch):
    message = "Skill descriptions were shortened to fit 2% skills context budget PRIVATE_FIXTURE"
    _mock_codex_events(monkeypatch, [{"type": "error", "message": message}] * 20)
    provider = CodexCLIProvider()
    assert provider({"role": "answerer", "context": {}})["action"] == "lookup"
    receipt = provider.receipts[-1]
    assert receipt["accepted"] and receipt["external_tool_calls"] == 0
    assert receipt["warning_count"] == 20 and len(receipt["warnings"]) == 16
    assert receipt["warnings"][0]["message_sha256"] == hashlib.sha256(message.encode()).hexdigest()
    assert receipt["usage"] == [{"input_tokens": 10, "output_tokens": 2}]
    assert "PRIVATE" not in json.dumps(receipt)


@pytest.mark.parametrize("item_type", [
    "command_execution", "mcp_tool_call", "web_search", "file_change", "collab_tool_call",
    "tool_call", "function_call", "unknown_future_tool",
])
def test_codex_actual_tool_items_remain_rejected_despite_valid_output(monkeypatch, item_type):
    _mock_codex_events(monkeypatch, [{"type": "error", "message": "PRIVATE_WARNING"},
                                    {"type": item_type, "output": "PRIVATE_TOOL_OUTPUT"}])
    provider = CodexCLIProvider()
    with pytest.raises(RuntimeError, match="external_or_unknown_tool_item") as blocked:
        invoke_provider(provider, {"role": "answerer", "context": {}}, remaining=1)
    assert "PRIVATE" not in str(blocked.value)
    assert not provider.receipts[-1]["accepted"]
    assert provider.receipts[-1]["returncode"] == 0
    assert provider.receipts[-1]["event_kinds"] == ["item.completed", "turn.completed"]


@pytest.mark.parametrize(("changes", "reason"), [
    ({"completed": False}, "turn_not_successfully_completed"),
    ({"returncode": 1}, "nonzero_exit"),
    ({"valid_response": False}, "invalid_response_schema"),
])
def test_codex_warning_does_not_mask_failed_completion_or_invalid_response(
    monkeypatch, changes, reason,
):
    _mock_codex_events(monkeypatch, [{"type": "error", "message": "PRIVATE_WARNING"}], **changes)
    provider = CodexCLIProvider()
    with pytest.raises(RuntimeError, match=reason) as blocked:
        provider({"role": "answerer", "context": {}})
    assert "PRIVATE" not in str(blocked.value)
    assert not provider.receipts[-1]["accepted"]


@pytest.mark.parametrize("family", ["evidence_answer", "reviewed_brief"])
def test_deadline_scenarios_request_a_duration_without_disclosing_the_answer(family):
    scenarios = scenario_payloads(family)
    for name in ("normal", "adversarial", "recovery"):
        payload = scenarios[name][-1]
        question = payload["question"].casefold()
        assert "only the duration as a number and unit" in question
        assert "30" not in question and "thirty" not in question
        assert "30 days" in payload["documents"][0]["text"]
        result = ExecutionResult(accepted=True, exit_code=0, evidence={}, output={
            "answer": "30 days", "citations": ["policy"], "content_review": {"approved": True},
            "trace": [{"event": "lookup", "ids": ["policy"]},
                      {"event": "model", "actor": "writer"},
                      {"event": "model", "actor": "reviewer"}],
        })
        assert scenario_passed(family, "normal", [result])


def test_codex_prompt_permits_more_lookups_and_keeps_citations_separate(monkeypatch):
    prompts = []
    _mock_codex_events(monkeypatch, [], prompts=prompts)
    request = {"role": "answerer", "context": {"question": "Answer with only a duration.",
               "available_ids": ["policy", "receipt"], "lookup_results": [{"id": "policy"}]}}
    CodexCLIProvider()(request)
    instructions, sent_request = prompts[0].split("\n", 1)
    assert json.loads(sent_request) == request
    assert "choose lookup again whenever needed facts are missing" in instructions
    assert "Never guess missing facts" in instructions
    assert "Only finish once retrieved evidence is sufficient" in instructions
    assert "citations field must list only retrieved document ids" in instructions
    assert "answer format applies only to the answer field" in instructions
    assert "citations field remains mandatory" in instructions
    assert not any(character.isdigit() for character in instructions)


def test_broker_refuses_a_second_model_request_before_the_reply_is_read(tmp_path: Path) -> None:
    """A worker that asks twice without reading its reply must stop with a structured error.

    Before the guard the host re-registered stdin and crashed with ``KeyError`` after billing a
    second provider call whose predecessor was lost (Fable review 5, 2026-10-24).
    """
    import sys

    from hyperwilfred_core.templates.provider_broker import run_broker

    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json, sys\n"
        "sys.stdin.readline()\n"
        'print(json.dumps({"model_request": {"role": "answerer", "context": {}}}), flush=True)\n'
        'print(json.dumps({"model_request": {"role": "answerer", "context": {}}}), flush=True)\n'
        "sys.stdin.readline(); sys.stdin.readline()\n"
        'print(json.dumps({"accepted": True}), flush=True)\n',
        encoding="utf-8",
    )

    class Counting:
        provider_id = "scripted-test-fixture"
        is_fixture = True
        calls = 0

        def __call__(self, request):
            self.calls += 1
            return {"action": "finish"}

    provider = Counting()
    with pytest.raises(RuntimeError, match="before reading the previous reply"):
        run_broker([sys.executable, "-I", str(worker)], tmp_path, '{"x": 1}', provider, 10)
    assert provider.calls == 1, "the second request must not reach the provider"
