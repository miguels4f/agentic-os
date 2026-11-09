from __future__ import annotations

import base64
import json
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyperwilfred_core import approvals, engine, gates
from hyperwilfred_core.schemas import BuildObjective, GateDecision, GateKind


def _key(path: Path) -> tuple[Ed25519PrivateKey, Path]:
    path.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    public = path / "fixture-public.pem"
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return private, public


def _open(repo: Path) -> dict:
    engine.plan(
        repo_root=repo,
        run_id="secure-run",
        product_dir="product",
        objective=BuildObjective(id="objective", title="t", objective="build"),
    )
    return gates.gate_open(
        repo_root=repo,
        run_id="secure-run",
        kind=GateKind.DEPLOY,
        action="write harmless host marker",
        evidence=["review.json"],
    )


def _signed(
    path: Path, opened: dict, private: Ed25519PrivateKey, public: Path,
    decision: str = "approve",
) -> Path:
    payload = approvals.build_decision_payload(
        opened["challenge"],
        decision=decision,
        decided_by="fixture-reviewer",
        note="test fixture only",
        public_key_path=public,
    )
    token = {
        "payload": payload,
        "signature": base64.b64encode(private.sign(approvals.canonical_bytes(payload))).decode(),
    }
    path.write_text(json.dumps(token), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("signed", "requested"),
    [
        (GateDecision.APPROVE, GateDecision.REJECT),
        (GateDecision.REJECT, GateDecision.MODIFY_SCOPE),
        (GateDecision.MODIFY_SCOPE, GateDecision.APPROVE),
    ],
)
def test_requested_decision_must_match_signed_token_without_consuming(
    tmp_path: Path, signed: GateDecision, requested: GateDecision,
) -> None:
    private, public = _key(tmp_path)
    opened = _open(tmp_path)
    gate_id = opened["gate"]["gate_id"]
    token = _signed(tmp_path / "decision.json", opened, private, public, signed.value)
    gates_dir = tmp_path / ".hw/secure-run/gates"

    with pytest.raises(gates.GateError, match="does not match"):
        gates.gate_approve(
            repo_root=tmp_path, run_id="secure-run", gate_id=gate_id,
            decision=requested, token_file=token, public_key_path=public,
        )
    assert not approvals.consumed_file(gates_dir, gate_id).exists()
    assert gates.load_gate(tmp_path, "secure-run", gate_id).status.value == "pending"
    assert gates.pending_marker_in(gates_dir, gate_id).exists()

    gates.gate_approve(
        repo_root=tmp_path, run_id="secure-run", gate_id=gate_id,
        decision=signed, token_file=token, public_key_path=public,
    )
    gate = gates.load_gate(tmp_path, "secure-run", gate_id)
    assert gate is not None and gate.decision is signed
    assert approvals.consumed_file(gates_dir, gate_id).exists()


def test_self_declared_cli_identity_and_bare_marker_cannot_authorize(tmp_path: Path) -> None:
    opened = _open(tmp_path)
    gate_id = opened["gate"]["gate_id"]
    with pytest.raises(gates.GateError, match="cannot mint"):
        gates.gate_approve(
            repo_root=tmp_path,
            run_id="secure-run",
            gate_id=gate_id,
            decision=GateDecision.APPROVE,
            decided_by="automated-agent",
        )
    gates.approved_marker_in(tmp_path / ".hw/secure-run/gates", gate_id).touch()
    assert gates.gate_status(repo_root=tmp_path, run_id="secure-run")["blocked"] is True


def test_forged_replayed_or_action_mismatched_decision_is_blocked(tmp_path: Path) -> None:
    trusted_private, trusted_public = _key(tmp_path)
    opened = _open(tmp_path)
    gate_id = opened["gate"]["gate_id"]
    forged_private, forged_public = _key(tmp_path / "forged")
    forged = _signed(tmp_path / "forged.json", opened, forged_private, forged_public)
    with pytest.raises(gates.GateError, match="rejected"):
        gates.gate_approve(
            repo_root=tmp_path,
            run_id="secure-run",
            gate_id=gate_id,
            decision=GateDecision.APPROVE,
            token_file=forged,
            public_key_path=trusted_public,
        )

    valid = _signed(tmp_path / "valid.json", opened, trusted_private, trusted_public)
    gate = gates.load_gate(tmp_path, "secure-run", gate_id)
    assert gate is not None
    gate.action = "replacement action not reviewed"
    (tmp_path / f".hw/secure-run/gates/{gate_id}.json").write_text(
        gate.model_dump_json(), encoding="utf-8"
    )
    with pytest.raises(gates.GateError, match="rejected"):
        gates.gate_approve(
            repo_root=tmp_path,
            run_id="secure-run",
            gate_id=gate_id,
            decision=GateDecision.APPROVE,
            token_file=valid,
            public_key_path=trusted_public,
        )


def test_signed_authorization_executes_harmless_host_callback_once(tmp_path: Path) -> None:
    private, public = _key(tmp_path)
    opened = _open(tmp_path)
    gate_id = opened["gate"]["gate_id"]
    token = _signed(tmp_path / "valid.json", opened, private, public)
    gates.gate_approve(
        repo_root=tmp_path,
        run_id="secure-run",
        gate_id=gate_id,
        decision=GateDecision.APPROVE,
        token_file=token,
        public_key_path=public,
    )
    marker = tmp_path / "host-action-ran"
    gates.execute_protected_action(
        repo_root=tmp_path,
        run_id="secure-run",
        gate_id=gate_id,
        public_key_path=public,
        callback=lambda: marker.write_text("authorized", encoding="utf-8"),
    )
    assert marker.read_text(encoding="utf-8") == "authorized"
    gate = gates.load_gate(tmp_path, "secure-run", gate_id)
    assert gate is not None
    gate.evidence.append("unreviewed-replacement-evidence")
    (tmp_path / f".hw/secure-run/gates/{gate_id}.json").write_text(
        gate.model_dump_json(), encoding="utf-8"
    )
    replacement_marker = tmp_path / "replacement-action-ran"
    with pytest.raises(gates.GateError, match="authorization is stale"):
        gates.execute_protected_action(
            repo_root=tmp_path, run_id="secure-run", gate_id=gate_id,
            public_key_path=public,
            callback=lambda: replacement_marker.write_text("bad", encoding="utf-8"),
        )
    assert not replacement_marker.exists()
    with pytest.raises(approvals.ApprovalError, match="replay"):
        approvals.verify_and_consume(
            tmp_path / ".hw/secure-run/gates",
            gate_id,
            token,
            expected_run_id="secure-run",
            public_key_path=public,
            expected_action=opened["gate"]["action"],
            expected_evidence=[*opened["gate"]["evidence"], *opened["gate"]["paths"]],
        )


def test_failed_protected_callback_preserves_at_most_once_attempt(tmp_path: Path) -> None:
    private, public = _key(tmp_path)
    opened = _open(tmp_path)
    gate_id = opened["gate"]["gate_id"]
    token = _signed(tmp_path / "valid.json", opened, private, public)
    gates.gate_approve(
        repo_root=tmp_path, run_id="secure-run", gate_id=gate_id,
        decision=GateDecision.APPROVE, token_file=token, public_key_path=public,
    )
    effects = {"count": 0}

    def uncertain_callback() -> None:
        effects["count"] += 1
        raise RuntimeError("lost acknowledgement after side effect")

    with pytest.raises(gates.GateError, match="uncertain outcome"):
        gates.execute_protected_action(
            repo_root=tmp_path, run_id="secure-run", gate_id=gate_id,
            public_key_path=public, callback=uncertain_callback,
        )
    assert effects["count"] == 1
    with pytest.raises(gates.GateError, match="already executed"):
        gates.execute_protected_action(
            repo_root=tmp_path, run_id="secure-run", gate_id=gate_id,
            public_key_path=public, callback=uncertain_callback,
        )
    assert effects["count"] == 1


def test_expired_pending_gate_renews_and_rejects_old_nonce(tmp_path: Path, monkeypatch) -> None:
    private, public = _key(tmp_path)
    opened = _open(tmp_path)
    gate_id = opened["gate"]["gate_id"]
    directory = tmp_path / ".hw/secure-run/gates"
    old = _signed(directory / f"{gate_id}.decision.json", opened, private, public)
    old_bytes = old.read_bytes()
    later = approvals._now() + timedelta(minutes=16)
    monkeypatch.setattr(approvals, "_now", lambda: later)
    renewed = gates.gate_open(repo_root=tmp_path, run_id="secure-run", kind=GateKind.DEPLOY)
    assert renewed["challenge"]["nonce"] != opened["challenge"]["nonce"]
    assert renewed["challenge"]["action"] == opened["challenge"]["action"]
    archive = json.loads(next((directory / "history").glob("*.json")).read_text())
    assert json.loads(archive["challenge"]) == opened["challenge"]
    assert archive["decision"].encode() == old_bytes
    with pytest.raises(gates.GateError, match="does not match"):
        gates.gate_approve(repo_root=tmp_path, run_id="secure-run", gate_id=gate_id,
                           decision=GateDecision.APPROVE, token_file=old, public_key_path=public)
    marker = tmp_path / "effect"
    with pytest.raises(gates.GateError, match="lacks valid"):
        gates.execute_protected_action(repo_root=tmp_path, run_id="secure-run", gate_id=gate_id,
                                       callback=lambda: marker.touch(), public_key_path=public)
    assert not marker.exists()
    fresh = _signed(tmp_path / "fresh.json", renewed, private, public)
    gates.gate_approve(repo_root=tmp_path, run_id="secure-run", gate_id=gate_id,
                       decision=GateDecision.APPROVE, token_file=fresh, public_key_path=public)
    gates.execute_protected_action(repo_root=tmp_path, run_id="secure-run", gate_id=gate_id,
                                   callback=lambda: marker.touch(), public_key_path=public)
    assert marker.exists()


@pytest.mark.parametrize("outcome", ["consumed", "completed", "uncertain"])
def test_reopening_never_resets_consumed_or_attempted_gate(
    tmp_path: Path, monkeypatch, outcome: str,
) -> None:
    private, public = _key(tmp_path)
    opened = _open(tmp_path)
    gate_id = opened["gate"]["gate_id"]
    directory = tmp_path / ".hw/secure-run/gates"
    token = _signed(tmp_path / "decision.json", opened, private, public)
    gates.gate_approve(repo_root=tmp_path, run_id="secure-run", gate_id=gate_id,
                       decision=GateDecision.APPROVE, token_file=token, public_key_path=public)
    effects = []

    def callback():
        effects.append("effect")
        if outcome == "uncertain":
            raise RuntimeError("lost response")

    if outcome != "consumed":
        if outcome == "uncertain":
            with pytest.raises(gates.GateError, match="uncertain outcome"):
                gates.execute_protected_action(repo_root=tmp_path, run_id="secure-run",
                    gate_id=gate_id, callback=callback, public_key_path=public)
        else:
            gates.execute_protected_action(repo_root=tmp_path, run_id="secure-run",
                gate_id=gate_id, callback=callback, public_key_path=public)
    before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    later = approvals._now() + timedelta(minutes=16)
    monkeypatch.setattr(approvals, "_now", lambda: later)
    with pytest.raises(gates.GateError, match="already decided"):
        gates.gate_open(repo_root=tmp_path, run_id="secure-run", kind=GateKind.DEPLOY)
    with pytest.raises(approvals.ApprovalError, match="consumed or action attempted"):
        approvals.issue_challenge(directory, run_id="secure-run", gate_id=gate_id,
                                  action=opened["gate"]["action"], evidence=["review.json"])
    assert before == {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    with pytest.raises(gates.GateError):
        gates.execute_protected_action(repo_root=tmp_path, run_id="secure-run", gate_id=gate_id,
                                       callback=callback, public_key_path=public)
    assert len(effects) == (outcome != "consumed")


@pytest.mark.parametrize("hostile", ["../escape", "/tmp/escape", "a/b", "a\\b", "a..b", "x" * 65])
def test_run_identifiers_reject_traversal_before_writes(tmp_path: Path, hostile: str) -> None:
    with pytest.raises(ValueError):
        engine.RunPaths(tmp_path, hostile)
    assert not (tmp_path / "escape" / "state.json").exists()
