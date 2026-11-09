"""Collect repositories, branches and worktrees of a workspace (discovery rules 1 and 2).

Read-only: directories are listed, git is read through the allowlist in ``gitread``. Nothing
is created, modified or deleted. Anything that cannot be read becomes a ``Problem``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from hyperwilfred_core.command_center.gitconfig import (
    SAFE_FILTER_NAME,
    filter_names,
    user_config_files,
)
from hyperwilfred_core.command_center.gitread import (
    LOG_FORMAT,
    REFS_FORMAT,
    GitResult,
    run_git,
    strip_credentials,
)
from hyperwilfred_core.command_center.model import Problem, Project, Remote, Source, Worktree
from hyperwilfred_core.command_center.paths import (
    SECRET_DIR_NAMES,
    canonical,
    is_inside_run_store,
    same_file,
    secret_part,
)

MAX_DEPTH = 3
# Directory names never entered (specification section 6 rule 1). The second group stands for
# "any path containing /_backups/, /received/, /package/ or /.hw/".
_SKIP_NAMES = frozenset({"node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".git"})
_SKIP_PATH_PARTS = frozenset({"_backups", "received", "package", ".hw"})


@dataclass
class _Entry:
    path: Path
    kind: str  # "repository" | "worktree" | "folder"
    owner: Path | None = None  # for a worktree: the repository that owns it
    git_dir: Path | None = None
    common_dir: Path | None = None
    project: Project | None = None
    sources: list[Source] = field(default_factory=list)


def project_id(path: Path, workspace: Path) -> str:
    """Stable id: the path relative to the workspace with ``/`` replaced by ``__``."""
    try:
        rel = path.relative_to(workspace).as_posix()
    except ValueError:
        rel = path.as_posix()  # a worktree that lives outside the workspace
    return rel.replace("/", "__")


def _discover(workspace: Path, problems: list[Problem]) -> list[Path]:
    """Directories down to ``MAX_DEPTH`` that contain a ``.git`` directory or file.

    Symbolic links are not followed, so the walk can never leave the workspace; a link that
    points at a repository is reported as a problem instead of being skipped silently.
    """
    found: list[Path] = []

    def walk(directory: Path, depth: int) -> None:
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            problems.append(
                Problem(path=str(directory), what="directory", reason=f"cannot be listed: {exc}")
            )
            return
        for entry in entries:
            name = entry.name
            if name in _SKIP_NAMES or name in _SKIP_PATH_PARTS or name in SECRET_DIR_NAMES:
                continue
            try:
                if entry.is_symlink():
                    if os.path.lexists(os.path.join(entry.path, ".git")):
                        problems.append(
                            Problem(
                                path=entry.path,
                                what="repository",
                                reason="symbolic link to a repository; links are not followed",
                            )
                        )
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError as exc:
                problems.append(
                    Problem(path=entry.path, what="directory entry", reason=f"unreadable: {exc}")
                )
                continue
            child = Path(entry.path)
            if os.path.lexists(child / ".git"):
                found.append(child)
            if depth < MAX_DEPTH:
                walk(child, depth + 1)

    walk(workspace, 1)
    return found


def _git_problem(problems: list[Problem], path: Path, result: GitResult) -> None:
    problems.append(Problem(path=str(path), what=result.command, reason=result.failure_reason))


def _classify(path: Path, problems: list[Problem]) -> _Entry:
    """Decide whether ``path`` is a repository, a linked worktree, or neither."""
    result = run_git(
        path,
        ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--show-toplevel"],
    )
    lines = result.stdout.splitlines()
    if not result.ok or len(lines) != 3:
        reason = "unexpected output from git rev-parse" if result.ok else result.failure_reason
        problems.append(Problem(path=str(path), what=result.command, reason=reason))
        return _Entry(path=path, kind="folder")
    git_dir, common_dir, toplevel = (Path(os.path.realpath(line)) for line in lines)
    if toplevel != path and not same_file(toplevel, path):
        # The ``.git`` here is not a usable repository: git answered for an enclosing one.
        problems.append(
            Problem(
                path=str(path),
                what="repository",
                reason=f"contains .git but git resolves it to {toplevel}",
            )
        )
        return _Entry(path=path, kind="folder")
    source = Source(kind="git", path=str(path), field="kind", command=result.command)
    if git_dir == common_dir:
        return _Entry(
            path=path, kind="repository", git_dir=git_dir, common_dir=common_dir, sources=[source]
        )
    owner = canonical(common_dir.parent) if common_dir.name == ".git" else None
    return _Entry(
        path=path,
        kind="worktree",
        owner=owner,
        git_dir=git_dir,
        common_dir=common_dir,
        sources=[source],
    )


def _parse_status(text: str) -> tuple[str | None, int | None, int | None, int]:
    """Parse ``git status --porcelain=v2 --branch``.

    Returns (branch name or None when detached, ahead, behind, number of uncommitted paths).
    """
    branch: str | None = None
    ahead: int | None = None
    behind: int | None = None
    changed = 0
    for line in text.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):]
            branch = None if head == "(detached)" else head
        elif line.startswith("# branch.ab "):
            parts = line.split()
            try:
                ahead, behind = int(parts[2].lstrip("+")), int(parts[3].lstrip("-"))
            except (IndexError, ValueError):
                ahead = behind = None
        elif line and not line.startswith("#"):
            changed += 1
    return branch, ahead, behind, changed


def _parse_worktrees(text: str, repository: Path) -> list[Worktree]:
    """Parse ``git worktree list --porcelain``; the repository's own entry is left out."""
    worktrees: list[Worktree] = []
    for block in text.split("\n\n"):
        path: str | None = None
        head: str | None = None
        branch: str | None = None
        detached = prunable = bare = False
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                path = value
            elif key == "HEAD":
                head = value
            elif key == "branch":
                branch = value.removeprefix("refs/heads/")
            elif key == "detached":
                detached = True
            elif key == "prunable":
                prunable = True
            elif key == "bare":
                bare = True
        if path is None or bare or path == str(repository) or same_file(path, repository):
            continue
        worktrees.append(
            Worktree(
                path=path,
                branch=branch,
                head=head,
                detached=detached,
                prunable=prunable,
                exists=os.path.isdir(path),
                inside_run_store=is_inside_run_store(path),
            )
        )
    return worktrees


def _filters_to_disable(entry: _Entry, problems: list[Problem]) -> list[str] | None:
    """Content filters ``git status`` would otherwise RUN in this project.

    Returns None (and records a problem) when they cannot all be identified and switched off;
    the caller then does not run ``status`` at all, so nothing can be executed.
    """
    configs = user_config_files()
    if entry.common_dir is not None:
        configs.append(entry.common_dir / "config")
    if entry.git_dir is not None:
        configs.append(entry.git_dir / "config.worktree")
    names, unverified = filter_names(configs)
    unsafe = sorted(n for n in names if not SAFE_FILTER_NAME.match(n))
    if unverified or unsafe:
        unsafe_notes = [f"filter name {n!r} cannot be disabled" for n in unsafe]
        detail = "; ".join([*unverified, *unsafe_notes])
        problems.append(
            Problem(
                path=str(entry.path),
                what="git status",
                reason="not run, so uncommitted paths and the branch are unknown: the content "
                f"filters git would execute could not be verified ({detail})",
            )
        )
        return None
    return sorted(names)


def _describe(entry: _Entry, workspace: Path, problems: list[Problem]) -> Project:
    """Read every git fact of one repository or worktree."""
    path = entry.path
    sources = list(entry.sources)

    def read(
        args: list[str],
        fields: list[str],
        *,
        expected: tuple[int, ...] = (0,),
        disable_filters: list[str] | None = None,
    ) -> GitResult:
        result = run_git(path, args, disable_filters=disable_filters or ())
        if result.returncode in expected and not result.timed_out and result.error is None:
            sources.extend(
                Source(kind="git", path=str(path), field=f, command=result.command) for f in fields
            )
        else:
            _git_problem(problems, path, result)
        return result

    project = Project(
        id=project_id(path, workspace),
        name=path.name,
        path=str(path),
        kind=entry.kind,  # type: ignore[arg-type]
    )

    # Exit code 1 with --quiet means "no commit yet", which is a fact, not a failure.
    head = read(
        ["rev-parse", "--verify", "--quiet", "HEAD"], ["head", "has_commits"], expected=(0, 1)
    )
    if head.returncode == 0 and head.stdout.strip():
        project.head = head.stdout.strip()
        project.has_commits = True
    elif head.returncode == 1 and not head.timed_out and head.error is None:
        project.has_commits = False

    filters = _filters_to_disable(entry, problems)
    if filters is not None:
        # A submodule's own status would run in a child process with its own filters, so only
        # the recorded commit of a submodule is compared, never its working tree.
        status = read(
            ["status", "--porcelain=v2", "--branch", "--ignore-submodules=dirty"],
            ["branch", "ahead", "behind", "uncommitted_paths"],
            disable_filters=filters,
        )
        if status.ok:
            branch, ahead, behind, changed = _parse_status(status.stdout)
            # Specification section 5: the branch is null when detached or without commits.
            project.branch = branch if project.has_commits else None
            project.ahead, project.behind = ahead, behind
            project.uncommitted_paths = changed

    refs = read(
        ["for-each-ref", REFS_FORMAT, "refs/heads"],
        ["local_branches", "branches_without_upstream"],
    )
    if refs.ok:
        rows = [line.split("\t") for line in refs.stdout.splitlines() if line]
        project.local_branches = len(rows)
        project.branches_without_upstream = sorted(
            row[0] for row in rows if len(row) < 2 or not row[1]
        )

    remotes = read(["remote", "-v"], ["remotes"])
    if remotes.ok:
        seen: dict[str, str] = {}
        for line in remotes.stdout.splitlines():
            name, _, rest = line.partition("\t")
            url = rest.rsplit(" (", 1)[0]
            if name and url and name not in seen:
                seen[name] = strip_credentials(url)
        project.remotes = [Remote(name=n, url=u) for n, u in seen.items()]

    if project.has_commits:
        last = read(["log", "-1", LOG_FORMAT], ["last_commit_at"])
        if last.ok and last.stdout.strip():
            project.last_commit_at = last.stdout.strip()

    if entry.kind == "repository":
        listed = read(["worktree", "list", "--porcelain"], ["worktrees"])
        if listed.ok:
            project.worktrees = _parse_worktrees(listed.stdout, path)

    project.sources = sources
    return project


def _nearest_repository(path: Path, repositories: dict[Path, Project]) -> str | None:
    for parent in path.parents:
        if parent in repositories:
            return repositories[parent].id
    return None


def collect_projects(workspace: Path) -> tuple[list[Project], list[Problem]]:
    """Every repository and worktree of ``workspace``, plus what could not be read.

    ``workspace`` must be an existing directory. It is always the first project: a
    ``repository`` when it is one, otherwise a ``folder`` (so that its own run store is seen).
    """
    workspace = canonical(workspace)
    problems: list[Problem] = []
    entries: dict[Path, _Entry] = {}

    if os.path.lexists(workspace / ".git"):
        entries[workspace] = _classify(workspace, problems)
    else:
        entries[workspace] = _Entry(path=workspace, kind="folder")
    for candidate in _discover(workspace, problems):
        if candidate not in entries:
            entries[candidate] = _classify(candidate, problems)

    # Repositories first: their worktree lists reveal worktrees the walk did not reach.
    for entry in [e for e in entries.values() if e.kind == "repository"]:
        entry.project = _describe(entry, workspace, problems)
        for worktree in entry.project.worktrees:
            if not worktree.exists or worktree.inside_run_store:
                continue
            real = canonical(worktree.path)
            if real in entries:
                continue
            secret = secret_part(real)
            if secret is not None:
                problems.append(
                    Problem(
                        path=str(real),
                        what="worktree",
                        reason=f"not read: it lies under a directory named {secret!r}",
                    )
                )
                continue
            entries[real] = _classify(real, problems)
    for entry in entries.values():
        if entry.project is not None:
            continue
        if entry.kind == "worktree":
            entry.project = _describe(entry, workspace, problems)
        else:
            entry.project = Project(
                id=project_id(entry.path, workspace),
                name=entry.path.name,
                path=str(entry.path),
                kind="folder",
                sources=[Source(kind="file", path=str(entry.path), field="kind")],
            )

    repositories = {
        e.path: e.project for e in entries.values() if e.kind == "repository" and e.project
    }
    ordered = sorted(
        entries.values(), key=lambda e: (e.path != workspace, e.project.id if e.project else "")
    )
    used_ids: dict[str, str] = {}
    for entry in ordered:
        project = entry.project
        assert project is not None
        if project.id in used_ids:
            # Two paths can map to one id ("a__b" and "a/b"). Keep ids unique, and say so.
            base, number = project.id, 2
            while f"{base}~{number}" in used_ids:
                number += 1
            project.id = f"{base}~{number}"
            problems.append(
                Problem(
                    path=project.path,
                    what="project id",
                    reason=f"id {base!r} is already used by {used_ids[base]}; "
                    f"this project is listed as {project.id!r}",
                )
            )
        used_ids[project.id] = project.path
    for entry in ordered:
        project = entry.project
        assert project is not None
        if entry.kind == "worktree":
            owner = repositories.get(entry.owner) if entry.owner is not None else None
            project.parent_repository = owner.id if owner is not None else None
        else:
            project.parent_repository = _nearest_repository(entry.path, repositories)

    return [e.project for e in ordered if e.project is not None], problems
