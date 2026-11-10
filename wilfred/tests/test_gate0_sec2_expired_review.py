"""Mutable preparation files cannot revive an expired review signature."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_gate0_fix_pinned_scope import _pinned_run

from hyperwilfred_core import approvals, quality


def test_handwritten_receipt_does_not_revive_expired_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, paths = _pinned_run(tmp_path, monkeypatch)
    private = Ed25519PrivateKey.generate()
    public = tmp_path.parent / "review-public.pem"
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    monkeypatch.setattr(approvals, "DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH", public)
    result = paths.run_dir / "subjective-input.json"
    result.write_text(
        json.dumps(
            {
                "evaluator": "probe",
                "iteration": 0,
                "criteria": [{"name": "quality", "score": 5, "justification": "passes"}],
            }
        )
    )
    kwargs = dict(repo_root=tmp_path, run_id="q", result_file=str(result), author="independent")
    first = quality.record_subjective(**kwargs)
    challenge_path = Path(first["attestation_challenge"])
    directory = challenge_path.parent
    challenge = json.loads(challenge_path.read_text())
    gate = challenge["gate_id"]
    payload = approvals.build_decision_payload(
        challenge,
        decision="approve",
        decided_by="reviewer",
        note="",
        public_key_path=public,
    )
    payload["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    token = {
        "payload": payload,
        "signature": base64.b64encode(private.sign(approvals.canonical_bytes(payload))).decode(),
    }
    supplied = tmp_path.parent / "expired-review.json"
    supplied.write_text(json.dumps(token))
    with pytest.raises(quality.QualityError, match="expired"):
        quality.record_subjective(**kwargs, attestation_file=str(supplied))
    evidence = challenge["evidence"]
    record_sha = next(
        item.split(":", 1)[1] for item in evidence if item.startswith("result_sha256:")
    )
    token_sha = hashlib.sha256(approvals.canonical_bytes(token)).hexdigest()
    manifest = quality._prepared_manifest(
        directory,
        gate_id=gate,
        run_id="q",
        iteration=0,
        packet_id="subjective-0",
        lens="subjective",
        record_sha256=record_sha,
        action=challenge["action"],
        evidence=evidence,
        token_sha256=token_sha,
        key_fingerprint=approvals.public_key_fingerprint(public),
    )
    (directory / f"{gate}.prepared.json").write_text(json.dumps(manifest))
    (directory / f"{gate}.public-key.pem").write_bytes(public.read_bytes())
    (directory / f"{gate}.decision.json").write_text(json.dumps(token))
    approvals.consumed_file(directory, gate).write_text(token_sha + "\n")
    with pytest.raises(quality.QualityError, match="expired"):
        quality.record_subjective(**kwargs, attestation_file=str(supplied))
