"""The trusted public keys are located from the account record, never from the environment.

Every key is generated in a temporary directory and the account record is replaced by a
fixture, so the installed key directory of the machine is never read.  The fresh-interpreter
cases start a child with the home variable pointing at a directory that holds a second key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyperwilfred_core import approvals, regression

_SRC = Path(__file__).resolve().parents[1] / "src"
_KEY_DIRECTORY = ("Library", "Application Support", "Wilfred")
_APPROVAL, _REVIEW = "approval-public-key.pem", "review-attestation-public-key.pem"
_CHILD = """
import json, pwd, sys, types
from pathlib import Path
mode, account, work = sys.argv[1], sys.argv[2], Path(sys.argv[3])
def _record(uid):
    if not account:
        raise KeyError(uid)
    return types.SimpleNamespace(pw_dir=account)
pwd.getpwuid = _record
from hyperwilfred_core import approvals, regression
out = {"approval": str(approvals.DEFAULT_PUBLIC_KEY_PATH),
       "review": str(approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH)}
if mode == "approval":
    try:
        out["decision"] = approvals.verify_and_consume(
            work, "gate", work / "gate.decision.json", expected_run_id="run",
            expected_action="act", expected_evidence=["proof"], consume=False)[0]
    except approvals.ApprovalError as exc:
        out["refused"] = str(exc)
elif mode == "review":
    baseline = json.loads((work / "baseline.json").read_text(encoding="utf-8"))
    approved = regression.verified_semantic_transitions(
        work / ".hw" / "run", run_id="run", target=work / "package", baseline=baseline)
    out["transitions"] = sorted(approved)
    out["passed"] = regression.compare(
        work / "package", baseline, approved_transitions=approved).passed
else:
    for name in ("approval", "review"):
        try:
            approvals.public_key_fingerprint(Path(out[name]))
            out[name + "_loaded"] = True
        except approvals.ApprovalError as exc:
            out[name + "_refused"] = str(exc)
print(json.dumps(out))
"""


def _install(home: Path, name: str) -> tuple[Ed25519PrivateKey, Path]:
    """Write a fresh public key where the engine looks below *home*; return signer and path."""
    private = Ed25519PrivateKey.generate()
    public = home.joinpath(*_KEY_DIRECTORY, name)
    public.parent.mkdir(parents=True, exist_ok=True)
    public.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return private, public


def _token(challenge: dict, private: Ed25519PrivateKey, public: Path) -> str:
    payload = approvals.build_decision_payload(
        challenge, decision="approve", decided_by="fixture-signer", note="fixture",
        public_key_path=public)
    return json.dumps({
        "payload": payload,
        "signature": base64.b64encode(private.sign(approvals.canonical_bytes(payload))).decode(),
    })


def _child(mode: str, account: Path | str, work: Path, environment_home: Path) -> dict:
    env = {**os.environ, "PYTHONPATH": str(_SRC), "PYTHONDONTWRITEBYTECODE": "1",
           "HOME": str(environment_home)}
    done = subprocess.run(
        [sys.executable, "-c", _CHILD, mode, str(account), str(work)],
        check=True, capture_output=True, text=True, env=env, cwd=work)
    return json.loads(done.stdout)


def _homes(tmp_path: Path, name: str) -> dict[str, tuple[Ed25519PrivateKey, Path]]:
    return {"account": _install(tmp_path / "account", name),
            "environment": _install(tmp_path / "environment", name)}


@pytest.mark.parametrize("signer", ["environment", "account"])
def test_approval_key_is_read_from_the_account_home(tmp_path: Path, signer: str) -> None:
    private, public = _homes(tmp_path, _APPROVAL)[signer]
    gates = tmp_path / "gates"
    challenge = approvals.issue_challenge(
        gates, run_id="run", gate_id="gate", action="act", evidence=["proof"])
    (gates / "gate.decision.json").write_text(
        _token(challenge, private, public), encoding="utf-8")
    seen = _child("approval", tmp_path / "account", gates, tmp_path / "environment")
    assert seen["approval"] == str((tmp_path / "account").joinpath(*_KEY_DIRECTORY, _APPROVAL))
    if signer == "account":
        assert seen.get("decision") == "approve"
    else:
        assert "untrusted key" in seen.get("refused", "")


@pytest.mark.parametrize("signer", ["environment", "account"])
def test_review_key_is_read_from_the_account_home(tmp_path: Path, signer: str) -> None:
    private, public = _homes(tmp_path, _REVIEW)[signer]
    work = tmp_path / "work"
    target, run_dir = work / "package", work / ".hw" / "run"
    target.mkdir(parents=True)
    test_file = target / "test_contract.py"
    test_file.write_text("def test_contract():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    baseline = regression.capture_baseline(target)
    (work / "baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
    test_file.write_text("def test_contract():\n    assert 3 + 3 == 6\n", encoding="utf-8")
    created = regression.create_semantic_review_challenge(
        run_dir, run_id="run", path="test_contract.py",
        before_sha256=baseline["test_file_hashes"]["test_contract.py"],
        after_sha256=hashlib.sha256(test_file.read_bytes()).hexdigest(),
        dispatch_sha256="a" * 64, report_sha256="b" * 64)
    Path(created["decision_file"]).write_text(
        _token(created["challenge"], private, public), encoding="utf-8")
    seen = _child("review", tmp_path / "account", work, tmp_path / "environment")
    assert seen["review"] == str((tmp_path / "account").joinpath(*_KEY_DIRECTORY, _REVIEW))
    if signer == "account":
        assert seen["transitions"] == ["test_contract.py"] and seen["passed"] is True
    else:
        assert seen["transitions"] == [] and seen["passed"] is False


@pytest.mark.parametrize("record", ["", "relative-home"])
def test_missing_or_unusable_account_record_fails_closed(tmp_path: Path, record: str) -> None:
    for name in (_APPROVAL, _REVIEW):
        _install(tmp_path / "environment", name)
        _install(tmp_path / "relative-home", name)
    seen = _child("paths", record, tmp_path, tmp_path / "environment")
    for name in ("approval", "review"):
        assert not Path(seen[name]).is_relative_to(tmp_path)
        assert name + "_loaded" not in seen
        assert "no account record" in seen[name + "_refused"]


@pytest.mark.parametrize("spelling", ["~/k.pem", "~nobody/k.pem"])
def test_key_path_needing_user_expansion_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str,
) -> None:
    private = Ed25519PrivateKey.generate()
    (tmp_path / "k.pem").write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(approvals.ApprovalError, match="user expansion"):
        approvals.public_key_fingerprint(Path(spelling))
    with pytest.raises(approvals.ApprovalError, match="user expansion"):
        approvals.require_external_key_location(Path(spelling))


class _Owned:
    """A stat result reporting another owner; every other field is the real one."""

    def __init__(self, real: os.stat_result, owner: int) -> None:
        self._real, self.st_uid = real, owner

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


@pytest.mark.parametrize("entry", ["file", "directory"])
def test_key_owned_by_another_account_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str,
) -> None:
    _private, public = _install(tmp_path, _APPROVAL)
    expected = approvals.public_key_fingerprint(public)
    subject = public if entry == "file" else public.parent
    real_lstat, real_stat = os.lstat, os.stat
    owner = 0

    def fake(real):
        def inspect(path, *args, **kwargs):
            found = real(path, *args, **kwargs)
            if isinstance(path, (str, Path)) and Path(path) == subject:
                return _Owned(found, owner)
            return found
        return inspect

    monkeypatch.setattr(approvals.os, "lstat", fake(real_lstat))
    monkeypatch.setattr(approvals.os, "stat", fake(real_stat))
    # The administrator account may own the installed key or its directory.
    assert approvals.public_key_fingerprint(public) == expected
    owner = os.getuid() + 1
    with pytest.raises(approvals.ApprovalError, match="not owned by this account"):
        approvals.public_key_fingerprint(public)
