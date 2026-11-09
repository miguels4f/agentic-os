# Agent Foundry MVP

Agent Foundry turns a supported objective into an executable local agent-system package.
The lifecycle records intake evidence, chooses a task-appropriate topology, validates a design,
generates code, executes scenarios, repairs a bounded source defect and retains versioned evidence.
The host lifecycle is deterministic; generated evidence agents use an explicit model provider.

Start with the [CLI and reproducible demo](cli.md). See [architecture and boundaries](architecture.md) for design decisions, deferred work, and the final PR evidence template.

## Lifecycle

Both guided and autopilot modes use the same engine and the same nine ordered stages. Modes change only how missing information is handled; they do not create separate implementations.

| Stage | Purpose | Advance with |
|---|---|---|
| `frame` | Gather and reconcile the objective, actors, inputs, outputs, decisions, constraints, risk, autonomy, success evidence, and non-goals. | `answer`, then `next` |
| `classify` | Confirm the smallest justified system topology. | `next` |
| `scout` | Record external candidates and fail-closed reuse decisions. | `next` |
| `blueprint` | Produce the canonical specification, blueprint, and contracts. | `next` |
| `contract` | Validate the design and its cross-references. | `validate` |
| `generate` | Build the local Python bundle description. | `generate` |
| `evaluate` | Require normal, edge, adversarial, and recovery evidence. | `evaluate` |
| `repair` | Record one surgical replacement for one failed scenario, then re-evaluate. | `repair` |
| `package` | Package only a fully accepted evaluation. | `package` |

`status` is read-only. `resume` verifies recorded digests before returning a safe continuation.
`state.json` points to the current state; `revisions/<sha256>.json`, content-addressed
`artifacts/<name>-<sha256>.json`, and `generated-<sha256>/` retain prior versions. Publication
uses temporary files, synchronization and atomic replacement. Actual kill-at-half-write tests
verify coherent recovery, not merely restarting a process between completed commands.

## Adaptive intake

Guided mode asks exactly one unresolved, highest-impact question at a time and explains the decision it affects. Contradictions are promoted ahead of ordinary gaps. Autopilot may record reversible, low-impact defaults, but consequential ambiguity remains a blocker and is still surfaced as a question. Explicit answers replace assumptions; likely secret values are rejected rather than persisted.

The framing gate does not advance until required evidence is resolved and the topology is valid. A model call must be justified. Duplicate roles, overlapping ownership, and handoffs without contracts are rejected.

## Outputs

The canonical Agent System Specification (ASS) is `agent-system-specification/v1`. It is the source of truth for the problem, evidence, assumptions and contradictions, decisions, topology, components, tools and permissions, contracts and handoffs, context and memory policies, states and transitions, failure policy, evaluations, reuse decisions, capability gaps, and runtime target. Other schema versions fail validation rather than being guessed or silently upgraded.

Blueprint and contract validation checks ownership and graph references, declared model use, cycles, orphan steps/components, contract schemas, evidence references, least-privilege permissions, handoff compatibility, bounded retry/timeout/fallback/escalation, external-reuse evidence, capability-gap mitigations, and supported schema versions.

The generated local bundle logically contains:

- `manifest.json`, `config.json`, `contracts.json`, `evaluations.json`, and `capabilities.json`;
- `INSTRUCTIONS.md` and `RUNBOOK.md`;
- an executable `runner.py` for `notes_to_actions`, `evidence_answer`, or `reviewed_brief`.

The CLI persists content-addressed bundle/build descriptions and immutable generated code.
The final package references specification, contracts, validation, code and accepted execution
evidence by digest. It does not publish, deploy, or certify a human approval.

The canonical schema is broader than this runtime. Generation rejects arbitrary transitions,
declared human gates and custom component tools, including attempts to defer them. The supported
families have a single terminal result state; evidence agents can use only bounded document lookup
and an explicitly configured host model provider. Declaring a capability is not executing it.

Protected actions belong to Wilfred's separately trusted host APIs, not to a generated runner's
configuration. The acceptance proof exercises a blocked harmless host action and hostile isolated
worker attempts; its positive signing fixture is not human consent. See the precise
[authorization boundary](architecture.md#protected-actions-host-enforcement-not-generated-gates).

## Review gate and publishing

publishing is intentionally not implemented, installed, or integrated in this MVP. It is a future reference case, not part of the factory. Starting publishing before the factory review would mix defects in the factory with defects in the first product and weaken the evidence for both. publishing work starts only after reviewers confirm the generic demo, lifecycle gates, resume behavior, and package evidence; it can then become the first real product-generation project.

See [cli.md](cli.md) for the reproducible demo and full command reference. See
[evidence.md](evidence.md) for final checks, review results, residual risks, changed files, and
command history.
