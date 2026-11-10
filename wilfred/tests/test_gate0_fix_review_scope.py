"""The review scope fails closed when the changed product paths cannot be determined.

For a run with a base commit a failing ``git status`` (or a missing git executable) must raise
instead of binding the product snapshot to an empty file list.  A run without a base commit
covers every regular file under the product directory, so rewriting the product changes it.
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
from hyperwilfred_core.schemas import BuildObjective


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
        check=True, capture_output=True,
    )


def _state(base_commit: str | None = None) -> Any:
    return SimpleNamespace(run_id="scope-run", base_commit=base_commit, product_dir="product")


def _head(repo: Path) -> str:
    probe = ["git", "-C", str(repo), "rev-parse", "HEAD"]
    return subprocess.run(probe, capture_output=True, check=True, text=True).stdout.strip()


def _assert_outside_git(path: Path) -> None:
    probe = ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"]
    assert subprocess.run(probe, capture_output=True, check=False).returncode != 0


def _repo_with_corrupt_index(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "product").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    (repo / "product" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "product/app.py")
    _git(repo, "commit", "-qm", "base")
    base = _head(repo)
    (repo / ".git" / "index").write_bytes(b"not an index")
    failing = ["git", "-C", str(repo), "status", "--porcelain=v1"]
    assert subprocess.run(failing, capture_output=True, check=False).returncode != 0
    return repo, base


def test_failing_git_status_inside_repository_raises_with_base_commit(
    tmp_path: Path,
) -> None:
    repo, base = _repo_with_corrupt_index(tmp_path)
    with pytest.raises(QualityError, match="working-tree review scope"):
        _changed_product_paths(repo, base)
    with pytest.raises(QualityError, match="working-tree review scope"):
        quality.product_snapshot_sha256(repo, _state(base))


def test_missing_git_executable_raises_instead_of_empty_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / "product").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    (repo / "product" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "product/app.py")
    _git(repo, "commit", "-qm", "base")
    base = _head(repo)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    with pytest.raises(QualityError, match="git"):
        quality.product_snapshot_sha256(repo, _state(base))
    outside = tmp_path / "outside"
    (outside / "product").mkdir(parents=True)
    with pytest.raises(QualityError, match="git"):
        quality.product_snapshot_sha256(outside, _state(base))


def test_snapshot_outside_git_tracks_every_product_file(tmp_path: Path) -> None:
    _assert_outside_git(tmp_path)
    product = tmp_path / "product"
    (product / "pkg").mkdir(parents=True)
    (product / "pkg" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    state = engine.plan(
        repo_root=tmp_path, objective=BuildObjective(id="o", title="t", objective="x"),
        run_id="scope-run", product_dir="product",
    )
    assert state.base_commit is None
    first = quality.product_snapshot_sha256(tmp_path, state)
    (product / "pkg" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    rewritten = quality.product_snapshot_sha256(tmp_path, state)
    assert rewritten != first
    assert _changed_product_paths(tmp_path, product_dir="product") == ["product/pkg/app.py"]
    (product / "new.py").write_text("NEW = True\n", encoding="utf-8")
    added = quality.product_snapshot_sha256(tmp_path, state)
    assert added != rewritten
    # Engine state under .hw is never part of the product scope.
    (tmp_path / ".hw" / "scope-run" / "note.txt").write_text("x", encoding="utf-8")
    assert quality.product_snapshot_sha256(tmp_path, state) == added


def test_snapshot_outside_git_refuses_symlink_under_product(tmp_path: Path) -> None:
    _assert_outside_git(tmp_path)
    product = tmp_path / "product"
    product.mkdir()
    outside_file = tmp_path / "elsewhere.py"
    outside_file.write_text("SECRET = 1\n", encoding="utf-8")
    os.symlink(outside_file, product / "link.py")
    with pytest.raises(QualityError, match="symlink"):
        quality.product_snapshot_sha256(tmp_path, _state())


def test_outside_git_without_declared_product_dir_fails_closed(tmp_path: Path) -> None:
    _assert_outside_git(tmp_path)
    with pytest.raises(QualityError, match="outside a git work tree"):
        _changed_product_paths(tmp_path)
