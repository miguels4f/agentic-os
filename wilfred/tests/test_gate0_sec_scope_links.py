"""An ignored directory link is bound to the place it resolves to, judged by file identity.

A link that only the directory walk finds and that git ignores may point at a directory only
when the fully resolved location is a directory the walk itself binds.  The spelling written in
the link decides nothing: an exempt directory reached under another letter case or through a
second name is refused, and the resolved location is part of the snapshot entry, so re-pointing
any hop of the chain changes the snapshot.  The conditions around the rule (ignored by git,
nested repository entries, targets that cannot be inspected, the critic path list, names that
cannot be encoded) are pinned so that removing one makes a test fail.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyperwilfred_core import engine, quality
from hyperwilfred_core.quality import QualityError, _changed_product_paths
from hyperwilfred_core.schemas import BuildMode, BuildObjective

_ISOLATED = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
APP = "product/app.py"
LINK = "venv/vendored"


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | _ISOLATED
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
        check=True, capture_output=True, text=True, env=env,
    )
    return done.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "product").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    (repo / APP).write_text("VALUE = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text("venv/\n.hw/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    for name in ("first", "second"):
        (repo / "venv" / name).mkdir(parents=True)
        (repo / "venv" / name / "__init__.py").write_text(f"V = {name!r}\n", encoding="utf-8")
    return repo, _git(repo, "rev-parse", "HEAD")


def _state(base: str, product_dir: str = "product") -> Any:
    return SimpleNamespace(run_id="scope-run", base_commit=base, product_dir=product_dir)


def _snapshot(repo: Path, base: str) -> str:
    return quality.product_snapshot_sha256(repo, _state(base))


def _link(at: Path, target: str) -> None:
    at.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(at):
        at.unlink()
    os.symlink(target, at)


# --- an exempt directory is recognised by identity, not by the spelling in the link -------------


@pytest.mark.parametrize(
    ("exempt", "spelled"),
    [
        (".hw", "../.HW"), (".git", "../.GIT"), (".venv", "../.VENV"),
        ("product/.mypy_cache", "../product/.MYPY_CACHE"), (".pytest_cache", "../.Pytest_Cache"),
        (".venv", "first/../../.venV"), (".pytest_cache", "../.PYTEſT_CACHE"),
        (".venv", "../.venv"), (".ruff_cache", "../.ruff_cache"),
    ],
)
@pytest.mark.parametrize("by_name_alone", [False, True], ids=["identity+name", "name"])
def test_exempt_directory_spelled_in_another_case_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exempt: str, spelled: str,
    by_name_alone: bool,
) -> None:
    repo, base = _repo(tmp_path)
    if by_name_alone:  # the name rule must hold without help from the identity rule
        monkeypatch.setattr(quality.os.path, "samefile", lambda _a, _b: False)
    (repo / exempt / "evilpkg").mkdir(parents=True)
    (repo / exempt / "evilpkg" / "__init__.py").write_text("V = 'honest'\n", encoding="utf-8")
    (repo / ".git" / "info" / "exclude").write_text(".venv/\n.*_cache/\n", encoding="utf-8")
    if not (repo / "venv" / spelled / "evilpkg").is_dir():  # file system that keeps the case apart
        (repo / "venv" / spelled / "evilpkg").mkdir(parents=True)
    _link(repo / LINK, f"{spelled}/evilpkg")
    assert LINK in _changed_product_paths(repo, base, product_dir="product")
    with pytest.raises(QualityError, match="directory that is not reviewed"):
        _snapshot(repo, base)
    _link(repo / LINK, str(repo / "venv" / spelled / "evilpkg"))  # absolute spelling
    with pytest.raises(QualityError, match="directory that is not reviewed"):
        _snapshot(repo, base)


@pytest.mark.parametrize(
    ("second_name", "real", "target"),
    [(".venv", "envs", "../envs/lib"), ("venv/.mypy_cache", "venv/store", "store/lib")],
    ids=["top-level-environment", "cache"],
)
def test_exempt_directory_reached_under_a_second_name_is_refused(
    tmp_path: Path, second_name: str, real: str, target: str,
) -> None:
    repo, base = _repo(tmp_path)
    (repo / real / "lib").mkdir(parents=True)
    (repo / ".git" / "info" / "exclude").write_text(".venv\n", encoding="utf-8")
    _link(repo / second_name, os.path.relpath(repo / real, (repo / second_name).parent))
    _link(repo / LINK, target)
    with pytest.raises(QualityError, match="is a link to a directory that is not reviewed"):
        _snapshot(repo, base)


# --- the resolved location is bound -------------------------------------------------------------


@pytest.mark.parametrize("hop", ["{tmp}/outside/hop", "{repo}/.venv/hop", "{repo}/.git/hop"])
def test_re_pointing_an_unbound_hop_changes_the_snapshot(tmp_path: Path, hop: str) -> None:
    repo, base = _repo(tmp_path)
    (repo / ".git" / "info" / "exclude").write_text(".venv/\n", encoding="utf-8")
    hop_path = Path(hop.format(tmp=tmp_path, repo=repo))
    _link(hop_path, str(repo / "venv" / "first"))
    _link(repo / LINK, str(hop_path))
    first = _snapshot(repo, base)
    assert _snapshot(repo, base) == first
    _link(hop_path, str(repo / "venv" / "second"))
    assert os.readlink(repo / LINK) == str(hop_path)  # the reviewed link itself did not change
    assert _snapshot(repo, base) != first
    (tmp_path / "elsewhere").mkdir()
    _link(hop_path, str(tmp_path / "elsewhere"))
    with pytest.raises(QualityError, match="directory that is not reviewed"):
        _snapshot(repo, base)


def test_directory_reached_through_another_ignored_link_is_bound_where_it_resolves(
    tmp_path: Path,
) -> None:
    repo, base = _repo(tmp_path)
    for name in ("first", "second"):
        (repo / "venv" / name / "sub").mkdir()
    _link(repo / "venv" / "alias", "first")
    _link(repo / LINK, "alias/sub")
    relative = _snapshot(repo, base)
    _link(repo / LINK, str(repo / "venv" / "alias" / "sub"))  # absolute spelling, same place
    absolute = _snapshot(repo, base)
    assert relative != absolute  # the target text stays bound as well
    _link(repo / "venv" / "alias", str(tmp_path))  # the first link now leaves the repository
    (tmp_path / "sub").mkdir()
    with pytest.raises(QualityError, match="directory that is not reviewed"):
        _snapshot(repo, base)


def test_repository_given_through_a_second_name_still_accepts_its_own_directory(
    tmp_path: Path,
) -> None:
    repo, base = _repo(tmp_path)
    _link(tmp_path / "second-name", "repo")
    _link(repo / LINK, "first")
    assert _snapshot(tmp_path / "second-name", base)
    _link(repo / LINK, str(repo / "venv" / "first"))
    assert _snapshot(tmp_path / "second-name", base)


@pytest.mark.parametrize("target", [".", "..", "../venv", "{repo}", "{repo}/venv", "../.."])
def test_link_to_the_repository_root_or_to_a_directory_that_holds_it_is_refused(
    tmp_path: Path, target: str,
) -> None:
    repo, base = _repo(tmp_path)
    _link(repo / LINK, target.format(repo=repo))
    with pytest.raises(QualityError, match="directory that is not reviewed"):
        _snapshot(repo, base)


# --- conditions around the rule -----------------------------------------------------------------


def test_link_that_git_does_not_ignore_is_refused_even_when_status_names_it_differently(
    tmp_path: Path,
) -> None:
    repo, base = _repo(tmp_path)
    run_root = repo / "product"  # git reports paths from the top level, the walk from here
    _link(run_root / "plugin.py", "app.py")
    assert "plugin.py" in _changed_product_paths(run_root, base, product_dir=".")
    with pytest.raises(QualityError, match="symlink"):
        quality.product_snapshot_sha256(run_root, _state(base, "."))
    (repo / ".git" / "info" / "exclude").write_text("plugin.py\n", encoding="utf-8")
    quality.product_snapshot_sha256(run_root, _state(base, "."))  # ignored: bound, not refused


def test_nested_repository_entry_of_the_base_commit_is_not_a_file(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},product/nested")
    _git(repo, "commit", "-qm", "nested repository entry")
    (repo / "product" / "nested").mkdir()
    with_entry = _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "ls-tree", "HEAD", "product/nested").startswith("160000 commit")
    assert _changed_product_paths(repo, with_entry, product_dir="product") == [
        "venv/first/__init__.py", "venv/second/__init__.py",
    ]
    _snapshot(repo, with_entry)


def test_link_whose_target_cannot_be_inspected_is_refused(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    _link(repo / LINK, "../product/app.py/inner")  # a file where a directory is needed
    with pytest.raises(QualityError, match="target cannot be inspected"):
        _snapshot(repo, base)
    _link(repo / LINK, "../product/absent/inner")  # plainly absent: bound as dangling
    _snapshot(repo, base)


def test_critic_path_list_evaluates_ignored_links(tmp_path: Path) -> None:
    repo, _base = _repo(tmp_path)
    engine.plan(
        repo_root=repo, objective=BuildObjective(id="o", title="t", objective="x"),
        run_id="work", product_dir="product", mode=BuildMode.BUILD,
    )
    _link(repo / LINK, "first")
    assert LINK in quality._critique_target_paths(repo, "work")
    (repo / ".VENV" / "evilpkg").mkdir(parents=True)
    (repo / ".git" / "info" / "exclude").write_text(".venv/\n.VENV/\n", encoding="utf-8")
    _link(repo / LINK, "../.VENV/evilpkg")
    with pytest.raises(QualityError, match="directory that is not reviewed"):
        quality._critique_target_paths(repo, "work")


def test_path_that_only_git_status_reports_stays_in_the_scope(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    (repo / ".venv").mkdir()
    (repo / ".venv" / "planted.py").write_text("X = 1\n", encoding="utf-8")  # the walk skips it
    assert ".venv/planted.py" in _changed_product_paths(repo, base, product_dir="product")


def test_file_name_that_cannot_be_encoded_is_refused_as_a_quality_error(tmp_path: Path) -> None:
    repo, _base = _repo(tmp_path)
    blob = _git(repo, "rev-parse", "HEAD:product/app.py")
    name = os.fsdecode(b"product/bad\xff.py")
    _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
    _git(repo, "commit", "-qm", "name that is not valid text")
    base = _git(repo, "rev-parse", "HEAD")
    assert name in _changed_product_paths(repo, base, product_dir="product")
    with pytest.raises(QualityError, match="cannot be encoded"):
        _snapshot(repo, base)
