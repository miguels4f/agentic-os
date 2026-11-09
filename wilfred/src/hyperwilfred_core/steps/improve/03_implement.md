# IMPROVE · Step 03 · Apply the scoped diff in parallel (FAN-OUT)

Identical mechanics to the BUILD `implement` step — the engine reuses the same fan-out and the
same acceptance guardrail — but the work is **editing existing code**, so the surgical
discipline matters more. You (the adapter) do NOT write code here; you dispatch one isolated
sub-agent per packet and route every result through the engine's acceptance gate.

## Adapter procedure
1. Ask the engine for the packets:
   ```
   hw fanout-plan --run-id <RUN_ID> --step implement
   ```
   Returns `waves` (dependency-safe, scope-disjoint). Waves run in order; packets within a wave
   run in parallel.

2. **For each wave, in order**, dispatch every packet as a parallel sub-agent, each with its OWN
   git worktree (a checkout from HEAD) and a tool-lock to its scope. Give each worker ONLY its
   packet. The worker:
   - Makes the **smallest** change that satisfies the slice — this is an edit to existing code,
     not a greenfield write. Prefer Edit over rewrite.
   - Writes **only** within `packet.allowed_globs`; never touches `packet.blocked_globs`.
   - Stays under `packet.max_files`, `packet.max_changed_lines`, `packet.max_hunk_span`.
   - Writes a short JSON result to `packet.result_path` (a disk path — never stdout).

3. Run the engine acceptance gate per packet:
   ```
   hw record-result --run-id <RUN_ID> --packet-id <PACKET_ID> --worktree <WORKTREE_PATH>
   ```
   Exit 0 = accepted. Exit 1 = rejected (scope escape or full-file rewrite); the engine already
   rolled the worktree back byte-clean — re-dispatch the SAME packet with a tightened
   "surgical, in-scope only" instruction.
   A worker that crashed, timed out or wrote no result file counts as a rejection. After **3
   rejections of the same packet**, stop re-dispatching it and escalate to the owner (the founder)
   with the packet id and the three rejection reasons; never loosen scope or hand-fix it.
   Remove each worker's worktree after acceptance **and** after a rejection, crash, timeout or
   abort, so no orphan checkout is left behind. (agency-agents wiring 2026-10-23, owner OK; method: specialized-workflow-architect)

4. Finish a wave before starting the next. When every packet is accepted:
   ```
   hw complete-step --run-id <RUN_ID> --step implement
   ```

The next step (`regression`) proves the existing tests still pass after these changes.
