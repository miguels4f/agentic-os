"""A review key a constrained worker could place never promotes a quality review.

The quality path applies the same location rule as control reviews: a verification key inside
any ``.hw`` directory, inside the product directory, or inside the enclosing git work tree is
refused by ``record_subjective`` and ``record_critique`` and makes ``provenance_report`` fail
closed. Every key is generated inside the test's temporary directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from test_final_trust_root_recovery_trust import _key, _sign
from test_quality import _run_at_quality

from hyperwilfred_core import approvals, engine, quality
from hyperwilfred_core.schemas import CritiqueReport, SubjectiveCriterion, SubjectiveEvaluation

LOCATIONS = ["hw", "product", "product-is-root", "work-tree"]
OPERATIONS = ["subjective", "critique", "configured-default"]


def _layout(tmp_path: Path, where: str) -> tuple[Path, Path]:
    """Return a run parked at quality and a key location inside what a worker can write."""
    repo = tmp_path / "repo"
    (repo / "product").mkdir(parents=True)
    (repo / "product" / "code.py").write_text("VALUE = 1\n", encoding="utf-8")
    if where == "work-tree":
        identity = ["-c", "user.email=fixture@example.test", "-c", "user.name=Fixture"]
        for args in (["init", "-q"], ["add", "product"], [*identity, "commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True)
    _run_at_quality(repo, product_dir="." if where == "product-is-root" else "product")
    folder = {"hw": repo / ".hw", "product": repo / "product"}.get(where, repo)
    return repo, folder / "keys" / "review.pem"


def _request(repo: Path, operation: str, public: Path, monkeypatch) -> tuple:
    """Issue the challenge for one review and return (first, operation, kwargs)."""
    kwargs: dict = {"repo_root": repo, "run_id": "q", "author": "independent"}
    if operation == "critique":
        quality.critique_plan(repo_root=repo, run_id="q")
        source = repo / ".hw/q/quality/iter-0/critics/tests.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        report = CritiqueReport(critic="fixture", lens="tests", findings=[])
        source.write_text(report.model_dump_json(), encoding="utf-8")
        record, kwargs["packet_id"] = quality.record_critique, "critic-0-tests"
    else:
        source = repo / ".hw/q/subjective-input.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        criteria = [SubjectiveCriterion(name="trust", score=5)]
        source.write_text(SubjectiveEvaluation(evaluator="fixture", criteria=criteria)
                          .model_dump_json(), encoding="utf-8")
        record = quality.record_subjective
    kwargs["result_file"] = str(source)
    if operation == "configured-default":
        monkeypatch.setattr(approvals, "DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH", public)
    else:
        kwargs["review_public_key_path"] = public
    return record(**kwargs), record, kwargs


def _anchors(repo: Path) -> dict:
    quality_state = engine.load_state(engine.RunPaths(repo, "q")).quality
    return dict(quality_state.review_trust_anchors) if quality_state else {"missing": True}


@pytest.mark.parametrize("where", LOCATIONS)
@pytest.mark.parametrize("operation", OPERATIONS)
def test_repository_local_review_key_is_refused_when_recording(
    tmp_path: Path, monkeypatch, where: str, operation: str,
) -> None:
    repo, inside = _layout(tmp_path, where)
    private, public = _key(inside)
    first, record, kwargs = _request(repo, operation, public, monkeypatch)
    token = _sign(first, private, public)
    with pytest.raises(quality.QualityError, match="review key must"):
        record(**kwargs, attestation_file=str(token))
    assert _anchors(repo) == {}


@pytest.mark.parametrize("where", ["product-is-root", "work-tree"])
@pytest.mark.parametrize("operation", OPERATIONS)
def test_external_review_key_is_still_accepted_under_the_tightest_fences(
    tmp_path: Path, monkeypatch, where: str, operation: str,
) -> None:
    repo, _inside = _layout(tmp_path, where)
    private, public = _key(tmp_path / "external-keys" / "review.pem")
    first, record, kwargs = _request(repo, operation, public, monkeypatch)
    token = _sign(first, private, public)
    assert record(**kwargs, attestation_file=str(token))["author"] == "independent"
    assert len(_anchors(repo)) == 1


@pytest.mark.parametrize("where", LOCATIONS)
@pytest.mark.parametrize("source", ["explicit", "configured-default"])
def test_provenance_refuses_a_repository_local_copy_of_the_trusted_key(
    tmp_path: Path, monkeypatch, where: str, source: str,
) -> None:
    repo, inside = _layout(tmp_path, where)
    private, public = _key(tmp_path / "external-keys" / "review.pem")
    # The copy exists before review so it is part of the reviewed scope from the start.
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(public.read_bytes())
    operation = "subjective" if source == "explicit" else "configured-default"
    first, record, kwargs = _request(repo, operation, public, monkeypatch)
    record(**kwargs, attestation_file=str(_sign(first, private, public)))

    def report(key: Path) -> dict:
        state = engine.load_state(engine.RunPaths(repo, "q"))
        if source == "explicit":
            return quality.provenance_report(
                repo_root=repo, run_id="q", state=state, review_public_key_path=key)
        monkeypatch.setattr(approvals, "DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH", key)
        return quality.provenance_report(repo_root=repo, run_id="q", state=state)

    assert "subjective" in report(public)["independent"]
    refused = report(inside)
    assert "subjective" not in refused["independent"] and refused["ok"] is False
