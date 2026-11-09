# QUALITY · The quality loop (0-5 evaluator + adversarial critics + surgical patch)

This step is **adapter-driven, engine-gated**. The engine decides pass / iterate / escalate;
you (the adapter) only spawn the evaluator, the critics, and the patcher, and feed
their results back to the engine. The same loop runs for BUILD and IMPROVE — only the steps
*before* this one differ.

**The precedence rule is non-negotiable and the engine enforces it:**
- A failing **deterministic** gate (build/lint/type/test, or the IMPROVE regression gate) is a
  HARD WALL. The loop can never pass while it is red. After every accepted patch the engine
  resets the deterministic gate so you MUST re-run it (a patch can break a test).
- The 0-5 evaluator gates on the **MIN** of its criteria, recomputed by the engine.
- A subjective shortfall or open blocker/major remains incomplete after the budget; the engine
  never claims a historical "best" was restored without selecting those exact bytes.

## Review provenance — no silent pass on self-review

Provenance is a **declaration made when you record a result**, not a field the result file can
claim for itself (an `author` written inside the JSON is ignored). Record a sub-agent's result with
`--author independent`; without the flag it is recorded as `adapter`. The engine stores a hash of
each recorded file, so a record edited, replaced or hand-dropped on disk afterwards is no longer
trusted. This is an audited honesty protocol (the declaration is in the event log), not
cryptographic proof — which is why the human override below needs a signed, expiring decision.

Every evaluation and critique record carries `author`:
- `independent` — produced by a **dispatched sub-agent** whose result you read back from disk.
- `adapter` — written by **you** (the orchestrating session) about work you did yourself.

Label honestly. The engine treats a missing `author` as `adapter` and will NOT pass the loop on
adapter-authored review: `hw quality-gate` returns `action: "pending_independent_review"`
(`status: PENDING_INDEPENDENT_REVIEW`) whenever the subjective evaluation or any required critic
lens of the final iteration is adapter-authored or missing. That is not a failure to hide — it is
the engine telling you the review has not really happened yet.

If sub-agents cannot run (usage limit / HTTP 429): do NOT write the critiques yourself and label
them `independent`. Record what you have (`author: adapter`), STOP at `pending_independent_review`,
report it, and **re-dispatch the critics once limits reset**, then re-record them and re-run
`hw quality-gate`. Only a human may accept the risk instead:
`hw quality-override --run-id <RUN_ID> --by <name> --reason <why>` first creates a challenge. A
trusted human must sign that challenge externally and place the token at the reported path; rerun
the command to verify and consume it. The override is recorded in verify output and the ship gate.

## The loop (repeat until the engine says you are done)

1. **Ask the engine where you are:**
   ```
   hw quality-status --run-id <RUN_ID>
   ```
   It returns `phase` and an `action`.

2. **If `action: "evaluate"`** — run the EVALUATE phase, in this order:
   1. **Deterministic hard wall first.** Run the deterministic gate over the product:
      ```
      hw evaluate --run-id <RUN_ID> --step <det_gate_step> --target <product_dir>
      ```
      (`det_gate_step` is in the status output — `evaluate` for BUILD, `regression` for IMPROVE.)
      Exit 1 = a gate failed → the product is broken; do NOT score it. Treat this exactly like
      an open blocker: go straight to a patch round to fix the build/test, then re-run this gate.
   2. **0-5 subjective evaluation.** Spawn ONE evaluator sub-agent with the
      `subjective_instruction` from the status output. It writes its `SubjectiveEvaluation` JSON
      to `subjective_result_path` (a disk path — never rely on stdout). Then:
      ```
      hw record-subjective --run-id <RUN_ID> --result-file <subjective_result_path>
      ```
      The engine recomputes `global_score = MIN(criteria)` — it ignores any aggregate the
      evaluator self-reports.
   3. **Adversarial critics.** Plan + spawn them:
      ```
      hw critique-plan --run-id <RUN_ID>
      ```
      Spawn one **read-only** critic sub-agent per returned packet (all in parallel), each with
      its packet's `instruction` + `lens` + `changed_paths`. Each writes a `CritiqueReport` JSON
      to its `result_path`; record it with `--author independent` only if a sub-agent wrote it
      (an `author` field inside the JSON is ignored). The
      **adversarial** lens (present for objectives touching personal data, gates, approvals or
      security) must be allowed to run commands in temporary roots, not just read. For each,
      feed it back:
      ```
      hw record-critique --run-id <RUN_ID> --packet-id <PACKET_ID> --result-file <result_path> \
        [--author independent|adapter]
      ```
   4. **Ask the engine to decide:**
      ```
      hw quality-gate --run-id <RUN_ID>
      ```

3. **Act on the `quality-gate` `action`:**
   - `pass` → the engine marked the quality step done. Go back to `hw next-step`.
   - `pending_independent_review` → the review was self-authored or incomplete. Dispatch the
     missing sub-agent critics (re-dispatch after a limit reset), re-record them, re-run
     `hw quality-gate`. Never relabel your own writing as independent.
   - `deterministic_gate_required` → re-run `hw evaluate --step <det_gate_step>` (and patch the
     build/test first if it is red). Then re-run `hw quality-gate`.
   - `patch` → run the PATCH phase (step 4).
   - `escalate` → a blocker, major, or subjective shortfall survived the budget. STOP and hand off
     to a human. Do not
     force the work through.

4. **PATCH phase** (when `action: "patch"`):
   1. ```
      hw patch-plan --run-id <RUN_ID>
      ```
      Returns ONE surgical patcher packet (tight line/file budgets) addressing the open
      blocker/major findings at `findings_path`.
   2. Spawn ONE **surgical patcher** sub-agent, tool-locked to Read + Edit (it must be unable to
      Write a whole file), in a git worktree of the product. Give it the packet's `instruction`,
      `findings_path`, `allowed_globs`, and budgets. It applies ONLY surgical edits and writes a
      short result JSON to `result_path`.
   3. Acceptance gate (patch-never-regenerate, engine-enforced):
      ```
      hw record-patch --run-id <RUN_ID> --packet-id <PACKET_ID> --worktree <WORKTREE>
      ```
      Exit 0 = accepted; the engine bumped the iteration and reset the deterministic gate.
      Exit 1 = rejected (out-of-scope write or full-file rewrite); the engine rolled the worktree
      back byte-clean — re-dispatch the SAME packet with a tightened "surgical, in-scope only"
      instruction. Never hand-fix and force it through.
   4. Loop back to step 1.

## Done when
`hw quality-gate` returns `pass` (the engine marks the quality step done). A
`pending_independent_review` is NOT done. Then
continue with `hw next-step`.
