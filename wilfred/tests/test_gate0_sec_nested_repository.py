"""A repository planted inside a target that an outer repository owns cannot choose which part
of the base commit is read as the target; unrelated outer repositories and linked work trees
keep working."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hyperwilfred_core import regression

STRICT = "import pytest\n\n\n@pytest.fixture\ndef tolerance():\n    return 0\n"
WEAK = "import pytest\n\n\n@pytest.fixture\ndef tolerance():\n    return 10**9\n"
TEST = ("from calc import add\n\n\ndef test_add(tolerance):\n"
        "    assert abs(add(1, 2) - 3) <= tolerance\n")
IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.test")
LEGACY_OMITS = {"control_file_hashes", "pytest_config_file_sha256", "data_file_hashes"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *IDENTITY, *arguments], check=True,
                          capture_output=True, text=True).stdout.strip()


def _layout(root: Path, conftest: str) -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests" / "conftest.py").write_text(conftest)
    (root / "tests" / "test_calc.py").write_text(TEST)
    return root


def _commit(repo: Path) -> str:
    if not (repo / ".git").exists():
        _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return _git(repo, "rev-parse", "HEAD")


def _witness(target: Path, base: str) -> str | None:
    _config, controls = regression.legacy_control_witness(target, (target / "tests",), base)
    return controls.get("tests/conftest.py")


def _shared_layout(tmp_path: Path, inner: str = "prod") -> tuple[Path, Path, str]:
    """A repository whose root (permissive control) and *inner* directory (strict control)
    share the same test layout; returns (repository, target, base commit)."""
    outer = _layout(tmp_path / "mono", WEAK)
    target = _layout(outer / inner, STRICT)
    return outer, target, _commit(outer)


def _plant_git_file(tmp_path: Path, source: Path, work_tree: Path, at: Path) -> None:
    """Plant ``at/.git`` as a git file naming a private copy of *source*'s repository."""
    store = tmp_path / f"planted-{len(list(tmp_path.glob('planted-*')))}.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(source), str(store)], check=True)
    _git(store, "config", "core.bare", "false")
    _git(store, "config", "core.worktree", str(work_tree))
    (at / ".git").write_text(f"gitdir: {store}\n")


def _plant_copied_directory(tmp_path: Path, outer: Path, target: Path) -> None:
    shutil.copytree(outer / ".git", target / ".git")


def _plant_private_copy(tmp_path: Path, outer: Path, target: Path) -> None:
    _plant_git_file(tmp_path, outer, target, target)


def _plant_file_to_genuine_directory(tmp_path: Path, outer: Path, target: Path) -> None:
    (target / ".git").write_text(f"gitdir: {outer / '.git'}\n")


PLANTS = {"copied-directory": _plant_copied_directory,
          "git-file-to-private-copy": _plant_private_copy,
          "git-file-to-genuine-directory": _plant_file_to_genuine_directory}


@pytest.mark.parametrize("form", sorted(PLANTS))
def test_planted_repository_cannot_shift_the_base_prefix(tmp_path: Path, form: str) -> None:
    outer, target, base = _shared_layout(tmp_path)
    assert _witness(target, base) == _sha(STRICT)
    PLANTS[form](tmp_path, outer, target)
    assert _git(target, "rev-parse", "--show-toplevel") == str(target.resolve())
    with pytest.raises(regression.RegressionError, match="more than one repository"):
        _witness(target, base)
    with pytest.raises(regression.RegressionError, match="more than one repository"):
        with regression._base_scope(target, (target / "tests",), base):
            pass


@pytest.mark.parametrize("form", sorted(PLANTS))
def test_planted_repository_makes_the_comparison_unavailable(tmp_path: Path, form: str) -> None:
    outer, target, base = _shared_layout(tmp_path)
    legacy = {key: value for key, value in regression.capture_baseline(target).items()
              if key not in LEGACY_OMITS}
    assert regression.compare(target, legacy, base_commit=base).passed
    # The worker breaks the product, takes the permissive root-level control and plants a
    # repository so that the root-level files are read as the target's.
    (target / "calc.py").write_text("def add(a, b):\n    return a * b\n")
    (target / "tests" / "conftest.py").write_text(WEAK)
    refused = regression.compare(target, legacy, base_commit=base)
    assert not refused.passed and "control files changed" in refused.details
    PLANTS[form](tmp_path, outer, target)
    result = regression.compare(target, legacy, base_commit=base)
    assert not result.passed
    assert result.details.startswith("regression evidence unavailable: ")
    assert "more than one repository" in result.details


def test_plant_at_an_intermediate_depth_behind_an_unrelated_repository(tmp_path: Path) -> None:
    outer = tmp_path / "mono"
    target = _layout(outer / "a" / "b" / "prod", STRICT)
    _layout(outer / "prod", WEAK)
    base = _commit(outer)
    assert _witness(target, base) == _sha(STRICT)
    _plant_git_file(tmp_path, outer, outer / "a" / "b", outer / "a" / "b")
    # An unrelated repository between the plant and the owner must not end the search.
    decoy = _layout(tmp_path / "decoy", WEAK)
    _commit(decoy)
    _plant_git_file(tmp_path, decoy, outer / "a", outer / "a")
    assert _git(target, "rev-parse", "--show-toplevel") == str((outer / "a" / "b").resolve())
    with pytest.raises(regression.RegressionError, match="more than one repository"):
        _witness(target, base)


def test_plant_whose_work_tree_is_above_the_owner(tmp_path: Path) -> None:
    """The planted repository may name any work tree: the search starts at the target, not at
    the top level the plant reports."""
    outer = tmp_path / "mono"
    target = _layout(outer / "prod", STRICT)
    _layout(outer / "mono" / "prod", WEAK)
    base = _commit(outer)
    assert _witness(target, base) == _sha(STRICT)
    _plant_git_file(tmp_path, outer, tmp_path, target)
    assert _git(target, "rev-parse", "--show-toplevel") == str(tmp_path.resolve())
    with pytest.raises(regression.RegressionError, match="more than one repository"):
        _witness(target, base)


def test_inherited_variables_cannot_hide_the_owner(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outer, target, base = _shared_layout(tmp_path)
    _plant_private_copy(tmp_path, outer, target)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(target.resolve()))
    monkeypatch.setenv("GIT_DIR", str(target / ".git"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "nowhere"))
    with pytest.raises(regression.RegressionError, match="more than one repository"):
        _witness(target, base)


def test_unusable_repository_entry_above_the_target_fails_closed(tmp_path: Path) -> None:
    outer, target, base = _shared_layout(tmp_path)
    (tmp_path / ".git").write_text("gitdir: /nonexistent/place\n")
    with pytest.raises(regression.RegressionError):
        _witness(target, base)


def _spied(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, str] | None]]:
    calls: list[tuple[list[str], dict[str, str] | None]] = []
    real = subprocess.run

    def spy(argv, *args, **kwargs):
        calls.append((list(argv), kwargs.get("env")))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(regression.subprocess, "run", spy)
    return calls


def test_target_inside_an_unrelated_outer_repository_keeps_working(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The outer repository has the same layout with a permissive control but does not hold the
    # base commit of the inner one (the layout of the operational checkout).
    outer = _layout(tmp_path / "outer", WEAK)
    target = _layout(outer / "checkout" / "prod", STRICT)
    base = _commit(outer / "checkout")
    _commit(outer)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(outer / "checkout"))
    calls = _spied(monkeypatch)
    assert _witness(target, base) == _sha(STRICT)
    # The outer repository was asked, explicitly and through the hardened runner.
    asked = [argv for argv, _env in calls if str(outer.resolve()) in argv]
    assert asked and all(argv[argv.index(str(outer.resolve())) - 1] == "-C" for argv in asked)
    for argv, env in calls:
        assert argv[:2] == ["git", "--no-replace-objects"]
        assert env is not None and "GIT_CEILING_DIRECTORIES" not in env
        assert env["GIT_NO_REPLACE_OBJECTS"] == "1" and env["GIT_CONFIG_GLOBAL"] == os.devnull
    legacy = {key: value for key, value in regression.capture_baseline(target).items()
              if key not in LEGACY_OMITS}
    assert regression.compare(target, legacy, base_commit=base).passed


def test_linked_work_tree_inside_an_unrelated_outer_repository_keeps_working(
        tmp_path: Path) -> None:
    main = tmp_path / "main"
    _layout(main / "prod", STRICT)
    base = _commit(main)
    outer = _layout(tmp_path / "outer", WEAK)
    _commit(outer)
    linked = outer / "trees" / "slice"
    _git(main, "worktree", "add", "-q", "--detach", str(linked), base)
    assert (linked / ".git").is_file()
    assert _witness(linked / "prod", base) == _sha(STRICT)
    assert _witness(main / "prod", base) == _sha(STRICT)


def test_work_tree_nested_inside_its_own_repository_is_refused(tmp_path: Path) -> None:
    """Stated limit: a linked work tree below the main work tree of the same repository cannot
    be told apart from a plant, so the base witness is unavailable there."""
    main = tmp_path / "main"
    _layout(main / "prod", STRICT)
    base = _commit(main)
    linked = main / "trees" / "slice"
    _git(main, "worktree", "add", "-q", "--detach", str(linked), base)
    with pytest.raises(regression.RegressionError, match="more than one repository"):
        _witness(linked / "prod", base)
