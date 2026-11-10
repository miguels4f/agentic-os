"""Acceptance inspects a worktree belonging to the run's repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from test_quality import _run_at_quality, _to_patch_phase

from hyperwilfred_core import engine, quality
from hyperwilfred_core.schemas import BuildObjective, Plan, Slice


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(path: Path) -> Path:
    path.mkdir()
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("value = 1\n")
    _git(path, "init", "-q")
    _git(path, "add", "-A")
    _git(
        path,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "base",
    )
    return path


def _packet(repo: Path) -> engine.RunPaths:
    engine.plan(
        repo_root=repo,
        run_id="r",
        product_dir="src",
        objective=BuildObjective(id="o", title="Scope", objective="build app"),
    )
    paths = engine.RunPaths(repo, "r")
    paths.plan.write_text(
        Plan(
            objective_id="o",
            product_dir="src",
            slices=[Slice(id="a", title="A", allowed_globs=["src/**"])],
        ).model_dump_json()
    )
    engine.fanout_plan(repo_root=repo, run_id="r")
    return paths


def test_unrelated_clean_repository_cannot_accept_dirty_packet(tmp_path: Path) -> None:
    repo, other = _repo(tmp_path / "packet"), _repo(tmp_path / "unrelated")
    paths = _packet(repo)
    paths.results_dir.mkdir(parents=True, exist_ok=True)
    (paths.results_dir / "a.json").write_text('{"status": "done"}\n')
    (repo / "evil.py").write_text("escape\n")
    with pytest.raises(engine.EngineError, match="same repository"):
        engine.record_result(
            repo_root=repo, run_id="r", packet_id="a", worktree=str(other), do_rollback=False
        )
    assert not (paths.verdicts_dir / "a.json").exists()


def test_linked_worktree_of_same_repository_is_allowed(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "packet")
    paths = _packet(repo)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "--detach", str(linked))
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="a", worktree=str(linked))
    (linked / "src" / "new.py").write_text("value = 2\n")
    result = paths.results_dir / "a.json"
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_text('{"status": "done"}\n')
    outcome = engine.record_result(
        repo_root=repo, run_id="r", packet_id="a", worktree=str(linked), do_rollback=False
    )
    assert outcome["verdict"]["accepted"]
    assert outcome["verdict"]["result_read_from_disk"]


def test_result_file_override_cannot_point_away_from_packet(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "packet")
    _packet(repo)
    other = tmp_path / "other-result.json"
    other.write_text('{"status": "done"}\n')
    with pytest.raises(engine.EngineError, match="result file"):
        engine.record_result(
            repo_root=repo,
            run_id="r",
            packet_id="a",
            worktree=str(repo),
            result_file=str(other),
            do_rollback=False,
        )


def test_patch_cannot_use_unrelated_clean_repository(tmp_path: Path) -> None:
    repo, other = _repo(tmp_path / "packet"), _repo(tmp_path / "unrelated")
    paths = _run_at_quality(repo, run_id="p", product_dir="src")
    paths.plan.write_text(
        Plan(
            objective_id="o",
            product_dir="src",
            slices=[Slice(id="s", title="S", allowed_globs=["src/**"])],
        ).model_dump_json()
    )
    _to_patch_phase(repo, "p")
    packet = quality.patch_plan(repo_root=repo, run_id="p")["packet"]
    result = repo / packet["result_path"]
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_text('{"patched": ["x"]}\n')
    with pytest.raises(quality.QualityError, match="same repository"):
        quality.record_patch(
            repo_root=repo,
            run_id="p",
            packet_id=packet["packet_id"],
            worktree=str(other),
            do_rollback=False,
        )


def test_patch_requires_packet_result_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "packet")
    paths = _run_at_quality(repo, run_id="p", product_dir="src")
    paths.plan.write_text(
        Plan(
            objective_id="o",
            product_dir="src",
            slices=[Slice(id="s", title="S", allowed_globs=["src/**"])],
        ).model_dump_json()
    )
    _to_patch_phase(repo, "p")
    packet = quality.patch_plan(repo_root=repo, run_id="p")["packet"]
    with pytest.raises(quality.QualityError, match="result file is missing"):
        quality.record_patch(
            repo_root=repo,
            run_id="p",
            packet_id=packet["packet_id"],
            worktree=str(repo),
            do_rollback=False,
        )


def test_patch_uses_only_its_registered_arbitrary_named_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "packet")
    base = "".join(f"value_{i} = {i}\n" for i in range(60))
    (repo / "src" / "app.py").write_text(base)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-qm", "seed")
    paths = _run_at_quality(repo, run_id="p", product_dir="src")
    paths.plan.write_text(
        Plan(objective_id="o", product_dir="src",
             slices=[Slice(id="s", title="S", allowed_globs=["src/**"])]).model_dump_json()
    )
    _to_patch_phase(repo, "p")
    packet = quality.patch_plan(repo_root=repo, run_id="p")["packet"]
    own = tmp_path / "workers" / "any-name"
    other = tmp_path / "workers" / "other-name"
    own.parent.mkdir()
    _git(repo, "worktree", "add", "--detach", str(own), "HEAD")
    _git(repo, "worktree", "add", "--detach", str(other), "HEAD")
    engine.register_worktree(
        repo_root=repo, run_id="p", packet_id=packet["packet_id"],
        worktree=str(own), kind="patch",
    )
    result = repo / packet["result_path"]
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_text('{"patched": ["x"]}\n')
    (own / "src" / "app.py").write_text(base.replace("value_0 = 0", "value_0 = 1"))
    (other / "src" / "app.py").write_text(base.replace("value_0 = 0", "value_0 = 1"))
    with pytest.raises(quality.QualityError, match="bound|stale"):
        quality.record_patch(
            repo_root=repo, run_id="p", packet_id=packet["packet_id"],
            worktree=str(other), do_rollback=False,
        )
    out = quality.record_patch(
        repo_root=repo, run_id="p", packet_id=packet["packet_id"],
        worktree=str(own), do_rollback=False,
    )
    assert out["verdict"]["accepted"]
    assert out["verdict"]["result_read_from_disk"]
