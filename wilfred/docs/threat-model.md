# Threat model, trust zones and privileged-route registry (P1.1)

Status: **reviewed reference for Phase 1.** Written 2026-10-09 against base commit `ecdcb66`
of `codex/wilfred-product`. Registry re-run 2026-10-24 on `acb54bb` (Fable review 5 fixes +
unification U0–U2): see §5.3 and §6.1. This document changes no code. Where the code and this document
disagree, the code is the fact and this document is wrong; the registry in §5 must be re-run
whenever runtime code changes, and in particular after the Gate 0 candidate is integrated
(that candidate changes `regression.py` and `known_failures.py`).

Scope: the installed `hyperwilfred_core` package and its `hw` command line, the packaged step
instructions and skills, and the Agent Foundry generated-worker runtime. The companion
[domain model](domain-model.md) defines the events that every route below must eventually emit.

## 1. Security goals

| Goal | Roadmap link |
|---|---|
| G-A A model or worker can propose but never authorize a privileged effect | R4 |
| G-B Every human approval is bound to the exact action, evidence, run and expiry, and is single-use (today: evidence identifiers only, see T2) | R4 |
| G-C Completion is reconstructable from recorded evidence, not from model prose | R5 |
| G-D Generated workers cannot reach the network, host secrets, signing authority or files outside their bundle | R4, R6 |
| G-E Secrets and private keys are never worker inputs and never written to run artifacts | R1, R6 |
| G-F Client project data reviewed by Wilfred stays local and is not mixed across projects | R6 |

## 2. Principals and trust zones

| Zone | Principal | Trust | What it can do today |
|---|---|---|---|
| **Z0 owner-offline** | the owner (owner, signer) | Root of authority | Holds the passphrase-protected Ed25519 private signing keys; signs approval challenges in his own terminal. Location: the owner's application-support folder for Wilfred (`~/Library/Application Support/Wilfred/`). Wilfred code only reads the **public** keys there; the private key files were not read for this document |
| **Z1 host** | Wilfred code (`hw`) running as the owner's macOS user | Trusted infrastructure, but **not** a trusted decider | Reads/writes the target repository and `.hw/` run roots, runs git, pytest, ruff, mypy, node, Codex CLI; verifies signatures; executes the trusted host callback |
| **Z2 coordinator** | The launching Claude Code or Codex session that drives `hw` | Untrusted for authority; trusted only to run commands | Has the owner's full user permissions through its own tools (shell, git, `gh`), independent of Wilfred. Submits worker results, usage, review records |
| **Z3 worker-sandbox** | Agent Foundry generated runners under `/usr/bin/sandbox-exec` (deny-default profile) | Untrusted | Reads its bundle and the Python runtime; no writes, no sockets, no child processes; talks to the host only over the JSON-line broker |
| **Z3' dispatched workers** | Codex/Claude sub-agents launched by the coordinator for build packets | Untrusted | Constrained by the coordinator's sandbox settings and by Wilfred's compiled hooks/guardrails, which run in the same user account |
| **Z4 provider** | Model providers reached through the authenticated Codex CLI | External, semi-trusted for availability only | Receives prompts and documents; returns text. Its output is a claim |
| **Z5 external** | GitHub (the owner's repositories), package indexes, the scout search endpoint | External | Stores code and PRs; public search results are untrusted input |
| **A1 same-user attacker** | Any other process running as the owner's user (another session, a compromised tool, a malicious dependency) | Hostile, **out of scope for prevention** (see §7) | Can edit Wilfred's installed source, `.hw/` state, the policy switch file and the **public** key files |
| **A2 other sessions on the same Mac** | Parallel scheduled Claude/Codex sessions | Same account as A1 | Can touch the same repositories, worktrees and `.hw/` folders; Wilfred does not support concurrent writers for one project |

Key consequence: **Z1, Z2, A1 and A2 share one operating-system identity.** Every boundary
between them is a convention enforced by Wilfred's own code, not by the operating system. Only
Z3 (sandbox-exec) and Z0 (private key held offline behind a passphrase) are real boundaries.

## 3. Data classes

| Class | Examples | Location | Handling rule |
|---|---|---|---|
| D1 Source code | Target repositories, worktrees | Owner's project folders | Read and written by Z1/Z2/Z3'; never sent to Z4 except as prompt context chosen by the coordinator |
| D2 Run artifacts | `state.json`, `events.jsonl`, plans, packets, results, verdicts, Foundry `artifacts/` and `generated-*` | `<repo>/.hw/<run_id>/`, Foundry project root | Integrity-relevant; content-addressed where Foundry publishes; otherwise plain files |
| D3 Baselines and fingerprints | `regression_baseline.json` (passing ids + test-file hashes), known-failures declaration at `base_commit` | Run root; committed declaration | Integrity-critical: a weakened baseline lets a regression pass |
| D4 Approval records | challenges, signed decisions, consumed receipts, `.executed` markers, policy decisions, review attestations | `<run>/gates/`, `<run>/review-attestations/` | Integrity-critical; single-use |
| D5 Private signing keys | Approval and review-attestation private keys | Owner's Wilfred application-support folder (Z0) | Never read by Wilfred, never a worker input, never logged. Not read for this review |
| D6 Public keys and policy switch | `approval-public-key.pem`, `review-attestation-public-key.pem`, `policy.json` | Same folder | Integrity-critical and **writable by A1** (§7) |
| D7 Provider credentials | Codex CLI authentication, any API keys in the environment | Owner's home / environment | Must not reach Z3; see env-inheritance column in §5 |
| D8 Client project data | Code, data and documents of the projects Wilfred works on | Their own repositories | Local only; when reviewed, prompts may carry excerpts to Z4; no cross-project memory today |
| D9 Session transcripts | Claude Code transcripts read by `hw record-usage` | `~/.claude/projects/…` | Read-only input for token counts; may contain any of D1–D8 |

## 4. Threats considered (summary)

| ID | Threat | Primary control today |
|---|---|---|
| T1 | Worker or model self-approves a protected action by writing JSON | Ed25519 signature over a challenge-bound payload (`approvals.verify_and_consume`) |
| T2 | Replay of an old approval for a new action or changed evidence | Nonce, action digest and digest of the evidence **identifiers** (not their content: files can change after signing, `gates.py:243-246`, `approvals.py:165-167`), expiry, single-use consumption marker (except test-evolution, §5.1) |
| T3 | Generated worker exfiltrates data or reaches the network | sandbox-exec deny-default profile, empty environment, no sockets/children |
| T4 | Dispatched worker writes outside its packet scope | Compiled PreToolUse hook, Codex pre-commit hook, acceptance guardrail with revert |
| T5 | Weakening tests or baselines to make a regression gate pass | Regression baseline of passing ids + test-file hashes; test-evolution review |
| T6 | Coordinator claims an independent review that was not independent | Signed review attestation; unattested reviews reported as not independent |
| T7 | Coordinator performs an irreversible effect (push, merge, deploy) without approval | **Not mediated by Wilfred** — see §5 findings |
| T8 | Secrets copied into run artifacts or Foundry records | `_reject_secrets` pattern scan on Foundry state/artifacts/events |
| T9 | Crash during publication corrupts accepted evidence | Atomic temp-file-then-rename, content-addressed immutable revisions |
| T10 | Untrusted external search results steer reuse decisions | Scout rejects unlicensed/unpinned candidates; results are evidence, not instructions |

## 5. Privileged-route registry

Method: every `subprocess`/`Popen`/`sandbox-exec` call, every git mutation, every network
client, every write outside `<repo>/.hw/<run_id>/`, every read of owner configuration and every
approval check in `src/hyperwilfred_core/` was located by search and confirmed by reading the
cited lines (survey 2026-10-09; the critical rows were re-read by the author).

Classification:

- **Mediated** — the effect runs only after a signed, challenge-bound approval verified on the
  same path (cite).
- **Policy-mediated** — mediated by an unsigned `policy:auto` decision while the owner switch is on.
- **Blocked** — the path refuses or fails closed (cite).
- **Out of scope** — read-only, unreachable from the `hw` command line, or confined to the run
  root; recorded so that a later change makes it visible.
- **Unmediated (finding)** — an external or privileged effect with no approval and no block.
  Each one maps to a finding in §6.

Env = environment given to the child process: *inherit* (all of the owner's environment,
including any credentials) or *filtered* (explicit allow-list).

| ID | Route (file:line) | Reached by | Effect | Zone crossing | Env | Classification |
|---|---|---|---|---|---|---|
| PR-01 | `guardrails.py:37,151` `_git` (status/diff/show) | `record-result`, `record-patch`, quality commands | read git state of a caller-named worktree | Z1→D1 | inherit | Out of scope (read-only) — but see F-03 on repository-config code execution |
| PR-02 | `guardrails.py:241-242` `rollback` (`git reset --hard`, `git clean -fd -e .hw`) | `hw rollback --worktree X` | destroys uncommitted and untracked work in any path X | Z1→D1 | inherit | **Unmediated (F-05)** |
| PR-03 | `guardrails.py:327-328` `accept`→`rollback` | `record-result`, `record-patch` on rejection (unless `--no-rollback`) | same destructive reset/clean; `--worktree` not checked against registered worktrees | Z1→D1 | inherit | **Unmediated (F-05)** |
| PR-04 | `quality.py:199` `_changed_product_paths` | quality commands | `git diff --name-only` | Z1→D1 | inherit | Out of scope (read-only) |
| PR-05 | `review.py:66,146` `_git` with `target.diff_range` | `hw review --target-kind diff --diff-range R` | R is passed as a git argument without `--` separation; a value starting with `-` (e.g. `--output=<file>`) makes git write an arbitrary file | Z1→D1 / host files | inherit | **Unmediated (F-07)** |
| PR-06 | `known_failures.py:132` `_git` | `plan`, `evaluate`, `regression-baseline` | read-only git at `base_commit` | Z1→D1 | inherit | Out of scope (read-only) |
| PR-07 | `evaluators/base.py:57` via ruff/mypy/pytest evaluators | `hw evaluate --target P` | runs pytest on P (imports `conftest.py` and tests) and mypy (loads plugins named in the project configuration, `evaluators/code/mypy_evaluator.py:43-59`): executes target code with network and full home access | Z1 executes D1 code | inherit | **Unmediated (F-03)** |
| PR-08 | `evaluators/code/coverage_evaluator.py:55,71,100` | `hw evaluate` (Python) | pytest under coverage, same execution | Z1 executes D1 code | inherit (`os.environ.copy()`) | **Unmediated (F-03)** |
| PR-09 | `evaluators/code/node_evaluator.py:88` | `hw evaluate` (Node) | `npm run build/lint/test`: package scripts, can reach the network | Z1 executes D1 code, →Z5 | inherit | **Unmediated (F-03)** |
| PR-10 | `regression.py:54` `_pytest_passing` | `regression-baseline`, `evaluate --step regression` | pytest over the target | Z1 executes D1 code | inherit | **Unmediated (F-03)** |
| PR-11 | `reproducibility.py:94,103,105,125,137` | `complete-step --step verify` | `git worktree add/remove` (runs the target's `post-checkout` hook with the inherited environment) + temp-dir removal; artifact paths are globbed and `unlink()`ed, so a committed symlinked directory deletes a real file outside the worktree | Z1 executes D1 hooks; Z1→host files | inherit | **Unmediated (F-03)** |
| PR-12 | `reproducibility.py:139` `_check_one` | `complete-step --step verify` | runs the regeneration command from the agent-written `plan.json`, no OS sandbox | Z2-authored command runs in Z1 | filtered (`reproducibility.py:119-120`) | **Unmediated (F-03)** |
| PR-13 | `live_demo.py:39` `check` | `complete-step --step verify` | runs `objective.live_demo.command` in the repository root, no sandbox; "read-only" only by convention | Z1 executes D1/Z2 command | inherit | **Unmediated (F-03)** |
| PR-14 | `live_demo.py:50-53` evidence branch | same | reads any absolute `evidence_path`; up to 400 characters copied into `verify-checks.json` | host file → D2 | n/a | **Unmediated (F-10)** |
| PR-15 | `codex.py:57,73` `_worktree_hooks_dir` | `hw codex-prepare` | `git rev-parse --git-path hooks` | Z1→D1 | inherit | Out of scope (read-only) |
| PR-16 | `codex.py:99-102` `install_pre_commit_hook` | `hw codex-prepare` (default) | overwrites `<hooks>/pre-commit` without backup; honours `core.hooksPath`, which can point outside the repository | Z1→git hooks | n/a | **Unmediated (F-06)** |
| PR-17 | `compiler.py:520` `install_guardrails` | `hw guardrails-install` | rewrites `<repo>/.claude/settings.local.json` (adds the PreToolUse hook); backup kept under `.hw` | Z1→agent-harness config | n/a | **Unmediated (F-11)** — a safety installer, but it edits the coordinator's configuration |
| PR-18 | `compiler.py:549-555` `remove_guardrails` | `hw guardrails-remove` | restores or deletes `.claude/settings.local.json` | Z1→agent-harness config | n/a | **Unmediated (F-11)** |
| PR-19 | `engine.py:172-175` and all `atomic_write_text` under the run root | every run command | writes `<repo>/.hw/<run_id>/`; `run_id` validated (`path_safety.py:10`) | run root | n/a | Out of scope (run root) |
| PR-20 | `learning.py:172-181,501`; `escalation.py:45` | `learn`, `propose`, `anomalies` | writes `.hw/lessons`, `.hw/proposals`, `.hw/observability` | `.hw` outside one run root | n/a | Out of scope (Wilfred state) — integrity relevance in F-02 |
| PR-21 | `cost.py:56,63-64` `find_session_transcript` | `hw record-usage` | reads Claude Code transcripts (`~/.claude/projects`, or any caller path) | owner home (D9) → D2 | n/a | **Unmediated (F-12)** — read of sensitive data, figures only are copied |
| PR-22 | `approvals.py:36-48,222-224,372-374` | every approval point, `policy-status` | reads `policy.json` and the two public keys; no code writes there; no code reads private keys | owner config (D6) | n/a | Out of scope (read) — trust consequence in F-02 and §7 |
| PR-23 | `foundry.py:88,217-218`; `foundry_storage.py:29,76-79` | all `hw foundry …` with `--project PATH` | creates/writes an arbitrary caller-chosen project directory | Z1→any owner path | n/a | Blocked for symlinked roots (`foundry.py:89`); otherwise **Unmediated (F-05)** low |
| PR-24 | `foundry_execution.py:46-56` `materialize_bundle` | `foundry generate` / `repair` | writes generated bundle into the project directory, verified against manifest hashes | Z1→Foundry root | n/a | Out of scope (Foundry root, content-addressed) |
| PR-25 | `foundry_sandbox.py:32` + `templates/provider_broker.py:20` | `foundry evaluate`, `repair`, `execute` | spawns generated `runner.py` under `sandbox-exec` deny-default (no writes, no network, no children) | Z1→Z3 | filtered (`provider_broker.py:16`) | **Blocked** by the sandbox only (fails closed off macOS, `foundry_sandbox.py:12-14`). The manifest hash check (`foundry_execution.py:148-174`) compares against hashes stored in the same writable project folder (`cli.py:199-203`) and is checked before `runner.py` runs (`foundry_execution.py:75,89`), so it gives no integrity against a same-user writer. Residual: profile allows `mach-lookup` and `file-read-metadata` unrestricted (F-09) |
| PR-26 | `templates/provider_broker.py:56-75` callback loop | same, with `--provider codex` | worker `model_request` → host makes a live model call; ≤12 calls, size and role limits; worker-chosen context forwarded verbatim | Z3→Z1→Z4 | n/a | **Unmediated (F-08)** — bounded, no approval |
| PR-27 | `foundry_agents.py:92-104` `CodexCLIProvider.__call__` | `foundry … --provider codex` | `codex exec --sandbox read-only --ignore-user-config --ephemeral`; sends instructions and request to the provider | Z1→Z4 | **inherit** | **Unmediated (F-08)** |
| PR-28 | `foundry_scout.py:147` `GitHubSearchProvider.search` | not reachable from the CLI (`cli.py:119-130` builds only the in-memory provider) | HTTPS GET to the GitHub search API | Z1→Z5 | n/a | Out of scope (unreachable) |
| PR-29 | `durable.py:139-150` `_ensure_dbos` | not imported by any module | would connect to a Postgres server named by environment | Z1→Z5 | n/a | Out of scope (unreachable; double opt-in) |
| PR-30 | `foundry_acceptance.py:104,293` | `python -m hyperwilfred_core.foundry_acceptance` only | child Python, temp proof directory, optional Codex provider | Z1→temp, →Z4 | inherit | Out of scope (acceptance harness, not an `hw` command) |
| PR-31 | `gates.py:431-485` `execute_protected_action` | no CLI, no production caller (only `foundry_acceptance.py:208` demo and tests) | runs a statically chosen trusted callback once | Z1 | n/a | **Mediated** (`gates.py:446-460` → `approvals.verify_and_consume`, not policy-eligible) |
| PR-33 | `engine.py:451-453`, `guardrails.py:35-43,297-301`; `quality.py:1155-1165` | `record-result`, `record-patch` with `--worktree` (`cli.py:765`) | the acceptance verdict (did the worker stay inside its allowed files?) is computed on a caller-chosen folder, not bound to `packet.worktree_hint`; `_git` ignores git's return code, so a non-git or clean folder reads as "no changes" and the packet is accepted | Z2 controls a Z1 check | inherit | **Unmediated (F-15)** |
| PR-34 | `review.py:140`, `schemas.py:1031` | `hw review --root` | review target resolved with no containment check: can name files of another client project or anywhere in home; the critic read allow-list derives from it | Z1 reads D1/D8 across projects | n/a | **Unmediated (F-16)** |
| PR-35 | `cli.py:293,313`; `engine.py:518-524,590` | `hw evaluate --target`, `regression-baseline --target` | the deterministic gate and the baseline run on any caller-selected folder and mark the step done | Z2 controls a Z1 check | inherit | **Unmediated (F-16)** |
| PR-36 | `cli.py:196`; `foundry_execution.py:81-90`; `templates/provider_broker.py:59-65` | `hw foundry execute --input-file` | any host JSON file becomes worker input and, with `--provider codex`, can be forwarded to the model provider | host file → Z3 → Z4 | n/a | **Unmediated (F-08)** |
| PR-37 | `quality.py:151-154` | `record-subjective` / `record-critique --attestation-file` | the full content of any caller-named file is copied into `.hw/<run>/review-attestations/` **before** it is verified | host file → D2 | n/a | **Unmediated (F-10)** |
| PR-38 | `foundry_storage.py:11-14,32` | any Foundry write when `FOUNDRY_CRASH_AT` is set | production code exits the process at a named point (test fault injection) | environment → Z1 | n/a | **Unmediated (F-18)** |
| PR-39 | `learning.py:209-210` (`_item_path`), `compiler.py:45-50`, `codex.py:50-51` | `hw promote --id`, compile/install/codex-prepare `--run-id` | identifiers joined into paths without `validate_identifier`: `promote --id ../../x` reads any `*.json`; a planted file with `status: approved` is trusted (`learning.py:525-527`) and starts an IMPROVE run (`learning.py:605`) | Z2/Z3' file → Z1 decision | n/a | **Unmediated (F-17, F-06)** |
| PR-32 | *Outside Wilfred code:* the coordinator's own shell/git/`gh` tools (git commit, push, PR, merge, deploy) | any Claude/Codex session | the irreversible effects the run gates are named after | Z2→Z5 | owner's | **Unmediated (F-01)** — Wilfred records a signed ship/merge/deploy gate but never performs or intercepts the effect |

### 5.1 Approval points

| Point | Command | What actually changes | Authority |
|---|---|---|---|
| Run gates (ship, merge, deploy, deps, secrets, migration) | `gate-approve`, `gate-status` | run **state** only: the step becomes done, `next-step` unblocks | Signed (`gates.py:86,336` → `approvals.py:432-438`); a policy token is inert (`approvals.py:335-336`) |
| Learning promotion | `hw promote --id` | creates a new IMPROVE run under `.hw` (`learning.py:605`) | Signed at first resolution (`gates.py:86`); **a stored `approved` status is trusted afterwards without re-verification** (`learning.py:525-527`); the challenge binds only `promote learning proposal <id>` plus evidence refs (`learning.py:507,530-531`), not `proposal.objective`, which is what runs (`learning.py:607`) |
| Independent-review attestation | `record-subjective`, `record-critique` with an attestation file | marks a critic independent | Signed with the review key (`quality.py:155,394`) |
| Quality override | `hw quality-override` | quality loop accepts despite missing independence | Policy-mediated (`quality.py:515,522-529`) |
| Verify-finding override | `hw verify-override` | live-demo gap stops blocking verify | Policy-mediated (`verify_checks.py:88,95-98,165-169`) |
| Test-evolution review | `hw evaluate --step regression` | a modified/deleted baseline test stops counting as a regression | Policy-mediated (`regression.py:212-217`): with the switch on, any writer of `.hw/<run>/test-evolution/` (an unsigned `*.review.json` + challenge) self-approves; `create_semantic_review_challenge` (`regression.py:165`) has no CLI caller. Not single-use (`consume=False`, `regression.py:213-217`); policy decisions have no expiry check (`approvals.py:328-368`). With policy off the verifying public key is **caller-selectable** (`cli.py:295-296` → `regression.py:197`) |

### 5.2 Counts

39 routes (PR-01 … PR-39): 1 mediated (PR-31), 1 blocked (PR-25, sandbox only, with a residual),
11 out of scope, and **26 unmediated**, grouped into 16 route findings (F-01 … F-12, F-15 … F-18).
F-13 and F-14 are findings about controls, not routes. Routes PR-33 … PR-39 and the
reclassification of PR-11 came from the independent review (§8).

### 5.3 Re-run 2026-10-24 (after Fable review 5 and unification steps U0–U2)

Re-run on `acb54bb` of branch `resolver/2-wilfred-unification` (base `codex/wilfred-product`
`627df5c` + Fable review 5 fixes `26d9c58`, `7a3a1dd` + unification U0 `63d9349`, U2 `c46c5fd`,
`acb54bb`). The commit that adds this section changes documentation only, so every citation
below holds on it too, except that the next commit (the engine-state check fails closed on
git errors, after a design review) moves `guardrails.py` lines from 154 down and
`engine.py:172-178`/`:607-633`, and makes PR-41 fail closed instead of reading a git error as
"nothing tracked". Same method as above, including the new `command_center/` package. It
was surveyed by a read-only subagent and the critical rows were re-read by the author (PR-05
refusal and `--end-of-options`; PR-33 verdict still computed on the caller's `--worktree`,
since `worktree_hint` is read only by `compiler.py:111`; M1 `quality.py:209`; PR-43 environment
deny-list `opsread.py:85-92`). Nothing was executed against git to probe behaviour; rows that
depend on git behaviour say "probable".

**Not yet the post-U4 tree.** Unification steps U3 (CI-parity gate) and U4 (flaky-test gate)
need the owner's signature and are not integrated. The CI-parity gate executes the target
repository's workflow `run:` steps, so it will add at least one F-03-class route: re-run this
registry again after U3/U4, and after the Gate 0 candidate is integrated.

The tables in §5 and §6 above are kept as written on 2026-10-09; where they disagree with §5.3,
§5.3 is the current fact.

#### 5.3.1 Rows PR-01 … PR-39 on the re-run tree

| ID | Route on HEAD (file:line) | Classification on HEAD | Changed vs ecdcb66? | Evidence (one line) |
|---|---|---|---|---|
| PR-01 | `guardrails.py:39-47` `_git` (unchecked, still used by `_status_entries(strict=False)` :101, `ensure_engine_state_excluded` :135, `tracked_engine_state` :156,159); `:50-69` `_git_checked`; `:72-90` `require_worktree`; `:247-257` `_original_line_count` (`git show HEAD:<path>`, unchecked) | Out of scope (read-only) — F-03 note still applies | Lines moved; new checked variant | No `env=` on any of these `subprocess.run` calls (:41,:58,:80,:249) and no `-c core.fsmonitor=false`, so repository config of a caller-named worktree is still honoured |
| PR-02 | `guardrails.py:333-341` `rollback` → `require_worktree` :339, `reset --hard HEAD` :340, `clean -fd -e .hw` :341; CLI `cli.py:307-309` → `engine.py:639-642` | **Unmediated (F-05)** — narrowed | Yes: non-git folders now refused | `require_worktree` (:72-90) only checks `rev-parse --is-inside-work-tree == true`; any git work tree anywhere (also a subdirectory of an unrelated repo, where `reset --hard` resets the whole repo) is still accepted, with no registry or approval check |
| PR-03 | `guardrails.py:430-434` `accept`→`rollback` on rejection; reached from `engine.py:453-466` and `quality.py:1231-1244` | **Unmediated (F-05)** — narrowed | Yes (same as PR-02) | `worktree` comes straight from `--worktree` (`cli.py:818`, `:937`); not compared with any registered worktree |
| PR-04 | `quality.py:204-221` `_changed_product_paths` (`changed_paths(strict=False)` :207; `git diff --name-only {base_commit}..HEAD` :209-215) | Out of scope (read-only scope hint) — **but see §4 M1** (option injection via `base_commit`) | Lines moved; now explicitly tolerant (`strict=False`) | Comment :206 "Review scope only … never authority"; the committed-range call checks the return code (:216-219) |
| PR-05 | `review.py:146-157` (refuses leading `-` :149-155; `git diff --name-only --end-of-options <range>` :156) | **Blocked** | **Yes — reclassified** | A range starting with `-` raises `ReviewError` before git runs; `--end-of-options` stops option parsing (git >= 2.24; on older git the unknown flag makes git fail, and :157-161 raises) |
| PR-06 | `known_failures.py:131-134` `_git`; calls :139, :145, :161, :195, :198 | Out of scope (read-only) | No | Unchanged file; see §4 M2 for `base_commit` at :198 (guarded by :195) |
| PR-07 | `evaluators/base.py:57-65` via ruff/mypy/pytest (`mypy_evaluator.py:59`, `pytest_evaluator.py:58`, `ruff_evaluator.py:47`) | **Unmediated (F-03)** | No | `run_subprocess` passes `env=None` (inherit) unless a caller gives one; unchanged |
| PR-08 | `evaluators/code/coverage_evaluator.py:55,71,100` | **Unmediated (F-03)** | No | `env = os.environ.copy()` at :55 |
| PR-09 | `evaluators/code/node_evaluator.py:88` | **Unmediated (F-03)** | No | `npm run <script>` with `cwd=target_path`, inherit env |
| PR-10 | `regression.py:54-57` `_pytest_passing` | **Unmediated (F-03)** | No | `run_subprocess([sys.executable,"-m","pytest",…,target])`, no env |
| PR-11 | `reproducibility.py:94,104,105,136-137` (`worktree add/remove`, `rmtree`, glob+`unlink`) | **Unmediated (F-03)** | No | Unchanged file; `_git` at :75-78 has no `env=` |
| PR-12 | `reproducibility.py:139-142` `_check_one` | **Unmediated (F-03)** | No | `env=_scrubbed_env(work)` (:113-120: PATH/LANG/LC_ALL/TZ + HOME=worktree) — filtered, not sandboxed |
| PR-13 | `live_demo.py:39-42` `check` | **Unmediated (F-03)** | No | `subprocess.run(shlex.split(demo.command), cwd=repo_root, …)`, no env |
| PR-14 | `live_demo.py:48-53` evidence branch (absolute path accepted :50-51, read :53) | **Unmediated (F-10)** | No | `p = p if p.is_absolute() else repo_root / p` then `read_text` |
| PR-15 | `codex.py:58-64` `_git`, `:67-84` `_worktree_hooks_dir` (`rev-parse --git-path hooks` :75) | Out of scope (read-only) | Lines moved (+2) | No `env=` |
| PR-16 | `codex.py:87-104` `install_pre_commit_hook` (overwrite :102-103, chmod :104) | **Unmediated (F-06)** | Lines moved; `run_id` now validated (`_run_dir` :53-55) | Still `dest.write_text(...)` without backup into whatever `--git-path hooks` returns (honours `core.hooksPath`) |
| PR-17 | `compiler.py:472-543` `install_guardrails` (merge `_merged_settings` :442, write `.claude/settings.local.json` :540; hook command `python3 … pretooluse.py` :271-287, timeout 10 at :283) | **Unmediated (F-11)** | Lines moved | Fragment read from `.hw/<run>/hooks/settings.json` (:476, :521) and merged into the coordinator's settings |
| PR-18 | `compiler.py:546-581` `remove_guardrails` (restore from backup :566-573 or delete :575) | **Unmediated (F-11)** | Lines moved | Restores bytes of `.hw/<run>/hooks/settings.local.backup` verbatim (:571) |
| PR-19 | `engine.py:172-178` + all `atomic_write_text` under the run root; `run_id` validated `engine.py:65` → `path_safety.py:10-15` | Out of scope (run root) | Lines moved | **New in the same function:** `engine.py:174` now also writes outside the run root — see PR-40 |
| PR-20 | `learning.py:174-186` (`record_lessons`), `:492-497` (`_save_proposal`), `:501` (`learn`), `:741-745` (`_render_pack`); `escalation.py:45,100-101`; `observability.py:378,423` | Out of scope (Wilfred state) — integrity note F-02 | Lines moved; new read-only analysis (`_promotion_effect` :321-362, `evidence_strength` :287, `effect_summary` :299) | All writes stay under `<repo>/.hw/{lessons,proposals,observability}` |
| PR-21 | `cost.py:56,63-64`; CLI `cli.py:326-328` | **Unmediated (F-12)** | No | `--transcript` paths and `--projects-dir` accepted as given |
| PR-22 | `approvals.py:36-48,222-224,372-374` | Out of scope (read) | No | Unchanged; no code writes there, no code reads private keys (re-grepped; the command center's default output dir is a *sibling* folder there, see PR-44) |
| PR-23 | `foundry.py:87-91` `_root` (symlink refusal :89), `:217-219`; `foundry_storage.py:28-34,76-79` | Blocked for symlinked roots; otherwise **Unmediated (F-05)** low | No (foundry.py edit is at :523-528, below) | `selected = Path(project).expanduser()`; `root.mkdir(..., mode=0o700)` |
| PR-24 | `foundry_execution.py:46-57` `materialize_bundle` | Out of scope (Foundry root, content-addressed) | No | Unchanged lines |
| PR-25 | `foundry_sandbox.py:12-32` + `templates/provider_broker.py:16,20-21`; manifest check now `foundry_execution.py:138-170`; checked at `:75` before runner at `:88-90`; hashes from `cli.py:201-205` | **Blocked** (sandbox only, residual F-09) | Lines moved (−4 in foundry_execution after :135; +2 in cli) | Profile at `foundry_sandbox.py:23-29` still `(allow mach-lookup)` and `(allow file-read-metadata)` unrestricted |
| PR-26 | `templates/provider_broker.py:56-83` callback loop; **new** second-request guard `:60-67`; limits `:68-71` (≤12 calls, role set, dict context); reply bound 16 KiB `:76-77` | **Unmediated (F-08)** — bounded, no approval | Yes: protocol guard added | A `model_request` received while the previous reply (or the initial input) is still pending raises `RuntimeError` instead of a second provider call; context still forwarded verbatim (`invoke_provider(provider, request, …)` :72) |
| PR-27 | `foundry_agents.py:92-104` `CodexCLIProvider.__call__` | **Unmediated (F-08)** | No | `subprocess.run(command + ["-"], …)` with no `env=` (inherit) |
| PR-28 | `foundry_scout.py:147`; CLI builds only in-memory providers `cli.py:121-132` | Out of scope (unreachable) | cli lines +2 | `_foundry_scout_providers` returns `InMemorySearchProvider` only |
| PR-29 | `durable.py:137-152` `_ensure_dbos` | Out of scope (unreachable) | No | Still not imported by any module (re-grepped) |
| PR-30 | `foundry_acceptance.py:103-104,293` | Out of scope (harness) | No | Note: its `engine.plan` call (:197) now also triggers PR-40 on the temp root |
| PR-31 | `gates.py:431-485` `execute_protected_action` (verify `:446-460`) | **Mediated** | No | Unchanged; still no CLI or production caller |
| PR-32 | Outside Wilfred code (coordinator's shell/git/`gh`) | **Unmediated (F-01)** | No | Nothing in the diff performs or intercepts push/PR/merge/deploy |
| PR-33 | `engine.py:423-487` `record_result` (`worktree=worktree` :455, caller value from `cli.py:282,818`); `quality.py:1207-1244` `record_patch` (`worktree=worktree` :1233, `cli.py:428,937`); gate `guardrails.py:368-434` (`require_worktree` :391, strict status :101→:58-69, numstat :262, hunks :277) | **Unmediated (F-15)** — narrowed | Yes: git failures now fail closed (`GuardrailError` → exit 2, `cli.py:1219-1221`) | **Verdict is still computed on the caller-chosen folder**: `record_result` never reads `packet.worktree_hint` (set at `engine.py:385`, only used by codex/compiler); a *valid, clean* git work tree other than the packet's still reads as "no changes" → accepted. Residual fail-open: `_original_line_count` (:247-257) returns 0 on any `git show` error, which disables the full-file-replacement signal (:316) for that file |
| PR-34 | `review.py:143` (`root = (repo_root / target.root).resolve()`, no containment); `schemas.py:1076` (`root: str = "."`); filesystem fallback `review.py:91-103` | **Unmediated (F-16)** | Lines moved | New opt-in lenses (`--lens`, `cli.py:1134-1141`; `quality.py:63-69`, `review_lenses` :356-369) add critics over the **same** target; no new read surface |
| PR-35 | `cli.py:295,315` (`--target` resolved as given); `engine.py:490-604` (`run_all` :521-526, step DONE :593), `regression_baseline` `:648` | **Unmediated (F-16)** | Lines moved; evaluate now also runs PR-41 on the target | `target=Path(args.target).resolve()` with no containment |
| PR-36 | `cli.py:198` (`--input-file`); `foundry_execution.py:81-90`; `templates/provider_broker.py:56-72` | **Unmediated (F-08)** | cli +2 | Any JSON object file becomes the worker input (`_read_foundry_json` `cli.py:101-105`) |
| PR-37 | `quality.py:157-168` (`atomic_write_text(canonical_token, supplied.read_text())` **:163**, verify **:164-168**) | **Unmediated (F-10)** | Lines moved only | The copy still happens before `verify_and_consume`; a failed verification leaves the copied content in `.hw/<run>/review-attestations/` |
| PR-38 | `foundry_storage.py:11-14,32` | **Unmediated (F-18)** | No | `os.environ.get("FOUNDRY_CRASH_AT") == point → os._exit(86)` unchanged |
| PR-39 | Identifier validation now: `learning.py:214-217` (`_item_path` → `validate_identifier` :216); `compiler.py:47-48` (`_run_dir`), `:127` (packet id), `:107-110` (packet.run_id must equal run_id); `codex.py:53-55`, `:182`. Promotion: `learning.py:681-737` | **Unmediated (F-17, F-02)** — narrowed (traversal part **Blocked**; F-06 id part fixed) | Yes | `promote --id ../../x` now raises `ValueError` → `input_error` exit 2 (`cli.py:1245-1250`). Still: `_resolve_review` returns early for a stored `APPROVED` (:640-642, no signature re-check), `promote` acts on it (:699, :720-725 `plan(objective=proposal.objective)`); the challenge binds only `promote learning proposal <id>` + evidence refs (:618-621, :643-647). `load_proposal` (:478-482) does not check that the file's `proposal_id` equals the requested id |

##### §5.1 approval-point citations on the re-run tree (only lines moved; authority unchanged)

| Point | New citation |
|---|---|
| Run gates | `gates.py:86,336` → `approvals.py:432-438` (unchanged) |
| Learning promotion | Signed at first resolution via `gates.detect_decision_in_dir` (`learning.py:643-647`); stored `approved` trusted afterwards (`learning.py:640-642`); objective unbound (`:618-621` vs `:722`) |
| Independent-review attestation | `quality.py:164-168` (consume) and `quality.py:401-423` `_record_attested` (consume=False, recorded=True), now shared by `provenance_report` (:461) and new `critic_authorship` (:426-458) |
| Quality override | Policy-mediated, `quality.py:590,597` |
| Verify-finding override | Policy-mediated, `verify_checks.py:88,95-98,165-169` (unchanged) |
| Test-evolution review | Policy-mediated, `regression.py:212-217`; caller-selectable key `cli.py:297-298` → `regression.py:197` |

---

#### 5.3.2 New routes PR-40 … PR-49

| ID | Route (file:line) | Reached by (CLI command) | Effect | Zone crossing | Env | Classification |
|---|---|---|---|---|---|---|
| PR-40 | `guardrails.py:127-151` `ensure_engine_state_excluded` (`git rev-parse --git-path info/exclude` :135; read :141; `mkdir` :147; append `/.hw/` :149-150); called from `engine.py:174` (`plan`) | `hw plan`, `hw review` (`review.py:236`), `hw promote` (`learning.py:720`); also `foundry_acceptance.py:197` | Appends a fixed comment + `/.hw/` line to the host repository's **common** `info/exclude` (shared by every worktree of that repository). Target path is whatever git reports for `--repo-root`: if that folder is a subfolder of an enclosing repository, the enclosing repository's exclude file is edited (and the root-anchored `/.hw/` then names the enclosing root's `.hw`, not `<repo-root>/.hw`). Follows a symlinked exclude file (`Path.open("a")`). Non-git folder: `_git` returns "" → no write (:136-137). On `c46c5fd` the line was unanchored `.hw/`; `acb54bb` does not remove an unanchored line already written (it only appends), and an unanchored line hides nested `.hw/` from `git status --untracked-files=all`, i.e. from `accept()` (comment at :141-142) | Z1 → host repo git metadata (outside `.hw/<run>/`) | inherit (`_git`, no `env=`; inherited `GIT_DIR`/`GIT_COMMON_DIR` would redirect the write — probable, not executed) | **Unmediated** (new; no approval, fixed content; no F-id yet — candidate F-19, Low) |
| PR-41 | `guardrails.py:154-161` `tracked_engine_state` (`rev-parse --show-toplevel`, `ls-files -- .hw`) via `engine.py:553,607-633` `_engine_state_check` over `repo_root` **and** the caller's `--target` | `hw evaluate` (all steps) | Read-only git listing at the top level of the repository that contains the caller-chosen target; a tracked `.hw/` adds an ERROR check that blocks the gate | Z1 → D1 (caller-chosen repo) | inherit | Out of scope (read-only). Not hardened like the command center (no `core.fsmonitor=false`); whether `ls-files` can invoke fsmonitor is **unverified** |
| PR-42 | `command_center/gitread.py:171-198` `run_git` (allowlist `:104-147`; fixed `--no-optional-locks --no-pager -c core.fsmonitor=false -c log.showSignature=false` `:64-71`; per-filter `clean/smudge/process` blanked `:161-168`); callers `collect_projects.py:116-119` (`_classify`), `:242-328` (`_describe`: rev-parse, status, for-each-ref, remote -v, log -1, worktree list), `snapshot.py:83-89` (`_product_commit` on the Wilfred checkout) | `hw command-center snapshot`, `hw command-center build` (`cli.py:667-707`, parser `:1152-1201`) | Read-only git in every repository found under `--workspace` (depth 3, `collect_projects.py:61-107`, symlinks not followed) **and in linked worktrees outside the workspace** named by those repositories' `worktree list` (`:356-374`). `status` runs only if every content filter in user + repo config can be identified (`collect_projects.py:214-239`, `gitconfig.py:135-193`, fail closed); submodule work trees not entered (`--ignore-submodules=dirty`, per code comment :274-275, not independently verified). Remote URLs stripped of credentials (`gitread.py:205-232`) | Z1 → D1/D8 metadata of all projects → output dir | inherit **minus `GIT_*`** (deny-list, not allow-list) + `GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`, `GIT_NO_LAZY_FETCH=1`, `GIT_CONFIG_NOSYSTEM=1`, `LC_ALL=C` (`gitread.py:150-158`); `stdin=DEVNULL`, 15 s timeout | Out of scope (read-only, allowlisted; arguments validated before any process starts) |
| PR-43 | `command_center/opsread.py:95-123` `run_ops` (allowlist `:35,71-82`); caller `collect_ops.py:279-310` | `hw command-center snapshot\|build [--ops-bin ABS]` (`cli.py:659-664` requires absolute; default `~/.local/bin/ops`, `snapshot.py:49-57`) — runs only if `<workspace>/ops` exists, is a real directory, and the program is an executable file (`collect_ops.py:281-298`) | **Executes a program chosen by path** (links not resolved, `snapshot.py:54-57`) as `<bin> export\|status --json --root <ws>/ops`, `cwd=<ws>/ops`, no sandbox; "read-only and offline" is a property claimed for the `ops` tool, not enforced here (`opsread.py:11-13`). The default tool's code lives inside the workspace (`opsread.py:17-18`). Output capped at 16 MiB, 30 s timeout | Z1 executes owner/caller-chosen code (Z2 can name any absolute binary) | inherit **minus `GIT_*` and `PYTHON*`** + fixed `GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`, `GIT_NO_LAZY_FETCH=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONIOENCODING=utf-8` (`opsread.py:85-92`) — all other credentials in the environment reach the child | **Unmediated (F-03 class)** — unsandboxed code execution with the owner's environment |
| PR-44 | `command_center/snapshot.py:157-197` (`_write_atomic` mkstemp mode 600 + `os.replace`; `_ensure_out_dir` mkdir 0700 `:174-181`); default dir `snapshot.py:39-41` = `~/Library/Application Support/Wilfred/command-center`; refusals `:200-207` (inside any git repo), `:210-216` (inside workspace), `:219-227` (inside any discovered project/worktree), `:241-245`, `:298-302` (inside memory dir) | `hw command-center snapshot\|render\|build [--out-dir D]` | Writes/overwrites `snapshot.json` and `index.html` (fixed names) in a caller-chosen directory that is not inside a repo/workspace/project/memory; `out_dir` is resolved through symlinks (`paths.py:21-50`); no `secret_part` check on `out_dir` (only on workspace :71-80 and memory :60-68). Content: project paths, branches, remote URLs (credential-stripped), run statuses/pending gates/last event message (`model.py:101-105`), memory note paths of malformed notes, ops task ids/titles/paths (`model.py:178-188`) — data of **all** projects in one file. **Default location is inside the owner's Wilfred application-support folder (Z0/D6 folder)**; it writes only the `command-center/` subfolder | Z1 → owner home (outside any repo) | n/a | Blocked for repository/workspace/project/memory targets (cited refusals); otherwise **Unmediated** (Low, F-05 class: fixed file names in any other caller-chosen directory) |
| PR-45 | `command_center/gitconfig.py:128-132` `user_config_files` (`~/.gitconfig`, `$XDG_CONFIG_HOME/git/config`); `:147-189` include following (relative/absolute include paths read, `~`/`%(` refused → unverified); repo `common_dir/config`, `git_dir/config.worktree` added at `collect_projects.py:220-224` | `hw command-center snapshot\|build` | Reads the owner's git configuration (≤1 MiB per file, depth ≤5) only to collect `filter.<name>` section names; no value is copied to the snapshot (problem text carries file path + parse reason only) | owner config → Z1 memory | n/a | Out of scope (read of owner configuration; fail closed) |
| PR-46 | `command_center/collect_memory.py:65-104` (walk, skips symlinks, `.git`, `Private`/`.ssh`/`secrets`, `.env*`), `:107-166` (reads every `.md` ≤4 MiB, frontmatter only, and `MEMORY.md`) | `hw command-center snapshot\|build [--memory DIR]` (default `~/.agents/memory`, `snapshot.py:44-46`) | Reads the owner's shared memory tree (or any caller-named directory without a secret-named component, `snapshot.py:60-68`); copies counts by type, index line count and the **paths** of malformed notes | owner home (memory may hold D8 facts) → snapshot | n/a | Out of scope (read; names and counts only) |
| PR-47 | `command_center/collect_runs.py:232-290` (each project's `.hw/` store; symlinked stores and `.hw/<run>/worktrees/` not entered), `:112-125` (`state.json` ≤8 MiB); `run_detail.py:49-63` `_read_bytes` (events ≤64 MiB, gates, `usage.json`; symlinks refused) | `hw command-center snapshot\|build` | Reads run state of every project and worktree found by PR-42 (including worktrees outside the workspace) | D2 of many projects → snapshot | n/a | Out of scope (read-only) |
| PR-48 | `command_center/collect_closed.py:60-80+` `read_register` of `<out_dir>/closed-runs.json` (`snapshot.py:31-32,250`); effect in `attention.py:183-196` | `hw command-center snapshot\|build` | An unsigned, hand-written file marks runs "closed": they leave the attention inbox, except while a high-risk approval window is open (`attention.py:190-192`). Nothing on disk changes | owner-written file → page content | n/a | Out of scope (display only; no authority) |
| PR-49 | `quality.py:426-458` `critic_authorship` → `_record_attested` `:401-423` (`approvals.verify_and_consume(..., consume=False, recorded=True)`); caller `review.py:397` (`review_report`) | `hw review-report` | Read-only signature verification that labels each critic `independent`/`adapter`/`missing` in `review-report.json`; fails closed to `adapter` (:420-423, :455-456) | D4 read | n/a | Out of scope (read-only approval check; nothing authorized) |

Not new routes (checked): `learning.py` additions (`evidence_strength`, `effect_summary`,
`_promotion_effect`) only read observations already under `.hw/`; `review.py`/`quality.py` lens
additions reuse the PR-34 target; `command_center/render.py` produces HTML in memory (outbound
`https://github.com/` links are plain anchors, `render.py:126-141`; CSP meta `:743`) and is written
only via PR-44; `paths.canonical` lists parent directories (`paths.py:33`).

---

#### 5.3.3 Counts on the re-run tree

**49 routes (PR-01 … PR-49):**

- **1 mediated** (PR-31).
- **2 blocked** (PR-25 sandbox only, residual F-09; **PR-05**, newly blocked).
- **18 out of scope**: the original 11 (PR-01, 04, 06, 15, 19, 20, 22, 24, 28, 29, 30) + 7 new
  (PR-41, 42, 45, 46, 47, 48, 49).
- **28 unmediated**: 25 of the original rows (PR-02, 03, 07–14, 16, 17, 18, 21, 23, 26, 27, 32–39;
  PR-02, 03, 33 and 39 narrowed but still reachable) + 3 new (PR-40, PR-43, PR-44).

Delta vs ecdcb66 on the original 39: unmediated 26 → 25 (PR-05 blocked); blocked 1 → 2.
Findings with no remaining route: **F-07** (PR-05 blocked). F-06 now covers only PR-16 (the id part
of PR-39 is fixed). New routes map to F-03 (PR-43), F-05 (PR-44) and a new candidate F-19 (PR-40).

---

#### 5.3.4 Routes the 2026-10-09 registry missed (present at `ecdcb66`)

| # | Route (file:line on HEAD; ecdcb66 line) | Reached by | What the code does | Suggested classification |
|---|---|---|---|---|
| M1 | `quality.py:209-215` (ecdcb66 `quality.py:200`) `git -C repo diff --name-only f"{base_commit}..HEAD"` | quality loop critique scope (`quality.py:249`, `:667`), `hw review` critic scope | `base_commit` is read from `.hw/<run>/state.json` (`schemas.py:761`, `base_commit: str \| None`, no validator; set by `engine.py:169` but writable afterwards). It is passed as a positional argument with no `--`/`--end-of-options`; a value starting with `-` (e.g. `--output=/path`) is parsed by git as an option — `git diff --output=<file>` writes a file (probable, not executed). Same class as F-07 but the source is a run-state file (F-02 writer) rather than a CLI argument | **Unmediated (F-07 class, needs an F-02 writer)** — PR-04's "read-only" label is not fully accurate |
| M2 | `known_failures.py:195-198` `git diff --quiet {base_commit} -- rel` | `hw evaluate` (`engine.py:557-565`) | Same unvalidated `base_commit`, but line :195 first requires `git cat-file -e "{base_commit}:{rel}"` to succeed; a leading-dash value would make that fail (probable), so :198 is not reached | Effectively guarded (record only) |
| M3 | `cli.py:136-139` `foundry create --objective-file`, `cli.py:114` `--value-file`, `cli.py:124` scout fixture (all `_read_foundry_json`/`read_text` of any caller path) | `hw foundry create/answer/scout` | Any host file's content becomes Foundry project state; whether it is then forwarded to the provider with `--provider codex` was **not verified** | Same class as PR-36 (F-08) if forwarded; otherwise host file → Foundry root (low) |
| M4 | `guardrails.py:247-257` `_original_line_count` (ecdcb66 `:151` area) | `record-result`, `record-patch` | Ignores `git show` failure (returns 0) → disables one of the two patch-never-regenerate signals for that file | Residual of PR-33 (F-15), not a separate route |

No missed network client, private-key read, or git mutation was found beyond the rows above
(re-grepped `urlopen|socket|httpx|requests|Popen|subprocess` across the package).

## 6. Findings

Severity: **High** = can defeat an R4 approval or run untrusted code with the owner's full
authority in normal use; **Medium** = needs a mistaken or malicious argument from the
coordinator, or affects one boundary; **Low** = information exposure or defence-in-depth.
No finding is fixed here; each needs an owner decision (accept, fix in Phase 1, or block).

| ID | Severity | Finding | Routes | Suggested Phase 1 home |
|---|---|---|---|---|
| F-01 | High | The signed ship/merge/deploy gate guards a status flag, not the effect. Push, PR, merge and deploy are done by the coordinator with its own tools; nothing in Wilfred performs, intercepts or records them, so a coordinator can merge with the gate still pending. The intended mechanism (`execute_protected_action`) has no production caller | PR-32, PR-31 | P1.5 (Trusted Action Envelope) — route these effects through mediated host callbacks |
| F-02 | High | Authorization is read back from unsigned files that a dispatched worker can write: gate `status` in gate JSON (`gates.py:368-373`), stored proposal status (`learning.py:522-525`), and the policy switch (`approvals.py:222-239`). The PreToolUse hook does not inspect Bash (`hooks/pretooluse.py:44-49`) and in BUILD mode allows writes outside registered worktrees (`compiler.py:184`) unless a global blocked glob matches; `.hw/**`, `.claude/**` and `policy.json` are not in `DEFAULT_BLOCKED_GLOBS` (`schemas.py:359-373`; the public keys are covered only by the `**/*.pem` pattern) | PR-20, PR-22, 5.1 | P1.5 + P1.6 (verify signatures at use, not cached status; ledger) |
| F-03 | High | Target and agent-authored code runs unsandboxed as the owner with the full environment: pytest/coverage/npm evaluators, the live-demo command, and (environment-filtered but not sandboxed) the regeneration command. Git calls against an untrusted worktree honour repository configuration such as `core.fsmonitor` (probable code-execution vector) | PR-01, PR-07–PR-13 | P1.5 (scoped secret resolver, filtered env) + sandbox backend for evaluators |
| F-04 | Medium | The public key that verifies a test-evolution review is selectable by the caller (`--review-public-key`), so with the policy switch off a coordinator can sign its own test change | 5.1 | P1.5 — fixed key location only |
| F-05 | Medium | Destructive or broad writes accept any path: `rollback`/`record-result`/`record-patch --worktree` wipe uncommitted work in an unrelated repository; `foundry --project` creates folders anywhere | PR-02, PR-03, PR-23 | P1.5 — restrict to registered worktrees/projects |
| F-06 | Medium | `codex-prepare` overwrites `pre-commit` without backup, possibly outside the repository via `core.hooksPath`; `run_id`/`packet_id` not validated in `codex.py:50-51,93,180`, `compiler.py:45-50` or `learning.py:209-210` | PR-16, PR-39 | P1.4 (Codex wrapper) |
| F-07 | Medium | Git option injection through `--diff-range` (`review.py:146`). **2026-10-24: route blocked** (`26d9c58`, `7a3a1dd`; §5.3). A related source remains: `base_commit` from `state.json` at `quality.py:209` (§5.3.4 M1) | PR-05; M1 | P1.4 |
| F-08 | Medium | Codex provider child inherits the whole environment (all credentials); worker-chosen context and any host JSON given as `--input-file` reach the provider without approval (bounded in count and size) | PR-26, PR-27, PR-36 | P1.4 + P1.5 |
| F-09 | Low | Worker sandbox allows unrestricted `mach-lookup` and `file-read-metadata` (`foundry_sandbox.py:23-29`): system services reachable, file names/sizes anywhere observable | PR-25 | Phase 1 hardening, verify with probes |
| F-10 | Low | Host files copied into run artifacts: live-demo evidence excerpt (any path) and the whole `--attestation-file` content before verification (conflicts with goal G-E) | PR-14, PR-37 | P1.4 |
| F-11 | High | `guardrails-install`/`-remove` copy hook groups from `.hw/<run>/hooks/settings.json` verbatim into the coordinator's `.claude/settings.local.json` and restore it byte-for-byte from `.hw/<run>/hooks/settings.local.backup` (`compiler.py:422-439,549-553`); the installed command runs `.hw/<run>/hooks/pretooluse.py` (`compiler.py:266-268`). Anyone who can write `.hw/` (F-02) gets a shell command run on every coordinator tool call. Instructions also disagree on whether to hand-merge it (`skills/hyperwilfred-build/SKILL.md:131` vs `skills/hyperwilfred-review/SKILL.md:88`) | PR-17, PR-18 | P1.4 docs fix |
| F-12 | Low | `record-usage` reads whole session transcripts (D9: may contain client data and secrets) from a caller-chosen path | PR-21 | P1.2 cost events (§ domain model) |
| F-13 | Medium (probable) | PreToolUse hook fails closed on its own errors (`pretooluse.py:289-296,317-322`) but can fail **open** at the harness level: bare `python3` command with a 10 s timeout (`compiler.py:266-276`); a missing interpreter, crash before `run()` or timeout is non-blocking in Claude Code | — | P1.5 |
| F-15 | High | The acceptance gate (worker stayed in scope) runs on a caller-chosen folder and treats git failure as "no changes", so it can be passed without examining the real worktree; this undermines threat T4 | PR-33 | P1.5 — bind to the registered packet worktree; fail closed on git errors |
| F-16 | Medium | Deterministic gate, baseline and review targets are caller-selected and not contained to the product/project folder (goals G-C, G-F) | PR-34, PR-35 | P1.5 |
| F-17 | High | Approval binding is weaker than §1 claims: evidence identifiers not content; test-evolution reusable and self-approvable under the policy switch; promotion signature does not bind the objective that runs; promotion trusts a stored status reachable by path traversal | 5.1, PR-39 | P1.5 |
| F-19 | Low (candidate, 2026-10-24) | `hw plan` appends `/.hw/` to the host repository's shared `.git/info/exclude` with the inherited environment (an inherited `GIT_DIR` would redirect the write, probable); a `--repo-root` inside an enclosing repository edits the enclosing one. Fixed content, no approval | PR-40 | P1.5 |
| F-18 | Low | Test fault injection (`FOUNDRY_CRASH_AT`) is live in production code: an environment variable can kill Foundry writes at chosen points | PR-38 | P1.6 — test-only build flag |
| F-14 | Info | `steps/build/06_verify.md:40` still describes an unsigned `.approved` file that the code ignores; instructions that are wrong about authority teach coordinators wrong habits. **2026-10-24: fixed** (F-5.7, `26d9c58`) | — | Docs |

### 6.1 Status on 2026-10-24 and findings of Fable review 5

Status of the 2026-10-09 findings on the re-run tree (§5.3). Owner decisions are still needed
for every High; nothing here changes F-01, F-02, F-03, F-11 or F-17.

| ID | Status 2026-10-24 |
|---|---|
| F-01 | Open (unchanged) |
| F-02 | Open (unchanged); `.hw/**`, `.claude/**`, `policy.json` still absent from `DEFAULT_BLOCKED_GLOBS` |
| F-03 | Open; new route PR-43 (command center runs the `--ops-bin` program unsandboxed, environment minus `GIT_*`/`PYTHON*`) |
| F-05 | Narrowed: `rollback` and the acceptance gate refuse a folder that is not a git work tree; any git work tree is still accepted. New low route PR-44 (command-center output directory) |
| F-06 | Narrowed: identifiers validated in `codex.py`, `compiler.py`, `learning.py` (F-5.4); `pre-commit` overwrite without backup (PR-16) still open |
| F-07 | Route blocked (F-5.3); related source M1 open |
| F-08, F-09, F-10, F-12, F-13, F-16, F-18 | Open (unchanged) |
| F-11 | Open (unchanged) |
| F-14 | Fixed (F-5.7) |
| F-15 | Narrowed: git failures fail closed (F-5.2); the verdict is still computed on the caller-chosen `--worktree`, not on `packet.worktree_hint`; `_original_line_count` still returns 0 on a `git show` error (M4) |
| F-17 | Open; the path-traversal part is blocked (F-5.4); stored `approved` status still trusted, objective still unbound |
| F-19 | New candidate (PR-40) |

Findings of [Fable review 5](reviews/fable-review-20261024/report.md) (§3 there holds the
evidence and the plain-language line for each):

| ID | Sev | Finding (short) | Maps to | Status 2026-10-24 |
|---|---|---|---|---|
| F-5.1 | major | Two `model_request` lines before a reply: provider billed twice, host crash | PR-26 (F-08) | Fixed `26d9c58` |
| F-5.2 | major | Acceptance gate accepted a non-repository, missing folder or corrupt index as "no changes" | PR-33 (F-15) | Fixed `26d9c58` + `7a3a1dd` (fail closed); binding to the packet worktree still open |
| F-5.3 | major | `--diff-range` option injection writes a file | PR-05 (F-07) | Fixed |
| F-5.4 | minor | Unvalidated `run_id`/`packet_id`/`proposal_id`; cross-run packet in compiler | PR-39 (F-06, F-17) | Fixed |
| F-5.5 | minor | Dead "interrupted build" branch in `foundry.resume` | — | Fixed; `.staging-*` cleanup open |
| F-5.6 | minor | `_scenario_passed` dead code | — | Fixed |
| F-5.7 | minor | Verify step text described an unsigned `.approved` file | F-14 | Fixed |
| F-5.8 | minor | Docs said the repository has no remote | — | Fixed |
| F-5.9 | minor | No `AGENTS.md`/`CLAUDE.md` | — | Fixed by unification step U0 (`63d9349`) |
| F-5.10 | minor | Untrusted documents in the same input string as instructions (Codex provider) | PR-27 (F-08) | Open (design) |
| F-5.11 | minor | Model-call receipts lack latency, trace id, cost | PR-26 | Open |
| F-5.12 | minor | Foundry package labelled `PRODUCTION` with no human review | — | Open |
| F-5.13 | minor | `resume` sets `in_progress` while `quality.escalated` stays true | — | Open |
| F-5.14 | minor | Orphan gate challenge blocks a run; no repair command | — | Open |

## 7. Out of scope and accepted residuals

Explicitly out of scope (not prevented by design; stated in [architecture](architecture.md)
and [known limitations](known-limitations.md)):

- **Same-user attacker (A1/A2).** Any process running as the owner can edit Wilfred's installed
  source, `.hw/` state, `policy.json` and the **public** key files. Replacing a public key with
  one whose private half the attacker holds defeats every signed approval; the `key_fingerprint`
  check (`approvals.py:433-434`) compares against the same replaceable file and does not prevent
  it. Mitigation is outside Wilfred: the owner's account hygiene and offline private key.
- Concurrent writers for one project, physical power loss, filesystem corruption, multi-host
  replication (known limitations, "Persistence").
- Linux/Windows worker isolation: not supported; execution is blocked, not downgraded.
- Provider-side data handling (Z4): prompts sent to the model provider are governed by that
  provider's terms; Wilfred controls only what it sends.

Accepted residuals of the Gate 0 remediation (decided by the owner in chat; records kept in the
owner's remediation workspace outside this repository, not restated here):

| Residual | Record |
|---|---|
| x1 — timing route that captures a weakened regression baseline | `s0.2l-fix/DECISION-residuals-20261002.md` |
| m2 — timestamp-only changes not detected by the "unchanged since green" check | `s0.2l-fix/DECISION-residuals-20261002.md` |
| v10 — review key inside a non-Git run root | `s0.2m-fix/DECISION-residuals-20261003.md` |
| n1b — work hidden at the instant of the check by a lying coordinator | `s0.2m-fix/DECISION-residuals-20261003.md` |
| s8h — result file as a hard link | `s0.2m-fix/DECISION-residuals-20261003.md` |
| F3 — collection-shrink cases (e1/e3/e4) accepted at baseline capture | **Not accepted.** Open; fix candidate S0.2q awaits independent verification (`s0.2p-verify/S0.2p-VERDICT.md`) |

These residuals concern the Gate 0 candidate lineage, which is not yet integrated into
`codex/wilfred-product`; the registry above describes `ecdcb66` and must be re-run after
integration.

## 8. Review record

Reviewed 2026-10-09 by an independent read-only subagent with the instruction "find a privileged
route the registry misses or a classification that the code contradicts; cite file:line". The
author re-read the cited lines for review items 1, 2 and 5 before applying them. All items were
applied:

| # | Reviewer finding | Change made |
|---|---|---|
| 1 | Acceptance gate passable with any clean/non-git `--worktree` | PR-33, F-15 (High) |
| 2 | PR-17/18 is code execution via `.hw/` hook files, not Low | F-11 raised to High |
| 3 | `run_id`/`proposal_id` unvalidated in compiler, codex, learning; promote via traversal | PR-39; F-06 extended; F-17 |
| 4 | PR-11 runs target `post-checkout` hooks and can delete outside via symlinked dirs | PR-11 reclassified Unmediated (F-03) |
| 5 | `--attestation-file` content copied before verification | PR-37, F-10 |
| 6 | `hw review --root` uncontained | PR-34, F-16 |
| 7 | `evaluate`/`regression-baseline --target` not bound to product | PR-35, F-16 |
| 8 | `foundry execute --input-file` host file → provider | PR-36, F-08 |
| 9 | Approval properties overstated (evidence ids, test-evolution reuse/self-approval, promotion objective unbound) | §1 G-B, §4 T2, §5.1 rows, F-17 |
| 10 | PR-25 "Blocked" rests on sandbox only; manifest hash not integrity against same-user writer | PR-25 text |
| 11 | mypy plugins; `FOUNDRY_CRASH_AT` in production code | PR-07 text; PR-38, F-18 |
| 12 | Three citation corrections | Applied |

Reviewer confirmed: PR-31 (the only mediated route) holds; no unlisted network route; no code
reads private keys.

**Re-run 2026-10-24 (§5.3, §6.1).** Unattended scheduled session (resolver chain, step U5 of the
unification plan in Fable review 5 §7). The survey was done by a read-only subagent with the
instruction to re-cite every row on the current tree, list new routes and routes the 2026-10-09
registry missed; the author re-read the rows named in §5.3 before applying them. No independent
second review of the re-run was done in this session: the next re-run (after U3/U4) should
include one.
