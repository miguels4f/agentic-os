"""Shared fixtures for hyperwilfred-core tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A fresh git repo with one committed file, used as a worker worktree under test."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    # Hermetic config so the suite never inherits a slow/failing global hook, GPG signing,
    # or an auto-gc that races a commit — the realistic sources of a rare subprocess flake.
    hooks_dir = tmp_path / "no-hooks"
    hooks_dir.mkdir()
    _git(repo, "config", "core.hooksPath", str(hooks_dir))
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "gc.auto", "0")
    (repo / "README.md").write_text("# product\nline1\nline2\nline3\n", encoding="utf-8")
    src = repo / "src"
    src.mkdir()
    (src / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.fixture
def git_helper():
    """Expose the raw git runner so tests can stage/commit inside the repo."""
    return _git


@pytest.fixture
def hw_history(tmp_path: Path) -> Path:
    """A repo with a constructed ``.hw/`` history exercising every Phase-5 code anomaly.

    Built with the real engine + schemas so the analyzer/observer read genuine on-disk state:

    * ``r1`` — quality loop maxed (fallback) + a diff-over-budget rejection + 2 gate failures
      → patch_loop_maxed, diff_over_budget, repeated_gate_fail.
    * ``r2`` — 3 of 4 packets rejected/rolled-back → high_reject_rate, high_rollback_rate.
    * ``r3`` — quality loop escalated an unresolved blocker → escalation_spike.
    """
    from hyperwilfred_core.engine import RunPaths, load_state, plan, save_state
    from hyperwilfred_core.run_store import RunEventStatus, RunStore
    from hyperwilfred_core.schemas import (
        AcceptanceVerdict,
        BuildObjective,
        PatchViolation,
        QualityLoopState,
    )

    repo = tmp_path / "histrepo"
    repo.mkdir()

    # r1: patch loop maxed + diff over budget + repeated gate failures
    plan(repo_root=repo, objective=BuildObjective(id="o1", title="t1", objective="build one"),
         run_id="r1")
    p1 = RunPaths(repo, "r1")
    s1 = load_state(p1)
    s1.quality = QualityLoopState(iteration=2, max_iterations=3, used_fallback=True, passed=True)
    save_state(p1, s1)
    p1.verdicts_dir.mkdir(parents=True, exist_ok=True)
    (p1.verdicts_dir / "s0.json").write_text(
        AcceptanceVerdict(
            packet_id="s0", worktree="wt", accepted=False, rolled_back=True,
            patch_violations=[PatchViolation(path="src/a.py", added=500, deleted=10,
                                             reasons=["too-many-changed-lines (510 > 400)"])],
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )
    store1 = RunStore(p1.run_dir)
    for _ in range(2):
        store1.append_event(run_id="r1", stage="evaluate",
                            status=RunEventStatus.gate_failed, message="gate failed")

    # r2: high reject + rollback rate (3 of 4 rejected/rolled-back)
    plan(repo_root=repo, objective=BuildObjective(id="o2", title="t2", objective="build two"),
         run_id="r2")
    p2 = RunPaths(repo, "r2")
    p2.verdicts_dir.mkdir(parents=True, exist_ok=True)
    for i, acc in enumerate([True, False, False, False]):
        (p2.verdicts_dir / f"s{i}.json").write_text(
            AcceptanceVerdict(packet_id=f"s{i}", worktree="wt", accepted=acc,
                              rolled_back=not acc, escapes=[] if acc else ["src/x.py"]
                              ).model_dump_json(indent=2),
            encoding="utf-8",
        )

    # r3: quality loop escalated an unresolved blocker
    plan(repo_root=repo, objective=BuildObjective(id="o3", title="t3", objective="build three"),
         run_id="r3")
    p3 = RunPaths(repo, "r3")
    s3 = load_state(p3)
    s3.quality = QualityLoopState(iteration=2, max_iterations=3, escalated=True, open_blocker=1)
    save_state(p3, s3)

    return repo
