"""Phase 5 — the runtime-neutral escalation sink (replaces the Linear-coupled handler)."""

from __future__ import annotations

import ast
from pathlib import Path

from hyperwilfred_core import escalation
from hyperwilfred_core.run_store import RunStore
from hyperwilfred_core.schemas import AnomalySeverity, EscalationKind

SRC = Path(__file__).resolve().parent.parent / "src" / "hyperwilfred_core"


def _escalate(repo: Path, run_id: str, **kw) -> object:
    return escalation.escalate(
        repo_root=repo,
        run_id=run_id,
        kind=kw.get("kind", EscalationKind.ANOMALY),
        severity=kw.get("severity", AnomalySeverity.HIGH),
        reason=kw.get("reason", "something needs a human"),
        source=kw.get("source", "test"),
        evidence_refs=kw.get("evidence_refs"),
        metadata=kw.get("metadata"),
    )


def test_escalate_writes_event_ledger_and_notification(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    esc = _escalate(repo, "run1", reason="patch loop maxed", evidence_refs=[".hw/run1/"])

    # 1. run-store timeline event
    events = RunStore(repo / ".hw" / "run1").read_events()
    assert any("ESCALATION" in e.message for e in events)
    assert any(e.metadata.get("escalation_id") == esc.escalation_id for e in events)

    # 2. per-run escalations ledger
    ledger = escalation.ledger_path(repo, "run1")
    assert ledger.exists()
    items = escalation.list_escalations(repo_root=repo, run_id="run1")
    assert len(items) == 1 and items[0].reason == "patch loop maxed"

    # 3. founder-facing notification artifact
    notif = escalation.notifications_path(repo)
    assert notif.exists()
    body = notif.read_text(encoding="utf-8")
    assert "Founder Notifications" in body
    assert "patch loop maxed" in body


def test_list_escalations_across_all_runs_newest_first(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _escalate(repo, "runA", reason="first")
    _escalate(repo, "runB", reason="second")
    _escalate(repo, "runA", reason="third")

    all_items = escalation.list_escalations(repo_root=repo)
    assert len(all_items) == 3
    # newest first
    assert all_items[0].created_at >= all_items[-1].created_at
    # scoped read
    just_a = escalation.list_escalations(repo_root=repo, run_id="runA")
    assert {e.reason for e in just_a} == {"first", "third"}


def test_empty_repo_lists_nothing(tmp_path: Path) -> None:
    assert escalation.list_escalations(repo_root=tmp_path / "nope") == []


def test_no_run_state_required(tmp_path: Path) -> None:
    """The sink is decoupled from the engine state machine — no state.json needed."""
    repo = tmp_path / "repo"
    assert not (repo / ".hw" / "ghost" / "state.json").exists()
    esc = _escalate(repo, "ghost", reason="anomaly on a run with no state")
    assert esc.run_id == "ghost"
    assert len(escalation.list_escalations(repo_root=repo, run_id="ghost")) == 1


def test_notification_rebuilt_deterministically(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _escalate(repo, "r", reason="one")
    _escalate(repo, "r", reason="two")
    body = escalation.notifications_path(repo).read_text(encoding="utf-8")
    assert body.count("## [") == 2  # both escalations rendered


def _imported_modules(src: str) -> set[str]:
    """Every module name imported by a source file (AST — ignores docstrings/comments)."""
    mods: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


# The runtime-neutral / engine-purity floor: an engine module may not import an LLM SDK, a
# network client, a subprocess/spawn facility, Linear, or the dropped common_py spine.
_FORBIDDEN_PREFIXES = (
    "anthropic", "openai", "subprocess", "urllib", "http", "requests", "httpx",
    "common_py", "linear", "smtplib", "socket",
)


def test_escalation_module_is_runtime_neutral_no_linear_no_network() -> None:
    """The replacement for escalation_handler.py: no Linear, no network, no spawn (AST-verified)."""
    mods = _imported_modules((SRC / "escalation.py").read_text(encoding="utf-8"))
    offenders = [m for m in mods if m.startswith(_FORBIDDEN_PREFIXES)]
    assert offenders == [], f"escalation sink imports forbidden modules: {offenders}"
