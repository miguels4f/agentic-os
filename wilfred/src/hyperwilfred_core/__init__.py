"""HyperWilfred portable engine (``hyperwilfred-core``).

One objective in, a built-or-improved product out — the orchestration brain that runs
identically on every runtime. This package is the L0 engine: it computes what happens next,
persists run state on disk, and hands packets to a thin per-runtime adapter. It NEVER calls
an LLM and NEVER spawns a sub-agent.

Phase 1 ships the BUILD spine + the engine-level guardrail floor on the Claude Code adapter.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
