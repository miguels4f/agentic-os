"""L2b Codex adapter WIRING — turn the Phase-3 passive Codex artifacts into live worker configs.

Phase 3 only GENERATED the Codex artifacts (``codex-sandbox.json`` + ``codex-pre-commit.sh``) as
passive text. Phase 4 WIRES them: this engine-side module reads those compiled artifacts and,
for one fan-out worker, (a) installs the git pre-commit hook into the worker's worktree, (b)
resolves that packet's sandbox scope, and (c) emits a Codex worker MANIFEST — the sandbox flags,
the result_path, and a one-TOML-agent-def-per-role delegation block — that the thin Codex adapter
skill hands to ``codex exec`` / ``spawn_agent``.

It is the symmetric twin of the Claude adapter's ``hw compile-guardrails`` + settings-fragment
merge: there the engine emits a PreToolUse hook the harness installs; here the engine emits a
sandbox + git-hook config the Codex adapter installs. **No engine orchestration logic is
reimplemented** — the same ``hw`` engine computes the plan, the packets, the waves, and the
acceptance gate; this module only translates the compiled guardrail into Codex's enforcement
surface.

Two Codex-specific rules from spec §1 are enforced here, not left to the adapter:

* **Read the worker result from disk, NEVER from stdout** (Codex Issue #15451: ``--json`` /
  ``--output-schema`` are silently ignored when MCP is live). The manifest carries the absolute
  ``result_path`` and ``read_result_from: "disk"``; the engine's acceptance gate
  (``hw record-result``) already reads the packet's ``result_path`` from disk, never stdout.
* **The engine owns explicit multi-wave batching** for the Codex concurrency cap (~6) and
  depth-1 nesting (:func:`codex_batches`). A wave wider than the cap is split into sequential
  sub-batches; the adapter never emits a single naive flat wave and assumes it fits.

Like the rest of the engine: calls NO LLM, spawns NOTHING (it does file I/O + git + text
emission only), and imports nothing from ``common_py``.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

from hyperwilfred_core.path_safety import validate_identifier
from hyperwilfred_core.run_store import RunEventStatus, RunStore, atomic_write_text
from hyperwilfred_core.schemas import Packet, Plan
from hyperwilfred_core.wave_planner import plan_waves

# Codex default ceilings (spec §1 capability matrix — UNVERIFIED until a live spike measures them,
# so the engine designs around them conservatively rather than assuming they match Claude Code).
CODEX_DEFAULT_CONCURRENCY = 6
CODEX_NESTING_DEPTH = 1


class CodexError(RuntimeError):
    """Raised on a Codex-wiring precondition violation (surfaces as CLI exit 2)."""


def _run_dir(repo_root: Path, run_id: str) -> Path:
    # A run id is a path component under ``.hw``; refuse traversal before any file I/O.
    return repo_root / ".hw" / validate_identifier(run_id, label="run_id")


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _worktree_hooks_dir(worktree: Path) -> Path:
    """Resolve the git hooks dir for *worktree*, honouring linked worktrees + core.hooksPath.

    ``git rev-parse --git-path hooks`` returns the correct hooks directory whether the worktree
    is the main checkout or a linked ``git worktree add`` (whose ``.git`` is a file), and respects
    a configured ``core.hooksPath``. The returned path may be relative to the worktree, so we
    resolve it against the worktree.
    """
    proc = _git(worktree, "rev-parse", "--git-path", "hooks")
    if proc.returncode != 0:
        raise CodexError(
            f"{worktree} is not a git worktree (git rev-parse failed: {proc.stderr.strip()})"
        )
    raw = proc.stdout.strip()
    hooks = Path(raw)
    if not hooks.is_absolute():
        hooks = (worktree / hooks).resolve()
    return hooks


def install_pre_commit_hook(*, repo_root: Path, run_id: str, worktree: Path) -> Path:
    """Install the compiled Codex git pre-commit hook into *worktree*'s git hooks dir.

    The hook is the git-lifecycle analog of the Claude PreToolUse hook: it rejects a commit that
    stages a write to a globally-blocked path (secrets/.env/.git/migrations/keys) at the commit
    boundary. The engine acceptance gate (``hw record-result``) remains the authoritative floor;
    this is defense-in-depth on the Codex side.
    """
    src = _run_dir(repo_root, run_id) / "hooks" / "codex-pre-commit.sh"
    if not src.exists():
        raise CodexError(
            f"no codex-pre-commit.sh for run {run_id!r} — run `hw compile-guardrails` first"
        )
    hooks_dir = _worktree_hooks_dir(worktree)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    dest = hooks_dir / "pre-commit"
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest


def _load_sandbox(repo_root: Path, run_id: str) -> dict:
    path = _run_dir(repo_root, run_id) / "hooks" / "codex-sandbox.json"
    if not path.exists():
        raise CodexError(
            f"no codex-sandbox.json for run {run_id!r} — run `hw compile-guardrails` first"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _packet_scope(sandbox: dict, packet_id: str) -> dict:
    for scope in sandbox.get("per_packet_scope", []):
        if scope.get("packet_id") == packet_id:
            return scope
    raise CodexError(
        f"packet {packet_id!r} is not in the compiled sandbox — re-run "
        f"`hw compile-guardrails` (optionally with --packet-id {packet_id})"
    )


def codex_agent_toml(*, packet: Packet, worktree: Path, scope: dict, result_path_abs: str) -> str:
    """Emit ONE Codex TOML agent definition for this packet's role.

    Codex defines a sub-agent per role via TOML. Each worker runs in its own git worktree
    (``--cd``), under a ``workspace-write`` sandbox with network disabled, and writes its result
    to ``result_path`` on disk (never stdout). The instruction is the engine's fresh-loaded step
    markdown — identical to what the Claude adapter hands its ``Task`` sub-agent.
    """
    blocked = ", ".join(json.dumps(g) for g in scope.get("blocked_globs", []))
    allowed = ", ".join(json.dumps(g) for g in scope.get("allowed_globs", []))
    # TOML multi-line basic string for the instruction; escape backslashes + triple-quotes.
    instr = packet.instruction.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return (
        f"# HyperWilfred Codex worker — packet {packet.packet_id} (run {packet.run_id})\n"
        f"[agents.{_toml_key(packet.packet_id)}]\n"
        f'role = "{packet.slice_id}"\n'
        f'title = "{_toml_escape(packet.title)}"\n'
        f'cd = "{_toml_escape(str(worktree))}"\n'
        'sandbox = "workspace-write"\n'
        "network_access = false\n"
        f'writable_roots = ["{_toml_escape(str(worktree))}"]\n'
        f"allowed_globs = [{allowed}]\n"
        f"blocked_globs = [{blocked}]\n"
        f"max_files = {packet.max_files}\n"
        f"max_changed_lines = {packet.max_changed_lines}\n"
        f"max_hunk_span = {packet.max_hunk_span}\n"
        f'result_path = "{_toml_escape(result_path_abs)}"\n'
        'read_result_from = "disk"  # Codex Issue #15451: never trust schema\'d stdout when MCP is live\n'  # noqa: E501
        f'instruction = """\n{instr}\n"""\n'
    )


def _toml_key(packet_id: str) -> str:
    """A TOML-safe table key derived from the packet id (bare keys allow [A-Za-z0-9_-])."""
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in packet_id)


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def prepare_codex_worker(
    *,
    repo_root: Path,
    run_id: str,
    packet_id: str,
    worktree: Path,
    install_hook: bool = True,
) -> dict:
    """Wire one Codex worker: install the git hook, resolve its sandbox scope, emit its manifest.

    Returns (and persists to ``.hw/<run_id>/codex/<packet_id>.json``) the manifest the thin Codex
    adapter consumes: the absolute worktree + result_path, the sandbox flags, the TOML agent def,
    and the disk-read rule. The engine never spawns the worker — it only translates the guardrail.
    """
    validate_identifier(packet_id, label="packet_id")
    packet_path = _run_dir(repo_root, run_id) / "packets" / f"{packet_id}.json"
    if not packet_path.exists():
        raise CodexError(f"no packet {packet_id!r} — run `hw fanout-plan` first")
    packet = Packet.model_validate_json(packet_path.read_text(encoding="utf-8"))

    sandbox = _load_sandbox(repo_root, run_id)
    scope = _packet_scope(sandbox, packet_id)

    worktree_abs = worktree.resolve()
    hook_path: str | None = None
    if install_hook:
        installed = install_pre_commit_hook(
            repo_root=repo_root, run_id=run_id, worktree=worktree_abs
        )
        hook_path = str(installed)

    result_path_abs = str((repo_root / packet.result_path).resolve())
    agent_toml = codex_agent_toml(
        packet=packet, worktree=worktree_abs, scope=scope, result_path_abs=result_path_abs
    )

    manifest = {
        "run_id": run_id,
        "packet_id": packet_id,
        "slice_id": packet.slice_id,
        "worktree": str(worktree_abs),
        "result_path": result_path_abs,
        "read_result_from": "disk",
        "issue_15451_note": "Codex --json/--output-schema are silently ignored when MCP is live; "
        "the engine reads this worker's result from result_path on disk, never from stdout.",
        "sandbox": {
            "mode": "workspace-write",
            "network_access": False,
            "writable_roots": [str(worktree_abs)],
            "allowed_globs": scope.get("allowed_globs", []),
            "blocked_globs": scope.get("blocked_globs", []),
        },
        "pre_commit_hook": hook_path,
        "codex_exec_flags": [
            "exec",
            "--cd",
            str(worktree_abs),
            "--sandbox",
            "workspace-write",
        ],
        "agent_toml": agent_toml,
        "record_result_cmd": (
            f"hw record-result --run-id {run_id} --packet-id {packet_id} "
            f"--worktree {worktree_abs}"
        ),
    }

    codex_dir = _run_dir(repo_root, run_id) / "codex"
    atomic_write_text(codex_dir / f"{packet_id}.json", json.dumps(manifest, indent=2) + "\n")
    RunStore(_run_dir(repo_root, run_id)).append_event(
        run_id=run_id,
        stage="codex-prepare",
        status=RunEventStatus.running,
        message=f"Wired Codex worker {packet_id} (worktree={worktree_abs}, "
        f"hook={'installed' if install_hook else 'skipped'})",
    )
    return manifest


def codex_batches(
    *,
    repo_root: Path,
    run_id: str,
    step_id: str = "implement",
    cap: int = CODEX_DEFAULT_CONCURRENCY,
) -> dict:
    """Split the fan-out into dependency-safe waves AND concurrency-capped sub-batches.

    The engine OWNS explicit multi-wave batching (spec §1): a dependency wave wider than the Codex
    concurrency cap (~6) is split into sequential sub-batches of at most ``cap`` packets, each run
    and joined before the next. Depth-1 nesting means a worker never itself fans out — the engine
    flattens any two-level plan into sequential top-level sub-batches here.
    """
    if cap < 1:
        raise CodexError("cap must be >= 1")
    plan_path = _run_dir(repo_root, run_id) / "plan.json"
    if not plan_path.exists():
        raise CodexError("no plan.json — the decompose/locate step must run before batching")
    plan_obj = Plan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    waves = plan_waves(plan_obj.slices)

    batches: list[dict] = []
    for wave_index, wave in enumerate(waves):
        packet_ids = [s.id for s in wave]
        sub_batches = [packet_ids[i : i + cap] for i in range(0, len(packet_ids), cap)]
        batches.append(
            {
                "wave_index": wave_index,
                "packet_ids": packet_ids,
                "sub_batches": sub_batches,
                "split_for_cap": len(sub_batches) > 1,
            }
        )

    total_packets = sum(len(b["packet_ids"]) for b in batches)
    total_sub_batches = sum(len(b["sub_batches"]) for b in batches)
    return {
        "run_id": run_id,
        "step_id": step_id,
        "concurrency_cap": cap,
        "nesting_depth": CODEX_NESTING_DEPTH,
        "waves": len(waves),
        "total_packets": total_packets,
        "total_sub_batches": total_sub_batches,
        "batches": batches,
    }


__all__ = [
    "CODEX_DEFAULT_CONCURRENCY",
    "CODEX_NESTING_DEPTH",
    "CodexError",
    "codex_agent_toml",
    "codex_batches",
    "install_pre_commit_hook",
    "prepare_codex_worker",
]
