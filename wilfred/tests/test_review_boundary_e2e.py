import json
import subprocess
from pathlib import Path

import pytest

from hyperwilfred_core import compiler, engine, quality, review
from hyperwilfred_core.schemas import BuildObjective, ReviewTargetKind, StepStatus

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_ADAPTER = REPO_ROOT / "skills/hyperwilfred-review/SKILL.md"
CODEX_ADAPTER = REPO_ROOT / "skills/codex/hyperwilfred-review/SKILL.md"


def _fire_tool(command: str, tool_name: str, tool_input: dict) -> tuple[int, str]:
    proc = subprocess.run(
        command,
        shell=True,
        input=json.dumps({"tool_name": tool_name, "tool_input": tool_input}),
        capture_output=True,
        text=True,
        check=False,
    )
    decision = ""
    if proc.stdout.strip():
        decision = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]
    return proc.returncode, decision


def _fire_hook(command: str, target: Path) -> tuple[int, str]:
    return _fire_tool(command, "Write", {"file_path": str(target)})


def test_codex_review_adapter_is_explicitly_absent() -> None:
    assert not CODEX_ADAPTER.exists(), "Codex REVIEW adapter has not been implemented"


def test_review_critic_boundary_end_to_end(git_repo: Path) -> None:
    adapter = CLAUDE_ADAPTER.read_text(encoding="utf-8").lower()
    assert "hyperwilfred-review" in adapter
    assert "read-only" in adapter and "result_path" in adapter
    assert "compile-guardrails" in adapter and ".claude/settings.local.json" in adapter
    assert "temporary hook" in adapter and "remove" in adapter
    assert "`read,grep,glob,write`" in adapter
    assert "no bash, edit, multiedit, notebookedit" in adapter

    run_id = "boundary-claude"
    review.create_review(
        repo_root=git_repo,
        objective=BuildObjective(id="o", title="audit", objective="audit target"),
        kind=ReviewTargetKind.PATHS,
        run_id=run_id,
        paths=["src/app.py"],
    )
    run_dir = git_repo / ".hw" / run_id
    (run_dir / "comprehension.md").write_text("understood\n", encoding="utf-8")
    engine.complete_step(repo_root=git_repo, run_id=run_id, step_id="comprehend")
    packets = quality.critique_plan(repo_root=git_repo, run_id=run_id)["critics"]
    packet = packets[0]

    with pytest.raises(review.ReviewError, match="incomplete critic panel"):
        engine.complete_step(repo_root=git_repo, run_id=run_id, step_id="critique")

    result = git_repo / packet["result_path"]
    assert result.is_relative_to(git_repo / ".hw")
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_text("{malformed", encoding="utf-8")
    with pytest.raises(ValueError):
        quality.record_critique(
            repo_root=git_repo, run_id=run_id, packet_id=packet["packet_id"],
            result_file=str(result),
        )
    sibling_report = result.with_name("sibling.json")
    sibling_report.write_text(
        json.dumps({"critic": "fake", "lens": packet["lens"], "findings": []}),
        encoding="utf-8",
    )
    with pytest.raises(quality.QualityError, match="must be read from"):
        quality.record_critique(
            repo_root=git_repo, run_id=run_id, packet_id=packet["packet_id"],
            result_file=str(sibling_report),
        )
    with pytest.raises(quality.QualityError, match="unknown or ambiguous"):
        quality.record_critique(
            repo_root=git_repo, run_id=run_id, packet_id="critic-0-absent",
            result_file=str(result),
        )

    manifest = compiler.compile_guardrails(
        repo_root=git_repo, run_id=run_id, packet_id=packet["packet_id"]
    )
    settings_path = git_repo / manifest["artifacts"]["claude_settings"]
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    target = git_repo / packet["changed_paths"][0]
    assert "return a + b" in target.read_text(encoding="utf-8")
    assert _fire_tool(command, "Read", {"file_path": str(target)}) == (0, "")
    assert _fire_tool(
        command,
        "Glob",
        {"path": str(git_repo), "pattern": packet["changed_paths"][0]},
    ) == (0, "")
    assert _fire_tool(
        command, "Read", {"file_path": str(git_repo / ".env")}
    ) == (2, "deny")
    assert _fire_tool(
        command, "Grep", {"path": str(git_repo / "unreviewed.py")}
    ) == (2, "deny")
    denied_targets = (
        target,
        result.with_name("sibling.json"),
        run_dir / "state.json",
    )
    for denied_target in denied_targets:
        assert _fire_hook(command, denied_target) == (2, "deny"), denied_target
    assert _fire_hook(command, result) == (0, "")

    # A component rebound after compile must not turn the lexical allow-list into an escape.
    original_parent = result.parent.with_name("critics-original")
    result.parent.rename(original_parent)
    outside = git_repo.parent / "outside-critics"
    outside.mkdir(exist_ok=True)
    result.parent.symlink_to(outside, target_is_directory=True)
    assert _fire_hook(command, result) == (2, "deny")
    result.parent.unlink()
    original_parent.rename(result.parent)
    result.unlink()
    result.symlink_to(run_dir / "state.json")
    assert _fire_hook(command, result) == (2, "deny")
    result.unlink()

    result.write_text(
        json.dumps({"critic": "fake", "lens": "security", "findings": []}),
        encoding="utf-8",
    )
    with pytest.raises(quality.QualityError, match="does not match packet lens"):
        quality.record_critique(
            repo_root=git_repo, run_id=run_id, packet_id=packet["packet_id"],
            result_file=str(result),
        )
    result.unlink()
    result.symlink_to(sibling_report)
    with pytest.raises(quality.QualityError, match="crosses symlink"):
        quality.record_critique(
            repo_root=git_repo, run_id=run_id, packet_id=packet["packet_id"],
            result_file=str(result),
        )
    result.unlink()

    result.write_text(
        json.dumps({"critic": "fake", "lens": packet["lens"], "findings": []}),
        encoding="utf-8",
    )
    accepted = quality.record_critique(
        repo_root=git_repo, run_id=run_id, packet_id=packet["packet_id"],
        result_file=str(result),
    )
    assert accepted["lens"] == packet["lens"]
    with pytest.raises(review.ReviewError, match="incomplete critic panel"):
        engine.complete_step(repo_root=git_repo, run_id=run_id, step_id="critique")
    for remaining in packets[1:]:
        remaining_result = git_repo / remaining["result_path"]
        remaining_result.write_text(
            json.dumps(
                {"critic": "fake", "lens": remaining["lens"], "findings": []}
            ),
            encoding="utf-8",
        )
        quality.record_critique(
            repo_root=git_repo, run_id=run_id, packet_id=remaining["packet_id"],
            result_file=str(remaining_result),
        )
    engine.complete_step(repo_root=git_repo, run_id=run_id, step_id="critique")
    state = engine.load_state(engine.RunPaths(git_repo, run_id))
    assert state.step_status["critique"] is StepStatus.DONE
