---
name: workflow-spec
description: Use before implementing or changing a multi-step stateful flow — a booking API's quote → book → cancel → fulfil, a checkout or payment, an order or refund, an agent pipeline with handoffs — to write a workflow tree spec: every branch, timeout, handoff contract, cleanup, and one test per branch. Do NOT use for single-step CRUD, UI layout, or choosing between solutions (use a shaping skill for that).
---

> Adapted from agency-agents `specialized-workflow-architect` (https://github.com/msitarzewski/agency-agents, MIT, commit 4e3ac3c). Persona removed, method kept, examples made generic.

# Workflow spec


> New flow, not a change to an existing one? First run `buscar-antes-de-construir` (search before building, in this kit) — a skill, plugin or library may already cover part of it (reuse before writing the spec).

Before code: write down every path a stateful flow can take, not just the happy one. Each step
gets a timeout, success and failure outputs, what each actor can see, and what must be cleaned up.
Every branch becomes one test. The spec states required behaviour, not implementation.

Overlap: a shaping skill picks the solution; a breadboarding skill maps places and affordances;
this skill specifies the state machine and failure branches of the chosen flow. An orchestrator's
decompose step (e.g. Wilfred, optional, see `wilfred/` in this kit) can consume the test list.

## When to use

- A partner or public API with a multi-call lifecycle, e.g. a booking API
  (`POST /bookings/quote`, `POST /bookings`, `GET /bookings/:id`, `POST /bookings/:id/cancel`,
  `POST /bookings/:id/fulfil`; statuses such as `HELD`, `PARTIALLY_HELD`, `PENDING_INVENTORY`,
  `CANCELLED`, `FULFILLED`). If contracts already exist (e.g. `<your-app>/docs/api/`, slice
  contracts, ADRs), extend them — do not duplicate.
- Claim, transfer or redemption of an asset or entitlement; any payment, refund or payout.
- Agent pipelines or scheduled jobs that hand work between steps or services.

## Inputs to gather

1. The trigger (API call, UI action, scheduled job, event) and the entity whose state changes.
2. The actual code if it exists: routes, services, ORM schema enums, migrations,
   `git log --oneline -10 -- <file>`. Code and intent drift; read the code.
3. Existing specs, slice contracts and ADRs for the flow.

## Method

1. **Discovery.** Find entry points and state writes:
   `rg -n "@(Post|Put|Patch|Delete|Get)\(" <module>` (or your framework's route pattern),
   `rg -n "status:" <module>`, enums in the schema file (e.g. `schema.prisma`, ORM models), cron /
   schedule definitions. List flows that exist in code without a spec as "Missing".
2. **Actors.** Every system, service, external party and human role (partner, end customer,
   internal operator, third-party provider, scheduler).
3. **Happy path first**, step by step.
4. **Branch every step** against seven failure classes: invalid input · timeout · transient
   (retryable) · permanent · partial (step N fails after 1..N-1 succeeded) · concurrent conflict
   (same resource twice, double submit, replay) · expiry (quote expired, hold released).
5. **Observable state per step and per failure:** what the customer/partner sees, what the operator
   sees, what is in the database, what is in the logs.
6. **Handoff contract** at every boundary:
   ```
   HANDOFF <from> -> <to>
     payload:  { field: type }
     success:  { field: type }
     failure:  { code, message, retryable: bool }
     timeout:  <n s> → treated as failure
     on failure: <recovery>
     idempotency: <key and replay behaviour>
   ```
7. **Cleanup inventory:** every resource created (rows, held inventory, pending requests,
   files, external records) with the step that creates it and how it is undone, in reverse order.
8. **Timing assumptions:** any step that assumes another is "already done" — name the mechanism
   that guarantees ordering (transaction, lock, unique constraint, poll) or flag it as a race.
9. **Assumptions table:** anything not verified from code, with risk if wrong.
10. **Derive tests:** one row per branch. A branch without a test is unspecified behaviour.
11. **Reality check:** reread the code against the finished spec and log divergences (spec wrong →
    update spec; code wrong → finding).

## Output — `WORKFLOW-<kebab-name>.md` next to the project's existing specs

```markdown
# WORKFLOW: <name>   Version 0.1 · <date> · Status: Draft | Review | Approved
Overview (2–3 sentences) · Actors table · Prerequisites · Trigger

## Steps
### STEP n: <name>
Actor · Action · Timeout · Input · On success → STEP n+1
On failure: validation / timeout / transient / permanent / conflict → <recovery>
Observable: customer · operator · database · logs

### ABORT_CLEANUP
Triggered by · actions in reverse creation order · what each actor sees afterwards

## State transitions   (text diagram of status changes)
## Handoff contracts
## Cleanup inventory   | resource | created at | undone by | method |
## Test cases          | TC | trigger | expected |
## Assumptions         | # | assumption | verified where | risk |
## Open questions
## Spec vs code log    | date | finding | action |
```

Summarise for the user in plain language (in the user's language): how many branches, how many
lack a test or cleanup, and which open questions need their decision.

## Stop / escalate

- A branch needs a business rule nobody has decided (refund policy, who pays a shortfall) → list it
  under Open questions and ask the user with a short multiple-choice survey (AskUserQuestion in
  Claude Code); do not invent the rule.
- The flow passes secrets or creates credentials → flag it for a security review (e.g. a read-only
  security reviewer subagent, or `secret-leak-response` in this kit if a value is already exposed).

## Out of scope

Writing the implementation, UI design, unrelated workflows (note them, one spec per flow).
