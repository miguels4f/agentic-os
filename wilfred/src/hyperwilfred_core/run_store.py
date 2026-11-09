"""On-disk run ledger — the 'state on disk' substrate.

Forked and cleaned from ``common_py.factory.run_store``. The original dragged in
``factory.verifier`` / ``factory.workspace`` and carried Notion/Linear-shaped fields
(issue_identifier, pr_url, Notion status states). This fork keeps only the clean,
dependency-light append-only ledger: an atomic JSON record + a ``events.jsonl`` timeline,
decoupled from any one stage's evidence so it can store any stage's output.

Atomicity: every write goes temp-file-then-rename so a process killed mid-write leaves the
previous good file intact (the durability property the architecture depends on for resume).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .foundry_storage import atomic


class RunEventStatus(StrEnum):
    """Coarse status carried on a timeline event."""

    running = "running"
    accepted = "accepted"
    rejected = "rejected"
    gate_passed = "gate_passed"
    gate_failed = "gate_failed"
    awaiting_human = "awaiting_human"
    completed = "completed"
    failed = "failed"


class RunEvent(BaseModel):
    """One timeline event emitted by a run."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    event_id: str
    run_id: str
    timestamp: datetime
    stage: str
    status: RunEventStatus
    message: str
    cost_usd: float = Field(default=0.0, ge=0.0)
    duration_ms: int = Field(default=0, ge=0)
    artifacts: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(UTC)


def atomic_write_text(path: Path, text: str) -> None:
    """Use the shared durable primitive for software-ledger and Foundry writes."""
    atomic(path, text.encode("utf-8"))


class RunStore:
    """Persist a run's timeline events + arbitrary evidence under ``.hw/<run_id>/``."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir

    @property
    def events_path(self) -> Path:
        return self.run_dir / "events.jsonl"

    @property
    def evidence_dir(self) -> Path:
        return self.run_dir / "evidence"

    def init(self) -> None:
        """Create the run directory + an empty event log if missing."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if not self.events_path.exists():
            self.events_path.write_text("", encoding="utf-8")

    def append_event(
        self,
        *,
        run_id: str,
        stage: str,
        status: RunEventStatus,
        message: str,
        cost_usd: float = 0.0,
        duration_ms: int = 0,
        artifacts: list[str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> RunEvent:
        """Append one timeline event to ``events.jsonl`` (append-only, never rewritten)."""
        self.init()
        event = RunEvent(
            event_id=uuid4().hex,
            run_id=run_id,
            timestamp=_now(),
            stage=stage,
            status=status,
            message=message,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            artifacts=artifacts or [],
            metadata=metadata or {},
        )
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
        return event

    def read_events(self) -> list[RunEvent]:
        """Load the whole timeline (skips truncated trailing lines defensively)."""
        if not self.events_path.exists():
            return []
        events: list[RunEvent] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(RunEvent.model_validate_json(line))
            except ValueError:
                continue  # truncated mid-write line — ignore
        return events

    def write_evidence(self, name: str, text: str) -> Path:
        """Persist a stage's evidence artifact atomically; return its path."""
        path = self.evidence_dir / name
        atomic_write_text(path, text)
        return path

    def total_cost_usd(self) -> float:
        return round(sum(e.cost_usd for e in self.read_events()), 6)


__all__ = [
    "RunEvent",
    "RunEventStatus",
    "RunStore",
    "atomic_write_text",
]
