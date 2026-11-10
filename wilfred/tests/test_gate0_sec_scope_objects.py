"""The base commit tree is read through hash-verified objects, and ignored links are bound.

An overwritten loose tree object below the top level must not hide a rewritten file.  A link
that only the directory walk finds and that git ignores (the layout of installed dependency
tools) is bound by its own target text instead of stopping the snapshot; a link that git tracks
or reports is still refused.  The guards around the tree reader, the walk and the status parser
are pinned so that removing one makes a test fail.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import zlib
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyperwilfred_core import engine, quality
from hyperwilfred_core.quality import QualityError, _changed_product_paths
from hyperwilfred_core.schemas import BuildMode, BuildObjective

_ISOLATED = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
APP = "product/app.py"
MOD = "product/pkg/mod.py"
TOOL = "node_modules/.bin/tsc"
INTERPRETER = "venv/bin/python"


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | _ISOLATED
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
        check=True, capture_output=True, text=True, env=env,
    )
    return done.stdout.strip()


def _repo(tmp_path: Path, *init_args: str) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "product" / "pkg").mkdir(parents=True)
    _git(tmp_path, "init", "-q", *init_args, str(repo))
    (repo / APP).write_text("VALUE = 1\n", encoding="utf-8")
    (repo / MOD).write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "product" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    os.symlink("app.py", repo / "product" / "alias.py")
    (repo / ".gitignore").write_text("node_modules/\nvenv/\n.hw/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _state(base: str) -> Any:
    return SimpleNamespace(run_id="scope-run", base_commit=base, product_dir="product")


def _scope(repo: Path, base: str) -> list[str]:
    return _changed_product_paths(repo, base, product_dir="product")


def _link(repo: Path, relative: str, target: str) -> str:
    (repo / relative).parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(repo / relative):
        (repo / relative).unlink()
    os.symlink(target, repo / relative)
    return relative


def _fake_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, action: str) -> None:
    real = shutil.which("git")
    assert real is not None
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "git").write_text(
        "#!/bin/sh\nfor arg in \"$@\"; do\n"
        f"  if [ \"$arg\" = {command} ]; then {action}; fi\n"
        f"done\nexec '{real}' \"$@\"\n",
        encoding="utf-8",
    )
    (fake_bin / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")


# --- the base tree is read through verified objects ---------------------------------------------


@pytest.mark.parametrize("init_args", [(), ("--object-format=sha256",)], ids=["sha1", "sha256"])
def test_overwritten_subtree_object_cannot_hide_a_rewritten_file(
    tmp_path: Path, init_args: tuple[str, ...],
) -> None:
    repo, base = _repo(tmp_path, *init_args)
    assert _scope(repo, base) == []
    clean = quality.product_snapshot_sha256(repo, _state(base))
    (repo / MOD).write_text("def f():\n    return 666\n", encoding="utf-8")
    forged_blob = _git(repo, "hash-object", "-w", MOD)
    kept_blob = _git(repo, "rev-parse", f"{base}:product/pkg/__init__.py")
    subtree = _git(repo, "rev-parse", f"{base}:product/pkg")
    body = (
        b"100644 __init__.py\0" + bytes.fromhex(kept_blob)
        + b"100644 mod.py\0" + bytes.fromhex(forged_blob)
    )
    loose = repo / ".git" / "objects" / subtree[:2] / subtree[2:]
    loose.chmod(0o644)
    loose.write_bytes(zlib.compress(b"tree %d\0" % len(body) + body))
    _git(repo, "add", MOD)
    # Git itself now believes nothing changed: the forged tree names the rewritten bytes.
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "ls-tree", "-r", base, "--", MOD).split()[2] == forged_blob
    try:
        listed = _scope(repo, base)
    except QualityError as exc:
        assert "does not hash to its own id" in str(exc)
    else:
        assert MOD in listed
    try:
        assert quality.product_snapshot_sha256(repo, _state(base)) != clean
    except QualityError as exc:
        assert "does not hash to its own id" in str(exc)


def test_run_rooted_below_the_git_top_level_compares_its_own_files(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    inner = repo / "product"
    assert _changed_product_paths(inner, base, product_dir="pkg") == []
    (repo / MOD).write_text("def f():\n    return 2\n", encoding="utf-8")
    _git(repo, "update-index", "--assume-unchanged", MOD)
    assert _changed_product_paths(inner, base, product_dir="pkg") == ["pkg/mod.py"]


@pytest.mark.parametrize(
    "action",
    ["exit 1", "exit 0", "printf '%s commit 5\\nabcde\\n' \"$(cat)\"; exit 0"],
    ids=["reader-fails", "reader-answers-nothing", "reader-answers-other-bytes"],
)
def test_unreadable_base_tree_stops_the_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    repo, base = _repo(tmp_path)
    _fake_git(tmp_path, monkeypatch, "cat-file", action)
    with pytest.raises(QualityError, match="cannot determine committed review scope"):
        _scope(repo, base)
    with pytest.raises(QualityError, match="cannot determine committed review scope"):
        quality.product_snapshot_sha256(repo, _state(base))


# --- ignored links of installed dependency tools ------------------------------------------------


def test_ignored_dependency_links_are_bound_by_their_target_text(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    (repo / "node_modules" / "typescript" / "bin").mkdir(parents=True)
    (repo / "node_modules" / "typescript" / "bin" / "tsc").write_text("#!/bin/sh\n", "utf-8")
    _link(repo, TOOL, "../typescript/bin/tsc")
    _link(repo, INTERPRETER, str(tmp_path / "no-such-interpreter"))  # dangling: never followed
    _link(repo, "venv/lib64", "lib")
    assert _scope(repo, base) == [
        TOOL, "node_modules/typescript/bin/tsc", INTERPRETER, "venv/lib64",
    ]
    first = quality.product_snapshot_sha256(repo, _state(base))
    assert quality.product_snapshot_sha256(repo, _state(base)) == first
    for relative, target in ((TOOL, "../other/bin/tsc"), (INTERPRETER, "/usr/bin/false")):
        _link(repo, relative, target)
        second = quality.product_snapshot_sha256(repo, _state(base))
        assert second != first
        first = second


def test_ignored_dependency_link_does_not_stop_the_critic_path_list(tmp_path: Path) -> None:
    repo, _base = _repo(tmp_path)
    engine.plan(
        repo_root=repo, objective=BuildObjective(id="o", title="t", objective="x"),
        run_id="work", product_dir="product", mode=BuildMode.BUILD,
    )
    (repo / APP).write_text("VALUE = 2\n", encoding="utf-8")
    _link(repo, TOOL, "../typescript/bin/tsc")
    assert quality._critique_target_paths(repo, "work") == [TOOL, APP]


def test_ignored_link_to_a_file_binds_the_bytes_it_points_to(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("PAYLOAD = 1\n", encoding="utf-8")
    (repo / ".git" / "info" / "exclude").write_text("product/plugin.py\n", encoding="utf-8")
    _link(repo, "product/plugin.py", str(outside))
    first = quality.product_snapshot_sha256(repo, _state(base))
    outside.write_text("PAYLOAD = 2\n", encoding="utf-8")
    second = quality.product_snapshot_sha256(repo, _state(base))
    assert second != first
    twin = tmp_path / "twin.py"  # same bytes, other target text
    twin.write_bytes(outside.read_bytes())
    _link(repo, "product/plugin.py", str(twin))
    assert quality.product_snapshot_sha256(repo, _state(base)) != second
    _link(repo, "product/plugin.py", str(outside))
    outside.chmod(0)
    try:
        with pytest.raises(QualityError, match="target of link"):
            quality.product_snapshot_sha256(repo, _state(base))
    finally:
        outside.chmod(0o644)


def test_ignored_link_to_a_reviewed_directory_is_bound(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    (repo / "venv" / "lib").mkdir(parents=True)
    _link(repo, "venv/lib64", "lib")
    first = quality.product_snapshot_sha256(repo, _state(base))
    (repo / "venv" / "lib" / "site.py").write_text("X = 1\n", encoding="utf-8")
    assert quality.product_snapshot_sha256(repo, _state(base)) != first


def _outside_directory(repo: Path, tmp: Path) -> str:
    (tmp / "outside-package").mkdir()
    return str(tmp / "outside-package")


def _top_level_environment(repo: Path, _tmp: Path) -> str:
    (repo / ".venv" / "lib").mkdir(parents=True)
    return "../.venv/lib"


def _cache_directory(repo: Path, _tmp: Path) -> str:
    (repo / "product" / ".mypy_cache" / "deep").mkdir(parents=True)
    return "../product/.mypy_cache/deep"


def _named_pipe(_repo_dir: Path, tmp: Path) -> str:
    os.mkfifo(tmp / "pipe")
    return str(tmp / "pipe")


def _loop(_repo_dir: Path, _tmp: Path) -> str:
    return "vendored"


@pytest.mark.parametrize(
    "target", [_outside_directory, _top_level_environment, _cache_directory, _named_pipe, _loop],
    ids=lambda target: target.__name__.strip("_"),
)
def test_ignored_link_to_unbound_code_is_refused(
    tmp_path: Path, target: Callable[[Path, Path], str],
) -> None:
    repo, base = _repo(tmp_path)
    _link(repo, "venv/vendored", target(repo, tmp_path))
    assert "venv/vendored" in _scope(repo, base)
    with pytest.raises(QualityError, match="is a link"):
        quality.product_snapshot_sha256(repo, _state(base))


def test_dangling_ignored_link_changes_the_snapshot_when_its_target_appears(
    tmp_path: Path,
) -> None:
    repo, base = _repo(tmp_path)
    later = tmp_path / "appears-later.py"
    _link(repo, INTERPRETER, str(later))
    first = quality.product_snapshot_sha256(repo, _state(base))
    later.write_text("", encoding="utf-8")
    assert quality.product_snapshot_sha256(repo, _state(base)) != first
    _link(repo, "venv/lib64", "lib")
    before = quality.product_snapshot_sha256(repo, _state(base))
    (repo / "venv" / "lib").mkdir()  # an empty directory adds no file of its own
    assert quality.product_snapshot_sha256(repo, _state(base)) != before


def test_dependency_layouts_pass_the_deterministic_gate(tmp_path: Path) -> None:
    repo, _base = _repo(tmp_path)
    (repo / "node_modules" / "typescript" / "bin").mkdir(parents=True)
    (repo / "node_modules" / "typescript" / "bin" / "tsc").write_text("#!/bin/sh\n", "utf-8")
    _link(repo, TOOL, "../typescript/bin/tsc")
    (repo / "venv" / "lib").mkdir(parents=True)
    _link(repo, "venv/lib64", "lib")
    _link(repo, INTERPRETER, os.path.realpath("/bin/sh"))
    engine.plan(
        repo_root=repo, objective=BuildObjective(id="o", title="t", objective="x"),
        run_id="work", product_dir="product", mode=BuildMode.BUILD,
    )
    (repo / APP).write_text("VALUE = 2\n", encoding="utf-8")
    engine.evaluate(repo_root=repo, run_id="work", target=repo / "product")
    pinned = repo / ".hw" / "work" / "deterministic-scope.sha256"
    state = engine.load_state(engine.RunPaths(repo, "work"))
    current = quality.product_snapshot_sha256(repo, state)
    assert pinned.read_text(encoding="utf-8").strip() == current


def _visible_untracked(repo: Path, _base: str) -> str:
    return _link(repo, "product/link.py", "app.py")


def _force_added(repo: Path, _base: str) -> str:
    _link(repo, INTERPRETER, "/usr/bin/false")
    _git(repo, "add", "-f", INTERPRETER)
    return INTERPRETER


def _tracked_then_excluded(repo: Path, _base: str) -> str:
    _git(repo, "update-index", "--skip-worktree", "product/alias.py")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("product/alias.py\n", encoding="utf-8")
    return _link(repo, "product/alias.py", "pkg/mod.py")


def _committed_then_dropped_from_the_index(repo: Path, _base: str) -> str:
    _force_added(repo, _base)
    _git(repo, "commit", "-qm", "link")
    _git(repo, "rm", "-q", "--cached", INTERPRETER)
    return INTERPRETER


@pytest.mark.parametrize(
    "plant",
    [_visible_untracked, _force_added, _tracked_then_excluded,
     _committed_then_dropped_from_the_index],
    ids=lambda plant: plant.__name__.strip("_"),
)
def test_link_that_git_tracks_or_reports_is_still_refused(
    tmp_path: Path, plant: Callable[[Path, str], str],
) -> None:
    repo, base = _repo(tmp_path)
    planted = plant(repo, base)
    assert planted in _scope(repo, base)
    with pytest.raises(QualityError, match="symlink"):
        quality.product_snapshot_sha256(repo, _state(base))


def test_bound_link_replaced_during_the_snapshot_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base = _repo(tmp_path)
    _link(repo, TOOL, "../typescript/bin/tsc")
    real = quality._ignored_by_git

    def swap(root: Path, candidates: set[str]) -> set[str]:
        ignored = real(root, candidates)
        (repo / TOOL).unlink()
        (repo / TOOL).write_text("#!/bin/sh\necho swapped\n", encoding="utf-8")
        return ignored

    monkeypatch.setattr(quality, "_ignored_by_git", swap)
    with pytest.raises(QualityError, match="changed while it was bound"):
        quality.product_snapshot_sha256(repo, _state(base))


def test_ignore_lookup_failure_stops_the_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base = _repo(tmp_path)
    _link(repo, TOOL, "../typescript/bin/tsc")
    _fake_git(tmp_path, monkeypatch, "check-ignore", "exit 128")
    with pytest.raises(QualityError, match="cannot determine working-tree review scope"):
        quality.product_snapshot_sha256(repo, _state(base))


# --- guards that had no test --------------------------------------------------------------------


def test_file_system_monitor_hook_is_never_run_by_the_scope(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    marker = tmp_path / "monitor-ran"
    hook = tmp_path / "monitor-hook.sh"
    hook.write_text(f"#!/bin/sh\n: > '{marker}'\nprintf 'token\\0'\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", str(hook))
    (repo / APP).write_text("VALUE = 'monitor'\n", encoding="utf-8")
    assert _scope(repo, base) == [APP]
    quality.product_snapshot_sha256(repo, _state(base))
    assert not marker.exists()
    _git(repo, "status", "--porcelain")  # the hook is live: plain git does run it
    assert marker.exists()


def test_unreadable_directory_stops_the_walk(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    locked = repo / "product" / "locked"
    locked.mkdir()
    (locked / "hidden.py").write_text("HIDDEN = 1\n", encoding="utf-8")
    locked.chmod(0)
    try:
        with pytest.raises(QualityError, match="cannot enumerate working-tree review scope"):
            _scope(repo, base)
    finally:
        locked.chmod(0o755)


def test_rename_recorded_only_in_the_second_status_column(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    moved = "product/moved.py"
    (repo / APP).rename(repo / moved)
    _git(repo, "add", "--intent-to-add", moved)
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "-z"],
        check=True, capture_output=True,
    ).stdout
    assert status == b" R product/moved.py\0product/app.py\0"
    assert _scope(repo, base) == [APP, moved]


def test_names_with_leading_or_trailing_whitespace_are_kept_verbatim(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    names = [" lead.py", "product/trail.py ", "product/ both .py"]
    for name in names:
        (repo / name).write_text("A = 1\n", encoding="utf-8")
    assert _scope(repo, base) == sorted(names)
    first = quality.product_snapshot_sha256(repo, _state(base))
    (repo / names[1]).write_text("A = 2\n", encoding="utf-8")
    assert quality.product_snapshot_sha256(repo, _state(base)) != first
