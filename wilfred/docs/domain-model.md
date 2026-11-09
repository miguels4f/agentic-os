# Canonical domain and event model (P1.2)

Status: **design reference, not implemented.** Written 2026-10-09 against base commit
`ecdcb66` of `codex/wilfred-product`. No runtime code implements this model yet; roadmap
sessions P1.3–P1.6 implement it. Nothing here changes current behaviour, and nothing here is
evidence that the current product already satisfies it. Requirement IDs (R1, R4, R5, R6) refer
to the non-negotiable requirements of the unified roadmap.

The companion [threat model](threat-model.md) defines the principals, trust zones and privileged
routes that the events below must record.

## 1. Principles

1. **Events are facts; state is a projection.** Every entity's current state is rebuilt by
   replaying its events in ledger order. No component writes "current state" that cannot be
   recomputed from events (R5). Projections (today's `state.json` files) become caches.
2. **Append-only and tamper-evident.** An event is never edited or deleted. Corrections are new
   events (`*.corrected`, `*.voided`) that reference the event they supersede. Each event carries
   the digest of the previous event in the same stream (hash chain), so a removed or rewritten
   line is detectable.
3. **Stable identity.** Every entity has an immutable `id` with a type prefix
   (`prj_`, `pkg_`, `agt_`, `wrk_`, `run_`, `att_`, `act_`, `apr_`, `evd_`, `cst_`, `sch_`,
   `con_`, `mem_`, `qlp_`, `fnd_`, `esc_`, `fpj_`) followed by a sortable unique suffix. Content-addressed artifacts are also
   identified by `sha256:<hex>`.
4. **Explicit versions.** Every event payload names `schema` (for example
   `wilfred-event/run.accepted/v1`). A reader that does not know a schema version stops; it
   never guesses (same rule as today's `VersionedModel`).
5. **Provenance on every event.** Who emitted it, from which trust zone, against which base
   commit, with which clock — see §3.
6. **Unknown is a value.** Cost, usage, duration, outcome and identity can be `unknown`. Unknown
   is never coerced to zero, empty or success. A sum that includes an unknown term is unknown.
7. **Model output is a claim, not a fact.** An event whose content originates from a model is
   typed as a *claim* (`emitter.kind = "model"`) and can never by itself transition an entity
   into an approved, accepted, completed or promoted state.

## 2. Principals that may emit events

| Principal (`actor.kind`) | Meaning | May emit |
|---|---|---|
| `owner` | the owner, proven only by an Ed25519 signature from the owner's offline key | approval decisions, policy changes, package activation/revocation, schedule enable/disable |
| `host` | Wilfred code running as the owner's user, outside any worker sandbox | everything mechanical: creation, deterministic check results, state transitions that follow a verified rule |
| `policy` | The owner policy switch (`policy:auto`) acting for the owner at policy-eligible points only | `approval.decided` for the three policy-eligible purposes; never for run gates, promotion or independence attestation |
| `coordinator` | The launching Claude/Codex session that drives the CLI | requests (`*.requested`), result submissions, usage submissions — all claims |
| `worker` | A generated or dispatched worker (sandboxed Foundry runner, Codex/Claude sub-agent) | result claims, tool-call requests |
| `reviewer` | A review process; *independent* only with a signed attestation | review reports, findings |
| `provider` | A model provider adapter (Codex CLI today) | provider receipts (raw + normalized) |
| `external` | GitHub, a connector, the operating system clock | observations imported by `host` with source references |

A principal is recorded, never inferred from a free-text label. Today's `decided_by`, `author`
and `set_by` strings are labels; they become principals only when bound to a signature or to
the host process that emitted them.

## 3. Event envelope (all events)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "wilfred-event-envelope/v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["event_id", "schema", "stream", "sequence", "prev_digest", "entity",
               "actor", "provenance", "time", "payload", "digest"],
  "properties": {
    "event_id":   {"type": "string", "pattern": "^evt_[0-9A-Za-z]{20,40}$"},
    "schema":     {"type": "string", "pattern": "^wilfred-event/[a-z_]+\\.[a-z_]+/v[0-9]+$"},
    "stream":     {"type": "string", "description": "ledger stream, e.g. project id"},
    "sequence":   {"type": "integer", "minimum": 0},
    "prev_digest":{"type": ["string", "null"], "pattern": "^sha256:[0-9a-f]{64}$",
                   "description": "digest of the previous event in this stream; null only for sequence 0"},
    "entity":     {"type": "object", "additionalProperties": false,
                   "required": ["type", "id"],
                   "properties": {"type": {"enum": ["project", "package", "agent", "work",
                                  "run", "attempt", "action", "approval", "evidence",
                                  "cost", "schedule", "connector", "memory", "quality_loop", "finding",
                                  "escalation", "foundry_project"]},
                                  "id": {"type": "string"},
                                  "version": {"type": ["integer", "null"], "minimum": 1}}},
    "actor":      {"type": "object", "additionalProperties": false,
                   "required": ["kind", "id", "zone"],
                   "properties": {"kind": {"enum": ["owner", "host", "policy", "coordinator",
                                  "worker", "reviewer", "provider", "external"]},
                                  "id": {"type": "string"},
                                  "zone": {"enum": ["Z0-owner-offline", "Z1-host",
                                           "Z2-coordinator", "Z3-worker-sandbox",
                                           "Z4-provider", "Z5-external"]},
                                  "signature_ref": {"type": ["string", "null"],
                                                    "description": "evidence id of a verified signature, required when kind=owner"}}},
    "provenance": {"type": "object", "additionalProperties": false,
                   "required": ["base_commit", "product_version", "causation_id", "correlation_id"],
                   "properties": {"base_commit": {"type": ["string", "null"],
                                  "description": "git commit of the target repository; null only when the entity has no repository"},
                                  "product_version": {"type": "string",
                                  "description": "Wilfred commit or package version that emitted the event"},
                                  "causation_id": {"type": ["string", "null"],
                                  "description": "event_id that caused this one"},
                                  "correlation_id": {"type": "string",
                                  "description": "run or work id grouping related events"}}},
    "time":       {"type": "object", "additionalProperties": false,
                   "required": ["recorded_at", "source"],
                   "properties": {"recorded_at": {"type": "string", "format": "date-time"},
                                  "source": {"enum": ["host-wallclock", "signed-payload",
                                             "provider-reported", "external-reported", "unknown"]},
                                  "observed_at": {"type": ["string", "null"], "format": "date-time"}}},
    "payload":    {"type": "object"},
    "digest":     {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$",
                   "description": "SHA-256 of the canonical JSON of every other field"}
  }
}
```

Notes:

- `time.source` exists because the host clock is owner-controlled and not trustworthy for
  ordering (accepted residual m2 concerns file timestamps; the same caution applies to event
  times). **Order comes from `sequence` and `prev_digest`, never from timestamps.**
- Canonical JSON: UTF-8, sorted keys, no insignificant whitespace (same convention as
  `approvals.canonical_bytes` today).

## 4. Shared value types

### 4.1 Cost and usage (never zero when unknown)

```json
{
  "$id": "wilfred-cost-amount/v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["status", "currency", "amount", "basis"],
  "properties": {
    "status":   {"enum": ["known", "estimated", "unknown", "not_applicable"]},
    "currency": {"type": ["string", "null"], "pattern": "^[A-Z]{3}$"},
    "amount":   {"type": ["string", "null"], "pattern": "^[0-9]+(\\.[0-9]+)?$",
                 "description": "decimal string; null unless status is known or estimated"},
    "basis":    {"type": "string", "description": "e.g. provider-invoice, price-table:<id>@<date>, token-estimate"},
    "unknown_reason": {"type": ["string", "null"]}
  },
  "allOf": [
    {"if": {"properties": {"status": {"const": "unknown"}}},
     "then": {"properties": {"amount": {"const": null}, "unknown_reason": {"type": "string", "minLength": 1}}}},
    {"if": {"properties": {"status": {"enum": ["known", "estimated"]}}},
     "then": {"properties": {"amount": {"type": "string"}, "currency": {"type": "string"}}}}
  ]
}
```

Usage (tokens, wall-clock, tool calls) uses the same `status` discipline with integer fields.
Aggregation rule: `known + unknown = unknown`; `known + estimated = estimated`.
Today's `RunEvent.cost_usd` defaults to `0.0` and is never set by any caller (see gap G3).

### 4.2 Evidence reference

`{"evidence_id": "evd_…", "digest": "sha256:…", "media_type": "…", "role": "input|output|log|report|signature|receipt"}`.
An evidence reference without a content digest is not evidence; it is a pointer.

## 5. Entities

Each row: identity · owner · lifecycle states · creating/transitioning events · who may emit ·
what evidence binds it.

| Entity | Identity | Owner | States | Events | Emitters | Binding evidence |
|---|---|---|---|---|---|---|
| **Project** | `prj_` + repository identity (remote URL digest + root commit) | owner | `registered → active → archived` | `project.registered`, `project.archived` | owner (register/archive), host (record) | repository root commit, data class of the project (threat model §3) |
| **Package** (`AgentPackage`) | `pkg_` + `name@version` + digest of spec, capabilities, permissions, evaluations, provenance | owner | `draft → validated → active → superseded / rolled_back / revoked → retired` | `package.registered`, `package.validated`, `package.activated`, `package.rolled_back`, `package.revoked`, `package.retired` | host (validate), owner via signed approval (activate, revoke, rollback) | package digest; evaluation evidence; approval id for activation |
| **Agent** | `agt_` + package id + role id | package | `defined → enabled → disabled` | `agent.defined`, `agent.enabled`, `agent.disabled` | host (from package), owner (enable/disable) | role contract digest, tool/permission list, model tier (D-MODEL-1) |
| **Work** | `wrk_` (today also a learning proposal id) | project | `proposed → (revised)* → under_review → accepted / rejected → promoted → in_progress → blocked → done / abandoned`; `proposed → withdrawn` | `work.proposed`, `work.revised`, `work.withdrawn`, `work.accepted`, `work.rejected`, `work.rescoped`, `work.promoted`, `work.blocked`, `work.completed`, `work.abandoned` | coordinator (propose), owner (accept when required), host (complete when receipts satisfy acceptance) | objective digest; acceptance criteria; terminal run receipt ids |
| **Run** | `run_` (today `run_id`) | work | see §5.1 transition table: `requested`, `created`, `in_progress`, `awaiting_human`, `gate_failed`, `completed`, `failed`, `cancelled` | `run.requested`, `run.created`, `run.plan_recorded`, `run.transitioned`, `run.step_transitioned`, `run.awaiting_human`, `run.hold_released`, `run.completed`, `run.failed`, `run.cancelled`, `run.receipt_issued` | coordinator (request), host (every transition) | base commit, plan digest, guardrail config digest, terminal `RunReceipt` |
| **Attempt** | `att_` + run + step + packet + ordinal | run | `started → succeeded / failed / rejected / timed_out / outcome_uncertain` | `attempt.started`, `attempt.finished` | host | step instruction digest, packet digest, worker/provider identity, result evidence |
| **Action** (privileged effect) | `act_` + route id from the threat-model registry | run | `requested → authorized → executing → succeeded / failed / outcome_uncertain`; or `requested → denied` | `action.requested`, `action.authorized`, `action.denied`, `action.started`, `action.finished` | coordinator/worker (request), host (authorize after verifying approval, execute, finish) | route id, exact arguments digest, scope, approval id, attempt marker |
| **Approval** | `apr_` (today `gate_id` + challenge nonce; a changed-evidence gate gets a derived id `<gate>-<fp16>`) | owner | `challenged → decided(approve / reject / modify_scope) → consumed`; `challenged → expired → renewed` (new `apr_`, same gate); `decided(modify_scope) → superseded`; `decided / consumed → voided` (key replaced, policy disabled or changed); `decided → expired` (token expired before use; cannot be re-challenged) | `approval.challenged`, `approval.renewed`, `approval.decided`, `approval.consumed`, `approval.expired`, `approval.voided`, `approval.superseded` | host (challenge, consume, expire, void), owner (decide, signed), policy (decide, policy-eligible purposes only) | challenge digest, action digest, evidence digest, nonce, expiry, key fingerprint or policy-file digest |
| **Evidence** | `evd_` + content digest | the entity it binds | `recorded → superseded` (never deleted while referenced) | `evidence.recorded`, `evidence.superseded` | host (deterministic outputs), reviewer/worker/provider (claims, typed as such) | the content digest itself; producer and command line |
| **Cost** | `cst_` | run or attempt | `open → reported → reconciled` | `cost.reported`, `cost.reconciled` | provider (reported), coordinator (claimed usage), host (reconciled) | provider receipt, transcript digest, price table version; §4.1 amount |
| **Schedule** | `sch_` | owner | `proposed → enabled → paused → disabled` | `schedule.proposed`, `schedule.enabled`, `schedule.paused`, `schedule.fired`, `schedule.disabled` | owner (enable/disable), host (fire) | schedule spec digest, the work/run it creates |
| **Connector** | `con_` + provider + scope | owner | `declared → authorized → active → suspended → revoked` | `connector.declared`, `connector.authorized`, `connector.used`, `connector.revoked` | owner (authorize/revoke), host (use, through the action route) | scope list, secret *reference* (never value), each use as an Action |
| **Memory** | `mem_` + project + principal + content digest | project | `proposed → stored → used → expired / deleted` | `memory.proposed`, `memory.stored`, `memory.used`, `memory.deleted` | worker/coordinator (propose), host (store under policy, record use), owner (delete) | provenance (source evidence ids), sensitivity class, retention rule, context-use receipt (R6) |
| **QualityLoop** | `qlp_` + run | run | `evaluating → patching → evaluating …` (iteration counter), `pending_independent_review`, `passed`, `escalated` | `quality.iteration_started`, `quality.phase_changed`, `quality.passed`, `quality.escalated` (open blocker/major/must-fix counts, best/last score) | host | critique and patch records (digests), independence attestations |
| **Finding** | `fnd_` | the run, review or project it was raised on | `open → resolved / overridden / waived` | `finding.raised`, `finding.resolved`, `finding.overridden` (requires `approval_id`) | reviewer, host (deterministic), worker (claim) | evidence refs; source (critic, verify check, review report, anomaly) |
| **Escalation** | `esc_` | run (may reference a run id with no run state) | `raised → acknowledged → closed` | `escalation.raised`, `escalation.acknowledged`, `escalation.closed` | host | anomaly or quality evidence; notification file digest |
| **FoundryProject** | `fpj_` + folder identity (today `project_id = root.name`) | owner | `frame → classify → scout → blueprint → contract → generate → evaluate ⇄ repair → package` | `foundry.created`, `foundry.intake_answered` (with `evidence_status`: grounded / assumed / contradictory / missing; `autopilot` flag), `foundry.stage_transitioned`, `foundry.evaluation_failed`, `foundry.repaired` (before/after digests) | host; owner for intake answers | content-addressed artifacts and revisions; produces a Package in `draft` only — pilot/production stages only via `package.activated` |

### 5.1 Run and step transition tables

Run (`from → to`, required `reason` on every row):

| From | To | Cause in today's code |
|---|---|---|
| — | `requested` | coordinator request |
| `requested` | `created` | `hw plan` (`engine.py:164`) |
| `created` | `in_progress` | first step starts |
| `in_progress` | `awaiting_human` | gate opened, or quality escalation (`hold_kind`) |
| `in_progress` | `gate_failed` | deterministic gate red |
| `gate_failed` | `in_progress` | re-evaluation (`engine.py:591`) |
| `awaiting_human` | `in_progress` | approval verified, or quality pass (`quality.py:1005`) |
| `awaiting_human` / `gate_failed` | `in_progress` / `created` | resume (`resume.py:260`) |
| `in_progress` / any non-terminal | `completed` | all steps done, including via resume (`resume.py:217`) |
| any non-terminal | `failed` / `cancelled` | failure / owner cancellation |

`run.plan_recorded` carries `plan_digest` when `plan.json` is written by its step (it is a
worker-written artifact, so the event is a claim checked by the host). `run.awaiting_human` has
nullable `approval_id` and required `hold_kind` (`gate`, `quality_escalation`, `anomaly`); a
`quality_escalation` hold is released only by `run.hold_released` with an owner approval, or is
declared terminal — today no code releases it (`quality.py:953-957`, `gates.py:372`).

Step: `pending → running → done / failed`, plus **backward** `done → pending` with required
`reset_cause` (`gate_opened` `gates.py:251-252`, `patch_accepted` `quality.py:364,1181`,
`resume_cascade` `resume.py:253-255`, `review_reset`).

Allowed-transition rule: a projection rejects any event that is not a listed transition from
the current state, or whose `actor.kind` is not in that entity's emitter list. A rejected event
stays in the ledger and is reported as an integrity finding; it does not change state.

## 6. Event payloads (typed tables)

Only fields beyond the envelope are listed. **R** = required. Every field marked *digest* is
`sha256:<hex>`.

### Run

| Event | Payload fields |
|---|---|
| `run.requested` | R `mode` (build/improve/review), R `objective_digest`, R `product_dir` (repository-relative), R `requested_by_session` (coordinator id or `unknown`), `tier` |
| `run.created` | R `guardrail_config_digest`, R `base_commit`, R `known_failures_digest` or `null` |
| `run.plan_recorded` | R `plan_digest`, R `step_id` |
| `run.step_transitioned` | R `step_id`, R `from`, R `to` (pending/running/done/failed), R `reason`, R `evidence` (list of evidence refs; empty only for `running`), `reset_cause` (required when `to = pending`) |
| `check.downgraded` | R `check`, R `original_severity`, R `new_severity`, R `declaration_digest`, R `base_commit`, R `matched_node_ids` (known-failures tolerance, `known_failures.py:208-280`, `engine.py:636-651`) |
| `run.awaiting_human` | R `hold_kind`, `approval_id` (null for quality escalation), `gate_kind`, R `warnings` (list) |
| `run.completed` / `run.failed` / `run.cancelled` | R `terminal_reason`, R `receipt_id` |
| `run.receipt_issued` | R `receipt_id`, R `steps` (id → final state + evidence ids), R `approvals` (ids, including every `policy:auto` approval), R `cost` (§4.1), R `usage`, R `unresolved_findings` (ids), R `provider_receipts` (ids) |

### Attempt

| Event | Payload fields |
|---|---|
| `attempt.started` | R `run_id`, R `step_id`, R `ordinal`, R `packet_id`, R `executor` (`{"kind": "worker|provider|host-tool", "id": …, "sandbox": "sandbox-exec-deny-default|codex-workspace-write|none"}`), R `instruction_digest`, `packet_digest` |
| `attempt.finished` | R `outcome` (succeeded/failed/rejected/timed_out/outcome_uncertain), `rollback` (`{performed, tree_digest_before, tree_digest_after}`, required when `rejected`), R `exit_status` (integer or `unknown`), R `result_evidence` (refs), R `usage` (§4.1 discipline) |

### Action

| Event | Payload fields |
|---|---|
| `action.requested` | R `route_id` (threat-model registry id), R `arguments_digest`, R `arguments_schema` (id + version), R `scope` (paths/remotes/hosts), R `requested_by` (actor) |
| `action.authorized` | R `approval_id`, R `verified_by` (host component + version), R `arguments_digest` (must equal the request) |
| `action.denied` | R `reason` (missing approval, scope widened, expired, replay, policy-ineligible, unknown route) |
| `action.started` | R `attempt_marker` (evidence ref of the `.executed`-style marker) |
| `action.finished` | R `outcome` (succeeded/failed/outcome_uncertain), R `effect_evidence` (refs), R `retry_allowed` (always false when `outcome_uncertain`) |

### Approval

| Event | Payload fields |
|---|---|
| `approval.challenged` | R `purpose` (protected-action, quality-review-override, verify-finding-override, test-evolution, learning-proposal, independent-quality-review), R `action_digest`, R `evidence_digest`, R `nonce_digest`, R `expires_at`, R `policy_eligible` (bool) |
| `approval.decided` | R `decision` (approve/reject/modify_scope), R `decided_by` (principal), R `authority` (`{"kind": "signature", "key_fingerprint": …}` or `{"kind": "policy", "policy_digest": …}`), R `challenge_digest`, R `decision_expires_at`, `note` |
| `approval.consumed` | R `consumer` (action or step id), R `receipt_digest` |
| `approval.renewed` | R `renews` (previous `apr_`), R `archived_record_digest` |
| `approval.expired` / `approval.voided` | R `reason` (`time`, `post_consumption_expiry`, `policy_disabled`, `policy_changed`, `key_replaced`, `evidence_changed`) |
| `approval.superseded` | R `superseded_by` (new `apr_`) — modify-scope decisions; today such gates stay `MODIFIED` and the run cannot move (`gates.py:207-208,543-548`) |

### Evidence, cost, package, others

| Event | Payload fields |
|---|---|
| `evidence.recorded` | R `digest`, R `media_type`, R `producer` (actor), R `command` (argv digest or `null`), R `claim` (bool: true when model- or worker-authored), `evidence_status` (grounded/assumed/contradictory/missing) |
| `cost.reported` | R `subject` (run/attempt id), R `amount` (§4.1), R `usage` (tokens in/out/cache read/cache write, each known or unknown, with per-model breakdown), R `source` (provider-receipt, transcript, estimate) |
| `cost.corrected` | R `corrects` (event id), R `amount`, R `usage` — re-recording usage today overwrites `usage.json` (`cost.py:284`) |
| `package.registered` | R `name`, R `version`, R `spec_digest`, R `capabilities`, R `permissions`, R `provenance` (list of evidence refs) |
| `package.activated` / `revoked` / `rolled_back` | R `approval_id`, R `previous_active` (id or null) |
| `schedule.fired` | R `schedule_id`, R `created` (work or run id), R `fired_at_source` (time source) |
| `connector.used` | R `action_id` (every connector use is an Action) |
| `memory.used` | R `memory_id`, R `consumer` (run/attempt), R `purpose`, R `sensitivity` |

## 7. Replay and projection rules

1. Replay reads one stream in `sequence` order, verifies `prev_digest` and `digest` for every
   event, and stops at the first mismatch (tamper or truncation) with an integrity finding.
2. A projection is a pure function of the verified event list and the projection's own version.
   Two replays of the same events with the same projection version produce byte-identical
   canonical JSON. Clocks, environment and file-system state are not inputs.
3. A projection never reads a "current state" file as input. Today's `state.json` files may be
   kept as caches but must be rebuildable and are compared against replay in tests (P1.6).
4. Unknown schema versions stop replay; they are never skipped.
5. A truncated last line (crash mid-write) is reported, not silently skipped. Today
   `RunStore.read_events` silently drops any unparseable line, anywhere in the file (gap G5).

## 8. Mapping from today's artifacts

Base `ecdcb66`. "Run root" = `<repo>/.hw/<run_id>/`. "Foundry root" = the Agent Foundry
project folder.

| Today's artifact (file) | Maps to | Gap |
|---|---|---|
| `.hw/<run>/state.json` (`RunState`, schemas.py) | Run projection | Written directly as current state, not derived from events; no event replays it |
| `.hw/<run>/events.jsonl` (`RunEvent`, run_store.py) | Run/Attempt events | Free-text `stage` + `message`; no actor, no zone, no base commit, no sequence, no hash chain; `metadata` is `dict[str, str]` only |
| `.hw/<run>/plan.json` (worker-written by a step), `guardrails.json`, `packets/`, `results/` | `run.plan_recorded`, `run.created` digests; Attempt evidence | No digests recorded in the event log; worker result files are claims without a typed producer |
| `.hw/<run>/verdicts/` (acceptance) | `evidence.recorded` + `run.step_transitioned` | Not hash-linked to the transition they justify |
| `.hw/<run>/gates/<gate>.json` (`HumanGate`) | Approval projection + `run.awaiting_human` | Gate record itself is unsigned (documented in human-approvals-policy.md); `decided_by` is a label |
| `gates/<gate>.challenge.json`, `<gate>.decision.json`, consumed receipt, `<gate>.executed`, `gates/history/` | `approval.challenged`, `approval.decided`, `approval.consumed`, `action.started/finished` | Closest to the target model already (digests, nonce, expiry). Missing: link from the consumed approval to an `act_` route id and arguments schema |
| `review-attestations/`, quality loop state (`QualityLoopState`) | `evidence.recorded` (reviewer), `approval.decided` (independence attestation) | Review author is an enum label unless attested |
| `test-evolution/`, `regression_baseline.json` | Evidence (baseline) + Approval (test-evolution, policy-eligible) | Baseline has no event recording who captured it from which commit |
| `known_failures` declaration read at `base_commit` (known_failures.py) | `run.accepted.known_failures_digest` | Digest not recorded |
| `usage.json` (cost.py, `hw record-usage`) | `cost.reported` with `source=transcript` | Tokens only; no currency amount; reads Claude transcripts supplied by the coordinator (claim) |
| `RunEvent.cost_usd` | `cost.reported` | Always `0.0`: unknown cost is recorded as zero (G3) |
| `.hw/observability/memory.json` (`CodebaseMemoryIndex`) | Memory (project scope, run-outcome entries) | Index is regenerated wholesale; no use receipts, no retention or deletion events |
| `escalations.jsonl` + notification file (escalation.py) | Escalation entity | Separate ledger; never changes `RunState` (`escalation.py:85-96`) and can create a run directory with no run; only observability anomalies use it — quality escalations do not |
| `.hw/proposals/` (learning proposals) (`LearningProposal`) | Work (`work.proposed`) + Approval (`learning-proposal`) | Proposal status stored as mutable field |
| Foundry `state.json` + `revisions/<sha>.json` (foundry_storage.py) | FoundryProject projection (`project_id = root.name`, a folder name) | Revisions are content-addressed (good); state pointer is mutable |
| Foundry `history.jsonl` (foundry.py `_event`) | FoundryProject design-stage events | Free-form `event` string; no actor/sequence/chain; `create` saves state before writing the event, so a crash between them leaves state without an event |
| Foundry `artifacts/<name>-<sha>.json`, `generated-<sha>/` | Evidence + Package build | Already content-addressed |
| `PackageRecordV1` (foundry_models.py) | `package.registered` | No permissions, capabilities or approval binding yet (P1.3); `package()` writes the record already at stage `PRODUCTION`, version `"1"`, with no activation step (`foundry.py:494-499`) |
| `verify-checks.json`, `verify-overrides.json` (verify_checks.py:26-27) | Finding + Approval (verify-finding-override) | Overwritten per verify run |
| `.hw/lessons/` (learning.py:160-185) | Memory (`memory.used` on every next step) | Lessons are attached to steps with no record of use |
| `.hw/observability/anomalies.json`, `dashboard.json` | Finding (anomaly) projection | Regenerated wholesale |
| `evidence/<name>` via `RunStore.write_evidence` (run_store.py:129-133) | `evidence.recorded` | Overwritten in place under the same name |
| quality loop state in `state.json` (`QualityLoopState`) | QualityLoop entity | Iterations, phases, scores only in mutable state |
| `provider-receipts.json` (foundry_agents.py / foundry_acceptance.py) | Provider receipt evidence + `cost.reported` | Written only by the acceptance demo path; no usage/cost fields |
| `~/Library/Application Support/Wilfred/policy.json` | `approval.decided` authority `policy` | Policy changes are not events; a change is only detectable by digest mismatch later |

### Gaps (summary)

- **G1** No common envelope: three separate logs (run events, Foundry history, escalations)
  with different shapes and no actor/zone/provenance.
- **G2** No tamper evidence: logs are append-mode files without sequence or hash chain.
- **G3** Unknown cost and unknown duration are recorded as `0.0` / `0` (`run_store.py:48-49`).
- **G4** State is primary; events are secondary commentary. Replay cannot rebuild `state.json`.
- **G5** Unparseable event lines are silently skipped anywhere in the file.
- **G6** No entity exists today for Schedule, Connector, Agent (as a runtime entity) or Work
  (beyond the run objective). These are model-only.
- **G7** Privileged effects performed by the coordinator outside Wilfred (git push, merge, PR,
  deploy) produce no Wilfred event at all; see the threat-model registry.
- **G8** Policy-switch changes and public-key replacement are not recorded as events, yet they silently void past approvals on re-verification (`approvals.py:337-339,350-351,436-439`).
- **G9** Several holds are dead ends today: a quality escalation and a modify-scope gate leave the run blocked with no release path; a modify-scope learning proposal stays under review indefinitely (`learning.py:540`).

## 9. Review record

Reviewed 2026-10-09 by an independent read-only subagent with the instruction "find an entity
or transition that today's code produces but the model cannot express". Findings and their
disposition are recorded in §9.1.

### 9.1 Reviewer findings

The reviewer reported 14 gaps and 5 mapping corrections; all were applied:

| # | Reviewer finding | Change made |
|---|---|---|
| 1 | Run states/transitions differ from code (`created`, late `plan.json`, resume and re-evaluate transitions) | Added `created`, `run.plan_recorded`, explicit run transition table (§5.1) |
| 2 | Steps go backwards (`done → pending`) | Step transitions with required `reset_cause` |
| 3 | Quality escalation awaits a human with no approval | `hold_kind`, nullable `approval_id`, `run.hold_released`; gap G9 |
| 4 | Quality loop has no entity | QualityLoop entity |
| 5 | Rejected patch rollback unrecorded | Attempt outcome `rejected` + `rollback` field |
| 6 | Findings referenced but undefined | Finding entity |
| 7 | Escalations mislabelled as run holds | Escalation entity; §8 row corrected |
| 8 | Approval renewal and derived ids | `approval.renewed`, derived-id note |
| 9 | Approvals voided/expired after use (key replaced, policy off) | New void/expiry reasons; gap G8 |
| 10 | Modify-scope dead ends | `approval.superseded`, `work.rescoped`; gap G9 |
| 11 | Proposals rewritten or dropped in place | `work.revised`, `work.withdrawn`, rejected/promoted states |
| 12 | Known-failure downgrades untraceable | `check.downgraded` event |
| 13 | Foundry design phase and intake statuses | FoundryProject entity, `evidence_status`, package only via activation |
| 14 | Attempt identity too coarse for fanout | `packet_id` in identity and required on `attempt.started` |
| minor | duration default 0; usage overwritten; no per-model usage | G3 widened; `cost.corrected`; per-model breakdown |
| §8 | escalation, Foundry history, Foundry state, plan.json rows; missing rows | Rows corrected; five rows added |

No reviewer finding was rejected.
