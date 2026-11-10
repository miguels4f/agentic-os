"""Review authority bound to the pinned deterministic scope written by a real evaluate.

Every other quality attestation test parks its run at the quality step without evaluating, so
only the legacy scope branch runs there. These tests drive ``engine.evaluate`` (with the
deterministic checks replaced by a passing fixture report) so the pinned scope digest and the
verification evidence exist, record an attested review, then prove each fail-closed rejection:
stale product bytes, partial deterministic evidence and removed deterministic evidence.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from review_attestation import fixture_signer, record_attested, write_signed_decision
from test_quality import _run_at_quality

from hyperwilfred_core import engine, quality
from hyperwilfred_core.evaluators.verification import (
    CheckKind,
    CheckResult,
    Severity,
    VerificationReport,
)
from hyperwilfred_core.schemas import (
    CritiqueReport,
    StepStatus,
    SubjectiveCriterion,
    SubjectiveEvaluation,
)

RUN_ID = "q"


def _git_base(repo: Path) -> None:
    """Create history only inside this test's disposable fixture repository."""
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (
        ("user.email", "fixture@example.test"), ("user.name", "Fixture"),
        ("commit.gpgsign", "false"),
    ):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)


def _passing_report(*_args: Any, **_kwargs: Any) -> VerificationReport:
    return VerificationReport(
        task_id="o", run_id=RUN_ID, completed_at="2026-09-29T00:00:00Z",
        checks=[CheckResult(
            kind=CheckKind.TEST, name="fixture", passed=True, severity=Severity.INFO,
        )],
        blocks_merge=False,
    )


def _pinned_run(repo: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, engine.RunPaths]:
    """A git-backed run whose deterministic gate really ran and pinned the current bytes."""
    product = repo / "product"
    product.mkdir()
    source = product / "code.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git_base(repo)
    paths = _run_at_quality(repo, run_id=RUN_ID)
    source.write_text("VALUE = 2\n", encoding="utf-8")

    monkeypatch.setattr(engine, "run_all", _passing_report)
    assert engine.evaluate(repo_root=repo, run_id=RUN_ID, target=product)["passed"] is True

    state = engine.load_state(paths)
    assert state.deterministic_evidence_emitted is True
    assert state.step_status["evaluate"] is StepStatus.DONE
    pinned = paths.artifact("deterministic-scope.sha256")
    assert pinned.read_text(encoding="utf-8").strip() == quality.product_snapshot_sha256(
        repo, state,
    )
    assert (paths.run_dir / "evidence" / "verification.json").is_file()
    return source, paths


def _record_subjective(repo: Path) -> dict[str, Any]:
    result = repo / ".hw" / RUN_ID / "subjective-input.json"
    result.write_text(SubjectiveEvaluation(
        evaluator="fixture", criteria=[SubjectiveCriterion(name="current bytes", score=5)],
    ).model_dump_json(), encoding="utf-8")
    return record_attested(
        quality.record_subjective, repo=repo,
        repo_root=repo, run_id=RUN_ID, result_file=str(result),
    )


def _presigned_critique(repo: Path, lens: str = "correctness") -> dict[str, Any]:
    """Obtain a signed approval for one critique now; the caller submits it later."""
    packets = quality.critique_plan(repo_root=repo, run_id=RUN_ID)["critics"]
    packet = next(item for item in packets if item["lens"] == lens)
    result = repo / packet["result_path"]
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_text(
        CritiqueReport(critic="fixture", lens=lens, findings=[]).model_dump_json(),
        encoding="utf-8",
    )
    kwargs: dict[str, Any] = {
        "repo_root": repo, "run_id": RUN_ID, "packet_id": packet["packet_id"],
        "result_file": str(result), "author": "independent",
    }
    with fixture_signer(repo) as (private, public):
        first = quality.record_critique(**kwargs, review_public_key_path=public)
        assert first["author"] == "adapter"
        decision = write_signed_decision(first, private, public)
    return {**kwargs, "attestation_file": str(decision), "review_public_key_path": public}


def _subjective_independent(repo: Path) -> bool:
    state = engine.load_state(engine.RunPaths(repo, RUN_ID))
    report = quality.provenance_report(repo_root=repo, run_id=RUN_ID, state=state)
    return "subjective" in report["independent"]


def _scope(repo: Path, packet_id: str = "subjective-0") -> str:
    state = engine.load_state(engine.RunPaths(repo, RUN_ID))
    return quality._review_scope_sha256(repo, RUN_ID, state, packet_id)


def test_pinned_scope_accepts_fresh_evidence_and_binds_the_verification_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, paths = _pinned_run(tmp_path, monkeypatch)
    pinned_scope = _scope(tmp_path)
    assert _record_subjective(tmp_path)["author"] == "independent"
    assert _subjective_independent(tmp_path)
    presigned = _presigned_critique(tmp_path)
    assert quality.record_critique(**presigned)["author"] == "independent"

    verification = paths.run_dir / "evidence" / "verification.json"
    original = verification.read_bytes()
    verification.write_bytes(original + b"\n")
    assert _scope(tmp_path) != pinned_scope
    assert not _subjective_independent(tmp_path)
    verification.write_bytes(original)
    assert _subjective_independent(tmp_path)


def test_stale_product_snapshot_rejects_record_and_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _paths = _pinned_run(tmp_path, monkeypatch)
    assert _record_subjective(tmp_path)["author"] == "independent"
    presigned = _presigned_critique(tmp_path)

    source.write_text("VALUE = 3\n", encoding="utf-8")
    with pytest.raises(quality.QualityError, match="deterministic gate is stale"):
        quality.record_critique(**presigned)
    with pytest.raises(quality.QualityError, match="deterministic gate is stale"):
        _record_subjective(tmp_path)
    with pytest.raises(quality.QualityError, match="deterministic gate is stale"):
        _scope(tmp_path)
    assert not _subjective_independent(tmp_path)

    # the rejection is exactly the byte drift: restoring the evaluated bytes restores authority
    source.write_text("VALUE = 2\n", encoding="utf-8")
    assert _subjective_independent(tmp_path)
    assert quality.record_critique(**presigned)["author"] == "independent"


@pytest.mark.parametrize("removed", ["verification", "pinned"])
def test_partial_deterministic_evidence_rejects_record_and_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, removed: str,
) -> None:
    _source, paths = _pinned_run(tmp_path, monkeypatch)
    assert _record_subjective(tmp_path)["author"] == "independent"
    presigned = _presigned_critique(tmp_path)

    target = {
        "verification": paths.run_dir / "evidence" / "verification.json",
        "pinned": paths.run_dir / "deterministic-scope.sha256",
    }[removed]
    target.unlink()
    with pytest.raises(quality.QualityError, match="deterministic review evidence is partial"):
        quality.record_critique(**presigned)
    with pytest.raises(quality.QualityError, match="deterministic review evidence is partial"):
        _record_subjective(tmp_path)
    with pytest.raises(quality.QualityError, match="deterministic review evidence is partial"):
        _scope(tmp_path)
    assert not _subjective_independent(tmp_path)


def test_removed_deterministic_evidence_rejects_record_and_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, paths = _pinned_run(tmp_path, monkeypatch)
    assert _record_subjective(tmp_path)["author"] == "independent"
    presigned = _presigned_critique(tmp_path)

    (paths.run_dir / "evidence" / "verification.json").unlink()
    (paths.run_dir / "deterministic-scope.sha256").unlink()
    state = engine.load_state(paths)
    assert state.deterministic_evidence_emitted is True
    assert state.step_status["evaluate"] is StepStatus.DONE  # the legacy binding would accept
    with pytest.raises(quality.QualityError, match="deterministic review evidence was removed"):
        quality.record_critique(**presigned)
    with pytest.raises(quality.QualityError, match="deterministic review evidence was removed"):
        _record_subjective(tmp_path)
    with pytest.raises(quality.QualityError, match="deterministic review evidence was removed"):
        _scope(tmp_path)
    assert not _subjective_independent(tmp_path)
