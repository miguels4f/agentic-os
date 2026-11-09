"""Command center: the runs collector (tolerant reading of ``.hw`` run stores)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hyperwilfred_core.command_center.collect_projects import collect_projects
from hyperwilfred_core.command_center.collect_runs import collect_runs
from hyperwilfred_core.command_center.model import Run


def _state(run_id: str, **extra) -> dict:
    state = {
        "run_id": run_id,
        "mode": "build",
        "tier": "full",
        "status": "in_progress",
        "objective": {"id": "o", "title": "t", "objective": "x"},
        "product_dir": ".",
        "steps": [{"id": "plan"}, {"id": "implement"}, {"id": "ship"}],
        "step_status": {"plan": "done", "implement": "running", "ship": "pending"},
        "quality": None,
        "base_commit": None,
        "created_at": "2026-09-01T10:00:00Z",
        "updated_at": "2026-09-02T11:00:00Z",
    }
    state.update(extra)
    return state


def _write_run(root: Path, run_id: str, content: str | dict) -> Path:
    run_dir = root / ".hw" / run_id
    run_dir.mkdir(parents=True)
    text = content if isinstance(content, str) else json.dumps(content)
    (run_dir / "state.json").write_text(text, encoding="utf-8")
    return run_dir / "state.json"


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _collect(workspace: Path) -> tuple[dict[str, Run], list]:
    projects, project_problems = collect_projects(workspace)
    assert project_problems == []
    runs, problems = collect_runs(projects, now=NOW)
    return {r.run_id: r for r in runs}, problems


def test_runs_written_by_the_real_engine_are_read(hw_history: Path) -> None:
    runs, problems = _collect(hw_history)
    assert problems == []
    assert sorted(runs) == ["r1", "r2", "r3"]
    for run in runs.values():
        assert run.readable is True
        assert run.mode == "build"
        assert run.project_id == "."
        assert run.store_path == str(hw_history.resolve() / ".hw")
        assert run.unknown_fields == []
        assert run.created_at and run.updated_at
        assert run.current_step in run.step_status or run.step_status == {}
    # hw_history gave r1 and r3 a quality block through the engine; it is a known key.
    assert all(s.kind == "file" for s in runs["r1"].sources)
    assert {Path(s.path).name for s in runs["r1"].sources} == {"state.json", "events.jsonl"}
    # r1: the engine recorded a quality block and events (plan + two gate failures).
    assert runs["r1"].quality is not None and runs["r1"].quality.passed is True
    assert (runs["r1"].quality.iteration, runs["r1"].quality.max_iterations) == (2, 3)
    assert runs["r1"].event_count and runs["r1"].event_count >= 2
    assert runs["r1"].last_event is not None and runs["r1"].last_event.status == "gate_failed"
    assert runs["r2"].quality is None
    assert all(run.cost.state == "unknown" and run.pending_gates == [] for run in runs.values())


def test_basic_fields_and_current_step(tmp_path: Path) -> None:
    _write_run(tmp_path, "run-a", _state("run-a"))
    done = {"plan": "done", "implement": "done", "ship": "done"}
    _write_run(tmp_path, "run-b", _state("run-b", status="completed", step_status=done))
    runs, problems = _collect(tmp_path)
    assert problems == []
    a, b = runs["run-a"], runs["run-b"]
    assert (a.mode, a.tier, a.status) == ("build", "full", "in_progress")
    assert a.step_status == {"plan": "done", "implement": "running", "ship": "pending"}
    assert a.current_step == "implement"
    assert a.created_at == "2026-09-01T10:00:00Z" and a.updated_at == "2026-09-02T11:00:00Z"
    assert b.current_step is None
    assert {s.field for s in a.sources} >= {"status", "mode", "tier", "step_status", "updated_at"}


def test_current_step_follows_the_declared_step_order(tmp_path: Path) -> None:
    # step_status lists "ship" first and omits "implement": the order comes from ``steps``.
    _write_run(tmp_path, "r", _state("r", step_status={"ship": "pending", "plan": "done"}))
    runs, _ = _collect(tmp_path)
    assert runs["r"].current_step == "implement"


def test_run_without_detail_files(tmp_path: Path) -> None:
    """No events file, no quality, no gates, no usage: facts, not problems; cost is unknown."""
    _write_run(tmp_path, "r", _state("r"))
    runs, problems = _collect(tmp_path)
    run = runs["r"]
    assert problems == []
    assert run.quality is None and run.last_event is None and run.event_count == 0
    assert run.pending_gates == [] and run.duplicate_of == []
    assert run.cost.state == "unknown" and run.cost.tokens is None
    cost_json = json.loads(run.model_dump_json())["cost"]
    assert cost_json == {"state": "unknown", "tokens": None}
    assert "0" not in json.dumps(cost_json), "an unknown cost must never show a zero"


def test_run_with_unknown_extra_keys(tmp_path: Path) -> None:
    _write_run(tmp_path, "r", _state("r", future_field=1, another_new_key={"a": 2}))
    runs, problems = _collect(tmp_path)
    assert problems == []
    assert runs["r"].readable is True
    assert runs["r"].unknown_fields == ["another_new_key", "future_field"]
    assert runs["r"].status == "in_progress"


def test_run_with_corrupt_state_still_appears(tmp_path: Path) -> None:
    state_path = _write_run(tmp_path, "broken", '{"run_id": "broken", "status": ')
    _write_run(tmp_path, "fine", _state("fine"))
    runs, problems = _collect(tmp_path)
    assert sorted(runs) == ["broken", "fine"]
    broken = runs["broken"]
    assert broken.readable is False
    assert (broken.mode, broken.tier, broken.status) == ("unknown", "unknown", "unknown")
    assert len(problems) == 1
    assert problems[0].path == str(state_path.resolve())
    assert "JSON" in problems[0].reason


def test_state_that_is_not_an_object_is_unreadable(tmp_path: Path) -> None:
    _write_run(tmp_path, "listy", "[1, 2, 3]")
    runs, problems = _collect(tmp_path)
    assert runs["listy"].readable is False
    assert len(problems) == 1 and "object" in problems[0].reason


def test_missing_and_wrongly_typed_fields(tmp_path: Path) -> None:
    _write_run(tmp_path, "r", {"status": 7, "step_status": {"a": "done", "b": 3}})
    runs, problems = _collect(tmp_path)
    run = runs["r"]  # run_id falls back to the directory name
    assert run.readable is True
    assert (run.mode, run.tier, run.status) == ("unknown", "unknown", "unknown")
    assert run.step_status == {"a": "done"}
    assert run.created_at is None
    assert sorted(p.what for p in problems) == ["field status", "field step_status.b"]


def test_non_run_entries_are_ignored_without_problems(tmp_path: Path) -> None:
    _write_run(tmp_path, "real", _state("real"))
    store = tmp_path / ".hw"
    (store / ".lock").write_text("", encoding="utf-8")
    (store / "hw.lock").write_text("pid 1", encoding="utf-8")
    (store / "loose-script.py").write_text("print(1)\n", encoding="utf-8")
    (store / "observability").mkdir()
    (store / "observability" / "dashboard.json").write_text("{not json", encoding="utf-8")
    (store / "proposals").mkdir()
    (store / "lessons").mkdir()
    runs, problems = _collect(tmp_path)
    assert problems == []
    assert sorted(runs) == ["real"]


def test_never_descends_into_run_worktrees(tmp_path: Path) -> None:
    _write_run(tmp_path, "outer", _state("outer"))
    nested = tmp_path / ".hw" / "outer" / "worktrees" / "s0"
    _write_run(nested, "inner", _state("inner"))
    runs, problems = _collect(tmp_path)
    assert problems == []
    assert sorted(runs) == ["outer"]


def test_runs_are_found_in_repository_and_worktree_stores(tmp_path, git_repo, git_helper) -> None:
    # ``git_repo`` lives at tmp_path/"repo"; tmp_path is the workspace.
    linked = tmp_path / "linked"
    git_helper(git_repo, "worktree", "add", "-q", "-b", "side", str(linked))
    _write_run(git_repo, "in-repo", _state("in-repo"))
    _write_run(linked, "in-worktree", _state("in-worktree"))
    runs, problems = _collect(tmp_path)
    assert problems == []
    assert runs["in-repo"].project_id == "repo"
    assert runs["in-worktree"].project_id == "linked"
    assert runs["in-worktree"].store_path == str(linked.resolve() / ".hw")


def test_run_id_is_the_directory_name_never_the_stored_text(tmp_path: Path) -> None:
    state_path = _write_run(tmp_path, "real-dir", _state("../../etc"))
    runs, problems = _collect(tmp_path)
    assert sorted(runs) == ["real-dir"]
    assert len(problems) == 1 and problems[0].what == "field run_id"
    assert problems[0].path == str(state_path.resolve())


def test_empty_text_is_reported_as_such(tmp_path: Path) -> None:
    _write_run(tmp_path, "r", _state("r", status=""))
    runs, problems = _collect(tmp_path)
    assert runs["r"].status == "unknown"
    assert [p.reason for p in problems] == ["expected text, found empty text"]


def test_run_directory_that_cannot_be_entered_is_a_problem(tmp_path: Path) -> None:
    _write_run(tmp_path, "open", _state("open"))
    _write_run(tmp_path, "locked", _state("locked"))
    locked = tmp_path / ".hw" / "locked"
    locked.chmod(0o000)
    try:
        if os.access(locked, os.X_OK):
            pytest.skip("running with privileges that ignore directory permissions")
        runs, problems = _collect(tmp_path)
    finally:
        locked.chmod(0o700)
    assert sorted(runs) == ["open"]
    assert len(problems) == 1 and problems[0].path == str(locked.resolve())


def test_symbolic_links_in_a_store_are_reported_not_followed(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    _write_run(elsewhere, "target", _state("target"))
    workspace = tmp_path / "ws"
    _write_run(workspace, "real", _state("real"))
    (workspace / ".hw" / "linked").symlink_to(elsewhere / ".hw" / "target")
    (workspace / ".hw" / "plain-link").symlink_to(tmp_path)  # not a run: no problem
    runs, problems = _collect(workspace)
    assert sorted(runs) == ["real"]
    assert [p.reason for p in problems] == ["symbolic link to a run; links are not followed"]

    other = tmp_path / "ws2"
    other.mkdir()
    (other / ".hw").symlink_to(elsewhere / ".hw")
    runs, problems = _collect(other)
    assert runs == {} and [p.what for p in problems] == ["run store"]


# ------------------------------------------------------------------------------- run detail


def _event(timestamp: str, stage: str, status: str = "running", message: str = "m") -> str:
    return json.dumps({"event_id": "e", "run_id": "r", "timestamp": timestamp, "stage": stage,
                       "status": status, "message": message, "cost_usd": 0.0})


def test_events_with_one_corrupt_line(tmp_path: Path) -> None:
    state_path = _write_run(tmp_path, "r", _state("r"))
    events = state_path.parent / "events.jsonl"
    events.write_text(
        "\n".join([
            _event("2026-09-02T10:00:00+00:00", "implement", "accepted", "latest by time"),
            "{this line is corrupt",
            _event("2026-09-01T10:00:00+00:00", "plan", message="older but written later"),
            "[1, 2]",
            "",
        ]) + "\n",
        encoding="utf-8",
    )
    runs, problems = _collect(tmp_path)
    run = runs["r"]
    assert run.event_count == 2
    assert run.last_event is not None
    assert (run.last_event.stage, run.last_event.status) == ("implement", "accepted")
    assert run.last_event.message == "latest by time"
    assert len(problems) == 1 and problems[0].path == str(events.resolve())
    assert "2 line(s)" in problems[0].reason
    assert "cost_usd" not in run.model_dump_json()


def test_long_event_message_is_shortened(tmp_path: Path) -> None:
    state_path = _write_run(tmp_path, "r", _state("r"))
    (state_path.parent / "events.jsonl").write_text(
        _event("2026-09-02T10:00:00Z", "plan", message="x" * 5000) + "\n", encoding="utf-8"
    )
    runs, _ = _collect(tmp_path)
    assert runs["r"].last_event is not None
    assert len(runs["r"].last_event.message or "") <= 501


def test_unreadable_events_file_leaves_the_count_unknown(tmp_path: Path) -> None:
    state_path = _write_run(tmp_path, "r", _state("r"))
    (state_path.parent / "events.jsonl").mkdir()  # not a regular file
    runs, problems = _collect(tmp_path)
    assert runs["r"].event_count is None and runs["r"].last_event is None
    assert [p.what for p in problems] == ["run events"]


def test_quality_absent_and_quality_with_override(tmp_path: Path) -> None:
    _write_run(tmp_path, "plain", _state("plain"))
    quality = {"phase": "patch", "iteration": 1, "max_iterations": 3, "passed": False,
               "independent_review_override": {"reviewer": "owner", "reason": "accepted"},
               "history": [], "newer_field": 1}
    _write_run(tmp_path, "overridden", _state("overridden", quality=quality))
    _write_run(tmp_path, "empty-override", _state(
        "empty-override", quality={"phase": "evaluate", "independent_review_override": {}}))
    runs, problems = _collect(tmp_path)
    assert problems == []
    assert runs["plain"].quality is None
    q = runs["overridden"].quality
    assert q is not None
    assert (q.phase, q.iteration, q.max_iterations, q.passed) == ("patch", 1, 3, False)
    assert q.override_present is True
    e = runs["empty-override"].quality
    assert e is not None and e.override_present is False
    assert e.iteration is None and e.passed is None, "absent values are unknown, not zero/false"


def _gate(run_dir: Path, gate_id: str, *, expires: str | None, consumed: bool = False) -> None:
    gates = run_dir / "gates"
    gates.mkdir(exist_ok=True)
    (gates / f"{gate_id}.json").write_text(
        json.dumps({"gate_id": gate_id, "kind": "deploy", "action": f"ship {gate_id}",
                    "status": "pending"}), encoding="utf-8")
    (gates / f"{gate_id}.pending").write_text("pending\n", encoding="utf-8")
    if expires is not None:
        (gates / f"{gate_id}.challenge.json").write_text(
            json.dumps({"gate_id": gate_id, "expires_at": expires}), encoding="utf-8")
    if consumed:
        (gates / f"{gate_id}.decision.json").write_text("SIGNED-TOKEN", encoding="utf-8")
        (gates / f"{gate_id}.decision.consumed").write_text("", encoding="utf-8")


def test_pending_gates_unexpired_expired_and_consumed(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path, "r", _state("r", status="awaiting_human")).parent
    _gate(run_dir, "fresh", expires="2026-10-01T13:00:00+00:00")   # NOW is 12:00
    _gate(run_dir, "stale", expires="2026-10-01T11:00:00+00:00")
    _gate(run_dir, "done", expires="2026-10-01T13:00:00+00:00", consumed=True)
    _gate(run_dir, "no-challenge", expires=None)
    runs, problems = _collect(tmp_path)
    assert problems == []
    gates = {g.gate_id: g for g in runs["r"].pending_gates}
    assert sorted(gates) == ["fresh", "no-challenge", "stale"]
    assert gates["fresh"].expired is False and gates["stale"].expired is True
    assert gates["fresh"].challenge_expires_at == "2026-10-01T13:00:00+00:00"
    bare = gates["no-challenge"]
    assert bare.expired is None and bare.challenge_expires_at is None
    assert (gates["fresh"].kind, gates["fresh"].action) == ("deploy", "ship fresh")
    assert "SIGNED-TOKEN" not in runs["r"].model_dump_json()


def test_gate_files_that_cannot_be_read_are_problems(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path, "r", _state("r")).parent
    gates = run_dir / "gates"
    gates.mkdir()
    (gates / "orphan.pending").write_text("pending\n", encoding="utf-8")
    (gates / "bad.pending").write_text("pending\n", encoding="utf-8")
    (gates / "bad.json").write_text("{corrupt", encoding="utf-8")
    (gates / "bad.challenge.json").write_text(json.dumps({"expires_at": "soon"}), encoding="utf-8")
    runs, problems = _collect(tmp_path)
    assert sorted(g.gate_id for g in runs["r"].pending_gates) == ["bad", "orphan"]
    assert all(g.kind is None and g.expired is None for g in runs["r"].pending_gates)
    assert sorted(p.what for p in problems) == ["gate", "gate", "gate challenge"]


def test_markers_that_are_not_gates_never_open_decision_files(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path, "r", _state("r")).parent
    _gate(run_dir, "real", expires="2026-10-01T13:00:00Z")
    gates = run_dir / "gates"
    token = json.dumps({"kind": "TOKEN-KIND", "action": "TOKEN-ACTION"})
    (gates / "x.decision.json").write_text(token, encoding="utf-8")
    (gates / "x.decision.pending").write_text("pending\n", encoding="utf-8")
    (gates / "y.challenge.json").write_text(token, encoding="utf-8")
    (gates / "y.challenge.pending").write_text("pending\n", encoding="utf-8")
    (gates / ".pending").write_text("pending\n", encoding="utf-8")
    (gates / "dir.pending").mkdir()
    (gates / "link.pending").symlink_to(gates / "nowhere")
    (gates / "dotted.name.pending").write_text("pending\n", encoding="utf-8")
    (gates / "dotted.name.json").write_text(json.dumps({"kind": "deploy"}), encoding="utf-8")
    runs, problems = _collect(tmp_path)
    run = runs["r"]
    assert sorted(g.gate_id for g in run.pending_gates) == ["dotted.name", "real"]
    assert next(g for g in run.pending_gates if g.gate_id == "real").expired is False
    assert "TOKEN" not in run.model_dump_json()
    assert len(problems) == 5 and {p.what for p in problems} == {"gate"}


def test_gates_entry_that_is_a_file_is_a_problem(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path, "r", _state("r")).parent
    (run_dir / "gates").write_text("oops", encoding="utf-8")
    runs, problems = _collect(tmp_path)
    assert runs["r"].pending_gates == []
    assert [(p.what, p.reason) for p in problems] == [("run gates", "not a directory")]


def test_hostile_values_never_abort_the_snapshot(tmp_path: Path) -> None:
    """Boundary dates, lone surrogates and oversized text are data, not crashes."""
    from hyperwilfred_core.command_center import snapshot

    run_dir = _write_run(tmp_path, "r", _state(
        "r", updated_at="0001-01-01T00:00:00+10:00", status="in\ud800progress",
        quality={"phase": 5, "passed": "yes", "iteration": True})).parent
    _write_run(tmp_path / "sub", "r", _state("r", updated_at="9999-12-31T23:59:59-10:00"))
    (tmp_path / "sub" / ".git").mkdir()  # not a usable repository: one problem, still a folder
    (run_dir / "events.jsonl").write_text(
        "\n".join([
            json.dumps({"timestamp": "0001-01-01T00:00:00+10:00", "stage": "s" * 5000,
                        "status": "running", "message": "\ud800 lone surrogate"}),
            json.dumps({"timestamp": "not a date", "stage": "x", "status": "y", "message": "z"}),
        ]) + "\n", encoding="utf-8")
    _gate(run_dir, "g", expires="9999-12-31T23:59:59-10:00")
    (run_dir / "usage.json").write_text(
        json.dumps({"totals": {"input_tokens": -5, "output_tokens": 10**400}}), encoding="utf-8")

    built = snapshot.build_snapshot(tmp_path, memory_dir=tmp_path / "no-memory", now=NOW)
    text = snapshot.serialise(built)
    reread = json.loads(text)
    text.encode("utf-8")  # must be writable
    run = next(r for r in reread["runs"] if r["store_path"] == str(run_dir.parent))
    assert run["event_count"] == 2 and len(run["last_event"]["stage"]) <= 501
    assert run["quality"] == {"phase": None, "iteration": None, "max_iterations": None,
                              "passed": None, "override_present": False}
    assert run["pending_gates"][0]["expired"] is None
    assert run["cost"] == {"state": "unknown", "tokens": None}
    whats = [p["what"] for p in reread["problems"]]
    for expected in ("field quality.phase", "field quality.passed", "field quality.iteration",
                     "gate challenge", "run usage", "run events"):
        assert expected in whats, expected


def test_same_run_id_in_three_stores_with_one_unreadable_copy(tmp_path: Path) -> None:
    from hyperwilfred_core.command_center import snapshot

    for name, content in (
        ("a", _state("shared", status="in_progress", updated_at="2026-09-02T00:00:00Z")),
        ("b", _state("shared", status="completed", updated_at="2026-09-03T00:00:00Z")),
        ("c", "{corrupt"),
    ):
        (tmp_path / name).mkdir()
        (tmp_path / name / ".git").mkdir()  # listed as a folder project (with a problem)
        _write_run(tmp_path / name, "shared", content)
    built = snapshot.build_snapshot(tmp_path, memory_dir=tmp_path / "no-memory", now=NOW)
    copies = [r for r in built.runs if r.run_id == "shared"]
    assert len(copies) == 3 and all(len(r.duplicate_of) == 2 for r in copies)
    assert built.totals.runs == 1 and built.totals.run_copies == 3
    assert built.totals.runs_by_status == {"completed": 1}


def test_cost_recorded_only_from_usage_json(tmp_path: Path) -> None:
    with_usage = _write_run(tmp_path, "paid", _state("paid")).parent
    totals = {"input_tokens": 1200, "output_tokens": 340, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 77}
    (with_usage / "usage.json").write_text(
        json.dumps({"totals": totals, "by_model": {}, "by_role": {}}), encoding="utf-8")
    (with_usage / "events.jsonl").write_text(
        json.dumps({"timestamp": "2026-09-02T10:00:00Z", "stage": "x", "status": "running",
                    "message": "m", "cost_usd": 12.5}) + "\n", encoding="utf-8")
    broken = _write_run(tmp_path, "broken-usage", _state("broken-usage")).parent
    (broken / "usage.json").write_text(json.dumps({"totals": {"input_tokens": "many"}}),
                                       encoding="utf-8")
    _write_run(tmp_path, "free", _state("free"))
    runs, problems = _collect(tmp_path)
    assert runs["paid"].cost.state == "recorded" and runs["paid"].cost.tokens == totals
    assert "12.5" not in runs["paid"].model_dump_json(), "cost_usd of events is never used"
    assert runs["free"].cost.state == "unknown" and runs["free"].cost.tokens is None
    assert runs["broken-usage"].cost.state == "unknown"
    assert runs["broken-usage"].cost.tokens is None
    assert [p.what for p in problems] == ["run usage"]


def test_detail_is_read_even_when_the_state_is_corrupt(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path, "broken", "{nope").parent
    (run_dir / "events.jsonl").write_text(_event("2026-09-02T10:00:00Z", "plan") + "\n",
                                          encoding="utf-8")
    _gate(run_dir, "ship", expires="2026-10-01T13:00:00+00:00")
    runs, problems = _collect(tmp_path)
    run = runs["broken"]
    assert run.readable is False and run.event_count == 1
    assert [g.gate_id for g in run.pending_gates] == ["ship"]
    assert [p.what for p in problems] == ["run state"]


def test_same_run_id_in_two_stores(tmp_path, git_repo, git_helper) -> None:
    linked = tmp_path / "linked"
    git_helper(git_repo, "worktree", "add", "-q", "-b", "side", str(linked))
    _write_run(git_repo, "shared", _state("shared", status="in_progress",
                                          updated_at="2026-09-02T11:00:00Z"))
    _write_run(linked, "shared", _state("shared", status="completed",
                                        updated_at="2026-09-03T11:00:00Z"))
    _write_run(git_repo, "alone", _state("alone"))
    projects, _ = collect_projects(tmp_path)
    runs, problems = collect_runs(projects, now=NOW)
    assert problems == [] and len(runs) == 3
    repo_store, linked_store = str(git_repo.resolve() / ".hw"), str(linked.resolve() / ".hw")
    copies = {r.store_path: r for r in runs if r.run_id == "shared"}
    assert copies[repo_store].duplicate_of == [linked_store]
    assert copies[linked_store].duplicate_of == [repo_store]
    assert next(r for r in runs if r.run_id == "alone").duplicate_of == []

    from hyperwilfred_core.command_center import snapshot

    built = snapshot.build_snapshot(tmp_path, memory_dir=tmp_path / "no-memory", now=NOW)
    assert built.totals.runs == 2 and built.totals.run_copies == 3
    # One status per distinct run: the copy that moved last ("completed") speaks for "shared".
    assert built.totals.runs_by_status == {"completed": 1, "in_progress": 1}
    assert sum(built.totals.runs_by_status.values()) == built.totals.runs


def test_one_corrupt_file_cannot_flood_the_problem_list(tmp_path: Path) -> None:
    statuses = {f"step{i}": i for i in range(1000)} | {"ok" + "k" * 2000: "done"}
    _write_run(tmp_path, "r", _state("r", step_status=statuses, **{"u" * 3000: 1}))
    runs, problems = _collect(tmp_path)
    assert len(problems) == 6 and "995 more" in problems[-1].reason
    assert all(len(key) <= 500 for key in runs["r"].step_status)
    assert all(len(name) <= 500 for name in runs["r"].unknown_fields)


def test_quality_numbers_out_of_range_are_unknown(tmp_path: Path) -> None:
    _write_run(tmp_path, "r", _state("r", quality={"iteration": 10**400, "max_iterations": -1}))
    runs, problems = _collect(tmp_path)
    assert runs["r"].quality is not None
    assert runs["r"].quality.iteration is None and runs["r"].quality.max_iterations is None
    assert [p.reason for p in problems] == ["number out of range"] * 2
