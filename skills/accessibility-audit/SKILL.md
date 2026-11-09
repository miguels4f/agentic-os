---
name: accessibility-audit
description: Use before releasing a UI change in a web app, marketing site or static HTML site — runs an axe-core scan plus a manual keyboard pass and reports PASS / FAIL / NOT TESTED per WCAG criterion, with conformance "NOT DETERMINED" when scope is incomplete. Do NOT use for visual design critique, legal advice on accessibility law, or backend-only changes.
---

> Adapted from agency-agents `testing-accessibility-auditor` (https://github.com/msitarzewski/agency-agents, MIT, commit 4e3ac3c). Persona removed, method kept, examples made generic.

# Accessibility audit

An honest accessibility check for a UI change. Automated scanners catch only part of WCAG; the
rest needs keyboard and human checks. An agent cannot run a real screen reader or voice control,
so those rows are reported as NOT TESTED, never as PASS. Read-only: report findings, do not fix
unless asked separately.

## When to use

- Before a merge or release that changes pages in: a web app (e.g. Next.js or React with a
  Playwright suite), a public marketing site, an app with only HTML-render tests and no browser
  suite yet, or a static HTML site.
- The user asks "is this accessible?" about a screen.

## Inputs to gather

1. Exact scope: list of URLs **and** states (logged out / logged in, empty, error, modal open).
   One URL is never "the site".
2. A running local build (see the project `AGENTS.md` / README) or the preview URL.
3. Which components are custom (tabs, menus, dialogs, date pickers, data tables, carousels): these
   get the full keyboard pattern check.

## Method

1. **Automated baseline, per URL and state.** Preferred inside an existing Playwright suite:
   ```ts
   import AxeBuilder from '@axe-core/playwright';
   const r = await new AxeBuilder({ page })
     .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze();
   expect(r.violations).toEqual([]);
   ```
   One-off alternative: `npx @axe-core/cli <url> --tags wcag2a,wcag2aa,wcag21a,wcag21aa,wcag22aa`.
   If `@axe-core/playwright` is not already a dependency of the project, ask before adding it.
   Check the current axe tag names in the axe-core docs; they change between versions.
2. **Keyboard pass (no mouse)** — global checklist:
   - [ ] every interactive element reachable with Tab, order follows the visual layout
   - [ ] focus indicator visible on every element; nothing hidden behind sticky headers
   - [ ] no keyboard trap; Escape closes dialogs, menus and overlays
   - [ ] focus returns to the trigger after a dialog closes; skip link present on long pages
3. **Per-component patterns** (follow WAI-ARIA Authoring Practices):
   - Tabs: arrows move between tabs, Home/End jump, `aria-selected` on the active tab.
   - Menus: arrows navigate, Enter/Space activate, Escape closes and returns focus.
   - Dialogs: focus moves in, is trapped, has an accessible name, returns on close.
   - Data tables (listings, orders, transaction history): header cells with `scope`, caption or
     label, sortable columns operable by keyboard and state announced.
   - Forms: visible labels tied to inputs, required state exposed, errors linked to the field and
     announced (live region), not colour-only.
   - Carousels: pause control, arrows move slides, position exposed.
4. **Visual checks** with the browser: 200 % and 400 % zoom (no overlap, no horizontal scroll at
   320 CSS px), `prefers-reduced-motion`, forced-colours / high contrast where possible, contrast of
   text and focus rings.
5. **Accessibility tree check** instead of a screen reader: read the page's accessibility tree
   (a browser tool's accessibility-tree reader, or Playwright `ariaSnapshot`) for names, roles,
   heading order and landmarks. Mark real screen-reader and voice-control rows NOT TESTED.
6. **Classify** each issue: WCAG number + name, level, severity (Critical blocks a task; Serious
   needs a workaround; Moderate; Minor), who is affected, location, evidence, fix, how to verify.

## Output

```markdown
# Accessibility audit — <feature> (<branch/PR>, <date>)
Standard: WCAG 2.2 A/AA
Scope: <URLs × states × widths>     Untested scope: <what was not covered>
Tools: axe-core <version>, keyboard, accessibility tree, zoom

| Criterion | Result | Evidence |
|---|---|---|
| 1.4.3 Contrast (Minimum) | PASS / FAIL / NOT TESTED / N/A | <selector, ratio, screenshot> |

Conformance: DOES NOT CONFORM (any in-scope A/AA FAIL) | NOT DETERMINED (any required test
missing, e.g. screen reader) | CONFORMS (only if every applicable A/AA criterion was evaluated on
full pages and complete processes)

Issues (Critical → Minor)
1. <title> — <criterion> — <severity> — <location> — fix: <...> — verify: <...>

Keyboard results: <reachable n/N, traps n, missing focus indicators n>
Working well: <patterns to keep>
```

A clean axe scan alone never yields CONFORMS. End with a plain-language line for the user (in the
user's language): can it ship, and what is blocking.

## Stop / escalate

- Questions about the European Accessibility Act, the ADA or any other legal duty → state it is a
  legal question, do not answer it; suggest a specialist.
- A fix needs a design change (colour palette, layout) → hand it to `ui-finish-gate` / the user.

## Out of scope

Legal applicability, real assistive-technology testing, rewriting components, merging.
