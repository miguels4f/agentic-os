# IMPROVE · Step 04 · Regression guard (existing tests must still pass)

This is the IMPROVE-specific deterministic hard wall and the whole point of the IMPROVE path:
**prove the change did not break the existing codebase.** It runs the same deterministic gate
chain as BUILD's `evaluate`, pointed at the *existing* repo's test suite. For **Python**, it also
compares against the green baseline snapshotted at `comprehend`: the same tests must still be
collected and still pass. A missing Python baseline blocks the gate. For **Node**, it runs the
native `npm run build`, `npm run lint` (when defined), and `npm run test` checks without a pytest
baseline. Node per-test deletion/skip and test-file integrity checks are not yet supported.

## Adapter procedure
Run the deterministic gate over the existing repo/target:
```
hw evaluate --run-id <RUN_ID> --step regression --target <product_dir>
```

- Exit 0 → the stack's native gate chain passes; for Python, every baseline test is also still
  collected and passing with its integrity checks satisfied. Continue to `hw next-step`.
- Exit 1 → **HARD BLOCK.** Either a check is now red, or the baseline comparison failed (a
  pre-existing test now fails, was deleted, was skipped/disabled, or was renamed out of
  discovery). The run status becomes `gate_failed`. Do NOT advance and do NOT "accept with best
  effort." Fix the regression (the quality loop's patch round, or a human) and re-run until green.

## The flaky-test gate (after the tests)
`flaky-tests` re-runs every test file this run added or changed since the run's base commit
5 times, in shuffled order, every other run under CPU load (CI runners are slow machines). A
test that passes in some runs and fails in others is non-deterministic and HARD-BLOCKS: fix the
cause (a race, an ordering dependency, shared state, randomness, a chance match) — never add a
retry, a sleep that merely widens the window, or a skip. A test that fails in every run is left
to the test gate and shown as a warning. It needs the run's base commit and blocks when git
cannot list the changed tests or when its time budget runs out.

## The CI-parity gate (last in every chain)
After lint/type/test the chain runs `ci-parity`: the target repository's own **pull-request CI**
(`.github/workflows/*.yml`), executed locally — every `run:` step of every job a
`pull_request` triggers, with its `working-directory`, `env` and `matrix` values — plus a check
that every path a workflow names (`working-directory`, `cache-dependency-path`) exists. A step
that would fail on the pull request fails here and HARD-BLOCKS like a red test. Fix the product
(or the workflow, if the workflow itself is wrong) — never skip the step.

It reports as a `warning`, never a pass-by-omission, whatever it could not reproduce here: a job
on another OS, a job with `services:`/`container:`, a step that needs `secrets`, a non-setup
`uses:` action. Tell the human which parts were NOT verified locally when you report the gate.
Push/deploy workflows are only path-checked, never executed. The workflows that run are the
ones committed at the run's base commit: editing or deleting `.github/workflows` during the run
does not change what is checked. Each job runs in its own fresh clone with no remote (your
worktree is never touched), an allow-listed environment and a throw-away HOME. If no
pull-request job can run on this machine (e.g. CI is `ubuntu-latest` and this is a Mac) the
gate BLOCKS: tell the human.

## Switching these gates off (humans only)
No environment variable turns either gate off. A human may commit `.hw-gates.json` at the repo
root **before `hw plan`** — `{"version": 1, "gates": {"ci-parity": {"mode": "allow-unverified",
"reason": "..."}}}` (`off` is also accepted, and is the only mode for `flaky-tests`). The engine
reads it as committed at the run's base commit, so you CANNOT add it during the run. If a gate
blocks for a reason only the human can lift, STOP and tell them.

### Python environment-only failures a human declared before the run
Some repos have tests that fail only because of the machine (e.g. they read a file that exists
on one laptop). A human may list them in `.hw-known-failures.json` at the repo root, **committed
before `hw plan`** — each entry a repo-relative node id / `file.py` / `dir/` plus a `reason`:
```json
{"version": 1, "entries": [
  {"id": "tests/test_live.py::test_reads_local_export", "reason": "needs ~/exports on the owner's Mac"}
]}
```
The engine reads it as committed at the run's base commit, so you CANNOT add to it during the run
(and a worker packet can never write it). If every failing test is declared, pre-existing,
untouched since the base commit and was not passing at the baseline, the pytest check becomes a
visible `warning` and the gate passes; any other red test still HARD-BLOCKS. If the gate blocks
on an environment-only failure that is not declared, STOP and tell the human — never edit
`state.json`, `events.jsonl` or `verification.json` to get past it.

This gate is the deterministic step the quality loop treats as its hard wall for IMPROVE: after
any surgical patch, the engine forces this gate to be re-run before the loop can pass, so a patch
that breaks a pre-existing test cannot slip through. Python also blocks quietly removing a
baseline test; Node retains the native checks described above.
