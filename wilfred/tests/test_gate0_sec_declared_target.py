"""The target a run declared at plan time is compared as written and never through a link.

A donor run declared on a symbolic link cannot be made to match another run by re-pointing
the link, a declared target with a link at any component below the repository root obtains
no baseline, a source run must still hold its baseline transaction, and a run that inherited
before these rules existed keeps working.
"""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyperwilfred_core import engine, learning, regression
from hyperwilfred_core.run_store import atomic_write_text
from hyperwilfred_core.schemas import BuildMode, BuildObjective, ProposalStatus

TRANSACTION = "regression_baseline.transaction.json"
LINKED = "must not pass through a symbolic link"


def _payload() -> bytes:
    return (json.dumps({"schema_version": regression.BASELINE_VERSION, "target": ".",
        "test_roots": ["."], "test_config_sha256": "0" * 64,
        "pytest_config_file_sha256": None, "control_file_hashes": {},
        "passing_ids": ["test_one.py::test_ok"], "passing": 1,
        "test_file_hashes": {"test_one.py": "1" * 64}}, indent=2) + "\n").encode()


def _plan(repo: Path, run_id: str, product_dir: str) -> engine.RunPaths:
    engine.plan(repo_root=repo, run_id=run_id, product_dir=product_dir, mode=BuildMode.IMPROVE,
                objective=BuildObjective(id="d", title="Declared", objective="Protect tests"))
    state = engine.load_state(paths := engine.RunPaths(repo, run_id))
    state.base_commit = state.base_commit or "a" * 40
    engine.save_state(paths, state)
    return paths


def _repo(root: Path) -> Path:
    for name in ("product", "decoy", "nest/inner"):
        (root / name).mkdir(parents=True)
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _pytest_passes(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []

    def candidate(target: Path):
        calls.append(target)
        return json.loads(_payload()), SimpleNamespace(passed=True)

    monkeypatch.setattr(regression, "baseline_candidate", candidate)
    # The run carries an invented base commit and no repository; the base-commit comparison
    # (covered by test_gate0_s02i_baseline_capture.py) is replaced together with pytest.
    monkeypatch.setattr(regression, "require_base_commit_witness", lambda *_: None)
    return calls


def _no_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(regression, "baseline_candidate", lambda _: pytest.fail("pytest ran"))


def _capture(repo: Path, run_id: str, target: str) -> dict:
    return engine.regression_baseline(repo_root=repo, run_id=run_id, target=repo / target)


def _inherit(repo: Path, run_id: str, source_run_id: str) -> dict:
    source = engine.RunPaths(repo, source_run_id).artifact(regression.BASELINE_FILE)
    return engine.inherit_regression_baseline(
        repo_root=repo, run_id=run_id, source_run_id=source_run_id, source_file=source)


def _edit_state(paths: engine.RunPaths, **changes: str | None) -> None:
    state = engine.load_state(paths)
    for name, value in changes.items():
        setattr(state, name, value)
    engine.save_state(paths, state)


def _transaction(paths: engine.RunPaths) -> dict:
    return json.loads(paths.artifact(TRANSACTION).read_text())


def _refused_unchanged(repo: Path, message: str, call) -> None:
    before = _snapshot(repo / ".hw")
    with pytest.raises(engine.EngineError, match=message):
        call()
    assert _snapshot(repo / ".hw") == before


# --- a donor declared on a link cannot be re-pointed at another run's target


@pytest.mark.parametrize("transaction", ["kept", "deleted"])
@pytest.mark.parametrize("link_now", ["points at the root", "removed", "is the product"])
def test_donor_declared_on_a_repointed_link_is_refused(
    tmp_path, monkeypatch, transaction: str, link_now: str,
) -> None:
    repo = _repo(tmp_path)
    donor = _plan(repo, "donor", "decoy")
    _pytest_passes(monkeypatch)
    _capture(repo, "donor", "decoy")
    # What an engine that followed links recorded for a donor planned on ``lnk -> decoy``.
    _edit_state(donor, product_dir="lnk", deterministic_target="lnk")
    assert _transaction(donor)["request_target"] == "decoy"
    declared = "product" if link_now == "is the product" else "."
    if link_now != "removed":
        (repo / "lnk").symlink_to(declared, target_is_directory=True)
    if transaction == "deleted":
        donor.artifact(TRANSACTION).unlink()
    destination = _plan(repo, "destination", declared)
    _no_pytest(monkeypatch)
    _refused_unchanged(repo, LINKED if link_now != "removed" else "declare the same product",
                       lambda: _inherit(repo, "destination", "donor"))
    assert not destination.artifact(regression.BASELINE_FILE).exists()
    assert engine.load_state(destination).regression_baseline_sha256 is None


def test_a_directory_later_replaced_by_a_link_does_not_match_the_link_destination(
    tmp_path, monkeypatch,
) -> None:
    repo = _repo(tmp_path)
    _plan(repo, "donor", "decoy")
    _pytest_passes(monkeypatch)
    _capture(repo, "donor", "decoy")
    (repo / "decoy").rmdir()
    (repo / "decoy").symlink_to("product", target_is_directory=True)
    _plan(repo, "destination", "product")
    _refused_unchanged(repo, LINKED, lambda: _inherit(repo, "destination", "donor"))


# --- a declared target with a link component obtains no baseline


@pytest.mark.parametrize("declared", ["lnk", "nest/lnk", "lnk/inner", "nest/../lnk"])
def test_capture_refuses_a_declared_target_with_a_link_component(
    tmp_path, monkeypatch, declared: str,
) -> None:
    repo = _repo(tmp_path)
    (repo / "lnk").symlink_to("nest", target_is_directory=True)
    (repo / "nest" / "lnk").symlink_to("inner", target_is_directory=True)
    paths = _plan(repo, "linked", "product")
    _edit_state(paths, product_dir=declared, deterministic_target=declared)
    _no_pytest(monkeypatch)
    for target in (declared, "nest", "nest/inner"):
        _refused_unchanged(repo, LINKED, lambda target=target: _capture(repo, "linked", target))
    assert not paths.artifact(regression.BASELINE_FILE).exists()


def test_inherit_refuses_a_destination_declared_through_a_link(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    _plan(repo, "source", "product")
    _pytest_passes(monkeypatch)
    _capture(repo, "source", "product")
    destination = _plan(repo, "destination", "decoy")
    _edit_state(destination, product_dir="lnk", deterministic_target="lnk")
    (repo / "lnk").symlink_to("product", target_is_directory=True)
    _refused_unchanged(repo, LINKED, lambda: _inherit(repo, "destination", "source"))
    assert not destination.artifact(regression.BASELINE_FILE).exists()


def test_plan_refuses_an_improve_target_declared_through_a_link(tmp_path) -> None:
    repo = _repo(tmp_path)
    (repo / "lnk").symlink_to("decoy", target_is_directory=True)
    with pytest.raises(engine.EngineError, match=LINKED):
        _plan(repo, "linked", "lnk")
    assert not engine.RunPaths(repo, "linked").run_dir.exists()


def test_a_declared_target_is_compared_as_written_not_as_resolved(tmp_path) -> None:
    repo = _repo(tmp_path)
    state = engine.load_state(_plan(repo, "spelled", "nest/../product/."))
    assert engine._plan_declared_target(repo, state) == "product"
    state.product_dir = str(repo / "nest" / "inner")
    assert engine._plan_declared_target(repo, state) == "nest/inner"
    for escaping in ("..", "product/../..", str(repo.parent)):
        state.product_dir = escaping
        with pytest.raises(engine.EngineError, match="escapes the repository"):
            engine._plan_declared_target(repo, state)


def test_a_link_above_the_repository_root_is_not_a_declared_component(
    tmp_path, monkeypatch,
) -> None:
    repo = _repo(tmp_path / "real" / "repo")
    (tmp_path / "alias").symlink_to("real", target_is_directory=True)
    through_link = tmp_path / "alias" / "repo"
    paths = _plan(through_link, "above", "product")
    calls = _pytest_passes(monkeypatch)
    _capture(through_link, "above", "product")
    assert calls == [repo.resolve() / "product"]
    assert _transaction(paths)["request_target"] == "product"
    _plan(through_link, "next", "product")
    assert _inherit(through_link, "next", "above")["baseline"] == json.loads(_payload())


# --- a source run must still hold its baseline transaction


def test_inherit_refuses_a_source_run_without_its_transaction(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    source = _plan(repo, "source", "product")
    _pytest_passes(monkeypatch)
    _capture(repo, "source", "product")
    source.artifact(TRANSACTION).unlink()
    destination = _plan(repo, "destination", "product")
    _refused_unchanged(repo, "source run has no baseline transaction",
                       lambda: _inherit(repo, "destination", "source"))
    assert engine.load_state(destination).regression_baseline_sha256 is None


# --- a run that inherited before these rules keeps evaluating and donating


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_run_that_inherited_earlier_still_evaluates_and_donates(tmp_path) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
    (repo / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add() -> None:\n    assert add(1, 2) == 3\n")
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["."]\n')
    (repo / ".gitignore").write_text(
        ".hw/\n__pycache__/\n.mypy_cache/\n.ruff_cache/\n.pytest_cache/\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@example.test", "-c", "user.name=T", "add", ".")
    _git(repo, "-c", "user.email=t@example.test", "-c", "user.name=T", "commit", "-qm", "base")

    _plan(repo, "origin", ".")
    _capture(repo, "origin", ".")
    earlier = _plan(repo, "earlier", ".")
    _inherit(repo, "earlier", "origin")
    # The shape an earlier engine wrote: no target in the transaction, none pinned in state.
    atomic_write_text(earlier.artifact(TRANSACTION),
                      json.dumps({**_transaction(earlier), "request_target": ""}, indent=2) + "\n")
    _edit_state(earlier, deterministic_target=None)

    later = _plan(repo, "later", ".")
    donated = _inherit(repo, "later", "earlier")
    assert donated["sha256"] == engine.load_state(earlier).regression_baseline_sha256
    assert _transaction(later)["request_target"] == "."
    assert _inherit(repo, "earlier", "origin")["recovered"] is True
    assert _transaction(earlier)["request_target"] == ""

    evaluation = engine.evaluate(repo_root=repo, run_id="earlier", target=repo,
                                 step_id="regression")
    checks = {check["name"]: check["passed"] for check in evaluation["report"]["checks"]}
    assert checks["regression-baseline"] is True and all(checks.values())


# --- a promoted proposal declares the repository root


def test_promoted_run_declares_the_repository_root(hw_history: Path, monkeypatch) -> None:
    learning.learn(repo_root=hw_history)
    proposal_id = learning.list_proposals(hw_history)[0].proposal_id

    def approved(_repo_root, proposal):
        proposal.status = ProposalStatus.APPROVED
        return proposal

    monkeypatch.setattr(learning, "_resolve_review", approved)
    promoted = learning.promote(repo_root=hw_history, proposal_id=proposal_id)
    assert promoted["promoted"] is True
    state = engine.load_state(engine.RunPaths(hw_history, promoted["run_id"]))
    assert state.mode is BuildMode.IMPROVE
    assert state.product_dir == "." and state.deterministic_target == "."
    assert engine._plan_declared_target(hw_history, state) == "."
