---
name: agents-maintainer
description: Maintain and update AGENTS.md files with accurate repository context and executable workflows. Use when creating AGENTS.md from scratch, syncing it after structure/tooling/process changes, or wiring recurring automations that keep AGENTS.md current with minimal and verifiable diffs.
---

# Agents Maintainer Skill

Keep `AGENTS.md` as an accurate operational map for coding agents.
Prioritize small, evidence-based edits over rewrites.

## Required Inputs

- Repository root path
- Target AGENTS path (default: `<repo>/AGENTS.md`)
- Key sources (read the ones that exist):
  - `<repo>/README.md`, plus the README of each app or package the AGENTS.md covers
  - package manifests and task runners: `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`,
    `Makefile`, `justfile`
  - `<repo>/CHANGELOG.md`
  - CI and hook config (e.g. `.github/workflows/`, pre-commit / pre-push hooks)

For automation prompt wording, read `references/automation-prompt-template.md`.

## Workflow

1. Read current `AGENTS.md` and the key sources.
2. Detect concrete deltas only in:
   - repository structure and ownership areas
   - build/test/dev commands
   - validation and QA expectations
   - security and secrets handling rules
   - maintenance protocol and update cadence
3. Apply minimal edits:
   - Keep existing section order unless broken.
   - Update the "Last updated" date line (add one if the file has none).
   - Keep commands directly executable.
4. Validate:
   - Every referenced path exists.
   - Every command exists in the corresponding scripts/docs.
   - No speculative claims are added.
5. Return a short change report.

## Guardrails

- Do not edit unrelated files.
- Do not invent commands, folders, or status claims.
- Do not rewrite style if facts did not change.
- Prefer a short TODO note over guessing.

## Output Contract

Always return:
1. Decision: `updated` or `no_changes`
2. Files inspected
3. Sections changed (if any)
4. One-line rationale per change
