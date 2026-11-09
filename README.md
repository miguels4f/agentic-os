# agentic-os

A starter kit for building software with AI coding agents (Codex, Claude Code, Cursor).

It gives you three things:

1. **A way to organise any project** so every agent app reads the same instructions and the same
   skills, instead of keeping one copy per app that drifts apart.
2. **A set of ready-made skills**: written procedures an agent follows for a recurring job
   (review a screen before merging, react to a leaked key, run the CI checks locally, and more).
3. **Wilfred** (optional, macOS): a local engine that splits a build into small, bounded pieces of
   work, checks each one with hard tests, and stops for a human signature before anything
   irreversible.

Everything here is plain files: Markdown instructions, a few shell and Python scripts. Nothing
runs in the background, nothing phones home, and nothing needs a paid account beyond the agent
app you already use.

## See the map first

[`docs/map.html`](docs/map.html) is an interactive map of the whole kit: every part, every file
with a one-line plain-English description, how the pieces connect, and three guided walkthroughs
(setting up a new computer, how the weekly update works, how Wilfred builds an agent). A
one-minute "Start here" tour runs the first time you open it.

GitHub shows HTML files as code, so open it from your copy instead. It is a single file and works
offline:

```bash
open docs/map.html        # macOS; on Linux use xdg-open, on Windows double-click it
```

The map was drawn with [Floorplan](https://github.com/haydenschmitty/floorplan). Its "Ask" box
needs that plugin and an agent app; everything else works in any browser.

## What is in the box

| Folder | What it is | Do you need it? |
| --- | --- | --- |
| [`template/`](template/) | The project layout: `AGENTS.md` (instructions), `CLAUDE.md` (points Claude Code at `AGENTS.md`), `.agents/skills/` (project skills), and an optional shared memory folder | Yes: copy it into each project |
| [`skills/`](skills/) | 16 general-purpose skills (list below) | Recommended |
| [`bin/local-ci`](bin/local-ci) | Runs your project's checks (tests, lint, type check, leak scan) on your machine before you push | Optional |
| [`third-party/`](third-party/) | Links to excellent tools by other people, and a script that installs them from their own source | Optional |
| [`wilfred/`](wilfred/) | The Wilfred engine (Python, macOS) | Optional, for bigger builds |
| [`docs/map.html`](docs/map.html) | Interactive map of everything in this kit | Open it first |
| [`docs/guide.md`](docs/guide.md) | How to use all of this with Codex, Claude Code and Cursor | Read once |
| [`tools/`](tools/) | How the maintainer keeps this kit up to date (you can ignore it) | No |

## Quick start (10 minutes)

You need `git`. For Wilfred you also need macOS, Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/miguels4f/agentic-os.git
cd agentic-os
./install.sh
```

`install.sh` copies the skills into `~/.agents/skills/` (where Codex looks) and links each one
into `~/.claude/skills/` (where Claude Code looks), and installs `local-ci` into `~/.local/bin/`.
It never overwrites something you already have with the same name; run `./install.sh --dry-run`
first to see exactly what it would do.

Then, for each project:

```bash
cp -R template/project/. /path/to/your-project/
```

and edit `/path/to/your-project/AGENTS.md`: what the project is, how to run it, how to test it,
and the rules an agent must follow. That one file is read by Codex directly, by Claude Code
through `CLAUDE.md`, and by Cursor (see the guide).

Optional extras:

```bash
third-party/install.sh --list     # see the recommended third-party tools
third-party/install.sh            # install them, asking before each one
```

## The skills

A skill is a folder with a `SKILL.md` file. The first lines say *when* to use it; the rest says
*how*. Agents load a skill automatically when your request matches its description, or when you
name it ("use the ui-finish-gate skill").

| Skill | Use it when |
| --- | --- |
| `accessibility-audit` | Before releasing a UI change: automated axe scan plus a manual keyboard pass, PASS/FAIL with fixes |
| `agents-maintainer` | Creating or updating an `AGENTS.md` so it matches the real repository |
| `annotated-system-map` | After a build: plain-language diagrams and a walkthrough of how the system works |
| `automation-governance` | Before creating a scheduled task, cron job or unattended agent chain: who approves, how it is switched off |
| `browser-test-hygiene` | Writing or fixing Playwright browser tests without flaky waits and hidden coupling |
| `buscar-antes-de-construir` | Before building a new workflow or skill: search what already exists and adopt instead of rebuilding ("search before you build") |
| `handoff-prompt` | Writing a complete prompt that hands work to a new agent session |
| `local-ci` | Before pushing or merging: run the CI checks locally on the exact commit through an independent agent |
| `local-first-mvp` | Planning an MVP on free local models (Ollama, LM Studio) and keeping paid models for the final step |
| `money-flow-integrity` | Touching payment, order, booking or refund code: no double charge, nothing lost on retry |
| `persona-walkthrough` | Before publishing a page: walk it as each of your target users and list what blocks them |
| `secret-leak-response` | A key or password shows up in a diff, log or chat: rotate, contain, purge, audit, in that order |
| `slopbuster-audit` | Reviewing a repository or diff for AI-generated dead code, mocks, duplication and drift |
| `ui-finish-gate` | Before merging a new screen: a short design contract and a HOLD/PASS verdict |
| `workflow-spec` | Before building a multi-step flow (checkout, booking, agent pipeline): states, failures and cleanup written down first |
| `youtube-summarizer` | Summarising a YouTube video from its transcript |

## Third-party tools (not included, linked)

The kit recommends: **superpowers**, **caveman**, **impeccable**, Anthropic's **frontend-design**,
**floorplan**, **hyperresearch**, OpenAI's **codex-plugin-cc**, **ECC**, plus the
**shaping-skills** and **pdf-to-markdown** skills. Their files are *not* copied here: see
[`third-party/README.md`](third-party/README.md) for links, licences and the installer.

## Wilfred

[`wilfred/`](wilfred/) is a local engine for bigger builds. You give it an objective; it plans
small work packets with limits on which files each may touch, hands them to your coding agent,
runs deterministic checks (tests, lint, regressions) and refuses to ship until they pass.
Anything irreversible (merge, deploy, dependency changes) waits for a signature you make in your
own terminal with a key only you hold. **No key ships with this kit; you create your own** (see
[`wilfred/README.md`](wilfred/README.md)). It works with Claude Code and with Codex
(`--provider codex`). It needs macOS, because the code it generates runs inside the macOS
sandbox.

```bash
cd wilfred
uv sync --group dev
uv run wilfred --help
uv run pytest -q
```

## Safety notes

- Skills, plugins and hooks run with your permissions. Read a skill before you rely on it, and
  read a third-party tool's README before installing it.
- Never paste real keys into agent chats. If it happens, follow `skills/secret-leak-response`.
- Keep personal memory and client data out of shared repositories. The template's memory folder
  is for project facts, not secrets.

## How this kit is kept up to date

The skills and Wilfred are cleaned-up copies of the maintainer's own working versions. Once a
week an automated job copies them again, strips private names, scans for leaked keys, runs
Wilfred's tests and, if anything changed, opens a pull request. A human reviews and merges it;
nothing is published automatically. The tooling is in [`tools/`](tools/).

## License

MIT, see [LICENSE](LICENSE). Parts adapted from other open-source projects are credited in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
