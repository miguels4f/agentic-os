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

Provenance is granted only by a **two-call, externally signed protocol**; an `author` field inside
JSON is ignored. First record the result with `--author independent`. The engine deliberately
stores it as `adapter` and returns a challenge and decision path. A trusted external reviewer,
whose private key is outside the repository and unavailable to workers, reviews and signs that
challenge. Place the token at the decision path, then repeat the same command with both
`--author independent` and `--attestation-file <decision-path>`. The signature binds the exact
run, iteration, packet, lens and persisted-result SHA-256. Editing the result revokes provenance.
The trusted public key must exist at
`~/Library/Application Support/Wilfred/review-attestation-public-key.pem`.

Every evaluation and critique record carries `author`:
- `independent` — the exact persisted result completed the external signed two-call protocol.
- `adapter` — any result without that completed attestation, including one from a dispatched
  sub-agent. Separation produces a review report; it does not by itself prove independence.

Label honestly. The engine treats a missing `author` as `adapter` and will NOT pass the loop on
adapter-authored review: `hw quality-gate` returns `action: "pending_independent_review"`
(`status: PENDING_INDEPENDENT_REVIEW`) whenever the subjective evaluation or any required critic
lens of the final iteration is adapter-authored or missing. That is not a failure to hide — it is
the engine telling you the review has not really happened yet.

If no trusted external signer or public key exists, keep the first-call records as `adapter`, STOP
at `pending_independent_review`, and report the missing external action. Never use a fixture key or
create a signer during the run. Only a human may accept the risk instead:
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
      hw record-subjective --run-id <RUN_ID> --result-file <subjective_result_path> --author independent
      # After the external signer writes the returned decision path:
      hw record-subjective --run-id <RUN_ID> --result-file <subjective_result_path> --author independent --attestation-file <signed-token>
      ```
      The engine recomputes `global_score = MIN(criteria)` — it ignores any aggregate the
      evaluator self-reports.
   3. **Adversarial critics.** Plan + spawn them:
      ```
      hw critique-plan --run-id <RUN_ID>
      ```
      Spawn one **read-only** critic sub-agent per returned packet (all in parallel), each with
      its packet's `instruction` + `lens` + `changed_paths`. Each writes a `CritiqueReport` JSON
      to its `result_path`; use the two-call external attestation protocol above (an `author` field
      inside the JSON and the first `--author independent` call grant no provenance). The
      **adversarial** lens (present for objectives touching personal data, gates, approvals or
      security) must be allowed to run commands in temporary roots, not just read. For each,
      feed it back:
      ```
      hw record-critique --run-id <RUN_ID> --packet-id <PACKET_ID> --result-file <result_path> --author independent
      # After the external signer writes the returned decision path:
      hw record-critique --run-id <RUN_ID> --packet-id <PACKET_ID> --result-file <result_path> --author independent --attestation-file <signed-token>
      ```
   4. **Ask the engine to decide:**
      ```
      hw quality-gate --run-id <RUN_ID>
      ```

3. **Act on the `quality-gate` `action`:**
   - `pass` → the engine marked the quality step done. Go back to `hw next-step`.
   - `pending_independent_review` → the signed protocol is incomplete. Obtain external signatures
     for every exact result and make the second record calls. If no
     trusted signer exists, retain the explicit pending state. Never relabel your own writing.
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
