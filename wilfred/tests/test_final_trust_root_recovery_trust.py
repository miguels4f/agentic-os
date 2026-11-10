"""Trust-root recovery and no-symlink review-scope policy."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_quality import _run_at_quality

from hyperwilfred_core import approvals, engine, quality
from hyperwilfred_core.schemas import (
    CritiqueReport,
    ReviewTrustAnchor,
    SubjectiveCriterion,
    SubjectiveEvaluation,
)


def _key(path: Path) -> tuple[Ed25519PrivateKey, Path]:
    private = Ed25519PrivateKey.generate()
    public = path
    public.parent.mkdir(parents=True, exist_ok=True)
    public.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    return private, public


def _sign(first: dict, private: Ed25519PrivateKey, public: Path) -> Path:
    challenge_path = Path(first["attestation_challenge"])
    gate_id = challenge_path.name.removesuffix(".challenge.json")
    challenge = approvals.load_challenge(challenge_path.parent, gate_id)
    payload = approvals.build_decision_payload(
        challenge, decision="approve", decided_by="external-fixture", note="",
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
    return decision


def _subjective_input(repo: Path) -> Path:
    path = repo / ".hw/q/subjective-input.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SubjectiveEvaluation(
        evaluator="fixture", criteria=[SubjectiveCriterion(name="trust", score=5)],
    ).model_dump_json(), encoding="utf-8")
    return path


def _accept_subjective(
    repo: Path, private: Ed25519PrivateKey, public: Path,
) -> tuple[dict, dict]:
    source = _subjective_input(repo)
    kwargs = {
        "repo_root": repo, "run_id": "q", "result_file": str(source),
        "author": "independent", "review_public_key_path": public,
    }
    first = quality.record_subjective(**kwargs)
    token = _sign(first, private, public)
    return quality.record_subjective(**kwargs, attestation_file=str(token)), kwargs


def _accept_critique(
    repo: Path, lens: str, private: Ed25519PrivateKey, public: Path,
) -> None:
    source = repo / f".hw/q/quality/iter-0/critics/{lens}.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        CritiqueReport(critic="fixture", lens=lens, findings=[]).model_dump_json(),
        encoding="utf-8",
    )
    kwargs = {
        "repo_root": repo, "run_id": "q", "packet_id": f"critic-0-{lens}",
        "result_file": str(source), "author": "independent",
        "review_public_key_path": public,
    }
    first = quality.record_critique(**kwargs)
    token = _sign(first, private, public)
    quality.record_critique(**kwargs, attestation_file=str(token))


def _provenance(repo: Path, public: Path) -> dict:
    state = engine.load_state(engine.RunPaths(repo, "q"))
    return quality.provenance_report(
        repo_root=repo, run_id="q", state=state, review_public_key_path=public,
    )


def test_explicit_key_dependency_covers_subjective_critique_and_gate(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    _run_at_quality(tmp_path)
    keys = tmp_path_factory.mktemp("external-keys")
    private, public = _key(keys / "external-review.pem")
    _accept_subjective(tmp_path, private, public)
    quality.critique_plan(repo_root=tmp_path, run_id="q")
    for lens in quality.CRITIC_LENSES:
        _accept_critique(tmp_path, lens, private, public)
    state = engine.load_state(engine.RunPaths(tmp_path, "q"))
    assert state.quality is not None
    assert len(state.quality.review_trust_anchors) == 5
    assert _provenance(tmp_path, public)["ok"] is True
    assert quality.quality_gate(
        repo_root=tmp_path, run_id="q", review_public_key_path=public,
    )["action"] == "pass"


@pytest.mark.parametrize("mutation", ["missing", "changed", "mismatch", "local-pin"])
def test_external_key_and_local_pin_are_always_revalidated(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, mutation: str,
) -> None:
    _run_at_quality(tmp_path)
    keys = tmp_path_factory.mktemp("external-keys")
    private_a, public_a = _key(keys / "external-a.pem")
    _accept_subjective(tmp_path, private_a, public_a)
    _private_b, public_b = _key(keys / "external-b.pem")
    checked = public_a
    if mutation == "missing":
        public_a.unlink()
    elif mutation == "changed":
        public_a.write_bytes(public_b.read_bytes())
    elif mutation == "mismatch":
        checked = public_b
    else:
        pin = next((tmp_path / ".hw/q/review-attestations").glob("*.public-key.pem"))
        pin.write_bytes(public_b.read_bytes())
    assert "subjective" not in _provenance(tmp_path, checked)["independent"]


def test_coherent_local_key_substitution_cannot_replace_external_root(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    _run_at_quality(tmp_path)
    keys = tmp_path_factory.mktemp("external-keys")
    private_a, public_a = _key(keys / "external-a.pem")
    _accept_subjective(tmp_path, private_a, public_a)
    private_b, public_b = _key(keys / "external-b.pem")
    paths = engine.RunPaths(tmp_path, "q")
    state = engine.load_state(paths)
    assert state.quality is not None
    anchor_key = "0:subjective"
    gate_id = state.quality.review_trust_anchors[anchor_key].gate_id
    directory = paths.run_dir / "review-attestations"

    challenge_path = approvals.challenge_file(directory, gate_id)
    challenge = json.loads(challenge_path.read_text())
    challenge["nonce"] = "coherent-key-b-substitution"
    challenge_path.write_text(json.dumps(challenge), encoding="utf-8")
    payload = approvals.build_decision_payload(
        challenge, decision="approve", decided_by="forged-b", note="",
        public_key_path=public_b,
    )
    token = {"payload": payload, "signature": base64.b64encode(
        private_b.sign(approvals.canonical_bytes(payload))).decode()}
    token_path = directory / f"{gate_id}.decision.json"
    token_path.write_text(json.dumps(token), encoding="utf-8")
    token_sha = hashlib.sha256(approvals.canonical_bytes(token)).hexdigest()
    manifest_path = directory / f"{gate_id}.prepared.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(
        challenge_sha256=hashlib.sha256(approvals.canonical_bytes(challenge)).hexdigest(),
        token_sha256=token_sha,
        key_fingerprint=approvals.public_key_fingerprint(public_b),
    )
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (directory / f"{gate_id}.public-key.pem").write_bytes(public_b.read_bytes())
    receipt = approvals.consumed_file(directory, gate_id)
    receipt.write_text(token_sha + "\n", encoding="utf-8")
    recorded_path = directory / f"{gate_id}.recorded.json"
    recorded = json.loads(recorded_path.read_text())
    recorded.update(
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
    )
    recorded_path.write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n")
    state.quality.review_trust_anchors[anchor_key] = ReviewTrustAnchor(
        gate_id=gate_id,
        external_key_fingerprint=approvals.public_key_fingerprint(public_b),
        transaction_sha256=hashlib.sha256(recorded_path.read_bytes()).hexdigest(),
    )
    engine.save_state(paths, state)

    assert "subjective" not in _provenance(tmp_path, public_a)["independent"]


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"],
                   check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "product").mkdir()
    (path / "product/reviewed.py").write_text("original = True\n")
    subprocess.run(["git", "-C", str(path), "add", "product/reviewed.py"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)


@pytest.mark.parametrize("link_kind", ["internal", "external", "broken", "cycle", "mutated"])
def test_changed_review_scope_rejects_every_symlink_kind(
    tmp_path: Path, link_kind: str,
) -> None:
    _git_repo(tmp_path)
    _run_at_quality(tmp_path)
    reviewed = tmp_path / "product/reviewed.py"
    reviewed.unlink()
    if link_kind in {"internal", "mutated"}:
        target = tmp_path / "product/referent.py"
        target.write_text("first = True\n")
        reviewed.symlink_to(target)
        if link_kind == "mutated":
            target.write_text("second = True\n")
    elif link_kind == "external":
        target = tmp_path.parent / f"{tmp_path.name}-external.py"
        target.write_text("outside = True\n")
        reviewed.symlink_to(target)
    elif link_kind == "broken":
        reviewed.symlink_to(tmp_path / "missing.py")
    else:
        reviewed.symlink_to(reviewed)

    with pytest.raises(quality.QualityError, match="symlink"):
        quality.critique_plan(repo_root=tmp_path, run_id="q")
