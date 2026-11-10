"""A packet result must come from its own registered worktree and result file."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hyperwilfred_core import engine
from hyperwilfred_core.schemas import BuildObjective, Plan, Slice


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _run(tmp_path: Path, *, two: bool = False) -> tuple[Path, engine.RunPaths]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("value = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-qm", "base")
    engine.plan(
        repo_root=repo, run_id="r", product_dir="src",
        objective=BuildObjective(id="o", title="Packets", objective="build app"),
    )
    paths = engine.RunPaths(repo, "r")
    slices = [Slice(id="a", title="A", allowed_globs=["src/**"])]
    if two:
        slices.append(Slice(id="b", title="B", allowed_globs=["src/**"]))
    paths.plan.write_text(
        Plan(objective_id="o", product_dir="src", slices=slices).model_dump_json()
    )
    engine.fanout_plan(repo_root=repo, run_id="r")
    return repo, paths


def _result(paths: engine.RunPaths, packet: str) -> None:
    paths.results_dir.mkdir(parents=True, exist_ok=True)
    (paths.results_dir / f"{packet}.json").write_text('{"status": "done"}\n')


def test_build_refuses_missing_result_even_in_own_checkout(tmp_path: Path) -> None:
    repo, paths = _run(tmp_path)
    (repo / "src" / "new.py").write_text("value = 2\n")
    with pytest.raises(engine.EngineError, match="result file"):
        engine.record_result(repo_root=repo, run_id="r", packet_id="a", worktree=str(repo),
                             do_rollback=False)
    assert not (paths.verdicts_dir / "a.json").exists()


def test_other_packets_clean_linked_worktree_cannot_stand_in(tmp_path: Path) -> None:
    repo, paths = _run(tmp_path, two=True)
    worker_a = tmp_path / "workers" / "first"
    worker_b = tmp_path / "workers" / "other-name"
    worker_a.parent.mkdir()
    _git(repo, "worktree", "add", "-q", "--detach", str(worker_a), "HEAD")
    _git(repo, "worktree", "add", "-q", "--detach", str(worker_b), "HEAD")
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="a", worktree=str(worker_a))
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="b", worktree=str(worker_b))
    (worker_a / "evil.py").write_text("escape\n")
    (worker_b / "src" / "good.py").write_text("value = 2\n")
    _result(paths, "a")
    with pytest.raises(engine.EngineError, match="registered|bound"):
        engine.record_result(repo_root=repo, run_id="r", packet_id="a",
                             worktree=str(worker_b), do_rollback=False)
    assert not (paths.verdicts_dir / "a.json").exists()


def test_registered_arbitrary_named_linked_worker_is_accepted(tmp_path: Path) -> None:
    repo, paths = _run(tmp_path)
    worker = tmp_path / "workers" / "any-name"
    worker.parent.mkdir()
    _git(repo, "worktree", "add", "-q", "--detach", str(worker), "HEAD")
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="a", worktree=str(worker))
    (worker / "src" / "new.py").write_text("value = 2\n")
    _result(paths, "a")
    out = engine.record_result(repo_root=repo, run_id="r", packet_id="a",
                               worktree=str(worker), do_rollback=False)
    assert out["verdict"]["accepted"]
    assert out["verdict"]["result_read_from_disk"]


def test_stale_registration_after_worktree_replaced_is_refused(tmp_path: Path) -> None:
    repo, paths = _run(tmp_path)
    worker = tmp_path / "workers" / "any-name"
    worker.parent.mkdir()
    _git(repo, "worktree", "add", "-q", "--detach", str(worker), "HEAD")
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="a", worktree=str(worker))
    _git(repo, "worktree", "remove", "--force", str(worker))
    _git(repo, "worktree", "add", "-q", "--detach", str(worker), "HEAD")
    (worker / "src" / "new.py").write_text("value = 2\n")
    _result(paths, "a")
    with pytest.raises(engine.EngineError, match="stale|bound"):
        engine.record_result(repo_root=repo, run_id="r", packet_id="a",
                             worktree=str(worker), do_rollback=False)


def test_result_symlink_to_other_packet_is_refused(tmp_path: Path) -> None:
    repo, paths = _run(tmp_path, two=True)
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="a", worktree=str(repo))
    _result(paths, "b")
    (paths.results_dir / "a.json").symlink_to(paths.results_dir / "b.json")
    with pytest.raises(engine.EngineError, match="result file"):
        engine.record_result(repo_root=repo, run_id="r", packet_id="a",
                             worktree=str(repo), do_rollback=False)


def test_copied_git_marker_is_not_a_registration(tmp_path: Path) -> None:
    repo, paths = _run(tmp_path)
    worker = tmp_path / "workers" / "real"
    fake = tmp_path / "workers" / "fake"
    worker.parent.mkdir()
    _git(repo, "worktree", "add", "-q", "--detach", str(worker), "HEAD")
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="a", worktree=str(worker))
    fake.mkdir()
    (fake / ".git").write_bytes((worker / ".git").read_bytes())
    (fake / "src").mkdir()
    (fake / "src" / "new.py").write_text("value = 2\n")
    _result(paths, "a")
    with pytest.raises(engine.EngineError, match="registered|worktree"):
        engine.record_result(repo_root=repo, run_id="r", packet_id="a",
                             worktree=str(fake), do_rollback=False)


def test_single_checkout_can_serve_packets_in_sequence_with_results(tmp_path: Path) -> None:
    repo, paths = _run(tmp_path, two=True)
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="a", worktree=str(repo))
    with pytest.raises(engine.EngineError, match="active packet"):
        engine.register_worktree(repo_root=repo, run_id="r", packet_id="b", worktree=str(repo))
    (repo / "src" / "first.py").write_text("value = 2\n")
    _result(paths, "a")
    first = engine.record_result(repo_root=repo, run_id="r", packet_id="a",
                                 worktree=str(repo), do_rollback=False)
    assert first["verdict"]["accepted"]
    _git(repo, "add", "src/first.py")
    _git(repo, "-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-qm", "first")
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="b", worktree=str(repo))
    (repo / "src" / "second.py").write_text("value = 3\n")
    _result(paths, "b")
    second = engine.record_result(repo_root=repo, run_id="r", packet_id="b",
                                  worktree=str(repo), do_rollback=False)
    assert second["verdict"]["accepted"]


def test_active_worker_cannot_be_registered_to_another_run(tmp_path: Path) -> None:
    repo, _ = _run(tmp_path)
    worker = tmp_path / "workers" / "shared"
    worker.parent.mkdir()
    _git(repo, "worktree", "add", "-q", "--detach", str(worker), "HEAD")
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="a", worktree=str(worker))

    engine.plan(
        repo_root=repo, run_id="s", product_dir="src",
        objective=BuildObjective(id="o", title="Packets", objective="build app"),
    )
    other_paths = engine.RunPaths(repo, "s")
    other_paths.plan.write_text(
        Plan(objective_id="o", product_dir="src", slices=[
            Slice(id="a", title="A", allowed_globs=["src/**"])
        ]).model_dump_json()
    )
    engine.fanout_plan(repo_root=repo, run_id="s")
    with pytest.raises(engine.EngineError, match="another active packet"):
        engine.register_worktree(repo_root=repo, run_id="s", packet_id="a",
                                 worktree=str(worker))
