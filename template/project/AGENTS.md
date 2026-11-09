# AGENTS.md — <project name>

Last updated: <YYYY-MM-DD>
Read by Codex and Cursor directly, and by Claude Code through `CLAUDE.md`. Keep it short and
true: agents follow it literally. Personal preferences live in `~/.agents/AGENTS.md`.

## What this is

<Two or three sentences: what the project does, for whom, and its current stage
(prototype / pilot / production).>

## Commands

```bash
<install>        # e.g. uv sync --group dev   |  npm ci
<run>            # e.g. uv run app            |  npm run dev
<test>           # e.g. uv run pytest -q      |  npm test
<lint>           # e.g. uv run ruff check .   |  npm run lint
<type check>     # e.g. uv run mypy src       |  npm run typecheck
```

Before pushing, run `local-ci` (or the `local-ci` skill).

## Map

| Path | What lives there |
| --- | --- |
| `src/` | <application code> |
| `tests/` | <tests> |
| `docs/` | <decisions, specs> |
| `.agents/skills/` | skills specific to this project |

## Rules for agents

- Ask before: adding a dependency, changing the database schema, deleting data, spending money
  (paid APIs, cloud resources), anything that sends messages or publishes.
- Never commit keys, tokens or `.env` files. If one leaks, follow the `secret-leak-response` skill.
- Do not weaken tests, lint rules or CI to make something pass.
- <Project-specific rules: wording that must or must not appear, data that must stay local,
  areas nobody may touch without review.>

## Finishing work

- Tests, lint and type check green locally.
- Open a pull request; do not merge it yourself unless this file says so.
- Summarise for the user in plain language what changed, what was verified and what was not.

## Project skills

<List skills in `.agents/skills/` and when to use them, or "none yet".>
