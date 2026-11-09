"""Test-only stand-ins for the ``ops`` command (the owner's tool for ``<workspace>/ops``).

``fake_ops`` writes a small ``/bin/sh`` program that prints a canned report for the
sub-command it receives and records its arguments and environment next to itself, so tests can
check exactly how the command center ran it. It always lives outside every test workspace.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BLOB = "https://github.com/example/ops/blob/main/"


def task_entry(task_id: str, status: str, **fields: Any) -> dict[str, Any]:
    """One task of an ``ops-export/v1`` report, every schema field present."""
    entry: dict[str, Any] = {
        "id": task_id,
        "path": f"tasks/{task_id}-x.md",
        "title": f"Task {task_id}",
        "project": ["project-a"],
        "status": status,
        "priority": "High",
        "owner": "CTO",
        "area": "Infrastructure",
        "phase": "",
        "due": "",
        "due_end": "",
        "blocked_by": "",
        "source": "",
        "created": "2026-10-07",
        "authorized_by": "",
        "authorized_at": "",
        "notion_id": "",
    }
    entry.update(fields)
    return entry


def export_report(root: str, **overrides: Any) -> dict[str, Any]:
    """An ``ops-export/v1`` report for the repository ``root`` (master plan shapes)."""
    report: dict[str, Any] = {
        "schema": "ops-export/v1",
        "generated_at": "2026-10-14T10:00:00Z",
        "root": root,
        "github_blob": BLOB,
        "projects": [],
        "tasks": [
            task_entry("T-0001", "Blocked", blocked_by="the owner's decision pending"),
            task_entry("T-0002", "In Progress", due="2026-10-01"),
            task_entry("T-0003", "Complete", due="2026-09-01"),
            task_entry("T-0004", "Not Started"),
        ],
        "sales": [
            {
                "slug": "acme",
                "path": "sales/acme.md",
                "company": "Acme",
                "status": "Email Sent",
                "contact_email": "person@example.com",
            }
        ],
        "proposals": [
            {
                "id": "P-0003",
                "path": "proposals/P-0003.json",
                "kind": "set",
                "target": "sales/acme.md",
                "changes": {"status": {"from": "Email Sent", "to": "LOI Negotiation"}},
                "reason": "Reply from person@example.com",
                "author": "pilot-agent",
                "created_at": "2026-10-13T09:00:00Z",
            }
        ],
        "problems": [],
    }
    report.update(overrides)
    return report


def status_report(**overrides: Any) -> dict[str, Any]:
    """An ``ops-status/v1`` report (master plan shape, amendment A6)."""
    report: dict[str, Any] = {
        "schema": "ops-status/v1",
        "uncommitted": ["sales/volotea.md"],
        "unapproved_sales": ["sales/volotea.md"],
        "unapproved_edits": ["sales/volotea.md"],
        "unpushed_commits": 0,
        "check_errors": [],
        "pending_proposals": 1,
        "last_sync_at": "2026-10-14T10:00:00Z",
        "last_sync_error": "",
        "conflicts": [],
    }
    report.update(overrides)
    return report


def fake_ops(
    where: Path,
    *,
    export: object = None,
    status: object = None,
    exit_code: int = 0,
    stderr: str = "",
    hang: bool = False,
) -> Path:
    """Write the stand-in program ``<where>/ops`` and its canned reports; return its path.

    ``export`` and ``status`` are a report (written as JSON) or raw text printed as it is. The
    program records ``args-<sub>.txt`` and ``env-<sub>.txt`` in ``where``; ``hang`` makes it
    sleep instead of answering.
    """
    where.mkdir(parents=True, exist_ok=True)
    reports = {
        "export": export if export is not None else export_report(root=""),
        "status": status if status is not None else status_report(),
    }
    for name, payload in reports.items():
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (where / f"{name}.json").write_text(text, encoding="utf-8")
    (where / "stderr.txt").write_text(stderr, encoding="utf-8")
    if hang:
        body = "exec sleep 30\n"
    else:
        body = (
            'sub=""\n'
            'for a in "$@"; do case "$a" in export|status) sub=$a;; esac; done\n'
            'printf "%s\\n" "$@" > "$here/args-$sub.txt"\n'
            'env > "$here/env-$sub.txt"\n'
            'cat "$here/stderr.txt" >&2\n'
            'cat "$here/$sub.json"\n'
            f"exit {exit_code}\n"
        )
    program = where / "ops"
    program.write_text('#!/bin/sh\nhere=$(dirname "$0")\n' + body, encoding="utf-8")
    program.chmod(0o755)
    return program
