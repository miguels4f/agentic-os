"""REVIEW resolves its target safely in a repository that may be hostile.

Paths reported by git are only reviewed when they are plain files inside the review root (no
link anywhere on the way), and git runs without the reviewed repository's own configuration
being able to start a program (file-system monitor, hooks, content filters, external diff).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hyperwilfred_core import review
from hyperwilfred_core.schemas import ReviewTarget, ReviewTargetKind


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True)
    return done.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    (repo / "dir").mkdir()
    (repo / "dir" / "inner.txt").write_text("inner\n")
    _git(repo, "add", "a.txt", "dir/inner.txt")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _whole(repo: Path) -> list[str]:
    target = ReviewTarget(run_id="r", kind=ReviewTargetKind.PATHS)
    return review.resolve_target(repo_root=repo, target=target).resolved_paths


def _diff(repo: Path, diff_range: str) -> list[str]:
    target = ReviewTarget(run_id="r", kind=ReviewTargetKind.DIFF, diff_range=diff_range)
    return review.resolve_target(repo_root=repo, target=target).resolved_paths


def _plant_links(tmp_path: Path, repo: Path) -> None:
    """A committed file link and a tracked directory swapped for a link, both leaving the repo."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("not for review\n")
    (outside / "inner.txt").write_text("not for review either\n")
    (repo / "leak.txt").symlink_to(outside / "private.txt")
    (repo / "inside-link.txt").symlink_to("a.txt")  # a link, even to a reviewed file
    _git(repo, "add", "leak.txt", "inside-link.txt")
    _git(repo, "commit", "-q", "-m", "links")
    # The index still says dir/inner.txt is a file; on disk dir now leads outside the repo.
    (repo / "dir" / "inner.txt").unlink()
    (repo / "dir").rmdir()
    (repo / "dir").symlink_to(outside, target_is_directory=True)


def test_whole_repo_target_never_lists_a_link(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _plant_links(tmp_path, repo)
    assert _whole(repo) == ["a.txt"]


def test_diff_target_never_lists_a_link(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _plant_links(tmp_path, repo)
    assert _diff(repo, "HEAD~1..HEAD") == []  # only the two links changed
    (repo / "a.txt").write_text("two\n")
    # Against the base commit the working tree also reports dir/inner.txt (now behind a link).
    assert _diff(repo, "HEAD~1") == ["a.txt"]


def _hostile_config_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = _repo(tmp_path)
    marker = tmp_path / "repository-command-ran"
    script = tmp_path / "spy.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    script.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", str(script))
    _git(repo, "config", "diff.external", str(script))
    (repo / "a.txt").write_text("two\n")
    return repo, marker


def test_repository_fsmonitor_does_not_run_for_a_diff_target(tmp_path: Path) -> None:
    repo, marker = _hostile_config_repo(tmp_path)
    assert _diff(repo, "HEAD") == ["a.txt"]
    assert not marker.exists()


def test_repository_fsmonitor_does_not_run_for_a_whole_repo_target(tmp_path: Path) -> None:
    repo, marker = _hostile_config_repo(tmp_path)
    assert _whole(repo) == ["a.txt", "dir/inner.txt"]
    assert not marker.exists()


def test_repository_filter_does_not_run_for_a_diff_target(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker = tmp_path / "filter-ran"
    _git(repo, "config", "filter.spy.clean", f"touch '{marker}'; cat")
    _git(repo, "config", "filter.spy.process", f"touch '{marker}'")
    (repo / ".gitattributes").write_text("*.txt filter=spy\n")
    (repo / "a.txt").write_text("two\n")
    assert "a.txt" in _diff(repo, "HEAD")
    assert not marker.exists()


def test_inherited_git_variables_do_not_redirect_the_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    (other / "elsewhere.txt").write_text("x\n")
    _git(other, "add", "elsewhere.txt")
    monkeypatch.setenv("GIT_DIR", os.fspath(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", os.fspath(other))
    assert _whole(repo) == ["a.txt", "dir/inner.txt"]
