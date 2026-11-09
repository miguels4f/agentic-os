"""Command center: the closed-runs register (``closed-runs.json`` next to the snapshot).

A run listed in the register was closed by the owner (archived elsewhere, or kept in place as
evidence). It stays visible on the page, in a collapsed "closed" group, but it no longer asks
for attention. The register is read only; a damaged register closes nothing and is reported.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hyperwilfred_core.command_center import collect_closed, render, snapshot, text_es
from hyperwilfred_core.command_center.attention import derive_attention
from hyperwilfred_core.command_center.model import (
    ClosedMark,
    Generator,
    LastEvent,
    PendingGate,
    Run,
    Snapshot,
    Source,
)
from hyperwilfred_core.command_center.paths import canonical

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
SCHEMA = collect_closed.REGISTER_SCHEMA


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_register(path: Path, runs: list[dict], schema: str = SCHEMA) -> Path:
    path.write_text(json.dumps({"schema_version": schema, "runs": runs}), encoding="utf-8")
    return path


def _make_run_dir(root: Path, run_id: str, status: str = "awaiting_human") -> Path:
    run_dir = root / ".hw" / run_id
    run_dir.mkdir(parents=True)
    state = {
        "run_id": run_id, "mode": "improve", "tier": "full", "status": status,
        "step_status": {"quality": "running"}, "updated_at": "2026-09-24T14:04:45Z",
    }
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return run_dir


def _memory(tmp_path: Path) -> Path:
    """A small valid memory tree, so the only problems in a snapshot are the register's."""
    root = tmp_path / "memory"
    root.mkdir()
    (root / "MEMORY.md").write_text("- [Note](note.md) — hook\n", encoding="utf-8")
    (root / "note.md").write_text(
        "---\nname: note\ndescription: d\nmetadata:\n  type: project\n---\nbody\n",
        encoding="utf-8",
    )
    return root


# ---------------------------------------------------------------- reading the register


def test_missing_register_closes_nothing_and_is_not_a_problem(tmp_path: Path) -> None:
    marks, problems = collect_closed.read_register(tmp_path / "closed-runs.json")
    assert marks == {}
    assert problems == []


def test_valid_register_marks_each_run_directory(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path / "repo", "old-run")
    register = _write_register(tmp_path / "closed-runs.json", [
        {"path": str(run_dir), "reason": "Evidencia de Gate 0", "closed_on": "2026-10-13"},
    ])
    marks, problems = collect_closed.read_register(register)
    assert problems == []
    mark = marks[str(canonical(run_dir))]
    assert mark.reason == "Evidencia de Gate 0"
    assert mark.closed_on == "2026-10-13"
    assert mark.source == Source(kind="file", path=str(register), field="runs")


def test_register_that_is_not_json_closes_nothing_and_is_reported(tmp_path: Path) -> None:
    register = tmp_path / "closed-runs.json"
    register.write_text("{not json", encoding="utf-8")
    marks, problems = collect_closed.read_register(register)
    assert marks == {}
    assert len(problems) == 1
    assert problems[0].path == str(register)
    assert problems[0].what == "closed-runs register"


def test_register_with_another_schema_closes_nothing(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path / "repo", "old-run")
    register = _write_register(
        tmp_path / "closed-runs.json",
        [{"path": str(run_dir), "reason": "r"}], schema="something-else/v9",
    )
    marks, problems = collect_closed.read_register(register)
    assert marks == {}
    assert [p.what for p in problems] == ["closed-runs register"]


def test_a_bad_entry_is_reported_and_the_good_ones_still_count(tmp_path: Path) -> None:
    good = _make_run_dir(tmp_path / "repo", "good")
    not_a_run = tmp_path / "repo" / "src"
    not_a_run.mkdir()
    register = _write_register(tmp_path / "closed-runs.json", [
        {"path": "relative/.hw/x", "reason": "r"},  # not absolute
        {"path": str(not_a_run), "reason": "r"},  # not a direct child of a .hw store
        {"path": str(tmp_path / "repo" / ".hw" / "no-reason")},  # reason missing
        {"path": str(good), "reason": "Sustituido"},
    ])
    marks, problems = collect_closed.read_register(register)
    assert list(marks) == [str(canonical(good))]
    assert [p.what for p in problems] == [
        "closed-runs register entry 0", "closed-runs register entry 1",
        "closed-runs register entry 2",
    ]


def test_snapshot_marks_listed_runs_and_reports_register_problems(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    closed = _make_run_dir(workspace, "gate0-evidence")
    _make_run_dir(workspace, "still-open")
    register = _write_register(tmp_path / "closed-runs.json", [
        {"path": str(closed), "reason": "Evidencia de Gate 0", "closed_on": "2026-10-13"},
        {"path": "nope", "reason": "r"},
    ])
    memory = _memory(tmp_path)
    shot = snapshot.build_snapshot(workspace, memory_dir=memory, now=NOW, closed_runs=register)
    runs = {run.run_id: run for run in shot.runs}
    assert runs["gate0-evidence"].closed is not None
    assert runs["gate0-evidence"].closed.reason == "Evidencia de Gate 0"
    assert runs["still-open"].closed is None
    assert [item.id for item in shot.attention if item.kind == "awaiting_human"] == [
        "awaiting_human:still-open"
    ]
    assert any(p.what == "closed-runs register entry 1" for p in shot.problems)


# ---------------------------------------------------------------- attention inbox


def _snapshot(*runs: Run) -> Snapshot:
    return Snapshot(
        generated_at=_iso(NOW), workspace="/ws",
        generator=Generator(wilfred_version="0.1.0"), runs=list(runs),
    )


def _mark() -> ClosedMark:
    return ClosedMark(
        reason="Evidencia de Gate 0", closed_on="2026-10-13",
        source=Source(kind="file", path="/cc/closed-runs.json", field="runs"),
    )


def _run(run_id: str = "r1", status: str = "awaiting_human", store: str = "/ws/repo/.hw",
         **fields) -> Run:
    base = {
        "run_id": run_id, "store_path": store, "project_id": "repo", "status": status,
        "updated_at": _iso(NOW - timedelta(days=30)),
        "last_event": LastEvent(timestamp=_iso(NOW - timedelta(days=30)), message="m"),
    }
    base.update(fields)
    return Run(**base)


def _gate(expires_in_hours: float) -> PendingGate:
    expires = NOW + timedelta(hours=expires_in_hours)
    return PendingGate(gate_id="ship", kind="ship", action="merge",
                       challenge_expires_at=_iso(expires), expired=expires < NOW)


def test_closed_run_waiting_for_a_human_leaves_the_inbox() -> None:
    items = derive_attention(_snapshot(_run(closed=_mark())), NOW)
    assert items == []


def test_closed_stale_run_leaves_the_inbox() -> None:
    items = derive_attention(_snapshot(_run(status="in_progress", closed=_mark())), NOW)
    assert items == []


def test_closed_run_with_an_expired_gate_leaves_the_inbox() -> None:
    run = _run(closed=_mark(), pending_gates=[_gate(-48)])
    assert derive_attention(_snapshot(run), NOW) == []


def test_closed_run_with_an_open_approval_window_still_asks_for_attention() -> None:
    run = _run(closed=_mark(), pending_gates=[_gate(5)])
    items = derive_attention(_snapshot(run), NOW)
    assert [(item.kind, item.risk) for item in items] == [("gate", "alto")]


def test_a_run_id_with_one_open_copy_still_asks_for_attention() -> None:
    closed_copy = _run(store="/ws/a/.hw", closed=_mark(), updated_at=_iso(NOW - timedelta(days=1)))
    open_copy = _run(store="/ws/b/.hw", updated_at=_iso(NOW - timedelta(days=40)))
    items = derive_attention(_snapshot(closed_copy, open_copy), NOW)
    assert [item.id for item in items] == ["awaiting_human:r1"]


# ---------------------------------------------------------------- page


def test_page_shows_closed_runs_collapsed_with_their_reason() -> None:
    shot = _snapshot(_run("gate0-evidence", closed=_mark()), _run("still-open"))
    page = render.render_page(shot, now=NOW)
    closed_header = page.index('id="grupo-closed"')
    awaiting_header = page.index('id="grupo-awaiting_human"')
    # The closed group comes after every other group and sits inside a <details>.
    assert closed_header > awaiting_header
    details_open = page.rfind("<details", 0, closed_header)
    assert details_open != -1 and page.find("</details>", details_open) > closed_header
    closed_part = page[closed_header:]
    assert "gate0-evidence" in closed_part
    assert "Evidencia de Gate 0" in closed_part
    assert "still-open" not in closed_part
    assert text_es.GROUP_LABELS["closed"] in page


def test_page_without_closed_runs_has_no_closed_group() -> None:
    page = render.render_page(_snapshot(_run("still-open")), now=NOW)
    assert 'id="grupo-closed"' not in page


# ---------------------------------------------------------------- build command


def test_build_reads_the_register_next_to_the_output(tmp_path: Path, monkeypatch) -> None:
    memory = _memory(tmp_path)
    workspace = tmp_path / "ws"
    closed = _make_run_dir(workspace, "gate0-evidence")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _write_register(out_dir / snapshot.CLOSED_RUNS_NAME, [
        {"path": str(closed), "reason": "Evidencia de Gate 0"},
    ])
    before = (out_dir / snapshot.CLOSED_RUNS_NAME).read_bytes()
    snapshot.run_build(workspace=workspace, out_dir=out_dir, memory_dir=memory)
    stored = json.loads((out_dir / snapshot.SNAPSHOT_NAME).read_text(encoding="utf-8"))
    assert stored["attention"] == []
    assert stored["runs"][0]["closed"]["reason"] == "Evidencia de Gate 0"
    page = (out_dir / render.PAGE_NAME).read_text(encoding="utf-8")
    assert 'id="grupo-closed"' in page
    # The register is read, never rewritten.
    assert (out_dir / snapshot.CLOSED_RUNS_NAME).read_bytes() == before
