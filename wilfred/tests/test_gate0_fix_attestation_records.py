"""Attestation transaction records are built by one helper each, shared by writer and checker.

The prepared manifest and the recorded state of an independent-review attestation are each
spelled out once, so a field added to the record reaches both the write and the comparison.
Every repository, run and signing key is created inside temporary directories.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_quality import _run_at_quality

from hyperwilfred_core import approvals, engine, quality
from hyperwilfred_core.schemas import SubjectiveCriterion, SubjectiveEvaluation

_PREPARED_KEYS = {
    "schema_version", "gate_id", "run_id", "iteration", "packet_id", "lens", "record_sha256",
    "challenge_sha256", "action_sha256", "evidence_sha256", "token_sha256", "key_fingerprint",
}
_RECORDED_KEYS = {
    "schema_version", "gate_id", "record_sha256", "manifest_sha256", "receipt_sha256",
}


def _key(path: Path) -> tuple[Ed25519PrivateKey, Path]:
    private = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    return private, path


def _sign(first: dict, private: Ed25519PrivateKey, public: Path) -> Path:
    challenge_path = Path(first["attestation_challenge"])
    gate_id = challenge_path.name.removesuffix(".challenge.json")
    challenge = approvals.load_challenge(challenge_path.parent, gate_id)
    payload = approvals.build_decision_payload(
        challenge, decision="approve", decided_by="external-fixture", note="",
        public_key_path=public,
    )
    token = {"payload": payload, "signature": base64.b64encode(
        private.sign(approvals.canonical_bytes(payload))).decode()}
    decision = Path(first["attestation_decision"])
    decision.write_text(json.dumps(token), encoding="utf-8")
    return decision


def _attest(repo: Path, keys: Path) -> tuple[dict, Path, str, Path]:
    """Submit one signed subjective review; return its call arguments, token and gate."""
    _run_at_quality(repo)
    private, public = _key(keys / "external-review.pem")
    source = repo / ".hw/q/subjective-input.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(SubjectiveEvaluation(
        evaluator="fixture", criteria=[SubjectiveCriterion(name="trust", score=5)],
    ).model_dump_json(), encoding="utf-8")
    kwargs = {
        "repo_root": repo, "run_id": "q", "result_file": str(source),
        "author": "independent", "review_public_key_path": public,
    }
    first = quality.record_subjective(**kwargs)
    token = _sign(first, private, public)
    gate_id = Path(first["attestation_challenge"]).name.removesuffix(".challenge.json")
    quality.record_subjective(**kwargs, attestation_file=str(token))
    return kwargs, token, gate_id, engine.RunPaths(repo, "q").run_dir / "review-attestations"


def test_recovery_with_exact_prepared_token_succeeds_and_altered_manifest_is_refused(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    repo = tmp_path / "run"
    repo.mkdir()
    kwargs, token, gate_id, directory = _attest(repo, tmp_path_factory.mktemp("keys"))
    manifest = directory / f"{gate_id}.prepared.json"
    recorded = directory / f"{gate_id}.recorded.json"
    prepared = json.loads(manifest.read_text())
    assert set(prepared) == _PREPARED_KEYS
    assert set(json.loads(recorded.read_text())) == _RECORDED_KEYS
    before = (manifest.read_bytes(), recorded.read_bytes())

    retried = quality.record_subjective(**kwargs, attestation_file=str(token))
    assert retried["author"] == "independent"
    assert (manifest.read_bytes(), recorded.read_bytes()) == before

    manifest.write_text(json.dumps({**prepared, "lens": "altered"}, indent=2, sort_keys=True)
                        + "\n", encoding="utf-8")
    with pytest.raises(quality.QualityError, match="prepared manifest was altered"):
        quality.record_subjective(**kwargs, attestation_file=str(token))


def test_prepared_manifest_helper_is_the_only_source_for_write_and_retry(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = quality._prepared_manifest
    monkeypatch.setattr(quality, "_prepared_manifest", lambda *args, **kwargs: {
        **original(*args, **kwargs), "probe": "prepared"})
    repo = tmp_path / "run"
    repo.mkdir()
    kwargs, token, gate_id, directory = _attest(repo, tmp_path_factory.mktemp("keys"))
    manifest = directory / f"{gate_id}.prepared.json"
    assert json.loads(manifest.read_text())["probe"] == "prepared"

    retried = quality.record_subjective(**kwargs, attestation_file=str(token))
    assert retried["author"] == "independent"

    monkeypatch.setattr(quality, "_prepared_manifest", original)
    with pytest.raises(quality.QualityError, match="prepared manifest was altered"):
        quality.record_subjective(**kwargs, attestation_file=str(token))


def test_recorded_state_helper_is_the_only_source_for_finalize_and_verify(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = quality._recorded_state
    monkeypatch.setattr(quality, "_recorded_state", lambda *args, **kwargs: {
        **original(*args, **kwargs), "probe": "recorded"})
    repo = tmp_path / "run"
    repo.mkdir()
    kwargs, token, gate_id, directory = _attest(repo, tmp_path_factory.mktemp("keys"))
    recorded = directory / f"{gate_id}.recorded.json"
    assert json.loads(recorded.read_text())["probe"] == "recorded"
    assert quality._verify_finalized(directory, gate_id, json.loads(
        recorded.read_text())["record_sha256"]) == hashlib.sha256(
            recorded.read_bytes()).hexdigest()

    retried = quality.record_subjective(**kwargs, attestation_file=str(token))
    assert retried["author"] == "independent"

    monkeypatch.setattr(quality, "_recorded_state", original)
    with pytest.raises(quality.QualityError, match="recorded state was altered"):
        quality.record_subjective(**kwargs, attestation_file=str(token))


def test_helpers_reproduce_the_records_on_disk(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    repo = tmp_path / "run"
    repo.mkdir()
    _kwargs, _token, gate_id, directory = _attest(repo, tmp_path_factory.mktemp("keys"))
    manifest = directory / f"{gate_id}.prepared.json"
    recorded = directory / f"{gate_id}.recorded.json"
    prepared = json.loads(manifest.read_text())
    state = json.loads(recorded.read_text())
    assert quality._recorded_state(directory, gate_id, state["record_sha256"]) == state
    assert (json.dumps(quality._recorded_state(directory, gate_id, state["record_sha256"]),
                       indent=2, sort_keys=True) + "\n").encode() == recorded.read_bytes()
    assert state["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert prepared["schema_version"] == state["schema_version"]
