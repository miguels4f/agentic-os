"""S0.2m: routes the S0.2k/T3 verification left open for the review-key fence and packet binding.

Key fence: a key in a linked worktree stays refused when the repository's registry entry for
that worktree is rewritten or renamed, when the run itself is rooted in a worktree whose marker
was removed.

Packet binding: another folder placed at the registered path, a results directory replaced by
a link, and work hidden from ``git status`` (a commit, a new local ignore rule, a skipped index
entry) are refused; the honest flows still pass.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyperwilfred_core import approvals, engine
from hyperwilfred_core.schemas import BuildObjective, Plan, Slice


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=fixture",
         "-c", "user.email=fixture@example.invalid", *args],
        check=True, capture_output=True,
    )


def _repo(path: Path) -> Path:
    (path / "src").mkdir(parents=True)
    (path / "src" / "app.py").write_text("value = 1\n")
    (path / "docs").mkdir()
    (path / "docs" / "README.md").write_text("doc\n")
    _git(path, "init", "-q")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    return path


def _key(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    pem = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    path = directory / "review.pem"
    path.write_bytes(pem)
    return path


def _linked(repo: Path, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--detach", str(path))
    return path


# ------------------------------------------------------------------------------- key fence


def test_key_in_linked_worktree_refused_when_registry_entry_is_repointed(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "main")
    worker = _linked(repo, tmp_path / "worker")
    key = _key(worker / "tools")
    with pytest.raises(approvals.ApprovalError):
        approvals.require_external_key_location(key, excluded_roots=[repo])
    (repo / ".git" / "worktrees" / "worker" / "gitdir").write_text("/nonexistent/.git\n")
    with pytest.raises(approvals.ApprovalError, match="outside the repository"):
        approvals.require_external_key_location(key, excluded_roots=[repo])


def test_key_in_linked_worktree_refused_when_registry_folder_is_renamed(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "main")
    worker = _linked(repo, tmp_path / "worker")
    key = _key(worker / "tools")
    hidden = repo / ".git" / "worker.hidden"
    shutil.move(str(repo / ".git" / "worktrees" / "worker"), str(hidden))
    (worker / ".git").write_text(f"gitdir: {hidden}\n")
    with pytest.raises(approvals.ApprovalError, match="outside the repository"):
        approvals.require_external_key_location(key, excluded_roots=[repo])


@pytest.mark.parametrize("marker", [None, "gitdir: /nonexistent/x\n"])
def test_run_rooted_in_worktree_without_marker_still_fences_its_repository(
    tmp_path: Path, marker: str | None,
) -> None:
    repo = _repo(tmp_path / "main")
    run_root = _linked(repo, tmp_path / "run")
    sibling = _linked(repo, tmp_path / "sibling")
    in_main = _key(repo / "tools")
    in_sibling = _key(sibling / "tools")
    (run_root / ".git").unlink()
    if marker is not None:
        (run_root / ".git").write_text(marker)
    for key in (in_main, in_sibling):
        with pytest.raises(approvals.ApprovalError, match="outside the repository"):
            approvals.require_external_key_location(key, excluded_roots=[run_root])


def test_external_key_still_accepted_beside_worktrees(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "main")
    worker = _linked(repo, tmp_path / "worker")
    key = _key(tmp_path / "outside" / "keys")
    for fence in (repo, worker):
        assert approvals.require_external_key_location(key, excluded_roots=[fence]).is_file()


def test_external_key_inside_an_unrelated_repository_is_accepted(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "main")
    other = _repo(tmp_path / "unrelated")
    key = _key(other / "keys")
    assert approvals.require_external_key_location(key, excluded_roots=[repo]).is_file()


# ------------------------------------------------------------------------- packet binding


def _run(tmp_path: Path) -> tuple[Path, engine.RunPaths, Path]:
    repo = _repo(tmp_path / "packet")
    engine.plan(repo_root=repo, run_id="r", product_dir="src",
                objective=BuildObjective(id="o", title="Scope", objective="build app"))
    paths = engine.RunPaths(repo, "r")
    paths.plan.write_text(Plan(
        objective_id="o", product_dir="src",
        slices=[Slice(id="a", title="A", allowed_globs=["src/**"])]).model_dump_json())
    engine.fanout_plan(repo_root=repo, run_id="r")
    worker = _linked(repo, tmp_path / "workers" / "any-name")
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="a", worktree=str(worker))
    paths.results_dir.mkdir(parents=True, exist_ok=True)
    (paths.results_dir / "a.json").write_text('{"status": "done"}\n')
    return repo, paths, worker


def _record(repo: Path, worker: Path) -> dict:
    return engine.record_result(
        repo_root=repo, run_id="r", packet_id="a", worktree=str(worker), do_rollback=False)


def test_honest_registered_worktree_is_still_accepted(tmp_path: Path) -> None:
    repo, _paths, worker = _run(tmp_path)
    (worker / "src" / "new.py").write_text("value = 2\n")
    assert _record(repo, worker)["verdict"]["accepted"]


def test_other_folder_at_registered_path_with_same_registration_is_refused(
    tmp_path: Path,
) -> None:
    repo, _paths, worker = _run(tmp_path)
    (worker / "evil.py").write_text("escape\n")
    marker = (worker / ".git").read_text()
    os.rename(worker, str(worker) + ".real")
    worker.mkdir()
    (worker / ".git").write_text(marker)
    _git(worker, "reset", "-q", "--hard", "HEAD")
    with pytest.raises(engine.EngineError, match="bound|stale"):
        _record(repo, worker)


def test_results_directory_replaced_by_a_link_is_refused(tmp_path: Path) -> None:
    repo, paths, worker = _run(tmp_path)
    (worker / "src" / "new.py").write_text("value = 2\n")
    outside = tmp_path / "outside-results"
    shutil.move(str(paths.results_dir), str(outside))
    os.symlink(outside, paths.results_dir)
    with pytest.raises(engine.EngineError, match="missing or unsafe"):
        _record(repo, worker)


def test_work_committed_by_the_worker_is_refused(tmp_path: Path) -> None:
    repo, paths, worker = _run(tmp_path)
    (worker / "evil.py").write_text("escape\n")
    _git(worker, "add", "-A")
    _git(worker, "commit", "-qm", "worker commit")
    with pytest.raises(engine.EngineError, match="commits made after registration"):
        _record(repo, worker)
    assert not (paths.verdicts_dir / "a.json").exists()
    # Registering again must not move the pin to the worker's commit.
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="a", worktree=str(worker))
    with pytest.raises(engine.EngineError, match="commits made after registration"):
        _record(repo, worker)


def test_new_local_ignore_rule_is_refused(tmp_path: Path) -> None:
    repo, _paths, worker = _run(tmp_path)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("evil.py\n")
    (worker / "evil.py").write_text("escape\n")
    with pytest.raises(engine.EngineError, match="ignore rules changed"):
        _record(repo, worker)


def test_configured_excludes_file_is_refused(tmp_path: Path) -> None:
    repo, _paths, worker = _run(tmp_path)
    rules = tmp_path / "rules"
    rules.write_text("evil.py\n")
    _git(repo, "config", "core.excludesFile", str(rules))
    (worker / "evil.py").write_text("escape\n")
    with pytest.raises(engine.EngineError, match="ignore rules changed"):
        _record(repo, worker)


def test_tracked_file_hidden_from_status_is_refused(tmp_path: Path) -> None:
    repo, _paths, worker = _run(tmp_path)
    _git(worker, "update-index", "--skip-worktree", "docs/README.md")
    (worker / "docs" / "README.md").write_text("rewritten out of scope\n")
    with pytest.raises(engine.EngineError, match="hides tracked files"):
        _record(repo, worker)


def test_ignore_rules_present_before_registration_do_not_block(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "packet")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("notes.txt\n")
    engine.plan(repo_root=repo, run_id="r", product_dir="src",
                objective=BuildObjective(id="o", title="Scope", objective="build app"))
    paths = engine.RunPaths(repo, "r")
    paths.plan.write_text(Plan(
        objective_id="o", product_dir="src",
        slices=[Slice(id="a", title="A", allowed_globs=["src/**"])]).model_dump_json())
    engine.fanout_plan(repo_root=repo, run_id="r")
    worker = _linked(repo, tmp_path / "w")
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="a", worktree=str(worker))
    paths.results_dir.mkdir(parents=True, exist_ok=True)
    (paths.results_dir / "a.json").write_text('{"status": "done"}\n')
    (worker / "src" / "new.py").write_text("value = 2\n")
    assert _record(repo, worker)["verdict"]["accepted"]


def test_single_checkout_serves_next_packet_after_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "packet")
    engine.plan(repo_root=repo, run_id="r", product_dir="src",
                objective=BuildObjective(id="o", title="Scope", objective="build app"))
    paths = engine.RunPaths(repo, "r")
    paths.plan.write_text(Plan(
        objective_id="o", product_dir="src",
        slices=[Slice(id=p, title=p, allowed_globs=["src/**"]) for p in ("a", "b")],
    ).model_dump_json())
    engine.fanout_plan(repo_root=repo, run_id="r")
    paths.results_dir.mkdir(parents=True, exist_ok=True)
    for packet in ("a", "b"):
        engine.register_worktree(repo_root=repo, run_id="r", packet_id=packet, worktree=str(repo))
        (repo / "src" / f"{packet}.py").write_text("value = 2\n")
        (paths.results_dir / f"{packet}.json").write_text('{"status": "done"}\n')
        out = engine.record_result(repo_root=repo, run_id="r", packet_id=packet,
                                   worktree=str(repo), do_rollback=False)
        assert out["verdict"]["accepted"]
        _git(repo, "add", "src")
        _git(repo, "commit", "-qm", packet)
