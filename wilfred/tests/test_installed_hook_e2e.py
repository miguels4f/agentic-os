"""End-to-end test of the INSTALLED PreToolUse hook path — not just the pure decision unit.

The other tests cover the pieces in isolation: ``test_pretooluse_hook.py`` exercises the
``decide``/``run`` functions and the emitted script as a subprocess against a HAND-BUILT registry,
and ``test_compiler.py`` checks the compiler writes the right files. This test wires the WHOLE
chain the way the Claude Code harness actually runs it:

    hw compile-guardrails  ->  emitted .claude settings.json fragment  ->  the EXACT command
    string in that fragment, executed in a shell with the PreToolUse payload on stdin  ->  exit
    code (2 = block before the write lands, 0 = allow/defer).

We never hand-type the command — we parse it out of the emitted ``settings.json`` so the test
proves the *installed* command, exactly as a Claude Code ``"type": "command"`` hook is invoked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hyperwilfred_core import compiler, engine
from hyperwilfred_core.schemas import BuildObjective, Plan, Slice

# --------------------------------------------------------------------------- run scaffolding


def _run_to_fanout(repo: Path, run_id: str = "e2e") -> engine.RunPaths:
    """Real pipeline: plan -> two scoped slices -> fanout packets (each with its own worktree)."""
    engine.plan(
        repo_root=repo,
        objective=BuildObjective(id="o", title="t", objective="build it"),
        run_id=run_id,
        product_dir="product",
    )
    paths = engine.RunPaths(repo, run_id)
    plan = Plan(
        objective_id="o",
        product_dir="product",
        slices=[
            Slice(id="api", title="api", allowed_globs=["product/api/**"]),
            Slice(id="ui", title="ui", allowed_globs=["product/ui/**"]),
        ],
    )
    paths.plan.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    engine.fanout_plan(repo_root=repo, run_id=run_id)
    return paths


def _installed_command(repo: Path, run_id: str = "e2e") -> str:
    """Compile, then return the EXACT PreToolUse command string from the emitted settings.json."""
    compiler.compile_guardrails(repo_root=repo, run_id=run_id)
    settings = json.loads((repo / ".hw" / run_id / "hooks" / "settings.json").read_text())
    group = settings["hooks"]["PreToolUse"][0]
    assert group["matcher"] == "Read|Grep|Glob|Write|Edit|MultiEdit|NotebookEdit"
    return group["hooks"][0]["command"]


def _fire(command: str, payload: dict) -> tuple[int, str]:
    """Invoke the hook command the way Claude Code does: shell + payload JSON on stdin."""
    proc = subprocess.run(
        command, shell=True, input=json.dumps(payload),
        capture_output=True, text=True, check=False,
    )
    decision = ""
    if proc.stdout.strip():
        decision = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]
    return proc.returncode, decision


# --------------------------------------------------------------------------- the e2e proof


def test_installed_command_blocks_out_of_scope_write(tmp_path: Path) -> None:
    repo = tmp_path
    _run_to_fanout(repo)
    command = _installed_command(repo)
    api_wt = repo / ".hw" / "e2e" / "worktrees" / "api"

    # Inside the 'api' worker worktree but under product/ui/** — escapes api's allow-list.
    target = str(api_wt / "product" / "ui" / "x.py")
    code, dec = _fire(command, {"tool_name": "Write", "tool_input": {"file_path": target}})
    assert code == 2 and dec == "deny"


def test_installed_command_blocks_globally_blocked_path(tmp_path: Path) -> None:
    repo = tmp_path
    _run_to_fanout(repo)
    command = _installed_command(repo)

    # A secret path is blocked ANYWHERE, even fully outside every worktree.
    for target in (
        "/home/u/.ssh/id_rsa",
        str(repo / ".hw" / "e2e" / "worktrees" / "api" / "product" / "api" / ".env"),
    ):
        code, dec = _fire(command, {"tool_name": "Write", "tool_input": {"file_path": target}})
        assert code == 2 and dec == "deny", target


def test_installed_command_allows_in_scope_write(tmp_path: Path) -> None:
    repo = tmp_path
    _run_to_fanout(repo)
    command = _installed_command(repo)
    api_wt = repo / ".hw" / "e2e" / "worktrees" / "api"

    # Inside the 'api' worktree AND under product/api/** — allowed → defer (exit 0, no output).
    target = str(api_wt / "product" / "api" / "x.py")
    code, dec = _fire(command, {"tool_name": "Write", "tool_input": {"file_path": target}})
    assert code == 0 and dec == ""


def test_installed_command_out_of_scope_write_never_lands(tmp_path: Path) -> None:
    """The block is BEFORE the write: exit 2 and the target file is never created."""
    repo = tmp_path
    _run_to_fanout(repo)
    command = _installed_command(repo)
    api_wt = repo / ".hw" / "e2e" / "worktrees" / "api"
    target = api_wt / "elsewhere" / "evil.py"
    assert not target.exists()

    code, _ = _fire(command, {"tool_name": "Write", "tool_input": {"file_path": str(target)}})
    assert code == 2
    assert not target.exists(), "a blocked write must never land on disk"


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_installed_command_gates_every_write_tool(tool: str, tmp_path: Path) -> None:
    repo = tmp_path
    _run_to_fanout(repo)
    command = _installed_command(repo)
    api_wt = repo / ".hw" / "e2e" / "worktrees" / "api"
    bad = str(api_wt / "elsewhere" / "evil")
    if tool == "MultiEdit":
        tool_input: dict = {"edits": [{"file_path": bad}]}
    elif tool == "NotebookEdit":
        tool_input = {"notebook_path": bad}
    else:
        tool_input = {"file_path": bad}
    code, dec = _fire(command, {"tool_name": tool, "tool_input": tool_input})
    assert code == 2 and dec == "deny"


# ----------------------------------------------------------------------- live committed artifacts
#
# These lock the artifacts that were committed to THIS repo by the live install. They are
# path-agnostic (no machine-specific absolute paths asserted) and SKIP gracefully if the hook
# was removed per the PR's documented removal steps — so following those steps never breaks CI.


def _repo_root_from_test() -> Path:
    # tests/ -> hyperwilfred-core/ -> packages/ -> repo root
    return Path(__file__).resolve().parents[3]


def test_committed_registry_shape_if_present() -> None:
    reg_path = _repo_root_from_test() / ".claude" / "guardrails" / "registry.json"
    if not reg_path.is_file():
        pytest.skip("no committed guardrail registry (hook not installed in this checkout)")
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    assert isinstance(reg.get("entries"), list) and reg["entries"], "registry needs >=1 entry"
    assert "**/secrets/**" in reg.get("global_blocked_globs", []), "protected-paths floor present"
    for entry in reg["entries"]:
        assert Path(entry["worktree"]).is_absolute(), "worktree must be absolute"
        assert isinstance(entry["allowed_globs"], list)
        assert isinstance(entry["blocked_globs"], list)


def test_committed_settings_hook_shape_if_present() -> None:
    settings_path = _repo_root_from_test() / ".claude" / "settings.json"
    if not settings_path.is_file():
        pytest.skip("no committed .claude/settings.json")
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    groups = settings.get("hooks", {}).get("PreToolUse", [])
    ours = [
        g for g in groups
        if any("pretooluse.py" in h.get("command", "") for h in g.get("hooks", []))
    ]
    if not ours:
        pytest.skip("PreToolUse guardrail hook not installed (removed per documented steps)")
    group = ours[0]
    assert "Write" in group["matcher"] and "Edit" in group["matcher"]
    cmd = group["hooks"][0]["command"]
    assert "pretooluse.py" in cmd and "--registry" in cmd
