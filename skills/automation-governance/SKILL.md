---
name: automation-governance
description: Use before creating or changing a scheduled task, routine, cron job, workflow-automation flow (n8n, Zapier, a CI schedule) or chain of unattended agent sessions — audits value and risk on four dimensions and returns APPROVE / PILOT / PARTIAL / DEFER / REJECT with reliability, logging and testing baselines; never edits the schedule itself. Do NOT use for one-off tasks or for finding an existing skill, plugin or agent that already does the job (use buscar-antes-de-construir).
---

> Adapted from agency-agents `automation-governance-architect` (https://github.com/msitarzewski/agency-agents, MIT, commit 4e3ac3c). Persona removed, method kept, examples made generic.

# Automation governance

Decide whether an automation should exist, in what form, and with which safeguards — before it is
built or changed. "Technically possible" is not a reason. Read-only: this skill produces a verdict;
it never creates, edits, runs or deletes a scheduled task.

Overlap: `buscar-antes-de-construir` checks whether something that already exists can do the job;
your agent's scheduling tool (a `schedule` skill, cron, a CI schedule) creates the task. This skill
audits the automation itself and runs before anything is created.

## Hard rules

1. **No scheduled task, routine or cron job is created or changed without the user's explicit OK
   in chat for that specific change.** The verdict is a proposal.
2. **Unattended runs must not depend on a human-gated step.** A scheduled session that hits a hook,
   permission prompt or approval dialog needing a person stalls silently. For every step, confirm
   it runs without approval prompts, or move that step to an attended session.
3. **Never automate outbound actions** (posting, emailing, messaging, writing to a CRM, task tracker
   or shared document) without a human checkpoint: a person approves the exact content before it
   leaves, and the automation only publishes what was approved.
4. Every automation needs an owner, a fallback and a way to switch it off.

## Inputs to gather

Process name and goal · trigger and frequency · steps · systems touched (repos, task tracker, email,
social accounts, shared docs, external APIs) · model and permission mode of the session · expected
cost per run · where results land · who reads them.

## Method

1. **Audit four dimensions** (score Low / Medium / High with one line of evidence each):
   - *Time saved per month* — recurring and material, or rare?
   - *Data criticality* — customer, money, contracts, public posts, compliance wording? What does a
     wrong, late, duplicated or missing run cost?
   - *Dependency risk* — how many external services, logins, rate limits, hooks, local apps that
     must be open, paid quotas?
   - *Scale* — at 10× volume do retries, dedupe and exception handling still hold?
2. **Verdict — exactly one:** APPROVE · APPROVE AS PILOT (limited runs, review after N) ·
   PARTIAL AUTOMATION ONLY (automate safe steps, keep human checkpoints) · DEFER (process or
   dependencies not stable) · REJECT (weak value or unacceptable risk).
3. **Reliability baseline:** explicit failure branch · duplicate protection (idempotent output,
   re-run safe) · bounded retries with a stop · timeout · alert or visible failure note · manual
   fallback · unattended-step check from rule 2 · cost cap per run if it calls paid models.
4. **Logging baseline:** automation name and version, run timestamp, source, affected entity ids,
   success/failure, error class with a short cause, cost.
5. **Testing baseline before enabling:** happy path · invalid input · dependency down · duplicate
   trigger · recovery after a failed run · one dry run attended by a human.
6. **Integration sheet per connected system:** role and source of truth, auth and token expiry,
   trigger model, fields written vs read-only, rate limits, owner and escalation.
7. **Re-audit triggers:** API or schema change, rising failures, volume jump, new compliance rule,
   repeated manual fixes.

## Output

```markdown
# Automation audit — <name> (<date>)
1. Process: goal · trigger · steps · systems
2. Audit: time saved <L/M/H — why> · criticality <...> · dependency risk <...> · scale <...>
3. Verdict: APPROVE | APPROVE AS PILOT | PARTIAL AUTOMATION ONLY | DEFER | REJECT
4. Rationale: impact · key risks · why this verdict
5. Architecture: trigger → validation → logic → external actions → result check → log → error
   branch → fallback → status note
6. Baselines: reliability ☐ · logging ☐ · tests ☐ · unattended-step check ☐
7. Preconditions: approvals the user must give · limits · rollout guardrails · how to switch it off
```

Then ask the user with a short multiple-choice survey (AskUserQuestion in Claude Code): approve as
proposed / pilot first / not now — each option explained in plain language (in the user's language)
with its cost and what changes for them.

## Stop / escalate

- The automation would publish, send or write to an external system without a human checkpoint →
  REJECT or PARTIAL, never APPROVE.
- A step cannot run unattended → say so plainly; do not schedule it hoping it works.

## Out of scope

Creating or editing the schedule, building the automation, choosing which existing agent to use.
