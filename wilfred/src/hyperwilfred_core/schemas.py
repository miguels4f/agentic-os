"""Frozen L1 contracts for HyperWilfred.

These pydantic models are the runtime-neutral interface every layer agrees on, so
neither the Claude Code adapter nor (later) the Codex adapter can drift. They are the
code equivalent of hyperresearch's JSON contracts.

Design rules:
* **Standalone.** Nothing here imports from ``common_py.factory.*`` (the import trap —
  that package eagerly drags the dropped Linear/Symphony spine back in). The keepers
  are forked, not depended on.
* **Notion/Linear/domain fields stripped.** ``BuildObjective`` is the cleaned ``TaskSpec``:
  it keeps RiskLevel / ExecutionMode / acceptance_criteria / constraints / scope and drops
  ``origin`` / ``task_type`` / ``payload`` / Notion status states.
* **Validation is load-bearing.** The resume scan validates each candidate artifact against
  its model before treating a step as "done"; a step killed mid-write leaves a truncated
  file that fails validation and is re-run rather than silently skipped.
"""

from __future__ import annotations

import fnmatch
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------- enums


class RiskLevel(StrEnum):
    """Risk classification used by gates and human-approval policy."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EstimatedComplexity(StrEnum):
    """T-shirt sizing for a build objective / slice."""

    XS = "XS"
    S = "S"
    M = "M"
    L = "L"
    XL = "XL"


class ExecutionMode(StrEnum):
    """How a build is expected to run end-to-end."""

    AUTONOMOUS = "autonomous"
    SEMI = "semi"
    HUMAN_ONLY = "human_only"


class BuildMode(StrEnum):
    """The pipeline modes. Phase 1 ships BUILD; IMPROVE is Phase 2; REVIEW is Phase 6.

    REVIEW is a standalone READ-ONLY audit — it reuses the critic panel over an arbitrary
    target and produces a findings report. It builds nothing, patches nothing, ships nothing.
    """

    BUILD = "build"
    IMPROVE = "improve"
    REVIEW = "review"


class Tier(StrEnum):
    """Tier router output, mirroring hyperresearch light/full."""

    LIGHT = "light"
    FULL = "full"


class StepKind(StrEnum):
    """What a pipeline step expects from the adapter."""

    SINGLE = "single"  # adapter executes one instruction, writes one artifact
    FANOUT = "fanout"  # adapter asks the engine for N packets and runs them in parallel
    GATE = "gate"  # engine runs a deterministic check; HARD-BLOCK on failure
    QUALITY = "quality"  # adapter drives the subjective+critic+patch loop; engine GATES it
    HUMAN_GATE = "human_gate"  # engine opens a disk-file HITL gate; BLOCKS until a human approves
    REVIEW = "review"  # adapter drives the read-only critic panel over a target; engine validates
    # findings and synthesizes the report — NO code-acceptance gate, NO writes to the reviewed repo


class Stack(StrEnum):
    """The product's technology stack — selects the deterministic gate chain.

    Phase 2 generalises the Phase-1 python-only gate floor to a per-stack registry; each
    stack name resolves (in ``evaluators.registry``) to its own ordered evaluator chain.
    """

    PYTHON = "python"
    NODE = "node"


class StepStatus(StrEnum):
    """Per-step lifecycle within a run."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class RunStatus(StrEnum):
    """Lifecycle status for a whole run."""

    CREATED = "created"
    IN_PROGRESS = "in_progress"
    GATE_FAILED = "gate_failed"
    AWAITING_HUMAN = "awaiting_human"
    COMPLETED = "completed"
    FAILED = "failed"


# --------------------------------------------------------------------------- objective


class ContextRef(BaseModel):
    """A typed pointer to supporting context for a build."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    kind: str  # "path" | "url" | "adr"
    value: str
    description: str | None = None


# Generated artifacts (single-line JSON snapshots, lockfiles, …) are data, not source: declaring
# one buys an exemption from the patch-never-regenerate rule, so only DATA-file extensions are
# accepted and a bare wildcard is refused — source files keep the protection.
GENERATED_ARTIFACT_EXTENSIONS: frozenset[str] = frozenset(
    {".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".snap", ".lock", ".yaml", ".yml", ".xml",
     ".svg", ".txt"}
)


# Data-extension files that nonetheless CONTROL execution (installs, CI, containers, tooling):
# rewriting one wholesale is a supply-chain change, not a snapshot refresh, so they can never be
# declared generated — a dependency/CI change goes through its own human gate. Matched on the
# lower-cased basename (fnmatch) and on any directory segment.
GENERATED_ARTIFACT_DENIED_NAMES: tuple[str, ...] = (
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "pnpm-workspace.yaml", "bun.lock", "uv.lock", "poetry.lock", "pipfile.lock", "cargo.lock",
    "gemfile.lock", "composer.json", "composer.lock", "go.sum", "*requirements*.txt",
    "constraints*.txt", "tsconfig*.json", "jsconfig*.json", "deno.json", "deno.lock",
    "docker-compose*.yml", "docker-compose*.yaml", "compose.yml", "compose.yaml",
    ".pre-commit-config.yaml", ".gitlab-ci.yml", "bitbucket-pipelines.yml", "azure-pipelines.yml",
    "renovate.json", ".renovaterc*", "vercel.json", "netlify.toml", "fly.toml", "app.yaml",
    "serverless.yml", "serverless.yaml", "mkdocs.yml", ".eslintrc*", ".prettierrc*",
    "biome.json", "turbo.json", "nx.json", "lerna.json", "manifest.json",
)
GENERATED_ARTIFACT_DENIED_DIRS: frozenset[str] = frozenset(
    {".github", ".gitlab", ".circleci", ".husky", ".devcontainer", ".vscode", ".idea", ".hw",
     ".claude", ".codex", "node_modules", ".git"}
)
_GLOB_CHARS = frozenset("*?[]")


def is_denied_generated_path(path: str) -> bool:
    """True if *path* (concrete or a declared pattern) may never be exempted as generated.

    Checked twice: on the declaration (literal names) and again on every CONCRETE path a
    wildcard declaration would exempt at acceptance time (``guardrails.patch_violations``).
    """
    segments = path.strip().replace("\\", "/").lower().split("/")
    if any(seg in GENERATED_ARTIFACT_DENIED_DIRS for seg in segments[:-1]):
        return True
    return any(fnmatch.fnmatchcase(segments[-1], pat) for pat in GENERATED_ARTIFACT_DENIED_NAMES)


class GeneratedArtifact(BaseModel):
    """A file the packet/objective declares as GENERATED, with its regeneration command.

    ``command`` is REQUIRED — an exemption or a reproducibility check without a documented way
    to regenerate the bytes is refused. ``volatile_fields`` names JSON keys (dotted paths) that
    legitimately differ between regenerations (e.g. ``generated_at``); the reproducibility
    check ignores exactly those.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    path: str
    command: str
    volatile_fields: list[str] = Field(default_factory=list)

    @field_validator("volatile_fields")
    @classmethod
    def _volatile_narrow(cls, v: list[str]) -> list[str]:
        for field in v:
            parts = field.split(".")
            if not field.strip() or any(not p for p in parts) or parts[-1] == "*":
                raise ValueError(
                    f"volatile field {field!r} is too broad — name a concrete key (a '*' may "
                    "stand for an intermediate segment, never for the key that is ignored)"
                )
        return v

    @field_validator("command")
    @classmethod
    def _command_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("a generated artifact must declare its regeneration command")
        return v

    @field_validator("path")
    @classmethod
    def _path_narrow(cls, v: str) -> str:
        norm = v.strip().replace("\\", "/")
        base = norm.rsplit("/", 1)[-1]
        ext = "." + base.rsplit(".", 1)[-1].lower() if "." in base else ""
        segments = norm.split("/")
        bad = (
            not norm
            or norm.startswith(("/", "./", "~"))
            or (len(norm) > 1 and norm[1] == ":")  # Windows drive
            or ".." in segments
            or "**" in norm  # no recursive globs: declare a specific directory
            or base in {"*", "**"}
            or ("*" in base and "/" not in norm)  # a wildcard file name needs a directory prefix
            or ext not in GENERATED_ARTIFACT_EXTENSIONS
            # a wildcard may only name files inside ONE concrete directory
            or any(ch in _GLOB_CHARS for seg in segments[:-1] for ch in seg)
        )
        if not bad and is_denied_generated_path(norm):
                raise ValueError(
                    f"generated artifact path {v!r} names a file that controls installs, CI or "
                    "tooling — it can never be declared generated (route a dependency/CI change "
                    "through its human gate instead)"
                )
        if bad:
            raise ValueError(
                f"generated artifact path {v!r} is too broad or not a data file (or escapes the "
                "repo) — declare a specific repo-relative artifact (no '..', no absolute path, "
                f"no '**'; wildcard names need a directory) with one of "
                f"{sorted(GENERATED_ARTIFACT_EXTENSIONS)}"
            )
        return norm


class LiveDemo(BaseModel):
    """The slice's promised demo, checked against CURRENT data by the verify step.

    Give a read-only ``command`` (the engine runs it in the repo root) OR an ``evidence_path``
    the adapter wrote from a real run. The demo counts as achievable only if the output is
    non-empty, contains every ``must_contain`` marker and none of the ``failure_markers``
    ("every action blocked", "0 results", …) — anything else is a FINDING, never asserted as
    correct by a test.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    promise: str
    command: str = ""
    evidence_path: str = ""
    must_contain: list[str] = Field(default_factory=list)
    failure_markers: list[str] = Field(default_factory=list)


class BuildObjective(BaseModel):
    """The cleaned build contract (forked from TaskSpec, domain fields stripped).

    This is what the founder's one-line objective becomes after intake. The AI
    ``decompose`` step (executed by the adapter, never the engine) turns it into a
    :class:`Plan` of :class:`Slice` items.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    title: str
    objective: str  # the founder's problem statement / what they typed
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    context_refs: list[ContextRef] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    estimated_complexity: EstimatedComplexity = EstimatedComplexity.M
    execution_mode: ExecutionMode = ExecutionMode.SEMI
    stack: Stack = Stack.PYTHON  # selects the per-stack deterministic gate chain
    # Requirement/part IDs (e.g. "B2.3", or a prefix wildcard "A*") the objective explicitly
    # declares out of scope — the traceability check treats them as accounted for.
    out_of_scope: list[str] = Field(default_factory=list)
    # Critic lenses for the quality loop. Empty = automatic: the four standard lenses, plus the
    # adversarial lens when the objective mentions personal data, gates, approvals or security.
    # The opt-in lenses ``cross-slice-drift`` and ``ai-failure-modes`` are never automatic: name
    # them here (BUILD/IMPROVE) or pass ``hw review --lens`` (REVIEW keeps the standard four).
    critic_lenses: list[str] = Field(default_factory=list)
    # Artifacts the objective declares as GENERATED (with their regeneration command and volatile
    # fields): the verify step regenerates them in a fresh worktree and fails on any drift.
    generated_artifacts: list[GeneratedArtifact] = Field(default_factory=list)
    live_demo: LiveDemo | None = None


# --------------------------------------------------------------------------- plan / slices


class Slice(BaseModel):
    """One atomic build item with its own machine-readable write scope.

    ``allowed_globs`` / ``blocked_globs`` are the guardrail allow-list: the engine
    rejects (at acceptance time) any write that matches no allow-glob or any blocked-glob.
    Waves are planned from these declared scopes, NOT from a predicted-changed-paths guess.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    title: str
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    allowed_globs: list[str] = Field(default_factory=list)
    blocked_globs: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    max_files: int = Field(default=20, ge=1)
    max_changed_lines: int = Field(default=400, ge=1)
    # Generated artifacts this slice (re)writes wholesale, each with its regeneration command.
    generated_artifacts: list[GeneratedArtifact] = Field(default_factory=list)


class Plan(BaseModel):
    """The decompose step's output: the objective plus its atomic slices."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    objective_id: str
    product_dir: str  # where the built product lives, relative to repo root
    slices: list[Slice] = Field(default_factory=list)


# --------------------------------------------------------------------------- guardrails


class GuardrailConfig(BaseModel):
    """Machine-readable write-scope + surgical-edit budgets for a run.

    ``global_blocked_globs`` are ALWAYS rejected regardless of any slice allow-list —
    the blocked-by-default protected-paths floor (secrets, .git, .env, migrations).
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    run_id: str
    global_blocked_globs: list[str] = Field(
        default_factory=lambda: list(DEFAULT_BLOCKED_GLOBS)
    )
    max_changed_lines: int = Field(default=400, ge=1)
    max_hunk_span: int = Field(default=120, ge=1)
    full_file_ratio: float = Field(default=0.9, gt=0.0, le=1.0)


# NOTE on .git: writes *inside* .git/ are structurally invisible to `git status`
# (the engine's change source), so this glob cannot catch them at acceptance time — and it
# does not need to: git itself refuses to commit files under .git/, and a sub-agent
# corrupting .git is a separate threat surface from "wrote an out-of-scope source file."
# The glob is kept as honest defense-in-depth (it blocks any path that DOES surface as .git/*,
# e.g. a stray top-level `.git` file), not as a claim of full .git/ protection. The realistic
# secret-exfiltration case (.env, secrets/, *.pem) IS fully caught.
DEFAULT_BLOCKED_GLOBS: tuple[str, ...] = (
    ".git/**",
    ".git",
    "**/.env",
    "**/.env.*",
    "**/secrets/**",
    "**/*.pem",
    "**/*.key",
    "**/id_rsa*",
    "**/.aws/**",
    "**/.ssh/**",
    "**/migrations/**",
    # The human-declared known-failures list: never a worker write (known_failures.py).
    "**/.hw-known-failures.json",
)


# --------------------------------------------------------------------------- human gates (HITL)


class GateKind(StrEnum):
    """The class of irreversible action a disk-file HITL gate guards.

    Before any of these the engine writes a ``.pending`` gate file and BLOCKS until a human
    approves — the architecture's third enforcement point (spec §2, human gates). Mirrors the
    irreversible-action surfaces the protected-paths classifier routes on.
    """

    DEPLOY = "deploy"  # ship to an environment / promote
    MERGE = "merge"  # merge to main / open+merge a PR
    DEPS = "deps"  # dependency bumps (lockfile / manifest changes)
    SECRETS = "secrets"  # anything touching secrets / credentials / keys
    MIGRATION = "migration"  # schema migrations (data-destructive)


class GateDecision(StrEnum):
    """The human's decision on a gate — the legacy HITL vocab, persisted to the run store."""

    APPROVE = "approve"
    REJECT = "reject"
    MODIFY_SCOPE = "modify_scope"


class GateStatus(StrEnum):
    """Lifecycle of one disk-file HITL gate."""

    PENDING = "pending"  # ``.pending`` written; the engine is BLOCKED awaiting a human
    APPROVED = "approved"  # a trusted host verified a signed, challenge-bound human decision
    REJECTED = "rejected"  # a human rejected — the gated action does NOT proceed
    MODIFIED = "modified"  # a human asked to modify scope before proceeding


class HumanGate(BaseModel):
    """One irreversible-action gate, persisted as ``.hw/<run_id>/gates/<gate_id>.json``.

    The engine NEVER sets ``status`` to anything but PENDING on its own — only a verified signed
    human decision flips it. An unapproved gate hard-blocks the run; the engine cannot self-approve.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    gate_id: str
    run_id: str
    kind: GateKind
    step_id: str | None = None  # the pipeline step this gate is attached to (e.g. "ship")
    action: str = ""  # human-readable description of the irreversible action
    paths: list[str] = Field(default_factory=list)  # sensitive paths that triggered the gate
    categories: list[str] = Field(default_factory=list)  # protected-path categories hit
    evidence: list[str] = Field(default_factory=list)  # artifacts the human should review
    status: GateStatus = GateStatus.PENDING
    decision: GateDecision | None = None
    decided_by: str | None = None
    note: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None
    # Prominent notices the approving human must see (e.g. quality passed on adapter
    # self-review, a recorded human override). Empty for a clean gate.
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- packets / verdicts


class Packet(BaseModel):
    """A self-contained sub-agent task packet emitted by ``hw fanout-plan``.

    The engine builds these; an adapter dispatches each one to an isolated sub-agent
    (its own clean context + tool-lock + git worktree). The engine never spawns it.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    packet_id: str
    run_id: str
    step_id: str
    slice_id: str
    title: str
    instruction: str  # the fresh-loaded step instruction the worker must follow
    allowed_globs: list[str]
    blocked_globs: list[str]
    max_files: int
    max_changed_lines: int
    max_hunk_span: int
    full_file_ratio: float
    worktree_hint: str  # suggested isolated working dir for this packet
    result_path: str  # the KNOWN DISK PATH the worker writes its result to (never stdout)
    generated_artifacts: list[GeneratedArtifact] = Field(default_factory=list)


class PatchViolation(BaseModel):
    """A patch-never-regenerate violation on a modified tracked file."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    path: str
    added: int
    deleted: int
    reasons: list[str]


class AcceptanceVerdict(BaseModel):
    """The engine acceptance gate's verdict for one packet's worktree."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    packet_id: str
    worktree: str
    changed_paths: list[str] = Field(default_factory=list)
    escapes: list[str] = Field(default_factory=list)
    patch_violations: list[PatchViolation] = Field(default_factory=list)
    result_read_from_disk: bool = False
    result_bytes: int = 0
    accepted: bool = False
    rolled_back: bool = False
    tree_clean_after: bool | None = None


# --------------------------------------------------------------------------- quality loop


class CriticSeverity(StrEnum):
    """Severity of a subjective issue or an adversarial-critic finding."""

    BLOCKER = "blocker"  # serious — never auto-waived; escalates to a human after N rounds
    MAJOR = "major"  # reduces quality materially — feeds the patcher while iterating
    MINOR = "minor"  # nice-to-have — recorded, does not block the gate


class ReviewAuthor(StrEnum):
    """Who produced an evaluation/critique record (review provenance, fail-closed).

    ``independent`` = a dispatched sub-agent whose result the adapter read back from disk.
    ``adapter`` = written by the orchestrating session about its own work. A record with no
    ``author`` field (older run state) is loaded as ``adapter`` — missing provenance never
    counts as independent.
    """

    INDEPENDENT = "independent"
    ADAPTER = "adapter"


class QualityPhase(StrEnum):
    """Where the quality loop currently sits (the adapter-driven sub-state machine)."""

    EVALUATE = "evaluate"  # adapter spawns the 0-5 evaluator + the adversarial critics
    PATCH = "patch"  # adapter spawns the surgical patcher to fix open findings
    PASSED = "passed"  # subjective>=threshold and no open blocker — loop done
    FALLBACK = "fallback"  # best-version-after-N (SUBJECTIVE shortfall only)
    ESCALATED = "escalated"  # an open BLOCKER survived N rounds — handed to a human


class SubjectiveCriterion(BaseModel):
    """One 0-5 subjective-quality criterion scored by the LLM evaluator."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str
    score: int = Field(ge=0, le=5)
    justification: str = ""


class SubjectiveIssue(BaseModel):
    """An actionable issue the evaluator raises for the patcher to address."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    severity: CriticSeverity
    criterion: str = ""
    description: str
    suggestion: str = ""
    tags: list[str] = Field(default_factory=list)  # e.g. personal_data_leak (severity floor)
    severity_raised_from: CriticSeverity | None = None  # set by the engine's severity floor


class SubjectiveEvaluation(BaseModel):
    """The 0-5 subjective evaluator's output (MIN-not-average gate).

    The engine RECOMPUTES ``global_score`` as the MIN of the criteria scores and ignores any
    value the LLM wrote here — so an evaluator cannot game the gate by reporting a high
    aggregate while individual criteria are low. ``global_score`` is informational on input;
    authoritative only after the engine overwrites it.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    evaluator: str
    author: ReviewAuthor = ReviewAuthor.ADAPTER  # fail closed: unlabelled = adapter-authored
    iteration: int = Field(default=0, ge=0)
    criteria: list[SubjectiveCriterion] = Field(default_factory=list)
    global_score: int = Field(default=0, ge=0, le=5)  # engine overwrites with MIN(criteria)
    issues: list[SubjectiveIssue] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class CriticFinding(BaseModel):
    """One finding emitted by an adversarial code critic reviewing the changed code."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    lens: str
    severity: CriticSeverity
    file: str = ""
    line: int | None = None
    title: str
    description: str = ""
    suggestion: str = ""
    tags: list[str] = Field(default_factory=list)  # e.g. personal_data_leak (severity floor)
    severity_raised_from: CriticSeverity | None = None  # set by the engine's severity floor
    # Adversarial-lens evidence: the command actually run and the observed output (L2).
    evidence_command: str = ""
    evidence_output: str = ""


class CritiqueProbe(BaseModel):
    """One hostile input the ADVERSARIAL critic actually ran: the command and what it observed."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    input_class: str  # e.g. "privacy: accented/lowercase name", "gate bypass: DO_NOT_CONTACT"
    command: str
    observed_output: str


class CritiqueReport(BaseModel):
    """One adversarial critic's findings JSON (read-only — the critic never mutates code)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    critic: str
    author: ReviewAuthor = ReviewAuthor.ADAPTER  # fail closed: unlabelled = adapter-authored
    lens: str
    findings: list[CriticFinding] = Field(default_factory=list)
    # The adversarial lens must list every hostile input it ran (empty for the other lenses).
    probes: list[CritiqueProbe] = Field(default_factory=list)


class CritiquePacket(BaseModel):
    """A read-only critic task packet emitted by ``hw critique-plan``.

    The critic reads the changed files under ``product_dir`` (listed in ``changed_paths``),
    applies its lens, and writes a :class:`CritiqueReport` to ``result_path``. It writes NO
    code — so there is no acceptance gate, only schema validation of its findings.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    packet_id: str
    run_id: str
    iteration: int
    lens: str
    instruction: str
    product_dir: str
    changed_paths: list[str] = Field(default_factory=list)
    result_path: str


class PatchPacket(BaseModel):
    """The surgical-patcher task packet emitted by ``hw patch-plan``.

    The patcher is tool-locked to Read+Edit (it physically cannot Write a whole file on
    Claude Code) and must address ONLY the open findings at ``findings_path`` with surgical
    edits. The engine enforces patch-never-regenerate at acceptance time (``hw record-patch``)
    with TIGHT budgets — the guarantee lives in the engine, not the runtime.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    packet_id: str
    run_id: str
    iteration: int
    instruction: str
    findings_path: str
    allowed_globs: list[str]
    blocked_globs: list[str]
    max_files: int
    max_changed_lines: int
    max_hunk_span: int
    full_file_ratio: float
    product_dir: str
    worktree_hint: str
    result_path: str
    generated_artifacts: list[GeneratedArtifact] = Field(default_factory=list)


class QualityIterationRecord(BaseModel):
    """One row of the quality loop's per-iteration history."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    iteration: int
    subjective_global_score: int | None = None
    open_blocker: int = 0
    open_major: int = 0
    deterministic_green: bool = False
    patched: bool = False


class IndependentReviewOverride(BaseModel):
    """An explicit, recorded human decision to accept adapter-authored review (who/when/why)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    by: str
    reason: str
    iteration: int = Field(ge=0)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    gate_id: str = ""
    evidence: list[str] = Field(default_factory=list)
    claimed_by: str = ""
    claimed_reason: str = ""


class QualityLoopState(BaseModel):
    """The quality loop's on-disk state, embedded in :class:`RunState`.

    Shared by BUILD and IMPROVE — only the *front* of the pipeline differs; the evaluator +
    gate + patcher core is identical. ``det_gate_step`` names which step is the deterministic
    hard wall for this run (``evaluate`` for BUILD, ``regression`` for IMPROVE) so the gate's
    precedence check is mode-agnostic.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    step_id: str = "quality"
    det_gate_step: str = "evaluate"
    iteration: int = 0
    max_iterations: int = Field(default=3, ge=1)
    subjective_threshold: int = Field(default=4, ge=0, le=5)
    phase: QualityPhase = QualityPhase.EVALUATE
    last_global_score: int | None = None
    best_global_score: int | None = None
    best_iteration: int | None = None
    open_blocker: int = 0
    open_major: int = 0
    open_minor: int = 0
    history: list[QualityIterationRecord] = Field(default_factory=list)
    passed: bool = False
    used_fallback: bool = False
    escalated: bool = False
    independent_review_override: IndependentReviewOverride | None = None
    # sha256 of each engine-written evaluation/critique record ("<iter>:<lens|subjective>") so a
    # record edited or dropped on disk after recording is not trusted as independent.
    record_hashes: dict[str, str] = Field(default_factory=dict)
    # Signed execution attestations accepted for those records.  The value is the challenge id;
    # provenance re-verifies its signature, exact record hash, and consumption receipt.
    review_attestations: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- pipeline / run state


class PipelineStep(BaseModel):
    """One step in the BUILD (or IMPROVE) graph."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    title: str
    kind: StepKind
    instruction_file: str  # relative path under the engine's steps/ tree
    produces: str | None = None  # artifact filename this step is expected to write (run-relative)


class RunState(BaseModel):
    """The whole on-disk run state (``.hw/<run_id>/state.json``)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    run_id: str
    mode: BuildMode
    tier: Tier
    status: RunStatus
    objective: BuildObjective
    product_dir: str
    steps: list[PipelineStep]
    step_status: dict[str, StepStatus] = Field(default_factory=dict)
    quality: QualityLoopState | None = None  # set when the run reaches the quality step
    # HEAD of the repo when `hw plan` ran. The known-failures declaration is read AS COMMITTED
    # here, so nothing done during the run can widen it (see known_failures.py).
    base_commit: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def next_pending_step(self) -> PipelineStep | None:
        """First step not yet done — the resume-safe cursor (derived, never stored)."""
        for step in self.steps:
            if self.step_status.get(step.id, StepStatus.PENDING) != StepStatus.DONE:
                return step
        return None


# --------------------------------------------------------------------------- Phase 5: observability
#
# The disk-observing anomaly/alert/dashboard contracts, RETARGETED from the legacy
# (client case-study) signals to CODE signals computed from ``.hw/*/`` — events.jsonl,
# acceptance verdicts, and the quality-loop history. These are facts the engine reads off
# disk, never model judgment.


class AnomalySeverity(StrEnum):
    """Severity of a code-signal anomaly (mirrors the legacy anomaly/alert vocab)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AnomalyCode(StrEnum):
    """The CODE anomaly signals the disk observer computes (spec §3, retargeted)."""

    PATCH_LOOP_MAXED = "patch_loop_maxed"  # quality loop hit max_iterations without a clean pass
    DIFF_OVER_BUDGET = "diff_over_budget"  # a packet's diff blew far past its changed-lines budget
    HIGH_REJECT_RATE = "high_reject_rate"  # a large share of packets were rejected by acceptance
    REPEATED_GATE_FAIL = "repeated_gate_fail"  # the deterministic gate failed repeatedly
    HIGH_ROLLBACK_RATE = "high_rollback_rate"  # a large share of packets were rolled back
    ESCALATION_SPIKE = "escalation_spike"  # the run escalated to a human (quality blocker survived)


class CodeAnomaly(BaseModel):
    """One anomaly flagged against a run's on-disk evidence (the alert payload)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    code: AnomalyCode
    severity: AnomalySeverity
    run_id: str
    metric: str
    actual: float
    threshold: float
    message: str
    evidence_refs: list[str] = Field(default_factory=list)


class RunObservation(BaseModel):
    """A per-run summary mined from disk — the observer's unit of measurement."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    run_id: str
    mode: BuildMode
    tier: Tier
    status: RunStatus
    product_dir: str
    stack: Stack
    total_events: int = 0
    accepted_packets: int = 0
    rejected_packets: int = 0
    rolled_back_packets: int = 0
    diff_over_budget_packets: int = 0
    gate_failures: int = 0
    gate_passes: int = 0
    quality_iterations: int = 0
    quality_max_iterations: int = 0
    quality_passed: bool = False
    quality_used_fallback: bool = False
    quality_escalated: bool = False
    quality_open_blocker: int = 0
    escalations: int = 0
    anomaly_codes: list[str] = Field(default_factory=list)
    # When the run was created, only if its state file records it (never defaulted to "now").
    created_at: datetime | None = None


class Dashboard(BaseModel):
    """The aggregate observability view (JSON; a text view is rendered from it)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    generated_at: datetime
    total_runs: int = 0
    runs_by_status: dict[str, int] = Field(default_factory=dict)
    runs_by_mode: dict[str, int] = Field(default_factory=dict)
    total_accepted_packets: int = 0
    total_rejected_packets: int = 0
    total_gate_failures: int = 0
    total_escalations: int = 0
    anomalies: list[CodeAnomaly] = Field(default_factory=list)
    observations: list[RunObservation] = Field(default_factory=list)


class CodebaseMemoryEntry(BaseModel):
    """One prior run's outcome, indexed so the analyzer/dashboard can reference it.

    The lightweight 'retrieval' retarget (spec §3): codebase memory = a disk index of prior
    runs' outcomes, NOT a vector store. Keyed by run_id; the analyzer reads it to see whether
    a signal recurs across runs.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    run_id: str
    mode: BuildMode
    status: RunStatus
    product_dir: str
    tier: Tier
    quality_passed: bool = False
    anomaly_codes: list[str] = Field(default_factory=list)


class CodebaseMemoryIndex(BaseModel):
    """The on-disk codebase-memory index (``.hw/observability/memory.json``)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    generated_at: datetime
    entries: list[CodebaseMemoryEntry] = Field(default_factory=list)


# ------------------------------------------------------------------------ Phase 5: escalation sink


class EscalationKind(StrEnum):
    """The class of event routed to the runtime-neutral escalation sink."""

    ANOMALY = "anomaly"  # an observability anomaly crossed the alert threshold
    QUALITY = "quality"  # the quality loop escalated an unresolved blocker
    GATE = "gate"  # a human gate needs attention
    OTHER = "other"


class Escalation(BaseModel):
    """One escalation persisted to the run store + the founder-facing notification artifact.

    Replaces the Linear-coupled ``escalation_handler.py`` with a runtime-neutral sink: it writes
    to ``events.jsonl`` + an escalations ledger on disk and surfaces a notification file a human
    (or any adapter) reads. No Linear, no external service, no network.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    escalation_id: str
    run_id: str
    kind: EscalationKind
    severity: AnomalySeverity
    reason: str
    source: str = ""  # what raised it (e.g. "observability", "quality")
    evidence_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# --------------------------------------------------------------------------- Phase 5: learning loop
#
# The learning analyzer adapted from the ``learning_loop.py`` PATTERN: mine the on-disk run
# history → emit REVIEW-ONLY proposals that are themselves IMPROVE objectives. The factory does
# not silently rewrite its own rules (constraint #2): every proposal carries
# ``requires_human_review=True`` / ``auto_apply=False`` and is promoted to an active run ONLY
# after a human approves its review gate.


class ProposalType(StrEnum):
    """The kind of self-improvement a learning proposal suggests."""

    GUARDRAIL = "guardrail"  # tighten a guardrail (changed-line budget, blocked globs, slice scope)
    ACCEPTANCE_CRITERION = "acceptance_criterion"  # add an acceptance criterion
    PROMPT_RULE = "prompt_rule"  # fix a recurring step/prompt weakness
    TEST_COVERAGE = "test_coverage"  # cover a recurring test gap


class ProposalStatus(StrEnum):
    """Lifecycle of a learning proposal — never auto-advances past PROPOSED without a human."""

    PROPOSED = "proposed"  # emitted by the analyzer; inert until a human acts
    UNDER_REVIEW = "under_review"  # a disk-file human-review gate is open and BLOCKING
    APPROVED = "approved"  # a human approved the review gate
    REJECTED = "rejected"  # a human rejected the proposal
    PROMOTED = "promoted"  # promoted to an active IMPROVE run (only after APPROVED)


class LearningSignal(BaseModel):
    """One observed on-disk pattern that can justify a learning proposal (deterministic)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    code: AnomalyCode
    run_id: str
    severity: AnomalySeverity
    message: str
    evidence_refs: list[str] = Field(default_factory=list)


# How the proposal confidence is computed — shown next to the number so nobody reads it as a
# probability. It ignores how many runs were observed in total (3 of 3 and 3 of 300 score alike).
LEARNING_CONFIDENCE_BASIS = (
    "heuristic: 0.45 + 0.15 per affected run, capped at 0.9 — not a probability, and it ignores "
    "how many runs were observed in total"
)


class PromotionOutcome(StrEnum):
    """What the run history shows after a proposal was promoted (a count, never a proof)."""

    FIX_NOT_SHIPPED = "fix_not_shipped"  # the promoted IMPROVE run has not completed
    TOO_FEW_RUNS = "too_few_runs"  # fewer than 3 finished runs on one side of the promotion
    FEWER_FLAGGED = "fewer_flagged"  # a lower share of finished runs flagged since
    NOT_FEWER = "not_fewer"  # the same or a higher share of finished runs flagged since


class PromotionEffect(BaseModel):
    """Share of finished BUILD/IMPROVE runs flagged with the proposal's signal, before vs after.

    Proposals are created when a problem peaks, so a drop afterwards is expected anyway: this is
    a count comparison, not proof that the change caused it.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    promoted_run_id: str
    promoted_at: datetime
    fix_shipped: bool
    runs_before: int
    flagged_before: int
    runs_after: int
    flagged_after: int
    outcome: PromotionOutcome


class LearningProposal(BaseModel):
    """One review-only proposal, materialized as an IMPROVE :class:`BuildObjective`.

    THE non-negotiable invariant: ``requires_human_review`` is always True and ``auto_apply`` is
    always False. A proposal becomes an active run ONLY via ``hw promote`` AFTER a human approves
    its review gate — there is no engine path that applies a proposal without a human file.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    proposal_id: str
    proposal_type: ProposalType
    title: str
    rationale: str
    suggested_change: str
    affected_area: str
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_basis: str = LEARNING_CONFIDENCE_BASIS
    evidence_refs: list[str] = Field(default_factory=list)
    source_run_ids: list[str] = Field(default_factory=list)
    # Total runs observed when the proposal was computed (None = not recorded: older proposal).
    runs_observed: int | None = None
    # Recomputed by `hw learn` once the proposal is promoted; informational only.
    effect_after_promotion: PromotionEffect | None = None
    # the materialized IMPROVE objective (run via `hw plan --mode improve`)
    objective: BuildObjective
    status: ProposalStatus = ProposalStatus.PROPOSED
    requires_human_review: bool = True
    auto_apply: bool = False
    review_gate_id: str | None = None  # the disk-file HITL gate guarding promotion
    promoted_run_id: str | None = None  # the IMPROVE run id once promoted
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LearningProposalSet(BaseModel):
    """The review packet the analyzer emits (rendered to JSON + a founder-readable markdown)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    generated_at: datetime
    signals: list[LearningSignal] = Field(default_factory=list)
    proposals: list[LearningProposal] = Field(default_factory=list)
    next_action: str = (
        "A human reviews each proposal and approves its review gate before it becomes an "
        "active IMPROVE run; the engine never auto-applies a change to the factory."
    )


# --------------------------------------------------------------------------- Phase 6: review mode
#
# REVIEW is a standalone READ-ONLY audit: point the EXISTING critic panel (the CRITIC_LENSES +
# CritiquePacket/CritiqueReport/CriticFinding contracts) at an arbitrary target — a path subset,
# the whole repo, or a git diff range — and synthesize the findings into a ReviewReport. NO
# building, NO patching, NO shipping. The critics stay read-only/tool-locked and REVIEW writes
# nothing to the reviewed repo (only findings under ``.hw/``). The same findings can later drive
# the IMPROVE patcher (review → improve continuity) or feed the Phase-5 learning loop.


class ReviewTargetKind(StrEnum):
    """How the review target is declared."""

    PATHS = "paths"  # explicit paths / globs under ``root`` (an empty list ⇒ the whole repo)
    DIFF = "diff"  # a git diff range (e.g. "main...HEAD", "HEAD~3..HEAD")


class ReviewTarget(BaseModel):
    """The resolved review target the critic panel audits (persisted as ``review-target.json``).

    Resolution is deterministic and engine-side (git / glob — never an LLM). ``resolved_paths`` is
    the concrete file list the read-only critics review; it is what the (generalized) critic
    fan-out hands each critic instead of a build's changed-product paths.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    run_id: str
    kind: ReviewTargetKind
    root: str = "."  # repo-relative root the paths/diff resolve against
    paths: list[str] = Field(default_factory=list)  # PATHS: the requested globs/paths
    diff_range: str = ""  # DIFF: the requested range
    resolved_paths: list[str] = Field(default_factory=list)  # concrete files the critics review
    total_matched: int = 0  # files matched before any cap (so a cap is never silent)
    truncated: bool = False  # True iff ``resolved_paths`` was capped below ``total_matched``


class ReviewVerdict(StrEnum):
    """The overall verdict of a standalone review (derived from the worst finding severity)."""

    PASS = "pass"  # no blocker/major/minor findings
    ADVISORY = "advisory"  # only minor findings — worth noting, nothing blocking
    CHANGES_REQUESTED = "changes_requested"  # at least one blocker or major finding
    # No blocker/major, but at least one lens was not independently written (the orchestrating
    # session reviewed its own target, or a lens is missing): never reported as pass/advisory.
    PENDING_INDEPENDENT_REVIEW = "pending_independent_review"


class ReviewReport(BaseModel):
    """The synthesized standalone-review findings report (``review-report.json`` + ``.md``).

    Aggregates every read-only critic's :class:`CriticFinding` over the declared target, grouped by
    severity, with file:line refs and an overall verdict + summary. The critics write NO code, so
    there is no acceptance gate — only schema validation of their findings and this synthesis.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    run_id: str
    generated_at: datetime
    target: ReviewTarget
    lenses: list[str] = Field(default_factory=list)
    findings: list[CriticFinding] = Field(default_factory=list)
    counts_by_severity: dict[str, int] = Field(default_factory=dict)
    counts_by_lens: dict[str, int] = Field(default_factory=dict)
    files_reviewed: int = 0
    verdict: ReviewVerdict = ReviewVerdict.PASS
    # The verdict from finding severities alone, before the authorship rule is applied.
    severity_verdict: ReviewVerdict = ReviewVerdict.PASS
    # lens -> "independent" | "adapter" (self-review) | "missing"; see quality.critic_authorship.
    critic_authors: dict[str, str] = Field(default_factory=dict)
    independent: bool = False  # True iff every lens of the panel is independent
    summary: str = ""


__all__ = [
    "DEFAULT_BLOCKED_GLOBS",
    "GENERATED_ARTIFACT_DENIED_DIRS",
    "GENERATED_ARTIFACT_DENIED_NAMES",
    "GENERATED_ARTIFACT_EXTENSIONS",
    "GeneratedArtifact",
    "is_denied_generated_path",
    "LiveDemo",
    "IndependentReviewOverride",
    "ReviewAuthor",
    "AcceptanceVerdict",
    "AnomalyCode",
    "AnomalySeverity",
    "BuildMode",
    "BuildObjective",
    "CodeAnomaly",
    "CodebaseMemoryEntry",
    "CodebaseMemoryIndex",
    "ContextRef",
    "CriticFinding",
    "CritiqueProbe",
    "CriticSeverity",
    "CritiquePacket",
    "CritiqueReport",
    "Dashboard",
    "Escalation",
    "EscalationKind",
    "EstimatedComplexity",
    "ExecutionMode",
    "GateDecision",
    "GateKind",
    "GateStatus",
    "GuardrailConfig",
    "HumanGate",
    "LEARNING_CONFIDENCE_BASIS",
    "LearningProposal",
    "PromotionEffect",
    "PromotionOutcome",
    "LearningProposalSet",
    "LearningSignal",
    "Packet",
    "PatchPacket",
    "PatchViolation",
    "PipelineStep",
    "Plan",
    "ProposalStatus",
    "ProposalType",
    "QualityIterationRecord",
    "QualityLoopState",
    "QualityPhase",
    "ReviewReport",
    "ReviewTarget",
    "ReviewTargetKind",
    "ReviewVerdict",
    "RiskLevel",
    "RunObservation",
    "RunState",
    "RunStatus",
    "Slice",
    "Stack",
    "StepKind",
    "StepStatus",
    "SubjectiveCriterion",
    "SubjectiveEvaluation",
    "SubjectiveIssue",
    "Tier",
]
