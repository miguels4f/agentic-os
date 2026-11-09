# HyperWilfred — Continuous Improvement: built-in loops, operating routines, future hardening

> Historical design reference, imported from the source repository. Claims,
> commands and phase status below are not current product acceptance evidence.
> Use [architecture](architecture.md), [acceptance](acceptance.md) and
> [verification](verification.md) for the standalone product.

> Companion to `docs/hyperwilfred-architecture.md`. Written after Phase 5 shipped (2026-06-28,
> PR #10 merged to `main`). Leads in plain language, keeps the commands intact.
>
> **The golden rule across everything here:** the factory may *propose* changes to its own rules,
> but it **never applies one without an explicit human approval file.** Nothing in this document
> changes that.

---

## Plain-language summary

HyperWilfred can now watch its own track record and suggest how to get better — but a human always
decides. Phase 5 added three things that work together: a **learning loop** (it proposes fixes to
its own rules), an **observability layer** (a dashboard + anomaly alarms computed from disk), and a
**founder notification inbox** (a file you read; no Linear, no external services). This document is
the operating manual for those three, plus a prioritized list of what to build next.

---

## 1. The continuous-improvement loops the factory already has (Phase 5)

These are built, tested, and on `main`. They are CLI commands a person runs; the factory never
changes itself unattended.

### Loop A — Self-improvement (`learn → propose → promote`)

The factory mines its own run history (`.hw/*/`: timeline events, acceptance verdicts, quality-loop
history), detects recurring problems, and emits **review-only proposals** — each one a concrete
IMPROVE objective pointed at the factory's own step instructions / guardrails / tests. Nothing is
applied automatically.

```
hw learn                       # mine history → emit review-only proposals (JSON + founder markdown)
hw propose                     # list the proposal pack + each one's status
hw propose --id <proposal>     # open a disk-file human-review gate (BLOCKS until a human decides)
#   a human approves:  touch .hw/proposals/gates/<proposal>.approved   (or `hw gate-approve`)
hw promote --id <proposal>     # ONLY now becomes a real IMPROVE run — HARD-REFUSES without approval
```

`hw promote` exits non-zero and creates nothing unless a human approval file exists. The engine
never writes that file. This is the Karpathy-style "propose → review → keep/discard" loop turned on
the factory itself.

### Loop B — Observability (dashboard + code anomalies)

Deterministically computes **code signals** from disk (no LLM, explicit thresholds):
`patch_loop_maxed`, `diff_over_budget`, `high_reject_rate`, `high_rollback_rate`,
`repeated_gate_fail`, `escalation_spike`.

```
hw dashboard      # aggregate view: runs, accept/reject counts, anomalies (JSON + a text view)
hw anomalies      # detect anomalies + route HIGH-severity alerts to the escalation sink (idempotent)
```

A lightweight **codebase-memory index** (`.hw/observability/memory.json`) records prior runs'
outcomes so the analyzer and dashboard can reference them — a disk index, not a vector store.

### Loop C — Runtime-neutral escalation sink

Replaces the old Linear-coupled escalator. Writes to the run timeline + a per-run escalations
ledger + a founder-facing notification file. No Linear, no network.

```
hw escalations    # list escalations; points at .hw/observability/notifications.md (your inbox)
```

---

## 2. Recommended operating routines (the cadence)

| When | Run | Why |
|---|---|---|
| Friday morning (review) | `hw learn` → `hw propose` to surface proposals | The factory proposes; you decide |
| During Friday (approve) | `touch .hw/proposals/gates/<id>.approved` for the ones you want | The single human gate — only you approve |
| Friday night (implement) | `hw promote` the approved ones → build via the IMPROVE pipeline → open a PR | The real work, using leftover weekly tokens, merge left to you |
| Anytime | `hw dashboard` + `hw anomalies` + `hw escalations` | Trends, problems, and your inbox |

**Automated two-step weekly routine (added 2026-06-28).** Two local Claude Code scheduled tasks
implement the cadence above around the weekly token reset (Saturday 01:00 Madrid):

1. **`hyperwilfred-weekly-improvement` — Friday MORNING (~09:16 Madrid).** Runs `hw learn` +
   `hw dashboard` + `hw anomalies`, opens a review gate per new proposal, and notifies the founder
   with the exact one-line `touch …approved` command per proposal. It **never approves or
   implements** — it only surfaces the work for the day's review.
2. **`hyperwilfred-weekly-implement` — Friday NIGHT (~22:07 Madrid, ~3h before the reset).**
   Promotes ONLY the proposals the founder approved that day (`hw promote` refuses the rest), builds
   each via the IMPROVE pipeline, and **opens a DRAFT PR — it never merges to `main`.** The final
   merge stays with the founder; most weeks (no approvals) it is a cheap no-op. The night build of
   the factory editing its own code is the dogfood path — the engine's write-scope guardrails,
   deterministic gates, rollback-on-escape, and the no-auto-merge rule bound the blast radius.

Both run while the Claude app is open (or on next launch if it was closed at fire time), so leave
the app open Friday night for the implement step to fire before the reset. To inspect / change
them: Claude Code scheduled tasks under `~/.claude/scheduled-tasks/hyperwilfred-weekly-*/`.

---

## 3. Future hardening (beyond the original 5-phase plan)

Ordered by value/effort.

1. **Automated routine with a human in the loop** *(done — see §2; the two-step Friday
   morning-review / night-implement tasks, with the founder's approval as the only gate and no
   auto-merge).*
2. **End-to-end dogfood** — actually run a *promoted* self-improvement to completion on the existing
   IMPROVE pipeline. Today `hw promote` stands up the IMPROVE run (correctly gated) but executing it
   to completion needs an adapter session. This would be the first time the factory improves itself
   for real.
3. **Calibrate thresholds against real data** — the anomaly thresholds (reject rate 0.5, 2 gate
   failures, min-sample 3, …) are first estimates; tune them once a real `.hw/` corpus exists.
4. **Trend (baseline) anomalies** — current anomalies are per-run absolute thresholds; add a
   moving-baseline (z-score) signal to catch a metric that is individually under threshold but
   trending worse (the legacy original's shape).
5. **Staged canary for self-improvement runs** — even with human approval, run a change to the
   factory's own rules first in a shadow worktree and diff-review it before the ship gate. A second
   safety layer for the most sensitive run type.
6. **Proposal expiry / snooze** — let a human snooze a recurring proposal (don't re-surface for N
   days) so a deliberately-accepted trade-off doesn't keep regenerating the same proposal.
7. **Live Codex parity for the Phase-5 CLI** — the commands are runtime-neutral (pure `hw` CLI), so
   they already work on Codex; a short live validation would close the loop (gated behind the still
   -pending Phase-0 Codex spike noted in the Phase-4 report).

---

## Invariants that constrain ALL of the above

Engine LLM-free + spawn-free · zero `common_py.factory` imports · deterministic gates hard-block ·
patch-never-regenerate · PreToolUse hook fail-closed · HITL gates never auto-approve · durable
resume + Codex adapter contract unbroken · **and, above all, no proposal is ever applied to the
factory without an explicit human approval file.**
