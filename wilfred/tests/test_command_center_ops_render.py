"""Command center: how inbox items from ``<workspace>/ops`` look on the page."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime

from hyperwilfred_core.command_center import render, text_es
from hyperwilfred_core.command_center.attention import derive_attention
from hyperwilfred_core.command_center.model import (
    Generator,
    Ops,
    OpsProposal,
    OpsTask,
    Snapshot,
    Source,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
BLOB = "https://github.com/example/ops/blob/main/"


def _ops(**fields) -> Ops:
    base = {"root": "/ws/ops", "bin": "/bin/ops", "github_blob": BLOB, "open_tasks": [],
            "proposals": [], "unapproved_edits": []}
    base.update(fields)
    return Ops(**base)


def _proposal(**fields) -> OpsProposal:
    base = {"id": "P-0003", "path": "proposals/P-0003.json", "kind": "set",
            "target": "sales/acme.md", "fields": ["status"], "author": "pilot-agent",
            "created_at": "2026-10-13T09:00:00Z"}
    base.update(fields)
    return OpsProposal(**base)


def _task(task_id: str = "T-0001", status: str = "In Progress", **fields) -> OpsTask:
    base = {"id": task_id, "path": f"tasks/{task_id}-x.md", "title": f"Tarea {task_id}",
            "status": status, "projects": ["project-a"]}
    base.update(fields)
    return OpsTask(**base)


def _page(ops: Ops) -> str:
    snapshot = Snapshot(generated_at="2026-10-14T11:00:00Z", workspace="/ws",
                        generator=Generator(wilfred_version="0.1.0"), ops=ops)
    snapshot.attention = derive_attention(snapshot, NOW, tz=UTC)
    return render.render_page(snapshot, now=NOW, snapshot_path="/out/snapshot.json")


def _attention_section(page: str) -> str:
    match = re.search(r'<section id="atencion">.*?</section>', page, flags=re.S)
    assert match is not None
    return match.group(0)


def test_the_page_lists_ops_items_with_file_and_github_links() -> None:
    ops = _ops(proposals=[_proposal()], open_tasks=[_task(due="2026-10-01"),
                                                    _task("T-0002", "Blocked")])
    section = _attention_section(_page(ops))
    assert section.count("<article") == 3
    assert f'href="{BLOB}proposals/P-0003.json" rel="noreferrer"' in section
    assert f'href="{render.file_href("/ws/ops/tasks/T-0001-x.md")}"' in section
    assert f">{html.escape(text_es.SOURCE_WEB_LINK)}</a>" in section
    assert html.escape(text_es.ATT_KIND_LABELS["ops_overdue_task"]) in section
    assert html.escape(text_es.RISK_MEANING["alto"]) in section
    assert "<button" not in section and "<form" not in section


def test_an_unapproved_edit_has_no_github_link() -> None:
    edits = ["sales/volotea.md", "tasks/T-0007-y.md"]
    section = _attention_section(_page(_ops(unapproved_edits=edits)))
    assert section.count("<article") == 2
    assert "https://" not in section
    for path in edits:
        assert f'href="{render.file_href("/ws/ops/" + path)}"' in section


def test_hostile_ops_text_is_escaped_and_only_safe_links_are_emitted() -> None:
    hostile = '<script>alert(1)</script>" onmouseover="x'
    ops = _ops(open_tasks=[_task(status="Blocked", title=hostile, blocked_by="<img src=x>")],
               proposals=[_proposal(author="<b>bot</b>")])
    page = _page(ops)
    section = _attention_section(page)
    assert "<script>alert" not in section and "<img" not in section and "<b>bot" not in section
    assert html.escape("<img src=x>") in section
    assert page.count("<script>") == 1, "only the page's own filter script"
    for href in re.findall(r'href="([^"]*)"', page):
        assert href.startswith(("file:///", "#", "https://github.com/")), href


def test_a_non_https_url_source_is_never_a_link() -> None:
    sneaky = Source.model_construct(kind="url", path="javascript:alert(1)")
    assert render.source_link(sneaky) == ""


def test_a_url_source_on_another_host_is_never_a_link() -> None:
    other = Source.model_construct(kind="url", path="https://evil.example/blob/main/x.md")
    assert render.source_link(other) == ""
