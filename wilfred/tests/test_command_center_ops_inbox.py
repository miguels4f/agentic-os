"""Command center: inbox items from ``<workspace>/ops`` (proposals, overdue and blocked tasks).

Every case builds its snapshot in memory, except the last one, which goes through
``build_snapshot`` with a stand-in ``ops`` program. ``now`` and the time zone are fixed.
"""

from __future__ import annotations

import typing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from ops_fakes import export_report, fake_ops

from hyperwilfred_core.command_center import text_es
from hyperwilfred_core.command_center.attention import derive_attention
from hyperwilfred_core.command_center.model import (
    AttentionItem,
    AttentionSubject,
    Generator,
    Ops,
    OpsProposal,
    OpsTask,
    PendingGate,
    Run,
    Snapshot,
    Source,
)
from hyperwilfred_core.command_center.snapshot import build_snapshot

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
BLOB = "https://github.com/example/ops/blob/main/"


def _task(task_id: str = "T-0001", status: str = "In Progress", **fields) -> OpsTask:
    base = {"id": task_id, "path": f"tasks/{task_id}-x.md", "title": f"Tarea {task_id}",
            "status": status, "projects": ["project-a"]}
    base.update(fields)
    return OpsTask(**base)


def _proposal(proposal_id: str = "P-0003", **fields) -> OpsProposal:
    base = {"id": proposal_id, "path": f"proposals/{proposal_id}.json", "kind": "set",
            "target": "sales/acme.md", "fields": ["status"], "author": "pilot-agent",
            "created_at": "2026-10-13T09:00:00Z"}
    base.update(fields)
    return OpsProposal(**base)


def _ops(**fields) -> Ops:
    base = {"root": "/ws/ops", "bin": "/bin/ops", "github_blob": BLOB, "open_tasks": [],
            "proposals": [], "unapproved_edits": []}
    base.update(fields)
    return Ops(**base)


def _snapshot(**overrides) -> Snapshot:
    data = {"generated_at": "2026-10-14T12:00:00Z", "workspace": "/ws",
            "generator": Generator(wilfred_version="0.1.0")}
    data.update(overrides)
    return Snapshot(**data)


def _items(ops: Ops | None, **overrides) -> list[AttentionItem]:
    return derive_attention(_snapshot(ops=ops, **overrides), NOW, tz=UTC)


def test_a_pending_proposal_is_high_risk_and_links_the_file_and_github() -> None:
    [item] = _items(_ops(proposals=[_proposal()]))
    assert (item.kind, item.risk, item.deadline) == ("ops_pending_proposal", "alto", None)
    assert "P-0003" in item.title_es and "acme" in item.title_es
    assert "estado del contacto" in item.title_es, "field names are translated"
    assert "«pilot-agent»" in item.why_es and "2026-10-13 09:00" in item.why_es
    assert "ops approve P-0003" in item.default_es and "ops reject P-0003" in item.default_es
    assert [(s.kind, s.path) for s in item.sources] == [
        ("file", "/ws/ops/proposals/P-0003.json"),
        ("url", BLOB + "proposals/P-0003.json"),
    ]
    assert item.subject == AttentionSubject(type="ops", id="proposals/P-0003.json")


def test_an_unapproved_sales_edit_is_a_proposal_that_links_only_the_local_file() -> None:
    [item] = _items(_ops(unapproved_edits=["sales/volotea.md"]))
    assert (item.kind, item.risk) == ("ops_pending_proposal", "alto")
    assert "volotea" in item.title_es
    assert "ops approve --file sales/volotea.md" in item.default_es
    assert [s.kind for s in item.sources] == ["file"]


def test_a_hand_typed_authorization_is_an_alto_item() -> None:
    path = "tasks/T-0007-y.md"
    [item] = _items(_ops(unapproved_edits=[path]))
    assert (item.kind, item.risk, item.deadline) == ("ops_pending_proposal", "alto", None)
    assert (
        f"autorización escrita a mano en {path}: revísala antes de aprobarla "
        f"con ops approve --file {path}"
        in item.title_es
    )
    assert f"ops approve --file {path}" in item.default_es
    assert item.subject == AttentionSubject(type="ops", id=path)
    assert [(s.kind, s.path) for s in item.sources] == [("file", "/ws/ops/" + path)]
    # A different decision from the task's own state: a blocked task keeps its own item.
    both = _items(_ops(unapproved_edits=[path], open_tasks=[_task("T-0007", "Blocked", path=path)]))
    assert [i.kind for i in both] == ["ops_pending_proposal", "ops_blocked_task"]


def test_an_overdue_task_is_medium_and_says_since_when() -> None:
    [item] = _items(_ops(open_tasks=[_task(due="2026-10-10")]))
    assert (item.kind, item.risk, item.deadline) == ("ops_overdue_task", "medio", None)
    assert item.title_es.startswith("project-a: la tarea T-0001")
    assert "venció hace 4 días" in item.title_es
    assert "2026-10-10" in item.why_es and "en curso" in item.why_es
    assert item.sources[1].path == BLOB + "tasks/T-0001-x.md"


def test_due_today_later_or_closed_is_not_overdue() -> None:
    for task in (
        _task(due="2026-10-14"),
        _task(due="2026-11-01"),
        _task("T-0002", "Complete", due="2026-01-01"),
        _task("T-0003", "Deferred", due="2026-01-01"),
    ):
        assert _items(_ops(open_tasks=[task])) == [], task


def test_a_date_range_is_overdue_only_after_its_end() -> None:
    assert _items(_ops(open_tasks=[_task(due="2026-10-01", due_end="2026-10-31")])) == []
    [item] = _items(_ops(open_tasks=[_task(due="2026-09-01", due_end="2026-10-13")]))
    assert "venció hace 1 día." in item.title_es


def test_today_is_the_calendar_day_of_the_given_time_zone() -> None:
    late_evening = datetime(2026, 10, 14, 23, 30, tzinfo=UTC)  # already the 15th at UTC+2
    snapshot = _snapshot(ops=_ops(open_tasks=[_task(due="2026-10-14")]))
    assert derive_attention(snapshot, late_evening, tz=UTC) == []
    [item] = derive_attention(snapshot, late_evening, tz=timezone(timedelta(hours=2)))
    assert item.kind == "ops_overdue_task"


def test_a_blocked_task_is_low_risk_and_names_its_blocker() -> None:
    [item] = _items(_ops(open_tasks=[_task(status="Blocked", blocked_by="Falta el contrato")]))
    assert (item.kind, item.risk) == ("ops_blocked_task", "bajo")
    assert "Falta el contrato" in item.why_es
    [silent] = _items(_ops(open_tasks=[_task(status="Blocked")]))
    assert "no dice por qué" in silent.why_es


def test_a_blocked_overdue_task_gives_one_overdue_item_that_mentions_the_block() -> None:
    task = _task(status="Blocked", due="2026-10-01", blocked_by="proveedor")
    [item] = _items(_ops(open_tasks=[task]))
    assert item.kind == "ops_overdue_task"
    assert "bloqueada" in item.why_es and "proveedor" in item.why_es


def test_missing_or_unknown_ops_data_gives_no_ops_item() -> None:
    assert _items(None) == []
    assert _items(_ops(open_tasks=None, proposals=None, unapproved_edits=None)) == []


def test_without_a_github_address_items_link_to_the_local_file_only() -> None:
    [item] = _items(_ops(github_blob=None, open_tasks=[_task(status="Blocked")]))
    assert [s.kind for s in item.sources] == ["file"]


def test_ops_items_have_labels_sources_and_plain_spanish() -> None:
    kinds = typing.get_args(AttentionItem.model_fields["kind"].annotation)
    assert set(kinds) <= set(text_es.ATT_KIND_LABELS), "a kind without a Spanish label"
    proposals = [
        _proposal(),
        _proposal("P-0004", kind="schema-option", target="schema/sales.json"),
        _proposal("P-0005", kind="append", fields=[]),
        _proposal("P-0006", kind="create", target="sales/new-co.md", fields=[]),
    ]
    tasks = [_task("T-0001", "Not Started", due="2026-10-01"), _task("T-0002", "Blocked")]
    edits = ["sales/volotea.md", "schema/sales.json", "tasks/T-0007-y.md"]
    items = _items(_ops(proposals=proposals, unapproved_edits=edits, open_tasks=tasks))
    assert sorted({i.kind for i in items}) == [
        "ops_blocked_task", "ops_overdue_task", "ops_pending_proposal"
    ]
    assert len({i.id for i in items}) == len(items) == 9
    for item in items:
        assert item.sources, item.id
        for text in (item.title_es, item.why_es, item.default_es, item.if_ignored_es):
            assert text.strip(), item.id
        shown = item.title_es + item.why_es + item.default_es
        for word in ("Not Started", "In Progress", "Blocked", "Complete", "Deferred",
                     "schema-option", "authorized_by", "authorized_at"):
            assert word not in shown, (item.id, word)


def _gated_run() -> Run:
    expires = (NOW + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Run(
        run_id="r1", store_path="/ws/repo/.hw", project_id="repo", status="awaiting_human",
        pending_gates=[PendingGate(gate_id="ship", action="merge", challenge_expires_at=expires)],
        sources=[Source(kind="file", path="/ws/repo/.hw/r1/gates/ship.pending",
                        field="pending_gates")],
    )


def test_a_flood_of_tasks_keeps_gates_and_proposals_on_top() -> None:
    blocked = [_task(f"T-{n:04d}", "Blocked") for n in range(30)]
    late = [_task(f"T-{n:04d}", "Not Started", due="2026-09-01") for n in range(30, 60)]
    snapshot = _snapshot(runs=[_gated_run()],
                         ops=_ops(proposals=[_proposal()], open_tasks=blocked + late))
    items = derive_attention(snapshot, NOW, tz=UTC)
    assert len(items) == 62
    assert [i.kind for i in items[:2]] == ["gate", "ops_pending_proposal"]
    assert items[2].kind == "ops_overdue_task" and items[-1].kind == "ops_blocked_task"


def test_build_snapshot_puts_ops_items_in_the_inbox(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "MEMORY.md").write_text("", encoding="utf-8")
    workspace = tmp_path / "ws"
    (workspace / "ops").mkdir(parents=True)
    program = fake_ops(tmp_path / "bin", export=export_report(root=str(workspace / "ops")))
    built = build_snapshot(workspace, memory_dir=memory, now=NOW, ops_bin=program)
    assert sorted({item.kind for item in built.attention}) == [
        "ops_blocked_task", "ops_overdue_task", "ops_pending_proposal"
    ]
