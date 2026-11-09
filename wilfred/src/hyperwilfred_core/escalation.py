"""Runtime-neutral escalation sink — the replacement for the Linear-coupled handler.

RECYCLE-TABLE verdict (spec §3): ``escalation_handler.py`` is **drop** — it is hard-coupled to
Linear comments (``LinearConnector.add_issue_comment``). This is its clean replacement: a sink
that writes escalations to the run store (``events.jsonl`` + a per-run ``escalations.jsonl``
ledger) and surfaces a **founder-facing notification artifact** (a markdown file a human or any
adapter reads). No Linear, no external service, no network — exactly the "runtime-neutral
escalation sink (write to run_store + notify the founder)" the architecture calls for.

Like the rest of the engine this module calls NO LLM and spawns NOTHING. It only computes and
writes disk state. It does not even require an engine ``RunState`` to exist — an escalation is a
lower-level fact than the pipeline state machine, so it scopes itself to a ``run_id`` directory
and works whether or not ``state.json`` is present.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from hyperwilfred_core.run_store import RunEventStatus, RunStore, atomic_write_text
from hyperwilfred_core.schemas import Escalation, EscalationKind

# The founder-facing notification artifact: one cross-run markdown view a human reads. It is
# REBUILT deterministically from the on-disk escalation ledgers on every escalate(), so it always
# reflects the full picture and never drifts from the ledgers it summarises.
OBSERVABILITY_DIR = "observability"
NOTIFICATIONS_FILE = "notifications.md"
ESCALATIONS_LEDGER = "escalations.jsonl"


def _hw_dir(repo_root: Path) -> Path:
    return repo_root / ".hw"


def _run_dir(repo_root: Path, run_id: str) -> Path:
    return _hw_dir(repo_root) / run_id


def ledger_path(repo_root: Path, run_id: str) -> Path:
    """The per-run escalations ledger (append-only)."""
    return _run_dir(repo_root, run_id) / ESCALATIONS_LEDGER


def notifications_path(repo_root: Path) -> Path:
    """The cross-run founder-facing notification artifact."""
    return _hw_dir(repo_root) / OBSERVABILITY_DIR / NOTIFICATIONS_FILE


# --------------------------------------------------------------------------- escalate


def escalate(
    *,
    repo_root: Path,
    run_id: str,
    kind: EscalationKind,
    severity,  # noqa: ANN001 — AnomalySeverity (avoids a redundant import at the call boundary)
    reason: str,
    source: str = "",
    evidence_refs: list[str] | None = None,
    metadata: dict[str, str] | None = None,
) -> Escalation:
    """Raise one escalation: record it on the run timeline + ledger + the founder notification.

    Three durable writes, all on disk:
      1. an ``awaiting_human`` event on the run's ``events.jsonl`` (via the run store);
      2. an append to the run's ``escalations.jsonl`` ledger;
      3. a rebuild of the cross-run founder notification markdown.

    Returns the :class:`Escalation`. Never raises for a missing run (the sink is decoupled from
    the engine state machine) and never reaches the network.
    """
    escalation = Escalation(
        escalation_id=uuid4().hex[:12],
        run_id=run_id,
        kind=kind,
        severity=severity,
        reason=reason,
        source=source,
        evidence_refs=list(evidence_refs or []),
        metadata=dict(metadata or {}),
    )

    # 1. Run-store timeline event (the run dir is created if absent — no RunState required).
    store = RunStore(_run_dir(repo_root, run_id))
    store.init()
    store.append_event(
        run_id=run_id,
        stage=source or "escalation",
        status=RunEventStatus.awaiting_human,
        message=f"ESCALATION [{escalation.severity.value}] {kind.value}: {reason}",
        artifacts=escalation.evidence_refs,
        metadata={"escalation_id": escalation.escalation_id, "kind": kind.value,
                  "severity": escalation.severity.value},
    )

    # 2. Per-run escalations ledger (append-only JSONL).
    led = ledger_path(repo_root, run_id)
    led.parent.mkdir(parents=True, exist_ok=True)
    with led.open("a", encoding="utf-8") as handle:
        handle.write(escalation.model_dump_json() + "\n")

    # 3. Rebuild the founder notification artifact from every ledger on disk.
    _rebuild_notifications(repo_root)
    return escalation


# --------------------------------------------------------------------------- read helpers


def _read_ledger(path: Path) -> list[Escalation]:
    if not path.exists():
        return []
    out: list[Escalation] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(Escalation.model_validate_json(line))
        except ValueError:
            continue  # truncated mid-write line — ignore
    return out


def list_escalations(*, repo_root: Path, run_id: str | None = None) -> list[Escalation]:
    """Return escalations for one run, or every run's escalations (newest first)."""
    hw = _hw_dir(repo_root)
    if run_id is not None:
        items = _read_ledger(ledger_path(repo_root, run_id))
    else:
        items = []
        if hw.exists():
            for led in sorted(hw.glob(f"*/{ESCALATIONS_LEDGER}")):
                items.extend(_read_ledger(led))
    return sorted(items, key=lambda e: e.created_at, reverse=True)


# --------------------------------------------------------------------------- notification artifact


def _rebuild_notifications(repo_root: Path) -> Path:
    """Render the cross-run founder notification markdown from every ledger (deterministic)."""
    items = list_escalations(repo_root=repo_root)
    lines = [
        "# HyperWilfred — Founder Notifications",
        "",
        "> Escalations the factory raised for your attention. Newest first.",
        "> The factory NEVER acts on these itself — it surfaces them and waits for a human.",
        "",
        f"Open escalations: {len(items)}",
        "",
    ]
    if not items:
        lines.append("_No escalations._")
    for e in items:
        lines.extend(
            [
                f"## [{e.severity.value.upper()}] {e.kind.value} — run `{e.run_id}`",
                "",
                f"- When: {e.created_at.isoformat()}",
                f"- Reason: {e.reason}",
                f"- Source: {e.source or 'n/a'}",
            ]
        )
        if e.evidence_refs:
            lines.append("- Evidence: " + ", ".join(f"`{r}`" for r in e.evidence_refs))
        lines.append("")
    path = notifications_path(repo_root)
    atomic_write_text(path, "\n".join(lines) + "\n")
    return path


__all__ = [
    "ESCALATIONS_LEDGER",
    "NOTIFICATIONS_FILE",
    "OBSERVABILITY_DIR",
    "escalate",
    "ledger_path",
    "list_escalations",
    "notifications_path",
]
