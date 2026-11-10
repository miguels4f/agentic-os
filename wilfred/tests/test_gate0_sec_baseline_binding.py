"""A regression baseline belongs to the product target its run declared at plan time.

Inheritance carries bytes between runs only when both declare the same target, a run that
declared none cannot obtain a baseline, and capture recovery trusts a prepared transaction
only when the run state already pins its digest.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyperwilfred_core import cli, engine, regression
from hyperwilfred_core.run_store import atomic_write_text
from hyperwilfred_core.schemas import BuildMode, BuildObjective

TRANSACTION = "regression_baseline.transaction.json"
UNDECLARED = "declared at plan time"


def _payload(label: str = "one") -> bytes:
    return (json.dumps({"schema_version": regression.BASELINE_VERSION, "target": ".",
        "test_roots": ["."], "test_config_sha256": "0" * 64,
        "pytest_config_file_sha256": None, "control_file_hashes": {},
        "passing_ids": [f"test_{label}.py::test_ok"], "passing": 1,
        "test_file_hashes": {f"test_{label}.py": "1" * 64}}, indent=2) + "\n").encode()


def _run(repo: Path, run_id: str, product_dir: str | None = "product") -> engine.RunPaths:
    for name in ("product", "decoy"):
        (repo / name).mkdir(parents=True, exist_ok=True)
    engine.plan(repo_root=repo, run_id=run_id, product_dir=product_dir, mode=BuildMode.IMPROVE,
                objective=BuildObjective(id="b", title="Binding", objective="Protect tests"))
    state = engine.load_state(paths := engine.RunPaths(repo, run_id))
    state.base_commit = "a" * 40
    engine.save_state(paths, state)
    return paths


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _pytest_passes(monkeypatch: pytest.MonkeyPatch, label: str = "one") -> list[Path]:
    calls: list[Path] = []

    def candidate(target: Path):
        calls.append(target)
        return json.loads(_payload(label)), SimpleNamespace(passed=True)

    monkeypatch.setattr(regression, "baseline_candidate", candidate)
    # These runs carry an invented base commit and no repository; the base-commit comparison
    # (covered by test_gate0_s02i_baseline_capture.py) is replaced together with pytest.
    monkeypatch.setattr(regression, "require_base_commit_witness", lambda *_: None)
    return calls


def _no_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(regression, "baseline_candidate", lambda _: pytest.fail("pytest ran"))


def _capture(repo: Path, run_id: str, target: str = "product") -> dict:
    return engine.regression_baseline(repo_root=repo, run_id=run_id, target=repo / target)


def _inherit(repo: Path, run_id: str, source_run_id: str) -> dict:
    source = engine.RunPaths(repo, source_run_id).artifact(regression.BASELINE_FILE)
    return engine.inherit_regression_baseline(
        repo_root=repo, run_id=run_id, source_run_id=source_run_id, source_file=source)


def _transaction(paths: engine.RunPaths) -> dict:
    return json.loads(paths.artifact(TRANSACTION).read_text())


def _edit_transaction(paths: engine.RunPaths, **changes: str) -> None:
    atomic_write_text(paths.artifact(TRANSACTION),
                      json.dumps({**_transaction(paths), **changes}, indent=2) + "\n")


def _edit_state(paths: engine.RunPaths, **changes: str | None) -> None:
    state = engine.load_state(paths)
    for name, value in changes.items():
        setattr(state, name, value)
    engine.save_state(paths, state)


# --- a baseline is inherited only between runs that declare the same target


def test_inherit_refuses_a_baseline_captured_for_another_declared_target(
    tmp_path, monkeypatch,
) -> None:
    _run(tmp_path, "source", product_dir="decoy")
    _pytest_passes(monkeypatch)
    _capture(tmp_path, "source", target="decoy")
    destination = _run(tmp_path, "destination", product_dir="product")
    before = _snapshot(tmp_path / ".hw")
    with pytest.raises(engine.EngineError, match="declare the same product target"):
        _inherit(tmp_path, "destination", "source")
    assert _snapshot(tmp_path / ".hw") == before
    assert not destination.artifact(regression.BASELINE_FILE).exists()
    assert engine.load_state(destination).regression_baseline_sha256 is None


@pytest.mark.parametrize("forged", ["state", "digest", "operation"])
def test_inherit_refuses_a_source_transaction_that_does_not_bind_the_target(
    tmp_path, monkeypatch, forged: str,
) -> None:
    captured_for = "decoy" if forged == "state" else "product"
    source = _run(tmp_path, "source", product_dir=captured_for)
    _pytest_passes(monkeypatch)
    _capture(tmp_path, "source", target=captured_for)
    if forged == "state":  # the run state is relabelled; its capture still names the decoy
        _edit_state(source, product_dir="product", deterministic_target="product")
    elif forged == "digest":
        _edit_transaction(source, sha256="0" * 64)
    else:
        _edit_transaction(source, operation="unknown")
    _run(tmp_path, "destination")
    before = _snapshot(tmp_path / ".hw")
    with pytest.raises(engine.EngineError, match="does not bind these bytes"):
        _inherit(tmp_path, "destination", "source")
    assert _snapshot(tmp_path / ".hw") == before


def test_inheritance_chain_with_one_declared_target_records_it(tmp_path, monkeypatch) -> None:
    runs = [_run(tmp_path, run_id) for run_id in ("first", "second", "third")]
    calls = _pytest_passes(monkeypatch)
    _capture(tmp_path, "first")
    _inherit(tmp_path, "second", "first")
    inherited = _inherit(tmp_path, "third", "second")
    assert len(calls) == 1 and inherited["baseline"] == json.loads(_payload())
    assert [_transaction(paths)["operation"] for paths in runs] == ["capture", "inherit", "inherit"]
    assert [_transaction(paths)["request_target"] for paths in runs] == ["product"] * 3
    assert {engine.load_state(paths).regression_baseline_sha256 for paths in runs} == {
        inherited["sha256"]}
    assert _inherit(tmp_path, "third", "second")["recovered"] is True


def test_inheritance_recorded_before_targets_were_bound_stays_usable(
    tmp_path, monkeypatch,
) -> None:
    origin, donor = _run(tmp_path, "origin", "."), _run(tmp_path, "donor", ".")
    _pytest_passes(monkeypatch)
    _capture(tmp_path, "origin", target=".")
    _inherit(tmp_path, "donor", "origin")
    # The shape an earlier engine wrote: no target in the transaction, none pinned in state.
    _edit_transaction(donor, request_target="")
    _edit_state(donor, deterministic_target=None)
    old_shape = _snapshot(donor.run_dir)
    assert _inherit(tmp_path, "donor", "origin")["recovered"] is True
    assert _snapshot(donor.run_dir) == old_shape  # the pinned bytes the gate reads are intact

    later = _run(tmp_path, "later", ".")
    assert _inherit(tmp_path, "later", "donor")["baseline"] == json.loads(_payload())
    assert _transaction(later)["request_target"] == "."
    assert _transaction(origin)["request_target"] == "."
    _run(tmp_path, "elsewhere", "product")
    with pytest.raises(engine.EngineError, match="declare the same product target"):
        _inherit(tmp_path, "elsewhere", "donor")


# --- a run that declared no target at plan time cannot obtain a baseline


@pytest.mark.parametrize("surface", ["api", "cli"])
def test_undeclared_improve_run_cannot_capture_a_baseline(
    tmp_path, monkeypatch, capsys, surface: str,
) -> None:
    paths = _run(tmp_path, "undeclared", product_dir=None)
    _no_pytest(monkeypatch)
    before = _snapshot(tmp_path / ".hw")
    for target in ("decoy", "product", "products/undeclared"):
        if surface == "api":
            with pytest.raises(engine.EngineError, match=UNDECLARED):
                _capture(tmp_path, "undeclared", target=target)
        else:
            assert cli.main(["--repo", str(tmp_path), "regression-baseline", "--run-id",
                             "undeclared", "--target", str(tmp_path / target)]) == 2
            assert UNDECLARED in capsys.readouterr().out
    assert _snapshot(tmp_path / ".hw") == before
    assert engine.load_state(paths).deterministic_target is None


def test_undeclared_improve_run_cannot_inherit_a_baseline(tmp_path, monkeypatch) -> None:
    _run(tmp_path, "source")
    _pytest_passes(monkeypatch)
    _capture(tmp_path, "source")
    paths = _run(tmp_path, "undeclared", product_dir=None)
    before = _snapshot(tmp_path / ".hw")
    with pytest.raises(engine.EngineError, match=UNDECLARED):
        _inherit(tmp_path, "undeclared", "source")
    assert _snapshot(tmp_path / ".hw") == before
    assert not paths.artifact(regression.BASELINE_FILE).exists()


def test_a_target_pinned_after_planning_is_not_a_declaration(tmp_path, monkeypatch) -> None:
    explicit = _run(tmp_path, "explicit", product_dir="products/explicit")
    (tmp_path / "products/explicit").mkdir(parents=True)
    _pytest_passes(monkeypatch)
    _capture(tmp_path, "explicit", target="products/explicit")
    assert _transaction(explicit)["request_target"] == "products/explicit"

    pinned = _run(tmp_path, "pinned", product_dir=None)
    _edit_state(pinned, deterministic_target="decoy")  # what a first evaluate call would pin
    _run(tmp_path, "source", product_dir="decoy")
    _capture(tmp_path, "source", target="decoy")
    _no_pytest(monkeypatch)
    before = _snapshot(tmp_path / ".hw")
    with pytest.raises(engine.EngineError, match=UNDECLARED):
        _capture(tmp_path, "pinned", target="decoy")
    with pytest.raises(engine.EngineError, match=UNDECLARED):
        _inherit(tmp_path, "pinned", "source")
    assert _snapshot(tmp_path / ".hw") == before


# --- capture recovery needs the digest pinned in run state


@pytest.mark.parametrize("status", ["prepared", "finalized"])
def test_planted_capture_transaction_is_refused_without_running_pytest(
    tmp_path, monkeypatch, status: str,
) -> None:
    paths = _run(tmp_path, "destination")
    planted = engine._baseline_request(
        operation="capture", run_id="destination", source_run_id="destination",
        source_file=".hw/destination/regression_baseline.json", request_target="product",
        payload=_payload("forged"))
    atomic_write_text(paths.artifact(TRANSACTION),
                      json.dumps({**planted, "status": status}, indent=2) + "\n")
    _no_pytest(monkeypatch)
    before = _snapshot(tmp_path / ".hw")
    with pytest.raises(engine.EngineError, match="before its SHA-256 was pinned") as refusal:
        _capture(tmp_path, "destination")
    assert TRANSACTION in str(refusal.value) and regression.BASELINE_FILE in str(refusal.value)
    assert _snapshot(tmp_path / ".hw") == before
    assert not paths.artifact(regression.BASELINE_FILE).exists()
    assert engine.load_state(paths).regression_baseline_sha256 is None


def _crash_at(monkeypatch: pytest.MonkeyPatch, point: str | None) -> None:
    def fault(candidate: str) -> None:
        if candidate == point:
            raise RuntimeError("crash")

    monkeypatch.setattr(engine, "fault", fault)


def test_capture_interrupted_before_the_pin_is_recaptured_only_after_cleanup(
    tmp_path, monkeypatch,
) -> None:
    for point in ("before_baseline_write", "after_baseline_write", "before_baseline_pin"):
        repo = tmp_path / point
        paths = _run(repo, "destination")
        calls = _pytest_passes(monkeypatch)
        _crash_at(monkeypatch, point)
        with pytest.raises(RuntimeError):
            _capture(repo, "destination")
        _crash_at(monkeypatch, None)
        assert len(calls) == 1 and engine.load_state(paths).regression_baseline_sha256 is None
        before = _snapshot(repo / ".hw")
        with pytest.raises(engine.EngineError, match="before its SHA-256 was pinned"):
            _capture(repo, "destination")
        assert len(calls) == 1 and _snapshot(repo / ".hw") == before, point

        paths.artifact(TRANSACTION).unlink()
        paths.artifact(regression.BASELINE_FILE).unlink(missing_ok=True)
        assert "recovered" not in _capture(repo, "destination")
        assert len(calls) == 2 and _transaction(paths)["status"] == "finalized"
        assert engine.load_state(paths).regression_baseline_sha256 == _transaction(paths)["sha256"]


def test_capture_interrupted_after_the_pin_recovers_but_a_swapped_one_does_not(
    tmp_path, monkeypatch,
) -> None:
    paths = _run(tmp_path, "destination")
    _pytest_passes(monkeypatch)
    _crash_at(monkeypatch, "after_baseline_pin")
    with pytest.raises(RuntimeError):
        _capture(tmp_path, "destination")
    _crash_at(monkeypatch, None)
    _no_pytest(monkeypatch)
    assert _transaction(paths)["status"] == "prepared"
    recovered = _capture(tmp_path, "destination")
    assert recovered["recovered"] is True and recovered["baseline"] == json.loads(_payload())
    assert _transaction(paths)["status"] == "finalized"

    # Same pinned state, but the transaction and the baseline file are replaced together.
    forged = engine._baseline_request(
        operation="capture", run_id="destination", source_run_id="destination",
        source_file=_transaction(paths)["source_file"], request_target="product",
        payload=_payload("forged"), recorded_at=_transaction(paths)["recorded_at"])
    atomic_write_text(paths.artifact(TRANSACTION), json.dumps(forged, indent=2) + "\n")
    atomic_write_text(paths.artifact(regression.BASELINE_FILE), _payload("forged").decode())
    before = _snapshot(tmp_path / ".hw")
    with pytest.raises(engine.EngineError, match="run state conflicts"):
        _capture(tmp_path, "destination")
    assert _snapshot(tmp_path / ".hw") == before

    _edit_state(paths, regression_baseline_sha256=None)  # the pin is gone: nothing vouches
    with pytest.raises(engine.EngineError, match="before its SHA-256 was pinned"):
        _capture(tmp_path, "destination")
