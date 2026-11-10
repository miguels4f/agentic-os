"""The review scope is bound to the bytes on disk, not to git bookkeeping.

Index flags, ignore rules, a file-system monitor hook, a clean filter, a replace ref or an
inherited git environment must not hide a file from the changed-path list or from the product
snapshot.  Only the top-level engine store, git metadata and virtual environment, and the tool
cache directories, stay outside.  The status parser is pinned on renames, copies and odd names.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyperwilfred_core import quality
from hyperwilfred_core.quality import QualityError, _changed_product_paths

_ISOLATED = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
APP = "product/app.py"


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | _ISOLATED
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
        check=True, capture_output=True, text=True, env=env,
    )
    return done.stdout.strip()


def _repo(tmp_path: Path, *init_args: str) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "product" / ".pytest_cache").mkdir(parents=True)
    _git(tmp_path, "init", "-q", *init_args, str(repo))
    (repo / APP).write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "product" / ".pytest_cache" / "kept.txt").write_text("kept\n", encoding="utf-8")
    os.symlink("app.py", repo / "product" / "alias.py")
    (repo / ".gitignore").write_text("*.gen.py\n__pycache__/\nbuild/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _state(base: str) -> Any:
    return SimpleNamespace(run_id="scope-run", base_commit=base, product_dir="product")


def _write(repo: Path, relative: str, text: str) -> str:
    (repo / relative).parent.mkdir(parents=True, exist_ok=True)
    (repo / relative).write_text(text, encoding="utf-8")
    return relative


def _skip_worktree(repo: Path, _tmp: Path) -> str:
    _git(repo, "update-index", "--skip-worktree", APP)
    return _write(repo, APP, "VALUE = 'skip'\n")


def _assume_unchanged(repo: Path, _tmp: Path) -> str:
    _git(repo, "update-index", "--assume-unchanged", APP)
    return _write(repo, APP, "VALUE = 'assume'\n")


def _info_exclude(repo: Path, _tmp: Path) -> str:
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("product/hidden.py\n", encoding="utf-8")
    return _write(repo, "product/hidden.py", "HIDDEN = 1\n")


def _committed_ignore_rule(repo: Path, _tmp: Path) -> str:
    return _write(repo, "product/secret.gen.py", "HIDDEN = 1\n")


def _nested_ignore_file(repo: Path, _tmp: Path) -> str:
    _write(repo, "product/pkg/.gitignore", "*\n")
    return _write(repo, "product/pkg/hidden.py", "HIDDEN = 1\n")


def _excludes_file(repo: Path, tmp: Path) -> str:
    (tmp / "excludes").write_text("hidden.py\n", encoding="utf-8")
    _git(repo, "config", "core.excludesFile", str(tmp / "excludes"))
    return _write(repo, "product/hidden.py", "HIDDEN = 1\n")


def _silent_monitor_hook(repo: Path, tmp: Path) -> str:
    hook = tmp / "monitor-hook.sh"
    hook.write_text("#!/bin/sh\nprintf 'token\\0'\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", str(hook))
    _git(repo, "status", "--porcelain")  # records the monitor's answer in the index
    return _write(repo, APP, "VALUE = 'monitor'\n")


def _clean_filter(repo: Path, tmp: Path) -> str:
    shutil.copyfile(repo / APP, tmp / "original-app")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text(f"{APP} filter=mask\n", encoding="utf-8")
    _git(repo, "config", "filter.mask.clean", f"cat '{tmp / 'original-app'}'")
    _write(repo, APP, "VALUE = 'filtered'\n")
    _git(repo, "add", APP)
    return APP


def _pyc_under_ignored_cache(repo: Path, _tmp: Path) -> str:
    return _write(repo, "product/__pycache__/app.cpython-312.pyc", "bytecode")


def _ignored_build_output(repo: Path, _tmp: Path) -> str:
    return _write(repo, "build/lib/app.py", "VALUE = 'built'\n")


def _nested_environment(repo: Path, _tmp: Path) -> str:
    _write(repo, "product/.venv/.gitignore", "*\n")
    return _write(repo, "product/.venv/sitecustomize.py", "HIDDEN = 1\n")


def _tracked_file_in_cache_dir(repo: Path, _tmp: Path) -> str:
    _git(repo, "update-index", "--skip-worktree", "product/.pytest_cache/kept.txt")
    return _write(repo, "product/.pytest_cache/kept.txt", "rewritten\n")


HIDERS: list[Callable[[Path, Path], str]] = [
    _skip_worktree, _assume_unchanged, _info_exclude, _committed_ignore_rule,
    _nested_ignore_file, _excludes_file, _silent_monitor_hook, _clean_filter,
    _pyc_under_ignored_cache, _ignored_build_output, _nested_environment,
    _tracked_file_in_cache_dir,
]


@pytest.mark.parametrize("hide", HIDERS, ids=lambda hide: hide.__name__.strip("_"))
def test_hidden_change_is_listed_and_bound(
    tmp_path: Path, hide: Callable[[Path, Path], str],
) -> None:
    repo, base = _repo(tmp_path)
    assert _changed_product_paths(repo, base, product_dir="product") == []
    clean = quality.product_snapshot_sha256(repo, _state(base))
    hidden = hide(repo, tmp_path)
    assert hidden in _changed_product_paths(repo, base, product_dir="product")
    first = quality.product_snapshot_sha256(repo, _state(base))
    assert first != clean
    _write(repo, hidden, "VALUE = 'rewritten again'\n")
    assert quality.product_snapshot_sha256(repo, _state(base)) != first


def test_replace_ref_cannot_empty_the_committed_scope(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    clean = quality.product_snapshot_sha256(repo, _state(base))
    _write(repo, APP, "VALUE = 'committed'\n")
    _git(repo, "commit", "-qam", "change")
    _git(repo, "replace", base, _git(repo, "rev-parse", "HEAD"))
    assert _git(repo, "diff", "--name-only", f"{base}..HEAD") == ""
    assert _changed_product_paths(repo, base, product_dir="product") == [APP]
    assert quality.product_snapshot_sha256(repo, _state(base)) != clean


def test_inherited_git_environment_cannot_redirect_the_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base = _repo(tmp_path)
    decoy = tmp_path / "decoy"
    _git(tmp_path, "clone", "-q", str(repo), str(decoy))
    _write(repo, APP, "VALUE = 'changed'\n")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "index"))
    assert _changed_product_paths(repo, base, product_dir="product") == [APP]


def test_only_fixed_tool_directories_stay_outside_the_scope(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    for exempt in (
        ".venv/lib/site.py", ".hw/scope-run/note.txt", ".ruff_cache/0.1/data",
        "product/.pytest_cache/v/cache/lastfailed", "product/deep/pkg/.mypy_cache/3.12/a.json",
    ):
        _write(repo, exempt, "tool state\n")
    _write(repo, ".git/info/exclude", ".venv/\n.hw/\n.ruff_cache/\n.pytest_cache/\n.mypy_cache/\n")
    assert _changed_product_paths(repo, base, product_dir="product") == []
    # A plain file that merely carries a cache name, and look-alike names, are product bytes.
    _write(repo, ".git/info/exclude", "*\n")
    listed = [
        _write(repo, relative, "X = 1\n")
        for relative in
        ("mypy-cache/a.py", "product/.pytest_cache_x/a.py", "product/pkg/.ruff_cache")
    ]
    assert _changed_product_paths(repo, base, product_dir="product") == sorted(listed)


def test_hidden_symlink_is_listed_and_refused(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    (tmp_path / "elsewhere.py").write_text("SECRET = 1\n", encoding="utf-8")
    os.symlink(tmp_path / "elsewhere.py", repo / "product" / "link.py")
    assert _changed_product_paths(repo, base, product_dir="product") == ["product/link.py"]
    with pytest.raises(QualityError, match="symlink"):
        quality.product_snapshot_sha256(repo, _state(base))


def test_hidden_deletion_and_retargeted_link_are_listed(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    clean = quality.product_snapshot_sha256(repo, _state(base))
    _git(repo, "update-index", "--skip-worktree", APP, "product/alias.py")
    (repo / APP).unlink()
    assert _changed_product_paths(repo, base, product_dir="product") == [APP]
    assert quality.product_snapshot_sha256(repo, _state(base)) != clean
    (repo / "product" / "alias.py").unlink()
    os.symlink("other.py", repo / "product" / "alias.py")
    assert _changed_product_paths(repo, base, product_dir="product") == ["product/alias.py", APP]


def test_sha256_object_format_repository_is_compared_by_bytes(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path, "--object-format=sha256")
    assert len(base) == 64
    assert _changed_product_paths(repo, base, product_dir="product") == []
    assert _skip_worktree(repo, tmp_path) in _changed_product_paths(repo, base)


# --- status parsing on renames, copies and odd names -------------------------------------------


def test_staged_rename_to_a_name_with_a_space(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    moved = "product/moved app.py"
    _git(repo, "mv", APP, moved)
    assert "R  " in _git(repo, "status", "--porcelain=v1")
    # The new name is reviewed; the old name is listed too and recorded as missing.
    assert _changed_product_paths(repo, base, product_dir="product") == [APP, moved]
    first = quality.product_snapshot_sha256(repo, _state(base))
    _write(repo, moved, "VALUE = 'after the rename'\n")
    assert quality.product_snapshot_sha256(repo, _state(base)) != first


def test_staged_copy_and_odd_names_are_returned_verbatim(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    _git(repo, "config", "status.renames", "copies")
    copy = "product/copy of app.py"
    shutil.copyfile(repo / APP, repo / copy)
    _write(repo, APP, "VALUE = 1\n# source edited so the copy is detected\n")
    staged = _write(repo, "product/line\nbreak.py", "A = 1\n")
    _git(repo, "add", "-A")
    assert "C  " in _git(repo, "status", "--porcelain=v1")
    untracked = _write(repo, "product/ M fake -> b.py", "B = 1\n")
    _write(repo, ".git/info/exclude", "tab*\n")
    hidden = _write(repo, "product/tab\there é.py", "C = 1\n")
    expected = sorted([APP, copy, staged, untracked, hidden])
    assert _changed_product_paths(repo, base, product_dir="product") == expected
    first = quality.product_snapshot_sha256(repo, _state(base))
    _write(repo, copy, "VALUE = 'copy changed'\n")
    assert quality.product_snapshot_sha256(repo, _state(base)) != first


@pytest.mark.parametrize(
    "records",
    [b"M \\0", b"R  product/new.py\\0", b"C  product/new.py\\0\\0 M product/app.py\\0"],
    ids=["too-short", "rename-without-source", "copy-with-empty-source"],
)
def test_malformed_status_record_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, records: bytes,
) -> None:
    repo, base = _repo(tmp_path)
    real = shutil.which("git")
    assert real is not None
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake = fake_bin / "git"
    fake.write_text(
        "#!/bin/sh\nfor arg in \"$@\"; do\n"
        f"  if [ \"$arg\" = status ]; then printf '{records.decode()}'; exit 0; fi\n"
        f"done\nexec '{real}' \"$@\"\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(QualityError, match="malformed git status entry"):
        _changed_product_paths(repo, base, product_dir="product")
