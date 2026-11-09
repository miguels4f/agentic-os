---
name: handoff-prompt
description: Use when the user asks for a prompt to continue or launch work in another session — "give me a prompt for another session", "write a prompt to launch X", "a very detailed prompt for a model like Opus or Sonnet", "so it runs with a clean context" — or when the current context window is nearly full and work must hand off.
---

# Handoff Prompt

## Overview

Generate a self-contained prompt that a **fresh session with zero shared context** can execute without asking questions. The receiving session knows nothing about this conversation — everything load-bearing must be in the prompt itself.

## The Output Contract

Produce ONE fenced code block (so the user can copy it in one click), containing these sections in this order:

1. **ROLE** — one line: what kind of engineer/agent the session is, and that the user may be away (and may not be an engineer), so execute autonomously and report honestly in plain language.
2. **REPO / PATHS** — absolute paths to every repo and directory involved. Name the git remote and base branch. If multiple copies of a product exist (demos, worktrees, mirrors), state explicitly which one is canonical and that the others must NOT be touched.
3. **READ FIRST** — the source-of-truth files, in reading order, with absolute paths. Add "code is truth: confirm each claim at the cited location before acting" when the docs may have drifted.
4. **ALREADY DONE — DO NOT REDO** — merged PRs, closed items, decisions taken. This prevents re-work and re-litigating settled decisions. Follow it with an **ENTRY CHECK**: the first step of TASK re-verifies each of these facts at its source (PR state, branch HEAD, file exists, test count) and STOPs with a short report if any is false — prompts written ahead of time go stale.
5. **TASK** — the goal stated as an outcome, then the concrete steps. Match detail to the executing model: for mid-size models, spell out file names, commands, and edge cases; for the most capable models, outcomes and constraints suffice.
6. **CONSTRAINTS** — merge policy (default: open a PR, no auto-merge, the user approves; state exceptions explicitly), test suites that must stay green with their exact commands and current counts, budget/cost limits, and **parallel safety**: the folders, branches and worktrees this session will write; any other session that may be running at the same time (name it — check `git worktree list` and open PR branches) and what this one must not touch; if they overlap, the condition to wait for before starting (e.g. "start only after PR #N is merged").
7. **DELIVERABLES & EVIDENCE** — what artifacts to produce, where to write them (absolute paths), and what proof of success looks like (test output, screenshots, PR URL). End with: "Be honest about what got deferred or stuck — never fake a clean result."

## Rules

- Scope constraints narrowly: constrain only what the user stated. When they ask for research or search prompts, do not exclude things they already own and do not add cost/tool assumptions they didn't give — over-constrained prompts are the most common reason a prompt has to be regenerated.
- Convert every relative reference ("the demo", "the document from before", "yesterday's thing") into an absolute path, URL, or PR number before it goes in the prompt.
- State the intended model and mode only if the user specified one.
- If the work is tracked as a task in the user's task tracker and stays open because the new session carries it on, add a note to that task ("continues in a new session: <short title>") before giving the prompt, so it is not mistaken for abandoned work.
- After the code block, add at most three plain-language sentences (in the user's language): what the prompt will do and, whenever you give more than one prompt or another session may still be running, whether they can run at the same time ("you can launch them together" / "launch B only after A finishes, because both touch X"). Nothing else.

## Common Mistakes

- Referencing "this session's plan/audit" without pasting or pointing to a file the new session can read.
- Omitting the ALREADY DONE section — the new session redoes merged work.
- Burying the prompt in explanation — the user copies exactly one block; anything outside it is lost.
