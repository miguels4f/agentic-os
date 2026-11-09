"""Disk-file HITL gates for irreversible actions — the third enforcement point.

Proves: gate-open writes a .pending + blocks the run; the engine NEVER auto-approves; a bare
touch OR `hw gate-approve` (a human writing a decision file) is the ONLY thing that unblocks; a
reject halts; decisions persist to the run store; and the protected-paths classifier routes the
gate kind.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyperwilfred_core import approvals, engine, gates, quality
from hyperwilfred_core.pipeline import steps_for
from hyperwilfred_core.run_store import RunStore
from hyperwilfred_core.schemas import (
    BuildMode,
    BuildObjective,
    GateDecision,
    GateKind,
    RunStatus,
    Stack,
    StepKind,
    StepStatus,
)


def _run(repo: Path, run_id: str = "g") -> engine.RunPaths:
    engine.plan(
        repo_root=repo,
        objective=BuildObjective(id="o", title="t", objective="build"),
        run_id=run_id,
        product_dir="product",
    )
    return engine.RunPaths(repo, run_id)


@pytest.fixture
def approval_key(tmp_path: Path, monkeypatch):
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "fixture-approval-public.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    monkeypatch.setattr(approvals, "DEFAULT_PUBLIC_KEY_PATH", public_path)
    return private, public_path


def _token(repo: Path, opened: dict, private, public_path: Path, decision: str, note: str = ""):
    payload = approvals.build_decision_payload(
        opened["challenge"],
        decision=decision,
        decided_by="fixture-human",
        note=note,
        public_key_path=public_path,
    )
    token = {
        "payload": payload,
        "signature": base64.b64encode(private.sign(approvals.canonical_bytes(payload))).decode(),
    }
    path = repo / "signed-decision-fixture.json"
    path.write_text(json.dumps(token), encoding="utf-8")
    return path


def test_gate_open_blocks_the_run(tmp_path: Path) -> None:
    paths = _run(tmp_path)
    out = gates.gate_open(
        repo_root=tmp_path, run_id="g", kind=GateKind.MERGE, step_id="ship", action="open a PR"
    )
    assert out["gate"]["status"] == "pending"
    gate_id = out["gate"]["gate_id"]
    assert (tmp_path / ".hw" / "g" / "gates" / f"{gate_id}.pending").exists()
    assert engine.load_state(paths).status is RunStatus.AWAITING_HUMAN


@pytest.mark.parametrize("initial", [StepStatus.PENDING, StepStatus.RUNNING, StepStatus.FAILED])
@pytest.mark.parametrize(("mode", "step_id", "recorded"), [
    (mode, step.id, step.kind) for mode in BuildMode for step in steps_for(mode)
] + [(BuildMode.BUILD, "evaluate", StepKind.HUMAN_GATE),
     (BuildMode.IMPROVE, "regression", StepKind.HUMAN_GATE),
     (BuildMode.BUILD, "ship", StepKind.SINGLE)])
def test_approval_preserves_step_completion_owner(
    tmp_path, approval_key, monkeypatch, mode, step_id, recorded, initial,
):
    product = tmp_path / "product"
    product.mkdir()
    (product / "package.json").write_text(json.dumps({"scripts": {"test": "exit 1"}}))
    engine.plan(repo_root=tmp_path, run_id="g", mode=mode,
                objective=BuildObjective(id="o", title="t", objective="guard", stack=Stack.NODE))
    paths = engine.RunPaths(tmp_path, "g")
    state = engine.load_state(paths)
    next(s for s in state.steps if s.id == step_id).kind = recorded
    state.step_status[step_id] = initial
    engine.save_state(paths, state)
    monkeypatch.setattr(engine, "run_all", lambda *a, **kw: pytest.fail("approval ran evaluator"))
    gate = gates.gate_open(repo_root=tmp_path, run_id="g", kind=GateKind.DEPS, step_id=step_id)
    result = gates.gate_approve(
        repo_root=tmp_path, run_id="g", gate_id=gate["gate"]["gate_id"],
        decision=GateDecision.APPROVE, token_file=_token(tmp_path, gate, *approval_key, "approve"),
    )
    assert result["gates"][0]["decision"] == "approve"
    human = recorded is StepKind.HUMAN_GATE and any(
        s.id == step_id and s.kind is StepKind.HUMAN_GATE for s in steps_for(mode))
    expected = StepStatus.DONE if human and initial is not StepStatus.FAILED else initial
    assert engine.load_state(paths).step_status[step_id] is expected
    assert not paths.artifact("verification.json").exists()
    assert not quality.quality_status(repo_root=tmp_path, run_id="g")["deterministic_green"]


def test_engine_never_auto_approves(tmp_path: Path) -> None:
    _run(tmp_path)
    out = gates.gate_open(repo_root=tmp_path, run_id="g", kind=GateKind.DEPLOY, step_id="ship")
    gate_id = out["gate"]["gate_id"]
    # poll status MANY times with NO human file present — it must stay pending every time.
    for _ in range(5):
        st = gates.gate_status(repo_root=tmp_path, run_id="g")
        assert st["blocked"] is True
        assert gate_id in st["pending"]
    assert gates.load_gate(tmp_path, "g", gate_id).status.value == "pending"


def test_bare_touch_approval_unblocks(tmp_path: Path, approval_key) -> None:
    """Legacy ID retained; corrected contract proves a bare marker cannot unblock."""
    paths = _run(tmp_path)
    out = gates.gate_open(repo_root=tmp_path, run_id="g", kind=GateKind.MERGE, step_id="ship")
    gate_id = out["gate"]["gate_id"]
    # A bare touch only claims approval; it does not authenticate a human.
    (tmp_path / ".hw" / "g" / "gates" / f"{gate_id}.approved").write_text("", encoding="utf-8")
    st = gates.gate_status(repo_root=tmp_path, run_id="g")
    assert st["blocked"] is True
    assert gates.pending_marker_in(tmp_path / ".hw/g/gates", gate_id).exists()
    assert engine.load_state(paths).step_status["ship"] is not StepStatus.DONE


def test_gate_approve_cli_path_unblocks_and_persists(tmp_path: Path, approval_key) -> None:
    """Legacy ID retained; gate approval now requires a signed token."""
    paths = _run(tmp_path)
    out = gates.gate_open(repo_root=tmp_path, run_id="g", kind=GateKind.MERGE, step_id="ship")
    gate_id = out["gate"]["gate_id"]
    res = gates.gate_approve(
        repo_root=tmp_path,
        run_id="g",
        gate_id=gate_id,
        decision=GateDecision.APPROVE,
        decided_by="owner",
        note="lgtm",
        token_file=_token(tmp_path, out, *approval_key, "approve", "lgtm"),
    )
    assert res["blocked"] is False
    gate = gates.load_gate(tmp_path, "g", gate_id)
    assert gate.status.value == "approved" and gate.decided_by == "fixture-human"
    assert gate.note == "lgtm"
    assert not gates.pending_marker_in(tmp_path / ".hw/g/gates", gate_id).exists()
    assert engine.load_state(paths).step_status["ship"] is StepStatus.DONE
    # decision persisted to the run store timeline (not in-memory only)
    events = RunStore(tmp_path / ".hw" / "g").read_events()
    assert any("APPROVED" in e.message for e in events)


def test_reject_halts_the_action(tmp_path: Path, approval_key) -> None:
    paths = _run(tmp_path)
    out = gates.gate_open(repo_root=tmp_path, run_id="g", kind=GateKind.MERGE, step_id="ship")
    gate_id = out["gate"]["gate_id"]
    res = gates.gate_approve(
        repo_root=tmp_path,
        run_id="g",
        gate_id=gate_id,
        decision=GateDecision.REJECT,
        decided_by="owner",
        note="not yet",
        token_file=_token(tmp_path, out, *approval_key, "reject", "not yet"),
    )
    assert res["blocked"] is True  # a recorded rejection still blocks the whole run
    state = engine.load_state(paths)
    assert state.step_status["ship"] is StepStatus.FAILED  # …but the action did NOT proceed
    assert state.status is RunStatus.FAILED
    assert gates.load_gate(tmp_path, "g", gate_id).status.value == "rejected"


def test_reject_run_cannot_be_resurrected_by_next_step(tmp_path: Path, approval_key) -> None:
    """Regression (verifier #3): after a human REJECT, the normal driver loop (`hw next-step`) must
    NOT flip the failed run back to in_progress and hand out the next step — a rejected run halts.
    """
    _run(tmp_path)
    out = gates.gate_open(repo_root=tmp_path, run_id="g", kind=GateKind.MERGE, step_id="ship")
    gates.gate_approve(
        repo_root=tmp_path,
        run_id="g",
        gate_id=out["gate"]["gate_id"],
        decision=GateDecision.REJECT,
        decided_by="owner",
        note="no",
        token_file=_token(tmp_path, out, *approval_key, "reject", "no"),
    )
    nxt = engine.next_step(repo_root=tmp_path, run_id="g")
    assert nxt.get("halted") is True
    assert "step" not in nxt  # it did NOT resurrect the pipeline and hand out 'frame'
    assert engine.load_state(engine.RunPaths(tmp_path, "g")).status is RunStatus.FAILED


def test_modify_scope_keeps_blocked(tmp_path: Path, approval_key) -> None:
    paths = _run(tmp_path)
    out = gates.gate_open(repo_root=tmp_path, run_id="g", kind=GateKind.DEPLOY, step_id="ship")
    gate_id = out["gate"]["gate_id"]
    result = gates.gate_approve(
        repo_root=tmp_path,
        run_id="g",
        gate_id=gate_id,
        decision=GateDecision.MODIFY_SCOPE,
        decided_by="owner",
        note="narrow it",
        token_file=_token(tmp_path, out, *approval_key, "modify_scope", "narrow it"),
    )
    assert result["blocked"] is True  # modify-scope is an unresolved run-wide hold
    state = engine.load_state(paths)
    assert state.status is RunStatus.AWAITING_HUMAN  # still blocked
    assert state.step_status["ship"] is not StepStatus.DONE


def test_gate_kind_derived_from_protected_paths(tmp_path: Path) -> None:
    _run(tmp_path)
    # no --kind: the engine classifies the sensitive path and routes the gate
    out = gates.gate_open(
        repo_root=tmp_path, run_id="g", step_id="ship", paths=["db/migrations/0003.sql"]
    )
    assert out["gate"]["kind"] == "migration"
    assert "migrations" in out["gate"]["categories"]


@pytest.mark.parametrize("step_id", [None, "ship"])
@pytest.mark.parametrize("decisions", [
    ("approve", "approve"), ("approve", "reject"), ("reject", "approve"),
    ("approve", "modify_scope"), ("modify_scope", "approve"),
])
def test_multiple_gate_decisions_preserve_global_holds(tmp_path, approval_key, step_id, decisions):
    paths = _run(tmp_path)
    opened = [gates.gate_open(repo_root=tmp_path, run_id="g", kind=kind, step_id=step_id)
              for kind in (GateKind.MERGE, GateKind.DEPLOY)]
    effects = []
    for index, (gate, decision) in enumerate(zip(opened, decisions, strict=True)):
        gate_id = gate["gate"]["gate_id"]
        result = gates.gate_approve(
            repo_root=tmp_path, run_id="g", gate_id=gate_id, decision=GateDecision(decision),
            token_file=_token(tmp_path, gate, *approval_key, decision),
        )
        blocked = index == 0 or decisions != ("approve", "approve")
        assert result["blocked"] is blocked
        assert gates.gate_status(repo_root=tmp_path, run_id="g")["blocked"] is blocked
        assert result["gates"][0]["decision"] == decision
        state = engine.load_state(paths)
        if "reject" in decisions[:index + 1]:
            assert state.status is RunStatus.FAILED
        if step_id:
            assert (state.step_status[step_id] is StepStatus.DONE) is (not blocked)
        if blocked:
            assert "step" not in engine.next_step(repo_root=tmp_path, run_id="g")
            with pytest.raises(gates.GateError, match="lacks valid"):
                gates.execute_protected_action(repo_root=tmp_path, run_id="g", gate_id=gate_id,
                                               callback=lambda: effects.append("effect"))
            assert effects == []
        else:
            assert state.status is RunStatus.IN_PROGRESS


def test_gate_open_refuses_unclassifiable_without_kind(tmp_path: Path) -> None:
    _run(tmp_path)
    with pytest.raises(gates.GateError):
        gates.gate_open(repo_root=tmp_path, run_id="g", step_id="ship", paths=["product/lib.py"])


def test_gate_open_is_idempotent(tmp_path: Path) -> None:
    _run(tmp_path)
    a = gates.gate_open(repo_root=tmp_path, run_id="g", kind=GateKind.MERGE, step_id="ship")
    b = gates.gate_open(repo_root=tmp_path, run_id="g", kind=GateKind.MERGE, step_id="ship")
    assert a["gate"]["gate_id"] == b["gate"]["gate_id"]
    assert b["already_open"] is True
    # exactly one gate json on disk
    gdir = tmp_path / ".hw" / "g" / "gates"
    assert (
        len(
            [
                p
                for p in gdir.glob("*.json")
                if not p.name.endswith((".decision.json", ".challenge.json"))
            ]
        )
        == 1
    )


@pytest.mark.parametrize("removed", [(), (".json",), (".json", ".pending"),
                                     (".json", ".challenge.json")])
def test_damaged_gate_records_fail_closed_with_valid_sibling(tmp_path, removed):
    paths = _run(tmp_path)
    opened = gates.gate_open(repo_root=tmp_path, run_id="g", kind=GateKind.DEPS)
    gates.gate_open(repo_root=tmp_path, run_id="g", kind=GateKind.DEPLOY)
    directory = paths.run_dir / "gates"
    gate_id = opened["gate"]["gate_id"]
    (directory / f"{gate_id}.json").write_text("{")
    for suffix in removed:
        (directory / f"{gate_id}{suffix}").unlink()
    with pytest.raises(gates.GateError, match="repair gate state"):
        gates.gate_status(repo_root=tmp_path, run_id="g", gate_id="deploy-adhoc")
    assert "step" not in engine.next_step(repo_root=tmp_path, run_id="g")


def test_classify_paths_helper(tmp_path: Path) -> None:
    res = gates.classify_paths([".env", "src/billing/charge.py", "product/ok.py"])
    assert res["requires_human_review"] is True
    assert res["irreversible"] is True
    assert "secrets" in res["categories"] and "payments" in res["categories"]
    assert res["recommended_kind"] in {"secrets", "deploy"}
