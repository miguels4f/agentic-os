"""A regression baseline file whose digest was never pinned in run state fails closed.

Historical IMPROVE runs may carry a ``regression_baseline.json`` written before the engine
pinned its SHA-256 in run state. The regression gate must refuse such a file instead of
comparing against it, and it must never execute a check or pin the digest on the fly.
"""

import json
from pathlib import Path

import pytest

from hyperwilfred_core import engine, regression
from hyperwilfred_core.schemas import BuildMode, BuildObjective, RunStatus, StepStatus


def _unpinned_improve_run(root: Path) -> engine.RunPaths:
    tests = root / "tests"
    tests.mkdir(parents=True)
    (tests / "test_guard.py").write_text("def test_guard():\n    assert True\n")
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    engine.plan(repo_root=root, run_id="u", mode=BuildMode.IMPROVE, product_dir=".",
                objective=BuildObjective(id="o", title="t", objective="x"))
    paths = engine.RunPaths(root, "u")
    # A well-formed, honest baseline written directly, bypassing the pinning transaction.
    baseline = regression.capture_baseline(root)
    paths.artifact(regression.BASELINE_FILE).write_text(json.dumps(baseline))
    state = engine.load_state(paths)
    assert state.regression_baseline_sha256 is None
    for step in ("comprehend", "locate", "implement"):
        state.step_status[step] = StepStatus.DONE
    engine.save_state(paths, state)
    return paths


def test_unpinned_baseline_turns_regression_gate_red_without_running_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _unpinned_improve_run(tmp_path)
    baseline_bytes = paths.artifact(regression.BASELINE_FILE).read_bytes()

    def forbidden(*_args, **_kwargs):
        pytest.fail("a check ran against an unpinned regression baseline")

    monkeypatch.setattr(engine, "run_all", forbidden)
    monkeypatch.setattr(regression, "compare", forbidden)
    monkeypatch.setattr(regression, "verified_semantic_transitions", forbidden)

    for _attempt in range(2):
        with pytest.raises(engine.GateFailure, match="digest is missing"):
            engine.evaluate(repo_root=tmp_path, run_id="u", target=tmp_path,
                            step_id="regression")
        state = engine.load_state(paths)
        assert state.step_status["regression"] is StepStatus.FAILED
        assert state.status is RunStatus.GATE_FAILED
        # The refusal never pins the legacy bytes, so a retry stays red.
        assert state.regression_baseline_sha256 is None

    report = json.loads(paths.artifact("verification.json").read_text())
    assert report["blocks_merge"] is True
    failed = [c for c in report["checks"] if not c["passed"]]
    assert [c["name"] for c in failed] == ["regression-baseline"]
    assert "baseline digest is missing from run state" in failed[0]["details"]
    assert paths.artifact(regression.BASELINE_FILE).read_bytes() == baseline_bytes
    assert not paths.artifact("deterministic-scope.sha256").exists()
