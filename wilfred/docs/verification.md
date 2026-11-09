# Verification (public copy)

Observed when this copy was prepared for publication (2026-11-09), on macOS with Python 3.11
and uv, after private names were scrubbed:

- `uv run pytest -q`: 1330 passed, 3 skipped.
- Leak scan (`gitleaks dir`): no findings.

Supported platform: macOS only for generated execution, because generated workers are isolated
with `/usr/bin/sandbox-exec`. Other operating systems fail closed (no unsandboxed fallback); the
rest of the engine and its tests run on macOS.

Not verified in this copy: live runs with a real model provider (`--provider codex` makes real,
billed model calls) and any integration with a task tracker. Treat those as "works on the
maintainer's machine" until you have run them yourself. See [acceptance](acceptance.md).
