"""The guardrail floor — does it ACTUALLY block an out-of-scope write and roll back clean?

These are the load-bearing tests of the Phase-1 moat. They use a real git working dir.
"""

from __future__ import annotations

from pathlib import Path

from hyperwilfred_core import guardrails


def test_in_scope_write_is_accepted(git_repo: Path) -> None:
    (git_repo / "src" / "feature.py").write_text("x = 1\n", encoding="utf-8")
    verdict = guardrails.accept(
        packet_id="p1",
        worktree=str(git_repo),
        allowed_globs=["src/**"],
        blocked_globs=[".git/**"],
        result_file=None,
        max_changed_lines=400,
        max_hunk_span=120,
        full_file_ratio=0.9,
    )
    assert verdict.accepted is True
    assert verdict.escapes == []
    assert (git_repo / "src" / "feature.py").exists()


def test_out_of_scope_write_is_rejected_and_rolled_back_clean(git_repo: Path) -> None:
    # in-scope file
    (git_repo / "src" / "ok.py").write_text("y = 2\n", encoding="utf-8")
    # ESCAPE: a write outside the allow-list, at repo root
    (git_repo / "escaped_secret.txt").write_text("leak\n", encoding="utf-8")

    verdict = guardrails.accept(
        packet_id="p2",
        worktree=str(git_repo),
        allowed_globs=["src/**"],
        blocked_globs=[".git/**"],
        result_file=None,
        max_changed_lines=400,
        max_hunk_span=120,
        full_file_ratio=0.9,
    )
    assert verdict.accepted is False
    assert "escaped_secret.txt" in verdict.escapes
    assert verdict.rolled_back is True
    assert verdict.tree_clean_after is True
    # BOTH files gone — rollback nukes the whole packet, not just the escape
    assert not (git_repo / "escaped_secret.txt").exists()
    assert not (git_repo / "src" / "ok.py").exists()
    assert guardrails.is_clean(str(git_repo))


def test_blocked_glob_beats_allow_glob(git_repo: Path) -> None:
    # A too-broad allow-list ("**") would admit everything — blocked must still win.
    (git_repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    verdict = guardrails.accept(
        packet_id="p3",
        worktree=str(git_repo),
        allowed_globs=["**"],
        blocked_globs=["**/.env"],
        result_file=None,
        max_changed_lines=400,
        max_hunk_span=120,
        full_file_ratio=0.9,
    )
    assert verdict.accepted is False
    assert ".env" in verdict.escapes
    assert not (git_repo / ".env").exists()


def test_patch_guard_rejects_full_file_rewrite(git_repo: Path) -> None:
    # Rewrite README entirely (delete all 4 lines, add 6 new ones).
    (git_repo / "README.md").write_text(
        "totally\nnew\ncontent\nreplacing\neverything\nhere\n", encoding="utf-8"
    )
    verdict = guardrails.accept(
        packet_id="p4",
        worktree=str(git_repo),
        allowed_globs=["**"],
        blocked_globs=[],
        result_file=None,
        max_changed_lines=3,  # surgical budget
        max_hunk_span=120,
        full_file_ratio=0.9,
    )
    assert verdict.accepted is False
    assert any("README.md" == v.path for v in verdict.patch_violations)
    assert verdict.rolled_back is True


def test_patch_guard_allows_surgical_edit(git_repo: Path) -> None:
    text = (git_repo / "README.md").read_text(encoding="utf-8")
    (git_repo / "README.md").write_text(text.replace("line2", "line2-edited"), encoding="utf-8")
    verdict = guardrails.accept(
        packet_id="p5",
        worktree=str(git_repo),
        allowed_globs=["**"],
        blocked_globs=[],
        result_file=None,
        max_changed_lines=20,
        max_hunk_span=120,
        full_file_ratio=0.9,
    )
    assert verdict.accepted is True
    assert verdict.patch_violations == []


def test_hunk_span_too_wide_is_rejected(git_repo: Path) -> None:
    big = "\n".join(f"l{i}" for i in range(50)) + "\n"
    (git_repo / "src" / "big.py").write_text("seed\n", encoding="utf-8")
    # commit the seed so the next write is a MODIFY with a wide hunk
    import subprocess

    subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(git_repo), "commit", "-q", "-m", "seed"], check=True
    )
    (git_repo / "src" / "big.py").write_text(big, encoding="utf-8")
    verdict = guardrails.accept(
        packet_id="p6",
        worktree=str(git_repo),
        allowed_globs=["src/**"],
        blocked_globs=[],
        result_file=None,
        max_changed_lines=400,
        max_hunk_span=10,
        full_file_ratio=0.9,
    )
    assert verdict.accepted is False
    assert any("hunk-span-too-wide" in r for v in verdict.patch_violations for r in v.reasons)


def test_result_read_from_disk_only(git_repo: Path, tmp_path: Path) -> None:
    result = tmp_path / "result.json"
    result.write_text('{"ok": true}\n', encoding="utf-8")
    (git_repo / "src" / "f.py").write_text("z = 3\n", encoding="utf-8")
    verdict = guardrails.accept(
        packet_id="p7",
        worktree=str(git_repo),
        allowed_globs=["src/**"],
        blocked_globs=[],
        result_file=str(result),
        max_changed_lines=400,
        max_hunk_span=120,
        full_file_ratio=0.9,
    )
    assert verdict.result_read_from_disk is True
    assert verdict.result_bytes > 0


def test_acceptance_gate_fails_closed_outside_a_git_worktree(tmp_path: Path) -> None:
    """A folder git cannot report on is an error, never an empty (accepted) change set.

    Threat-model finding F-15: ``git status`` on a non-repository or a missing folder exits
    non-zero with no output, which used to read as "no changes" and ACCEPT the packet.
    """
    import pytest

    plain = tmp_path / "plain"
    plain.mkdir()
    for worktree in (plain, tmp_path / "missing"):
        with pytest.raises(guardrails.GuardrailError, match="fails closed"):
            guardrails.accept(
                packet_id="p-nogit",
                worktree=str(worktree),
                allowed_globs=["src/**"],
                blocked_globs=[],
                result_file=None,
                max_changed_lines=400,
                max_hunk_span=120,
                full_file_ratio=0.9,
            )
    with pytest.raises(guardrails.GuardrailError):
        guardrails.rollback(str(plain))


def test_acceptance_gate_fails_closed_when_git_cannot_list_the_tree(git_repo: Path) -> None:
    """A worktree that answers ``rev-parse`` but whose index is corrupt must raise, not accept.

    Independent review of Fable review 5 (2026-10-24) planted a blocked file and garbage in
    ``.git/index``: ``git status`` exited 128 with no output, which read as "no changes".
    """
    import pytest

    (git_repo / "secrets.env").write_text("TOKEN=1\n", encoding="utf-8")
    (git_repo / ".git" / "index").write_bytes(b"garbage")
    with pytest.raises(guardrails.GuardrailError, match="fails closed"):
        guardrails.accept(
            packet_id="p-corrupt",
            worktree=str(git_repo),
            allowed_globs=["src/**"],
            blocked_globs=["**/*.env"],
            result_file=None,
            max_changed_lines=400,
            max_hunk_span=120,
            full_file_ratio=0.9,
        )
    assert (git_repo / "secrets.env").exists(), "nothing was judged, so nothing was rolled back"
