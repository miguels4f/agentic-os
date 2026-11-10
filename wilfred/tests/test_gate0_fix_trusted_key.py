"""The review verification key must live where a constrained worker cannot place it.

Keys are generated inside temporary directories; no production signer or run is touched.
Command-line and control-path proofs live in ``test_control_evolution_abuse.py``.
"""

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyperwilfred_core import approvals, regression


def test_location_rule_is_public_and_accepts_an_external_key(tmp_path):
    assert "require_external_key_location" in approvals.__all__
    assert approvals.require_external_key_location.__doc__
    root = tmp_path / "product"
    root.mkdir()
    key = tmp_path / "keys" / ".." / "keys" / "k.pem"
    accepted = approvals.require_external_key_location(key, excluded_roots=[root])
    assert accepted == tmp_path / "keys" / "k.pem" and accepted.is_absolute()


@pytest.mark.parametrize("where", ["hw", "root", "work-tree", "symlinked-dir", "hw-real"])
def test_location_rule_refuses_worker_writable_locations(tmp_path, where):
    tree, outside = tmp_path / "tree", tmp_path / "outside"
    root = tree / "product"
    root.mkdir(parents=True)
    outside.mkdir()
    subprocess.run(["git", "init", "-q", str(tree)], check=True, capture_output=True)
    if where == "hw":
        key = outside / ".hw" / "k.pem"
    elif where == "root":
        key = root / "k.pem"
    elif where == "work-tree":
        key = tree / "sibling" / "k.pem"
    elif where == "symlinked-dir":
        (outside / "link").symlink_to(root, target_is_directory=True)
        key = outside / "link" / "k.pem"
    else:
        (tmp_path / "state" / ".hw").mkdir(parents=True)
        (outside / "alias").symlink_to(tmp_path / "state" / ".hw", target_is_directory=True)
        key = outside / "alias" / "k.pem"
    with pytest.raises(approvals.ApprovalError, match="review key"):
        approvals.require_external_key_location(key, excluded_roots=[root])


def _semantic(monkeypatch, target, run_dir, key, *, repo_root=None, default=False,
              volume_alias=False):
    """Sign one exact test transition with a key at *key*; return transitions and the check."""
    target.mkdir(parents=True, exist_ok=True)
    test_file = target / "test_contract.py"
    test_file.write_text("def test_contract():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    baseline = regression.capture_baseline(target)
    test_file.write_text("def test_contract():\n    assert 3 + 3 == 6\n", encoding="utf-8")
    private = Ed25519PrivateKey.generate()
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    created = regression.create_semantic_review_challenge(
        run_dir, run_id="run", path="test_contract.py",
        before_sha256=baseline["test_file_hashes"]["test_contract.py"],
        after_sha256=hashlib.sha256(test_file.read_bytes()).hexdigest(),
        dispatch_sha256="a" * 64, report_sha256="b" * 64)
    payload = approvals.build_decision_payload(
        created["challenge"], decision="approve", decided_by="fixture-independent-reviewer",
        note="separate review fixture", public_key_path=key)
    Path(created["decision_file"]).write_text(json.dumps({
        "payload": payload,
        "signature": base64.b64encode(private.sign(approvals.canonical_bytes(payload))).decode(),
    }), encoding="utf-8")
    if default:
        monkeypatch.setattr(approvals, "DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH", key)
    extra = {} if repo_root is None else {"repo_root": repo_root}
    verify_key = key
    if volume_alias:
        st = key.stat()
        verify_key = Path("/.vol") / str(st.st_dev) / str(st.st_ino)
        assert verify_key.exists() and os.path.samefile(verify_key, key)
    transitions = regression.verified_semantic_transitions(
        run_dir, run_id="run", target=target, baseline=baseline,
        public_key_path=None if default else verify_key, **extra)
    return transitions, regression.compare(target, baseline, approved_transitions=transitions)


def test_semantic_path_uses_installed_default_key_when_none_injected(tmp_path, monkeypatch):
    transitions, check = _semantic(
        monkeypatch, tmp_path / "package", tmp_path / ".hw" / "run",
        tmp_path / "installed" / "review-public.pem", default=True)
    assert set(transitions) == {"test_contract.py"}
    assert check.passed is True


@pytest.mark.parametrize("where", ["hw", "target", "repository", "work-tree", "symlinked-dir",
                                   "default-in-hw"])
def test_semantic_path_refuses_key_inside_repository_or_hw(tmp_path, monkeypatch, where):
    repo = tmp_path / "repo"
    target, run_dir, repo_root = repo / "package", repo / ".hw" / "run", None
    if where in {"hw", "default-in-hw"}:
        key = run_dir / "review-public.pem"
    elif where == "target":
        key = target / "keys" / "review-public.pem"
    elif where == "repository":
        key, repo_root = repo / "keys" / "review-public.pem", repo
    elif where == "work-tree":
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        key = repo / "keys" / "review-public.pem"
    else:
        (target / "keys").mkdir(parents=True)
        (tmp_path / "linked").symlink_to(target / "keys", target_is_directory=True)
        key = tmp_path / "linked" / "review-public.pem"
    transitions, check = _semantic(monkeypatch, target, run_dir, key, repo_root=repo_root,
                                   default=where == "default-in-hw")
    assert transitions == {}
    assert check.passed is False


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS volume aliases only")
def test_semantic_regression_rejects_inode_alias_of_repository_key(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    target = repo / "package"
    transitions, check = _semantic(
        monkeypatch, target, repo / ".hw" / "run", repo / "keys" / "review.pem",
        repo_root=repo, volume_alias=True,
    )
    assert transitions == {}
    assert check.passed is False
