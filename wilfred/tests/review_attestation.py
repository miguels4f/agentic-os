"""Test-only signer standing in for the guarded independent-review execution boundary."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyperwilfred_core import approvals

_KEYS: dict[Path, tuple[Ed25519PrivateKey, Path]] = {}


def _key(repo: Path) -> tuple[Ed25519PrivateKey, Path]:
    root = repo.resolve()
    if root not in _KEYS:
        private = Ed25519PrivateKey.generate()
        public = root / ".hw" / "test-review-attestation-public.pem"
        public.parent.mkdir(parents=True, exist_ok=True)
        public.write_bytes(private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ))
        _KEYS[root] = private, public
    private, public = _KEYS[root]
    approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH = public
    return private, public


def record_attested(
    operation: Callable[..., dict[str, Any]], *, repo: Path, **kwargs: Any,
) -> dict[str, Any]:
    """Request, externally sign, and submit one exact-result attestation."""
    private, public = _key(repo)
    first = operation(**kwargs, author="independent")
    challenge_path = Path(first["attestation_challenge"])
    gate_id = challenge_path.name.removesuffix(".challenge.json")
    challenge = approvals.load_challenge(challenge_path.parent, gate_id)
    payload = approvals.build_decision_payload(
        challenge, decision="approve", decided_by="fixture-independent-execution", note="",
        public_key_path=public,
    )
    token = {
        "payload": payload,
        "signature": base64.b64encode(
            private.sign(approvals.canonical_bytes(payload))
        ).decode(),
    }
    decision = Path(first["attestation_decision"])
    decision.write_text(json.dumps(token), encoding="utf-8")
    return operation(**kwargs, author="independent", attestation_file=str(decision))
