"""The documented two-call review attestation protocol, driven through the command line.

Operators run ``hw record-subjective`` / ``hw record-critique`` with ``--author independent``,
have the external signer sign the issued challenge, then repeat the call with
``--attestation-file``.  These tests exercise that exact operator path through ``cli.main``:
argparse wiring, the command-line guard that refuses an attestation file without
``--author independent`` (exit 2, kind ``quality_error``, nothing recorded), and the full flow
verified with the installed DEFAULT review key, which the command line cannot override.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from review_attestation import fixture_signer, write_signed_decision
from test_quality import _run_at_quality

from hyperwilfred_core import approvals
from hyperwilfred_core.cli import main
from hyperwilfred_core.schemas import (
    CritiqueReport,
    ReviewAuthor,
    SubjectiveCriterion,
    SubjectiveEvaluation,
)

RUN = "q"


def _cli(capsys: pytest.CaptureFixture[str], repo: Path, *argv: str) -> tuple[int, dict]:
    capsys.readouterr()
    code = main(["--repo", str(repo), *argv])
    return code, json.loads(capsys.readouterr().out)


def _subjective_input(repo: Path) -> Path:
    evaluation = SubjectiveEvaluation(
        evaluator="t",
        criteria=[SubjectiveCriterion(name=f"c{i}", score=5) for i in range(3)],
        global_score=0,
        author=ReviewAuthor.INDEPENDENT,
    )
    path = repo / ".hw" / RUN / "_inputs" / "subj_in.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(evaluation.model_dump_json(), encoding="utf-8")
    return path


def _critique_input(repo: Path, lens: str) -> Path:
    report = CritiqueReport(critic="c", lens=lens, findings=[], author=ReviewAuthor.INDEPENDENT)
    path = repo / ".hw" / RUN / "quality" / "iter-0" / "critics" / f"{lens}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(), encoding="utf-8")
    return path


def _attestation_dir(repo: Path) -> Path:
    return repo / ".hw" / RUN / "review-attestations"


# --------------------------------------------------------------------------- the guard


@pytest.mark.parametrize("author", [None, "adapter"])
def test_record_subjective_attestation_file_requires_independent_author(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], author: str | None,
) -> None:
    _run_at_quality(tmp_path, run_id=RUN)
    inp = _subjective_input(tmp_path)
    token = tmp_path / "outside-token.json"
    token.write_text("{}", encoding="utf-8")
    argv = ["record-subjective", "--run-id", RUN, "--result-file", str(inp),
            "--attestation-file", str(token)]
    if author is not None:
        argv += ["--author", author]
    code, out = _cli(capsys, tmp_path, *argv)
    assert code == 2
    assert out["kind"] == "quality_error"
    assert "--attestation-file requires --author independent" in out["error"]
    # Refused before anything is recorded or any challenge is issued.
    assert not (tmp_path / ".hw" / RUN / "quality" / "iter-0" / "subjective.json").exists()
    assert not _attestation_dir(tmp_path).exists()


@pytest.mark.parametrize("author", [None, "adapter"])
def test_record_critique_attestation_file_requires_independent_author(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], author: str | None,
) -> None:
    _run_at_quality(tmp_path, run_id=RUN)
    code, _plan = _cli(capsys, tmp_path, "critique-plan", "--run-id", RUN)
    assert code == 0
    inp = _critique_input(tmp_path, "tests")
    before = inp.read_bytes()
    token = tmp_path / "outside-token.json"
    token.write_text("{}", encoding="utf-8")
    argv = ["record-critique", "--run-id", RUN, "--packet-id", "critic-0-tests",
            "--result-file", str(inp), "--attestation-file", str(token)]
    if author is not None:
        argv += ["--author", author]
    code, out = _cli(capsys, tmp_path, *argv)
    assert code == 2
    assert out["kind"] == "quality_error"
    assert "--attestation-file requires --author independent" in out["error"]
    assert inp.read_bytes() == before
    assert not _attestation_dir(tmp_path).exists()


# --------------------------------------------------------------------------- the full flow


def test_record_subjective_two_call_flow_through_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _run_at_quality(tmp_path, run_id=RUN)
    inp = _subjective_input(tmp_path)
    original = approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH
    with fixture_signer(tmp_path) as (private, public):
        assert not public.resolve().is_relative_to(tmp_path.resolve())
        base = ["record-subjective", "--run-id", RUN, "--result-file", str(inp),
                "--author", "independent"]
        code, first = _cli(capsys, tmp_path, *base)
        assert code == 0
        assert first["author"] == "adapter"  # a request alone never promotes the record
        assert Path(first["attestation_challenge"]).is_file()

        code, status = _cli(capsys, tmp_path, "quality-status", "--run-id", RUN)
        assert code == 0
        assert "subjective" in status["provenance"]["adapter_authored"]

        decision = write_signed_decision(first, private, public)
        code, second = _cli(capsys, tmp_path, *base, "--attestation-file", str(decision))
        assert code == 0
        assert second["author"] == "independent"

        code, status = _cli(capsys, tmp_path, "quality-status", "--run-id", RUN)
        assert code == 0
        assert status["provenance"]["independent"] == ["subjective"]
        assert "subjective" not in status["provenance"]["adapter_authored"]
    assert approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH == original


def test_record_critique_two_call_flow_through_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _run_at_quality(tmp_path, run_id=RUN)
    code, _plan = _cli(capsys, tmp_path, "critique-plan", "--run-id", RUN)
    assert code == 0
    inp = _critique_input(tmp_path, "tests")
    original = approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH
    with fixture_signer(tmp_path) as (private, public):
        assert not public.resolve().is_relative_to(tmp_path.resolve())
        base = ["record-critique", "--run-id", RUN, "--packet-id", "critic-0-tests",
                "--result-file", str(inp), "--author", "independent"]
        code, first = _cli(capsys, tmp_path, *base)
        assert code == 0
        assert first["author"] == "adapter"
        assert Path(first["attestation_challenge"]).is_file()

        decision = write_signed_decision(first, private, public)
        code, second = _cli(capsys, tmp_path, *base, "--attestation-file", str(decision))
        assert code == 0
        assert second["author"] == "independent"

        code, status = _cli(capsys, tmp_path, "quality-status", "--run-id", RUN)
        assert code == 0
        assert "tests" in status["provenance"]["independent"]
        assert "tests" not in status["provenance"]["adapter_authored"]
    assert approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH == original


def test_cli_second_call_is_refused_without_the_installed_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A token signed by a key that is not the installed default never promotes the record."""
    _run_at_quality(tmp_path, run_id=RUN)
    inp = _subjective_input(tmp_path)
    base = ["record-subjective", "--run-id", RUN, "--result-file", str(inp),
            "--author", "independent"]
    with fixture_signer(tmp_path) as (private, public):
        code, first = _cli(capsys, tmp_path, *base)
        assert code == 0
        decision = write_signed_decision(first, private, public)
    with fixture_signer(tmp_path):  # a different installed key
        code, out = _cli(capsys, tmp_path, *base, "--attestation-file", str(decision))
        assert code == 2
        assert out["kind"] == "quality_error"
        code, status = _cli(capsys, tmp_path, "quality-status", "--run-id", RUN)
        assert code == 0
        assert "subjective" not in status["provenance"]["independent"]
