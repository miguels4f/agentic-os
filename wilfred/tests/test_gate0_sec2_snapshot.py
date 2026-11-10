"""A green deterministic decision and a human override cover exact product bytes."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_gate0_fix_pinned_scope import _pinned_run

from hyperwilfred_core import approvals, engine, quality


def test_product_edit_turns_status_and_gate_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _pinned_run(tmp_path, monkeypatch)
    assert quality.quality_status(repo_root=tmp_path, run_id="q")["deterministic_green"]
    source.write_text("VALUE = 3\n")
    assert not quality.quality_status(repo_root=tmp_path, run_id="q")["deterministic_green"]


def test_override_signs_product_and_expires_on_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, paths = _pinned_run(tmp_path, monkeypatch)
    private = Ed25519PrivateKey.generate()
    public = tmp_path.parent / "approval-public.pem"
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    monkeypatch.setattr(approvals, "DEFAULT_PUBLIC_KEY_PATH", public)
    with pytest.raises(quality.QualityError, match="must sign"):
        quality.record_independent_override(
            repo_root=tmp_path,
            run_id="q",
            by="owner",
            reason="review unavailable",
        )
    challenge_path = next((paths.run_dir / "gates").glob("*.challenge.json"))
    challenge = json.loads(challenge_path.read_text())
    state = engine.load_state(paths)
    current = quality.product_snapshot_sha256(tmp_path, state)
    assert any(current in line for line in challenge["evidence"])
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
    challenge_path.with_name(
        challenge_path.name.replace(".challenge.json", ".decision.json")
    ).write_text(json.dumps(token))
    quality.record_independent_override(
        repo_root=tmp_path,
        run_id="q",
        by="owner",
        reason="review unavailable",
    )
    state = engine.load_state(paths)
    assert quality.provenance_report(repo_root=tmp_path, run_id="q", state=state)["override"]
    source.write_text("VALUE = 3\n")
    state = engine.load_state(paths)
    assert (
        quality.provenance_report(repo_root=tmp_path, run_id="q", state=state)["override"] is None
    )
