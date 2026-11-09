"""Assemble the snapshot and write ``snapshot.json`` and ``index.html`` outside the workspace.

This is the only module of the package that writes, and only into the output directory.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from hyperwilfred_core import __version__
from hyperwilfred_core.command_center import render
from hyperwilfred_core.command_center.attention import derive_attention
from hyperwilfred_core.command_center.collect_closed import mark_closed, read_register
from hyperwilfred_core.command_center.collect_memory import collect_memory
from hyperwilfred_core.command_center.collect_ops import collect_ops
from hyperwilfred_core.command_center.collect_projects import collect_projects
from hyperwilfred_core.command_center.collect_runs import collect_runs, distinct_runs
from hyperwilfred_core.command_center.gitread import run_git
from hyperwilfred_core.command_center.model import Generator, Problem, Snapshot, Totals
from hyperwilfred_core.command_center.paths import canonical, is_inside, secret_part

SNAPSHOT_NAME = "snapshot.json"
# Written by a person, read here: runs the owner closed (see ``collect_closed``).
CLOSED_RUNS_NAME = "closed-runs.json"


class CommandCenterError(ValueError):
    """An invalid argument to a command-center command (bad workspace or output directory)."""


def default_out_dir() -> Path:
    """Default D-S2-e: output lives outside every repository."""
    return Path.home() / "Library" / "Application Support" / "Wilfred" / "command-center"


def default_memory_dir() -> Path:
    """The shared memory tree read for the memory summary."""
    return Path.home() / ".agents" / "memory"


def default_ops_bin() -> Path:
    """The owner's ``ops`` command, run only when the workspace holds an ``ops`` folder."""
    return Path.home() / ".local" / "bin" / "ops"


def _ops_bin(path: Path | None) -> Path:
    """Absolute, links NOT resolved: the program runs by the path the owner gave."""
    chosen = path if path is not None else default_ops_bin()
    return Path(os.path.abspath(os.path.expanduser(chosen)))


def _memory_dir(path: Path | None) -> Path:
    memory = canonical(path if path is not None else default_memory_dir())
    secret = secret_part(memory)
    if secret is not None:
        raise CommandCenterError(
            f"memory directory {memory} lies under a directory named {secret!r}, "
            "which is never read"
        )
    return memory


def _workspace(path: Path) -> Path:
    workspace = canonical(path)
    if not os.path.isdir(workspace):
        raise CommandCenterError(f"workspace is not a directory: {workspace}")
    secret = secret_part(workspace)
    if secret is not None:
        raise CommandCenterError(
            f"workspace {workspace} lies under a directory named {secret!r}, which is never read"
        )
    return workspace


def _product_commit() -> str | None:
    """Commit of the product checkout this code runs from, or None for an installed wheel."""
    root = Path(__file__).resolve().parents[3]
    if not os.path.lexists(root / ".git"):
        return None
    result = run_git(root, ["rev-parse", "--verify", "--quiet", "HEAD"])
    return result.stdout.strip() or None if result.ok else None


def build_snapshot(
    workspace: Path,
    *,
    memory_dir: Path | None = None,
    now: datetime | None = None,
    closed_runs: Path | None = None,
    ops_bin: Path | None = None,
) -> Snapshot:
    """Collect projects, runs, the memory summary and the ops inbox data. Reads only.

    ``closed_runs`` is the closed-runs register; None reads no register (nothing is closed).
    """
    workspace = _workspace(workspace)
    memory_root = _memory_dir(memory_dir)
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    projects, problems = collect_projects(workspace)
    runs, run_problems = collect_runs(projects, now=moment)
    closed_problems: list[Problem] = []
    if closed_runs is not None:
        marks, closed_problems = read_register(closed_runs)
        mark_closed(runs, marks)
    memory, memory_problems = collect_memory(memory_root)
    ops, ops_problems = collect_ops(workspace, _ops_bin(ops_bin))
    problems = [*problems, *run_problems, *closed_problems, *memory_problems, *ops_problems]
    distinct = distinct_runs(runs)
    snapshot = Snapshot(
        generated_at=moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        workspace=str(workspace),
        generator=Generator(wilfred_version=__version__, product_commit=_product_commit()),
        projects=projects,
        runs=runs,
        memory=memory,
        ops=ops,
        problems=problems,
        totals=Totals(
            projects=len(projects),
            repositories=sum(p.kind == "repository" for p in projects),
            runs=len(distinct),
            run_copies=len(runs),
            # One status per distinct run: that of the copy that moved last.
            runs_by_status=dict(sorted(Counter(r.status for r in distinct).items())),
            problems=len(problems),
        ),
    )
    # Derived from the snapshot alone, at the same moment the facts were collected.
    snapshot.attention = derive_attention(snapshot, moment)
    return snapshot


def _scrub(value: Any) -> Any:
    """Replace text that cannot be written as UTF-8 (lone surrogates from files or names)."""
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, dict):
        return {_scrub(key): _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def serialise(snapshot: Snapshot) -> str:
    """The snapshot as JSON text. Untrusted text never makes the write fail."""
    return json.dumps(_scrub(snapshot.model_dump(mode="python")), indent=2, ensure_ascii=False)


def _write_atomic(out_dir: Path, name: str, text: str) -> Path:
    """Write ``out_dir/name`` atomically (temporary file, fsync, rename). Mode 600 via mkstemp."""
    target = out_dir / name
    fd, temporary = tempfile.mkstemp(dir=out_dir, prefix=f".{name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        if os.path.lexists(temporary):
            os.unlink(temporary)
        raise
    return target


def _ensure_out_dir(out_dir: Path) -> Path:
    out_dir = canonical(out_dir)
    if os.path.lexists(out_dir) and not os.path.isdir(out_dir):
        raise CommandCenterError(f"output path is not a directory: {out_dir}")
    if not os.path.isdir(out_dir):
        out_dir.mkdir(mode=0o700, parents=True)
        os.chmod(out_dir, 0o700)  # mkdir's mode is narrowed by the umask, never widened
    return out_dir


def write_snapshot(snapshot: Snapshot, out_dir: Path) -> Path:
    """Write ``snapshot.json`` atomically into ``out_dir`` (created with mode 700).

    The caller is responsible for ``out_dir`` being outside the workspace (``run_snapshot``
    checks it). An existing directory is used as it is; its permissions are not changed.
    """
    out_dir = _ensure_out_dir(out_dir)
    return _write_atomic(out_dir, SNAPSHOT_NAME, serialise(snapshot) + "\n")


def write_page(page: str, out_dir: Path) -> Path:
    """Write ``index.html`` atomically into ``out_dir``, next to ``snapshot.json``."""
    out_dir = _ensure_out_dir(out_dir)
    return _write_atomic(out_dir, render.PAGE_NAME, _scrub(page))


def _refuse_repository(target_dir: Path) -> None:
    """Default D-S2-e: the output never goes inside a git repository, wherever it is."""
    for candidate in (target_dir, *target_dir.parents):
        if os.path.lexists(candidate / ".git"):
            raise CommandCenterError(
                f"output directory {target_dir} is inside the repository {candidate}; "
                "the command center never writes inside a repository"
            )


def _refuse_inside(target_dir: Path, workspace: Path) -> None:
    if is_inside(target_dir, workspace):
        raise CommandCenterError(
            f"output directory {target_dir} is inside the workspace {workspace}; "
            "the command center never writes under the workspace"
        )
    _refuse_repository(target_dir)


def _refuse_projects(target_dir: Path, snapshot: Snapshot) -> None:
    for project in snapshot.projects:
        # A worktree can live outside the workspace, and its directory may be missing.
        for owned in (project.path, *(w.path for w in project.worktrees)):
            if is_inside(target_dir, Path(owned)):
                raise CommandCenterError(
                    f"output directory {target_dir} is inside the project {owned}; "
                    "the command center never writes inside a repository"
                )


def run_snapshot(
    *,
    workspace: Path,
    out_dir: Path | None = None,
    memory_dir: Path | None = None,
    ops_bin: Path | None = None,
) -> dict[str, Any]:
    """The ``hw command-center snapshot`` action: collect, write, and summarise."""
    workspace = _workspace(workspace)
    memory_root = _memory_dir(memory_dir)
    target_dir = canonical(out_dir if out_dir is not None else default_out_dir())
    if is_inside(target_dir, memory_root):
        raise CommandCenterError(
            f"output directory {target_dir} is inside the memory directory {memory_root}; "
            "the command center never writes where it reads"
        )
    _refuse_inside(target_dir, workspace)
    snapshot = build_snapshot(
        workspace,
        memory_dir=memory_root,
        closed_runs=target_dir / CLOSED_RUNS_NAME,
        ops_bin=ops_bin,
    )
    _refuse_projects(target_dir, snapshot)
    try:
        written = write_snapshot(snapshot, target_dir)
    except OSError as exc:
        raise CommandCenterError(f"snapshot could not be written to {target_dir}: {exc}") from exc
    return {
        "snapshot": str(written),
        "totals": _scrub(snapshot.totals.model_dump(mode="json")),
        "problems": len(snapshot.problems),
    }


def load_snapshot(out_dir: Path) -> tuple[Snapshot, Path]:
    """Read and validate ``<out_dir>/snapshot.json``. Missing or invalid -> CommandCenterError."""
    source = canonical(out_dir) / SNAPSHOT_NAME
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CommandCenterError(
            f"no snapshot at {source}; run `hw command-center snapshot` or `build` first"
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise CommandCenterError(f"snapshot {source} could not be read: {exc}") from exc
    try:
        return Snapshot.model_validate_json(text), source
    except ValidationError as exc:
        reason = exc.errors()[0]["msg"] if exc.errors() else str(exc)
        raise CommandCenterError(
            f"snapshot {source} is not a valid command-center snapshot ({exc.error_count()} "
            f"errors, first: {reason})"
        ) from exc


def run_render(*, out_dir: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The ``hw command-center render`` action: snapshot.json -> index.html in the same place."""
    target_dir = canonical(out_dir if out_dir is not None else default_out_dir())
    snapshot, source = load_snapshot(target_dir)
    # The snapshot names its workspace and projects; never write inside any of them.
    _refuse_inside(target_dir, canonical(snapshot.workspace))
    _refuse_projects(target_dir, snapshot)
    refresh: list[str] = []
    if target_dir != canonical(default_out_dir()):
        refresh += ["--out-dir", str(target_dir)]
    if snapshot.memory is not None:
        memory_root = canonical(snapshot.memory.root)
        if is_inside(target_dir, memory_root):
            raise CommandCenterError(
                f"output directory {target_dir} is inside the memory directory {memory_root}; "
                "the command center never writes where it reads"
            )
        if memory_root != canonical(default_memory_dir()):
            refresh += ["--memory", str(memory_root)]
    if snapshot.ops is not None and snapshot.ops.bin != str(_ops_bin(None)):
        refresh += ["--ops-bin", snapshot.ops.bin]
    try:
        page = render.render_page(
            snapshot, now=now, snapshot_path=str(source), refresh_args=refresh
        )
    except (ValueError, OverflowError) as exc:  # hostile values in a hand-edited snapshot
        raise CommandCenterError(f"page could not be rendered from {source}: {exc}") from exc
    try:
        written = write_page(page, target_dir)
    except OSError as exc:
        raise CommandCenterError(f"page could not be written to {target_dir}: {exc}") from exc
    return {"snapshot": str(source), "page": str(written)}


def run_build(
    *,
    workspace: Path,
    out_dir: Path | None = None,
    memory_dir: Path | None = None,
    ops_bin: Path | None = None,
) -> dict[str, Any]:
    """The ``hw command-center build`` action: ``snapshot`` then ``render``."""
    collected = run_snapshot(
        workspace=workspace, out_dir=out_dir, memory_dir=memory_dir, ops_bin=ops_bin
    )
    target_dir = Path(collected["snapshot"]).parent
    rendered = run_render(out_dir=target_dir)
    return {**collected, "page": rendered["page"]}
