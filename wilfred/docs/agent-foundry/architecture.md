# Architecture and decisions

## MVP boundary

The MVP designs and packages an agent system; it is not a general deployment platform. It runs locally, stores JSON/Markdown artifacts, rejects secrets, and performs no external side effects. Its first adapter targets an inspectable Python manifest because that is the smallest runtime-neutral bridge that can prove the lifecycle without committing to a vendor framework.

The local Python adapter classifies every requested capability:

- `direct`: instructions, local JSON configuration, JSON Schema data contracts, and evaluations.
- `direct` for evidence-agent families: `model_execution` through the trusted host provider and
  `bounded_document_lookup` over the documents supplied as task input.
- `unsupported`: generic `tool_execution`, `state_machine`, `human_gates`, persistent memory,
  external side effects and unknown capabilities. The generated runner does not emulate them.

Generation supports only the three bounded families and their single terminal result state.
Specifications containing arbitrary transitions, declared human gates or custom component tools
are rejected, even when a deferral is supplied. A schema's ability to describe something is not
proof that this adapter can execute it. Required unsupported behavior must not disappear silently.

The runner uses macOS deny-by-default process isolation. It cannot read host credentials,
write host state, spawn children or open sockets. The trusted host brokers bounded model calls
for `evidence_answer` and `reviewed_brief`; the worker only receives selected evidence and
JSON responses. Writer and reviewer receive separate contexts. `notes_to_actions` needs no model.
Absent providers and unsupported operating systems fail closed.

## Protected actions: host enforcement, not generated gates

`gates.execute_protected_action` is a trusted-host API separate from the generated runner. It
checks the signed decision against the current action, evidence, expiry and one-time challenge
before invoking a callback. A worker cannot mint a signature, alter the host approval directory,
or replace authorization with `human_approved: true`. The bounded workers expose no arbitrary
protected external effect or generated approval workflow.

The R6 demonstration has two observable boundaries: a real harmless host callback remains
unexecuted without authorization and after a self-approval claim; isolated-worker tests attempt
forbidden host access and unavailable actions. An ephemeral signing fixture then exercises only
the verifier's positive path and replay rejection. This is not a real person's consent, nor proof
of an arbitrary generated human-gate workflow. Independent review must examine both boundaries
and retain any unproven essential requirement as incomplete.

## Evaluation and repair

Evaluation executes normal, edge, adversarial-input, and recovery scenarios against the
materialized package. Caller-supplied results are never pass evidence. The record separates
deterministic scenario checks from model execution (live/fixture/no-model) and human approval
(`not_requested`). A writer's content-review approval is not permission for a protected host
action. Packaging does not certify broad semantic accuracy or human sign-off.

A failure moves the project to `repair`. The supported seeded empty-input repair modifies
`runner.py` and its manifest, records a source diff, runs regression and publishes a new immutable
version only after success. Accepted upstream artifacts and old code/evidence remain unchanged.
Unknown defects are not magically repaired: they stop with a required source intervention.

## External scouting

Scouting is read-only and provider-neutral. The CLI currently accepts a local JSON fixture through `next --scout-fixture`; the core also defines optional providers such as GitHub search. A candidate can be classified as `reference_only`, `external_dependency`, `licensed_adaptation`, or `rejected`.

Reuse fails closed unless the dossier has a URL, sufficient provenance, an immutable reviewed version or commit, an allowed license (MIT, Apache-2.0, BSD-2-Clause, or BSD-3-Clause by default), and positive checks for provenance, maintenance, security, tests, compatibility, and correctness against a primary source. Search metadata alone does not prove those checks. Rejected candidates remain evidence; they are not downloaded, executed, or copied.

## Optional Assist lane

Assist is optional and off by default. When disabled, unavailable, unconfigured, or failed, bounded low-impact choices use and record the declared baseline. High-impact or irreversible decisions always fall back to the baseline and require human approval; Assist never authorizes an action.

Adoption requires a paired benchmark against the baseline. Activation is recommended only if every configured threshold passes: quality and accuracy gains, cost and latency ratios, low-confidence rate, and failure rate. A single failed threshold keeps Assist off.

## Relationship to HyperWilfred

Agent Foundry is a namespaced capability inside `hyperwilfred-core`; its `hw foundry ...` verbs coexist with existing HyperWilfred run commands such as `hw plan`, `hw next-step`, and `hw evaluate`. It reuses the same installed CLI entry point, Pydantic-based typed contracts, stable JSON conventions, deterministic gates, local artifact discipline, and human-control principles. The namespace prevents lifecycle verbs such as `status`, `evaluate`, and `resume` from colliding with HyperWilfred build runs.

Foundry does not reimplement HyperWilfred's worker fan-out, worktree acceptance, quality loop, deployment, or merge gates. Conversely, creating a Foundry project does not start a HyperWilfred build run. Foundry produces a reviewed design/package that later automation can consume.

## Deferred capabilities and risks

- The three supported families are bounded, not arbitrary agent generation.
- Persistent agent memory, arbitrary external tools, deployment and registry publication are absent.
- Online provider wiring and registry-specific scouting remain adapter work; incomplete evidence is rejected.
- Autopilot cannot resolve consequential ambiguity without a person, so unattended runs may stop during framing.
- ASS v1 evolution needs an explicit migration path; unsupported versions currently stop.
- Immutable revisions retain prior artifacts and code; multi-writer concurrency is not supported.
- Protected host actions verify signed, expiring, one-time challenges. Private signing keys must
  remain outside the generated process. Test signatures prove the protocol, not human consent.
- Repair currently fixes the seeded empty-input source defect and verifies the changed runtime.
- Assist needs representative benchmark data before activation; synthetic wins are insufficient.
- Runtime choice is currently macOS/local Python; Linux and Windows execution fail closed.
- publishing implementation is deferred until the factory itself passes review.

## Final PR evidence template

Copy this section into the final pull request and replace placeholders with observed output. Do not mark a check as passed without command evidence.

### Scope

- Objective: `<what changed and why>`
- Paths changed: `<git diff --name-only>`
- Explicit exclusions: no publishing implementation; no deploy, secrets, migrations, or external side effects

### Command history

| Command | Result | Evidence |
|---|---|---|
| `uv run pytest tests -q` | `<pass/fail>` | `<counts or failure summary>` |
| `uv run ruff check .` | `<pass/fail>` | `<summary>` |
| `git diff --check` | `<pass/fail>` | `<summary>` |
| `<generic demo commands from docs/agent-foundry/cli.md>` | `<pass/fail>` | `<final stage and artifacts>` |

### Review evidence

- Guided and autopilot use the same lifecycle: `<evidence>`
- Resume detects missing/corrupt artifacts: `<evidence>`
- Invalid specification/blueprint/contracts block generation: `<evidence>`
- Unlicensed or unproven reuse is rejected: `<evidence>`
- Failed evaluation requires targeted repair and re-evaluation: `<evidence>`
- Package contains only accepted, digest-tracked artifacts: `<evidence>`
- Assist remains off unless every benchmark threshold passes: `<evidence>`
- Reviewer/sign-off: `<name, date, decision>`
