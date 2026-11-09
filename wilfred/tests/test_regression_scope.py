import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyperwilfred_core import approvals, regression


def test_exactly_scoped_test_evolution_requires_every_baseline_id(tmp_path: Path) -> None:
    """Legacy ID retained; packet path scope cannot waive a gutted assertion."""
    package = tmp_path / "package"
    package.mkdir()
    test_file = package / "test_contract.py"
    test_file.write_text("def test_contract():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    baseline = regression.capture_baseline(package)
    test_file.write_text("def test_contract():\n    assert True\n", encoding="utf-8")

    result = regression.compare(package, baseline)

    assert result.passed is False
    assert "regression-witness integrity" in result.details


def test_exact_signed_semantic_transition_can_evolve_test(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    test_file = package / "test_contract.py"
    test_file.write_text("def test_contract():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    baseline = regression.capture_baseline(package)
    test_file.write_text("def test_contract():\n    assert 3 + 3 == 6\n", encoding="utf-8")
    after = hashlib.sha256(test_file.read_bytes()).hexdigest()
    private = Ed25519PrivateKey.generate()
    public = tmp_path / "review-public.pem"
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    run_dir = tmp_path / ".hw" / "run"
    created = regression.create_semantic_review_challenge(
        run_dir,
        run_id="run",
        path="test_contract.py",
        before_sha256=baseline["test_file_hashes"]["test_contract.py"],
        after_sha256=after,
        dispatch_sha256="a" * 64,
        report_sha256="b" * 64,
    )
    payload = approvals.build_decision_payload(
        created["challenge"],
        decision="approve",
        decided_by="fixture-independent-reviewer",
        note="separate review fixture",
        public_key_path=public,
    )
    token = {
        "payload": payload,
        "signature": base64.b64encode(private.sign(approvals.canonical_bytes(payload))).decode(),
    }
    Path(created["decision_file"]).write_text(json.dumps(token), encoding="utf-8")
    transitions = regression.verified_semantic_transitions(
        run_dir,
        run_id="run",
        target=package,
        baseline=baseline,
        public_key_path=public,
    )

    assert regression.compare(package, baseline, approved_transitions=transitions).passed is True


def test_empty_or_undeterminable_baseline_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(regression, "_pytest_passing", lambda _target: (None, None, []))
    with pytest.raises(regression.RegressionError, match="unavailable"):
        regression.capture_baseline(tmp_path)
    monkeypatch.setattr(regression, "_pytest_passing", lambda _target: (set(), 0, []))
    with pytest.raises(regression.RegressionError, match="no passing tests"):
        regression.capture_baseline(tmp_path)


def test_partially_failing_suite_cannot_be_captured_as_green_baseline(tmp_path: Path) -> None:
    (tmp_path / "test_mixed.py").write_text(
        "def test_passes():\n    assert True\n\n"
        "def test_fails():\n    assert False\n",
        encoding="utf-8",
    )
    with pytest.raises(regression.RegressionError, match="did not pass cleanly"):
        regression.capture_baseline(tmp_path)
