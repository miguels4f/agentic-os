"""Boundary error types around the trusted review and approval keys.

An operating-system error while reading the trusted approval key must leave ``approvals`` as
``ApprovalError`` (callers such as the protected-action gate and the live-demo override only
handle that type), and the Python-level quality guard must refuse an attestation file that is not
paired with an independent author even when no command line sits in front of it. Every key and
run here is a temporary fixture.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_quality import _run_at_quality

from hyperwilfred_core import approvals, quality
from hyperwilfred_core.schemas import (
    CritiqueReport,
    ReviewAuthor,
    SubjectiveCriterion,
    SubjectiveEvaluation,
)

RUN = "q"
GUARD_MESSAGE = "--attestation-file requires author='independent'"


def _pem(path: Path) -> Path:
    public = Ed25519PrivateKey.generate().public_key()
    path.write_bytes(public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return path


def _failing(*_args, **_kwargs):
    raise OSError(errno.EIO, "simulated I/O error")


# --------------------------------------------------------------------------- key read errors


@pytest.mark.parametrize("call", ["read", "fstat"])
def test_io_error_while_reading_the_trusted_key_is_an_approval_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str,
) -> None:
    key = _pem(tmp_path / "trusted-public.pem")
    monkeypatch.setattr(os, call, _failing)
    with pytest.raises(approvals.ApprovalError) as caught:
        approvals._load_public_key(key)
    assert str(caught.value) == f"trusted approval public key unavailable at {key}"
    assert isinstance(caught.value.__cause__, OSError)


def test_public_fingerprint_keeps_the_approval_error_on_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _pem(tmp_path / "trusted-public.pem")
    expected = approvals.public_key_fingerprint(key)
    monkeypatch.setattr(os, "read", _failing)
    with pytest.raises(approvals.ApprovalError, match="public key unavailable"):
        approvals.public_key_fingerprint(key)
    monkeypatch.undo()
    assert approvals.public_key_fingerprint(key) == expected


# --------------------------------------------------------------------------- quality guard


def _attestation_dir(repo: Path) -> Path:
    return repo / ".hw" / RUN / "review-attestations"


def _token(repo: Path) -> Path:
    token = repo / "outside-token.json"
    token.write_text("{}", encoding="utf-8")
    return token


@pytest.mark.parametrize("author", [None, "adapter"])
def test_record_subjective_function_refuses_attestation_without_independent_author(
    tmp_path: Path, author: str | None,
) -> None:
    _run_at_quality(tmp_path, run_id=RUN)
    evaluation = SubjectiveEvaluation(
        evaluator="t",
        criteria=[SubjectiveCriterion(name=f"c{i}", score=5) for i in range(3)],
        global_score=0,
        author=ReviewAuthor.INDEPENDENT,
    )
    inp = tmp_path / ".hw" / RUN / "_inputs" / "subj_in.json"
    inp.parent.mkdir(parents=True, exist_ok=True)
    inp.write_text(evaluation.model_dump_json(), encoding="utf-8")
    with pytest.raises(quality.QualityError) as caught:
        quality.record_subjective(
            repo_root=tmp_path, run_id=RUN, result_file=str(inp), author=author,
            attestation_file=str(_token(tmp_path)),
        )
    assert str(caught.value) == GUARD_MESSAGE
    assert not (tmp_path / ".hw" / RUN / "quality" / "iter-0" / "subjective.json").exists()
    assert not _attestation_dir(tmp_path).exists()


@pytest.mark.parametrize("author", [None, "adapter"])
def test_record_critique_function_refuses_attestation_without_independent_author(
    tmp_path: Path, author: str | None,
) -> None:
    _run_at_quality(tmp_path, run_id=RUN)
    quality.critique_plan(repo_root=tmp_path, run_id=RUN)
    report = CritiqueReport(critic="c", lens="tests", findings=[], author=ReviewAuthor.INDEPENDENT)
    inp = tmp_path / ".hw" / RUN / "quality" / "iter-0" / "critics" / "tests.json"
    inp.parent.mkdir(parents=True, exist_ok=True)
    inp.write_text(report.model_dump_json(), encoding="utf-8")
    before = inp.read_bytes()
    with pytest.raises(quality.QualityError) as caught:
        quality.record_critique(
            repo_root=tmp_path, run_id=RUN, packet_id="critic-0-tests", result_file=str(inp),
            author=author, attestation_file=str(_token(tmp_path)),
        )
    assert str(caught.value) == GUARD_MESSAGE
    assert inp.read_bytes() == before
    assert not _attestation_dir(tmp_path).exists()
