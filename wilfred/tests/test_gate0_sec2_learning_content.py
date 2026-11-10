"""A learning approval covers the proposal content shown to the signer."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyperwilfred_core import approvals, learning
from hyperwilfred_core.schemas import BuildObjective, LearningProposal, ProposalType


def _proposal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    private = Ed25519PrivateKey.generate()
    public = tmp_path / "approval-public.pem"
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    monkeypatch.setattr(approvals, "DEFAULT_PUBLIC_KEY_PATH", public)
    proposal = LearningProposal(
        proposal_id="learn-one",
        proposal_type=ProposalType.TEST_COVERAGE,
        title="Add coverage",
        rationale="Gap",
        suggested_change="Add tests",
        affected_area="tests",
        confidence=0.9,
        evidence_refs=["run:r1"],
        source_run_ids=["r1"],
        objective=BuildObjective(
            id="improve-one",
            title="Improve tests",
            objective="Add a safe test",
        ),
    )
    learning._item_path(repo, proposal.proposal_id).parent.mkdir(parents=True)
    learning._save_proposal(repo, proposal)
    return repo, private, public


def _sign(repo: Path, private: Ed25519PrivateKey, public: Path, challenge: dict) -> None:
    payload = approvals.build_decision_payload(
        challenge,
        decision="approve",
        decided_by="owner",
        note="approved",
        public_key_path=public,
    )
    token = {
        "payload": payload,
        "signature": base64.b64encode(private.sign(approvals.canonical_bytes(payload))).decode(),
    }
    (repo / ".hw/proposals/gates/learn-one.decision.json").write_text(json.dumps(token))


@pytest.mark.parametrize(
    "change,approve_first",
    [
        ("objective", False),
        ("suggested_change", False),
        ("objective", True),
        ("affected_area", True),
    ],
)
def test_changed_proposal_cannot_be_promoted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    approve_first: bool,
) -> None:
    repo, private, public = _proposal(tmp_path, monkeypatch)
    opened = learning.open_review_gate(repo_root=repo, proposal_id="learn-one")
    challenge = opened["challenge"]
    assert "Add a safe test" in json.dumps(challenge["evidence"])
    assert "Add tests" in json.dumps(challenge["evidence"])
    _sign(repo, private, public, challenge)
    if approve_first:
        assert learning.review_status(repo_root=repo, proposal_id="learn-one")["approved"]
    proposal = learning.load_proposal(repo, "learn-one")
    assert proposal is not None
    if change == "objective":
        proposal.objective.objective = "Rewrite safety rules"
    else:
        setattr(proposal, change, "Rewrite safety rules")
    learning._save_proposal(repo, proposal)
    try:
        outcome = learning.promote(repo_root=repo, proposal_id="learn-one")
    except learning.LearningError:
        return
    assert not outcome["promoted"]
