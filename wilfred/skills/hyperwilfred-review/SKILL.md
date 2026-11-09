---
name: wilfred-review
description: >
  Drive a Wilfred REVIEW run — a standalone READ-ONLY audit of an existing
  repo, a path subset, or a git diff range. It runs the SAME adversarial critic panel BUILD/IMPROVE
  use, but over an arbitrary target, and synthesizes a structured findings report by severity with
  file:line refs. It builds NOTHING, patches NOTHING, ships NOTHING, and writes NOTHING to the
  reviewed repo (only findings under .hw/). This skill is the THIN Claude Code adapter: every
  decision comes from the `hw` engine; the skill only reads fresh step instructions, spawns the
  read-only critics in parallel, and routes their findings through the engine.
  Trigger: "review X with Wilfred", "wilfred review", "hw review", "audit this repo with Wilfred".
---

# Wilfred — Claude Code adapter (REVIEW)

You are the **adapter**, not the brain. The `hw` engine computes what happens next and persists
all state under `.hw/<run_id>/`. REVIEW is a read-only audit — you never write to the reviewed
repo, never patch, never ship. You spawn the critics, route their findings, and synthesize the
report. This mirrors the BUILD adapter: a thin router that loads each step fresh.

Resolve instructions and resources from the installed Wilfred package or this product checkout;
never depend on an absolute path to a historical source repository. Run commands from the target
repository root (or pass `--repo <repo-root>` explicitly). `wilfred` is the primary command;
`hw` remains a supported compatibility alias for existing workflows and examples below.
The containing `hyperwilfred-review` directory keeps its historical name only for adapter-path
compatibility; it is not the product name.

## Honest positioning

REVIEW is **not** trying to beat Claude Code's built-in `/code-review` on raw review quality —
that is a mature tool. REVIEW's value is being **portable** (the same engine runs on Claude Code
and Codex), **customizable** (you own the lenses — e.g. add a domain compliance lens later),
**engine-gated** (findings land on disk and can feed the dashboard + learning loop), and
**continuous** (the SAME findings can drive an IMPROVE run). It is a v1 reviewer that improves via
the loop. For a one-off review on Claude Code where none of that matters, `/code-review` is fine.

## The engine surface

```
hw review          create a REVIEW run over a target (resolves the target deterministically)
                   --target-kind paths|diff   --root <dir>   --path <glob> (repeatable)
                   --diff-range <range>   --objective "<what to look for>"   --max-files N
hw next-step       return the next step packet + its fresh instruction (resume-safe)
hw complete-step   mark comprehend/report done (validates the declared artifact)
hw critique-plan   emit one READ-ONLY critic packet per lens over the REVIEW target (REUSED)
hw record-critique ingest one critic's findings JSON (REUSED — schema-validated, no acceptance gate)
hw review-report   synthesize the recorded findings into the ReviewReport (JSON + markdown)
```

`hw review` over a path subset / whole repo / git diff range. The engine resolves the target into
`review-target.json` and the reused `hw critique-plan` reviews exactly those files.

## The loop

### 0. Start the review
```
hw review --run-id <id> --target-kind paths --path src/ --objective "find correctness + security issues"
# or a whole repo:        hw review --run-id <id>
# or a git diff range:    hw review --run-id <id> --target-kind diff --diff-range main...HEAD
# opt-in lenses (repeatable, never automatic), on top of the four standard ones:
#   hw review --run-id <id> --target-kind paths --path backend/src/orders --path backend/src/partner \
#     --lens cross-slice-drift --objective "pre-release drift audit since <previous tag>"
#   hw review --run-id <id> --path src/ --lens ai-failure-modes
```
Critics read ONLY the target files, so run `cross-slice-drift` with `--target-kind paths` over the
modules of the flow (a diff target cannot see values in unchanged files; `hw review` warns).
Record the `run_id`. The engine wrote `state.json`, `guardrails.json`, the step graph, and
`review-target.json` (the resolved file list).

### 1. Ask the engine what's next — every turn
```
hw next-step --run-id <id>
```
Read the returned `instruction` FRESH and follow it. `{"done": true}` ⇒ the review is complete.

### 2. Execute by `kind`
- **`single` · comprehend** — Read `review-target.json`, skim the target files (read-only),
  write a short comprehension note to `produces_path`, then
  `hw complete-step --run-id <id> --step comprehend`.
- **`review` · critique** — Run the read-only critic panel:
  1. `hw critique-plan --run-id <id>` → one packet per lens; each packet's `changed_paths` is the
     REVIEW target.
  2. Run critics **sequentially**. For each packet, first run
     `hw compile-guardrails --run-id <id> --packet-id <pid>`, then
     `hw guardrails-install --run-id <id> --packet-id <pid>`.
  3. In a `try`, spawn exactly one critic with runtime `allowed-tools` set to exactly
     `Read,Grep,Glob,Write` — no Bash, Edit, MultiEdit, NotebookEdit, or any other tool. Give it
     only (`lens`, `instruction`, `product_dir`, `changed_paths`, `result_path`). `Write` exists
     solely so it can emit its `CritiqueReport` JSON to the exact `result_path`; the installed
     hook limits reads to `changed_paths` and denies product edits, sibling artifacts, run-state
     writes, secret reads, and unmatched paths.
  4. In `finally`, always run `hw guardrails-remove --run-id <id>`, including when spawn fails.
     The engine restores `.claude/settings.local.json` byte-for-byte, or removes it if absent
     before install. Never hand-edit or manually merge that file.
  5. After cleanup, run `hw record-critique --run-id <id> --packet-id <pid>
     --result-file <path>`, then repeat compile → install → spawn → remove → record.
  6. `hw complete-step --run-id <id> --step critique` (the engine requires exactly one report
     per lens of the run: the four standard lenses plus any `--lens` requested).
  Label provenance honestly on every record: `author: independent` only for a critic you actually
  dispatched and whose result you read from disk; anything you wrote yourself is `author: adapter`
  (a missing label counts as adapter). If limits (HTTP 429) stop a critic, report that lens as
  not independently reviewed and re-dispatch it after the reset — do not fill it in yourself.
- **`single` · report** — `hw review-report --run-id <id>` (the engine synthesizes the report
  deterministically), then `hw complete-step --run-id <id> --step report`. Hand the founder the
  verdict + `.hw/<id>/review-report.md`. The report lists who wrote each critique; if any lens is
  not independent (adapter-authored or its attestation does not verify), `pass`/`advisory`
  becomes `pending_independent_review` — say so first, in plain words.

### 3. Repeat until `hw next-step` returns `done`.

## Hard rules (non-negotiable)
- **Read-only.** REVIEW writes NOTHING to the reviewed repo. The critics never edit code; if a
  sub-agent would write product code, the hook denies it. A critic's sole write permission is its
  exact `result_path` under `.hw/`; compile and install that policy before dispatch, then remove
  the temporary hook in `finally` after every critic.
- **No ship, no patch, no deploy.** REVIEW ends at the report. Acting on findings is a separate,
  explicit decision (e.g. an IMPROVE run — `hw plan --mode improve` over the same target with the
  findings as acceptance criteria).
- **The engine owns the target and the synthesis.** Don't hand-pick files or hand-write the
  report; the engine resolved the target and synthesizes the report deterministically.
- **Read each step's instruction fresh** from `hw next-step`. Do not cache or assume it.
