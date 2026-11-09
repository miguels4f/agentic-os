# Project skills

Put skills that only make sense for this project here, one folder each:

```text
.agents/skills/<skill-name>/SKILL.md
```

Codex reads this folder directly. Claude Code reads it through the `.claude/skills` symlink.
General-purpose skills belong in `~/.agents/skills/` instead (see the kit's `install.sh`).

`SKILL.md` starts with:

```markdown
---
name: <skill-name>
description: Use when <situation>. Do NOT use for <other situation>.
---
```
