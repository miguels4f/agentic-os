# IMPROVE · Step 01 · Comprehend the existing repository

This is the first net-new IMPROVE step. Unlike BUILD (greenfield), IMPROVE operates on an
**existing** codebase. Before changing anything, build an accurate map of what is there so the
later steps change the right place and prove they broke nothing.

## What to do
1. Read the objective (`objective` in the step packet) — what change is being asked for.
2. Survey the repo at `product_dir` (here, the existing repo/target): its structure, the stack,
   how it is built and tested, where the relevant behavior lives.
3. **Establish the baseline for the declared stack.** Existing checks must be GREEN *before*
   any change. For **Python**, snapshot the suite so regression proves the SAME tests still pass:
   ```
   hw regression-baseline --run-id <RUN_ID> --target <product_dir>
   ```
   `<product_dir>` must be the directory the run declared with `hw plan --product-dir` (use
   `.` for the repository root). A run planned without `--product-dir` is refused: the engine
   does not accept a caller-chosen baseline target. The same rule applies to
   `hw regression-baseline-inherit`, which also requires the source run to declare that same
   directory. If a capture is interrupted and the engine names two files to remove, STOP and
   ask a human to remove them — never remove or edit them yourself.
   This records the collected test node ids + passing count (`regression_baseline.json`). The
   regression guard later FAILS if any baseline test is deleted, skipped, or renamed out of
   discovery — not just if one fails — so a change cannot neutralise the very test that protects
   it. If the baseline is already red, say so and STOP — IMPROVE assumes a green starting point.
   The one exception: red tests a human declared as environment-only in `.hw-known-failures.json`
   (committed before `hw plan`; see `04_regression.md`). Name them in `comprehension.md`.
   For **Node**, run `npm run build`, `npm run lint` (when defined), and `npm run test` in
   `product_dir`, and record their results in `comprehension.md`. Stop if any check fails.
   Do not run `hw regression-baseline`: it explicitly rejects non-Python stacks. Node regression
   reruns the native chain; per-test deletion/skip and test-file integrity checks are not yet
   supported for Node.

## Write `comprehension.md`
A concise map for the downstream steps:
- **Stack & tooling:** language, build command, test command, lint/type tooling.
- **Where the change belongs:** the modules/files most relevant to the objective.
- **Baseline:** how tests run and the baseline result (green/red + counts).
- **Risks:** anything that makes the change delicate (shared code, public APIs, migrations).
- **Files inspected:** the files you actually read to build this map (agency-agents wiring 2026-10-23, owner OK; method: specialized-workflow-architect).
- **Blind spots:** relevant areas you did not read (callers, config, generated code, other
  services) and why.
- **Assumptions not verified:** anything you inferred without reading the code or running the
  check that proves it, marked "not verified" so `locate` does not treat it as fact.

Keep it short and factual — it is context for `locate`, not an essay.
