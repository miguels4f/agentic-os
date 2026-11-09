"""CLI surface for the Phase-4 commands: resume, status, codex-prepare, codex-batches."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hyperwilfred_core import engine
from hyperwilfred_core.cli import main
from hyperwilfred_core.run_store import atomic_write_text
from hyperwilfred_core.schemas import BuildObjective, Plan, Slice


def _objective() -> BuildObjective:
    return BuildObjective(id="obj-1", title="Landing", objective="build a landing page")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _fresh_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("# product\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def test_resume_cli_reports_cursor_after_lost_artifact(tmp_path: Path, capsys) -> None:
    engine.plan(repo_root=tmp_path, objective=_objective(), run_id="r1")
    paths = engine.RunPaths(tmp_path, "r1")
    # frame done + valid, then a crash before decompose produced plan.json.
    paths.artifact("frame.md").write_text("framed\n", encoding="utf-8")
    main(["--repo", str(tmp_path), "complete-step", "--run-id", "r1", "--step", "frame"])
    capsys.readouterr()

    code = main(["--repo", str(tmp_path), "resume", "--run-id", "r1"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["resume_cursor"] == "decompose"
    assert out["applied"] is True


def test_status_cli_is_read_only(tmp_path: Path, capsys) -> None:
    engine.plan(repo_root=tmp_path, objective=_objective(), run_id="r2")
    before = engine.RunPaths(tmp_path, "r2").state.read_text(encoding="utf-8")
    code = main(["--repo", str(tmp_path), "status", "--run-id", "r2"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["resume_cursor"] == "frame"  # nothing done yet
    assert "steps" in out
    assert engine.RunPaths(tmp_path, "r2").state.read_text(encoding="utf-8") == before


def test_codex_batches_cli(tmp_path: Path, capsys) -> None:
    repo = _fresh_repo(tmp_path / "repo")
    engine.plan(repo_root=repo, objective=_objective(), run_id="r3", product_dir="products/x")
    paths = engine.RunPaths(repo, "r3")
    slices = [
        Slice(id=f"s{i}", title=f"S{i}", allowed_globs=[f"products/x/s{i}/**"]) for i in range(7)
    ]
    atomic_write_text(
        paths.plan,
        Plan(objective_id="obj-1", product_dir="products/x", slices=slices).model_dump_json(),
    )
    engine.fanout_plan(repo_root=repo, run_id="r3")

    code = main(["--repo", str(repo), "codex-batches", "--run-id", "r3", "--cap", "6"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["concurrency_cap"] == 6
    assert [len(b) for b in out["batches"][0]["sub_batches"]] == [6, 1]


def test_next_step_on_failed_run_exits_1(tmp_path: Path, capsys) -> None:
    # A terminally-halted (human-rejected) run must signal exit 1 from next-step too — consistent
    # with `hw resume` — and must NOT advance.
    from hyperwilfred_core.schemas import RunStatus

    engine.plan(repo_root=tmp_path, objective=_objective(), run_id="rf")
    paths = engine.RunPaths(tmp_path, "rf")
    state = engine.load_state(paths)
    state.status = RunStatus.FAILED
    engine.save_state(paths, state)

    code = main(["--repo", str(tmp_path), "next-step", "--run-id", "rf"])
    out = json.loads(capsys.readouterr().out)
    assert out["halted"] is True
    assert code == 1


def test_codex_prepare_cli_errors_without_guardrails(tmp_path: Path, capsys) -> None:
    repo = _fresh_repo(tmp_path / "repo")
    engine.plan(repo_root=repo, objective=_objective(), run_id="r4", product_dir="products/x")
    paths = engine.RunPaths(repo, "r4")
    atomic_write_text(
        paths.plan,
        Plan(
            objective_id="obj-1",
            product_dir="products/x",
            slices=[Slice(id="a", title="A", allowed_globs=["products/x/**"])],
        ).model_dump_json(),
    )
    engine.fanout_plan(repo_root=repo, run_id="r4")
    wt = tmp_path / "wt-a"
    _git(repo, "worktree", "add", "--detach", str(wt), "HEAD")

    code = main(
        [
            "--repo", str(repo), "codex-prepare", "--run-id", "r4",
            "--packet-id", "a", "--worktree", str(wt),
        ]
    )
    assert code == 2  # CodexError: compile-guardrails must run first
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "codex_error"
