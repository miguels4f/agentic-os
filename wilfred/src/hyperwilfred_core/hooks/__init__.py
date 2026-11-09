"""Runtime-enforcement hook artifacts the guardrail COMPILER emits.

This subpackage holds the standalone PreToolUse hook the Claude Code adapter installs to block
out-of-scope writes *before they land* (the Phase-3 real-time write-time layer, defense-in-depth
ON TOP of the Phase-1 engine acceptance reject). The hook is a SEPARATE harness-invoked process:
the engine only GENERATES it as text (the compiler copies this module's source to disk) — the
engine itself never imports it, never calls an LLM, and never spawns a sub-agent.

``pretooluse.py`` is intentionally STDLIB-ONLY and self-contained so the on-disk copy under
``.hw/<run_id>/hooks/`` runs even when the package is not importable in the harness's environment.
"""

from __future__ import annotations

__all__ = ["pretooluse"]
