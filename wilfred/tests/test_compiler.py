"""The guardrail compiler — emits the runtime-enforcement artifacts, LLM-free + spawn-free.

Proves: it reads guardrails.json + the active packets and writes the PreToolUse hook + registry
+ settings + Codex sandbox/git-hook; the registry maps worktree→policy; the emitted script is a
byte-copy of the committed standalone hook; and it shells out to nothing (no LLM, no subprocess).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hyperwilfred_core import cli, compiler, engine, quality, review, schemas
from hyperwilfred_core.schemas import BuildObjective, Plan, ReviewTargetKind, Slice


def _plan_with_two_slices(repo: Path, run_id: str) -> None:
    paths = engine.RunPaths(repo, run_id)
    plan = Plan(
        objective_id="o",
        product_dir="product",
        slices=[
            Slice(id="api", title="api", allowed_globs=["product/api/**"]),
            Slice(id="ui", title="ui", allowed_globs=["product/ui/**"], depends_on=[]),
        ],
    )
    paths.plan.write_text(plan.model_dump_json(indent=2), encoding="utf-8")


def _run_to_fanout(repo: Path, run_id: str = "c") -> engine.RunPaths:
    engine.plan(
        repo_root=repo,
        objective=BuildObjective(id="o", title="t", objective="build it"),
        run_id=run_id,
        product_dir="product",
    )
    _plan_with_two_slices(repo, run_id)
    engine.fanout_plan(repo_root=repo, run_id=run_id)
    return engine.RunPaths(repo, run_id)


def _run_to_critique(repo: Path, run_id: str = "review") -> dict:
    target = repo / "src" / "app.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def app():\n    return True\n", encoding="utf-8")
    review.create_review(
        repo_root=repo,
        objective=BuildObjective(id="o", title="audit", objective="audit it"),
        kind=ReviewTargetKind.PATHS,
        run_id=run_id,
        paths=["src/app.py"],
    )
    paths = engine.RunPaths(repo, run_id)
    (paths.run_dir / "comprehension.md").write_text("understood\n", encoding="utf-8")
    engine.complete_step(repo_root=repo, run_id=run_id, step_id="comprehend")
    return quality.critique_plan(repo_root=repo, run_id=run_id)["critics"][0]


def test_compile_emits_all_artifacts(tmp_path: Path) -> None:
    repo = tmp_path
    _run_to_fanout(repo)
    out = compiler.compile_guardrails(repo_root=repo, run_id="c")

    hooks = repo / ".hw" / "c" / "hooks"
    for name in ("pretooluse.py", "registry.json", "settings.json",
                 "codex-sandbox.json", "codex-pre-commit.sh"):
        assert (hooks / name).exists(), name
    assert set(out["packets"]) == {"api", "ui"}


def test_registry_maps_worktree_to_policy(tmp_path: Path) -> None:
    repo = tmp_path
    _run_to_fanout(repo)
    compiler.compile_guardrails(repo_root=repo, run_id="c")
    registry = json.loads((repo / ".hw" / "c" / "hooks" / "registry.json").read_text())
    assert registry["run_id"] == "c"
    by_packet = {e["packet_id"]: e for e in registry["entries"]}
    assert by_packet["api"]["allowed_globs"] == ["product/api/**"]
    assert by_packet["ui"]["allowed_globs"] == ["product/ui/**"]
    # worktrees are ABSOLUTE (the hook resolves by absolute file_path, no cwd)
    for e in registry["entries"]:
        assert Path(e["worktree"]).is_absolute()
        assert e["worktree"].endswith(f"worktrees/{e['packet_id']}")
    # the global blocked-by-default list is carried for the anywhere-blocked check
    assert "**/secrets/**" in registry["global_blocked_globs"]
    assert registry["deny_unmatched_writes"] is False
    assert registry["deny_unmatched_reads"] is False


def test_emitted_hook_is_byte_copy_of_committed_source(tmp_path: Path) -> None:
    repo = tmp_path
    _run_to_fanout(repo)
    compiler.compile_guardrails(repo_root=repo, run_id="c")
    emitted = (repo / ".hw" / "c" / "hooks" / "pretooluse.py").read_text(encoding="utf-8")
    committed = Path(compiler._HOOK_SOURCE).read_text(encoding="utf-8")
    assert emitted == committed


def test_settings_fragment_registers_pretooluse_for_guarded_filesystem_tools(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    _run_to_fanout(repo)
    compiler.compile_guardrails(repo_root=repo, run_id="c")
    settings = json.loads((repo / ".hw" / "c" / "hooks" / "settings.json").read_text())
    pre = settings["hooks"]["PreToolUse"][0]
    assert pre["matcher"] == "Read|Grep|Glob|Write|Edit|MultiEdit|NotebookEdit"
    cmd = pre["hooks"][0]["command"]
    assert "pretooluse.py" in cmd and "--registry" in cmd


def test_install_help_uses_transactional_commands_and_finally(tmp_path: Path) -> None:
    repo = tmp_path
    packet = _run_to_critique(repo)
    out = compiler.compile_guardrails(
        repo_root=repo, run_id="review", packet_id=packet["packet_id"]
    )
    help_text = out["install"]["claude"]
    assert "`hw guardrails-install --run-id review --packet-id critic-0-correctness`" in help_text
    assert "`hw guardrails-remove --run-id review`" in help_text
    assert "finally" in help_text
    assert "merge" not in help_text.lower()


def test_codex_artifacts_are_passive_not_driven(tmp_path: Path) -> None:
    repo = tmp_path
    _run_to_fanout(repo)
    compiler.compile_guardrails(repo_root=repo, run_id="c")
    sandbox = json.loads((repo / ".hw" / "c" / "hooks" / "codex-sandbox.json").read_text())
    assert sandbox["sandbox_mode"] == "workspace-write"
    assert sandbox["network_access"] is False
    assert "Passive artifact" in sandbox["_note"]
    hook = (repo / ".hw" / "c" / "hooks" / "codex-pre-commit.sh").read_text()
    assert hook.startswith("#!/usr/bin/env bash")
    assert "PASSIVE artifact" in hook


def test_compile_single_packet_scope(tmp_path: Path) -> None:
    repo = tmp_path
    _run_to_fanout(repo)
    out = compiler.compile_guardrails(repo_root=repo, run_id="c", packet_id="api")
    assert out["packets"] == ["api"]
    registry = json.loads((repo / ".hw" / "c" / "hooks" / "registry.json").read_text())
    assert [e["packet_id"] for e in registry["entries"]] == ["api"]


def test_compile_critique_packet_allows_only_exact_result_path(tmp_path: Path) -> None:
    repo = tmp_path
    packet = _run_to_critique(repo)
    out = compiler.compile_guardrails(
        repo_root=repo, run_id="review", packet_id=packet["packet_id"]
    )

    registry = json.loads((repo / out["artifacts"]["registry"]).read_text(encoding="utf-8"))
    assert out["packets"] == [packet["packet_id"]]
    assert registry["deny_unmatched_writes"] is True
    assert registry["deny_unmatched_reads"] is True
    assert registry["entries"] == [
        {
            "packet_id": packet["packet_id"],
            "worktree": str(repo.resolve()),
            "allowed_globs": [packet["result_path"]],
            "allowed_read_globs": packet["changed_paths"],
            "blocked_globs": [],
            "result_path": packet["result_path"],
            "runtime_allowed_tools": ["Read", "Grep", "Glob", "Write"],
        }
    ]


def test_compiled_review_registry_matches_nextjs_bracket_paths_literally(
    tmp_path: Path,
) -> None:
    """REVIEW packets carry literal paths; the registry must escape glob metacharacters so a
    Next.js ``[projectId]`` folder is neither a crash nor a one-character class."""
    from hyperwilfred_core.hooks import pretooluse

    repo = tmp_path
    route = "app/api/v1/projects/[project-id]/route.ts"
    page = "app/(studio)/projects/[projectId]/page.tsx"
    siblings = ["app/(studio)/projects/p/page.tsx", "app/api/v1/projects/other-id/route.ts"]
    for rel in (route, page, *siblings):
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("export {}\n", encoding="utf-8")
    review.create_review(
        repo_root=repo,
        objective=BuildObjective(id="o", title="audit", objective="audit it"),
        kind=ReviewTargetKind.PATHS,
        run_id="review",
        paths=[route, page],
    )
    paths = engine.RunPaths(repo, "review")
    (paths.run_dir / "comprehension.md").write_text("understood\n", encoding="utf-8")
    engine.complete_step(repo_root=repo, run_id="review", step_id="comprehend")
    packet = quality.critique_plan(repo_root=repo, run_id="review")["critics"][0]
    assert set(packet["changed_paths"]) == {route, page}

    out = compiler.compile_guardrails(
        repo_root=repo, run_id="review", packet_id=packet["packet_id"]
    )
    registry = json.loads((repo / out["artifacts"]["registry"]).read_text(encoding="utf-8"))

    def decide(rel: str) -> bool:
        payload = {"tool_name": "Read", "tool_input": {"file_path": str(repo / rel)}}
        try:
            return pretooluse.decide(payload, registry) is None
        except pretooluse._Deny:
            return False

    assert decide(route) and decide(page)
    for sibling in siblings:
        assert not decide(sibling), sibling


@pytest.mark.parametrize(
    "unsafe_result",
    ("../outside.json", ".hw/another-run/critics/correctness.json"),
)
def test_compile_rejects_critique_result_outside_its_run(
    tmp_path: Path, unsafe_result: str
) -> None:
    repo = tmp_path
    packet = _run_to_critique(repo)
    packet_path = (
        repo
        / ".hw/review/quality/iter-0/critic-packets"
        / f"{packet['lens']}.json"
    )
    packet["result_path"] = unsafe_result
    packet_path.write_text(json.dumps(packet), encoding="utf-8")

    with pytest.raises(compiler.CompilerError, match="result_path must stay inside"):
        compiler.compile_guardrails(
            repo_root=repo, run_id="review", packet_id=packet["packet_id"]
        )


def test_compile_rejects_critique_result_through_existing_symlink(tmp_path: Path) -> None:
    repo = tmp_path
    packet = _run_to_critique(repo)
    critics = repo / ".hw/review/quality/iter-0/critics"
    critics.parent.mkdir(parents=True, exist_ok=True)
    critics.symlink_to(repo / ".hw/review/state.json")
    with pytest.raises(compiler.CompilerError, match="crosses existing symlink"):
        compiler.compile_guardrails(
            repo_root=repo, run_id="review", packet_id=packet["packet_id"]
        )


def test_compile_rejects_packet_from_different_run(tmp_path: Path) -> None:
    repo = tmp_path
    packet = _run_to_critique(repo)
    packet_path = repo / ".hw/review/quality/iter-0/critic-packets/correctness.json"
    packet["run_id"] = "other"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    with pytest.raises(compiler.CompilerError, match="belongs to run"):
        compiler.compile_guardrails(
            repo_root=repo, run_id="review", packet_id=packet["packet_id"]
        )


def test_quoted_hook_command_runs_in_metacharacter_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo with spaces; false"
    repo.mkdir()
    packet = _run_to_critique(repo)
    out = compiler.compile_guardrails(
        repo_root=repo, run_id="review", packet_id=packet["packet_id"]
    )
    settings = json.loads((repo / out["artifacts"]["claude_settings"]).read_text())
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    proc = subprocess.run(
        command,
        shell=True,
        input=json.dumps(
            {"tool_name": "Write", "tool_input": {"file_path": str(repo / packet["result_path"])}}
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_precommit_blocks_nul_delimited_whitespace_path(tmp_path: Path) -> None:
    repo = tmp_path
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _run_to_fanout(repo)
    compiler.compile_guardrails(repo_root=repo, run_id="c")
    blocked = repo / "folder with space/key.pem"
    blocked.parent.mkdir()
    blocked.write_text("not-a-secret", encoding="utf-8")
    subprocess.run(["git", "add", str(blocked)], cwd=repo, check=True)
    hook = repo / ".hw/c/hooks/codex-pre-commit.sh"
    proc = subprocess.run(["bash", str(hook)], cwd=repo, capture_output=True, text=True)
    assert proc.returncode == 1
    assert "folder with space/key.pem" in proc.stderr


def _precommit_exit(repo: Path, staged: list[str]) -> subprocess.CompletedProcess[str]:
    for name in staged:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PLACEHOLDER=1\n", encoding="utf-8")
        subprocess.run(["git", "add", "--", name], cwd=repo, check=True)
    hook = repo / ".hw/c/hooks/codex-pre-commit.sh"
    return subprocess.run(["bash", str(hook)], cwd=repo, capture_output=True, text=True)


@pytest.mark.parametrize(
    ("staged", "allowed"),
    [
        # Owner decision 2026-10-05: a committed `.env.example` template must not block a commit
        # (it blocked a merge that only brought in another project's reviewed template).
        ([".env.example"], True),
        (["app/.env.example"], True),
        ([".env"], False),
        (["app/.env"], False),
        ([".env.local"], False),
        (["app/.env.production"], False),
        (["app/.env.example.bak"], False),
        (["app/env.example/.env"], False),
        # One allowed file never lets a blocked one through in the same commit.
        (["app/.env.example", "app/.env"], False),
    ],
)
def test_precommit_allows_only_the_env_example_template(
    tmp_path: Path, staged: list[str], allowed: bool
) -> None:
    repo = tmp_path
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _run_to_fanout(repo)
    compiler.compile_guardrails(repo_root=repo, run_id="c")
    proc = _precommit_exit(repo, staged)
    assert (proc.returncode == 0) is allowed, proc.stderr


def test_env_example_exception_is_commit_only() -> None:
    """Agents still may not write `.env.example`: the write-time floor is unchanged."""
    assert compiler.COMMIT_ALLOWED_BASENAMES == (".env.example",)
    assert "**/.env.*" in schemas.DEFAULT_BLOCKED_GLOBS


@pytest.mark.parametrize("preexisting", [True, False])
def test_settings_lifecycle_is_idempotent_and_lossless(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], preexisting: bool
) -> None:
    repo = tmp_path
    packet = _run_to_critique(repo)
    compiler.compile_guardrails(
        repo_root=repo, run_id="review", packet_id=packet["packet_id"]
    )
    settings = repo / ".claude/settings.local.json"
    original = b'{  "permissions": {"allow": ["Read"]} }\n' if preexisting else None
    if original is not None:
        settings.parent.mkdir(parents=True)
        settings.write_bytes(original)

    assert cli.main(["--repo", str(repo), "guardrails-install", "--run-id", "review"]) == 0
    assert compiler.install_guardrails(repo_root=repo, run_id="review")["changed"] is False
    backup = repo / ".hw/review/hooks/settings.local.backup"
    assert backup.exists() is preexisting
    if preexisting:
        assert backup.read_bytes() == original
        assert backup.stat().st_mode & 0o777 == 0o600
    capsys.readouterr()

    assert cli.main(["--repo", str(repo), "guardrails-remove", "--run-id", "review"]) == 0
    assert compiler.remove_guardrails(repo_root=repo, run_id="review")["changed"] is False
    assert settings.exists() is preexisting
    if preexisting:
        assert settings.read_bytes() == original
    assert not backup.exists()
    assert not (repo / ".hw/review/hooks/settings.local.lifecycle.json").exists()
    assert not (repo / ".hw/settings.local.owner.json").exists()


def test_settings_owner_rejects_other_run_then_hands_off(tmp_path: Path) -> None:
    repo = tmp_path
    for run_id in ("first", "second"):
        packet = _run_to_critique(repo, run_id)
        compiler.compile_guardrails(
            repo_root=repo, run_id=run_id, packet_id=packet["packet_id"]
        )

    compiler.install_guardrails(repo_root=repo, run_id="first")
    with pytest.raises(compiler.CompilerError, match="owns the active policy"):
        compiler.compile_guardrails(
            repo_root=repo, run_id="second", packet_id="critic-0-correctness"
        )
    with pytest.raises(compiler.CompilerError, match="owned by active run 'first'"):
        compiler.install_guardrails(repo_root=repo, run_id="second")
    with pytest.raises(compiler.CompilerError, match="owned by active run 'first'"):
        compiler.remove_guardrails(repo_root=repo, run_id="second")

    compiler.remove_guardrails(repo_root=repo, run_id="first")
    assert compiler.install_guardrails(repo_root=repo, run_id="second")["changed"] is True
    assert compiler.remove_guardrails(repo_root=repo, run_id="second")["changed"] is True
    assert not (repo / ".hw/settings.local.owner.json").exists()


def test_install_rejects_policy_replaced_between_compile_and_install(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    first = _run_to_critique(repo, "first")
    second = _run_to_critique(repo, "second")
    compiler.compile_guardrails(
        repo_root=repo, run_id="first", packet_id=first["packet_id"]
    )
    compiler.compile_guardrails(
        repo_root=repo, run_id="second", packet_id=second["packet_id"]
    )

    with pytest.raises(compiler.CompilerError, match="not packet 'wrong-packet'"):
        compiler.install_guardrails(
            repo_root=repo, run_id="second", packet_id="wrong-packet"
        )
    assert not (repo / ".claude/settings.local.json").exists()
    assert not (repo / ".hw/settings.local.owner.json").exists()


@pytest.mark.parametrize("preexisting", [True, False])
def test_remove_recovers_failure_after_settings_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preexisting: bool,
) -> None:
    repo = tmp_path
    packet = _run_to_critique(repo)
    compiler.compile_guardrails(
        repo_root=repo, run_id="review", packet_id=packet["packet_id"]
    )
    settings = repo / ".claude/settings.local.json"
    original = b'{ "permissions": { "allow": ["Read"] } }\n'
    if preexisting:
        settings.parent.mkdir(parents=True)
        settings.write_bytes(original)
        settings.chmod(0o640)

    write_record = compiler._write_record

    def fail_installed(path: Path, value: dict) -> None:
        if path.name == "settings.local.lifecycle.json" and value.get("phase") == "installed":
            raise OSError("injected interruption")
        write_record(path, value)

    monkeypatch.setattr(compiler, "_write_record", fail_installed)
    with pytest.raises(OSError, match="injected interruption"):
        compiler.install_guardrails(repo_root=repo, run_id="review")

    lifecycle = repo / ".hw/review/hooks/settings.local.lifecycle.json"
    assert json.loads(lifecycle.read_text())["phase"] == "prepared"
    assert b"PreToolUse" in settings.read_bytes()
    monkeypatch.setattr(compiler, "_write_record", write_record)

    assert compiler.remove_guardrails(repo_root=repo, run_id="review")["changed"] is True
    assert settings.exists() is preexisting
    if preexisting:
        assert settings.read_bytes() == original
        assert settings.stat().st_mode & 0o777 == 0o640
    assert not lifecycle.exists()
    assert not (repo / ".hw/review/hooks/settings.local.backup").exists()
    assert not (repo / ".hw/settings.local.owner.json").exists()


def test_compile_errors_without_packets(tmp_path: Path) -> None:
    repo = tmp_path
    engine.plan(
        repo_root=repo,
        objective=BuildObjective(id="o", title="t", objective="x"),
        run_id="empty",
        product_dir="product",
    )
    with pytest.raises(compiler.CompilerError):
        compiler.compile_guardrails(repo_root=repo, run_id="empty")


def test_compiler_never_shells_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defense of the invariant: the compiler GENERATES files; it spawns nothing."""
    import subprocess

    repo = tmp_path
    _run_to_fanout(repo)

    def _boom(*a, **k):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("compiler must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    out = compiler.compile_guardrails(repo_root=repo, run_id="c")  # must not raise
    assert out["packets"]
