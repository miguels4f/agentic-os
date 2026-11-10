"""The review scope mode follows the run state recorded at planning time, not the directory.

A run recorded without a base commit digests every file under its product directory, even when
a git repository (or a first commit) appears after planning; a run with a base commit uses git.
The fail-closed git cases for base-commit runs are pinned in test_gate0_fix_review_scope.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyperwilfred_core import engine, quality
from hyperwilfred_core.quality import _changed_product_paths
from hyperwilfred_core.schemas import BuildObjective


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
        check=True, capture_output=True, text=True,
    )
    return completed.stdout.strip()


def _assert_outside_git(path: Path) -> None:
    probe = ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"]
    assert subprocess.run(probe, capture_output=True, check=False).returncode != 0


def _plan(repo: Path, product_dir: str = "product") -> Any:
    return engine.plan(
        repo_root=repo, objective=BuildObjective(id="o", title="t", objective="x"),
        run_id="mode-run", product_dir=product_dir,
    )


def _committed_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "product").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    (repo / "product" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "product/app.py")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_git_init_after_plan_outside_git_keeps_binding_every_product_file(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "product").mkdir(parents=True)
    _assert_outside_git(repo)
    (repo / "product" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    state = _plan(repo)
    assert state.base_commit is None
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "worker commit")
    assert _changed_product_paths(repo, None, product_dir="product") == ["product/app.py"]
    before = quality.product_snapshot_sha256(repo, state)
    (repo / "product" / "app.py").write_text("VALUE = 'rewritten'\nimport os\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "rewrite")
    assert _changed_product_paths(repo, None, product_dir="product") == ["product/app.py"]
    assert quality.product_snapshot_sha256(repo, state) != before


def test_run_planned_on_unborn_head_binds_every_product_file_after_commit(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "product").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    (repo / "product" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    state = _plan(repo)
    assert state.base_commit is None
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "first commit")
    before = quality.product_snapshot_sha256(repo, state)
    (repo / "product" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "rewrite")
    assert quality.product_snapshot_sha256(repo, state) != before


def test_run_without_base_commit_does_not_depend_on_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _base = _committed_repo(tmp_path)
    (repo / ".git" / "index").write_bytes(b"not an index")
    state = SimpleNamespace(run_id="mode-run", base_commit=None, product_dir="product")
    assert _changed_product_paths(repo, None, product_dir="product") == ["product/app.py"]
    corrupt = quality.product_snapshot_sha256(repo, state)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    assert quality.product_snapshot_sha256(repo, state) == corrupt




def test_run_with_base_commit_uses_git_scope(tmp_path: Path) -> None:
    repo, base = _committed_repo(tmp_path)
    (repo / "product" / "other.py").write_text("OTHER = 1\n", encoding="utf-8")
    assert _changed_product_paths(repo, base, product_dir="product") == ["product/other.py"]
    (repo / "product" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "change")
    both = ["product/app.py", "product/other.py"]
    assert _changed_product_paths(repo, base, product_dir="product") == both


def test_whole_repository_product_without_base_commit_skips_git_metadata(tmp_path: Path) -> None:
    repo, _base = _committed_repo(tmp_path)
    (repo / ".hw" / "mode-run").mkdir(parents=True)
    (repo / ".hw" / "mode-run" / "note.txt").write_text("x", encoding="utf-8")
    assert _changed_product_paths(repo, None, product_dir=".") == ["product/app.py"]
