"""Collect run stores and runs (discovery rules 3 and 4) with their detail.

``state.json`` files were written by different Wilfred versions, so they are read as plain
dictionaries (never through the engine's ``RunState``, which rejects unknown keys). Known
keys are copied, every other top-level key is listed in ``unknown_fields``, and a file that
cannot be read still yields a run with ``readable: false`` plus a ``Problem``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hyperwilfred_core.command_center.model import UNKNOWN, Problem, Project, Run, Source
from hyperwilfred_core.command_center.paths import is_inside_run_store, secret_part
from hyperwilfred_core.command_center.run_detail import (
    parse_time,
    read_cost,
    read_events,
    read_pending_gates,
    read_quality,
)

STORE_NAME = ".hw"
STATE_NAME = "state.json"
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_NAME_CHARS = 500
MAX_FIELD_PROBLEMS = 5

# Top-level keys of ``state.json`` known to this reader. The first group is copied into the
# snapshot (``steps`` only gives the step order); the rest is recognised but not shown.
_COPIED_KEYS = ("run_id", "mode", "tier", "status", "step_status", "created_at", "updated_at")
KNOWN_STATE_KEYS = frozenset(
    {
        *_COPIED_KEYS, "steps", "quality", "objective", "product_dir", "base_commit",
        "base_collection_sha256", "deterministic_evidence_emitted",
        "deterministic_scope_sha256", "deterministic_target", "packet_worktrees",
        "regression_baseline_recorded_at", "regression_baseline_sha256",
        "regression_baseline_source_file", "regression_baseline_source_run_id",
    }
)


def _text(
    state: dict[str, Any], key: str, default: str | None, path: Path, problems: list[Problem]
) -> str | None:
    value = state.get(key)
    if isinstance(value, str) and value:
        return value
    if value is not None:
        found = "empty text" if isinstance(value, str) else type(value).__name__
        problems.append(
            Problem(path=str(path), what=f"field {key}", reason=f"expected text, found {found}")
        )
    return default


def _step_status(state: dict[str, Any], path: Path, problems: list[Problem]) -> dict[str, str]:
    raw = state.get("step_status")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        problems.append(
            Problem(
                path=str(path),
                what="field step_status",
                reason=f"expected an object, found {type(raw).__name__}",
            )
        )
        return {}
    result: dict[str, str] = {}
    wrong: list[str] = []
    for step, status in raw.items():
        if isinstance(status, str):
            result[str(step)[:MAX_NAME_CHARS]] = status[:MAX_NAME_CHARS]
        else:
            wrong.append(f"{str(step)[:MAX_NAME_CHARS]} ({type(status).__name__})")
    # One file must not flood the snapshot: a few entries are named, the rest are counted.
    for entry in wrong[:MAX_FIELD_PROBLEMS]:
        step_name, _, found = entry.rpartition(" (")
        problems.append(
            Problem(
                path=str(path),
                what=f"field step_status.{step_name}",
                reason=f"expected text, found {found.rstrip(')')}",
            )
        )
    if len(wrong) > MAX_FIELD_PROBLEMS:
        problems.append(
            Problem(
                path=str(path),
                what="field step_status",
                reason=f"{len(wrong) - MAX_FIELD_PROBLEMS} more step(s) have a status that is "
                "not text",
            )
        )
    return result


def _current_step(state: dict[str, Any], step_status: dict[str, str]) -> str | None:
    """First step whose status is not ``done``; the order comes from ``steps`` when present."""
    order: list[str] = []
    steps = state.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict) and isinstance(step.get("id"), str):
                order.append(step["id"])
    order.extend(step for step in step_status if step not in order)
    for step in order:
        if step_status.get(step, "pending") != "done":
            return step
    return None


def _load_state(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Return (state, None) or (None, reason). Never raises for a bad file."""
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return None, "not a regular file"
        if os.path.getsize(path) > MAX_STATE_BYTES:
            return None, f"larger than {MAX_STATE_BYTES} bytes"
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        return None, f"cannot be read as JSON: {exc}"
    if not isinstance(loaded, dict):
        return None, f"expected a JSON object, found {type(loaded).__name__}"
    return loaded, None


def _read_run(
    run_dir: Path, store: Path, project: Project, now: datetime, problems: list[Problem]
) -> Run:
    state_path = run_dir / STATE_NAME
    state, reason = _load_state(state_path)
    # Events, gates and usage live in their own files: they are read even when the state
    # file is corrupt, so a broken run still says what it can.
    event_count, last_event, event_sources = read_events(run_dir, problems)
    pending_gates, gate_sources = read_pending_gates(run_dir, now, problems)
    cost, cost_sources = read_cost(run_dir, problems)
    detail_sources = [*event_sources, *gate_sources, *cost_sources]
    if state is None:
        problems.append(Problem(path=str(state_path), what="run state", reason=reason or ""))
        return Run(
            run_id=run_dir.name,
            store_path=str(store),
            project_id=project.id,
            readable=False,
            last_event=last_event,
            event_count=event_count,
            pending_gates=pending_gates,
            cost=cost,
            sources=[Source(kind="file", path=str(state_path)), *detail_sources],
        )
    # The run id is the directory name (that is how the engine addresses a run). The value
    # stored inside the file is untrusted text and must never be used to build a path.
    stored_id = _text(state, "run_id", None, state_path, problems)
    if stored_id is not None and stored_id != run_dir.name:
        problems.append(
            Problem(
                path=str(state_path),
                what="field run_id",
                reason=f"the file names the run {stored_id!r} but its directory is "
                f"{run_dir.name!r}; the directory name is used",
            )
        )
    step_status = _step_status(state, state_path, problems)
    return Run(
        run_id=run_dir.name,
        store_path=str(store),
        project_id=project.id,
        readable=True,
        mode=_text(state, "mode", UNKNOWN, state_path, problems) or UNKNOWN,
        tier=_text(state, "tier", UNKNOWN, state_path, problems) or UNKNOWN,
        status=_text(state, "status", UNKNOWN, state_path, problems) or UNKNOWN,
        step_status=step_status,
        current_step=_current_step(state, step_status),
        created_at=_text(state, "created_at", None, state_path, problems),
        updated_at=_text(state, "updated_at", None, state_path, problems),
        unknown_fields=sorted(
            str(key)[:MAX_NAME_CHARS] for key in state if key not in KNOWN_STATE_KEYS
        ),
        quality=read_quality(state, state_path, problems),
        last_event=last_event,
        event_count=event_count,
        pending_gates=pending_gates,
        cost=cost,
        sources=[
            *(
                Source(kind="file", path=str(state_path), field=key)
                for key in (*_COPIED_KEYS, "steps", "quality")
                if key in state
            ),
            *detail_sources,
        ],
    )


def _mark_duplicates(runs: list[Run]) -> None:
    """Each copy of a run id found in several stores lists the OTHER stores."""
    stores: dict[str, list[str]] = {}
    for run in runs:
        stores.setdefault(run.run_id, []).append(run.store_path)
    for run in runs:
        run.duplicate_of = sorted(s for s in stores[run.run_id] if s != run.store_path)


def distinct_runs(runs: list[Run]) -> list[Run]:
    """One copy per run id: the readable copy that moved last (greatest ``updated_at``)."""
    best: dict[str, Run] = {}

    def rank(run: Run) -> tuple[bool, datetime, str]:
        moved = parse_time(run.updated_at) or datetime.min.replace(tzinfo=UTC)
        return (run.readable, moved, run.store_path)

    for run in runs:
        if run.run_id not in best or rank(run) > rank(best[run.run_id]):
            best[run.run_id] = run
    return list(best.values())


def _has_state(run_dir: Path, problems: list[Problem]) -> bool:
    """Whether ``run_dir`` holds a ``state.json``; an unreadable directory is a problem."""
    try:
        os.lstat(run_dir / STATE_NAME)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        problems.append(
            Problem(path=str(run_dir), what="run store entry", reason=f"unreadable: {exc}")
        )
        return False
    return True


def collect_runs(
    projects: list[Project], *, now: datetime | None = None
) -> tuple[list[Run], list[Problem]]:
    """Every run of every project's ``.hw`` store, plus what could not be read.

    A run is a direct child directory of the store that contains ``state.json``. Everything
    else at that level (lock files, ``observability``, ``proposals``, loose scripts) is not a
    run and not a problem. Nothing below a run directory is listed, so
    ``.hw/<run>/worktrees/`` is never entered. Symbolic links are not followed; a linked store
    or run directory is reported as a problem.
    """
    runs: list[Run] = []
    problems: list[Problem] = []
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    for project in projects:
        root = Path(project.path)
        if is_inside_run_store(root) or secret_part(root) is not None:
            continue
        store = root / STORE_NAME
        if os.path.islink(store):
            problems.append(
                Problem(path=str(store), what="run store", reason="symbolic link; not followed")
            )
            continue
        if not os.path.isdir(store):
            continue
        try:
            with os.scandir(store) as it:
                children = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            problems.append(
                Problem(path=str(store), what="run store", reason=f"cannot be listed: {exc}")
            )
            continue
        for child in children:
            try:
                if child.is_symlink():
                    if os.path.lexists(os.path.join(child.path, STATE_NAME)):
                        problems.append(
                            Problem(
                                path=child.path,
                                what="run store entry",
                                reason="symbolic link to a run; links are not followed",
                            )
                        )
                    continue
                if not child.is_dir(follow_symlinks=False):
                    continue
            except OSError as exc:
                problems.append(
                    Problem(path=child.path, what="run store entry", reason=f"unreadable: {exc}")
                )
                continue
            run_dir = Path(child.path)
            if _has_state(run_dir, problems):
                runs.append(_read_run(run_dir, store, project, moment, problems))
    runs.sort(key=lambda r: (r.store_path, r.run_id))
    _mark_duplicates(runs)
    return runs, problems
