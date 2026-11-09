# IMPROVE · Step 05 · Verify the change works

The deterministic gates are green, the regression guard passed, and the quality loop is done.
Confirm the change actually delivers the objective before handing off to the human ship gate.

## What to do
1. Re-read the objective and its acceptance criteria.
2. Check, against the changed code, that each acceptance criterion is met — not just that tests
   pass, but that the requested behavior is actually present.
3. Where practical, exercise the changed behavior (run the relevant entry point / a focused
   test) and capture the evidence.
4. If any changed artifact makes **state claims** (asserts current repo/system state — "PR #N
   merged", "no X exists", "N tests green"), re-verify each against the repo AS OF NOW — state
   observed earlier in the run may have drifted underneath you. Two changed artifacts asserting
   contradictory states is a not-met criterion: stop and reconcile before the ship gate. See
   `../quality/state-claims-methodology.md`.

5. `hw complete-step --step verify` also runs the deterministic verify checks (requirement
   traceability, reproducibility of declared `generated_artifacts`, the `live_demo` check) and
   refuses to complete while one is blocking — see `../build/06_verify.md` for what each does and
   how a human overrides a live-demo finding.

## Write `verify.md`
- Each acceptance criterion → met / not-met, with the evidence (a test name, a command output,
  a code reference).
- Note whether the quality loop passed outright or fell back to its best subjective version.
- Any residual risk a human should know before shipping.

Then continue to `hw next-step` — the final step is the human `ship` gate. You STOP there; a
human performs any irreversible action (commit to main, PR, deploy).
