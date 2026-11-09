"""Read ``<workspace>/ops`` through the owner's ``ops`` command, for the attention inbox.

``<workspace>/ops`` is the git repository that replaced Notion for tasks, projects and the
sales pipeline (design: Ideas/ops/specs/2026-10-14-notion-to-ops-design.md, outside this
repository). Nothing runs when that folder does not exist.

The two reports (``ops-export/v1``, ``ops-status/v1``) are parsed as plain JSON and checked
field by field: they are input, not a promise. ``ops`` is called with ``--root <workspace>/ops``
and the export must name that folder as its ``root`` again; otherwise none of its data is used
and the status report is not read. Only what the inbox needs is kept: open tasks that are
blocked or dated, pending proposals with the NAMES of the fields they change, and the paths of
hand edits that wait for the owner's ``ops approve --file`` (``unapproved_edits``: files under
``sales/`` or ``schema/``, and task files whose authorization was typed by hand). No sales
record, contact, proposal value or proposal reason is copied. Every failure becomes a
``Problem``; "could not be read" (``None``) is never reported as "none" (``[]``).
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

from hyperwilfred_core.command_center.model import (
    OPS_BLOCKED_STATUS,
    OPS_CLOSED_STATUSES,
    Ops,
    OpsProposal,
    OpsTask,
    Problem,
)
from hyperwilfred_core.command_center.opsread import OpsResult, run_ops
from hyperwilfred_core.command_center.paths import canonical

OPS_DIR_NAME = "ops"
EXPORT_SCHEMA = "ops-export/v1"
STATUS_SCHEMA = "ops-status/v1"
# Where a hand edit that waits for ``ops approve --file`` can live (ops-status/v1).
UNAPPROVED_PREFIXES = ("sales/", "schema/", "tasks/")
MAX_TEXT_CHARS = 500
MAX_ENTRY_PROBLEMS = 5
MAX_FIELD_NAMES = 20
# Relative to the ops root; every segment starts with a letter or digit, so "..", hidden names
# and absolute paths never match.
_SAFE_RELATIVE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*")
_GITHUB_BLOB = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
    r"/blob/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/"
)
_OPS_ID = re.compile(r"[A-Z]+-[0-9]{4,}")  # the ops repository's schema.ID_RE
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


class _Found:
    """Problems of one ops read. Entry problems are capped: a few named, the rest counted."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.problems: list[Problem] = []
        self.entries = 0

    def add(self, relative: str | None, what: str, reason: str) -> None:
        where = self.root / relative if relative else self.root
        self.problems.append(Problem(path=str(where), what=what, reason=reason))

    def entry(self, relative: str | None, what: str, reason: str) -> None:
        self.entries += 1
        if self.entries <= MAX_ENTRY_PROBLEMS:
            self.add(relative, what, reason)

    def finish(self) -> list[Problem]:
        hidden = self.entries - MAX_ENTRY_PROBLEMS
        if hidden > 0:
            self.add(None, "ops entries", f"{hidden} more entries could not be read")
        return self.problems


def _text(value: object) -> str | None:
    return value[:MAX_TEXT_CHARS] if isinstance(value, str) and value else None


def _names(value: object) -> list[str]:
    """A project slug or a list of them, as a list of non-empty texts."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [item[:MAX_TEXT_CHARS] for item in value if isinstance(item, str) and item]


def _relative(value: object, *prefixes: str) -> str | None:
    """``value`` when it is a safe path below the ops root starting with one of ``prefixes``."""
    if isinstance(value, str) and value.startswith(prefixes) and _SAFE_RELATIVE.fullmatch(value):
        return value
    return None


def _date(value: object) -> tuple[str | None, bool]:
    """(the date, malformed?). A missing or empty date is absent, not malformed."""
    if value is None or value == "":
        return None, False
    if isinstance(value, str) and _DATE.fullmatch(value):
        try:
            date.fromisoformat(value)
        except ValueError:
            return None, True
        return value, False
    return None, True


def _report(
    result: OpsResult, schema: str, what: str, ops: Ops, found: _Found
) -> dict[str, Any] | None:
    """The parsed report, or None after recording why it cannot be used."""
    ops.commands.append(result.command)
    if not result.ok:
        found.add(None, what, result.failure_reason)
        return None
    try:
        data = json.loads(result.stdout)
    except (ValueError, RecursionError) as exc:
        found.add(None, what, f"output is not JSON: {str(exc)[:200]}")
        return None
    version = data.get("schema") if isinstance(data, dict) else None
    if version != schema:
        found.add(None, what, f"expected an {schema} report, found {str(version)[:80]!r}")
        return None
    return data


def _same_root(data: dict[str, Any], root: Path, found: _Found) -> bool:
    reported = data.get("root")
    if isinstance(reported, str) and reported:
        try:
            if canonical(reported) == canonical(root):
                return True
        except (ValueError, OSError):
            pass  # an unusable address (embedded NUL, lone surrogate) is a mismatch, not a crash
    found.add(
        None,
        "ops export",
        f"ops reported the repository {str(reported)[:200]!r}, not {str(root)!r}; "
        "its data is not shown",
    )
    return False


def _tasks(raw: object, found: _Found) -> list[OpsTask] | None:
    if not isinstance(raw, list):
        found.add(None, "ops export", "tasks is not a list")
        return None
    kept: list[OpsTask] = []
    for entry in raw:
        if not isinstance(entry, dict):
            found.entry(None, "ops task", "entry is not an object")
            continue
        path = _relative(entry.get("path"), "tasks/")
        task_id, status = _text(entry.get("id")), _text(entry.get("status"))
        if path is None or task_id is None or status is None or not _OPS_ID.fullmatch(task_id):
            found.entry(path, "ops task", "missing or unsafe id, path or status")
            continue
        if status in OPS_CLOSED_STATUSES:
            continue
        due, bad_due = _date(entry.get("due"))
        due_end, bad_end = _date(entry.get("due_end"))
        if bad_due or bad_end:
            found.entry(path, "ops task", "due or due_end is not a YYYY-MM-DD date")
        if status != OPS_BLOCKED_STATUS and due is None and due_end is None:
            continue  # neither blocked nor dated: it can never become an inbox item
        kept.append(
            OpsTask(
                id=task_id,
                path=path,
                title=_text(entry.get("title")) or "",
                status=status,
                due=due,
                due_end=due_end,
                blocked_by=_text(entry.get("blocked_by")),
                projects=_names(entry.get("project")),
            )
        )
    return kept


def _field_names(kind: str, changes: object) -> list[str]:
    """Names of the fields a proposal touches. Values (old, new, added text) are never kept."""
    if not isinstance(changes, dict):
        return []
    if kind == "set":
        names: list[object] = list(changes)
    elif kind == "schema-option":
        names = [changes.get("field")]
    else:
        return []
    valid = {name for name in names if isinstance(name, str) and _FIELD_NAME.fullmatch(name)}
    return sorted(valid)[:MAX_FIELD_NAMES]


def _proposals(raw: object, found: _Found) -> list[OpsProposal] | None:
    if not isinstance(raw, list):
        found.add(None, "ops export", "proposals is not a list")
        return None
    kept: list[OpsProposal] = []
    for entry in raw:
        if not isinstance(entry, dict):
            found.entry(None, "ops proposal", "entry is not an object")
            continue
        path = _relative(entry.get("path"), "proposals/")
        proposal_id, kind = _text(entry.get("id")), _text(entry.get("kind"))
        target = _relative(entry.get("target"), "sales/", "schema/")
        if path is None or proposal_id is None or kind is None or target is None:
            found.entry(path, "ops proposal", "missing or unsafe id, path, kind or target")
            continue
        # The id is printed inside a command the owner pastes; agents write proposal files.
        if not _OPS_ID.fullmatch(proposal_id) or path != f"proposals/{proposal_id}.json":
            found.entry(path, "ops proposal", "id is not an ops id or does not match the path")
            continue
        kept.append(
            OpsProposal(
                id=proposal_id,
                path=path,
                kind=kind,
                target=target,
                fields=_field_names(kind, entry.get("changes")),
                author=_text(entry.get("author")),
                created_at=_text(entry.get("created_at")),
            )
        )
    return kept


def _unapproved(data: dict[str, Any], found: _Found) -> list[str] | None:
    """Hand edits waiting for ``ops approve --file``; ``unapproved_sales`` alone is not enough."""
    raw = data.get("unapproved_edits")
    if not isinstance(raw, list):
        found.add(None, "ops status", "unapproved_edits is not a list")
        return None
    kept: list[str] = []
    for value in raw:
        path = _relative(value, *UNAPPROVED_PREFIXES)
        if path is None:
            reason = f"unexpected path in unapproved_edits: {str(value)[:80]!r}"
            found.entry(None, "ops status", reason)
        elif path not in kept:
            kept.append(path)
    return kept


def _export_problems(raw: object, found: _Found) -> None:
    """Files ``ops`` itself could not read become read problems of the snapshot."""
    if raw is None:
        return
    if not isinstance(raw, list):
        found.add(None, "ops export", "problems is not a list")
        return
    for entry in raw:
        if not isinstance(entry, dict):
            found.entry(None, "ops item", "entry is not an object")
            continue
        message = _text(entry.get("message")) or "unreadable"
        found.entry(_relative(entry.get("path"), ""), "ops item", message)


def _read_export(data: dict[str, Any], ops: Ops, found: _Found) -> None:
    blob = data.get("github_blob")
    if isinstance(blob, str) and _GITHUB_BLOB.fullmatch(blob):
        ops.github_blob = blob
    else:
        found.add(None, "ops export", "github_blob is not a GitHub address; links stay local")
    ops.exported_at = _text(data.get("generated_at"))
    ops.open_tasks = _tasks(data.get("tasks"), found)
    ops.proposals = _proposals(data.get("proposals"), found)
    _export_problems(data.get("problems"), found)


def collect_ops(workspace: Path, ops_bin: Path) -> tuple[Ops | None, list[Problem]]:
    """The ``ops`` block for ``<workspace>/ops``; ``None`` without a problem when absent."""
    root = workspace / OPS_DIR_NAME
    if not os.path.lexists(root):
        return None, []
    found = _Found(root)
    if os.path.islink(root) or not os.path.isdir(root):
        found.add(None, "ops folder", "not a directory (links are not followed); not read")
        return None, found.finish()
    ops = Ops(root=str(root), bin=str(ops_bin))
    if not (os.path.isfile(ops_bin) and os.access(ops_bin, os.X_OK)):
        found.problems.append(
            Problem(
                path=str(ops_bin),
                what="ops command",
                reason="missing or not executable; ops tasks and proposals were not read",
            )
        )
        return ops, found.finish()
    export = _report(
        run_ops(ops_bin, root, ["export", "--json"]), EXPORT_SCHEMA, "ops export", ops, found
    )
    if export is None or not _same_root(export, root, found):
        return ops, found.finish()  # without a confirmed root nothing ops says can be placed
    _read_export(export, ops, found)
    status = _report(
        run_ops(ops_bin, root, ["status", "--json"]), STATUS_SCHEMA, "ops status", ops, found
    )
    if status is not None:
        ops.unapproved_edits = _unapproved(status, found)
    return ops, found.finish()
