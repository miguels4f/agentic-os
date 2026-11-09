"""The guardrail COMPILER — turn a run's machine-readable scope into runtime-enforcement files.

This is the engine-side, **LLM-free, spawn-free** capability that reads a run's ``guardrails.json``
plus the active fan-out / patch packet's allow / blocked globs + budgets and EMITS, to disk under
``.hw/<run_id>/hooks/``, the artifacts a runtime adapter installs to enforce scope in real time:

  (a) a **Claude Code PreToolUse hook**: a copy of the standalone ``pretooluse.py`` script + a
      worktree→policy ``registry.json`` it reads + a ``settings.json`` fragment that registers it
      for guarded filesystem reads and writes; and
  (b) a **Codex** sandbox + git-pre-commit-hook config, emitted as PORTABLE ON-DISK ARTIFACTS
      ONLY — Phase 3 generates them; it does NOT wire or drive Codex (that is the Phase-4 adapter).

The engine GENERATES text/config files here; it still NEVER calls an LLM and NEVER spawns a
sub-agent. Generating a hook script as text is not the same as running one — the hook is a
separate process the *harness* invokes later.
"""

from __future__ import annotations

import fcntl
import json
import os
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from glob import escape as glob_escape
from pathlib import Path, PurePosixPath
from uuid import uuid4

from hyperwilfred_core.path_safety import validate_identifier
from hyperwilfred_core.run_store import RunEventStatus, RunStore, atomic_write_text
from hyperwilfred_core.schemas import CritiquePacket, GuardrailConfig, Packet, PatchPacket

# The standalone hook source the compiler copies verbatim to each run (read as text, never
# imported — so the on-disk copy is self-contained + stdlib-only).
_HOOK_SOURCE = Path(__file__).resolve().parent / "hooks" / "pretooluse.py"

_GUARDED_TOOL_MATCHER = "Read|Grep|Glob|Write|Edit|MultiEdit|NotebookEdit"
_CRITIC_ALLOWED_TOOLS = ("Read", "Grep", "Glob", "Write")


class CompilerError(RuntimeError):
    """Raised on a compiler precondition violation (surfaces as CLI exit 2)."""


def _run_dir(repo_root: Path, run_id: str) -> Path:
    # A run id is a path component under ``.hw``; refuse traversal before any file I/O.
    return repo_root / ".hw" / validate_identifier(run_id, label="run_id")


def _hooks_dir(repo_root: Path, run_id: str) -> Path:
    return _run_dir(repo_root, run_id) / "hooks"


# --------------------------------------------------------------------------- policy sourcing


def _entry_from_packet(
    repo_root: Path, run_id: str, packet: Packet | PatchPacket | CritiquePacket
) -> dict:
    """One registry entry: the worktree (absolute) + that packet's allow/blocked scope.

    Worktrees are stored ABSOLUTE because the PreToolUse payload only carries an absolute
    ``file_path`` (no cwd) — the hook resolves which packet a write belongs to by which worktree
    contains the target path.
    """
    if isinstance(packet, CritiquePacket):
        repo_abs = repo_root.resolve()
        if packet.run_id != run_id:
            raise CompilerError(
                f"critique packet {packet.packet_id!r} belongs to run {packet.run_id!r}, "
                f"not {run_id!r}"
            )
        result_path = PurePosixPath(packet.result_path)
        run_path = PurePosixPath(".hw") / run_id
        if (
            result_path.is_absolute()
            or ".." in result_path.parts
            or result_path == run_path
            or not result_path.is_relative_to(run_path)
        ):
            raise CompilerError(
                f"critique packet {packet.packet_id!r} result_path must stay inside "
                f".hw/{run_id}/ lexically"
            )
        candidate = repo_abs
        for component in result_path.parts:
            candidate /= component
            if candidate.is_symlink():
                raise CompilerError(
                    f"critique packet {packet.packet_id!r} result_path crosses existing symlink "
                    f"{candidate}"
                )
        result_lexical = result_path.as_posix()
        return {
            "packet_id": packet.packet_id,
            "worktree": str(repo_abs),
            "allowed_globs": [result_lexical],
            # changed_paths are LITERAL files; escape so a Next.js ``[projectId]`` folder is
            # matched as itself, not as a one-character class (or an uncompilable range).
            "allowed_read_globs": [glob_escape(p) for p in packet.changed_paths],
            "blocked_globs": [],
            "result_path": result_lexical,
            "runtime_allowed_tools": list(_CRITIC_ALLOWED_TOOLS),
        }

    if packet.run_id != run_id:
        raise CompilerError(
            f"packet {packet.packet_id!r} belongs to run {packet.run_id!r}, not {run_id!r}"
        )
    worktree_abs = str((repo_root / packet.worktree_hint).resolve())
    return {
        "packet_id": packet.packet_id,
        "worktree": worktree_abs,
        "allowed_globs": list(packet.allowed_globs),
        "blocked_globs": list(packet.blocked_globs),
        "max_changed_lines": packet.max_changed_lines,
        "max_hunk_span": packet.max_hunk_span,
        "max_files": packet.max_files,
    }


def _load_packet(
    repo_root: Path, run_id: str, packet_id: str
) -> Packet | PatchPacket | CritiquePacket:
    """Load one fan-out, quality patch, or read-only critique packet by id."""
    validate_identifier(packet_id, label="packet_id")
    fanout = _run_dir(repo_root, run_id) / "packets" / f"{packet_id}.json"
    if fanout.exists():
        return Packet.model_validate_json(fanout.read_text(encoding="utf-8"))
    for pp in sorted((_run_dir(repo_root, run_id)).glob("quality/iter-*/patch-packet.json")):
        patch_candidate = PatchPacket.model_validate_json(pp.read_text(encoding="utf-8"))
        if patch_candidate.packet_id == packet_id:
            return patch_candidate
    for cp in sorted(
        (_run_dir(repo_root, run_id)).glob("quality/iter-*/critic-packets/*.json")
    ):
        critique_candidate = CritiquePacket.model_validate_json(cp.read_text(encoding="utf-8"))
        if critique_candidate.packet_id == packet_id:
            return critique_candidate
    raise CompilerError(f"no packet {packet_id!r} for run {run_id!r} — run fanout/patch-plan first")


def _all_fanout_packets(repo_root: Path, run_id: str) -> list[Packet]:
    packets_dir = _run_dir(repo_root, run_id) / "packets"
    if not packets_dir.exists():
        return []
    out: list[Packet] = []
    for f in sorted(packets_dir.glob("*.json")):
        out.append(Packet.model_validate_json(f.read_text(encoding="utf-8")))
    return out


# --------------------------------------------------------------------------- compile


def _compile_guardrails_unlocked(
    *,
    repo_root: Path,
    run_id: str,
    packet_id: str | None = None,
) -> dict:
    """Emit the runtime-enforcement artifacts for *run_id* and return a manifest.

    With ``packet_id`` the registry scopes to that single packet (the adapter activating the hook
    for one worker); without it the registry covers every current fan-out packet (the whole
    implement wave). Either way the engine only WRITES files — it never runs the hook.
    """
    guardrails_path = _run_dir(repo_root, run_id) / "guardrails.json"
    if not guardrails_path.exists():
        raise CompilerError(f"no guardrails.json for run {run_id!r} — run `hw plan` first")
    guardrail = GuardrailConfig.model_validate_json(guardrails_path.read_text(encoding="utf-8"))

    if packet_id is not None:
        packets: list[Packet | PatchPacket | CritiquePacket] = [
            _load_packet(repo_root, run_id, packet_id)
        ]
    else:
        packets = list(_all_fanout_packets(repo_root, run_id))
    if not packets:
        raise CompilerError(
            f"no packets to compile for run {run_id!r} — run `hw fanout-plan`, "
            "`hw patch-plan`, or `hw critique-plan` first"
        )

    entries = [_entry_from_packet(repo_root, run_id, p) for p in packets]
    registry = {
        "run_id": run_id,
        "repo_root": str(repo_root.resolve()),
        "global_blocked_globs": list(guardrail.global_blocked_globs),
        "deny_unmatched_writes": any(isinstance(p, CritiquePacket) for p in packets),
        "deny_unmatched_reads": any(isinstance(p, CritiquePacket) for p in packets),
        "entries": entries,
    }

    hooks_dir = _hooks_dir(repo_root, run_id)
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # (a) Claude Code PreToolUse hook: the standalone script + the registry + the settings fragment.
    hook_script = hooks_dir / "pretooluse.py"
    atomic_write_text(hook_script, _HOOK_SOURCE.read_text(encoding="utf-8"))
    registry_path = hooks_dir / "registry.json"
    atomic_write_text(registry_path, json.dumps(registry, indent=2) + "\n")
    settings_path = hooks_dir / "settings.json"
    atomic_write_text(
        settings_path, json.dumps(_claude_settings(hook_script, registry_path), indent=2) + "\n"
    )

    # (b) Codex sandbox + git-pre-commit hook — PASSIVE artifacts (Phase 4 wires them; not driven).
    codex_sandbox_path = hooks_dir / "codex-sandbox.json"
    atomic_write_text(
        codex_sandbox_path,
        json.dumps(_codex_sandbox(registry, guardrail), indent=2) + "\n",
    )
    codex_hook_path = hooks_dir / "codex-pre-commit.sh"
    atomic_write_text(codex_hook_path, _codex_pre_commit(guardrail))

    RunStore(_run_dir(repo_root, run_id)).append_event(
        run_id=run_id, stage="compile-guardrails", status=RunEventStatus.running,
        message=f"Compiled guardrail artifacts: {len(entries)} packet scope(s) "
        f"(PreToolUse hook + registry + settings + Codex sandbox/git-hook)",
        artifacts=[
            str(hook_script.relative_to(repo_root)),
            str(registry_path.relative_to(repo_root)),
            str(settings_path.relative_to(repo_root)),
            str(codex_sandbox_path.relative_to(repo_root)),
            str(codex_hook_path.relative_to(repo_root)),
        ],
    )

    return {
        "run_id": run_id,
        "scoped_to": packet_id or "all-fanout-packets",
        "packets": [e["packet_id"] for e in entries],
        "artifacts": {
            "hook_script": str(hook_script.relative_to(repo_root)),
            "registry": str(registry_path.relative_to(repo_root)),
            "claude_settings": str(settings_path.relative_to(repo_root)),
            "codex_sandbox": str(codex_sandbox_path.relative_to(repo_root)),
            "codex_pre_commit": str(codex_hook_path.relative_to(repo_root)),
        },
        "install": _install_help(
            repo_root, run_id, packet_id, settings_path, hook_script, registry_path
        ),
    }


def compile_guardrails(
    *,
    repo_root: Path,
    run_id: str,
    packet_id: str | None = None,
) -> dict:
    """Compile one policy without permitting replacement of an installed policy."""
    with _settings_transaction(repo_root):
        owner, _ = _repository_lifecycle_paths(repo_root)
        if owner.exists():
            status = _read_record(owner, "guardrail settings owner")
            raise CompilerError(
                f"cannot compile guardrails while run {status.get('run_id')!r} "
                "owns the active policy"
            )
        return _compile_guardrails_unlocked(
            repo_root=repo_root, run_id=run_id, packet_id=packet_id
        )


# --------------------------------------------------------------------------- emitters


def _claude_settings(hook_script: Path, registry_path: Path) -> dict:
    """A Claude Code ``settings.json`` fragment registering the PreToolUse hook (fail-closed)."""
    command = (
        f"python3 {shlex.quote(str(hook_script.resolve()))} "
        f"--registry {shlex.quote(str(registry_path.resolve()))}"
    )
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": _GUARDED_TOOL_MATCHER,
                    "hooks": [
                        {"type": "command", "command": command, "timeout": 10},
                    ],
                }
            ]
        }
    }


def _codex_sandbox(registry: dict, guardrail: GuardrailConfig) -> dict:
    """A PORTABLE description of the Codex sandbox the Phase-4 adapter would apply (not driven)."""
    return {
        "_note": "Passive artifact emitted by the HyperWilfred guardrail compiler. Phase 3 only "
        "GENERATES this; the Phase-4 Codex adapter wires + drives it. Not executed here.",
        "sandbox_mode": "workspace-write",
        "network_access": False,
        "writable_roots": [e["worktree"] for e in registry["entries"]],
        "blocked_globs": list(guardrail.global_blocked_globs),
        "per_packet_scope": [
            {
                "packet_id": e["packet_id"],
                "worktree": e["worktree"],
                "allowed_globs": e["allowed_globs"],
                "blocked_globs": e["blocked_globs"],
            }
            for e in registry["entries"]
        ],
    }


# Exact file names the commit hook lets through although a blocked glob matches them. Owner
# decision 2026-10-05: `.env.example` is a committed template with placeholder values; blocking
# it stopped merges that only carried another project's reviewed template. Commit-time only:
# agents still may not write it (DEFAULT_BLOCKED_GLOBS and the write-time hooks are unchanged).
COMMIT_ALLOWED_BASENAMES: tuple[str, ...] = (".env.example",)


def _codex_pre_commit(guardrail: GuardrailConfig) -> str:
    """A git pre-commit hook (text) that rejects a staged write to a globally-blocked path.

    Emitted as a passive artifact only — Phase 4 installs it on the Codex side. It is the
    git-lifecycle analog of the Claude PreToolUse hook: blocked-by-default at the commit boundary.
    """
    patterns = " ".join(f"'{g}'" for g in guardrail.global_blocked_globs)
    allowed = " ".join(f"'{name}'" for name in COMMIT_ALLOWED_BASENAMES)
    return (
        "#!/usr/bin/env bash\n"
        "# HyperWilfred guardrail — Codex git pre-commit hook (PASSIVE artifact; Phase 4 installs).\n"  # noqa: E501
        "# Rejects a commit that stages a write to a globally-blocked path (secrets/.env/.git/\n"
        "# migrations/keys). Defense-in-depth at the git boundary; the engine acceptance gate\n"
        "# (`hw record-result`) remains the authoritative floor.\n"
        "set -euo pipefail\n"
        f"BLOCKED=({patterns})\n"
        "# Exact template names allowed at commit time only (e.g. .env.example).\n"
        f"ALLOWED_BASENAMES=({allowed})\n"
        "while IFS= read -r -d '' path; do\n"
        '  base=$(basename "$path")\n'
        '  for ok in "${ALLOWED_BASENAMES[@]}"; do\n'
        '    [[ "$base" == "$ok" ]] && continue 2\n'
        "  done\n"
        '  for pat in "${BLOCKED[@]}"; do\n'
        "    # Strip a LITERAL leading '**/' only (quote it so '**' is not glob-expanded — an\n"
        "    # unquoted ${pat#**/} greedily eats '.git/' from '.git/**', leaving '**' which\n"
        '    # matches every path and blocks all commits).\n'
        "    stripped=${pat#'**'/}\n"
        '    # shellcheck disable=SC2053\n'
        '    if [[ "$path" == $pat || "$path" == $stripped || "$base" == $stripped ]]; then\n'
        '      echo "guardrail: refusing to commit blocked path: $path (matched $pat)" >&2\n'
        "      exit 1\n"
        "    fi\n"
        "  done\n"
        "done < <(git diff --cached --name-only -z)\n"
        "exit 0\n"
    )


def _install_help(
    repo_root: Path,
    run_id: str,
    packet_id: str | None,
    settings_path: Path,
    hook_script: Path,
    registry_path: Path,
) -> dict:
    """Plain instructions the adapter follows to activate the hook for the active worker(s)."""
    packet_arg = f" --packet-id {shlex.quote(packet_id)}" if packet_id else ""
    return {
        "claude": f"Run `hw guardrails-install --run-id {run_id}{packet_arg}` before the worker, "
        "then always "
        f"run `hw guardrails-remove --run-id {run_id}` in a finally/cleanup step.",
        "settings_fragment": str(settings_path.relative_to(repo_root)),
        "hook_command": (
            f"python3 {shlex.quote(str(hook_script.resolve()))} "
            f"--registry {shlex.quote(str(registry_path.resolve()))}"
        ),
        "scope_per_worker": "re-run `hw compile-guardrails --packet-id <id>` to scope the registry "
        "to one worker, or compile once for the whole wave (the hook resolves the right packet by "
        "which worktree contains the write target).",
    }


def _lifecycle_paths(repo_root: Path, run_id: str) -> tuple[Path, Path, Path]:
    hooks = _hooks_dir(repo_root, run_id)
    return (
        repo_root / ".claude" / "settings.local.json",
        hooks / "settings.local.backup",
        hooks / "settings.local.lifecycle.json",
    )


def _repository_lifecycle_paths(repo_root: Path) -> tuple[Path, Path]:
    """Repository-global owner and lock for the one shared Claude settings file."""
    return (
        repo_root / ".hw" / "settings.local.owner.json",
        repo_root / ".hw" / "settings.local.lock",
    )


@contextmanager
def _settings_transaction(repo_root: Path) -> Iterator[None]:
    """Serialize settings lifecycle changes across independent ``hw`` processes."""
    _, lock = _repository_lifecycle_paths(repo_root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_record(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompilerError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise CompilerError(f"{label} must be a JSON object")
    return value


def _write_record(path: Path, value: dict) -> None:
    atomic_write_text(path, json.dumps(value, indent=2) + "\n")


def _atomic_write_bytes(path: Path, value: bytes, mode: int) -> None:
    """Atomically replace a byte file and set its final permission mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _merged_settings(original: bytes | None, fragment_path: Path) -> bytes:
    try:
        current = json.loads(original.decode("utf-8")) if original is not None else {}
        fragment = json.loads(fragment_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompilerError("Claude settings are not valid UTF-8 JSON") from exc
    if not isinstance(current, dict) or not isinstance(current.get("hooks", {}), dict):
        raise CompilerError("Claude settings must be an object with an object `hooks`")
    try:
        groups = fragment["hooks"]["PreToolUse"]
    except (KeyError, TypeError) as exc:
        raise CompilerError("compiled Claude settings have no PreToolUse hook") from exc
    hooks = current.setdefault("hooks", {})
    pretool = hooks.setdefault("PreToolUse", [])
    if not isinstance(pretool, list) or not isinstance(groups, list):
        raise CompilerError("Claude settings `hooks.PreToolUse` must be a list")
    pretool.extend(group for group in groups if group not in pretool)
    return (json.dumps(current, indent=2) + "\n").encode()


def _verify_owner(owner: Path, run_id: str) -> dict:
    status = _read_record(owner, "guardrail settings owner")
    if status.get("run_id") != run_id:
        raise CompilerError(
            f"Claude settings are owned by active run {status.get('run_id')!r}; "
            f"run {run_id!r} cannot change them"
        )
    return status


def install_guardrails(
    *, repo_root: Path, run_id: str, packet_id: str | None = None
) -> dict:
    """Transactionally install one run's hook while preserving prior settings exactly."""
    settings, backup, lifecycle = _lifecycle_paths(repo_root, run_id)
    owner, _ = _repository_lifecycle_paths(repo_root)
    fragment_path = _hooks_dir(repo_root, run_id) / "settings.json"
    registry_path = _hooks_dir(repo_root, run_id) / "registry.json"
    if not fragment_path.exists():
        raise CompilerError(f"no compiled settings for run {run_id!r} — compile guardrails first")

    with _settings_transaction(repo_root):
        registry = _read_record(registry_path, "compiled guardrail registry")
        entries = registry.get("entries")
        if not isinstance(entries, list):
            raise CompilerError("compiled guardrail registry has no entries list")
        compiled_packet_ids = [
            entry.get("packet_id") for entry in entries if isinstance(entry, dict)
        ]
        if packet_id is not None and compiled_packet_ids != [packet_id]:
            raise CompilerError(
                f"compiled policy is scoped to {compiled_packet_ids!r}, not packet "
                f"{packet_id!r}; recompile that packet before install"
            )
        if owner.exists():
            _verify_owner(owner, run_id)
        status = (
            _read_record(lifecycle, "guardrail settings lifecycle")
            if lifecycle.exists()
            else {}
        )
        phase = status.get("phase")
        if phase == "installed":
            if not owner.exists():
                raise CompilerError("installed guardrail lifecycle has no repository owner")
            return {"run_id": run_id, "installed": True, "changed": False}
        if phase not in (None, "prepared"):
            raise CompilerError(f"unknown guardrail settings lifecycle phase {phase!r}")

        if phase == "prepared":
            had_original = status.get("had_original") is True
            if had_original and not backup.exists():
                raise CompilerError("guardrail settings backup is missing — refusing install")
            original = backup.read_bytes() if had_original else None
            original_mode = int(status.get("mode", 0o600))
        else:
            original = settings.read_bytes() if settings.exists() else None
            original_mode = (settings.stat().st_mode & 0o777) if settings.exists() else 0o600

        merged = _merged_settings(original, fragment_path)
        if not owner.exists():
            _write_record(
                owner,
                {"run_id": run_id, "packet_ids": compiled_packet_ids},
            )
        if phase is None:
            backup.parent.mkdir(parents=True, exist_ok=True)
            if original is not None:
                _atomic_write_bytes(backup, original, 0o600)
            else:
                backup.unlink(missing_ok=True)
            status = {
                "phase": "prepared",
                "had_original": original is not None,
                "mode": original_mode,
            }
            _write_record(lifecycle, status)

        _atomic_write_bytes(settings, merged, original_mode)
        status["phase"] = "installed"
        _write_record(lifecycle, status)
        return {"run_id": run_id, "installed": True, "changed": True}


def remove_guardrails(*, repo_root: Path, run_id: str) -> dict:
    """Restore either prepared/installed transactions, then relinquish repository ownership."""
    settings, backup, lifecycle = _lifecycle_paths(repo_root, run_id)
    owner, _ = _repository_lifecycle_paths(repo_root)
    with _settings_transaction(repo_root):
        if not owner.exists():
            if lifecycle.exists():
                raise CompilerError("guardrail settings lifecycle has no repository owner")
            return {"run_id": run_id, "installed": False, "changed": False}
        _verify_owner(owner, run_id)

        if not lifecycle.exists():
            backup.unlink(missing_ok=True)
            owner.unlink()
            return {"run_id": run_id, "installed": False, "changed": True}

        status = _read_record(lifecycle, "guardrail settings lifecycle")
        phase = status.get("phase")
        if phase not in ("prepared", "installed"):
            raise CompilerError(f"unknown guardrail settings lifecycle phase {phase!r}")
        if status.get("had_original") is True:
            if not backup.exists():
                raise CompilerError("guardrail settings backup is missing — refusing cleanup")
            _atomic_write_bytes(
                settings,
                backup.read_bytes(),
                int(status.get("mode", 0o600)),
            )
        else:
            settings.unlink(missing_ok=True)

        # Remove the journal first: if cleanup is interrupted, a retry knows restoration finished.
        lifecycle.unlink()
        backup.unlink(missing_ok=True)
        owner.unlink()
        return {"run_id": run_id, "installed": False, "changed": True}


__all__ = [
    "CompilerError",
    "compile_guardrails",
    "install_guardrails",
    "remove_guardrails",
]
