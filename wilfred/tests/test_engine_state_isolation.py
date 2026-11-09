"""The engine's ``.hw/`` run state must never reach the host repo's commits.

Regression for example-org/app (2026-09-23 → 09-29): a pilot branch committed 171
``.hw/`` run artifacts, and the repo's own Task Contract scope check failed on every run until
they were untracked. The acceptance gate exempted ``.hw/`` from scope checks and nothing kept it
out of ``git add -A``.
"""

from __future__ import annotations

from pathlib import Path

from hyperwilfred_core import engine, guardrails
from hyperwilfred_core.schemas import BuildObjective


def _accept(worktree: Path):
    return guardrails.accept(
        packet_id="p",
        worktree=str(worktree),
        allowed_globs=["src/**"],
        blocked_globs=[".git/**"],
        result_file=None,
        max_changed_lines=400,
        max_hunk_span=120,
        full_file_ratio=0.9,
    )


def test_plan_excludes_engine_state_from_git(git_repo: Path, git_helper) -> None:
    engine.plan(
        repo_root=git_repo,
        objective=BuildObjective(id="o", title="t", objective="build it"),
        run_id="r1",
    )
    assert (git_repo / ".hw" / "r1" / "state.json").exists()
    assert ".hw" not in git_helper(git_repo, "status", "--porcelain", "--untracked-files=all")
    git_helper(git_repo, "add", "-A")
    assert git_helper(git_repo, "diff", "--cached", "--name-only") == ""


def test_exclude_is_written_once(git_repo: Path) -> None:
    assert guardrails.ensure_engine_state_excluded(git_repo) is True
    assert guardrails.ensure_engine_state_excluded(git_repo) is True
    exclude = (git_repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude.count(".hw/") == 1
    assert "/.hw/" in exclude.splitlines()


def test_exclude_does_not_hide_nested_hw_dirs(git_repo: Path) -> None:
    """Only the root run-state folder is excluded; a nested ``.hw/`` stays visible to the gate."""
    guardrails.ensure_engine_state_excluded(git_repo)
    (git_repo / "src" / ".hw").mkdir()
    (git_repo / "src" / ".hw" / "planted.py").write_text("x = 1\n", encoding="utf-8")
    assert "src/.hw/planted.py" in guardrails.changed_paths(str(git_repo))
    verdict = _accept(git_repo)
    assert "src/.hw/planted.py" in verdict.changed_paths


def test_exclude_outside_git_is_a_no_op(tmp_path: Path) -> None:
    assert guardrails.ensure_engine_state_excluded(tmp_path) is False


def test_untracked_engine_state_stays_exempt(git_repo: Path) -> None:
    (git_repo / ".hw" / "r").mkdir(parents=True)
    (git_repo / ".hw" / "r" / "state.json").write_text("{}", encoding="utf-8")
    (git_repo / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    verdict = _accept(git_repo)
    assert verdict.accepted is True
    assert verdict.escapes == []


def test_staged_engine_state_is_rejected(git_repo: Path, git_helper) -> None:
    (git_repo / ".hw" / "r").mkdir(parents=True)
    (git_repo / ".hw" / "r" / "state.json").write_text("{}", encoding="utf-8")
    git_helper(git_repo, "add", "-f", ".hw/r/state.json")
    verdict = _accept(git_repo)
    assert verdict.accepted is False
    assert ".hw/r/state.json" in verdict.escapes


def test_tracked_engine_state_fails_the_gate(git_repo: Path, git_helper) -> None:
    (git_repo / ".hw" / "old").mkdir(parents=True)
    (git_repo / ".hw" / "old" / "events.jsonl").write_text("{}\n", encoding="utf-8")
    git_helper(git_repo, "add", "-f", ".hw")
    git_helper(git_repo, "commit", "-q", "-m", "oops: commit run state")

    check = engine._engine_state_check(git_repo, git_repo)
    assert check.passed is False
    assert ".hw/old/events.jsonl" in (check.details or "")
    assert "git rm -r --cached .hw" in (check.details or "")

    git_helper(git_repo, "rm", "-r", "-q", "--cached", ".hw")
    git_helper(git_repo, "commit", "-q", "-m", "untrack run state")
    assert engine._engine_state_check(git_repo, git_repo).passed is True


def test_tracked_state_check_fails_closed_on_git_error(git_repo: Path) -> None:
    """A repository git cannot list must fail the check, never read as "nothing tracked"."""
    (git_repo / ".git" / "index").write_bytes(b"not an index")
    check = engine._engine_state_check(git_repo, git_repo)
    assert check.passed is False
    assert "cannot list tracked files" in (check.details or "")


def test_tracked_state_outside_git_is_empty(tmp_path: Path) -> None:
    assert guardrails.tracked_engine_state(tmp_path) == []
    assert engine._engine_state_check(tmp_path, tmp_path).passed is True


def test_broken_git_file_fails_closed(tmp_path: Path) -> None:
    """A ``.git`` file pointing nowhere is a broken repository, not "outside any repository"."""
    (tmp_path / ".git").write_text("gitdir: /nonexistent/gitdir\n", encoding="utf-8")
    check = engine._engine_state_check(tmp_path, tmp_path)
    assert check.passed is False
    assert "cannot list tracked files" in (check.details or "")
