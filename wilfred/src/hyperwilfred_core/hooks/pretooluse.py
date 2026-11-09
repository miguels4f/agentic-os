#!/usr/bin/env python3
"""HyperWilfred Claude Code PreToolUse hook — real-time filesystem guardrail (fail-closed).

The Claude Code harness invokes this script as a SEPARATE process before a filesystem tool runs.
It reads the PreToolUse payload on stdin + the active run's guardrail registry from disk
(``--registry <path>``), and BLOCKS (deny) any guarded access that:

  * matches a globally-blocked glob (secrets / .env / .git / migrations / ssh keys …) — anywhere,
    regardless of worktree (the high-value exfiltration / irreversible-touch case), OR
  * lands inside a registered worker worktree but ESCAPES that packet's allow-list
    (blocked-glob wins over allow-glob), OR
  * matches no worktree while ``deny_unmatched_writes`` is enabled (the REVIEW profile).

REVIEW additionally limits Read / Grep / Glob to the packet's declared changed paths. This stops
untrusted reviewed content from instructing a critic to read secrets and copy them into its one
permitted report.

This catches a bad write EARLIER than the Phase-1 engine acceptance reject (which fires at
``hw record-result``, after the write is already on disk). It does NOT replace that reject —
the engine acceptance gate stays the authoritative floor; this hook is defense-in-depth.

FAIL-CLOSED is the rule. If the registry is missing / unreadable / malformed, or a write tool's
target path cannot be resolved, the hook DENIES rather than allows — a broken guardrail must
never silently pass a write. (The engine writes its own state via the ``hw`` CLI over the Bash
tool, which this hook does not match, so a fail-closed deny never deadlocks the engine.)

STDLIB ONLY + self-contained: the guardrail compiler copies this exact file to
``.hw/<run_id>/hooks/pretooluse.py`` so it runs even when ``hyperwilfred_core`` is not importable
in the harness environment. The glob matcher mirrors ``guardrails.matches`` /
``guardrails.allowlist_escapes`` byte-for-byte; a drift-guard test pins the two together.
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import os
import re
import sys

# REVIEW also gates filesystem reads so a prompt-injected critic cannot read secrets and copy
# them into its permitted report. Bash remains unavailable at the runtime capability layer.
WRITE_TOOLS: frozenset[str] = frozenset(
    {"Write", "Edit", "MultiEdit", "NotebookEdit"}
)
READ_TOOLS: frozenset[str] = frozenset({"Read", "Grep", "Glob"})
GUARDED_TOOLS: frozenset[str] = WRITE_TOOLS | READ_TOOLS


# --------------------------------------------------------------------------- glob matching
# Kept byte-identical to ``guardrails.matches`` / ``allowlist_escapes`` (a unit test asserts the
# two never drift). Duplicated here ON PURPOSE so this file stays stdlib-only and standalone.


def matches(path: str, glob: str) -> bool:
    """Path/glob match with gitignore-style ``**/`` semantics on top of fnmatch.

    An exact string match always wins, so a literal path holding glob metacharacters (Next.js
    ``[projectId]`` folders) matches itself. A pattern fnmatch cannot compile (``[t-i]`` raises
    on Python 3.9) matches nothing beyond that exact string instead of raising.
    """
    if path == glob:
        return True
    try:
        if fnmatch.fnmatch(path, glob):
            return True
        if glob.startswith("**/") and fnmatch.fnmatch(path, glob[3:]):
            return True
    except re.error:
        return False
    return False


def malformed_globs(globs: list[str]) -> list[str]:
    """The patterns fnmatch cannot compile on this interpreter."""
    bad: list[str] = []
    for g in globs:
        try:
            re.compile(fnmatch.translate(g))
        except re.error:
            bad.append(g)
    return bad


def allowlist_escapes(
    paths: list[str], allow_globs: list[str], blocked_globs: list[str]
) -> list[str]:
    """Return the paths that escape scope (blocked-by-default wins over allow).

    A blocked glob that cannot be evaluated blocks every path (fail-closed); an allow glob that
    cannot be evaluated simply admits nothing.
    """
    escapes: list[str] = []
    blocked_unreadable = bool(malformed_globs(blocked_globs))
    for p in paths:
        if blocked_unreadable or any(matches(p, g) for g in blocked_globs):
            escapes.append(p)
            continue
        if not any(matches(p, g) for g in allow_globs):
            escapes.append(p)
    return escapes


# --------------------------------------------------------------------------- decision


class _Deny(Exception):
    """Internal signal: deny the tool call with this reason (fail-closed default)."""


def _extract_targets(tool: str, tool_input: dict) -> list[str]:
    """Every filesystem path the guarded tool would read, list, or modify."""
    if tool == "Glob":
        pattern = tool_input.get("pattern")
        base = tool_input.get("path")
        if not isinstance(pattern, str) or not pattern.strip():
            return []
        candidate = os.path.join(base, pattern) if isinstance(base, str) and base else pattern
        return sorted(glob.glob(os.path.expanduser(candidate), recursive=True))

    targets: list[str] = []
    for key in ("file_path", "notebook_path", "path"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            targets.append(val)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for e in edits:
            if isinstance(e, dict):
                fp = e.get("file_path")
                if isinstance(fp, str) and fp.strip():
                    targets.append(fp)
    # de-dupe, preserve order
    seen: list[str] = []
    for t in targets:
        if t not in seen:
            seen.append(t)
    return seen


def _norm(path: str) -> str:
    """Canonical absolute path (symlinks resolved) for containment + relpath checks.

    MUST use ``realpath``, not ``abspath``: the compiler records each worktree via
    ``Path.resolve()`` (symlinks resolved), so on platforms where the temp/cwd prefix is a
    symlink (macOS ``/var`` → ``/private/var``) an ``abspath``-only normalization would fail
    worktree containment and silently DEFER an out-of-scope write. ``realpath`` makes the hook's
    view of a path identical to the registry's.
    """
    return os.path.realpath(os.path.expanduser(path))


def _match_entry(target_abs: str, entries: list[dict]) -> dict | None:
    """The registry entry whose worktree CONTAINS *target_abs* (longest worktree wins)."""
    best: dict | None = None
    best_len = -1
    for entry in entries:
        wt = entry.get("worktree")
        if not isinstance(wt, str) or not wt:
            continue
        wt_abs = _norm(wt)
        if target_abs == wt_abs or target_abs.startswith(wt_abs + os.sep):
            if len(wt_abs) > best_len:
                best = entry
                best_len = len(wt_abs)
    return best


def decide(payload: dict, registry: dict) -> dict | None:
    """Decide allow/deny for one PreToolUse payload against *registry*.

    Returns ``None`` to ALLOW (defer to the harness's normal permission flow — the hook does NOT
    force-approve in-scope writes), or a Claude Code ``hookSpecificOutput`` deny dict to BLOCK.
    Raises :class:`_Deny` internally for fail-closed paths; the caller maps that to a deny dict.
    """
    tool = payload.get("tool_name", "")
    if tool not in GUARDED_TOOLS:
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        raise _Deny("write tool with no tool_input object — fail-closed")
    if not isinstance(registry, dict):
        raise _Deny("guardrail registry is malformed — fail-closed")
    entries = registry.get("entries")
    if not isinstance(entries, list):
        raise _Deny("guardrail registry has no entries list — fail-closed")
    global_blocked = registry.get("global_blocked_globs") or []
    if not isinstance(global_blocked, list):
        raise _Deny("guardrail registry global_blocked_globs is malformed — fail-closed")
    deny_unmatched = registry.get("deny_unmatched_writes", False)
    if not isinstance(deny_unmatched, bool):
        raise _Deny("guardrail registry deny_unmatched_writes is malformed — fail-closed")
    deny_unmatched_reads = registry.get("deny_unmatched_reads", False)
    if not isinstance(deny_unmatched_reads, bool):
        raise _Deny("guardrail registry deny_unmatched_reads is malformed — fail-closed")
    bad_global = malformed_globs(list(global_blocked))
    if bad_global:
        raise _Deny(
            f"guardrail registry has malformed blocked glob {bad_global[0]!r} — fail-closed"
        )

    targets = _extract_targets(tool, tool_input)
    if not targets:
        if tool in READ_TOOLS and not deny_unmatched_reads:
            return None
        raise _Deny(f"{tool} with no resolvable target path — fail-closed")
    if tool in READ_TOOLS and not deny_unmatched_reads:
        return None

    for raw in targets:
        target_abs = _norm(raw)

        # 1. Globally-blocked path (secrets/.env/.git/migrations/ssh) — blocked ANYWHERE, so an
        #    absolute write to ~/.ssh/id_rsa is caught even if it is outside every worktree. This
        #    is the highest-value real-time protection. Matched against every path-suffix so a
        #    deep absolute prefix cannot hide a ``**/secrets/**`` or ``**/.env`` target.
        if _path_blocked(target_abs, list(global_blocked)):
            raise _Deny(
                f"write to '{raw}' matches a globally-blocked path "
                f"(secret/.env/.git/migration/key) — denied before it lands"
            )

        # 2. Worker-worktree allow-list. A write inside a registered worktree must stay within
        #    that packet's allowed_globs (blocked wins). REVIEW enables deny_unmatched_writes, so
        #    a symlink rebind or any other escape outside the repository fails closed.
        entry = _match_entry(target_abs, entries)
        if entry is None:
            if deny_unmatched or (tool in READ_TOOLS and deny_unmatched_reads):
                raise _Deny(
                    f"{tool} target '{raw}' matches no registered worktree — fail-closed"
                )
            continue
        wt_abs = _norm(entry["worktree"])
        rel = os.path.relpath(target_abs, wt_abs)
        if rel.startswith(".."):
            raise _Deny(f"write to '{raw}' resolves outside its packet worktree — denied")
        allowed_key = "allowed_read_globs" if tool in READ_TOOLS else "allowed_globs"
        allowed = entry.get(allowed_key) or []
        blocked = entry.get("blocked_globs") or []
        if not isinstance(allowed, list) or not isinstance(blocked, list):
            raise _Deny("registry entry has malformed globs — fail-closed")
        bad_blocked = malformed_globs(list(blocked))
        if bad_blocked:
            raise _Deny(
                f"registry entry has malformed blocked glob {bad_blocked[0]!r} — fail-closed"
            )
        if allowlist_escapes([rel], list(allowed), list(blocked)):
            raise _Deny(
                f"{tool} target '{rel}' escapes packet '{entry.get('packet_id', '?')}' scope "
                f"(blocked-glob or not in allow-list) — denied before it lands"
            )

    return None  # every target is in-scope


def _path_blocked(target_abs: str, blocked: list[str]) -> bool:
    """True iff the absolute path (or any of its path-suffixes) matches a blocked glob.

    A blocked glob like ``**/secrets/**`` or ``**/.env`` must catch the path no matter how deep
    the absolute prefix is — so we test progressively-shorter path tails.
    """
    parts = target_abs.split(os.sep)
    tails = ["/".join(parts[i:]) for i in range(len(parts))]
    for tail in tails:
        if not tail:
            continue
        if any(matches(tail, g) for g in blocked):
            return True
    return False


# --------------------------------------------------------------------------- I/O wrapper


def _deny_output(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _load_registry(path: str | None) -> dict:
    if not path:
        raise _Deny("no guardrail registry path provided to the hook — fail-closed")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise _Deny(f"guardrail registry unreadable/malformed ({exc}) — fail-closed") from exc
    return data


def run(argv: list[str] | None = None, stdin_text: str | None = None) -> tuple[int, str, str]:
    """Pure entry point: return ``(exit_code, stdout, stderr)`` for a hook invocation.

    Exit 0 + empty stdout = allow (defer to normal flow). A deny prints the deny JSON to stdout
    AND the reason to stderr AND exits 2 — so the call is blocked on BOTH the structured-JSON
    protocol and the exit-code protocol, whichever the running Claude Code build honours.
    """
    parser = argparse.ArgumentParser(prog="hyperwilfred-pretooluse-hook", add_help=False)
    parser.add_argument("--registry", default=os.environ.get("HW_GUARDRAIL_REGISTRY"))
    args, _unknown = parser.parse_known_args(argv if argv is not None else sys.argv[1:])

    raw = stdin_text if stdin_text is not None else sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            raise _Deny("PreToolUse payload is not a JSON object — fail-closed")
        registry = _load_registry(args.registry)
        decision = decide(payload, registry)
    except _Deny as deny:
        out = json.dumps(_deny_output(str(deny)))
        return 2, out, str(deny)
    except Exception as exc:  # noqa: BLE001 — ANY error is fail-closed by contract
        reason = f"guardrail hook error ({type(exc).__name__}: {exc}) — fail-closed deny"
        return 2, json.dumps(_deny_output(reason)), reason

    if decision is None:
        return 0, "", ""
    return 2, json.dumps(decision), decision["hookSpecificOutput"]["permissionDecisionReason"]


def main(argv: list[str] | None = None) -> int:
    code, out, err = run(argv)
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
