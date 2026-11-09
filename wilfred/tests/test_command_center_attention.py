"""Command center: the attention inbox (``attention.py``) and how the page shows it.

Every case builds its own snapshot in memory; nothing reads the real workspace. ``now`` and the
time zone are fixed so the results do not depend on the clock or on the Mac running the tests.
"""

from __future__ import annotations

import ast
import html
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hyperwilfred_core.command_center import attention, render, text_es
from hyperwilfred_core.command_center.attention import (
    RECENT_DAYS,
    UNCOMMITTED_DAYS,
    derive_attention,
)
from hyperwilfred_core.command_center.model import (
    AttentionItem,
    AttentionSubject,
    Generator,
    LastEvent,
    MalformedNote,
    Memory,
    PendingGate,
    Problem,
    Project,
    Run,
    Snapshot,
    Source,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(days: float) -> str:
    return _iso(NOW - timedelta(days=days))


def _snapshot(**overrides) -> Snapshot:
    data = {
        "generated_at": _iso(NOW),
        "workspace": "/ws",
        "generator": Generator(wilfred_version="0.1.0"),
    }
    data.update(overrides)
    return Snapshot(**data)


def _run(run_id: str = "r1", status: str = "in_progress", store: str = "/ws/repo/.hw",
         **fields) -> Run:
    state = f"{store}/{run_id}/state.json"
    sources = [Source(kind="file", path=state, field=f)
               for f in ("status", "updated_at", "quality")]
    sources.append(Source(kind="file", path=f"{store}/{run_id}/events.jsonl", field="last_event"))
    base = {
        "run_id": run_id, "store_path": store, "project_id": "repo", "status": status,
        "current_step": "implement", "updated_at": _ago(1), "sources": sources,
        "last_event": LastEvent(timestamp=_ago(1), message="working"),
    }
    base.update(fields)
    return Run(**base)


def _gate(gate_id: str = "ship", *, expires_in_hours: float | None = 5, store: str = "/ws/repo/.hw",
          run_id: str = "r1") -> tuple[PendingGate, Source]:
    expires = NOW + timedelta(hours=expires_in_hours) if expires_in_hours is not None else None
    gate = PendingGate(
        gate_id=gate_id, kind="ship", action="merge the change",
        challenge_expires_at=_iso(expires) if expires else None,
        expired=(expires < NOW) if expires else None,
    )
    source = Source(kind="file", path=f"{store}/{run_id}/gates/{gate_id}.pending",
                    field="pending_gates")
    return gate, source


def _gated_run(status: str = "awaiting_human", **gate_args) -> Run:
    gate, source = _gate(**gate_args)
    run = _run(status=status, pending_gates=[gate])
    run.sources.append(source)
    return run


def _repo(**fields) -> Project:
    path = fields.get("path", "/ws/repo")
    base = {
        "id": "repo", "name": "repo", "path": path, "kind": "repository", "branch": "main",
        "head": "b" * 40, "has_commits": True, "uncommitted_paths": 0, "ahead": 0, "behind": 0,
        "branches_without_upstream": [], "local_branches": 1, "last_commit_at": _ago(1),
        "sources": [Source(kind="git", path=path, field=f, command=f"git -C {path} x")
                    for f in ("ahead", "branches_without_upstream", "uncommitted_paths",
                              "last_commit_at")],
    }
    base.update(fields)
    return Project(**base)


def _items(**overrides) -> list[AttentionItem]:
    return derive_attention(_snapshot(**overrides), NOW, tz=UTC)


def _kinds(items: list[AttentionItem]) -> list[str]:
    return [item.kind for item in items]


# ------------------------------------------------------------------ the rules of section 7


def test_pending_gate_unexpired_is_high_risk_with_its_deadline() -> None:
    [item] = _items(runs=[_gated_run(expires_in_hours=5)])
    assert item.kind == "gate" and item.risk == "alto"
    assert item.deadline == _iso(NOW + timedelta(hours=5))
    assert "2026-10-09 17:00" in item.default_es and "2026-10-09 17:00" in item.if_ignored_es
    assert any(s.path.endswith("/gates/ship.pending") for s in item.sources)


def test_pending_gate_expired_is_medium_and_says_it_must_be_reissued() -> None:
    [item] = _items(runs=[_gated_run(expires_in_hours=-3)])
    assert item.kind == "gate" and item.risk == "medio" and item.deadline is None
    assert "caducó" in item.title_es
    assert "volver a pedirla" in item.why_es
    assert "vuelva a abrir la petición" in item.default_es


def test_gate_expiry_is_judged_against_now_not_the_stored_answer() -> None:
    gate, source = _gate(expires_in_hours=-1)
    gate.expired = False  # stored when the snapshot was taken, before the deadline passed
    run = _run(status="awaiting_human", pending_gates=[gate])
    [item] = _items(runs=[run])
    assert item.risk == "medio"


def test_awaiting_human_without_gate_is_medium() -> None:
    [item] = _items(runs=[_run(status="awaiting_human", updated_at=_ago(3),
                               last_event=LastEvent(timestamp=_ago(3)))])
    assert item.kind == "awaiting_human" and item.risk == "medio"
    assert "hace 3 días" in item.if_ignored_es


def test_run_with_a_gate_gives_exactly_one_item() -> None:
    for status in ("awaiting_human", "gate_failed", "in_progress"):
        items = _items(runs=[_gated_run(status=status)])
        assert _kinds(items) == ["gate"], status


def test_gate_failed_three_days_ago_is_an_item() -> None:
    [item] = _items(runs=[_run(status="gate_failed", updated_at=_ago(3))])
    assert item.kind == "gate_failed" and item.risk == "medio"
    leaves = (NOW - timedelta(days=3) + timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d %H:%M")
    assert leaves in item.if_ignored_es
    assert "programar el cambio" in item.why_es  # the step, in Spanish


def test_gate_failed_thirty_days_ago_is_not_an_item() -> None:
    assert _items(runs=[_run(status="gate_failed", updated_at=_ago(30))]) == []


def test_in_progress_silent_for_twenty_days_is_a_stale_run() -> None:
    run = _run(updated_at=_ago(20), last_event=LastEvent(timestamp=_ago(20)))
    [item] = _items(runs=[run])
    assert item.kind == "stale_run" and item.risk == "bajo"
    assert "20 días" in item.title_es


def test_in_progress_with_an_event_yesterday_is_not_an_item() -> None:
    run = _run(updated_at=_ago(40), last_event=LastEvent(timestamp=_ago(1)))
    assert _items(runs=[run]) == []


def test_repository_ahead_of_upstream_is_unpushed_work() -> None:
    [item] = _items(projects=[_repo(ahead=2)])
    assert item.kind == "unpushed_work" and item.risk == "medio"
    assert text_es.att_ahead(2) in item.why_es
    assert [s.field for s in item.sources] == ["ahead"]


def test_branch_without_upstream_is_unpushed_work() -> None:
    [item] = _items(projects=[_repo(branches_without_upstream=["idea"])])
    assert item.kind == "unpushed_work" and "idea" in item.why_es


def test_uncommitted_paths_with_an_old_last_commit_are_unpushed_work() -> None:
    [item] = _items(projects=[_repo(uncommitted_paths=4, last_commit_at=_ago(10))])
    assert item.kind == "unpushed_work"
    assert "4 archivos cambiados" in item.why_es and "hace 10 días" in item.why_es


def test_uncommitted_paths_with_a_commit_yesterday_are_not_an_item() -> None:
    assert _items(projects=[_repo(uncommitted_paths=4, last_commit_at=_ago(1))]) == []
    assert UNCOMMITTED_DAYS == 7 and RECENT_DAYS == 14


def test_worktrees_and_folders_are_not_unpushed_work_items() -> None:
    worktree = _repo(id="wt", name="wt", path="/ws/wt", kind="worktree", ahead=3,
                     parent_repository="repo")
    folder = Project(id="f", name="f", path="/ws/f", kind="folder")
    assert _items(projects=[worktree, folder]) == []


def test_duplicate_run_gives_one_item() -> None:
    first = _run(status="awaiting_human", store="/ws/a/.hw")
    second = _run(status="awaiting_human", store="/ws/b/.hw", duplicate_of=["/ws/a/.hw"])
    items = _items(runs=[first, second])
    assert _kinds(items) == ["awaiting_human"]


def test_problems_and_malformed_notes_are_low_risk_items() -> None:
    memory = Memory(root="/mem", malformed=[
        MalformedNote(path="/mem/a.md", problem="missing name, type"),
        MalformedNote(path="/mem/b.md", problem="no frontmatter block"),
    ])
    problems = [Problem(path="/ws/repo/.hw/r9/state.json", what="run state", reason="bad JSON")]
    items = _items(memory=memory, problems=problems)
    assert sorted(_kinds(items)) == ["memory_malformed", "memory_malformed", "unreadable"]
    assert {item.risk for item in items} == {"bajo"}
    unreadable = next(i for i in items if i.kind == "unreadable")
    assert unreadable.subject.type == "run" and "bad JSON" in unreadable.why_es
    note = next(i for i in items if i.subject.id == "/mem/a.md")
    assert "le falta: nombre, tipo" in note.why_es


# ------------------------------------------------------------------ order and completeness


def _one_of_each() -> Snapshot:
    runs = [
        _gated_run(expires_in_hours=5),
        _run("r2", "awaiting_human"),
        _run("r3", "gate_failed", updated_at=_ago(2)),
        _run("r4", updated_at=_ago(30), last_event=LastEvent(timestamp=_ago(30))),
    ]
    run_expired = _run("r5", "awaiting_human", pending_gates=[_gate(expires_in_hours=-2)[0]])
    return _snapshot(
        runs=[*runs, run_expired],
        projects=[_repo(ahead=1)],
        problems=[Problem(path="/ws/x.json", what="file", reason="broken")],
        memory=Memory(root="/mem", malformed=[MalformedNote(path="/mem/n.md",
                                                            problem="missing type")]),
    )


def test_order_is_risk_then_deadline_then_age() -> None:
    items = derive_attention(_one_of_each(), NOW, tz=UTC)
    risks = [item.risk for item in items]
    assert risks == sorted(risks, key=["alto", "medio", "bajo"].index)
    assert items[0].kind == "gate" and items[0].risk == "alto"
    early = _gated_run(expires_in_hours=1)
    early.run_id = "early"
    late = _gated_run(expires_in_hours=9)
    late.run_id = "late"
    ordered = _items(runs=[late, early])
    assert [item.subject.id for item in ordered] == ["early", "late"]
    old = _run("old", "awaiting_human", updated_at=_ago(9), last_event=LastEvent(timestamp=_ago(9)))
    new = _run("new", "awaiting_human", updated_at=_ago(2), last_event=LastEvent(timestamp=_ago(2)))
    assert [i.subject.id for i in _items(runs=[new, old])] == ["old", "new"]


def test_every_item_has_a_source_and_every_spanish_field_is_filled() -> None:
    items = derive_attention(_one_of_each(), NOW, tz=UTC)
    assert sorted(set(_kinds(items))) == sorted(
        ["gate", "awaiting_human", "gate_failed", "stale_run", "unpushed_work", "unreadable",
         "memory_malformed"]
    )
    for item in items:
        assert item.sources, item.id
        for field in (item.title_es, item.why_es, item.default_es, item.if_ignored_es):
            assert field.strip(), item.id
        for word in ("awaiting_human", "gate_failed", "in_progress", "stale_run"):
            assert word not in item.title_es + item.why_es + item.default_es, item.id
    assert len({item.id for item in items}) == len(items)


def test_attention_module_reads_nothing_and_never_asks_the_clock() -> None:
    tree = ast.parse(Path(attention.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]]
        else:
            names = []
        for name in names:
            assert name not in ("os", "pathlib", "subprocess", "shutil", "io"), name
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("now", "today", "utcnow", "time"), node.attr
        if isinstance(node, ast.Name):
            assert node.id != "open", "attention.py must not open files"


def test_build_snapshot_carries_the_attention_list(tmp_path: Path) -> None:
    from hyperwilfred_core.command_center.snapshot import build_snapshot

    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "MEMORY.md").write_text("", encoding="utf-8")
    (memory / "bad.md").write_text("no header\n", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    snapshot = build_snapshot(workspace, memory_dir=memory, now=NOW)
    assert [item.kind for item in snapshot.attention] == ["memory_malformed"]


# ------------------------------------------------------------------ the page


def _page(snapshot: Snapshot) -> str:
    snapshot.attention = derive_attention(snapshot, NOW, tz=UTC)
    return render.render_page(snapshot, now=NOW, snapshot_path="/out/snapshot.json")


def _attention_section(page: str) -> str:
    match = re.search(r'<section id="atencion">.*?</section>', page, flags=re.S)
    assert match is not None
    return match.group(0)


def test_empty_inbox_says_nothing_needs_attention_with_the_time() -> None:
    section = _attention_section(_page(_snapshot()))
    assert html.escape(text_es.attention_empty(render._local(_iso(NOW)))) in section
    assert "Nada necesita tu atención ahora mismo" in html.unescape(section)
    assert "<article" not in section


def test_fifteen_items_show_twelve_and_a_more_line() -> None:
    runs = [_run(f"r{n:02d}", "awaiting_human") for n in range(15)]
    section = _attention_section(_page(_snapshot(runs=runs)))
    head, _, rest = section.partition("<details>")
    assert head.count("<article") == 12
    assert f"<summary>{html.escape(text_es.attention_more(3))}</summary>" in section
    assert rest.count("<article") == 3


def test_page_item_shows_why_deadline_default_consequence_and_source() -> None:
    section = _attention_section(_page(_snapshot(runs=[_gated_run(expires_in_hours=5)])))
    for label in (text_es.ATT_WHY, text_es.ATT_DEADLINE, text_es.ATT_DEFAULT,
                  text_es.ATT_IF_IGNORED):
        assert f'<span class="k">{html.escape(label)}</span>' in section
    assert html.escape(text_es.RISK_MEANING["alto"]) in section
    assert html.escape(text_es.until(0, 5)) in section, "a future deadline says what is left"
    late = _gated_run(expires_in_hours=5)
    late.pending_gates[0].challenge_expires_at = _iso(NOW + timedelta(hours=5))
    shown = _snapshot(runs=[late])
    shown.attention = derive_attention(shown, NOW, tz=UTC)
    after = render.render_page(shown, now=NOW + timedelta(hours=6))
    assert html.escape(text_es.DEADLINE_PASSED) in _attention_section(after)
    assert "gates/ship.pending" in section and 'class="src"' in section
    assert "<button" not in section and "<form" not in section


def test_hostile_text_in_an_item_is_escaped() -> None:
    problems = [Problem(path="/ws/<script>x.json", what="file", reason="<img src=x>")]
    section = _attention_section(_page(_snapshot(problems=problems)))
    assert "<script>" not in section and "<img" not in section
    assert "&lt;img src=x&gt;" in section


def test_stored_items_are_rendered_as_they_are() -> None:
    item = AttentionItem(
        id="x", kind="unreadable", title_es="T", why_es="W", risk="bajo", default_es="D",
        if_ignored_es="I", subject=AttentionSubject(type="project", id="p"),
        sources=[Source(kind="file", path="/ws/p")],
    )
    page = render.render_page(_snapshot(attention=[item]), now=NOW)
    assert "<h4>T</h4>" in _attention_section(page)


# ------------------------------------------------------------------ review findings (S2.4)


def test_gate_next_to_a_corrupt_state_still_gives_a_gate_item() -> None:
    gate, source = _gate()
    run = Run(run_id="r1", store_path="/ws/repo/.hw", readable=False, pending_gates=[gate],
              sources=[source])
    problems = [Problem(path="/ws/repo/.hw/r1/state.json", what="run state", reason="bad")]
    items = _items(runs=[run], problems=problems)
    assert sorted(_kinds(items)) == ["gate", "unreadable"]
    gate_item = next(i for i in items if i.kind == "gate")
    assert gate_item.risk == "alto"
    unreadable = next(i for i in items if i.kind == "unreadable")
    assert unreadable.subject == AttentionSubject(type="run", id="r1")


def test_gate_in_an_older_copy_of_a_duplicated_run_is_not_lost() -> None:
    gate, source = _gate(store="/ws/a/.hw")
    older = _run(status="awaiting_human", store="/ws/a/.hw", updated_at=_ago(3),
                 pending_gates=[gate])
    older.sources.append(source)
    newer = _run(status="in_progress", store="/ws/b/.hw", updated_at=_ago(1))
    [item] = _items(runs=[older, newer])
    assert item.kind == "gate" and item.risk == "alto"
    assert any(s.path.startswith("/ws/a/.hw/") for s in item.sources)


def test_mixed_gates_take_the_earliest_open_deadline() -> None:
    expired, _ = _gate("old", expires_in_hours=-4)
    later, _ = _gate("later", expires_in_hours=9)
    sooner, _ = _gate("sooner", expires_in_hours=2)
    [item] = _items(runs=[_run(status="awaiting_human", pending_gates=[expired, later, sooner])])
    assert item.risk == "alto" and item.deadline == _iso(NOW + timedelta(hours=2))


def test_gate_without_an_approval_window_is_medium_not_high() -> None:
    [item] = _items(runs=[_gated_run(expires_in_hours=None)])
    assert item.kind == "gate" and item.risk == "medio" and item.deadline is None
    assert text_es.ATT_GATE_NO_WINDOW in item.why_es


def test_thresholds_are_strictly_more_than_fourteen_and_seven_days() -> None:
    at_limit = _run(status="gate_failed", updated_at=_ago(RECENT_DAYS))
    assert _kinds(_items(runs=[at_limit])) == ["gate_failed"]
    just_over = _run(status="gate_failed", updated_at=_ago(RECENT_DAYS + 0.01))
    assert _items(runs=[just_over]) == []
    quiet = _run(updated_at=_ago(RECENT_DAYS), last_event=LastEvent(timestamp=_ago(RECENT_DAYS)))
    assert _items(runs=[quiet]) == []
    commit_at_limit = _repo(uncommitted_paths=1, last_commit_at=_ago(UNCOMMITTED_DAYS))
    assert _items(projects=[commit_at_limit]) == []


def test_unknown_dates_show_a_failed_gate_but_not_a_stale_run() -> None:
    # Decision: a stopped run whose date is unknown stays visible (recent cannot be ruled
    # out); "stale" is a claim that needs a date, so an undated run is not called stale.
    failed = _run("f", "gate_failed", updated_at=None, last_event=None)
    undated = _run("u", "in_progress", updated_at=None, last_event=None)
    [item] = _items(runs=[failed, undated])
    assert item.kind == "gate_failed" and "No se sabe cuándo" in item.if_ignored_es


def test_unreadable_memory_note_is_listed_once() -> None:
    memory = Memory(root="/mem", malformed=[MalformedNote(path="/mem/a.md", problem="unreadable")])
    problems = [Problem(path="/mem/a.md", what="memory note", reason="unreadable: denied")]
    [item] = _items(memory=memory, problems=problems)
    assert item.kind == "unreadable" and item.subject.type == "memory"


def test_uncommitted_with_commits_but_no_readable_date_does_not_claim_no_commits() -> None:
    [item] = _items(projects=[_repo(uncommitted_paths=2, last_commit_at=None)])
    assert "no se pudo leer la fecha" in item.why_es
    [fresh] = _items(projects=[_repo(uncommitted_paths=2, last_commit_at=None,
                                     has_commits=False, head=None)])
    assert "no tiene ningún commit" in fresh.why_es


def test_naive_now_is_read_as_utc() -> None:
    naive = NOW.replace(tzinfo=None)
    snapshot = _snapshot(runs=[_gated_run(expires_in_hours=5)])
    assert derive_attention(snapshot, naive, tz=UTC) == derive_attention(snapshot, NOW, tz=UTC)


def test_item_anchor_is_a_valid_html_id() -> None:
    problems = [Problem(path="/ws/a b/x.json", what="file", reason="broken")]
    section = _attention_section(_page(_snapshot(problems=problems)))
    anchors = re.findall(r'id="atencion-([^"]*)"', section)
    assert anchors and all(re.fullmatch(r"[0-9a-f]{12}", a) for a in anchors)
