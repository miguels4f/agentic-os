"""Observable model/tool turns; fixtures are explicitly not live-model evidence."""
import pytest
from test_foundry_contract_hardening import runtime_design

from hyperwilfred_core.foundry_agents import ScriptedProvider
from hyperwilfred_core.foundry_execution import (
    FoundryExecutionBlocked,
    execute_bundle,
    materialize_bundle,
    run_scenarios,
)
from hyperwilfred_core.foundry_families import FAMILIES
from hyperwilfred_core.foundry_runtime import LocalPythonManifestAdapter, repair_bundle_source
from hyperwilfred_core.foundry_scenarios import scenario_passed, scenario_payloads


def bundle(family, seeded=False):
    specification, blueprint = runtime_design(family)
    return LocalPythonManifestAdapter().build_bundle(
        specification, blueprint=blueprint,
        task_family=family, seeded_defect=seeded,
    )


@pytest.mark.parametrize("family", FAMILIES)
def test_three_families_have_distinct_sources_and_executed_scenarios(tmp_path, family):
    generated = bundle(family)
    assert len({bundle(f).files()["runner.py"] for f in FAMILIES}) == 3
    assert generated.manifest["architecture"]["rationale"]
    evidence = materialize_bundle(generated, tmp_path / family)
    results = run_scenarios(tmp_path / family, provider=ScriptedProvider(),
                            expected_manifest_sha256=evidence["manifest_sha256"])
    assert all(r.passed for r in results.values())
    normal = results["normal"].executions[0]
    assert normal.evidence["model_evidence"] == (
        "no-model" if family == "notes_to_actions" else "fixture")
    if family != "notes_to_actions":
        assert [t["event"] for t in normal.output["trace"]][:3] == ["model", "lookup", "model"]
        assert all(c["is_fixture"] for c in normal.evidence["model_calls"])


@pytest.mark.parametrize("family", FAMILIES)
def test_repair_modifies_source_only_and_preserves_accepted_components(tmp_path, family):
    defective = bundle(family, seeded=True)
    fixed = repair_bundle_source(defective)
    before, after = defective.files(), fixed.files()
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {"runner.py", "manifest.json"}
    assert "if True:  # seeded_empty_defect" in before["runner.py"]
    assert "if False:  # seeded_empty_defect" in after["runner.py"]
    for version, expected in ((defective, False), (fixed, True)):
        root = tmp_path / version.bundle_id
        evidence = materialize_bundle(version, root)
        results = run_scenarios(root, provider=ScriptedProvider(),
                                expected_manifest_sha256=evidence["manifest_sha256"])
        assert results["edge"].passed is expected
        assert all(results[name].passed for name in ("normal", "adversarial", "recovery"))


def test_reviewer_rejection_causes_revision_in_separate_context(tmp_path):
    class RejectOnce(ScriptedProvider):
        requests = []

        def __call__(self, request):
            self.requests.append(request)
            if request["role"] == "reviewer":
                assert set(request["context"]) == {"question", "draft", "evidence", "task"}
                count = sum(r["role"] == "reviewer" for r in self.requests)
                return {"action": "reject" if count == 1 else "approve",
                        "feedback": "Clarify the deadline"}
            return super().__call__(request)
    provider = RejectOnce()
    evidence = materialize_bundle(bundle("reviewed_brief"), tmp_path / "bundle")
    result = execute_bundle(tmp_path / "bundle", scenario_payloads("reviewed_brief")["normal"][0],
                            provider=provider, expected_manifest_sha256=evidence["manifest_sha256"])
    assert result.accepted and result.output["content_review"]["approved"]
    assert any(t["event"] == "revision" for t in result.output["trace"])
    assert any("revision" in r["context"] for r in provider.requests if r["role"] == "writer")
    assert len([r for r in provider.requests if r["role"] == "reviewer"]) == 2


def test_agent_requires_explicit_provider_and_cannot_publish(tmp_path):
    evidence = materialize_bundle(bundle("evidence_answer"), tmp_path / "bundle")
    payload = {**scenario_payloads("evidence_answer")["normal"][0], "human_approved": True}
    with pytest.raises(FoundryExecutionBlocked, match="provider"):
        execute_bundle(tmp_path / "bundle", payload,
                       expected_manifest_sha256=evidence["manifest_sha256"])
    class Publish(ScriptedProvider):
        def __call__(self, request):
            return {"action": "publish", "human_approved": True}
    result = execute_bundle(tmp_path / "bundle", payload, provider=Publish(),
                            expected_manifest_sha256=evidence["manifest_sha256"])
    assert not result.accepted and "unavailable operation" in result.output["error"]["why"]
    assert "human_approved" not in result.output


def test_scenario_assertions_reject_plausible_but_wrong_answers(tmp_path):
    evidence = materialize_bundle(bundle("evidence_answer"), tmp_path / "bundle")
    result = execute_bundle(tmp_path / "bundle", scenario_payloads("evidence_answer")["normal"][0],
                            provider=ScriptedProvider(),
                            expected_manifest_sha256=evidence["manifest_sha256"])
    result.output["answer"] = "Support responds in 70 days."
    assert not scenario_passed("evidence_answer", "normal", [result])
