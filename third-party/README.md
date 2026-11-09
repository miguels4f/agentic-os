# Third-party tools (linked, not copied)

These are tools by other people that work well with this kit. **None of their files are in this
repository.** `install.sh` in this folder fetches each one from its own source, so you always get
the authors' current version under the authors' own licence.

| Tool | What it does for you | Source | Licence |
| --- | --- | --- | --- |
| superpowers | Process skills: brainstorm before building, write a plan, test-driven development, systematic debugging, code review | <https://github.com/obra/superpowers> | MIT |
| caveman | A terse answer style that cuts tokens (and cost) while keeping technical detail; installs a session hook | <https://github.com/JuliusBrussee/caveman> | Apache-2.0 |
| impeccable | Frontend design: critique, polish, accessibility and anti-pattern audits | <https://github.com/pbakaus/impeccable> | Apache-2.0 |
| frontend-design | Anthropic's skill for distinctive, production-grade interfaces | <https://github.com/anthropics/skills/tree/main/skills/frontend-design> | Apache-2.0 (per skill) |
| floorplan | Plain-language, browsable maps of a codebase | <https://github.com/haydenschmitty/floorplan> | MIT |
| hyperresearch | Deep, multi-source research with adversarial critique, plus the `hpr` command | <https://github.com/jordan-gibbs/hyperresearch> | MIT |
| codex-plugin-cc | Call Codex from inside Claude Code (second opinion, hand over a stuck task) | <https://github.com/openai/codex-plugin-cc> | Apache-2.0 |
| ECC (Everything Claude Code) | A very large collection of agents, skills, hooks and rules; pick what you need | <https://github.com/affaan-m/ECC> | MIT |
| shaping-skills | Shape Up style `shaping`, `breadboarding` and `breadboard-reflection` skills | <https://github.com/rjs/shaping-skills> (archived) | none stated |
| pdf-to-markdown | Turn a whole PDF into Markdown an agent can read | <https://github.com/aliceisjustplaying/claude-resources-monorepo/tree/main/skills/pdf-to-markdown> | none stated for the repo |

"None stated" means the repository has no licence file. The code is public to read, but nobody
granted the right to redistribute it, so this kit only links to it and the installer clones it
for your personal use.

## Install

```bash
third-party/install.sh --list      # see exactly what would run
third-party/install.sh             # asks before each tool
third-party/install.sh superpowers floorplan   # only some
```

What the script does, per tool:

- **Claude Code** (if the `claude` command exists): adds the tool's plugin marketplace and installs the plugin.
- **Codex** (if the `codex` command exists): the same through `codex plugin`, for tools that publish a Codex marketplace.
- **Plain skill folders** (frontend-design for Codex, shaping-skills, pdf-to-markdown): a shallow `git clone`
  into `~/.local/share/agentic-os/third-party/` and a symlink into `~/.agents/skills/` and `~/.claude/skills/`.
  Run the script again to update them (`git pull`).

Plugins and skills run with your permissions, and some add hooks that run on every session.
Read a tool's README before you install it.
