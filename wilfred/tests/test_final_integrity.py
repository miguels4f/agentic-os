import hashlib
import json
import shutil
from pathlib import Path

import pytest
from review_attestation import fixture_signer, write_signed_decision
from test_quality import _run_at_quality

from hyperwilfred_core import approvals, engine, quality, regression
from hyperwilfred_core.evaluators.code.pytest_evaluator import maintained_test_roots
from hyperwilfred_core.schemas import (
    BuildMode,
    BuildObjective,
    StepStatus,
    SubjectiveCriterion,
    SubjectiveEvaluation,
)


def _suite(root: Path) -> Path:
    tests = root / "tests"
    tests.mkdir(parents=True)
    (tests / "test_guard.py").write_text("def test_guard():\n    assert True\n")
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    return tests


def _review(root: Path, kind: str):
    _run_at_quality(root)
    if kind == "subjective":
        source = root / "subjective-input.json"
        source.write_text(SubjectiveEvaluation(
            evaluator="fixture", criteria=[SubjectiveCriterion(name="safe", score=5)]
        ).model_dump_json())
        return quality.record_subjective, {
            "repo_root": root, "run_id": "q", "result_file": str(source)}
    quality.critique_plan(repo_root=root, run_id="q")
    source = root / ".hw/q/quality/iter-0/critics/tests.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('{"critic":"fixture","lens":"tests","findings":[]}')
    return quality.record_critique, {
        "repo_root": root, "run_id": "q", "packet_id": "critic-0-tests",
        "result_file": str(source),
    }


def test_coherent_baseline_tamper_is_blocked_before_execution(tmp_path: Path, monkeypatch) -> None:
    tests = _suite(tmp_path)
    engine.plan(repo_root=tmp_path, run_id="i", mode=BuildMode.IMPROVE,
                product_dir=".", objective=BuildObjective(id="o", title="t", objective="x"))
    engine.regression_baseline(repo_root=tmp_path, run_id="i", target=tmp_path)
    paths = engine.RunPaths(tmp_path, "i")
    state = engine.load_state(paths)
    for step in ("comprehend", "locate", "implement"):
        state.step_status[step] = StepStatus.DONE
    engine.save_state(paths, state)
    payload = json.loads(paths.artifact(regression.BASELINE_FILE).read_text())
    payload.update(passing=0, passing_ids=[], test_file_hashes={})
    paths.artifact(regression.BASELINE_FILE).write_text(json.dumps(payload))
    monkeypatch.setattr(engine, "run_all", lambda *a, **k: pytest.fail("executed"))
    with pytest.raises(engine.GateFailure, match="digest pinned in run state"):
        engine.evaluate(repo_root=tmp_path, run_id="i", target=tmp_path,
                        step_id="regression")
    assert tests.exists()
    assert engine.load_state(paths).step_status["regression"] is StepStatus.FAILED


@pytest.mark.parametrize("mutation", ["delete", "rename", "skip", "weaken", "testpaths"])
def test_witness_mutations_fail(tmp_path: Path, mutation: str) -> None:
    test = _suite(tmp_path) / "test_guard.py"
    baseline = regression.capture_baseline(tmp_path)
    if mutation == "delete":
        test.unlink()
    elif mutation == "rename":
        test.rename(test.with_name("guard.py"))
    elif mutation == "skip":
        test.write_text("import pytest\n@pytest.mark.skip\ndef test_guard(): pass\n")
    elif mutation == "weaken":
        test.write_text("def test_guard():\n    assert True\n# weakened\n")
    else:
        (tmp_path / "other").mkdir()
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["other"]\n')
    assert not regression.compare(tmp_path, baseline).passed


@pytest.mark.parametrize("name,body", [
    ("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths=["tests"]'),
    ("pytest.ini", "[pytest]\ntestpaths=tests"), ("tox.ini", "[pytest]\ntestpaths=tests"),
    ("setup.cfg", "[tool:pytest]\ntestpaths=tests"),
])
def test_supported_pytest_configs_and_relocation(tmp_path: Path, name: str, body: str) -> None:
    source = tmp_path / "a"
    _suite(source)
    (source / "pyproject.toml").unlink()
    (source / name).write_text(body)
    baseline = regression.capture_baseline(source)
    moved = tmp_path / "b"
    shutil.copytree(source, moved)
    assert maintained_test_roots(source) == (source / "tests",)
    assert regression.compare(moved, baseline).passed


@pytest.mark.parametrize("kind", ["malformed", "traversal", "external", "ambiguous"])
def test_invalid_pytest_scope_fails_closed(tmp_path: Path, kind: str) -> None:
    _suite(tmp_path)
    baseline = regression.capture_baseline(tmp_path)
    config = tmp_path / "pyproject.toml"
    if kind == "malformed":
        config.write_text("[")
    elif kind in {"traversal", "external"}:
        value = "../outside" if kind == "traversal" else "/tmp"
        config.write_text(f'[tool.pytest.ini_options]\ntestpaths=["{value}"]')
    else:
        (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths=tests")
    with pytest.raises(ValueError):
        maintained_test_roots(tmp_path)
    assert not regression.compare(tmp_path, baseline).passed


@pytest.mark.parametrize("count,historical", [(809, False), (618, True)])
def test_preserved_legacy_shapes_migrate_without_recapture(
    tmp_path: Path, count: int, historical: bool,
) -> None:
    test = _suite(tmp_path) / "test_guard.py"
    ids = [f"tests/test_guard.py::test_{n}" for n in range(count)]
    sha = hashlib.sha256(test.read_bytes()).hexdigest()
    hashes = {"tests/test_guard.py": sha} if historical else {"test_guard.py": sha}
    if historical:
        hashes[".hw/archive/tests/test_guard.py"] = "0" * 64
    captured = tmp_path if historical else tmp_path / "tests"
    baseline = {"target": str(captured), "passing_ids": ids, "passing": count,
                "test_file_hashes": hashes}
    regression.validate_baseline(tmp_path, baseline)


@pytest.mark.parametrize("roots", [["../tests"], ["/tmp"], ["tests", "tests"]])
def test_versioned_baseline_roots_reject_traversal_external_and_duplicates(
    tmp_path: Path, roots: list[str],
) -> None:
    test = _suite(tmp_path) / "test_guard.py"
    baseline = {"schema_version": regression.BASELINE_VERSION, "target": ".",
                "test_roots": roots, "test_config_sha256": approvals.digest(roots),
                "passing_ids": ["tests/test_guard.py::test_guard"], "passing": 1,
                "test_file_hashes": {"tests/test_guard.py": hashlib.sha256(
                    test.read_bytes()).hexdigest()}}
    with pytest.raises(regression.RegressionError):
        regression.validate_baseline(tmp_path, baseline)


@pytest.mark.parametrize("kind", ["subjective", "critique"])
@pytest.mark.parametrize("decision", ["approve", "reject", "modify_scope"])
def test_review_decisions_are_consumed_before_interpretation(
    tmp_path: Path, kind: str, decision: str,
) -> None:
    operation, kwargs = _review(tmp_path, kind)
    assert operation(**kwargs, author="independent")["author"] == "adapter"
    with fixture_signer(tmp_path) as (private, public):
        first = operation(**kwargs, author="independent")
        token = write_signed_decision(first, private, public, decision=decision)
        if decision == "approve":
            assert operation(**kwargs, author="independent",
                             attestation_file=str(token))["author"] == "independent"
            assert operation(**kwargs, author="independent",
                             attestation_file=str(token))["author"] == "independent"
        else:
            with pytest.raises(quality.QualityError, match=f"was {decision}"):
                operation(**kwargs, author="independent", attestation_file=str(token))
        assert next(token.parent.glob("*.decision.consumed")).is_file()


@pytest.mark.parametrize("kind", ["subjective", "critique"])
@pytest.mark.parametrize("fault", ["pin", "receipt", "report", "state"])
def test_review_transaction_recovers_exact_token_after_each_write_boundary(
    tmp_path: Path, monkeypatch, kind: str, fault: str,
) -> None:
    operation, kwargs = _review(tmp_path, kind)
    with fixture_signer(tmp_path) as (private, public):
        first = operation(**kwargs, author="independent")
        token = write_signed_decision(first, private, public)
        if fault in {"pin", "report"}:
            original = quality.atomic_write_text
            suffix = ".public-key.pem" if fault == "pin" else (
                "subjective.json" if kind == "subjective" else "tests.json")
            monkeypatch.setattr(quality, "atomic_write_text", lambda path, text: (
                (_ for _ in ()).throw(OSError(fault)) if str(path).endswith(suffix)
                else original(path, text)))
        elif fault == "receipt":
            monkeypatch.setattr(approvals, "_atomic_create_text",
                                lambda *_: (_ for _ in ()).throw(OSError(fault)))
        else:
            monkeypatch.setattr(engine, "save_state",
                                lambda *_: (_ for _ in ()).throw(OSError(fault)))
        with pytest.raises((OSError, quality.QualityError)):
            operation(**kwargs, author="independent", attestation_file=str(token))
        monkeypatch.undo()
        approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH = public
        assert operation(**kwargs, author="independent",
                         attestation_file=str(token))["author"] == "independent"


@pytest.mark.parametrize("kind", ["subjective", "critique"])
@pytest.mark.parametrize("artifact,delete", [
    ("prepared", False), ("prepared", True), ("public-key", False),
    ("public-key", True), ("decision.consumed", False),
    ("decision.consumed", True), ("recorded", False), ("recorded", True),
])
def test_missing_or_tampered_attestation_artifacts_fail_closed(
    tmp_path: Path, kind: str, artifact: str, delete: bool,
) -> None:
    operation, kwargs = _review(tmp_path, kind)
    with fixture_signer(tmp_path) as (private, public):
        first = operation(**kwargs, author="independent")
        token = write_signed_decision(first, private, public)
        operation(**kwargs, author="independent", attestation_file=str(token))
        target = next(token.parent.glob(f"*.{artifact}*"))
        target.unlink() if delete else target.write_text("tampered")
        with pytest.raises(quality.QualityError):
            operation(**kwargs, author="independent", attestation_file=str(token))


def test_review_fixture_key_is_call_scoped_on_success_and_error(tmp_path: Path) -> None:
    original = approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH
    with fixture_signer(tmp_path):
        assert approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH != original
    assert approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH == original
    with pytest.raises(RuntimeError), fixture_signer(tmp_path):
        raise RuntimeError("fixture failure")
    assert approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH == original


def test_review_fixture_key_lives_outside_the_repository(tmp_path: Path) -> None:
    with fixture_signer(tmp_path) as (_private, public):
        assert public.is_file() and not public.resolve().is_relative_to(tmp_path.resolve())
        approvals.require_external_key_location(public, excluded_roots=[tmp_path])


@pytest.mark.parametrize("kind", ["subjective", "critique"])
@pytest.mark.parametrize(
    "field", ["run_id", "iteration", "packet_id", "lens", "result_sha256"],
)
def test_each_visible_review_binding_is_enforced(tmp_path: Path, kind: str, field: str) -> None:
    operation, kwargs = _review(tmp_path, kind)
    first = operation(**kwargs, author="independent")
    challenge_path = Path(first["attestation_challenge"])
    challenge = json.loads(challenge_path.read_text())
    challenge["evidence"] = [
        f"{field}:altered" if item.startswith(f"{field}:") else item
        for item in challenge["evidence"]]
    challenge["evidence_sha256"] = approvals.digest(sorted(challenge["evidence"]))
    challenge_path.write_text(json.dumps(challenge))
    with fixture_signer(tmp_path) as (private, public):
        token = write_signed_decision(first, private, public)
        with pytest.raises(quality.QualityError):
            operation(**kwargs, author="independent", attestation_file=str(token))


@pytest.mark.parametrize("kind", ["subjective", "critique"])
@pytest.mark.parametrize("field,value", [
    ("action", 7), ("evidence", "bad"), ("evidence", ["ok", 7]),
    ("action", "changed"), ("evidence", ["changed"]),
    ("action_sha256", "0" * 64), ("evidence_sha256", "0" * 64),
])
def test_visible_challenge_types_and_hashes_fail_closed(
    tmp_path: Path, kind: str, field: str, value: object,
) -> None:
    operation, kwargs = _review(tmp_path, kind)
    first = operation(**kwargs, author="independent")
    path = Path(first["attestation_challenge"])
    challenge = json.loads(path.read_text())
    challenge[field] = value
    path.write_text(json.dumps(challenge))
    with pytest.raises(quality.QualityError):
        operation(**kwargs, author="independent", attestation_file="missing-token")


@pytest.mark.parametrize("kind", ["subjective", "critique"])
@pytest.mark.parametrize("scenario", ["token", "expiry", "missing-key", "wrong-key", "edit"])
def test_review_token_key_expiry_and_record_integrity_matrix(
    tmp_path: Path, kind: str, scenario: str,
) -> None:
    operation, kwargs = _review(tmp_path, kind)
    with fixture_signer(tmp_path) as (private, public):
        first = operation(**kwargs, author="independent")
        old_nonce = json.loads(Path(first["attestation_challenge"]).read_text())["nonce"]
        if scenario == "wrong-key":
            with fixture_signer(tmp_path) as (other_private, other_public):
                token = write_signed_decision(first, other_private, other_public)
        else:
            token = write_signed_decision(first, private, public)
        if scenario == "token":
            value = json.loads(token.read_text())
            value["signature"] = "AAAA"
            token.write_text(json.dumps(value))
        elif scenario == "expiry":
            challenge_path = Path(first["attestation_challenge"])
            value = json.loads(challenge_path.read_text())
            value["expires_at"] = "2000-01-01T00:00:00+00:00"
            challenge_path.write_text(json.dumps(value))
            token = write_signed_decision(first, private, public)
        elif scenario == "missing-key":
            public.unlink()
        if scenario != "edit":
            with pytest.raises(quality.QualityError):
                operation(**kwargs, author="independent", attestation_file=str(token))
            if scenario == "expiry":
                renewed = operation(**kwargs, author="independent")
                renewed_nonce = json.loads(
                    Path(renewed["attestation_challenge"]).read_text())["nonce"]
                assert renewed_nonce != old_nonce
            return
        operation(**kwargs, author="independent", attestation_file=str(token))
        recorded = (tmp_path / ".hw/q/quality/iter-0/subjective.json"
                    if kind == "subjective" else Path(kwargs["result_file"]))
        recorded.write_text(recorded.read_text() + "\n")
        state = engine.load_state(engine.RunPaths(tmp_path, "q"))
        assert not quality.provenance_report(repo_root=tmp_path, run_id="q", state=state)["ok"]
