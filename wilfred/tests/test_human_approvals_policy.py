"""The owner's human-approvals policy switch.

Proves: with the switch absent or malformed every approval point still needs a signature
(fail-closed); with it on, the eligible points pass with an honest ``policy:auto`` label and no
signature; every run gate (ship, merge, deploy, deps, secrets, migration), learning-proposal
promotion and independent-review attestation need a signature in every case; and the
non-signature checks keep blocking.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from test_gates import _run
from test_improve import _improve_run_with_baseline, _toolchain_ready, improve_repo  # noqa: F401
from test_quality import _record_subjective, _run_at_quality
from test_review_provenance import _panel
from test_verify_checks import PY, _at_verify

from hyperwilfred_core import approvals, engine, gates, learning, quality, regression, verify_checks
from hyperwilfred_core.cli import main
from hyperwilfred_core.schemas import (
    BuildObjective,
    GateKind,
    LiveDemo,
    ProposalStatus,
    ReviewAuthor,
)

VALID = {
    "human_approvals": "auto-except-ship-and-promote",
    "set_by": "Fixture Owner",
    "since": "2026-09-30",
}
NOTICE = "Auto-approved by policy (no human signature)"


@pytest.fixture
def policy_file(tmp_path: Path, monkeypatch) -> Path:
    """A policy path the test controls; the switch is OFF until the test writes the file."""
    path = tmp_path / "wilfred-home" / "policy.json"
    path.parent.mkdir()
    monkeypatch.setattr(approvals, "DEFAULT_POLICY_PATH", path)
    return path


def _switch_on(path: Path) -> str:
    path.write_text(json.dumps(VALID), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _forge_policy_decision(gates_dir: Path, gate_id: str) -> Path:
    """What an agent could write by hand: a well-formed policy decision for any challenge."""
    challenge = approvals.load_challenge(gates_dir, gate_id)
    policy = approvals.active_policy()
    payload = {
        "schema_version": approvals.POLICY_DECISION_SCHEMA,
        "purpose": challenge["purpose"],
        "run_id": challenge["run_id"],
        "gate_id": gate_id,
        "challenge_sha256": hashlib.sha256(approvals.canonical_bytes(challenge)).hexdigest(),
        "nonce": challenge["nonce"],
        "decision": "approve",
        "approved_by": approvals.POLICY_APPROVER,
        "human_signature": False,
        "policy_sha256": policy["sha256"] if policy else "0" * 64,
    }
    token = gates_dir / f"{gate_id}.decision.json"
    token.write_text(json.dumps({"payload": payload}), encoding="utf-8")
    return token


def _adapter_reviewed_run(repo: Path) -> engine.RunPaths:
    paths = _run_at_quality(repo, threshold=4)
    _record_subjective(repo, "q", [5, 5, 5, 5, 5])
    _panel(repo, "q", ReviewAuthor.ADAPTER)
    return paths


def _override(repo: Path) -> dict:
    return quality.record_independent_override(
        repo_root=repo, run_id="q", by="agent", reason="critics rate limited"
    )


# --------------------------------------------------------------------------- the switch itself


def test_suite_is_isolated_from_the_hosts_policy_file() -> None:
    host = Path.home() / "Library" / "Application Support" / "Wilfred" / "policy.json"
    assert approvals.DEFAULT_POLICY_PATH != host
    assert approvals.active_policy() is None


def test_valid_switch_is_reported_with_its_hash(policy_file: Path) -> None:
    assert approvals.active_policy() is None
    sha = _switch_on(policy_file)
    policy = approvals.active_policy()
    assert policy is not None
    assert policy["sha256"] == sha and policy["set_by"] == "Fixture Owner"


@pytest.mark.parametrize("content", [
    "",
    "not json",
    "[]",
    '"auto-except-ship-and-promote"',
    json.dumps({**VALID, "human_approvals": "auto"}),
    json.dumps({**VALID, "human_approvals": "AUTO-EXCEPT-SHIP-AND-PROMOTE"}),
    json.dumps({**VALID, "human_approvals": True}),
    json.dumps({**VALID, "set_by": "  "}),
    json.dumps({**VALID, "set_by": 7}),
    json.dumps({**VALID, "since": "yesterday"}),
    json.dumps({**VALID, "since": 20260930}),
    json.dumps({**VALID, "extra": "key"}),
    json.dumps({k: v for k, v in VALID.items() if k != "since"}),
])
def test_malformed_switch_is_fail_closed(policy_file: Path, tmp_path: Path, content: str) -> None:
    policy_file.write_text(content, encoding="utf-8")
    assert approvals.active_policy() is None
    _adapter_reviewed_run(tmp_path)
    with pytest.raises(quality.QualityError, match="must sign"):
        _override(tmp_path)


def test_policy_status_command_reports_the_switch(policy_file: Path, capsys) -> None:
    assert main(["policy-status"]) == 0
    off = json.loads(capsys.readouterr().out)
    assert off["active"] is False and off["human_approvals"] == "signature-required"
    sha = _switch_on(policy_file)
    assert main(["policy-status"]) == 0
    on = json.loads(capsys.readouterr().out)
    assert on["active"] is True and on["policy"]["sha256"] == sha
    assert on["human_approvals"] == "auto-except-ship-and-promote"


# --------------------------------------------------------------------------- quality override


def test_quality_override_without_switch_still_needs_a_signature(
    policy_file: Path, tmp_path: Path,
) -> None:
    _adapter_reviewed_run(tmp_path)
    with pytest.raises(quality.QualityError, match="must sign"):
        _override(tmp_path)
    assert quality.quality_gate(repo_root=tmp_path, run_id="q")["action"] == (
        "pending_independent_review"
    )


def test_quality_override_is_auto_approved_and_labelled_honestly(
    policy_file: Path, tmp_path: Path, capsys,
) -> None:
    paths = _adapter_reviewed_run(tmp_path)
    sha = _switch_on(policy_file)

    override = _override(tmp_path)["override"]

    assert NOTICE in capsys.readouterr().err
    assert override["by"] == "policy:auto"
    assert "no human signature" in override["reason"] and sha in override["reason"]
    assert override["claimed_by"] == "agent"
    token = json.loads(
        (paths.run_dir / "gates" / f"{override['gate_id']}.decision.json").read_text()
    )
    assert "signature" not in token
    assert token["payload"]["approved_by"] == "policy:auto"
    assert token["payload"]["human_signature"] is False
    assert token["payload"]["policy_sha256"] == sha

    gate = quality.quality_gate(repo_root=tmp_path, run_id="q")
    assert gate["action"] == "pass"
    assert gate["independent_review_override"]["by"] == "policy:auto"
    banner = quality.quality_status(repo_root=tmp_path, run_id="q")["provenance_banner"]
    assert any("POLICY AUTO-APPROVAL (no human signature)" in line for line in banner)
    assert not any("HUMAN OVERRIDE" in line for line in banner)
    # The review is still reported as NOT independent: the policy never relabels it.
    assert any("NOT independent" in line for line in banner)

    # The ship gate shows the unsigned approval to the human who signs the ship decision.
    opened = gates.gate_open(
        repo_root=tmp_path, run_id="q", kind=GateKind.MERGE, step_id="ship", action="merge"
    )
    assert any(line.startswith("POLICY AUTO-APPROVAL (no human signature): accept "
                               "non-independent quality review") for line in opened["banner"])


def test_policy_approval_is_inert_once_the_switch_is_removed(
    policy_file: Path, tmp_path: Path,
) -> None:
    paths = _adapter_reviewed_run(tmp_path)
    _switch_on(policy_file)
    _override(tmp_path)
    state = engine.load_state(paths)
    assert quality.provenance_report(repo_root=tmp_path, run_id="q", state=state)["override"]

    policy_file.unlink()

    state = engine.load_state(paths)
    assert quality.provenance_report(
        repo_root=tmp_path, run_id="q", state=state
    )["override"] is None


def test_policy_does_not_waive_the_deterministic_gate(policy_file: Path, tmp_path: Path) -> None:
    _run_at_quality(tmp_path, threshold=4, det_green=False)
    _record_subjective(tmp_path, "q", [5, 5, 5, 5, 5])
    _panel(tmp_path, "q", ReviewAuthor.ADAPTER)
    _switch_on(policy_file)
    _override(tmp_path)
    assert quality.quality_gate(repo_root=tmp_path, run_id="q")["action"] == (
        "deterministic_gate_required"
    )


def test_policy_does_not_waive_a_low_quality_score(policy_file: Path, tmp_path: Path) -> None:
    _run_at_quality(tmp_path, threshold=4)
    _record_subjective(tmp_path, "q", [2, 2, 2, 2, 2])
    _panel(tmp_path, "q", ReviewAuthor.ADAPTER)
    _switch_on(policy_file)
    _override(tmp_path)
    assert quality.quality_gate(repo_root=tmp_path, run_id="q")["action"] == "patch"


# --------------------------------------------------------------------------- verify override


def _live_demo_gap(repo: Path) -> engine.RunPaths:
    obj = BuildObjective(
        id="o", title="t", objective="x",
        live_demo=LiveDemo(
            promise="account actions work", command=f"{PY} -c \"print('every action blocked')\"",
            failure_markers=["every action blocked"],
        ),
    )
    paths = _at_verify(repo, obj)
    with pytest.raises(engine.EngineError, match="Live-demo gap"):
        engine.complete_step(repo_root=repo, run_id="v", step_id="verify")
    return paths


def test_verify_override_without_switch_still_needs_a_signature(
    policy_file: Path, tmp_path: Path,
) -> None:
    _live_demo_gap(tmp_path)
    with pytest.raises(verify_checks.VerifyCheckError, match="sign"):
        verify_checks.record_override(
            repo_root=tmp_path, run_id="v", check="live_demo", by="agent", reason="known gap"
        )
    with pytest.raises(engine.EngineError, match="Live-demo gap"):
        engine.complete_step(repo_root=tmp_path, run_id="v", step_id="verify")


def test_verify_override_is_auto_approved_and_labelled_honestly(
    policy_file: Path, tmp_path: Path, capsys,
) -> None:
    _live_demo_gap(tmp_path)
    sha = _switch_on(policy_file)

    entry = verify_checks.record_override(
        repo_root=tmp_path, run_id="v", check="live_demo", by="agent", reason="known gap"
    )["override"]

    assert NOTICE in capsys.readouterr().err
    assert entry["by"] == "policy:auto" and entry["approved_by"] == "policy:auto"
    assert entry["human_signature"] is False and entry["policy_sha256"] == sha
    out = engine.complete_step(repo_root=tmp_path, run_id="v", step_id="verify")
    finding = out["verify_checks"]["findings"][0]
    assert finding["override"]["human_signature"] is False

    # A check with no override stays non-overridable under the policy.
    with pytest.raises(verify_checks.VerifyCheckError, match="cannot be overridden"):
        verify_checks.record_override(
            repo_root=tmp_path, run_id="v", check="traceability", by="agent", reason="r"
        )


# --------------------------------------------------------------------------- test evolution


def _evolved_test(tmp_path: Path, *, record_review: bool = True) -> tuple[Path, dict, Path]:
    package = tmp_path / "package"
    package.mkdir()
    test_file = package / "test_contract.py"
    test_file.write_text("def test_contract():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    baseline = regression.capture_baseline(package)
    test_file.write_text("def test_contract():\n    assert 3 + 3 == 6\n", encoding="utf-8")
    run_dir = tmp_path / ".hw" / "run"
    if record_review:
        regression.create_semantic_review_challenge(
            run_dir, run_id="run", path="test_contract.py",
            before_sha256=baseline["test_file_hashes"]["test_contract.py"],
            after_sha256=hashlib.sha256(test_file.read_bytes()).hexdigest(),
            dispatch_sha256="a" * 64, report_sha256="b" * 64,
        )
    return package, baseline, run_dir


def _transitions(package: Path, baseline: dict, run_dir: Path) -> dict:
    return regression.verified_semantic_transitions(
        run_dir, run_id="run", target=package, baseline=baseline
    )


def test_test_evolution_without_switch_still_needs_a_signature(
    policy_file: Path, tmp_path: Path,
) -> None:
    package, baseline, run_dir = _evolved_test(tmp_path)
    transitions = _transitions(package, baseline, run_dir)
    assert transitions == {}
    result = regression.compare(package, baseline, approved_transitions=transitions)
    assert result.passed is False and "regression-witness integrity" in result.details


def test_test_evolution_review_is_auto_approved_and_labelled_honestly(
    policy_file: Path, tmp_path: Path, capsys,
) -> None:
    package, baseline, run_dir = _evolved_test(tmp_path)
    _switch_on(policy_file)

    transitions = _transitions(package, baseline, run_dir)

    assert NOTICE in capsys.readouterr().err
    assert list(transitions) == ["test_contract.py"]
    unsigned = regression.policy_approved_paths(run_dir)
    assert unsigned == {"test_contract.py"}
    result = regression.compare(
        package, baseline, approved_transitions=transitions, policy_approved=unsigned
    )
    assert result.passed is True
    assert "0 exact signed semantic transition(s) approved" in result.details
    assert "1 auto-approved by policy (no human signature): test_contract.py" in result.details


def test_policy_does_not_waive_an_unreviewed_or_failing_test(
    policy_file: Path, tmp_path: Path,
) -> None:
    _switch_on(policy_file)
    # No recorded review of the changed test file: the policy has nothing to approve.
    package, baseline, run_dir = _evolved_test(tmp_path, record_review=False)
    transitions = _transitions(package, baseline, run_dir)
    assert transitions == {}
    assert regression.compare(package, baseline, approved_transitions=transitions).passed is False

    # A reviewed change that makes a baseline test fail is still a regression. (The new body
    # has a different byte length: a same-size rewrite within one second can run from a stale
    # .pyc on a filesystem with coarse mtimes, which made this flaky in CI.)
    (package / "test_contract.py").write_text(
        "def test_contract():\n    assert 3 + 3 == 7  # wrong on purpose\n", encoding="utf-8"
    )
    regression.create_semantic_review_challenge(
        run_dir, run_id="run", path="test_contract.py",
        before_sha256=baseline["test_file_hashes"]["test_contract.py"],
        after_sha256=hashlib.sha256((package / "test_contract.py").read_bytes()).hexdigest(),
        dispatch_sha256="a" * 64, report_sha256="b" * 64,
    )
    transitions = _transitions(package, baseline, run_dir)
    result = regression.compare(package, baseline, approved_transitions=transitions)
    assert result.passed is False and "no longer passing" in result.details


# --------------------------------------------------------------------------- run gates


@pytest.mark.parametrize(("kind", "step"), [
    (GateKind.MERGE, "ship"),
    (GateKind.DEPLOY, "ship"),
    (GateKind.DEPS, "ship"),
    (GateKind.SECRETS, "ship"),
    (GateKind.MIGRATION, "ship"),
    (GateKind.MERGE, None),
    (GateKind.DEPLOY, None),
    (GateKind.DEPS, None),
    (GateKind.SECRETS, None),
    (GateKind.MIGRATION, None),
    (GateKind.DEPS, "implement"),
])
def test_every_run_gate_always_needs_a_signature(
    policy_file: Path, tmp_path: Path, kind: GateKind, step: str | None,
) -> None:
    paths = _run(tmp_path)
    _switch_on(policy_file)
    opened = gates.gate_open(
        repo_root=tmp_path, run_id="g", kind=kind, step_id=step, action="publish"
    )
    gate_id = opened["gate"]["gate_id"]
    gates_dir = paths.run_dir / "gates"

    status = gates.gate_status(repo_root=tmp_path, run_id="g")
    assert status["blocked"] is True and status["pending"] == [gate_id]
    assert not (gates_dir / f"{gate_id}.decision.json").exists()

    # A hand-written policy decision for this gate is inert too.
    forged = _forge_policy_decision(gates_dir, gate_id)
    status = gates.gate_status(repo_root=tmp_path, run_id="g")
    assert status["blocked"] is True and status["pending"] == [gate_id]

    # Even with the gate record and its receipt forged as approved, the protected action
    # re-verifies the decision and refuses the unsigned one.
    record = json.loads((gates_dir / f"{gate_id}.json").read_text(encoding="utf-8"))
    record.update(status="approved", decision="approve", decided_by="policy:auto")
    (gates_dir / f"{gate_id}.json").write_text(json.dumps(record), encoding="utf-8")
    (gates_dir / f"{gate_id}.pending").unlink()
    token = json.loads(forged.read_text(encoding="utf-8"))
    (gates_dir / f"{gate_id}.decision.consumed").write_text(
        hashlib.sha256(approvals.canonical_bytes(token)).hexdigest() + "\n", encoding="utf-8"
    )
    ran: list[str] = []
    with pytest.raises(gates.GateError, match="human signature"):
        gates.execute_protected_action(
            repo_root=tmp_path, run_id="g", gate_id=gate_id, callback=lambda: ran.append("x")
        )
    assert ran == []


def test_policy_token_cannot_be_submitted_through_gate_approve(
    policy_file: Path, tmp_path: Path,
) -> None:
    paths = _run(tmp_path)
    _switch_on(policy_file)
    gates.gate_open(
        repo_root=tmp_path, run_id="g", kind=GateKind.MERGE, step_id="ship", action="merge"
    )
    forged = _forge_policy_decision(paths.run_dir / "gates", "merge-ship")
    outside = tmp_path / "forged-token.json"
    outside.write_text(forged.read_text(encoding="utf-8"), encoding="utf-8")
    forged.unlink()
    with pytest.raises(gates.GateError, match="human signature"):
        gates.gate_approve(
            repo_root=tmp_path, run_id="g", gate_id="merge-ship",
            decision=gates.GateDecision.APPROVE, token_file=outside,
        )


def test_ship_gate_lists_policy_approvals_even_when_granted_after_it_opened(
    policy_file: Path, tmp_path: Path,
) -> None:
    _live_demo_gap(tmp_path)
    _switch_on(policy_file)
    first = gates.gate_open(
        repo_root=tmp_path, run_id="v", kind=GateKind.MERGE, step_id="ship", action="merge"
    )
    assert first["gate"]["warnings"] == []

    verify_checks.record_override(
        repo_root=tmp_path, run_id="v", check="live_demo", by="agent", reason="known gap"
    )
    again = gates.gate_open(
        repo_root=tmp_path, run_id="v", kind=GateKind.MERGE, step_id="ship", action="merge"
    )
    expected = ("POLICY AUTO-APPROVAL (no human signature): accept verify finding live_demo "
                "[verify-override-live_demo]")
    assert again["banner"] == [expected]
    assert gates.load_gate(tmp_path, "v", "merge-ship").warnings == [expected]


# --------------------------------------------------------------------------- decision binding


def _pending_override_challenge(repo: Path) -> tuple[Path, str]:
    """A quality-override challenge with no decision yet (the switch was off when requested)."""
    paths = _adapter_reviewed_run(repo)
    with pytest.raises(quality.QualityError, match="must sign"):
        _override(repo)
    return paths.run_dir / "gates", "independent-override-0"


@pytest.mark.parametrize("tamper", [
    {"challenge_sha256": "0" * 64},
    {"run_id": "another-run"},
    {"policy_sha256": "0" * 64},
    {"human_signature": True},
    {"approved_by": "Alex Owner"},
    {"decision": "reject"},
])
def test_policy_decision_must_match_the_challenge_and_the_active_policy(
    policy_file: Path, tmp_path: Path, tamper: dict,
) -> None:
    gates_dir, gate_id = _pending_override_challenge(tmp_path)
    _switch_on(policy_file)
    token_file = _forge_policy_decision(gates_dir, gate_id)
    token = json.loads(token_file.read_text(encoding="utf-8"))
    token["payload"].update(tamper)
    token_file.write_text(json.dumps(token), encoding="utf-8")

    with pytest.raises(quality.QualityError, match="not authorized"):
        _override(tmp_path)
    assert not approvals.consumed_file(gates_dir, gate_id).exists()


def test_stale_policy_decision_for_a_renewed_challenge_is_replaced(
    policy_file: Path, tmp_path: Path,
) -> None:
    """A policy decision bound to an older nonce is the engine's own artifact: it is rewritten,
    never left to block the point (a signed or consumed decision is never touched)."""
    gates_dir, gate_id = _pending_override_challenge(tmp_path)
    _switch_on(policy_file)
    token_file = _forge_policy_decision(gates_dir, gate_id)
    token = json.loads(token_file.read_text(encoding="utf-8"))
    token["payload"]["nonce"] = "older-nonce"
    token_file.write_text(json.dumps(token), encoding="utf-8")
    assert approvals.policy_approve(gates_dir, gate_id, eligible=True) is True
    fresh = json.loads(token_file.read_text(encoding="utf-8"))
    assert fresh["payload"]["nonce"] == approvals.load_challenge(gates_dir, gate_id)["nonce"]


def test_policy_decision_carrying_a_signature_field_is_refused(
    policy_file: Path, tmp_path: Path,
) -> None:
    gates_dir, gate_id = _pending_override_challenge(tmp_path)
    _switch_on(policy_file)
    token_file = _forge_policy_decision(gates_dir, gate_id)
    token = json.loads(token_file.read_text(encoding="utf-8"))
    token["signature"] = "AAAA"
    token_file.write_text(json.dumps(token), encoding="utf-8")
    with pytest.raises(quality.QualityError, match="not authorized"):
        _override(tmp_path)


def test_policy_decision_is_voided_when_the_policy_file_changes(
    policy_file: Path, tmp_path: Path,
) -> None:
    paths = _adapter_reviewed_run(tmp_path)
    _switch_on(policy_file)
    _override(tmp_path)
    policy_file.write_text(json.dumps({**VALID, "since": "2026-10-01"}), encoding="utf-8")
    assert approvals.active_policy() is not None
    state = engine.load_state(paths)
    assert quality.provenance_report(
        repo_root=tmp_path, run_id="q", state=state
    )["override"] is None


def test_policy_never_replaces_an_existing_or_consumed_decision(
    policy_file: Path, tmp_path: Path,
) -> None:
    gates_dir, gate_id = _pending_override_challenge(tmp_path)
    _switch_on(policy_file)
    decision = gates_dir / f"{gate_id}.decision.json"

    signed = json.dumps({"payload": {"schema_version": "wilfred-signed-decision/v1"},
                         "signature": "AAAA"})
    decision.write_text(signed, encoding="utf-8")
    assert approvals.policy_approve(gates_dir, gate_id, eligible=True) is False
    assert decision.read_text(encoding="utf-8") == signed

    decision.unlink()
    approvals.consumed_file(gates_dir, gate_id).write_text("spent\n", encoding="utf-8")
    assert approvals.policy_approve(gates_dir, gate_id, eligible=True) is False
    assert not decision.exists()


def test_ineligible_call_or_purpose_writes_nothing(policy_file: Path, tmp_path: Path) -> None:
    gates_dir, gate_id = _pending_override_challenge(tmp_path)
    _switch_on(policy_file)
    assert approvals.policy_approve(gates_dir, gate_id, eligible=False) is False

    paths = _run(tmp_path / "other")
    gates.gate_open(repo_root=tmp_path / "other", run_id="g", kind=GateKind.DEPS, action="bump")
    assert approvals.policy_approve(paths.run_dir / "gates", "deps-adhoc", eligible=True) is False
    assert not (paths.run_dir / "gates" / "deps-adhoc.decision.json").exists()


def test_switch_off_leaves_no_new_files(policy_file: Path, tmp_path: Path) -> None:
    gates_dir, gate_id = _pending_override_challenge(tmp_path)
    before = sorted(p.name for p in gates_dir.iterdir())
    assert approvals.policy_approve(gates_dir, gate_id, eligible=True) is False
    assert sorted(p.name for p in gates_dir.iterdir()) == before


def test_oversized_or_deeply_nested_policy_file_is_fail_closed(policy_file: Path) -> None:
    policy_file.write_text("[" * 200_000, encoding="utf-8")
    assert approvals.active_policy() is None
    policy_file.write_text(json.dumps({**VALID, "set_by": "x" * 5000}), encoding="utf-8")
    assert approvals.active_policy() is None


def test_verify_override_label_comes_from_the_decision_not_the_editable_file(
    policy_file: Path, tmp_path: Path,
) -> None:
    paths = _live_demo_gap(tmp_path)
    sha = _switch_on(policy_file)
    verify_checks.record_override(
        repo_root=tmp_path, run_id="v", check="live_demo", by="agent", reason="known gap"
    )
    overrides_file = paths.run_dir / verify_checks.VERIFY_OVERRIDES_FILE
    entries = json.loads(overrides_file.read_text(encoding="utf-8"))
    for key in ("approved_by", "human_signature", "policy_sha256"):
        entries[0].pop(key)
    entries[0]["by"] = "Alex Owner (signed)"
    overrides_file.write_text(json.dumps(entries), encoding="utf-8")

    out = engine.complete_step(repo_root=tmp_path, run_id="v", step_id="verify")

    shown = out["verify_checks"]["findings"][0]["override"]
    assert shown["by"] == "policy:auto" and shown["approved_by"] == "policy:auto"
    assert shown["human_signature"] is False and shown["policy_sha256"] == sha


def test_quality_override_run_event_is_labelled_as_policy(
    policy_file: Path, tmp_path: Path,
) -> None:
    paths = _adapter_reviewed_run(tmp_path)
    _switch_on(policy_file)
    _override(tmp_path)
    events = (paths.run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "POLICY AUTO-APPROVAL (no human signature) of non-independent review" in events
    assert "SIGNED HUMAN OVERRIDE" not in events


def test_engine_regression_step_reports_policy_approved_test_change(
    policy_file: Path, improve_repo: Path,  # noqa: F811
) -> None:
    if not _toolchain_ready():
        pytest.skip("toolchain not on PATH")
    pkg = improve_repo / "pkg"
    paths = _improve_run_with_baseline(improve_repo, pkg)
    baseline = regression.load_baseline(paths.artifact(regression.BASELINE_FILE))
    assert baseline is not None
    test_file = pkg / "test_calc.py"
    test_file.write_text(
        "from calc import double\n\n\ndef test_double():\n    assert double(4) == 8\n",
        encoding="utf-8",
    )
    regression.create_semantic_review_challenge(
        paths.run_dir, run_id="i", path="test_calc.py",
        before_sha256=baseline["test_file_hashes"]["test_calc.py"],
        after_sha256=hashlib.sha256(test_file.read_bytes()).hexdigest(),
        dispatch_sha256="a" * 64, report_sha256="b" * 64,
    )
    with pytest.raises(engine.GateFailure):  # switch off: the changed test blocks
        engine.evaluate(repo_root=improve_repo, run_id="i", target=pkg, step_id="regression")

    _switch_on(policy_file)
    out = engine.evaluate(repo_root=improve_repo, run_id="i", target=pkg, step_id="regression")

    assert out["passed"] is True
    report = json.loads(paths.artifact("verification.json").read_text(encoding="utf-8"))
    details = next(c for c in report["checks"] if c["name"] == "regression-baseline")["details"]
    assert "0 exact signed semantic transition(s) approved" in details
    assert "1 auto-approved by policy (no human signature): test_calc.py" in details


# --------------------------------------------------------------------------- always signed


def test_promote_always_needs_a_signature(policy_file: Path, hw_history: Path) -> None:
    _switch_on(policy_file)
    learning.learn(repo_root=hw_history)
    pid = learning.list_proposals(hw_history)[0].proposal_id
    learning.open_review_gate(repo_root=hw_history, proposal_id=pid)

    assert learning.promote(repo_root=hw_history, proposal_id=pid)["promoted"] is False

    gdir = hw_history / ".hw" / "proposals" / "gates"
    _forge_policy_decision(gdir, pid)
    assert learning.promote(repo_root=hw_history, proposal_id=pid)["promoted"] is False
    proposal = learning.load_proposal(hw_history, pid)
    assert proposal is not None and proposal.status is ProposalStatus.UNDER_REVIEW


def test_policy_never_labels_a_review_independent(policy_file: Path, tmp_path: Path) -> None:
    _run_at_quality(tmp_path, threshold=4)
    _switch_on(policy_file)
    quality.critique_plan(repo_root=tmp_path, run_id="q")
    inp = tmp_path / ".hw/q/quality/iter-0/critics/tests.json"
    inp.parent.mkdir(parents=True, exist_ok=True)
    inp.write_text(json.dumps({"critic": "c", "lens": "tests", "findings": []}))
    kwargs = {"repo_root": tmp_path, "run_id": "q", "packet_id": "critic-0-tests",
              "result_file": str(inp), "author": "independent"}

    first = quality.record_critique(**kwargs)
    assert first["author"] == "adapter"

    challenge = Path(first["attestation_challenge"])
    forged = _forge_policy_decision(
        challenge.parent, challenge.name.removesuffix(".challenge.json")
    )
    with pytest.raises(quality.QualityError, match="human signature"):
        quality.record_critique(**kwargs, attestation_file=str(forged))
