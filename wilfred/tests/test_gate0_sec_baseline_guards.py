"""Baseline guards: one capture, only before implementation, IMPROVE runs only."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyperwilfred_core import engine, regression
from hyperwilfred_core.run_store import atomic_write_text
from hyperwilfred_core.schemas import BuildMode, BuildObjective, StepStatus


def _payload(label: str = "one") -> bytes:
    return (json.dumps({"schema_version": regression.BASELINE_VERSION, "target": ".",
        "test_roots": ["."], "test_config_sha256": "0" * 64,
        "pytest_config_file_sha256": None, "control_file_hashes": {},
        "passing_ids": [f"test_{label}.py::test_ok"], "passing": 1,
        "test_file_hashes": {f"test_{label}.py": "1" * 64}}, indent=2) + "\n").encode()


def _run(repo: Path, run_id: str, mode: BuildMode = BuildMode.IMPROVE) -> engine.RunPaths:
    (repo / "product").mkdir(exist_ok=True)
    engine.plan(repo_root=repo, run_id=run_id, product_dir="product", mode=mode,
                objective=BuildObjective(id="g", title="Guards", objective="Protect tests"))
    state = engine.load_state(paths := engine.RunPaths(repo, run_id))
    state.base_commit = "a" * 40
    engine.save_state(paths, state)
    return paths


def _pinned(repo: Path, run_id: str, mode: BuildMode = BuildMode.IMPROVE) -> engine.RunPaths:
    paths, content = _run(repo, run_id, mode), _payload()
    atomic_write_text(paths.artifact(regression.BASELINE_FILE), content.decode())
    state = engine.load_state(paths)
    state.regression_baseline_sha256 = hashlib.sha256(content).hexdigest()
    engine.save_state(paths, state)
    return paths


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _capture(repo: Path) -> dict:
    return engine.regression_baseline(repo_root=repo, run_id="destination",
                                      target=repo / "product")


def _inherit(repo: Path, source: engine.RunPaths) -> dict:
    return engine.inherit_regression_baseline(
        repo_root=repo, run_id="destination", source_run_id="source",
        source_file=source.artifact(regression.BASELINE_FILE))


def _no_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(regression, "baseline_candidate", lambda _: pytest.fail("pytest ran"))


def test_first_capture_is_never_replaced_by_a_second_one(tmp_path, monkeypatch) -> None:
    paths = _run(tmp_path, "destination")
    monkeypatch.setattr(regression, "baseline_candidate",
                        lambda _: (json.loads(_payload()), SimpleNamespace(passed=True)))
    # The run carries an invented base commit and no repository; the base-commit comparison
    # (covered by test_gate0_s02i_baseline_capture.py) is replaced together with pytest.
    monkeypatch.setattr(regression, "require_base_commit_witness", lambda *_: None)
    _capture(tmp_path)
    monkeypatch.setattr(regression, "baseline_candidate",
                        lambda _: (json.loads(_payload("weaker")), SimpleNamespace(passed=True)))
    before = _snapshot(tmp_path / ".hw")
    assert _capture(tmp_path)["baseline"] == json.loads(_payload())   # the first bytes, no rerun
    assert _snapshot(tmp_path / ".hw") == before
    paths.artifact("regression_baseline.transaction.json").unlink()
    before = _snapshot(tmp_path / ".hw")
    with pytest.raises(engine.EngineError, match="immutable after its first capture"):
        _capture(tmp_path)
    assert _snapshot(tmp_path / ".hw") == before


@pytest.mark.parametrize("leftover", ["file", "pin"])
def test_capture_refuses_any_leftover_baseline_without_running_pytest(
    tmp_path, monkeypatch, leftover: str,
) -> None:
    paths = _pinned(tmp_path, "destination")
    if leftover == "file":
        state = engine.load_state(paths)
        state.regression_baseline_sha256 = None
        engine.save_state(paths, state)
    else:
        paths.artifact(regression.BASELINE_FILE).unlink()
    _no_pytest(monkeypatch)
    before = _snapshot(tmp_path / ".hw")
    with pytest.raises(engine.EngineError, match="immutable after its first capture"):
        _capture(tmp_path)
    assert _snapshot(tmp_path / ".hw") == before


@pytest.mark.parametrize("operation", ["capture", "inherit"])
@pytest.mark.parametrize(("step", "status"), [
    ("comprehend", StepStatus.DONE), ("comprehend", StepStatus.FAILED),
    ("locate", StepStatus.RUNNING), ("implement", StepStatus.DONE),
    ("regression", StepStatus.FAILED), ("ship", StepStatus.DONE),
])
def test_baseline_is_refused_once_the_run_moved_past_comprehend(
    tmp_path, monkeypatch, operation: str, step: str, status: StepStatus,
) -> None:
    source, paths = _pinned(tmp_path, "source"), _run(tmp_path, "destination")
    state = engine.load_state(paths)
    assert step in state.step_status
    state.step_status[step] = status
    engine.save_state(paths, state)
    _no_pytest(monkeypatch)
    before = _snapshot(tmp_path / ".hw")
    with pytest.raises(engine.EngineError, match="only before implementation"):
        _capture(tmp_path) if operation == "capture" else _inherit(tmp_path, source)
    assert _snapshot(tmp_path / ".hw") == before


@pytest.mark.parametrize("status", [StepStatus.PENDING, StepStatus.RUNNING])
def test_baseline_is_accepted_while_comprehend_is_open(tmp_path, status: StepStatus) -> None:
    source, paths = _pinned(tmp_path, "source"), _run(tmp_path, "destination")
    state = engine.load_state(paths)
    state.step_status["comprehend"] = status
    engine.save_state(paths, state)
    assert _inherit(tmp_path, source)["baseline"] == json.loads(_payload())


@pytest.mark.parametrize("operation", ["capture", "inherit"])
def test_baseline_destination_must_be_an_improve_run(tmp_path, monkeypatch, operation) -> None:
    source = _pinned(tmp_path, "source")
    _run(tmp_path, "destination", BuildMode.BUILD)
    _no_pytest(monkeypatch)
    before = _snapshot(tmp_path / ".hw")
    with pytest.raises(engine.EngineError, match="requires an IMPROVE run"):
        _capture(tmp_path) if operation == "capture" else _inherit(tmp_path, source)
    assert _snapshot(tmp_path / ".hw") == before


def test_baseline_source_must_be_an_improve_run(tmp_path) -> None:
    source = _pinned(tmp_path, "source", BuildMode.BUILD)
    _run(tmp_path, "destination")
    before = _snapshot(tmp_path / ".hw")
    with pytest.raises(engine.EngineError, match="baseline source must be an IMPROVE run"):
        _inherit(tmp_path, source)
    assert _snapshot(tmp_path / ".hw") == before


@pytest.mark.parametrize("leftover", ["file", "pin"])
def test_inherit_refuses_a_destination_that_already_has_a_baseline(
    tmp_path, leftover: str,
) -> None:
    source, paths = _pinned(tmp_path, "source"), _pinned(tmp_path, "destination")
    if leftover == "file":
        state = engine.load_state(paths)
        state.regression_baseline_sha256 = None
        engine.save_state(paths, state)
    else:
        paths.artifact(regression.BASELINE_FILE).unlink()
    before = _snapshot(tmp_path / ".hw")
    with pytest.raises(engine.EngineError, match="destination already has a regression baseline"):
        _inherit(tmp_path, source)
    assert _snapshot(tmp_path / ".hw") == before
