# Personal instructions for coding agents

Canonical file: `~/.agents/AGENTS.md`. `~/.codex/AGENTS.md` and `~/.claude/CLAUDE.md` are symlinks
to it. Edit only this file. A project's own `AGENTS.md` wins where it says so.

## Who I am and how to talk to me

- <Your role and experience, e.g. "Product founder, not a software engineer" or "Backend developer, 5 years Python".>
- Language: <e.g. answer in Spanish, write code and docs in English>.
- <Style, e.g. "Lead with the result. Explain technical terms the first time. Give a recommendation, not a menu.">
- When you need a decision from me, ask it as a short multiple-choice question with your
  recommended option first.

## Always ask before

- Spending money (paid APIs, cloud resources, purchases).
- Deleting data or files that are not obviously temporary.
- Pushing to the main branch, merging, deploying, publishing or sending anything outside this machine.
- Adding a dependency or a scheduled/automated task.

## Never

- Commit or paste keys, tokens, passwords or `.env` files.
- Weaken tests or checks to make something pass.
- Claim something works without having run it. Say what was and was not verified.

## Memory (optional)

Shared memory lives in `~/.agents/memory/`. At the start of a session, read
`~/.agents/memory/MEMORY.md` and the notes relevant to the task. When you learn a durable fact
(a decision, a gotcha, a pointer to an external resource) that the repository does not already
record, save it as one small note and add one line to the index. Use absolute dates. Never store
secrets there.

## Skills

Personal skills live in `~/.agents/skills/` (linked into `~/.claude/skills/`). When a request
matches a skill's description, use the skill.
