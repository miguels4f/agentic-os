"""Pydantic models of the snapshot (``wilfred-command-center-snapshot/v1``).

These models describe what the command center WRITES, so they forbid unknown keys. What it
READS (run stores written by any Wilfred version) is parsed tolerantly as plain dictionaries
in the collectors, never through these models or the engine's ``RunState``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION: Literal["wilfred-command-center-snapshot/v1"] = (
    "wilfred-command-center-snapshot/v1"
)

UNKNOWN = "unknown"

# Values of the ops task ``status`` field (ops design, section 7) that the inbox depends on.
OPS_BLOCKED_STATUS = "Blocked"
OPS_CLOSED_STATUSES = frozenset({"Complete", "Deferred"})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(_Model):
    """Where one fact came from: a file, the exact git command, or a web page.

    A ``url`` source is only ever an ``https://`` address (the GitHub copy of an ops file). The
    page links to it; nothing is ever loaded from it.
    """

    kind: Literal["file", "git", "url"]
    path: str
    field: str | None = None
    command: str | None = None

    @model_validator(mode="after")
    def url_is_https(self) -> Source:
        if self.kind == "url" and not self.path.startswith("https://"):
            raise ValueError("a url source must be an https:// address")
        return self


class Problem(_Model):
    """Something that could not be read. Nothing is skipped silently."""

    path: str
    what: str
    reason: str


class Remote(_Model):
    name: str
    url: str  # credentials stripped


class Worktree(_Model):
    path: str
    branch: str | None = None
    head: str | None = None
    detached: bool = False
    prunable: bool = False
    exists: bool = True
    inside_run_store: bool = False


class Project(_Model):
    id: str
    name: str
    path: str
    kind: Literal["repository", "worktree", "folder"]
    parent_repository: str | None = None
    branch: str | None = None
    head: str | None = None
    # Unknown is not zero: these four stay ``None`` when git could not answer (a ``problems``
    # entry says why) and for a plain folder, instead of reading as "clean" or "empty".
    has_commits: bool | None = None
    remotes: list[Remote] = Field(default_factory=list)
    local_branches: int | None = None
    branches_without_upstream: list[str] | None = None
    ahead: int | None = None
    behind: int | None = None
    uncommitted_paths: int | None = None
    last_commit_at: str | None = None
    worktrees: list[Worktree] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)


class QualitySummary(_Model):
    phase: str | None = None
    iteration: int | None = None
    max_iterations: int | None = None
    passed: bool | None = None
    override_present: bool = False


class LastEvent(_Model):
    timestamp: str | None = None
    stage: str | None = None
    status: str | None = None
    message: str | None = None


class PendingGate(_Model):
    gate_id: str
    kind: str | None = None
    action: str | None = None
    challenge_expires_at: str | None = None
    expired: bool | None = None


class Cost(_Model):
    """Unknown is not zero: a cost is ``unknown`` unless a usage file was recorded."""

    state: Literal["unknown", "recorded"] = "unknown"
    tokens: dict[str, int] | None = None


class ClosedMark(_Model):
    """Why a run is listed in the closed-runs register. Read from the register, never derived."""

    reason: str
    closed_on: str | None = None
    source: Source


class Run(_Model):
    run_id: str
    store_path: str
    project_id: str | None = None
    readable: bool = True
    mode: str = UNKNOWN
    tier: str = UNKNOWN
    status: str = UNKNOWN
    step_status: dict[str, str] = Field(default_factory=dict)
    current_step: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    unknown_fields: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    # ``None`` means "could not be read", never "zero".
    quality: QualitySummary | None = None
    last_event: LastEvent | None = None
    event_count: int | None = None
    pending_gates: list[PendingGate] = Field(default_factory=list)
    cost: Cost = Field(default_factory=Cost)
    duplicate_of: list[str] = Field(default_factory=list)
    # Set when this run directory is listed in the closed-runs register.
    closed: ClosedMark | None = None


class MalformedNote(_Model):
    path: str
    problem: str


class MemoryByType(_Model):
    project: int = 0
    feedback: int = 0
    user: int = 0
    reference: int = 0
    untyped: int = 0


class Memory(_Model):
    root: str
    notes: int = 0
    by_type: MemoryByType = Field(default_factory=MemoryByType)
    index_lines: int | None = None  # None: the index could not be read
    malformed: list[MalformedNote] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)


class OpsTask(_Model):
    """An open ops task that can need the owner: blocked, or with a due date."""

    id: str
    path: str  # relative to the ops root, e.g. "tasks/T-0042-decide-eslint.md"
    title: str
    status: str
    due: str | None = None  # YYYY-MM-DD
    due_end: str | None = None  # YYYY-MM-DD, end of a date range
    blocked_by: str | None = None
    projects: list[str] = Field(default_factory=list)


class OpsProposal(_Model):
    """A pending sales or schema proposal. Field NAMES only: values are never copied."""

    id: str
    path: str  # "proposals/P-0003.json"
    kind: str  # set | append | create | schema-option
    target: str  # "sales/acme.md" or "schema/sales.json"
    fields: list[str] = Field(default_factory=list)
    author: str | None = None
    created_at: str | None = None


class Ops(_Model):
    """What the owner's ``ops`` command reported about ``<workspace>/ops``.

    Unknown is not zero: a list is ``None`` when the report that provides it could not be read
    (a ``problems`` entry says why) and ``[]`` when ops answered "none".
    """

    root: str
    bin: str
    github_blob: str | None = None  # validated https://github.com/<o>/<r>/blob/<b>/ prefix
    exported_at: str | None = None
    open_tasks: list[OpsTask] | None = None
    proposals: list[OpsProposal] | None = None
    # Hand edits waiting for ``ops approve --file`` (ops-status/v1 ``unapproved_edits``): files
    # under sales/ or schema/, and task files whose authorized_by/authorized_at was typed by hand.
    unapproved_edits: list[str] | None = None
    commands: list[str] = Field(default_factory=list)  # exact command lines that were run


class AttentionSubject(_Model):
    type: Literal["run", "project", "memory", "ops"]
    id: str


class AttentionItem(_Model):
    id: str
    kind: Literal[
        "gate",
        "awaiting_human",
        "gate_failed",
        "stale_run",
        "unpushed_work",
        "unreadable",
        "memory_malformed",
        "ops_pending_proposal",
        "ops_overdue_task",
        "ops_blocked_task",
    ]
    title_es: str
    why_es: str
    deadline: str | None = None
    risk: Literal["alto", "medio", "bajo"]
    default_es: str
    if_ignored_es: str
    subject: AttentionSubject
    sources: list[Source] = Field(default_factory=list)


class Generator(_Model):
    wilfred_version: str
    product_commit: str | None = None


class Totals(_Model):
    projects: int = 0
    repositories: int = 0
    runs: int = 0  # distinct run ids
    run_copies: int = 0  # every copy found; the same run id can live in several stores
    runs_by_status: dict[str, int] = Field(default_factory=dict)
    problems: int = 0


class Snapshot(_Model):
    schema_version: Literal["wilfred-command-center-snapshot/v1"] = SCHEMA_VERSION
    generated_at: str
    workspace: str
    generator: Generator
    projects: list[Project] = Field(default_factory=list)
    runs: list[Run] = Field(default_factory=list)
    memory: Memory | None = None  # null until S2.2
    ops: Ops | None = None  # null when the workspace holds no "ops" folder
    attention: list[AttentionItem] = Field(default_factory=list)  # empty until S2.4
    problems: list[Problem] = Field(default_factory=list)
    totals: Totals = Field(default_factory=Totals)
