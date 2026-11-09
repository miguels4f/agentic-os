---
name: ui-finish-gate
description: Use before merging a new or redesigned screen in a web app (dashboard, editor, checkout, settings page...) — writes a Design Contract for the screen and returns a HOLD or PASS gate checked with real screenshots at desktop and mobile width. Do NOT use for backend-only changes, copy tweaks, or open-ended redesign ideation (use a design-critique or frontend-design skill for that).
---

> Adapted from agency-agents `design-ui-finish-gate-reviewer` (https://github.com/msitarzewski/agency-agents, MIT, commit 4e3ac3c). Persona removed, method kept, examples made generic.

# UI finish gate

A last review of an implemented screen before merge. The question is not "is it pretty" but "does
this screen show our product's object and main action, in every state, at both widths". Output is
a decision (HOLD or PASS) with evidence, not a mood board. Read-only: report, never push or merge.

Overlap: design-critique and frontend-design skills help design or critique; this skill only
formalises the pre-merge decision. Run `accessibility-audit` (in this kit) separately for WCAG.

## When to use

- A PR adds or reshapes a page in a web app (e.g. `<your-app>/app/` in Next.js,
  `<your-app>/src/` in Vite + React, or any other frontend).
- Someone says "the screen is ready", "can we merge this design?".

## Inputs to gather

1. Branch or PR and the route(s) changed. Read the project `AGENTS.md` / README for how to run it
   locally.
2. Who uses the screen and the job they finish there (ask if not in the PR or docs; otherwise mark
   it as an assumption).
3. Existing tokens / component library / brand already in the repo — keep them unless a concrete
   problem forces a change.
4. A running local instance. Use a built-in browser tool (browser pane or browser MCP) or
   Playwright `page.screenshot` to capture each state at **1440×900** and **390×844**.

Do not upload unpublished product screens to any external reference catalogue or service.

## Method

1. **Product lens (one paragraph before any critique):** user, task, the object or status that must
   be read first, what repeats daily, what is rare but risky (e.g. cancelling a booking, deleting
   an account, issuing a refund).
2. **Design Contract** — fill this before judging:
   ```markdown
   # <Screen> Design Contract
   User + job:          <who finishes what>
   First-read object:   <the thing the eye must land on first>
   Primary action:      <one observable action>
   Density:             compact | balanced | spacious — because <reason>
   Hierarchy:           <headline → key signal → controls → supporting info>
   Interaction model:   table | form | editor | timeline | wizard | feed ...
   Responsive priority: <what stays, collapses or moves at 390 px>
   References:          <pattern → lesson> (2–5, from comparable products; never copied)
   Forbidden defaults:  <specific generic patterns banned on this screen>
   Finish evidence:     <states × widths that must be screenshotted>
   ```
3. **Capture evidence** at both widths for: default, loading, empty, error, long labels / large
   numbers, keyboard focus on the primary action, disabled state where it exists.
4. **Review in this order**, writing each finding as *observed → why it breaks the lens*:
   1. Product legibility — can a new user name the object and the main task in the first viewport?
   2. Hierarchy — does visual weight follow decisions, or component-library defaults?
   3. Pattern fit — does each block (cards, tables, hero, charts) earn its place?
   4. States — loading, empty, error, selection, focus, disabled are intentional and useful.
   5. Responsive — the narrow layout keeps the job (status, owner, next action), not just stacked cards.
   6. Fidelity — tokens, components, copy and assets match the rest of the product.
5. **Wording check (optional, project rules):** if the project keeps a list of banned or required
   terms (legal, regulatory or brand wording — e.g. "never call this export a certified report"),
   check the screen against it. A violation of such a rule is an automatic HOLD.
6. **Decide.** PASS only if first-read object and primary action are visible at both widths, no
   forbidden default remains without a stated product reason, and every listed state was seen.
   Otherwise HOLD. Never soften a HOLD into "nice to have".

## Output

```markdown
# UI Finish Gate — <screen> (<branch/PR>)
Decision: HOLD | PASS
Design Contract: <inline or path>
Evidence: <screenshot paths, one per state × width>

Findings (required before PASS)
1. <observed issue> → <why it breaks the lens> — fix: <concrete change> — verify: <state + width>

Keep
- <decision that already works; do not rewrite it>

Optional refinements (not blocking)
- ...

Not checked
- <states or widths that could not be reached, and why>
```

Close with two lines in plain language for the user (in the user's language): whether the screen
can be merged and what is missing.

## Stop / escalate

- The app cannot be started locally or the route needs data you cannot seed → report "NOT
  CHECKED", list what is missing; do not PASS from code reading alone.
- The fix needs a brand or product decision (new colour system, different primary action) → ask
  the user with a short multiple-choice survey (AskUserQuestion in Claude Code) instead of deciding.

## Out of scope

Redesigning the screen, writing the fix, WCAG conformance (`accessibility-audit`), conversion copy
(`persona-walkthrough`), merging or deploying.
