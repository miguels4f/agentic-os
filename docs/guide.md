# Guide: one setup for Codex, Claude Code and Cursor

This guide explains the layout in `template/` and how each agent app reads it. Read it once; after
that you only edit `AGENTS.md` and add skills.

## The idea in one paragraph

Every agent app wants instructions and reusable procedures, but each looks in a different place.
If you keep a copy per app, the copies drift and the agents behave differently. This kit keeps
**one source of truth** and lets every app read it:

| What | Single source | Codex reads | Claude Code reads | Cursor reads |
| --- | --- | --- | --- | --- |
| Project instructions | `<project>/AGENTS.md` | `AGENTS.md` directly | `CLAUDE.md`, which contains only `@AGENTS.md` (an import) | `AGENTS.md` directly |
| Personal instructions (all projects) | `~/.agents/AGENTS.md` | `~/.codex/AGENTS.md` → symlink to it | `~/.claude/CLAUDE.md` → symlink to it | paste into Cursor's User Rules |
| Project skills | `<project>/.agents/skills/<name>/SKILL.md` | `.agents/skills/` directly | `.claude/skills` → symlink to `.agents/skills` | reference the file from `AGENTS.md` or a rule |
| Personal skills | `~/.agents/skills/<name>/` | `~/.agents/skills/` directly | `~/.claude/skills/<name>` → symlink | as above |
| Shared memory (optional) | `~/.agents/memory/` | told where it is in `~/.agents/AGENTS.md` | same | same |

The rule: **edit only the single source**. Never edit the symlinked copies.

## Set up a project

```bash
cp -R /path/to/agentic-os/template/project/. /path/to/your-project/
```

This gives the project:

```text
your-project/
├── AGENTS.md            # the instructions (edit this)
├── CLAUDE.md            # one line: @AGENTS.md
├── .agents/skills/      # skills that only make sense for this project
└── .claude/skills -> ../.agents/skills
```

Fill in `AGENTS.md`. The template has sections for: what the project is, the commands to run,
test and lint it, the folder map, rules agents must follow (what they may never do without
asking), and how work is finished (tests green, pull request, who merges). Keep it short and
true: agents follow it literally, and a wrong command wastes every session.

The `agents-maintainer` skill keeps `AGENTS.md` in step with the repository later.

> Windows: symlinks need Developer Mode or `git config core.symlinks true`. Without them, replace
> `.claude/skills` with a copy, or keep project skills only in `.agents/skills` and use Codex.

## Set up your personal (user-level) files once

```bash
mkdir -p ~/.agents
cp /path/to/agentic-os/template/user/AGENTS.md ~/.agents/AGENTS.md   # then edit it
mkdir -p ~/.codex ~/.claude
ln -s ~/.agents/AGENTS.md ~/.codex/AGENTS.md
ln -s ~/.agents/AGENTS.md ~/.claude/CLAUDE.md
```

(If `~/.codex/AGENTS.md` or `~/.claude/CLAUDE.md` already exist, merge their content into
`~/.agents/AGENTS.md` first, then replace them with the links.)

Put here what is true for you in every project: language, how you like answers, what an agent
must always ask before doing (spending money, deleting data, pushing to main).

## Optional: shared memory

Agents forget everything between sessions. A memory folder is a set of small Markdown notes an
agent reads at the start of a session and updates when it learns a durable fact (a decision, a
gotcha, a pointer to a dashboard).

```bash
cp -R /path/to/agentic-os/template/user/memory ~/.agents/memory
```

`~/.agents/memory/MEMORY.md` is the index (one line per note); each note is one fact with a
short header. The user-level `AGENTS.md` template already tells agents to read the index first
and how to add notes. Claude Code also has its own built-in memory; pointing both apps at this
folder keeps them in agreement. Never store keys or passwords in memory notes.

## Skills

A skill is a folder with `SKILL.md`:

```markdown
---
name: my-skill
description: Use when <situation>. Do NOT use for <other situation>.
---

# Steps the agent follows
...
```

The `description` decides when the agent picks the skill, so write it as a trigger ("Use when…"),
not as a title. Extra files (scripts, references) live next to `SKILL.md` and the steps say when to
read or run them.

- **Codex** discovers skills in `~/.agents/skills/` and the project's `.agents/skills/`.
- **Claude Code** discovers skills in `~/.claude/skills/` and the project's `.claude/skills/`;
  the symlinks above make both point at the same files. Type `/<skill-name>` to force one.
- **Cursor** has no skill loader: mention the skill in `AGENTS.md` ("for X, follow
  `.agents/skills/x/SKILL.md`") or in a Cursor rule that references the file.

`./install.sh` puts this kit's skills in your personal skill folder. A skill that only matters for
one repository belongs in that repository's `.agents/skills/` instead.

## Daily use

- **Start a task** by saying what you want and what "done" means. The agent reads `AGENTS.md`
  and picks skills by itself.
- **Before pushing**, run `local-ci` (or ask for the `local-ci` skill) so the checks run on your
  machine, on the exact commit.
- **Before merging a screen**, ask for `ui-finish-gate` and `accessibility-audit`.
- **When a key leaks**, stop and follow `secret-leak-response`.
- **For a big build**, consider Wilfred: it adds planning, bounded work packets and signed gates
  on top of your agent (see `wilfred/README.md`).

## Third-party tools

`third-party/install.sh` installs the recommended plugins for whichever apps you have
(`claude plugin …` for Claude Code, `codex plugin …` for Codex) and links plain skill folders.
See `third-party/README.md`.
