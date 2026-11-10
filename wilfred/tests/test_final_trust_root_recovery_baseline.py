import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyperwilfred_core import cli, engine, regression
from hyperwilfred_core.run_store import atomic_write_text
from hyperwilfred_core.schemas import BuildMode, BuildObjective


def _run(repo: Path, run_id: str) -> engine.RunPaths:
    engine.plan(repo_root=repo, run_id=run_id, product_dir="product", mode=BuildMode.IMPROVE,
                objective=BuildObjective(id="b", title="Baseline", objective="Protect tests"))
    state = engine.load_state(paths := engine.RunPaths(repo, run_id))
    state.base_commit = "a" * 40
    engine.save_state(paths, state)
    return paths


def _payload(label: str = "one") -> bytes:
    return (json.dumps({"schema_version": regression.BASELINE_VERSION, "target": ".",
        "test_roots": ["."], "test_config_sha256": "0" * 64,
        "pytest_config_file_sha256": None, "control_file_hashes": {},
        "passing_ids": [f"test_{label}.py::test_ok"], "passing": 1,
        "test_file_hashes": {f"test_{label}.py": "1" * 64}}, indent=2) + "\n").encode()


def _source(repo: Path) -> engine.RunPaths:
    paths, content = _run(repo, "source"), _payload()
    atomic_write_text(paths.artifact(regression.BASELINE_FILE), content.decode())
    state = engine.load_state(paths)
    state.regression_baseline_sha256 = hashlib.sha256(content).hexdigest()
    engine.save_state(paths, state)
    return paths


def _inherit(repo: Path, source: engine.RunPaths) -> dict:
    supplied = repo / ".hw/protected.json"
    return engine.inherit_regression_baseline(
        repo_root=repo, run_id="destination", source_run_id="source",
        source_file=supplied if supplied.exists() else source.artifact(regression.BASELINE_FILE))


def test_api_and_cli_inherit_exact_bytes_without_pytest(tmp_path, monkeypatch, capsys) -> None:
    source, destination = _source(tmp_path), _run(tmp_path, "destination")
    protected = tmp_path / ".hw/protected.json"
    source.artifact(regression.BASELINE_FILE).replace(protected)
    source.artifact(regression.BASELINE_FILE).symlink_to(protected)
    monkeypatch.setattr(regression, "baseline_candidate", lambda _: pytest.fail("pytest ran"))
    first, second = _inherit(tmp_path, source), _inherit(tmp_path, source)
    state = engine.load_state(destination)
    assert destination.artifact(regression.BASELINE_FILE).read_bytes() == _payload()
    assert state.regression_baseline_sha256 == first["sha256"]
    assert state.regression_baseline_source_file == ".hw/protected.json"
    assert state.regression_baseline_recorded_at and second["recovered"] is True
    _run(tmp_path, "other")
    code = cli.main(["--repo", str(tmp_path), "regression-baseline-import", "--run-id", "other",
                     "--source-run", "source", "--source-file",
                     str(protected)])
    assert code == 0 and json.loads(capsys.readouterr().out)["sha256"] == first["sha256"]


@pytest.mark.parametrize("kind", ["external", "symlink", "directory", "hash", "existing", "commit"])
def test_inheritance_rejects_untrusted_inputs(tmp_path, kind: str) -> None:
    source, destination = _source(tmp_path), _run(tmp_path, "destination")
    supplied = source.artifact(regression.BASELINE_FILE)
    if kind == "external":
        supplied = tmp_path / "outside.json"
        supplied.write_bytes(_payload())
    elif kind == "symlink":
        real = tmp_path / "real.json"
        supplied.replace(real)
        supplied.symlink_to(real)
    elif kind == "directory":
        supplied = tmp_path / ".hw"
    elif kind == "hash":
        supplied.write_bytes(_payload("changed"))
    elif kind == "existing":
        destination.artifact(regression.BASELINE_FILE).write_bytes(_payload("existing"))
    else:
        state = engine.load_state(destination)
        state.base_commit = "b" * 40
        engine.save_state(destination, state)
    with pytest.raises(engine.EngineError):
        engine.inherit_regression_baseline(repo_root=tmp_path, run_id="destination",
            source_run_id="source", source_file=supplied)


@pytest.mark.parametrize("point", ["before_baseline_write", "after_baseline_write",
                                    "before_baseline_pin", "after_baseline_pin"])
def test_import_recovers_each_crash_boundary(tmp_path, monkeypatch, point: str) -> None:
    source, _destination = _source(tmp_path), _run(tmp_path, "destination")
    def crash(candidate: str) -> None:
        if candidate == point:
            raise RuntimeError("crash")
    monkeypatch.setattr(engine, "fault", crash)
    with pytest.raises(RuntimeError):
        _inherit(tmp_path, source)
    monkeypatch.setattr(engine, "fault", lambda _: None)
    assert _inherit(tmp_path, source)["recovered"] is True


def test_capture_recovers_without_recapturing_and_conflicts_block(tmp_path, monkeypatch) -> None:
    paths = _run(tmp_path, "destination")
    (tmp_path / "product").mkdir()
    monkeypatch.setattr(regression, "baseline_candidate",
                        lambda _: (json.loads(_payload()), SimpleNamespace(passed=True)))
    # The run carries an invented base commit and no repository; the base-commit comparison
    # (covered by test_gate0_s02i_baseline_capture.py) is replaced together with pytest.
    monkeypatch.setattr(regression, "require_base_commit_witness", lambda *_: None)
    monkeypatch.setattr(engine, "fault", lambda p: (_ for _ in ()).throw(RuntimeError())
                        if p == "after_baseline_write" else None)
    with pytest.raises(RuntimeError):
        engine.regression_baseline(repo_root=tmp_path, run_id="destination",
                                   target=tmp_path / "product")
    paths.artifact(regression.BASELINE_FILE).write_bytes(_payload("conflict"))
    monkeypatch.setattr(engine, "fault", lambda _: None)
    monkeypatch.setattr(regression, "baseline_candidate", lambda _: pytest.fail("recaptured"))
    with pytest.raises(engine.EngineError, match="conflicts"):
        engine.regression_baseline(repo_root=tmp_path, run_id="destination",
                                   target=tmp_path / "product")
