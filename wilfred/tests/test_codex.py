"""L2b Codex adapter wiring (Phase 4).

Under test: the engine WIRES the Phase-3 passive Codex artifacts into a live worker config —
installs the git pre-commit hook into the worker's worktree (and it actually blocks a blocked-path
commit), emits a manifest that reads the worker result from DISK not stdout (Issue #15451), and
owns explicit concurrency-capped multi-wave batching. No engine logic is reimplemented.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from hyperwilfred_core import codex, compiler, engine
from hyperwilfred_core.run_store import atomic_write_text
from hyperwilfred_core.schemas import BuildObjective, Plan, Slice


def _objective() -> BuildObjective:
    return BuildObjective(id="obj-1", title="Landing", objective="build a landing page")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _fresh_repo(path: Path) -> Path:
    """A git repo using the DEFAULT hooks dir (so an installed pre-commit hook actually fires)."""
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("# product\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _setup_fanout(repo: Path, run_id: str, slices: list[Slice]) -> engine.RunPaths:
    engine.plan(repo_root=repo, objective=_objective(), run_id=run_id, product_dir="products/x")
    paths = engine.RunPaths(repo, run_id)
    plan = Plan(objective_id="obj-1", product_dir="products/x", slices=slices)
    atomic_write_text(paths.plan, plan.model_dump_json(indent=2) + "\n")
    engine.fanout_plan(repo_root=repo, run_id=run_id)
    return paths


def test_prepare_requires_compiled_guardrails(tmp_path: Path) -> None:
    repo = _fresh_repo(tmp_path / "repo")
    _setup_fanout(repo, "r1", [Slice(id="a", title="A", allowed_globs=["products/x/**"])])
    wt = tmp_path / "wt-a"
    _git(repo, "worktree", "add", "--detach", str(wt), "HEAD")
    # No compile-guardrails run yet → no codex-sandbox.json.
    with pytest.raises(codex.CodexError, match="compile-guardrails"):
        codex.prepare_codex_worker(repo_root=repo, run_id="r1", packet_id="a", worktree=wt)


def test_prepare_emits_disk_read_manifest_and_toml(tmp_path: Path) -> None:
    repo = _fresh_repo(tmp_path / "repo")
    _setup_fanout(repo, "r2", [Slice(id="a", title="Slice A", allowed_globs=["products/x/a/**"])])
    compiler.compile_guardrails(repo_root=repo, run_id="r2")
    wt = tmp_path / "wt-a"
    _git(repo, "worktree", "add", "--detach", str(wt), "HEAD")

    manifest = codex.prepare_codex_worker(repo_root=repo, run_id="r2", packet_id="a", worktree=wt)

    # Issue #15451: the worker result is read from disk, never stdout.
    assert manifest["read_result_from"] == "disk"
    assert manifest["result_path"].endswith(".hw/r2/results/a.json")
    assert Path(manifest["result_path"]).is_absolute()
    # Sandbox scope is workspace-write + network OFF + the worktree as the only writable root.
    assert manifest["sandbox"]["mode"] == "workspace-write"
    assert manifest["sandbox"]["network_access"] is False
    assert manifest["sandbox"]["writable_roots"] == [str(wt.resolve())]
    assert "--cd" in manifest["codex_exec_flags"]
    # The manifest persisted to disk (the adapter consumes it from a known path).
    assert (repo / ".hw" / "r2" / "codex" / "a.json").exists()

    # The TOML agent def is valid TOML and carries the cd worktree + disk result_path.
    parsed = tomllib.loads(manifest["agent_toml"])
    agent = next(iter(parsed["agents"].values()))
    assert agent["cd"] == str(wt.resolve())
    assert agent["read_result_from"] == "disk"
    assert agent["sandbox"] == "workspace-write"
    assert agent["network_access"] is False


def test_installed_hook_blocks_a_blocked_path_commit(tmp_path: Path) -> None:
    repo = _fresh_repo(tmp_path / "repo")
    _setup_fanout(repo, "r3", [Slice(id="a", title="A", allowed_globs=["products/x/**"])])
    compiler.compile_guardrails(repo_root=repo, run_id="r3")
    wt = tmp_path / "wt-a"
    _git(repo, "worktree", "add", "--detach", str(wt), "HEAD")

    manifest = codex.prepare_codex_worker(repo_root=repo, run_id="r3", packet_id="a", worktree=wt)
    hook = Path(manifest["pre_commit_hook"])
    assert hook.exists()
    import os

    assert os.access(hook, os.X_OK), "the installed pre-commit hook must be executable"

    # A blocked path (.env) staged in the worktree must be REFUSED at commit time by the hook.
    (wt / ".env").write_text("SECRET=1\n", encoding="utf-8")
    _git(wt, "add", ".env")
    blocked = _git(wt, "commit", "-m", "leak secret")
    assert blocked.returncode != 0, "the git pre-commit hook must block a blocked-path commit"
    assert "guardrail" in (blocked.stderr + blocked.stdout).lower()

    # An in-scope file commits cleanly (the hook only blocks globally-blocked paths).
    _git(wt, "reset", "-q", "HEAD")
    (wt / "ok.py").write_text("x = 1\n", encoding="utf-8")
    _git(wt, "add", "ok.py")
    ok = _git(wt, "commit", "-m", "add ok")
    assert ok.returncode == 0, ok.stderr


def test_prepare_can_skip_hook_install(tmp_path: Path) -> None:
    repo = _fresh_repo(tmp_path / "repo")
    _setup_fanout(repo, "r4", [Slice(id="a", title="A", allowed_globs=["products/x/**"])])
    compiler.compile_guardrails(repo_root=repo, run_id="r4")
    wt = tmp_path / "wt-a"
    _git(repo, "worktree", "add", "--detach", str(wt), "HEAD")
    manifest = codex.prepare_codex_worker(
        repo_root=repo, run_id="r4", packet_id="a", worktree=wt, install_hook=False
    )
    assert manifest["pre_commit_hook"] is None


def test_codex_batches_splits_a_wave_wider_than_the_cap(tmp_path: Path) -> None:
    repo = _fresh_repo(tmp_path / "repo")
    # 8 INDEPENDENT slices → one dependency wave of 8; cap 6 → 2 sub-batches (6 + 2).
    slices = [
        Slice(id=f"s{i}", title=f"S{i}", allowed_globs=[f"products/x/s{i}/**"]) for i in range(8)
    ]
    _setup_fanout(repo, "r5", slices)
    out = codex.codex_batches(repo_root=repo, run_id="r5", cap=6)
    assert out["concurrency_cap"] == 6
    assert out["waves"] == 1
    wave = out["batches"][0]
    assert len(wave["packet_ids"]) == 8
    assert [len(b) for b in wave["sub_batches"]] == [6, 2]
    assert wave["split_for_cap"] is True
    assert out["total_sub_batches"] == 2


def test_codex_batches_preserves_dependency_waves(tmp_path: Path) -> None:
    repo = _fresh_repo(tmp_path / "repo")
    slices = [
        Slice(id="a", title="A", allowed_globs=["products/x/a/**"]),
        Slice(id="b", title="B", allowed_globs=["products/x/b/**"], depends_on=["a"]),
    ]
    _setup_fanout(repo, "r6", slices)
    out = codex.codex_batches(repo_root=repo, run_id="r6", cap=6)
    assert out["waves"] == 2  # b depends on a → two sequential waves
    assert out["batches"][0]["packet_ids"] == ["a"]
    assert out["batches"][1]["packet_ids"] == ["b"]


def test_codex_batches_rejects_bad_cap(tmp_path: Path) -> None:
    repo = _fresh_repo(tmp_path / "repo")
    _setup_fanout(repo, "r7", [Slice(id="a", title="A", allowed_globs=["products/x/**"])])
    with pytest.raises(codex.CodexError, match="cap"):
        codex.codex_batches(repo_root=repo, run_id="r7", cap=0)


def test_run_and_packet_ids_cannot_traverse_out_of_the_run_root(tmp_path: Path) -> None:
    """Identifiers are path components under ``.hw``; traversal is refused before any I/O (F-06)."""
    repo = _fresh_repo(tmp_path / "repo")
    with pytest.raises(ValueError, match="run_id"):
        codex.codex_batches(repo_root=repo, run_id="../outside", cap=6)
    with pytest.raises(ValueError, match="run_id"):
        compiler.compile_guardrails(repo_root=repo, run_id="../outside")
    _setup_fanout(repo, "r7", [Slice(id="a", title="A", allowed_globs=["products/x/**"])])
    with pytest.raises(ValueError, match="packet_id"):
        codex.prepare_codex_worker(
            repo_root=repo, run_id="r7", packet_id="../a", worktree=repo, install_hook=False
        )


def test_compiler_rejects_traversal_packet_id_and_cross_run_packets(tmp_path: Path) -> None:
    """``compile-guardrails --packet-id ../../B/packets/p`` must not install another run's scope."""
    repo = _fresh_repo(tmp_path / "repo")
    _setup_fanout(repo, "r8", [Slice(id="a", title="A", allowed_globs=["products/x/**"])])
    with pytest.raises(ValueError, match="packet_id"):
        compiler.compile_guardrails(repo_root=repo, run_id="r8", packet_id="../r8/packets/a")
    # A packet file copied from another run carries that run's id and is refused.
    paths = engine.RunPaths(repo, "r8")
    foreign = paths.packets_dir / "a.json"
    foreign.write_text(
        foreign.read_text(encoding="utf-8").replace('"run_id": "r8"', '"run_id": "other"'),
        encoding="utf-8",
    )
    with pytest.raises(compiler.CompilerError, match="belongs to run"):
        compiler.compile_guardrails(repo_root=repo, run_id="r8", packet_id="a")
