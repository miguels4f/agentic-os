"""Read the closed-runs register: runs the owner closed, which stop asking for attention.

The register is ``closed-runs.json`` in the command center's output directory, written by a
person (or an agent acting on the owner's decision), never by this package::

    {
      "schema_version": "wilfred-closed-runs/v1",
      "runs": [
        {"path": "/abs/project/.hw/<run>", "reason": "plain sentence", "closed_on": "2026-10-13"}
      ]
    }

``path`` is the run directory (a direct child of a ``.hw`` store). Closing changes nothing on
disk: the run stays where it is and stays on the page, in a collapsed group. A register that
cannot be read closes nothing; a bad entry is skipped. Both are reported as problems, so a
mistake shows more on the page, never less.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from hyperwilfred_core.command_center.model import ClosedMark, Problem, Run, Source
from hyperwilfred_core.command_center.paths import canonical, secret_part

REGISTER_SCHEMA = "wilfred-closed-runs/v1"
MAX_REGISTER_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 500
MAX_ENTRY_PROBLEMS = 20
_WHAT = "closed-runs register"


def _entry(raw: object, register: Path) -> tuple[str, ClosedMark] | str:
    """(canonical run directory, mark), or the reason the entry is skipped."""
    if not isinstance(raw, dict):
        return f"expected an object, found {type(raw).__name__}"
    path = raw.get("path")
    reason = raw.get("reason")
    closed_on = raw.get("closed_on")
    if not isinstance(path, str) or not os.path.isabs(path):
        return "path must be an absolute path to a run directory"
    if Path(path).parent.name != ".hw":
        return "path must be a run directory directly inside a .hw store"
    if secret_part(path) is not None:
        return "path lies under a secrets directory, which is never read"
    if not isinstance(reason, str) or not reason.strip():
        return "reason must be a non-empty sentence"
    if closed_on is not None and not isinstance(closed_on, str):
        return f"closed_on must be text, found {type(closed_on).__name__}"
    mark = ClosedMark(
        reason=reason.strip()[:MAX_TEXT_CHARS],
        closed_on=closed_on[:MAX_TEXT_CHARS] if closed_on else None,
        source=Source(kind="file", path=str(register), field="runs"),
    )
    return str(canonical(path)), mark


def read_register(register: Path) -> tuple[dict[str, ClosedMark], list[Problem]]:
    """Marks keyed by canonical run directory, plus what could not be used. Reads only."""
    problems: list[Problem] = []
    if not os.path.lexists(register):
        return {}, problems

    def refuse(reason: str) -> tuple[dict[str, ClosedMark], list[Problem]]:
        problems.append(Problem(path=str(register), what=_WHAT, reason=reason))
        return {}, problems

    try:
        if os.path.islink(register) or not os.path.isfile(register):
            return refuse("not a regular file; nothing is treated as closed")
        if os.path.getsize(register) > MAX_REGISTER_BYTES:
            return refuse(f"larger than {MAX_REGISTER_BYTES} bytes; nothing is treated as closed")
        loaded = json.loads(register.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        return refuse(f"cannot be read as JSON ({exc}); nothing is treated as closed")
    if not isinstance(loaded, dict) or loaded.get("schema_version") != REGISTER_SCHEMA:
        return refuse(f"schema_version is not {REGISTER_SCHEMA!r}; nothing is treated as closed")
    entries = loaded.get("runs")
    if not isinstance(entries, list):
        return refuse("runs must be a list; nothing is treated as closed")
    marks: dict[str, ClosedMark] = {}
    skipped = 0
    for index, raw in enumerate(entries):
        result = _entry(raw, register)
        if isinstance(result, str):
            skipped += 1
            if skipped <= MAX_ENTRY_PROBLEMS:
                problems.append(
                    Problem(path=str(register), what=f"{_WHAT} entry {index}", reason=result)
                )
            continue
        key, mark = result
        marks[key] = mark
    if skipped > MAX_ENTRY_PROBLEMS:
        problems.append(
            Problem(path=str(register), what=_WHAT,
                    reason=f"{skipped - MAX_ENTRY_PROBLEMS} more entries were skipped")
        )
    return marks, problems


def mark_closed(runs: list[Run], marks: dict[str, ClosedMark]) -> None:
    """Attach each register mark to the run whose directory it names."""
    if not marks:
        return
    for run in runs:
        run_dir = Path(run.store_path) / run.run_id
        run.closed = marks.get(str(canonical(run_dir)))


def closed_run_ids(runs: list[Run]) -> set[str]:
    """Run ids whose every copy is listed in the register.

    A copy left open elsewhere still deserves attention, so one open copy keeps the run id open.
    """
    open_ids = {run.run_id for run in runs if run.closed is None}
    return {run.run_id for run in runs} - open_ids
