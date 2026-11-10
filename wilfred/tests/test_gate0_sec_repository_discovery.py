"""The repository that owns a legacy baseline target is found by git discovery from the target
and from every directory above it, not by looking for an entry with the usual name.

A repository planted as loose metadata files (no entry with the usual name), a plant that
lends only its work tree setting, and an owner that reports another work tree all fail
closed; ordinary layouts keep working."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hyperwilfred_core import engine, regression
from hyperwilfred_core.schemas import BuildMode, BuildObjective

STRICT = "import pytest\n\n\n@pytest.fixture\ndef tolerance() -> int:\n    return 0\n"
WEAK = "import pytest\n\n\n@pytest.fixture\ndef tolerance() -> int:\n    return 10**9\n"
TEST = ("from calc import add\n\n\ndef test_add(tolerance: int) -> None:\n"
        "    assert abs(add(1, 2) - 3) <= tolerance\n")
GOOD = "def add(a: int, b: int) -> int:\n    return a + b\n"
BROKEN = "def add(a: int, b: int) -> int:\n    return a * b\n"
PYPROJECT = '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["."]\n'
IGNORED = ".hw/\n__pycache__/\n.mypy_cache/\n.ruff_cache/\n.pytest_cache/\n"
IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.test")
LEGACY_OMITS = {"control_file_hashes", "pytest_config_file_sha256", "data_file_hashes"}
METADATA = ("HEAD", "objects", "refs", "config", "packed-refs")
SECOND = "more than one repository"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *IDENTITY, *arguments], check=True,
                          capture_output=True, text=True).stdout.strip()


def _layout(root: Path, conftest: str) -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / "calc.py").write_text(GOOD)
    (root / "tests" / "conftest.py").write_text(conftest)
    (root / "tests" / "test_calc.py").write_text(TEST)
    return root


def _commit(repo: Path) -> str:
    (repo / ".gitignore").write_text(IGNORED)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return _git(repo, "rev-parse", "HEAD")


def _owner(tmp_path: Path, inner: str) -> tuple[Path, Path, str]:
    """A repository holding the strict layout at *inner* and the permissive one at the path
    a shifted reading would use; returns (repository, target, base commit)."""
    owner = tmp_path / "mono"
    target = _layout(owner / inner, STRICT)
    _layout(owner if inner == "prod" else owner / "prod", WEAK)
    return owner, target, _commit(owner)


def _plant_loose(tmp_path: Path, source: Path | None, at: Path, work_tree: str | None, *,
                 bare: str = "false") -> None:
    """Make *at* itself a git directory: loose metadata files, no entry with the usual name.
    *source* None plants an empty repository."""
    store = tmp_path / f"seed-{len(list(tmp_path.glob('seed-*')))}.git"
    command = ["init", "-q", "--bare", str(store)] if source is None else [
        "clone", "-q", "--bare", str(source), str(store)]
    subprocess.run(["git", *command], check=True, capture_output=True)
    _git(store, "config", "core.bare", bare)
    if work_tree is not None:
        _git(store, "config", "core.worktree", work_tree)
    for name in METADATA:
        if (store / name).is_dir():
            shutil.copytree(store / name, at / name)
        elif (store / name).exists():
            shutil.copy(store / name, at / name)
    assert not (at / ".git").exists()


def _witness(target: Path, base: str) -> str | None:
    _config, controls = regression.legacy_control_witness(target, (target / "tests",), base)
    return controls.get("tests/conftest.py")


def _legacy(target: Path) -> dict:
    return {key: value for key, value in regression.capture_baseline(target).items()
            if key not in LEGACY_OMITS}


def _worker_edits(target: Path) -> None:
    (target / "calc.py").write_text(BROKEN)
    (target / "tests" / "conftest.py").write_text(WEAK)


PLACEMENTS = {"target": ("prod", "prod"), "intermediate": ("a/b/prod", "a/b")}


@pytest.mark.parametrize("placement", sorted(PLACEMENTS))
def test_loose_metadata_plant_makes_the_comparison_unavailable(
        tmp_path: Path, placement: str) -> None:
    inner, at = PLACEMENTS[placement]
    owner, target, base = _owner(tmp_path, inner)
    legacy = _legacy(target)
    assert regression.compare(target, legacy, base_commit=base).passed
    _worker_edits(target)
    refused = regression.compare(target, legacy, base_commit=base)
    assert not refused.passed and "control files changed" in refused.details
    _plant_loose(tmp_path, owner, owner / at, str(owner / at))
    assert _git(target, "rev-parse", "--show-toplevel") == str((owner / at).resolve())
    result = regression.compare(target, legacy, base_commit=base)
    assert not result.passed
    assert result.details.startswith("regression evidence unavailable: ")
    assert SECOND in result.details
    with pytest.raises(regression.RegressionError, match=SECOND):
        _witness(target, base)


def _legacy_run(repo: Path, product_dir: str) -> None:
    """Plan and capture run ``r``, then give it the baseline shape of an earlier engine."""
    engine.plan(repo_root=repo, run_id="r", product_dir=product_dir, mode=BuildMode.IMPROVE,
                objective=BuildObjective(id="o", title="t", objective="protect tests"))
    engine.regression_baseline(repo_root=repo, run_id="r", target=repo / product_dir)
    paths = engine.RunPaths(repo, "r")
    stored = paths.artifact(regression.BASELINE_FILE)
    payload = json.dumps({key: value for key, value in json.loads(stored.read_text()).items()
                          if key not in LEGACY_OMITS}, indent=2) + "\n"
    stored.write_text(payload)
    paths.artifact("regression_baseline.transaction.json").unlink()
    state = engine.load_state(paths)
    state.regression_baseline_sha256 = hashlib.sha256(payload.encode()).hexdigest()
    engine.save_state(paths, state)


def _gate(repo: Path, product_dir: str) -> dict[str, bool]:
    evaluation = engine.evaluate(repo_root=repo, run_id="r", target=repo / product_dir,
                                 step_id="regression")
    return {check["name"]: check["passed"] for check in evaluation["report"]["checks"]}


@pytest.mark.parametrize("placement", sorted(PLACEMENTS))
def test_loose_metadata_plant_fails_the_gate_with_the_true_repository_root(
        tmp_path: Path, placement: str) -> None:
    inner, at = PLACEMENTS[placement]
    owner, target, _base = _owner(tmp_path, inner)
    _legacy_run(owner, inner)
    _worker_edits(target)
    _plant_loose(tmp_path, owner, owner / at, str(owner / at))
    with pytest.raises(engine.GateFailure, match=SECOND):
        _gate(owner, inner)


@pytest.mark.parametrize("work_tree", ["owner-root", "relative-parent"])
def test_loose_plant_naming_another_work_tree_is_refused(tmp_path: Path, work_tree: str) -> None:
    owner, target, base = _owner(tmp_path, "a/b/prod")
    assert _witness(target, base) == _sha(STRICT)
    _plant_loose(tmp_path, owner, target, {"owner-root": str(owner), "relative-parent": ".."}[
        work_tree])
    with pytest.raises(regression.RegressionError, match=SECOND):
        _witness(target, base)


@pytest.mark.parametrize("form", ["loose-metadata", "git-file"])
def test_plant_without_the_base_commit_cannot_lend_its_work_tree(
        tmp_path: Path, form: str) -> None:
    """An empty planted repository naming a directory between the target and the owner as its
    work tree would have the owner's objects read at the shifted path."""
    owner, target, base = _owner(tmp_path, "a/b/prod")
    if form == "loose-metadata":
        _plant_loose(tmp_path, None, target, str(owner / "a" / "b"))
    else:
        store = tmp_path / "empty.git"
        subprocess.run(["git", "init", "-q", "--bare", str(store)], check=True)
        _git(store, "config", "core.bare", "false")
        _git(store, "config", "core.worktree", str(owner / "a" / "b"))
        (target / ".git").write_text(f"gitdir: {store}\n")
    assert _git(target, "rev-parse", "--show-toplevel") == str((owner / "a" / "b").resolve())
    with pytest.raises(regression.RegressionError):
        _witness(target, base)


def test_owner_reporting_another_work_tree_is_refused(tmp_path: Path) -> None:
    """The directory read as the repository root is the one the repository is discovered
    from, never one named by a work tree setting."""
    owner, target, base = _owner(tmp_path, "a/b/prod")
    assert _witness(target, base) == _sha(STRICT)
    _git(owner, "config", "core.worktree", str(owner / "a" / "b"))
    assert _git(target, "rev-parse", "--show-toplevel") == str((owner / "a" / "b").resolve())
    with pytest.raises(regression.RegressionError, match="work tree"):
        _witness(target, base)


def test_inherited_ceiling_cannot_hide_the_owner_from_a_loose_plant(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, target, base = _owner(tmp_path, "prod")
    _plant_loose(tmp_path, owner, target, str(target))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(target.resolve()))
    monkeypatch.setenv("GIT_DISCOVERY_ACROSS_FILESYSTEM", "0")
    with pytest.raises(regression.RegressionError, match=SECOND):
        _witness(target, base)


def test_every_directory_above_the_target_is_asked_through_the_hardened_runner(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _owner_root, target, base = _owner(tmp_path, "a/b/prod")
    calls: list[tuple[list[str], dict[str, str]]] = []
    real = subprocess.run

    def spy(argv, *args, **kwargs):
        calls.append((list(argv), kwargs["env"]))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(regression.subprocess, "run", spy)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(target.resolve()))
    assert _witness(target, base) == _sha(STRICT)
    question = ["rev-parse", "--show-toplevel", "--absolute-git-dir"]
    asked = [argv[argv.index("-C") + 1] for argv, _env in calls if argv[-3:] == question]
    resolved = target.resolve()
    assert asked == [str(directory) for directory in (resolved, *resolved.parents)]
    for argv, env in calls:
        assert argv[:2] == ["git", "--no-replace-objects"] and "-C" in argv
        assert not [name for name in env if name.startswith("GIT_CEILING")]
        assert env["GIT_NO_REPLACE_OBJECTS"] == "1" and env["LC_ALL"] == "C"


def test_git_failure_other_than_no_repository_fails_closed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, target, base = _owner(tmp_path, "prod")
    real = subprocess.run

    def failing_above(argv, *args, **kwargs):
        result = real(argv, *args, **kwargs)
        if argv[argv.index("-C") + 1] == str(tmp_path.resolve()) and "rev-parse" in argv:
            assert result.returncode == 128
            result.stderr = b"fatal: detected dubious ownership in repository\n"
        return result

    monkeypatch.setattr(regression.subprocess, "run", failing_above)
    with pytest.raises(regression.RegressionError, match="cannot read base_commit evidence"):
        _witness(target, base)


# --- layouts that keep working


def test_target_equal_to_the_repository_root_keeps_working(tmp_path: Path) -> None:
    repo = _layout(tmp_path / "repo", STRICT)
    base = _commit(repo)
    assert _witness(repo, base) == _sha(STRICT)
    assert regression.compare(repo, _legacy(repo), base_commit=base).passed


def test_repository_root_inside_an_unrelated_outer_repository_keeps_working(
        tmp_path: Path) -> None:
    """The operational layout: the checkout is the target and sits below a repository that
    does not hold its base commit."""
    outer = _layout(tmp_path / "outer", WEAK)
    repo = _layout(outer / "area" / "checkout", STRICT)
    base = _commit(repo)
    _commit(outer)
    assert _witness(repo, base) == _sha(STRICT)
    assert regression.compare(repo, _legacy(repo), base_commit=base).passed
    _legacy_run(repo, ".")
    checks = _gate(repo, ".")
    assert checks["regression-baseline"] is True and all(checks.values())
    (repo / "tests" / "conftest.py").write_text(WEAK)
    with pytest.raises(engine.GateFailure, match="control files changed"):
        _gate(repo, ".")


def test_linked_work_tree_outside_its_main_work_tree_keeps_working(tmp_path: Path) -> None:
    main = tmp_path / "main"
    _layout(main / "prod", STRICT)
    base = _commit(main)
    linked = tmp_path / "trees" / "slice"
    _git(main, "worktree", "add", "-q", "--detach", str(linked), base)
    assert (linked / ".git").is_file()
    for target in (linked / "prod", main / "prod"):
        assert _witness(target, base) == _sha(STRICT)
        assert regression.compare(target, _legacy(target), base_commit=base).passed


def test_submodule_keeps_working(tmp_path: Path) -> None:
    library = _layout(tmp_path / "library", STRICT)
    base = _commit(library)
    parent = _layout(tmp_path / "parent", WEAK)
    _commit(parent)
    _git(parent, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
         str(library), "vendor/library")
    target = parent / "vendor" / "library"
    assert (target / ".git").is_file() and _git(target, "config", "core.worktree")
    assert _witness(target, base) == _sha(STRICT)
    assert regression.compare(target, _legacy(target), base_commit=base).passed
