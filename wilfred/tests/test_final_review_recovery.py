import json
from pathlib import Path

import pytest
from test_approval_boundary import _key, _signed
from test_quality import _run_at_quality

from hyperwilfred_core import approvals, quality, regression
from hyperwilfred_core.schemas import SubjectiveCriterion, SubjectiveEvaluation


def _review_input(repo: Path, kind: str) -> tuple[object, dict, dict]:
    _run_at_quality(repo)
    if kind == "subjective":
        source = repo / "subjective.json"
        source.write_text(SubjectiveEvaluation(
            evaluator="fixture", criteria=[SubjectiveCriterion(name="safe", score=5)]
        ).model_dump_json(), encoding="utf-8")
        operation = quality.record_subjective
        kwargs = {"repo_root": repo, "run_id": "q", "result_file": str(source)}
    else:
        quality.critique_plan(repo_root=repo, run_id="q")
        source = repo / ".hw/q/quality/iter-0/critics/tests.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('{"critic":"fixture","lens":"tests","findings":[]}')
        operation = quality.record_critique
        kwargs = {"repo_root": repo, "run_id": "q", "packet_id": "critic-0-tests",
                  "result_file": str(source)}
    first = operation(**kwargs, author="independent")
    return operation, kwargs, first


@pytest.mark.parametrize("kind", ["subjective", "critique"])
@pytest.mark.parametrize("field", ["run_id", "iteration", "packet_id", "lens", "result_sha256"])
def test_review_attestation_rejects_every_binding_change(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch, kind: str, field: str,
) -> None:
    operation, kwargs, first = _review_input(tmp_path, kind)
    challenge_path = Path(first["attestation_challenge"])
    challenge = json.loads(challenge_path.read_text())
    challenge["evidence"] = [
        f"{field}:wrong" if item.startswith(f"{field}:") else item
        for item in challenge["evidence"]
    ]
    challenge["evidence_sha256"] = approvals.digest(sorted(challenge["evidence"]))
    challenge_path.write_text(json.dumps(challenge))
    private, public = _key(tmp_path_factory.mktemp("external-keys"))
    monkeypatch.setattr(approvals, "DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH", public)
    decision = Path(first["attestation_decision"])
    _signed(decision, {"challenge": challenge}, private, public)
    with pytest.raises(quality.QualityError, match="attestation is invalid"):
        operation(**kwargs, author="independent", attestation_file=str(decision))


@pytest.mark.parametrize(
    ("field", "value"),
    [("action", 7), ("evidence", "not-list"), ("evidence", ["ok", 7]),
     ("action", "changed"), ("evidence", ["changed"])],
)
def test_challenge_types_and_visible_hashes_fail_closed(
    tmp_path: Path, field: str, value: object,
) -> None:
    approvals.issue_challenge(
        tmp_path, run_id="run", gate_id="gate", action="act", evidence=["proof"]
    )
    path = approvals.challenge_file(tmp_path, "gate")
    challenge = json.loads(path.read_text())
    challenge[field] = value
    path.write_text(json.dumps(challenge))
    with pytest.raises(approvals.ApprovalError):
        approvals.load_challenge(tmp_path, "gate")


def test_configured_suite_ignores_archives_and_narrows_legacy_baseline(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs/evidence").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    test = tmp_path / "tests/test_live.py"
    test.write_text("def test_live():\n    assert True\n", encoding="utf-8")
    (tmp_path / "docs/evidence/test_live.py").write_text("raise RuntimeError('archive')\n")
    baseline = regression.capture_baseline(tmp_path / "tests")
    baseline["passing_ids"] = [f"tests/{node}" for node in baseline["passing_ids"]]
    assert regression.compare(tmp_path, baseline).passed
    legacy = {**baseline, "target": str(tmp_path)}
    legacy["passing_ids"].append("docs/evidence/test_live.py::test_old")
    legacy["test_file_hashes"] = {
        **{f"tests/{path}": value for path, value in baseline["test_file_hashes"].items()},
        "docs/evidence/test_live.py": "0" * 64,
    }
    assert not regression.compare(tmp_path, legacy).passed


def test_only_canonical_current_critic_reports_are_loaded(tmp_path: Path) -> None:
    _run_at_quality(tmp_path)
    quality.critique_plan(repo_root=tmp_path, run_id="q")
    (critics := tmp_path / ".hw/q/quality/iter-0/critics").mkdir(parents=True)
    (critics / "security.json").write_text(
        '{"critic":"current","lens":"security","findings":[]}'
    )
    (critics / "security.worker.json").write_text(
        '{"critic":"stale","lens":"security","findings":['
        '{"id":"x","lens":"security","severity":"blocker","title":"stale"}]}'
    )
    reports = quality._read_critique_reports(tmp_path, "q", 0)
    assert [(report.lens, report.findings) for report in reports] == [("security", [])]
