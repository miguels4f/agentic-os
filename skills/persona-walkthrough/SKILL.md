---
name: persona-walkthrough
description: Use after a change to a marketing site, landing or pricing page, signup flow or developer-facing docs page, before publishing — simulates one of your defined buyer/user personas scrolling the page (five-second test, then fold by fold in two voices) and returns prioritised fixes. Do NOT use as evidence of what users think, for WCAG checks, or for pre-merge visual gating (ui-finish-gate).
---

> Adapted from agency-agents `design-persona-walkthrough` (https://github.com/msitarzewski/agency-agents, MIT, commit 4e3ac3c). Persona removed, method kept, examples made generic.

# Persona walkthrough

Read a page as a specific buyer would: impatient, scanning, with their own fears. Each fold gets
two separate voices — the persona's raw reaction and a structured assessment — so friction becomes
visible. This is a qualitative simulation: hypotheses to test with real people, never proof.

## When to use

- Before publishing changes to a marketing site, a landing or pricing page, a signup / onboarding
  flow, or partner- and developer-facing pages (API docs, sandbox, integration guides).
- Before publishing a consumer app screen meant for first-time visitors.

## Personas (pick one or two, never a generic "user")

> **Replace these examples with your own personas.** They are generic placeholders that show the
> level of detail needed. Keep your real persona table here (or in a project file this skill
> points to) so every walkthrough uses the same people.

| Persona | Arrives with | Fears | Trusts |
|---|---|---|---|
| B2B buyer evaluating a SaaS tool | a shortlist of 3 vendors and a budget owner to convince | hidden pricing, lock-in, a tool the team will not adopt | customer logos in their industry, transparent pricing, a security/compliance page |
| Developer integrating your API | a ticket to ship an integration this sprint | poor docs, breaking changes, unclear auth and rate limits | a quickstart that works in 5 minutes, API reference, sandbox, changelog |
| First-time consumer | a problem to solve today, arriving from a search or a social post | scams, surprise costs, being forced to create an account too early | plain prices, reviews, a clear contact and company identity |
| Accessibility-dependent user | a screen reader, keyboard-only navigation or high zoom | unlabeled controls, keyboard traps, content that only works with a mouse | predictable structure, visible focus, text readable at 200 % zoom |

Fill for the chosen persona: name, role, company type, how they arrived (search query, social
post, referral), what they saw before, device (default 390×844), urgency, decision style, what
would make them act now.

## Method

1. **Pre-arrival (no screenshot):** 3–4 sentences in the persona's voice about expectations, then
   the relevance contract — what the first screen must show to keep them.
2. **Five-second test** on the first stable screenshot (built-in browser tool or Playwright) at
   390×844, and at 1440×900 for B2B personas who use desktop:
   - What is this? · Is it for me? · What should I do next?
   Any "no / unclear" is a critical finding.
3. **Scroll fold by fold** (~700–800 px). For each fold write two blocks, never mixed:
   - *Persona voice:* first person, plain words, no UX jargon.
   - *Assessment:*
     ```
     Fold N · emotion: <one word> · trust: ↑/↓ <why>
     LIFT: <value prop / relevance / clarity / urgency / anxiety / distraction most affected>
     Persuasion present / missing: <social proof, authority, reciprocity, commitment, liking, scarcity, unity>
     Fogg: motivation L/M/H · ability L/M/H · prompt visible yes/no
     CTA reachable without scrolling: yes/no
     Wording issues: <see rule below>
     ```
4. **Wording rule (optional, project rules = automatic finding):** if the project keeps banned or
   required claims (regulatory wording, "never promise X"), or forbids unqualified absolutes such
   as "100 % secure" or "carbon neutral" without the method behind them, any violation is an
   automatic finding.
5. **Verdict** in the persona's voice, then: confidence 1–10, clarity 1–10, relevance 1–10,
   would act (yes / no / maybe — why), top 3 strengths, top 3 weaknesses, the fold where they
   almost left, the fold where they were most engaged.
6. **Recommendations**, each tied to a fold, a framework factor and the reaction it fixes:
   ```
   <Quick win | Major | Strategic> — <title>
   Fold N · <framework factor> · What: <change> · Why: <persona reaction> · Expected effect: <...>
   ```
7. With two personas, add a short table of where their needs conflict.

## Output

One Markdown report: header line "Qualitative simulation, not user evidence", persona card,
five-second test, folds, verdict, recommendations, screenshot paths. End with three plain-language
lines for the user (in the user's language): the biggest problem, the cheapest fix, and whether it
can be published.

## Stop / escalate

- Page needs login or seeded data you cannot create → walk only the public part and say so.
- A fix changes positioning, pricing or claims → propose it to the user; do not edit copy yourself.

## Out of scope

Publishing, editing copy, social media posts, WCAG (`accessibility-audit`), pre-merge UI gate
(`ui-finish-gate`), real user research.
