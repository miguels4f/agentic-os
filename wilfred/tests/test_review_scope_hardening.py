"""The review scope runs git without repository filters and binds the venv's start-up hooks."""

from __future__ import annotations

import subprocess
from pathlib import Path

from hyperwilfred_core import quality


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
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def test_repository_filter_commands_are_never_run_by_the_scope(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker = tmp_path / "filter-ran"
    _git(repo, "config", "filter.spy.clean", f"touch {marker}; cat")
    _git(repo, "config", "filter.spy.process", f"touch {marker}")
    (repo / ".gitattributes").write_text("*.txt filter=spy\n")
    (repo / "a.txt").write_text("two\n")
    base = _git(repo, "rev-parse", "HEAD")
    paths = quality._changed_product_paths(repo, base)
    assert "a.txt" in paths
    assert not marker.exists()


def test_global_git_configuration_does_not_steer_the_scope(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    marker = tmp_path / "global-filter-ran"
    config = tmp_path / "gitconfig"
    config.write_text(f'[filter "spy"]\n\tclean = touch {marker}; cat\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    (repo / ".gitattributes").write_text("*.txt filter=spy\n")
    (repo / "a.txt").write_text("two\n")
    quality._changed_product_paths(repo, _git(repo, "rev-parse", "HEAD"))
    assert not marker.exists()


def test_venv_startup_hooks_are_part_of_the_scope(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    site = repo / ".venv" / "lib" / "python3.13" / "site-packages"
    site.mkdir(parents=True)
    (repo / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (site / "evil.pth").write_text("import os\n")
    (site / "sitecustomize.py").write_text("print('hi')\n")
    (site / "ordinary_package").mkdir()
    (site / "ordinary_package" / "__init__.py").write_text("")
    (repo / ".gitignore").write_text(".venv/\n")
    paths = quality._changed_product_paths(repo, _git(repo, "rev-parse", "HEAD"))
    assert ".venv/lib/python3.13/site-packages/evil.pth" in paths
    assert ".venv/lib/python3.13/site-packages/sitecustomize.py" in paths
    assert ".venv/pyvenv.cfg" in paths
    assert not any("ordinary_package" in path for path in paths)


def test_submodule_filters_are_never_run_and_its_files_stay_in_scope(tmp_path: Path) -> None:
    inner = tmp_path / "inner"
    inner.mkdir()
    _git(inner, "init", "-q")
    _git(inner, "config", "user.email", "t@example.com")
    _git(inner, "config", "user.name", "t")
    (inner / "s.txt").write_text("one\n")
    _git(inner, "add", "s.txt")
    _git(inner, "commit", "-q", "-m", "inner")
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "-c", "protocol.file.allow=always", "submodule",
                    "add", "-q", str(inner), "sub"], check=True, capture_output=True)
    _git(repo, "commit", "-q", "-m", "add submodule")
    marker = tmp_path / "sub-filter-ran"
    _git(repo / "sub", "config", "filter.spy.clean", f"touch {marker}; cat")
    (repo / "sub" / ".gitattributes").write_text("*.txt filter=spy\n")
    (repo / "sub" / "s.txt").write_text("two\n")
    paths = quality._changed_product_paths(repo, _git(repo, "rev-parse", "HEAD"))
    assert not marker.exists()
    assert "sub/s.txt" in paths
