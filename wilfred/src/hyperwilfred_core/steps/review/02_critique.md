# REVIEW · Run the read-only critic panel over the target

This step runs the SAME adversarial critic panel BUILD/IMPROVE use — but over the declared
review target instead of a build's changed code. It is read-only: the critics emit findings, they
NEVER edit code, and nothing is written to the reviewed repo (only findings under `.hw/`).

## Process
1. `hw critique-plan --run-id <id>` → returns one read-only critic packet per lens
   (correctness / security / maintainability / tests, plus any opt-in lens the run requested with
   `hw review --lens`). The packets' `changed_paths` are the resolved review target (from
   `review-target.json`), NOT a build's changed paths.
2. Process packets **sequentially**. For each packet, run
   `hw compile-guardrails --run-id <id> --packet-id <pid>` and
   `hw guardrails-install --run-id <id> --packet-id <pid>`.
3. In `try`, spawn one critic with runtime `allowed-tools` exactly
   `Read,Grep,Glob,Write`. Give it only `lens`, `instruction`, `product_dir`, `changed_paths`, and
   `result_path`. Write is solely for the exact result JSON; Bash/Edit/MultiEdit/NotebookEdit and
   every other tool are unavailable.
4. In `finally`, run `hw guardrails-remove --run-id <id>`, even after failure. Then ingest with
   `hw record-critique --run-id <id> --packet-id <pid>
   --result-file <path>`. The engine validates the findings schema (there is NO acceptance gate —
   the critic wrote no code). Repeat compile → install → spawn → remove → record for each lens.
5. When every lens is recorded: `hw complete-step --run-id <id> --step critique`. The engine
   refuses to complete this step until exactly one valid report exists for each lens of the run
   (the four standard lenses plus any requested opt-in lens), so a partial critique cannot be
   silently completed.

## Opt-in lenses
- `hw review --lens cross-slice-drift` — mismatches between slices (units, state assumed to exist,
  fallbacks, renamed fields). Critics read ONLY the target files, so a `--target-kind diff` run
  cannot follow a value into unchanged files. Run it as `--target-kind paths` over the modules of
  the flow, with the range in the objective, e.g.
  `hw review --target-kind paths --path backend/src/orders --path backend/src/partner
  --lens cross-slice-drift --objective "pre-release drift audit since <previous tag>"`. Watch the
  200-file cap: `hw review` warns when the target is truncated or is a diff.
- `hw review --lens ai-failure-modes` — secrets reaching the client, authorization trusting a
  client value, untrusted input in LLM instructions, model output trusted as code or decision.
- Each opt-in lens is one more sequential critic (more time and model tokens). Neither is ever
  switched on automatically.

## Who wrote each critique
Record each critic result through the independent-review boundary (`--author independent` with
its signed attestation). A result the orchestrating session wrote itself — including after a
critic failed or hit a rate limit — is recorded as `adapter` (self-review). Never relabel it: the
report shows who wrote each lens and a self-reviewed panel can never get the verdict `pass`.

## Hard rules
- **Read-only.** The hook limits reads to the packet's `changed_paths` and denies secret reads,
  product/run-state/sibling/unmatched/symlink-escaped writes; the sole permitted write is the
  current packet's exact `result_path`.
- **Default to skepticism.** A finding you are unsure about is `minor`; reserve `blocker` for a
  real defect you can point at in the code. Do not invent issues to look productive.
