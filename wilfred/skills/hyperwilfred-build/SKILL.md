---
name: wilfred-build
description: >
  Drive a Wilfred BUILD or IMPROVE run — turn one founder objective into a
  built (or improved) product that passes the deterministic gates and the quality loop, with the
  repo protected from out-of-scope writes by the engine guardrail. This skill is the THIN Claude
  Code adapter (L2a): it owns no orchestration logic. Every decision comes from Wilfred;
  the skill only reads fresh step instructions, executes them, spawns parallel sub-agents for
  fan-out/critique steps, and drives the engine-gated quality loop.
  Trigger: "build X with Wilfred", "improve X with Wilfred", "wilfred build", "hw improve".
---

# Wilfred — Claude Code adapter (BUILD)

You are the **adapter**, not the brain. The brain is the `hw` engine (the
`wilfred` CLI). It computes what happens next and persists all state on disk under
`.hw/<run_id>/`. You call it, read what it returns, do exactly that, and call it again. You
never bypass its scope or safety gates. The user's acceptance criteria remain the completion
contract: an engine `done` signal or iteration limit does not establish product correctness.

Resolve instructions and resources from the installed Wilfred package or this product checkout;
never depend on an absolute path to a historical source repository. Run commands from the target
repository root (or pass `--repo <repo-root>` explicitly).
The containing `hyperwilfred-build` directory keeps its historical name only for adapter-path
compatibility; it is not the product name.

## The engine surface

```
hw plan          create a run from an objective (intake + tier; NO decomposition)
                 --mode build|improve   --stack python|node
hw next-step     return the next step packet + its fresh instruction markdown (resume-safe)
hw complete-step mark a single/fanout step done (engine validates the declared artifact)
hw fanout-plan   emit N self-contained sub-agent packets in dependency-safe waves
hw record-result THE ACCEPTANCE GATE — accept, or hard-reject + git rollback, one worktree
hw evaluate      run deterministic gates (per-stack chain) — HARD-BLOCK on failure
                 --step evaluate (BUILD) | regression (IMPROVE)   --stack-selected chain
hw rollback      roll a worktree back to a byte-clean HEAD
hw plan-lint     lint plan.json (cut by responsibility; blocks at high/critical risk) — also run
                 automatically by `complete-step` on the decompose/locate step
hw trace-check   requirement traceability: shaping/requirement IDs vs the contract (exit 1=MISSING)
hw verify-override  HUMAN-only: accept a live-demo finding (recorded, shown in verify output)
hw record-usage  record tokens (main + sub-agents) from the session transcript → usage.json
hw cost-report   wall-clock (from events.jsonl) + recorded tokens for one run
```
`hw complete-step --step verify` also runs the deterministic verify checks (traceability,
reproducibility of declared `generated_artifacts`, the `live_demo` check); a blocking result
refuses completion. `hw next-step` attaches the curated `lessons` that apply to the step.

**Quality loop (Phase 2 — the `quality` step, kind `quality`):**
```
hw quality-status   report the loop phase + the next action (evaluate | patch | done)
hw record-subjective ingest the 0-5 evaluation (engine recomputes the MIN gate)
hw critique-plan     emit one READ-ONLY critic packet per lens (run them in parallel)
hw record-critique   ingest one critic's findings JSON
hw quality-gate      THE decision: pass | patch | escalate | deterministic_gate_required
                     | pending_independent_review (self-review never passes)
hw quality-override  HUMAN-only: accept non-independent review (--by, --reason; recorded + shown)
hw patch-plan        emit ONE surgical patcher packet (tight budgets) from the open findings
hw record-patch      acceptance gate for the surgical patch (patch-never-regenerate, engine-enforced)
```

**Real-time guardrails + human gates (Phase 3):**
```
hw compile-guardrails  emit the PreToolUse hook + registry + settings (+ Codex sandbox/git-hook)
                       --packet-id <id>  scope the hook to one worker (else the whole wave)
hw gate-open           open a disk-file HITL gate for an IRREVERSIBLE action — BLOCKS the run
                       --kind deploy|merge|deps|secrets|migration  --step <id>  [--path ...]
hw gate-status         verify signed human decisions against current action/evidence (exit 1=blocking)
hw gate-approve        submit an externally signed decision via --token-file; cannot mint permission
hw classify-paths      classify paths into sensitive categories + a recommended gate kind
```

**The self-improving autonomy loop + observability (Phase 5 — ALL review-gated):**
```
hw learn         mine .hw/*/ run history → emit review-only proposals (each an IMPROVE objective)
hw propose       list the proposal pack; with --id, open its disk-file human-review gate (BLOCKS)
hw promote       promote an APPROVED proposal to an IMPROVE run — REFUSES without a human approval
                 file (the factory NEVER auto-applies a change to its own rules)
hw dashboard     compute the observability dashboard (JSON + a text view) from disk
hw anomalies     detect code anomalies (patch-loop-maxed, diff-over-budget, gate-fail, …) + route
                 high-severity alerts to the escalation sink
hw escalations   list escalations from the runtime-neutral sink (a founder-facing notification file)
```
The autonomy loop is **operator-driven, not part of the per-build adapter loop**: a human runs
`hw learn`, reviews the pack, opens a proposal's gate with `hw propose --id <pid>`, **a human
approves** its challenge using an external trusted signer, then
`hw promote --id <pid>` runs it as a normal IMPROVE run (human-gated at ship like any other). The
engine never approves a proposal itself. Bare `.approved` markers and self-asserted names do not
authorize anything; the shared verifier requires a valid signed, expiring, one-time decision.

Every command prints one JSON object to stdout. Run them with `--repo <repo-root>` (or from
the repo root). `wilfred` is the primary command; `hw` remains a supported compatibility alias.

## The loop

### 0. Start a run
```
hw plan --objective "<the founder's one line>" --run-id <id> [--acceptance "..."] [--risk medium]
```
Record the `run_id`. The engine wrote `.hw/<run_id>/state.json`, `guardrails.json`, and the
step graph.

### 1. Ask the engine what's next — every turn
```
hw next-step --run-id <id>
```
The JSON gives you:
- `step.id`, `step.kind` (`single` | `fanout` | `gate`),
- `instruction` — the step's markdown, loaded FRESH. **Read it now and follow it exactly.**
- `produces_path` — where a `single` step must write its artifact,
- `objective`, `product_dir`, `available_inputs`, `guardrails_path`.

If it returns `{"done": true}`, coordination has finished. Verify the user's observable acceptance
criteria independently before declaring the product complete. If `{"blocked": true}` a gate
failed — fix the product and re-run `hw evaluate`.

### 2. Execute by `kind`

- **`single`** — Do what the instruction says, write the artifact at `produces_path`, then:
  ```
  hw complete-step --run-id <id> --step <step.id>
  ```
  The engine validates the artifact exists (and, for `plan.json`, that it matches the schema)
  before advancing. A half-done step is re-run, never skipped.

- **`fanout`** (the `implement` step) — Do NOT write code yourself:
  1. `hw fanout-plan --run-id <id>` → returns `waves` (lists of packets).
  2. **Compile + install the real-time write-time guardrail (Phase 3):**
     `hw compile-guardrails --run-id <id>` emits a PreToolUse hook + a worktree→policy
     `registry.json` + a `settings.json` fragment under `.hw/<id>/hooks/`. Merge that fragment
     into `.claude/settings.local.json` for the run so the hook fires before every
     Write/Edit/MultiEdit/NotebookEdit and **blocks an out-of-scope write before it lands**
     (fail-closed: a missing/malformed registry denies). The hook resolves which packet a write
     belongs to by the worktree that contains the target path, so one install covers a whole
     parallel wave. **Remove the fragment when the run ends — the hook is run-scoped.** The hook
     is defense-in-depth; `hw record-result` stays the authoritative reject either way.
  3. For each wave **in order**, spawn one `Task` sub-agent per packet, **all packets in a
     wave in a single message so they run in parallel**. Give each sub-agent ONLY its packet:
     its `instruction`, `title`, `allowed_globs`, `blocked_globs`, budgets, `worktree_hint`,
     and `result_path`. Create a git worktree per packet (`git worktree add --detach
     <worktree_hint> HEAD`) and run the sub-agent with that as its working directory + a
     tool-lock to its scope.
  4. The sub-agent writes ONLY within `allowed_globs`, stays under the budgets, and writes its
     result JSON to `result_path` (a disk path — never rely on its stdout).
  5. Run the acceptance gate per packet:
     ```
     hw record-result --run-id <id> --packet-id <pid> --worktree <worktree>
     ```
     Exit 0 = accepted. Exit 1 = rejected (escape or full-file rewrite); the engine already
     rolled the worktree back clean — re-dispatch the SAME packet with a tightened
     "surgical, in-scope only" instruction. Never hand-fix and force it through.
  6. Finish a wave before starting the next. When all packets are accepted:
     ```
     hw complete-step --run-id <id> --step implement
     ```

- **`gate`** — Run the engine gate and respect a hard block:
  - `evaluate` (BUILD) / `regression` (IMPROVE): `hw evaluate --run-id <id> --step <step.id>
    --target <product_dir>`. Exit 1 = a deterministic gate failed — fix and re-run; do NOT
    advance. (The chain is selected by the run's stack — python or node.)

- **`human_gate`** (the `ship` step) — The irreversible action is human-gated (Phase 3):
  1. `hw gate-open --run-id <id> --kind merge --step ship --action "..." [--path ...]`
     (omit `--kind` to let the engine classify `--path` via protected-paths). The engine writes
     a `.pending` gate and BLOCKS the run.
  2. **STOP and notify the founder** with the gate id + evidence. Do NOT commit to main, open a
     PR, or deploy. The engine NEVER self-approves.
  3. A trusted external signer signs the current challenge; submit the token with
     `hw gate-approve --run-id <id> --gate-id <gate_id> --decision approve --token-file SIGNED.json`.
     Never generate a production signing key or claim a test signature is human authorization.
  4. `hw gate-status --run-id <id>` (exit 1 = still blocking, exit 0 = decided). On approval the
     engine marks `ship` done and the run completes.

- **`quality`** (the `quality` step) — Drive the engine-gated quality loop. Read the step's
  fresh instruction (`quality/00_loop.md`) and follow it: each round, run the deterministic gate
  (hard wall), spawn the 0-5 evaluator and the read-only critics, route their results through
  `hw record-subjective` / `hw record-critique`, then ask `hw quality-gate`. On `patch`, run
  `hw patch-plan`, then `hw compile-guardrails --run-id <id> --packet-id patch-<n>` to scope the
  PreToolUse hook to the patch worker, dispatch ONE surgical patcher (tool-locked to Read+Edit)
  in a worktree, then `hw record-patch`. Only `pass` marks the quality step done; unresolved major
  findings never become a passing fallback. On `escalate`, retain evidence and explain the remaining
  issue. Continue reversible corrections already authorized by the user within the valid scope;
  request direction only for a real external blocker or a necessary expansion of authority.
  **Label provenance honestly:** critiques/evaluations written by a dispatched sub-agent carry
  `--author independent` at record time; anything you wrote yourself is `adapter` (the default —
  an `author` inside the JSON is ignored, and files edited after recording lose independence).
  A final iteration with adapter-authored or missing lenses returns `pending_independent_review`,
  never `pass` — when limits (e.g. HTTP 429) stop the critics, STOP, report, and re-dispatch them
  after the reset. Never relabel self-review as independent; only a human may `hw quality-override`.

### 3. Repeat from step 1 until `hw next-step` returns `done`.

## Modes
- **BUILD** (greenfield): `hw plan --objective "..." --mode build` → frame → decompose →
  scaffold → implement → evaluate → **quality** → verify → ship.
- **IMPROVE** (existing repo): `hw plan --objective "..." --mode improve` → comprehend → locate
  → implement → **regression** (existing tests must still pass) → **quality** → verify → ship.
  Same evaluator + gate + patcher core as BUILD; only the front of the pipeline differs.

## Hard rules (non-negotiable)
- **Enforce the authorized scope.** Never accept a worker's output without `hw record-result`.
  Engine acceptance is necessary but not sufficient: unmet essential user criteria remain incomplete.
- **No code-writing fan-out runs against a real repo without the guardrail.** It ships in the
  engine; you simply must route every result through `hw record-result`.
- **Read each step's instruction fresh** from `hw next-step`. Do not cache or assume it.
- **Deterministic gate failures HARD-BLOCK.** Never advance past a red lint/type/test or regression
  gate. A subjective shortfall or unresolved major also prevents quality acceptance; exhausted
  iterations are an incomplete result, not a product-completion fallback.
- **Patch-never-regenerate.** The patcher makes surgical edits only; the engine rejects and rolls
  back any full-file rewrite at `hw record-patch`. Re-dispatch a tightened packet, never hand-fix.
- **Irreversible actions are human-gated.** You stop at `ship` (and any deploy/merge/deps/
  secrets/migration gate); you never deploy or merge. The gate hard-blocks until a HUMAN creates
  signed decision through the external trusted signer; you cannot replace it with an assertion.
- **Install the write-time hook before any code-writing fan-out.** `hw compile-guardrails` emits
  it; merge the settings fragment for the run and remove it after. It blocks an out-of-scope or
  blocked-path write before it lands (fail-closed), on top of the engine's authoritative reject.
