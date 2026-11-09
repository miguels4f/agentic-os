"""Phase 5 — the learning analyzer + the review-gated IMPROVE-feeds-BUILD autonomy loop.

The headline invariant (spec §2, constraint #2): the factory does not silently rewrite its own
rules. A proposal becomes an active IMPROVE run ONLY after a signed human decision verifies.
"""

from __future__ import annotations

import ast
import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyperwilfred_core import approvals, gates, learning
from hyperwilfred_core.engine import RunPaths, load_state
from hyperwilfred_core.schemas import BuildMode, ProposalStatus

SRC = Path(__file__).resolve().parent.parent / "src" / "hyperwilfred_core"


@pytest.fixture
def approval_key(tmp_path: Path, monkeypatch):
    private = Ed25519PrivateKey.generate()
    public = tmp_path / "learning-public.pem"
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    monkeypatch.setattr(approvals, "DEFAULT_PUBLIC_KEY_PATH", public)
    return private, public


def _decide(repo: Path, proposal_id: str, key, decision: str) -> None:
    private, public = key
    gdir = repo / ".hw" / "proposals" / "gates"
    challenge = approvals.load_challenge(gdir, proposal_id)
    payload = approvals.build_decision_payload(
        challenge,
        decision=decision,
        decided_by="fixture-human",
        note="fixture",
        public_key_path=public,
    )
    token = {
        "payload": payload,
        "signature": base64.b64encode(private.sign(approvals.canonical_bytes(payload))).decode(),
    }
    gates.decision_file_in(gdir, proposal_id).write_text(json.dumps(token), encoding="utf-8")


# --------------------------------------------------------------------------- the analyzer


def test_analyze_emits_review_only_improve_proposals(hw_history: Path) -> None:
    pack = learning.analyze(repo_root=hw_history)
    assert pack.proposals, "expected proposals from a history with anomalies"
    for p in pack.proposals:
        # review-only: the non-negotiable flags
        assert p.requires_human_review is True
        assert p.auto_apply is False
        assert p.status is ProposalStatus.PROPOSED
        # materialized as an IMPROVE objective (acceptance criteria + factory-targeted)
        assert p.objective.acceptance_criteria
        assert p.objective.id.startswith("improve-")


def test_analyzer_is_deterministic(hw_history: Path) -> None:
    a = learning.analyze(repo_root=hw_history)
    b = learning.analyze(repo_root=hw_history)
    # stable identity + content (timestamps excluded)
    assert [p.proposal_id for p in a.proposals] == [p.proposal_id for p in b.proposals]
    assert [p.source_run_ids for p in a.proposals] == [p.source_run_ids for p in b.proposals]
    assert [p.suggested_change for p in a.proposals] == [p.suggested_change for p in b.proposals]


def test_learn_persists_pack_and_items(hw_history: Path) -> None:
    out = learning.learn(repo_root=hw_history)
    assert out["proposals"] >= 1
    assert Path(out["pack_json"]).exists()
    assert Path(out["pack_md"]).exists()
    items = learning.list_proposals(hw_history)
    assert {p.proposal_id for p in items} == set(out["proposal_ids"])


def test_empty_history_no_proposals(tmp_path: Path) -> None:
    out = learning.learn(repo_root=tmp_path / "empty")
    assert out["proposals"] == 0


# --------------------------------------------------------------------------- the autonomy invariant


def test_unapproved_proposal_cannot_become_an_active_run(hw_history: Path) -> None:
    """THE invariant: no signed approval → promote REFUSES, no IMPROVE run is created."""
    learning.learn(repo_root=hw_history)
    pid = learning.list_proposals(hw_history)[0].proposal_id

    # promote with NO gate, NO approval → refused, inert.
    out = learning.promote(repo_root=hw_history, proposal_id=pid)
    assert out["promoted"] is False
    assert not RunPaths(hw_history, f"imp-{pid}").state.exists()

    # open the review gate (still no human file) → still refused.
    learning.open_review_gate(repo_root=hw_history, proposal_id=pid)
    out = learning.promote(repo_root=hw_history, proposal_id=pid)
    assert out["promoted"] is False
    assert not RunPaths(hw_history, f"imp-{pid}").state.exists()

    proposal = learning.load_proposal(hw_history, pid)
    assert proposal is not None and proposal.status is ProposalStatus.UNDER_REVIEW


def test_approved_proposal_promotes_to_an_improve_run(hw_history: Path, approval_key) -> None:
    learning.learn(repo_root=hw_history)
    pid = learning.list_proposals(hw_history)[0].proposal_id
    learning.open_review_gate(repo_root=hw_history, proposal_id=pid)

    _decide(hw_history, pid, approval_key, "approve")

    out = learning.promote(repo_root=hw_history, proposal_id=pid)
    assert out["promoted"] is True
    run_id = out["run_id"]
    state = load_state(RunPaths(hw_history, run_id))
    assert state.mode is BuildMode.IMPROVE
    assert state.steps[0].id == "comprehend"  # the EXISTING IMPROVE pipeline
    proposal = learning.load_proposal(hw_history, pid)
    assert proposal is not None and proposal.status is ProposalStatus.PROMOTED


def test_gate_approve_cli_path_also_promotes(hw_history: Path) -> None:
    """Legacy ID retained; unsigned self-declared JSON no longer promotes."""
    learning.learn(repo_root=hw_history)
    pid = learning.list_proposals(hw_history)[0].proposal_id
    learning.open_review_gate(repo_root=hw_history, proposal_id=pid)

    gdir = hw_history / ".hw" / "proposals" / "gates"
    gates.decision_file_in(gdir, pid).write_text(
        json.dumps({"decision": "approve", "decided_by": "owner", "note": "looks right"}),
        encoding="utf-8",
    )
    out = learning.promote(repo_root=hw_history, proposal_id=pid)
    assert out["promoted"] is False


def test_human_reject_keeps_proposal_inert(hw_history: Path, approval_key) -> None:
    learning.learn(repo_root=hw_history)
    pid = learning.list_proposals(hw_history)[0].proposal_id
    learning.open_review_gate(repo_root=hw_history, proposal_id=pid)

    _decide(hw_history, pid, approval_key, "reject")

    out = learning.promote(repo_root=hw_history, proposal_id=pid)
    assert out["promoted"] is False
    proposal = learning.load_proposal(hw_history, pid)
    assert proposal is not None and proposal.status is ProposalStatus.REJECTED
    assert not RunPaths(hw_history, f"imp-{pid}").state.exists()


def test_relearn_does_not_resurrect_decided_proposals(hw_history: Path, approval_key) -> None:
    learning.learn(repo_root=hw_history)
    pid = learning.list_proposals(hw_history)[0].proposal_id
    learning.open_review_gate(repo_root=hw_history, proposal_id=pid)
    _decide(hw_history, pid, approval_key, "reject")
    learning.review_status(repo_root=hw_history, proposal_id=pid)  # resolve → REJECTED

    learning.learn(repo_root=hw_history)  # re-run the analyzer
    proposal = learning.load_proposal(hw_history, pid)
    assert proposal is not None and proposal.status is ProposalStatus.REJECTED, (
        "re-running learn must NOT resurrect a human-rejected proposal"
    )


def test_promote_unknown_proposal_errors(hw_history: Path) -> None:
    import pytest

    with pytest.raises(learning.LearningError):
        learning.promote(repo_root=hw_history, proposal_id="learn-does-not-exist")


# --------------------------------------------------------------------------- invariants


def test_learning_is_llm_free_and_spawn_free() -> None:
    """AST import check — the analyzer computes deterministically, imports no LLM SDK, no spawn."""
    mods: set[str] = set()
    for node in ast.walk(ast.parse((SRC / "learning.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    forbidden = ("anthropic", "openai", "subprocess", "common_py", "urllib", "http", "requests")
    offenders = [m for m in mods if m.startswith(forbidden)]
    assert offenders == [], f"learning analyzer imports forbidden modules: {offenders}"
