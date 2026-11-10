"""Test-only signer standing in for the guarded independent-review execution boundary."""

from __future__ import annotations

import atexit
import base64
import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyperwilfred_core import approvals

_KEY_DIRECTORY: list[Path] = []
# One fixture signer per resolved repository path, like the single installed key that signs
# every review of one production installation.  Test-only state; production keeps none.
_REPOSITORY_KEYS: dict[Path, tuple[Ed25519PrivateKey, Path]] = {}


def _key_directory() -> Path:
    """One per-process directory outside every repository, removed when the process exits.

    Review keys must live where a constrained worker cannot place them, so fixture keys never
    go inside the reviewed repository, its product directory or any ``.hw`` directory.
    """
    if not _KEY_DIRECTORY:
        directory = Path(tempfile.mkdtemp(prefix="wilfred-fixture-review-key-")).resolve()
        atexit.register(shutil.rmtree, directory, True)
        _KEY_DIRECTORY.append(directory)
    _KEY_DIRECTORY[0].mkdir(mode=0o700, exist_ok=True)
    return _KEY_DIRECTORY[0]


def _key(repo: Path) -> tuple[Ed25519PrivateKey, Path]:
    """Create one call-scoped fixture signer outside *repo*; production pins the accepted key."""
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    public = _key_directory() / f"test-review-{hashlib.sha256(raw).hexdigest()[:16]}.pem"
    public.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ))
    return private, public


def _repository_key(repo: Path) -> tuple[Ed25519PrivateKey, Path]:
    """Return the one fixture signer of *repo*, creating it outside *repo* on first use."""
    resolved = repo.resolve()
    if resolved not in _REPOSITORY_KEYS or not _REPOSITORY_KEYS[resolved][1].is_file():
        _REPOSITORY_KEYS[resolved] = _key(repo)
    return _REPOSITORY_KEYS[resolved]


@contextmanager
def fixture_signer(repo: Path):
    """Install a call-scoped review key and restore production configuration on every exit."""
    private, public = _key(repo)
    original = approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH
    try:
        approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH = public
        yield private, public
    finally:
        approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH = original


def write_signed_decision(
    first: dict[str, Any], private: Ed25519PrivateKey, public: Path,
    *, decision: str = "approve",
) -> Path:
    challenge_path = Path(first["attestation_challenge"])
    gate_id = challenge_path.name.removesuffix(".challenge.json")
    challenge = approvals.load_challenge(challenge_path.parent, gate_id)
    payload = approvals.build_decision_payload(
        challenge, decision=decision, decided_by="fixture-independent-execution", note="",
        public_key_path=public,
    )
    token = {"payload": payload, "signature": base64.b64encode(
        private.sign(approvals.canonical_bytes(payload))).decode()}
    result = Path(first["attestation_decision"])
    result.write_text(json.dumps(token), encoding="utf-8")
    return result


def record_attested(
    operation: Callable[..., dict[str, Any]], *, repo: Path, **kwargs: Any,
) -> dict[str, Any]:
    """Request, externally sign, and submit one exact-result attestation.

    Later checks of *repo* (``quality_gate``, ``quality_status``, the ship gate) verify with the
    installed default key, as production does, so the repository's fixture key is installed as
    that default and stays installed until a review of another repository installs its own.
    """
    private, public = _repository_key(repo)
    approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH = public
    injected = {**kwargs, "review_public_key_path": public}
    first = operation(**injected, author="independent")
    decision = write_signed_decision(first, private, public)
    return operation(
        **injected, author="independent", attestation_file=str(decision)
    )
