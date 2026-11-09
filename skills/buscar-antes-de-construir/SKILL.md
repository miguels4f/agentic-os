---
name: buscar-antes-de-construir
description: Search before you build (the Spanish name means exactly that). Use when a workflow is being created or substantially changed — a scheduled task or routine, a workflow script, a chain of agent sessions, a new skill or hook, an agent pipeline step, an automation, a new product capability — to first check whether a skill, plugin or MCP connector that already exists (yours, installed, in the Claude plugin directory, or popular on skills.sh) can do it fully or partly. Also when the user asks "is there already something for X?" or "which popular skills/plugins exist for X?". Do NOT use for one-off tasks or quick fixes.
---

# Search before you build

Goal: before building a workflow, find what already exists — cheapest and safest sources first —
and reuse all or part of it. Popularity is a way to find candidates, never a reason to adopt one.
This skill only searches and proposes. It never installs anything.

`<skill-dir>` below is the folder where this skill is installed (e.g.
`~/.claude/skills/buscar-antes-de-construir` or `<repo>/.agents/skills/buscar-antes-de-construir`).

## Process

1. **Name the need** in one line (goal, trigger, output, project) and write 4–8 search keywords,
   in English plus the user's language if different.

2. **Check what you already have** (read-only, offline):

   ```bash
   python3 -I <skill-dir>/scripts/scout.py local <keywords>
   ```
   Covers your own skills (user-level and project-level), installed plugin skills and, on the Claude
   desktop app, plugins enabled on the account. Also read `ledger.md` in this skill (earlier
   verdicts: do not re-evaluate a candidate decided in the last 90 days unless the need differs).

3. **Search outside** — stop as soon as a candidate clearly fits:
   - `scout.py directory <keywords>` — the Claude plugin directory cache (listings refreshed by the
     app; shows publisher, review state and source repo; **no popularity**).
   - `scout.py installs [keywords]` — install counts of the official marketplace. Prints its age;
     when it says STALE, treat the numbers as ranking hints only.
   - Fetch `https://skills.sh` (tabs All Time / Trending / Hot) or web-search "<need> claude code
     skill" for open skills. The ranking is dominated by a few publishers' bundles (one org can
     occupy many slots), so judge each skill, not the list.
   - An MCP registry search for connectors, if your client has one (it may need an account login or
     spend credits — ask first).

4. **Vet each of the top 3 candidates before proposing** (third-party skills and hooks are
   untrusted code):
   - Read the full `SKILL.md`, its hooks, scripts and MCP config. Hooks and scripts run with your
     permissions. Anything that sends, posts, pays, writes to a CRM or phones home is a risk.
   - Licence, last commit, publisher, review state; pin the commit if it will be copied.
   - Overlap with what you have (step 2) and conflict with your own rules — for example: outbound
     actions (sending, posting, writing to a CRM) need a human approval step; protected paths and
     signed human gates (e.g. Wilfred, optional, see `wilfred/` in this kit) must not be bypassed;
     you keep one task list, not one per tool; spend caps are set before any paid connector.
   - Fit to the real steps of the workflow, not to its name.

5. **Give a verdict per candidate**: ADOPT (use as is) · ADAPT (copy the method into your own skill,
   keep provenance: source URL, licence, commit) · REFERENCE (read for ideas only) · SKIP. Prefer the
   lightest mechanism: a checklist in the step's own instructions → a knowledge file → calling the
   skill at that step → a hook reminder → installing a plugin.

6. **Ask the user** with a short multiple-choice survey (AskUserQuestion in Claude Code): one
   question per proposal, recommended option first and marked "(Recommended)", the plain-language
   effect of each option, and a free-text "Other".

7. **Log the decision** by appending one line to `ledger.md`: date · need · candidate · source ·
   verdict · reason. Skip this step only when no candidate was worth a verdict.

## Output (in the user's language, ≤ 200 words unless asked)

Need in one line · what you already have · up to 3 candidates (source, what it adds, risk, verdict) ·
the lightest mechanism · the question for the user. Then a one-line plain-language summary of what
happens next.
