# HyperWilfred — Architecture

> Historical design reference, imported from the source repository. Claims,
> commands and phase status below are not current product acceptance evidence.
> Use [architecture](architecture.md), [acceptance](acceptance.md) and
> [verification](verification.md) for the standalone product.

> One objective in. A built-or-improved software product out. Runs on both Claude Code and Codex.
> Modeled on the hyperresearch architecture: a thin router that loads each step fresh, isolated sub-agents each in their own context, all state on disk, tier routing, adversarial critics, and a patch-never-regenerate quality loop.

**Audience note.** This document leads with the business outcome in plain language, then keeps full technical substance for the engineers who build it. Technical terms are glossed the first time they appear.

---

## 0. What HyperWilfred is, in one paragraph

Today, getting software built or improved means a human shapes the work, writes the prompts, watches the agent, checks the result, and ships it. HyperWilfred removes the human from the middle of that loop without removing the human from the *decisions that matter* (deploying, merging, touching payments or secrets). You type one objective — "build me a landing page with a waitlist" or "make the checkout flow 30% faster" — and a coordinator program breaks it into pieces, runs many AI workers in parallel (each in its own clean workspace so they don't trip over each other), checks the result against hard quality gates (does it build, do the tests pass, is it secure), and either ships it or hands you a short list of decisions. The same coordinator runs identically whether you're driving it from Claude Code or from Codex (OpenAI's coding tool), because all the intelligence lives in one shared program and only a thin "translator" layer differs per tool.

---

## 1. CODEX VERDICT — can HyperWilfred run on Codex?

**Verdict: YES — design it cross-runtime — but the green light is CONTINGENT on a Phase-0 proof spike, not already proven end-to-end.**

What is genuinely proven today: the *pattern* is cross-runtime. The same skill catalog already runs on both tools (`codex_skills/` auto-synced from `.claude/skills/`), and the S42-S43 guardrails+rollback pipeline already executes *through Codex*. What is NOT yet demonstrated: the specific HyperWilfred mechanisms — parallel `spawn_agent` fan-out with per-worker git worktrees, and engine-level rollback-on-escape — running end-to-end on Codex with MCP servers live. The Codex-side artifacts in the repo today amount to a short pseudocode contract and a single-shot `codex exec` wrapper, not a working parallel orchestrator. So the honest verdict: of the nine primitives the architecture needs, **seven are natively portable**, **two need a thin per-runtime adapter** (both *symmetric* — the same gap exists on Claude Code, so HyperWilfred owns them either way), and **zero are known blockers — but two carry UNVERIFIED ceilings on Codex** (default concurrency ~6, nesting depth 1, plus the MCP/structured-output bug below) that the Phase-0 spike must measure before the engine surface is committed. If the spike fails, the fallback is explicit: ship Claude-Code-first, treat Codex as a later best-effort adapter — never tell the founder "cross-runtime is done" when it is a 14-line spec.

### What is ALREADY cross-runtime (the strongest evidence)

Two of these are *proven running on Codex*; the rest are *specified as Codex-capable but not yet demonstrated end-to-end*. Both matter — but do not conflate them (the session-driven contract and the docs-emitter are specs; the codex_skills sync and S42-S43 are the parts actually executed through Codex):

- **The session-driven orchestration contract already names Codex.** `wilfred/packages/legacy/docs/session-driven-orchestration.md` (verified) opens by stating it is **provider-neutral** — "Claude Code, Codex, o cualquier LLM con capacidad de tool use + subagentes" — and that "todo ocurre via JSON en disco y CLI. No hay dependencia del tool `Task` de Claude Code." It exposes exactly three CLI commands (`plan` → emits the DAG plus per-agent envelopes as JSON; `validate-agent` → runs pre+post checks plus gates, exit 0/1; `advance` → updates the on-disk checkpoint and returns the next agent). That is the hyperresearch shape — thin router, fresh-loaded step procedures, isolated sub-agents, state on disk, parallel fan-out — *and* the answer to the dual-runtime constraint, in one document.
- **One skill catalog already runs on both runtimes.** `codex_skills/` is auto-generated from the Claude `.claude/skills/` tree by `sync_codex_skills.py` (107 canonical sources → 81 active skills). The founder already maintains a single skill library that both tools consume.
- **A Codex-native guardrails+verification pipeline already exists.** The S42-S43 methodology writes machine-readable `guardrails.json` / evidence JSON to disk, runs a manual approval gate, executes within bounds, and auto-rolls-back on violation — proven running *through Codex*. This is the same shape as hyperresearch's tool-locked patcher + critic gates.
- **A one-source → two-output emitter already exists.** `ai-common/scripts/sync-agent-docs.py` (verified) generates both `CLAUDE.md` and `AGENTS.md` from a single canonical `.ai/AGENT_CONVENTIONS.md`. `AGENTS.md` is a cross-runtime standard read natively by both Codex and Claude Code. The cross-runtime emitter is half-built.
- **Codex Subagents reached GA.** Parallel `spawn_agent`, batch fan-out (`spawn_agents_on_csv`), MCP-server and skills inheritance from the parent, and per-worker output schemas are first-class on Codex, not experimental.

### What is portable for free (7 of 9 primitives)

Load a step's instructions fresh on demand · spawn an isolated sub-agent with its own context · fan out N sub-agents in parallel · MCP tool access · read/write disk state · run unattended/headless · structured output (for steps with no live MCP). On Claude Code these are the Skill / Agent / Bash / Write / Edit tools; on Codex they are skills / `spawn_agent` / `apply_patch` / shell. The hyperresearch patterns map one-to-one.

### What needs a per-runtime adapter (2 of 9 — and both are symmetric)

1. **Git-worktree isolation per code-writing sub-agent.** A "worktree" is a separate checked-out copy of the repo so two workers editing files never collide. Neither Codex *nor* Claude Code hands a sub-agent a fresh worktree automatically — it's the orchestrator's job on both. HyperWilfred creates one worktree per build/improve sub-agent and dispatches with that as the working directory (`--cd` on Codex, working dir on Claude). Read-only workers (research, critics) need no worktree. (OpenAI Symphony already does per-task repo cloning into isolated worktrees and is a valid delegation target, but HyperWilfred does not need to adopt the full Symphony/AgentOS/Mastra stack to get this.)
2. **Durable resume after a crash.** Neither runtime offers true mid-run checkpoint recovery — both persist a *conversation* you can continue, not a *killed step* you can resume. HyperWilfred gets durability the hyperresearch way: every step writes a named artifact to disk; on restart the router scans for the first missing artifact and resumes there. Durability is a property of HyperWilfred's disk state, not of any runtime's session log. (Two engineering requirements make this safe: every engine artifact is written atomically — temp file then rename — and the resume scan validates each candidate artifact against its L1 JSON Schema before treating it as "done", so a step killed mid-write leaves a truncated file that resume re-runs rather than silently skips.)

### The one hard design rule Codex forces

**Codex Issue #15451:** when MCP servers are live, Codex's `--json` / `--output-schema` structured-output flags are silently ignored and can emit malformed output. **Therefore: never depend on a Codex step's schema'd stdout when that step also has MCP tools active.** Instead, every step *writes its structured result to a known disk path* and the router reads it back. This works identically on both runtimes and sidesteps the bug entirely — and it happens to be the same disk-as-source-of-truth discipline the whole architecture already wants. This is an *enforced engine rule*, not a convention: the Codex adapter always reads a worker's result from a known disk path, never from stdout, whether or not MCP is live. (Bug-fix status on the founder's local CLI is unverified — confirm with `codex --version` in Phase 0 — but the disk-read rule makes it moot either way.)

**Plain-Spanish closing:** HyperWilfred puede correr en Codex y en Claude Code sin recortar calidad. Nada está bloqueado. Solo hay que construir dos "traductores" finos (uno por herramienta) y poner toda la inteligencia en un motor compartido. La regla de oro: el estado vive en disco, no en la memoria de la sesión.

---

## 2. THE ARCHITECTURE

### The shape: one brain, two thin translators

The single most important decision is **where to draw the line between WHAT the system does and HOW a given tool executes it.** Everything that determines final quality — how work is broken down, sequenced, checked, and repaired — lives in one shared program (the engine). Only two things genuinely differ between Claude Code and Codex — how you spawn parallel workers, and how you enforce a sandbox — and those live in thin adapters. This **inverts the usual cross-runtime trap**: instead of dumbing the system down to features both tools share (the "lowest common denominator"), we put all intelligence in the engine (the identical program on both sides) so it is never dumbed down. The common denominator is raised, not lowered. **There is no quality tax for being cross-runtime.**

### The four layers

**L0 — Portable Engine (`hyperwilfred-core`, a plain CLI).**
The single source of orchestration truth, runnable as a standalone command-line program (exactly the hyperresearch "CLI-as-engine" move). It owns: objective intake + tier classification; the BUILD+IMPROVE pipeline graph and step sequencing; all on-disk run state (`.hw/<run_id>/state.json`, `plan.json`, `slices.json`, `evidence/`, `events.jsonl`); the TaskSpec + risk-level contract; the deterministic evaluators (lint/type/test/coverage/doc) and the 0-5 patch-don't-regenerate evaluator; `guardrails.json` generation + rollback-on-escape; cost accounting; and the **fan-out PLAN** (it decides what parallel work exists and emits N self-contained sub-agent task packets). **Crucial invariant: the engine never calls an LLM and never spawns a sub-agent.** It computes "what should happen next," persists it, and hands packets to the active adapter. Because it is literally the same process on both runtimes, its decisions are byte-identical. Subcommands: `hw plan`, `hw next-step`, `hw fanout-plan`, `hw record-result`, `hw evaluate`, `hw rollback`.

**L1 — Shared Contracts (the runtime-neutral spec).**
The frozen interface every layer agrees on, so neither adapter can drift: JSON Schemas for TaskSpec, the sub-agent task packet, evaluator results, pipeline events, and `guardrails.json`; the canonical `AGENTS.md`/`CLAUDE.md` (one source, both emitted) governing protected paths and risk gates; and **the BUILD+IMPROVE step-instruction markdown files** — the code equivalent of hyperresearch's 16 step skills, loaded fresh into context only when their step runs. Because procedures live as plain markdown + JSON on disk (not buried inside Claude-Code skill internals or Codex TOML), they are 100% portable. This is the structural move that prevents the "Claude-Code-only" trap.

**L2a — Claude Code Adapter (thin).**
A router skill (like the hyperresearch entry skill) that each turn calls `hw next-step` via Bash, reads the returned step markdown fresh into context, executes it, and for fan-out steps calls `hw fanout-plan` then spawns the N packets as parallel `Task()` sub-agents (each with its own clean context + tool-lock). Maps `guardrails.json` onto Claude Code PreToolUse/PostToolUse hooks + permissions for **real-time scope enforcement** (it can block a bad write *before* it lands).

**L2b — Codex Adapter (thin, symmetric).**
A Codex skill/custom-prompt driving the *same* engine: `hw next-step` via Codex's shell, reads the same step markdown, and for fan-out emits Codex's explicit "spawn one sub-agent per packet, wait for all, summarize each" delegation with one TOML agent def per role. Maps `guardrails.json` onto Codex sandbox modes (`workspace-write` + network allowlist) + git-lifecycle hooks for scope enforcement and rollback. **No engine logic is reimplemented here — that is the whole point.**

### The capability matrix, distilled

| Primitive | Claude Code | Codex | Verdict |
|---|---|---|---|
| Load a step fresh | `Skill` tool | read step file before executing | portable |
| Isolated sub-agent context | `Task`/`Agent` | `spawn_agent` (GA) | portable |
| Parallel fan-out | many `Task` in one turn | many `spawn_agent` / `spawn_agents_on_csv` (GA) | portable (cap ~6 on Codex by default) |
| Git-worktree isolation | orchestrator-owned | orchestrator-owned | **adapter (symmetric)** |
| MCP tools | native | native | portable |
| Disk state I/O | Read/Write/Edit/Bash | shell + `apply_patch` | portable |
| Durable resume after crash | artifact-scan (HW-owned) | artifact-scan (HW-owned) | **adapter (symmetric)** |
| Unattended/headless | `claude -p` / scheduled-tasks MCP | `codex exec` + cron/CI | portable (scheduler shim differs) |
| Structured output | StructuredOutput tool | `--json`/schema — **breaks with MCP live** | portable for non-MCP; else write to disk |

Two Codex-specific ceilings to design around (do not assume they match Claude, and both are UNVERIFIED until the Phase-0 spike measures them): **default concurrency ~6** and **default nesting depth 1**. The engine therefore owns **explicit multi-wave batching** — it never emits a single naive flat wave and assumes it fits. A step needing 12 workers becomes 3 sequential waves of 4 on a cap-6 runtime (slower, but isolation preserved). Critically, any step where a sub-agent would itself spawn workers (e.g. a draft-orchestrator that fans out to readers) is re-expressed by the engine as a **two-level plan it flattens into sequential top-level waves** — the sub-agent must NEVER read large source bodies inline in its parent context, because that is exactly the monolithic-context growth (the V7 compaction failure) the whole architecture exists to avoid. "No depth lost" is therefore something the Phase-0 spike must *measure* (orchestrator-context growth across a full Codex run with the cap in force), not an assumption.

### BUILD mode and IMPROVE mode

Hyperresearch routes a query to *light* vs *full*. HyperWilfred's analog is a **two-axis tier router**: BUILD vs IMPROVE × light vs full.

- **BUILD** (greenfield: vague objective → new product). Full path: frame → shape → breadboard → slice → task-contract → (parallel) implement → evaluate → patch → verify → ship. The legacy DAG already covers most of this — *after* stripping the consulting stages (Accounting, PM, client-comms).
- **IMPROVE** (existing codebase → targeted change). **This is genuinely net-new — nothing audited models it.** It needs four stages no existing pipeline has: (1) **comprehend** the existing repo, (2) **locate the change site**, (3) **scoped diff** against the existing code, (4) **regression guard** (prove existing tests still pass). IMPROVE shares the *same* evaluator + gate + patcher core as BUILD — only the front of the pipeline differs.

### The autonomy loop — how IMPROVE feeds BUILD

The self-improving half closes the loop the way the founder's Karpathy-style "propose → test → keep/discard" loops already work elsewhere. After each run, a **learning analyzer** (adapted from `learning_loop.py`) mines the on-disk run history (`events.jsonl`, evaluator failures, patch cycles) and emits **review-only proposals**: tighten this guardrail, add this acceptance criterion, fix this recurring prompt weakness, cover this test gap. These proposals are themselves IMPROVE objectives — so HyperWilfred can be pointed at *its own* step-instruction files and guardrails to harden them, with a human approving before anything changes. IMPROVE feeding BUILD is therefore not a special mechanism; it's the same pipeline turned on the factory itself. This stays **review-gated, never auto-applied** (constraint #2: quality is paramount; the factory does not silently rewrite its own rules).

### Quality lessons from the pilot runs (2026-09-19)

Evidence and diagnosis: `docs/findings/pilot-cockpit-wilfred-vs-ecc-2026-09-18.md`; change record:
`docs/findings/hyperwilfred-quality-lessons-pilot-agent-report.md`. The engine now enforces, in code:

- **Review provenance** (`quality.py`): every evaluation/critique carries `author: independent |
  adapter` (missing = adapter). `hw quality-gate` returns `pending_independent_review`
  (`PENDING_INDEPENDENT_REVIEW`) instead of pass/fallback while any required lens of the final
  iteration is adapter-authored or missing; a human may `hw quality-override` (who/when/why, shown
  in verify output and on the ship HITL gate).
- **Adversarial lens** (`02b_adversarial.md`): runs hostile inputs in temp roots; default-on when
  the objective mentions personal data, gates, approvals or security; configurable via
  `objective.critic_lenses`; findings must cite command + observed output.
- **Opt-in read-only lenses** (`02_critique.md`): `cross-slice-drift` (mismatches between slices:
  units, state assumed to exist, fallbacks, renamed fields, docs vs code) and `ai-failure-modes`
  (secrets reaching the client, authorization trusting client values, untrusted input in LLM
  instructions, model output trusted as code). Never automatic; `hw review --lens` adds them to
  the four standard REVIEW lenses (completion then needs exactly that panel) and a BUILD/IMPROVE
  objective may name them in `critic_lenses`. Critics read only the target files.
- **REVIEW authorship** (`review.py`): the report records who wrote each critique
  (`critic_authors`, same attestation check as the quality loop); a panel that is not fully
  independent never reports `pass`/`advisory` — the verdict is `pending_independent_review` and
  the findings-only result is kept in `severity_verdict`.
- **Severity floor** (`severity_floor.py`): personal-data leaks, gate/permission bypasses and
  UI-vs-engine divergences are at least `major` and gate like blockers.
- **Verify checks** (`verify_checks.py`): requirement traceability (`traceability.py`),
  reproducibility of declared generated artifacts in a fresh worktree (`reproducibility.py`), and
  the live-demo check (`live_demo.py`) — a blocking result refuses `complete-step verify`.
- **Plan lint** (`plan_lint.py`): warns on re-implemented rules and rules cut off from their
  publication point; blocks at high/critical risk. `max_files` is a ceiling, not a target.
- **Guardrails**: declared `generated_artifacts` (path + required regeneration command, data-file
  extensions only, inside the slice scope) are exempt from the full-file-replacement rule; source
  files keep the protection.
- **Lessons** (`learning.py`): L1–L9 ship as curated lessons attached to the relevant step packets.
- **Learning evidence** (`learning.py`): each proposal states "N of M observed runs", flags a
  single-run signal as possible noise and labels its confidence a heuristic (not a probability).
  After promotion, `hw learn` reports the share of finished BUILD/IMPROVE runs flagged before vs
  after the fix (`effect_after_promotion`; a count, not proof). An unreadable ledger item stops
  `hw learn` instead of being overwritten.

### Guardrails and rollback — the safety envelope

Quality and safety are enforced at **three** points, with the **engine as the authoritative gate** so neither runtime gets a weaker floor:

1. **Write-time (adapter, defense-in-depth):** Claude PreToolUse hooks block a bad write before it lands; Codex sandbox + git-lifecycle catches it at the workspace boundary. Claude's is strictly stronger, which is why it is *not* the only line.
2. **Acceptance-time (engine, authoritative):** `hw record-result` / `hw evaluate` check the result against `guardrails.json` (allowed/blocked write-paths, max files, max lines) **before any result is accepted**, on both runtimes identically. Escape → engine-level git rollback that leaves the tree clean. This is the S42-S43 enforcement built as real engine code, not a hopeful prompt.
3. **Human gates (disk-file approval, cross-runtime by construction):** before any **irreversible** action — deploy, merge to main, dependency bumps, anything touching secrets/migrations/payments — the engine writes a `.pending` gate file and blocks until a human approves (a bare file `touch` works from any terminal on any runtime). This merges legacy's HITL gate mechanism with ai-common's blocked-by-default protected-paths list, retargeted to software irreversible actions.

The deterministic gates are the no-compromise floor: an LLM evaluator alone ships *plausible-but-broken* code. **A failing deterministic gate (build/test/security) must HARD-BLOCK, never "advance with best effort"** — this corrects the one legacy behavior that conflicts with "quality is paramount" (its evaluator advances with the best version after 5 failed iterations; for software that fallback is unacceptable on objective gates).

**Precedence rule (resolves the contradiction the 0-5 loop creates):** deterministic gates are a hard wall — no advance, ever; on failure the engine escalates to a human gate. The 0-5 evaluator's "best version after N iterations" fallback applies ONLY to *subjective* quality criteria no deterministic gate can judge — never to build/test/security.

**Patch-never-regenerate has a weaker floor on Codex, so the engine — not the runtime — is the guarantee.** On Claude Code the patcher physically lacks the Write tool, so a wholesale file rewrite is *impossible*. On Codex, `apply_patch` *can* replace a whole file, so the engine enforces the invariant at acceptance time: `guardrails.json` carries a per-patch max-changed-lines and max-hunk-span, and the engine HARD-REJECTS any patch whose diff looks like a full-file replacement (deletions ≈ original line count), re-dispatching the same packet with a tightened "surgical-edit-only, structural rewrites are escalated not applied" instruction — never a silent rollback-then-advance. On Codex the patch worker also runs under a profile that denies file creation/truncation. Document plainly that Codex's *write-time* floor is lower than Claude's; the engine-level reject is what makes the guarantee equal.

---

## 3. HONEST RECYCLE TABLE

Verdicts: **core** = keep and build directly on · **adapt** = keep the pattern, clean the implementation · **drop** = rebuild or discard, even though it exists.

**Non-negotiable build rule (the import trap).** Every "core"/"adapt" keeper is **forked as standalone source into `hyperwilfred-core`, never imported from `common_py.factory`.** The existing `factory/__init__.py` eagerly imports the entire dropped Linear/Symphony spine (backlog_compiler, coordinator, decomposer, escalation_handler, intake…), so a single `from common_py.factory.conflict_planner import …` drags all the baggage back in. Copy the ~6-8 keeper files, sever the package, and add a CI guard that fails if `hyperwilfred-core` imports anything under `common_py.factory.*`. Here, "reuse" means *fork-and-clean*, not *depend-on*. For `durable.py` and `run_store.py` specifically this means "lift the ~30-line pattern, rewrite the file" — not "import the module".

| Asset | Verdict | No-compromise reason |
|---|---|---|
| **session-driven orchestration contract** (legacy doc) | **core** | The literal hyperresearch shape *and* the dual-runtime answer in one doc — provider-neutral, plan/validate/advance, disk state, explicit Codex section. Adopt as the backbone. |
| **evaluators/** (ruff/mypy/pytest/coverage/doc) | **core** | The strongest, cleanest, most runtime-agnostic asset in the stack: subprocess-isolated (version drift can't break it), exception-proof, timeouts. The objective-truth quality gate. Non-negotiable keep. |
| **legacy deterministic gates** (build/test/security/audit/smoke) | **core** | The objective-correctness layer an LLM evaluator cannot provide. Zero consulting baggage. Generalize npm-specific gates to a per-stack registry; keep the chain. |
| **legacy evaluator(0-5, MIN-not-avg) + separate surgical patcher** | **core** | Independently-arrived-at adversarial-critic + patch-never-regenerate design. MIN-not-average gate is a real quality lever. Replace consulting rubrics with software-quality rubrics; keep the loop shape. |
| **conflict_planner.py** | **adapt (pattern only)** | Keep the IDEA — group independent work into parallel waves — but base the waves on the **decompose step's declared per-item scope** (allowed/blocked paths), not its predicted-changed-paths + coarse top-3-segment key. Once each worker runs in its own git worktree, same-dir collision (its reason to exist) is already prevented; the real hard problem — integration-time merge conflict — it does not model. Lift the design, rewrite the file. |
| **run_store.py** | **core** | Clean, dependency-light on-disk run+event ledger = the "state on disk" substrate. Decouple it from the verifier report so it stores any stage's evidence. |
| **disk checkpoint/resume** (legacy `.harness` advance) | **core** | State-on-disk + resume is exactly what lets work move *between* Claude Code and Codex. Make `next_agent` derive from the DAG so parallel phases resume correctly. |
| **verifier.py** (evidence-only claim verifier) | **core** | Runtime-agnostic, typed claim-by-claim delivery report routing sensitive failures to human review — a structural critic gate. Replace the weak substring evidence-match with the LLM critics; keep the report model + human-review routing. |
| **JSON-contract machinery** (dataclass + sync-into-instructions + golden-fixture CI) | **adapt** | Keep the typed-versioned-validated-I/O machinery; **re-author the schemas around code artifacts** (plan, file-change manifest, diff, test-plan, review-findings, gate-results) instead of consulting deliverables. Prefer language-neutral JSON Schema so Codex validates identically. |
| **sync-agent-docs.py** (one source → CLAUDE.md + AGENTS.md) | **core** | This *is* the cross-runtime emitter. Extend it to also emit the per-runtime adapter manifests. |
| **protected_paths.py** | **core** | The read-side guardrail (sensitivity classifier feeding human-review). It is HALF the story — keep it; build write-time enforcement + rollback *around* it. |
| **schemas/task.py (TaskSpec / risk_level)** | **adapt** | Keep RiskLevel/ExecutionMode/acceptance_criteria/constraints as the gating contract; **strip** the Notion/Linear/origin/domain fields (task_type defaults to B2B compliance values — a smell). |
| **sw-agent durable.py (env-guarded DBOS wrapper + pure fallback)** | **adapt** | The exemplary "rent durability, keep the moat" template: durability only when configured, pure path always works. Adopt the pattern; re-granularize from cron-session to build-wave. |
| **sw-agent orchestrator.py (HALT-not-cascade dependency gate)** | **adapt** | The halt-without-cascade inter-step gate is a clean, correct primitive. Lift the gate function + handoff concept; **drop** SessionSpec/manifest/offset_minutes/self-disable cron framing. |
| **learning_loop.py** | **adapt** | The propose→review→keep/discard analyzer is the IMPROVE-half engine. Heavyweight and tuned to "the owner-reviews"; adapt the analyzer→proposal→review-packet pattern as a *phase-2* layer, not day-1 core. |
| **error_classifier.py + retry_handler.py** | **adapt** | Clean, generic transient/permanent classification + backoff — sub-agents will hit rate limits. Fix the line-50 operator-precedence bug on lift; do not copy verbatim. |
| **product_verification.py** (smoke + screenshot evidence) | **adapt** | Real need: "did the product actually run." Adopt the smoke-runner + screenshot evidence; **tighten** the broken `ProductVerificationStatus` enum (latent bug). |
| **feedback_handler.py (HITL decision vocab)** | **adapt** | Reuse the approve/reject/modify-scope enum; **rebuild** storage to persist to run_store (it's in-memory only today — decisions evaporate on exit). |
| **ai-common Symphony software-factory** (closed-argv recipes, .runs evidence, canary ladder) | **adapt** | Borrow the *concepts* — closed-command execution, evidence-in-.runs, blocked-by-default protected paths, staged-canary trust ladder. **Do NOT adopt the four-platform (Symphony+AgentOS+Mastra) stack wholesale** — that violates "no compromise / don't keep it just because it exists." |
| **recipe_catalog.py** | **drop** | Bound to the dropped recipe-runner; default entries are self-referential ("run pytest on test_factory_*"). Lift only the `_validate_argv` shell-injection guard into the command-safety layer. |
| **factory/orchestrator.py (recipe-runner)** | **drop** | Well-tested but NOT hyperresearch-shaped: deterministic shell-recipe runner, no AI, no fan-out, no tiering. Keep the *idea* (disk-state progress + dependency-ordered selection + dry-run/execute split); rebuild the class. |
| **decomposer.py** | **drop** | Toy keyed to B2B compliance (discovery/policy_check → S9/S10/S11 spokes). Nothing transfers. Replace with an AI decompose step. |
| **backlog_compiler.py (67KB)** | **drop** | Largest module, entirely in service of the dropped recipe-runner (emits progress.json/recipes.json with S12 slices). Anchors HyperWilfred to the time-discretized model. Drop. |
| **task_spec.py (Linear parser)** | **drop** | Despite the name, a 100% Linear-issue parser hard-wired to "Symphony Ready" state. No Linear in HyperWilfred. Reuse the *clean* TaskSpec schema instead. |
| **task_executor.py** | **drop** | Linear-poll + slash-command pull workflow — the opposite of "founder types one objective." Pure baggage. |
| **symphony_* / linear_state_updater / mastra_adapter / intake / approvals / coordinator (9+ modules)** | **drop** | The exact ticket-pull, policy-gated, scheduled-session Linear/Symphony spine HyperWilfred exists to replace. Mints `payments:FACTORY` claim ids, requires Linear labels. Drop the whole spine. |
| **escalation_handler.py** | **drop** | Hard-coupled to Linear comments. Replace with a runtime-neutral escalation sink (write to run_store + notify founder). |
| **harness-driven legacy orchestrator.py (`claude -p`)** | **drop** | Claude-Code-only (`find_claude_binary`), harness-driven (fails the dual-runtime constraint), hardcoded consulting stages. **Recycle the eval_loop + verify_outputs + read-score-from-disk PATTERNS only.** |
| **legacy consulting stages** (Accounting / PM / client-comms) | **drop** | Serve quoting/billing a client, not software quality. Not an optional module — woven into the DAG, so excise carefully. |
| **legacy per-agent model routing** | **adapt** | Keep role→tier heuristic in the envelope; **abstract `model_tier` to a provider-neutral tier** ("reasoning"/"standard"/"cheap") each runtime resolves to its own catalog. Hardcoded model names (Opus 4.6/Sonnet 4.6) are stale and Claude-specific. |
| **legacy observability** (anomaly/alert/dashboard + retrieval) | **adapt** | Adopt the disk-observing anomaly/alert/dashboard stack (retarget signals to code: "patch loop maxed," "diff >> spec"); retarget retrieval from client case-studies to codebase memory. Secondary to the core loop. |

**Conflict resolution (where audits diverged).**
- **`protected_paths.py`:** the factory audit and the brief both flag it as "only half the guardrail." I rule **core** (not adapt/drop) because the read-side classifier is genuinely reusable as-is; the *missing* half (write-time block + rollback) is a separate net-new item, not a reason to downgrade the existing module. Keep it, build around it.
- **`learning_loop.py`:** the factory audit says **adapt, later-phase**. I concur and make it explicit: it is the IMPROVE-half engine but **phase-2**, not day-1 — front-loading it would delay the working build+improve loop.
- **Symphony/ai-common factory:** the factory audit says **drop** the Symphony spine; the legacy audit says **adapt** the ai-common factory's *concepts*. These are not in conflict — they describe different things. Verdict: **drop the Symphony/Linear orchestration spine** (modules), **adapt the sandboxing/evidence/canary concepts** (discipline), and **do not import the four-platform stack**.

---

## 4. WHAT IS GENUINELY NET-NEW

A short list — most of HyperWilfred is recycled-and-cleaned. These few pieces have no existing shape and must be built fresh:

1. **The AI-native thin-router orchestrator (the HyperWilfred core).** No existing factory code does this — the existing orchestrator is a deterministic recipe-runner with no AI and no fan-out. This is the `hw` CLI: classify tier, sequence steps by loading each step's procedure fresh, emit fan-out packets, never call an LLM itself.
2. **The BUILD+IMPROVE step-instruction set + the IMPROVE branch.** Hyperresearch has a research graph; HyperWilfred needs a *build/improve* graph. The IMPROVE branch in particular (comprehend repo → locate change site → scoped diff → regression guard) is unmodeled anywhere in the audited code.
3. **The AI decompose step.** Turn one founder objective into atomic build items + dependency graph + acceptance criteria + per-item allowed/blocked scope — modeled on hyperresearch step 1, replacing `decomposer.py` + `backlog_compiler.py`.
4. **The full S42-S43 guardrail enforcement (write-allowlist + write-time block + rollback-on-escape).** `protected_paths.py` only *detects* post-hoc. This is the defensible moat: machine-readable allowed/blocked write-paths + max files/lines, enforced at write-time and at engine acceptance-time, with a clean rollback on escape — built as real engine code, not a prompt template.
5. **Adversarial code critics + patch-never-regenerate loop.** LLM critics that read the built diff and emit findings JSON, plus a patcher tool-locked to surgical edits (Read+Edit only on Claude; restricted-profile + `apply_patch`-diff-guard + git-diff verification on Codex). Quality compounds without churn.
6. **The dual-runtime spawn + tier-resolution abstraction.** A thin `spawn_agent(packet, tier, tool_allowlist)` interface with two backends (Claude `Task` / Codex `spawn_agent`) and an abstract-tier → concrete-model resolver per runtime. Highest-uncertainty design question — validate with a thin spike on both runtimes *first*.
7. **A clean build-objective contract + runtime-neutral escalation sink.** The slimmed TaskSpec (risk_level + execution_mode + acceptance_criteria + scope, Notion/Linear/domain stripped) and a notify-the-founder escalation that replaces the Linear-comment handler.

---

## 5. SEQUENCING

Front-load a working build+improve loop on ONE runtime; harden autonomy, durability, and the second runtime later. Each phase ends with something that *works*, not a half-built layer.

**Phase 0 — De-risk the riskiest assumption (days, not weeks).**
A thin spike: prove the same flat fan-out plan + tool-locked patcher runs on **both** Claude Code (`Task`) and Codex (`spawn_agent` + `--cd` worktree). Confirm the Codex CLI version against Issue #15451 (`codex --version`); confirm `agents.max_threads`/`max_depth` raises actually lift the ceiling. **Goal: kill the dual-runtime uncertainty before building the pipeline on a false assumption.**

**Phase 1 — The portable engine + one runtime, BUILD path (the spine).**
Stand up `hw` (plan/next-step/fanout-plan/record-result/evaluate/rollback) on `run_store` + `evaluators` + `conflict_planner` + the cleaned TaskSpec. Build the L1 step-instruction markdown for BUILD. Wire the Claude Code adapter (router skill + `Task` fan-out + hooks). **Precondition for any code-writing fan-out (pulled forward from Phase 3):** the MINIMUM guardrail ships in *this* phase — machine-readable allowed/blocked write-paths + engine-acceptance-time rejection + git rollback-on-escape. No sub-agent writes to a real repo before this floor exists; the richer real-time write-time hooks can wait for Phase 3, the engine-level reject cannot. **Goal: type a greenfield objective, get a built product that passes the deterministic gates, on Claude Code, with the repo protected from out-of-scope writes.**

**Phase 2 — The quality loop + IMPROVE path.**
Add the 0-5 evaluator + surgical patcher + adversarial code critics (patch-never-regenerate). Add the IMPROVE branch (comprehend → locate → scoped-diff → regression-guard) sharing the same evaluator+gate+patcher core. Add the per-stack gate registry. **Goal: HyperWilfred both builds new and improves existing code, with quality gated by the engine, not the host model's first draft.**

**Phase 3 — Full real-time guardrails + human gates (trust to touch real repos).**
The engine-level guardrail floor already shipped in Phase 1. This phase adds the *richer* enforcement: Claude PreToolUse write-time blocking + Codex sandbox/git-hook compilation around `protected_paths`, and the disk-file HITL gates for irreversible actions (deploy/merge/deps/secrets/migrations). **Goal: HyperWilfred can be pointed at a real product repo unattended without risk of an unreviewed dangerous change.**

**Phase 4 — Durability + the Codex adapter (true cross-runtime).**
Add the env-guarded durable wrapper (DBOS pattern from `durable.py`) + artifact-scan resume. Build the L2b Codex adapter (skill + explicit delegation + TOML sandbox + git-hook guardrail compiler). Extend `sync-agent-docs.py` to emit both adapter manifests. **Goal: a long build survives interruption and resumes on *either* runtime — start in Claude Code, finish in Codex.**

**Phase 5 — The IMPROVE-feeds-BUILD autonomy loop + observability.**
Add the `learning_loop`-derived analyzer emitting review-only proposals (which are themselves IMPROVE objectives), and the retargeted anomaly/alert/dashboard + codebase-memory retrieval. **Goal: HyperWilfred proposes hardening to its own guardrails/prompts/tests, human-approved — the self-improving factory.**

**Why this order.** It front-loads a *usable* build+improve loop (Phases 1-2) so the founder gets value early, defers the highest-uncertainty cross-runtime work behind a Phase-0 spike that de-risks it, and treats durability + the second adapter + self-improvement as hardening on a system that already works — never blocking the first win on the hardest layer.

**Plain-Spanish closing:** Primero construimos el cerebro compartido y lo hacemos funcionar en una herramienta (Claude Code) para que escribas un objetivo y obtengas software que pasa los controles de calidad. Luego añadimos el bucle de mejora, las barreras de seguridad para tocar repos reales, y al final la segunda herramienta (Codex) y la auto-mejora. Cada fase deja algo que ya funciona; nunca arriesgamos la primera victoria por construir lo más difícil primero.
