"""The engine-level guardrail floor — the defensible moat.

Forked and cleaned from the Phase-0 spike (``spike/engine.py``), promoted from throwaway
to real engine code. This module is the **authoritative acceptance gate**: at acceptance
time it compares a worker's changed paths against a machine-readable allow-list and rejects
any escape; it rejects any diff that looks like a full-file regeneration; and on any
rejection it rolls the worktree back to a byte-clean HEAD.

Why the guarantee lives HERE, not in the worker:
* On Claude Code a patcher can be tool-locked (no Write tool), but the BUILD workers DO
  write — so the engine, not the absence of a tool, is what bounds them.
* On Codex ``apply_patch`` can replace a whole file, so the engine MUST reject a wholesale
  rewrite. The Phase-0 spike proved a real Codex regeneration evaded a ratio-only check —
  so the guard uses BOTH a changed-lines budget AND a deletion-ratio signal.

Pure stdlib. Runtime-neutral: it only shells out to ``git``, so it behaves identically
whether the worker was a Claude Code ``Task`` sub-agent or a ``codex exec`` process. The
result is ALWAYS read from a known disk path, never from a worker's stdout (Issue #15451).
"""

from __future__ import annotations

import collections
import contextlib
import fnmatch
import hashlib
import itertools
import os
import posixpath
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from hyperwilfred_core.schemas import (
    AcceptanceVerdict,
    PatchViolation,
    is_denied_generated_path,
)


class GuardrailError(RuntimeError):
    """The worktree cannot be inspected, so the gate fails closed (surfaces as CLI exit 2)."""


# Every git read of the acceptance gate runs like ``regression._run_git``: no replacement refs,
# no inherited ``GIT_*`` variable, no user or system configuration, and none of the repository
# settings that let a worker answer for git. A worker-set ``core.fsmonitor`` hook would otherwise
# tell ``git status`` that nothing changed (hiding out-of-scope edits) and be executed by the
# engine; an untracked cache in the worker-written index could likewise vouch for directories.
_GIT = ("git", "--no-replace-objects", "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false")


def _git_env() -> dict[str, str]:
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("GIT_") and name != "LANGUAGE"}
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_CONFIG_NOSYSTEM="1", GIT_NO_LAZY_FETCH="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
               LC_ALL="C")
    return env


def _git(worktree: str, *args: str) -> str:
    """Run a git command inside *worktree* and refuse an inspection error."""
    clean_env = _git_env()
    out = subprocess.run(
        [*_GIT, "-C", worktree, *args],
        capture_output=True,
        text=True,
        check=False,
        env=clean_env,
    )
    if out.returncode:
        raise ValueError(f"cannot inspect acceptance worktree: git {args[0]} failed")
    return out.stdout


def _git_checked(worktree: str, *args: str, stdin: str | None = None) -> str:
    """Run a READ-side git command and fail closed on any error.

    A corrupt index, a rewritten ``.git`` file or any other git failure prints nothing and exits
    non-zero. Read as "no changes" that would ACCEPT a worker's tree unseen (the second half of
    threat-model finding F-15: ``require_worktree`` alone does not catch a worktree that answers
    ``rev-parse`` but cannot be listed).
    """
    clean_env = _git_env()
    out = subprocess.run(
        [*_GIT, "-C", worktree, *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=clean_env,
    )
    if out.returncode != 0:
        detail = out.stderr.strip() or f"exit {out.returncode}"
        raise GuardrailError(
            f"git {args[0]} failed in {worktree!r}: {detail} — the acceptance gate fails closed"
        )
    return out.stdout


def require_worktree(worktree: str) -> None:
    """Refuse to judge a folder git cannot report on.

    ``git status`` on a missing folder or a non-repository prints nothing and exits non-zero;
    read as "no changes" that would ACCEPT a packet without looking at any file (threat-model
    finding F-15). The gate therefore fails closed: an uninspectable worktree is an error, never
    an empty change set.
    """
    out = subprocess.run(
        [*_GIT, "-C", worktree, "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
        env=_git_env(),
    )
    if out.returncode != 0 or out.stdout.strip() != "true":
        detail = out.stderr.strip() or "git reported no work tree"
        raise GuardrailError(
            f"cannot inspect worktree {worktree!r}: {detail} — the acceptance gate fails closed"
        )


def require_related_worktree(repo_root: Path, worktree: str) -> str:
    """Return a git worktree root sharing this run repository's common git directory.

    A worker may use a linked worktree outside the run directory. Its path must still name
    the complete worktree root so status cannot hide changes outside a nested directory.
    """
    clean_env = _git_env()

    def identity(directory: Path) -> tuple[Path, Path]:
        result = subprocess.run(
            [*_GIT, "-C", str(directory), "rev-parse", "--show-toplevel",
             "--path-format=absolute", "--git-common-dir", "--is-inside-work-tree"],
            capture_output=True, text=True, check=False, env=clean_env,
        )
        lines = result.stdout.splitlines()
        if result.returncode or len(lines) != 3 or lines[2] != "true":
            raise ValueError("acceptance worktree is not a git worktree")
        return Path(lines[0]).resolve(), Path(lines[1]).resolve()

    root = Path(worktree).resolve()
    repo_top, repo_common = identity(repo_root.resolve())
    worktree_top, worktree_common = identity(root)
    if root != worktree_top:
        raise ValueError("acceptance worktree must name its git worktree root")
    if not repo_common.samefile(worktree_common):
        raise ValueError("acceptance worktree must belong to the same repository as the packet")
    return str(root)


def worktree_identity(
    repo_root: Path, worktree: str, base_commit: str | None,
) -> dict[str, str | int]:
    """Identify a live registered worktree of this repository, independent of its name."""
    bound = require_related_worktree(repo_root, worktree)
    listings = _git(str(repo_root), "worktree", "list", "--porcelain").split("\n\n")
    registered = False
    for block in listings:
        lines = block.splitlines()
        if lines and lines[0] == f"worktree {bound}" and "prunable" not in block:
            registered = True
            break
    if not registered:
        raise ValueError("acceptance worktree is not a live registered worktree")
    git_dir = Path(_git(bound, "rev-parse", "--absolute-git-dir").strip()).resolve()
    try:
        stat = git_dir.stat()
    except OSError as exc:
        raise ValueError("acceptance worktree git registration is missing") from exc
    if base_commit:
        _git(bound, "merge-base", "--is-ancestor", base_commit, "HEAD")
    try:
        root_stat = os.stat(bound)
    except OSError as exc:
        raise ValueError("acceptance worktree directory is missing") from exc
    return {
        "path": bound, "git_dir_device": stat.st_dev, "git_dir_inode": stat.st_ino,
        # The directory itself: another folder put at the same path and attached to the same
        # Git registration is a different checkout.
        "root_device": root_stat.st_dev, "root_inode": root_stat.st_ino,
    }


def worktree_pins(worktree: str) -> dict[str, str]:
    """What must not change between registration and acceptance for status to be the truth.

    ``git status`` only reports uncommitted, non-ignored, non-hidden differences. A commit made
    by the worker, a new local ignore rule, or an index entry flagged to be skipped would each
    hide out-of-scope work from the acceptance gate, so their state is pinned at registration.
    """
    head = _git(worktree, "rev-parse", "HEAD").strip()
    common = Path(_git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    try:
        exclude = (common / "info" / "exclude").read_bytes()
    except FileNotFoundError:
        exclude = b""
    except OSError as exc:
        raise ValueError("cannot inspect acceptance worktree: local ignore rules") from exc
    clean_env = _git_env()
    configured = subprocess.run(
        [*_GIT, "-C", worktree, "config", "--get-all", "core.excludesFile"],
        capture_output=True, text=True, check=False, env=clean_env,
    ).stdout
    hidden = "\n".join(
        line for line in _git(worktree, "ls-files", "-v").splitlines()
        if line[:1] == "S" or line[:1].islower()
    )
    return {
        "head": head,
        "ignore_rules_sha256": hashlib.sha256(exclude + b"\0" + configured.encode()).hexdigest(),
        "hidden_index_sha256": hashlib.sha256(hidden.encode()).hexdigest(),
    }


# ---- reading a worktree without trusting its git configuration ------------------------------
#
# The acceptance gate does not ask ``git status`` or ``git diff`` what changed: both honour
# configuration the worker can write (``.git/config``, ``core.attributesFile``, an included
# file, the personal ignore file). A worker-set ``core.trustctime=false`` + ``core.checkStat``
# let a same-size edit with a restored mtime pass as unchanged; a clean filter mapped new bytes
# back to the committed ones AND ran the worker's program inside the gate. Instead the gate
# hashes the bytes on disk itself and compares them with the HEAD tree and the index, which it
# reads as raw objects (no filter, no textconv, no stat cache). Ignore rules are the only git
# behaviour it still consults, and only to keep build output and virtual environments invisible:
# a file stays invisible only when the deciding rule comes from a ``.gitignore`` that is
# committed and byte-identical to HEAD, or from the repository's local exclude file (pinned at
# registration by :func:`worktree_pins`). Anything else ignored is a changed path.
#
# Repositories that rely on checkout filters (git-lfs, end-of-line conversion) therefore show
# every filtered file as changed and cannot be registered: that is the fail-closed side.

_READ_OVERRIDES = (
    "-c", f"core.excludesFile={os.devnull}", "-c", "core.ignoreCase=false",
    "-c", f"core.attributesFile={os.devnull}", "-c", "core.precomposeUnicode=false",
)
_TIMEOUT = 600  # seconds; a FIFO where git expects a file must not hang the gate
_REGULAR, _EXECUTABLE, _SYMLINK, _GITLINK = "100644", "100755", "120000", "160000"


@dataclass(frozen=True)
class _Entry:
    """One path as git would store it: a mode and an object id ("" when not computed)."""

    mode: str
    oid: str


@dataclass
class _Snapshot:
    root: Path
    object_format: str
    head: dict[str, _Entry]
    index: dict[str, _Entry]
    conflicted: set[str]
    disk: dict[str, Path]  # every non-directory entry on disk, by its exact relative name
    untracked_visible: list[str]
    changes: list[tuple[str, str]]  # (XY, path) in git-status style
    # For every changed path: its disk entry (None: absent) and the bytes judged, read once, so
    # the scope check, the patch budget and the record of accepted work see the same content.
    state: dict[str, _Entry | None]
    data: dict[str, bytes]


def _read_env() -> dict[str, str]:
    return _git_env()


def _raw(worktree: str | Path, *args: str, stdin: bytes | None = None,
         ok: tuple[int, ...] = (0,)) -> bytes:
    """A READ-side git call with the acceptance overrides; any other exit fails closed."""
    try:
        out = subprocess.run(
            [*_GIT, *_READ_OVERRIDES, "-C", str(worktree), *args],
            input=stdin, capture_output=True, check=False, env=_read_env(), timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise GuardrailError(f"git {args[0]} did not finish in {_TIMEOUT}s in {str(worktree)!r}"
                             " — the acceptance gate fails closed") from exc
    if out.returncode not in ok:
        detail = os.fsdecode(out.stderr).strip() or f"exit {out.returncode}"
        raise GuardrailError(
            f"git {args[0]} failed in {str(worktree)!r}: {detail} — the acceptance gate fails "
            "closed"
        )
    return out.stdout


def _hash_object(object_format: str, kind: bytes, data: bytes) -> str:
    digest = hashlib.new("sha256" if object_format == "sha256" else "sha1")
    digest.update(kind + b" " + str(len(data)).encode() + b"\0" + data)
    return digest.hexdigest()


def _read_disk(path: Path, object_format: str) -> tuple[_Entry, bytes | None]:
    """Mode, blob id and bytes of one disk entry, read once (never from a stat cache)."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(os.fsencode(path))
        return _Entry(_SYMLINK, _hash_object(object_format, b"blob", target)), target
    if not stat.S_ISREG(info.st_mode):
        return _Entry("special", ""), None
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        data = handle.read()
    mode = _EXECUTABLE if info.st_mode & stat.S_IXUSR else _REGULAR
    return _Entry(mode, _hash_object(object_format, b"blob", data)), data


def _disk_entry(path: Path, object_format: str) -> _Entry:
    return _read_disk(path, object_format)[0]


def _split_z(raw: bytes) -> list[str]:
    return [os.fsdecode(field) for field in raw.split(b"\0") if field]


def _head_entries(root: Path) -> dict[str, _Entry]:
    head = _raw(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", ok=(0, 1)).strip()
    if not head:
        # Only a branch with no commit yet is the empty tree. A HEAD that names an object git
        # cannot read must not turn every file into a new one (no original lines to budget).
        branch = _raw(root, "symbolic-ref", "-q", "HEAD", ok=(0, 1)).strip()
        if not branch or _raw(root, "for-each-ref", "--format=%(objectname)",
                              branch.decode()).strip():
            raise GuardrailError("HEAD cannot be read — the acceptance gate fails closed")
        return {}
    return _tree_listing(root, head.decode())


def _tree_listing(root: str | Path, commit: str) -> dict[str, _Entry]:
    entries: dict[str, _Entry] = {}
    for record in _split_z(_raw(root, "ls-tree", "-r", "-z", "--full-tree", commit)):
        meta, _, path = record.partition("\t")
        mode, _kind, oid = meta.split(" ")
        entries[path] = _Entry(mode, oid)
    return entries


def _index_entries(root: Path) -> tuple[dict[str, _Entry], set[str]]:
    entries: dict[str, _Entry] = {}
    conflicted: set[str] = set()
    for record in _split_z(_raw(root, "ls-files", "-s", "-z")):
        meta, _, path = record.partition("\t")
        mode, oid, stage = meta.split(" ")
        if stage != "0":
            conflicted.add(path)
        else:
            entries[path] = _Entry(mode, oid)
    return entries, conflicted


def _ignore_verdicts(root: Path, git_dir: Path, names: list[str]) -> list[tuple[str, str]]:
    """``(source, pattern)`` deciding each name (``("", "")`` when no rule matches)."""
    if not names:
        return []
    raw = _raw(root, f"--git-dir={git_dir}", f"--work-tree={root}", "check-ignore",
               "--no-index", "-n", "-v", "-z", "--stdin",
               # "./" keeps a name that starts with ":" from being read as pathspec magic.
               stdin=b"".join(b"./" + os.fsencode(name) + b"\0" for name in names), ok=(0, 1))
    fields = [os.fsdecode(field) for field in raw.split(b"\0")]
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) != 4 * len(names):
        raise GuardrailError("git check-ignore returned an unexpected listing — the acceptance "
                             "gate fails closed")
    return [(fields[at], fields[at + 2]) for at in range(0, len(fields), 4)]


def _inspect(worktree: str) -> _Snapshot:
    """Everything the worker changed relative to HEAD, judged from bytes on disk."""
    require_worktree(worktree)
    root = Path(worktree).resolve()
    top = Path(os.fsdecode(_raw(root, "rev-parse", "--show-toplevel").strip())).resolve()
    if top != root:
        # A worker-set core.worktree (or a subdirectory) would make git judge another folder.
        raise GuardrailError(f"{worktree!r} is not the root git reports for its work tree — "
                             "the acceptance gate fails closed")
    git_dir = Path(os.fsdecode(_raw(root, "rev-parse", "--absolute-git-dir").strip()))
    common = Path(os.fsdecode(_raw(
        root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()))
    object_format = os.fsdecode(_raw(root, "rev-parse", "--show-object-format").strip())
    head = _head_entries(root)
    index, conflicted = _index_entries(root)
    known = set(head) | set(index) | conflicted
    known_dirs = {parent for path in known for parent in _parents(path)}
    # Only a submodule committed at HEAD, with the same index entry, is judged as a submodule.
    # A gitlink the worker staged (``update-index --cacheinfo 160000``, ``git add`` of a nested
    # repository) is walked like any folder, so the files under it are seen.
    gitlinks = {path for path, entry in head.items()
                if entry.mode == _GITLINK and index.get(path) == entry}

    exclude_file = (common / "info" / "exclude").resolve()
    trust: dict[str, bool] = {}

    def trusted(source: str) -> bool:
        if not source:
            return False
        if source not in trust:
            source_path = Path(source)
            if source_path.is_absolute() or source.startswith(".git/"):
                candidate = source_path if source_path.is_absolute() else root / source_path
                trust[source] = candidate.resolve() == exclude_file
            else:
                # Exact, case-sensitive name: git reads ``app/.GITIGNORE`` as ``app/.gitignore``
                # on a case-insensitive disk, but only the committed spelling is trusted.
                committed = head.get(source)
                on_disk = disk.get(source)
                trust[source] = (
                    source.rsplit("/", 1)[-1] == ".gitignore" and committed is not None
                    and committed.mode in (_REGULAR, _EXECUTABLE) and on_disk is not None
                    and _disk_entry(on_disk, object_format) == committed)
        return trust[source]

    def invisible(verdict: tuple[str, str]) -> bool:
        source, pattern = verdict
        return trusted(source) and not pattern.startswith("!")

    # Walk the tree level by level so a directory ignored by a trusted rule (a virtual
    # environment, node_modules) is skipped in one check instead of file by file.
    disk: dict[str, Path] = {}
    untracked_dirs: list[str] = []
    level: list[str] = [""]
    while level:
        next_level: list[str] = []
        candidates: list[str] = []
        for directory in level:
            base = root / directory if directory else root
            with os.scandir(base) as listing:
                for item in listing:
                    name = f"{directory}/{item.name}" if directory else item.name
                    if not directory and item.name == ".git":
                        continue
                    _refuse_special_ignore_file(item, name)
                    if item.is_dir(follow_symlinks=False):
                        if name in gitlinks:
                            disk[name] = Path(item.path)
                        elif name in known_dirs:
                            next_level.append(name)
                        else:
                            # git reads this folder's own .gitignore while judging the folder.
                            with os.scandir(item.path) as inside:
                                for entry in inside:
                                    _refuse_special_ignore_file(entry, f"{name}/{entry.name}")
                            candidates.append(name)
                    else:
                        disk[name] = Path(item.path)
        # The trusted .gitignore files of this level are on disk now; prune ignored dirs.
        for name, verdict in zip(candidates, _ignore_verdicts(
                root, git_dir, [name + "/" for name in candidates]), strict=True):
            if not invisible(verdict):
                next_level.append(name)
                untracked_dirs.append(name)
        level = next_level

    untracked = sorted(path for path in disk if path not in known)
    untracked_visible = [
        path for path, verdict in zip(untracked, _ignore_verdicts(root, git_dir, untracked),
                                      strict=True)
        if not invisible(verdict)
    ]

    data: dict[str, bytes] = {}
    inside_submodules: dict[str, list[tuple[str, str]]] = {}

    def current(path: str) -> _Entry | None:
        location = disk.get(path)
        if location is None:
            return None
        if path in gitlinks and location.is_dir() and not location.is_symlink():
            if not any(location.iterdir()):
                return head.get(path)  # a submodule that was never initialised
            entry, inner = _gitlink_entry(location, head[path], object_format)
            inside_submodules[path] = inner
            return entry
        entry, content = _read_disk(location, object_format)
        if content is not None:
            data[path] = content
        return entry

    changes: list[tuple[str, str]] = []
    state: dict[str, _Entry | None] = {}
    for path in sorted(known):
        committed, staged = head.get(path), index.get(path)
        on_disk = current(path)
        if path in conflicted:
            changes.append(("UU", path))
            state[path] = on_disk
            continue
        x = " " if staged == committed else ("A" if committed is None else
                                             "D" if staged is None else "M")
        y = " " if on_disk == committed else ("D" if on_disk is None else "M")
        if x != " " or y != " ":
            changes.append((x + y, path))
            state[path] = on_disk
            # Files changed inside a committed submodule are named too, so a blocked glob
            # (``**/conftest.py``) applies to them.
            changes += [(xy, f"{path}/{inner}") for xy, inner in inside_submodules.get(path, [])]
        else:
            data.pop(path, None)
    for path in untracked_visible:
        changes.append(("??", path))
        state[path] = current(path)
    return _Snapshot(root=root, object_format=object_format, head=head, index=index,
                     conflicted=conflicted, disk=disk, untracked_visible=untracked_visible,
                     changes=changes, state=state, data=data)


def _refuse_special_ignore_file(item: os.DirEntry[str], name: str) -> None:
    """A FIFO or device named like an ignore file would block git's read forever."""
    if (item.name.lower() == ".gitignore" and not item.is_symlink()
            and not item.is_file(follow_symlinks=False)
            and not item.is_dir(follow_symlinks=False)):
        raise GuardrailError(f"{name!r} is not a regular file — the acceptance gate fails closed")


def _parents(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    return ["/".join(parts[: depth + 1]) for depth in range(len(parts))]


def _verified_object(location: Path, kind: str, oid: str, object_format: str) -> bytes:
    """One object from a store the worker can write, accepted only if it hashes to *oid*."""
    body = _raw(location, "cat-file", kind, oid)
    if _hash_object(object_format, kind.encode(), body) != oid:
        raise GuardrailError(f"object {oid} under {str(location)!r} does not match its id — the "
                             "acceptance gate fails closed")
    return body


def _verified_tree(location: Path, commit: str, object_format: str) -> dict[str, _Entry]:
    """The recorded commit's tree, every object re-hashed (a forged pack cannot lie)."""
    header = _verified_object(location, "commit", commit, object_format).split(b"\n", 1)[0]
    if not header.startswith(b"tree "):
        raise GuardrailError(f"commit {commit} has no tree — the acceptance gate fails closed")
    width = 32 if object_format == "sha256" else 20
    entries: dict[str, _Entry] = {}
    pending = [("", header[5:].decode())]
    while pending:
        prefix, tree = pending.pop()
        body = _verified_object(location, "tree", tree, object_format)
        at = 0
        while at < len(body):
            space = body.index(b" ", at)
            nul = body.index(b"\0", space)
            mode = body[at:space].decode()
            name = prefix + os.fsdecode(body[space + 1:nul])
            oid = body[nul + 1:nul + 1 + width].hex()
            at = nul + 1 + width
            if mode == "40000":
                pending.append((name + "/", oid))
            else:
                entries[name] = _Entry(mode, oid)
    return entries


def _gitlink_entry(
    location: Path, recorded: _Entry, object_format: str,
) -> tuple[_Entry, list[tuple[str, str]]]:
    """An initialised committed submodule, judged against the commit the PARENT records.

    Nothing the submodule's own repository says is trusted: not its HEAD (the worker can
    commit inside it), not its ignore or exclude files, not where its ``.git`` link points.
    Every file under the folder (except that root ``.git`` link) is compared byte for byte
    with the tree of the recorded commit, read by its content-addressed id; an extra file is
    a change. Nested submodules inside it are not supported and fail closed.
    """
    tree = _verified_tree(location, recorded.oid, object_format)
    if any(entry.mode == _GITLINK for entry in tree.values()):
        raise GuardrailError(f"nested submodule under {str(location)!r} — the acceptance gate "
                             "fails closed")
    found: dict[str, Path] = {}
    pending = [("", location)]
    while pending:
        prefix, folder = pending.pop()
        with os.scandir(folder) as listing:
            for item in listing:
                name = f"{prefix}{item.name}"
                if not prefix and item.name == ".git":
                    continue
                if item.is_dir(follow_symlinks=False):
                    pending.append((name + "/", Path(item.path)))
                else:
                    found[name] = Path(item.path)
    inner: list[tuple[str, str]] = []
    for name in sorted(set(tree) | set(found)):
        expected, place = tree.get(name), found.get(name)
        if place is None:
            inner.append((" D", name))
        elif expected is None:
            inner.append(("??", name))
        elif _disk_entry(place, object_format) != expected:
            inner.append((" M", name))
    return _Entry(_GITLINK, recorded.oid + ("+dirty" if inner else "")), inner


def _status_entries(worktree: str, *, strict: bool = True) -> list[tuple[str, str]]:
    """``(XY status, path)`` for every changed path, in ``git status --porcelain`` style.

    Computed by :func:`_inspect` (bytes on disk against the HEAD tree and the index), never by
    ``git status``. Each new file is listed individually. ``strict`` (the default, used by the
    acceptance gate) raises :class:`GuardrailError` when the tree cannot be inspected;
    ``strict=False`` raises ``ValueError`` instead, for read-only scope hints.
    """
    try:
        return _inspect(worktree).changes
    except GuardrailError as exc:
        if strict:
            raise
        raise ValueError(str(exc)) from exc


def changed_paths(worktree: str, *, strict: bool = True) -> list[str]:
    """Every path the worker touched (modified, added, deleted, untracked). See :func:`_inspect`."""
    return [p for _, p in _status_entries(worktree, strict=strict)]


def accepted_changes(worktree: str) -> dict[str, list[str] | None]:
    """What the worktree changed relative to HEAD, as ``{path: [mode, object id] | None}``.

    ``None`` marks a deletion. Untracked engine state is left out. Recorded for an accepted
    packet so a later registration can tell accepted work from anything else in its HEAD.
    """
    return _changes_record(_inspect(worktree))


def _changes_record(snapshot: _Snapshot) -> dict[str, list[str] | None]:
    changes: dict[str, list[str] | None] = {}
    for xy, path in snapshot.changes:
        if xy == "??" and is_engine_state(path):
            continue
        entry = snapshot.state.get(path)
        changes[path] = None if entry is None else [entry.mode, entry.oid]
    return changes


def tree_entries(worktree: str, revision: str) -> dict[str, tuple[str, str]]:
    """``{path: (mode, object id)}`` of a commit's tree, read from the raw objects."""
    commit = _raw(worktree, "rev-parse", "--verify", f"{revision}^{{commit}}").strip()
    return {path: (entry.mode, entry.oid)
            for path, entry in _tree_listing(worktree, commit.decode()).items()}


ENGINE_STATE_DIR = ".hw"


def is_engine_state(path: str) -> bool:
    return path == ENGINE_STATE_DIR or path.startswith(ENGINE_STATE_DIR + "/")


def ensure_engine_state_excluded(repo_root: str | Path) -> bool:
    """Keep the engine's ``.hw/`` state out of the host repo's commits.

    Appends ``.hw/`` to the repository's LOCAL exclude file (``.git/info/exclude``, shared by
    every worktree) so a ``git add -A`` in the product repo can never stage run state. The
    local exclude is used — not ``.gitignore`` — so the host project's tracked files are not
    touched. Returns False when *repo_root* is not inside a git repository.
    """
    try:
        raw = _git(str(repo_root), "rev-parse", "--git-path", "info/exclude").strip()
    except ValueError:  # ``_git`` refuses a git error; outside a repository stays a no-op
        return False
    if not raw:
        return False
    exclude = Path(raw)
    if not exclude.is_absolute():
        exclude = Path(repo_root) / exclude
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    # Root-anchored only: an unanchored ``.hw/`` would also hide a nested ``src/.hw/`` from
    # ``git status``, so the acceptance gate would never see worker files planted there.
    wanted = {"/.hw", "/.hw/"}
    if any(line.strip() in wanted for line in existing.splitlines()):
        return True
    exclude.parent.mkdir(parents=True, exist_ok=True)
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write(f"{prefix}# HyperWilfred run state — never commit it\n/.hw/\n")
    return True


def tracked_engine_state(path: str | Path, limit: int = 20) -> list[str]:
    """``.hw/`` files that git TRACKS at the repository containing *path* (should be none).

    A folder outside any repository tracks nothing. Any other git failure (corrupt index, broken
    ``.git`` file) raises :class:`GuardrailError`: reading it as "nothing tracked" would pass the
    check unseen, the same fail-open shape as threat-model finding F-15.
    """
    out = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "LC_ALL": "C"},  # the message below is matched in English
    )
    if out.returncode != 0:
        # "Outside any repository" needs BOTH git's exact no-repository message and no ``.git``
        # entry at or above *path*: a broken ``.git`` file says "not a git repository: <gitdir>"
        # and must fail closed, not read as "nothing tracked".
        no_repo = "not a git repository (or any of the parent directories)" in out.stderr
        here = Path(path).resolve()
        if no_repo and not any((d / ".git").exists() for d in (here, *here.parents)):
            return []
        detail = out.stderr.strip() or f"exit {out.returncode}"
        raise GuardrailError(f"git rev-parse failed in {str(path)!r}: {detail}")
    top = out.stdout.strip()
    tracked = _git_checked(top, "ls-files", "--", ENGINE_STATE_DIR).splitlines()
    return [t for t in tracked if t.strip()][:limit]


def matches(path: str, glob: str) -> bool:
    """Path/glob match with gitignore-style ``**/`` semantics on top of fnmatch.

    Python's ``fnmatch`` has no notion of ``**`` matching *zero* leading directories, so a
    blocked glob like ``**/.env`` would silently miss a repo-root ``.env`` (a real guardrail
    gap caught in testing). We additionally try the pattern with a leading ``**/`` stripped,
    so ``**/.env`` covers both ``.env`` and ``a/b/.env``.

    An exact string match always wins, so a literal path holding glob metacharacters (Next.js
    ``[projectId]`` folders) matches itself. A pattern fnmatch cannot compile (``[t-i]`` raises
    on Python 3.9) matches nothing beyond that exact string instead of raising.
    """
    if path == glob:
        return True
    try:
        if fnmatch.fnmatch(path, glob):
            return True
        if glob.startswith("**/") and fnmatch.fnmatch(path, glob[3:]):
            return True
    except re.error:
        return False
    return False


def malformed_globs(globs: list[str]) -> list[str]:
    """The patterns fnmatch cannot compile on this interpreter."""
    bad: list[str] = []
    for g in globs:
        try:
            re.compile(fnmatch.translate(g))
        except re.error:
            bad.append(g)
    return bad


def allowlist_escapes(
    paths: list[str], allow_globs: list[str], blocked_globs: list[str]
) -> list[str]:
    """Return the paths that escape the scope.

    A path escapes if it matches a blocked glob (blocked-by-default wins) OR matches no
    allow glob. Blocked takes precedence so a too-broad allow-list can never re-admit a
    protected path (secrets, .git, .env, migrations). A blocked glob that cannot be evaluated
    blocks every path (fail-closed); an allow glob that cannot be evaluated admits nothing.
    """
    escapes: list[str] = []
    blocked_unreadable = bool(malformed_globs(blocked_globs))
    for p in paths:
        if blocked_unreadable or any(matches(p, g) for g in blocked_globs):
            escapes.append(p)
            continue
        if not any(matches(p, g) for g in allow_globs):
            escapes.append(p)
    return escapes


def matches_segments(path: str, glob: str) -> bool:
    """Segment-aware match: same depth, each segment fnmatch'ed (``*`` never crosses ``/``).

    This is how a DECLARED generated artifact is resolved — identical to ``Path.glob`` in the
    reproducibility check, so the set of files exempted is exactly the set proven reproducible.
    """
    p_parts, g_parts = path.split("/"), glob.split("/")
    return len(p_parts) == len(g_parts) and all(
        fnmatch.fnmatchcase(a, b) for a, b in zip(p_parts, g_parts, strict=True)
    )


def generated_exempt(path: str, generated_globs: list[str]) -> bool:
    """True iff *path* is a declared generated artifact that may skip the patch budgets."""
    return not is_denied_generated_path(path) and any(
        matches_segments(path, g) for g in generated_globs
    )


def effective_generated_globs(generated: list[str], allowed_globs: list[str]) -> list[str]:
    """Only generated paths that sit inside the packet's allow-list keep their exemption.

    The packet JSON lives on disk, so the exemption is re-validated against the scope at
    acceptance time — it can never cover a path the worker was not allowed to write.
    """
    return [g for g in generated if any(matches(g, a) for a in allowed_globs)]


def _blob(worktree: str | Path, oid: str) -> bytes:
    return _raw(worktree, "cat-file", "blob", oid)  # raw object bytes: no filter, no textconv


def _unified_zero(old: bytes, new: bytes) -> str | None:
    """``git diff --no-index --unified=0`` of two byte strings, outside any repository.

    Run in a fresh temporary directory with no repository, no user or system configuration and
    no attributes file, so nothing the worker wrote can change the counts or run a program.
    ``None`` when git calls the content binary.
    """
    with tempfile.TemporaryDirectory(prefix="hw-diff-") as scratch:
        Path(scratch, "a").write_bytes(old)
        Path(scratch, "b").write_bytes(new)
        env = _read_env()
        env["GIT_CEILING_DIRECTORIES"] = str(Path(scratch).parent)
        out = subprocess.run(
            [*_GIT, *_READ_OVERRIDES, "diff", "--no-index", "--no-ext-diff", "--no-textconv",
             "--no-color", "--unified=0", "--", "a", "b"],
            cwd=scratch, capture_output=True, check=False, env=env,
        )
    if out.returncode not in (0, 1):
        raise GuardrailError("git diff --no-index failed — the acceptance gate fails closed")
    text = out.stdout.decode("utf-8", "replace")
    if "\nBinary files " in "\n" + text or "\nGIT binary patch" in "\n" + text:
        return None
    return text


def _diff_counts(text: str) -> tuple[int, int, int]:
    """``(added, deleted, largest hunk span)`` from a ``--unified=0`` diff."""
    added = deleted = span = 0
    in_hunks = False
    for line in text.splitlines():
        if line.startswith("@@"):
            in_hunks = True
            try:
                newpart = line.split("+", 1)[1].split(" ", 1)[0]
                span = max(span, int(newpart.split(",")[1]) if "," in newpart else 1)
            except (IndexError, ValueError):
                pass
        elif in_hunks and line.startswith("+"):
            added += 1
        elif in_hunks and line.startswith("-"):
            deleted += 1
    return added, deleted, span


def _modified_diffs(worktree: str) -> dict[str, tuple[int, int, int, int]]:
    return _diffs_of(_inspect(worktree))


def _diffs_of(snapshot: _Snapshot) -> dict[str, tuple[int, int, int, int]]:
    """``{path: (added, deleted, hunk span, lines at HEAD)}`` for every changed tracked path.

    The same set ``git diff HEAD`` covers (paths in HEAD or in the index), but compared from the
    raw HEAD blob and the bytes the snapshot read. Binary content and submodules are skipped.
    """
    result: dict[str, tuple[int, int, int, int]] = {}
    for xy, path in snapshot.changes:
        if xy == "??":
            continue
        committed = snapshot.head.get(path)
        if (committed and committed.mode == _GITLINK) or (
                snapshot.index.get(path) and snapshot.index[path].mode == _GITLINK):
            continue
        entry = snapshot.state.get(path)
        if entry is not None and entry.mode not in (_REGULAR, _EXECUTABLE, _SYMLINK):
            continue
        if committed is None and entry is None:
            continue
        old = _blob(snapshot.root, committed.oid) if committed else b""
        new = b"" if entry is None else snapshot.data.get(path, b"")
        text = _unified_zero(old, new)
        orig = len(old.decode("utf-8", "replace").splitlines())
        if text is None:
            if (not old and _is_binary(new)) or (old and _is_binary(old)):
                continue  # binary before and after (an image, an archive): no line budget
            # A text file turned binary: every line counts as changed, so one NUL byte cannot
            # carry a wholesale rewrite past the patch budget.
            added = len(new.splitlines()) or (1 if new else 0)
            result[path] = (added, orig, added, orig)
            continue
        added, deleted, span = _diff_counts(text)
        result[path] = (added, deleted, span, orig)
    return result


def _original_line_count(worktree: str, path: str) -> int:
    """Line count of *path* at HEAD (0 if the file is new), from the raw blob."""
    committed = _head_entries(Path(worktree).resolve()).get(path)
    if committed is None or committed.mode == _GITLINK:
        return 0
    return len(_blob(worktree, committed.oid).decode("utf-8", "replace").splitlines())


def _numstat(worktree: str) -> dict[str, tuple[int, int]]:
    """``{path: (added, deleted)}`` for tracked-file changes vs HEAD (see :func:`_modified_diffs`).

    Never ``git diff`` in the worktree: a worker-set ``diff.external``, textconv driver, clean
    filter or stat setting must neither run during acceptance nor change the counts.
    """
    return {path: (added, deleted)
            for path, (added, deleted, _span, _lines) in _modified_diffs(worktree).items()}


def _largest_hunk_span(worktree: str, path: str) -> int:
    """Largest contiguous changed-line span for one file (see :func:`_modified_diffs`)."""
    return _modified_diffs(worktree).get(path, (0, 0, 0, 0))[2]


def patch_violations(
    worktree: str,
    *,
    max_changed_lines: int,
    max_hunk_span: int,
    full_file_ratio: float,
    generated_globs: list[str] | None = None,
) -> list[PatchViolation]:
    """Detect patch-never-regenerate violations on MODIFIED tracked files.

    Two independent signals — a real Codex regeneration (Phase-0 A3) evaded the ratio
    check but tripped the changed-lines budget, so neither alone is sufficient.

    ``generated_globs`` are paths the packet EXPLICITLY declares as generated (each with a
    required regeneration command — see :class:`GeneratedArtifact`). Those are rewritten
    wholesale by design (a single-line JSON snapshot is 100% changed on every regeneration),
    so they are exempt from the full-file/lines/hunk rules. Every other path — all source
    files — keeps the full protection, and the allow-list escape check still applies to both.
    """
    return _violations_of(_inspect(worktree), max_changed_lines=max_changed_lines,
                          max_hunk_span=max_hunk_span, full_file_ratio=full_file_ratio,
                          generated_globs=generated_globs)


def _violations_of(
    snapshot: _Snapshot, *, max_changed_lines: int, max_hunk_span: int,
    full_file_ratio: float, generated_globs: list[str] | None,
) -> list[PatchViolation]:
    found: dict[str, PatchViolation] = {}

    def flag(path: str, added: int, deleted: int, reason: str) -> None:
        if path not in found:
            found[path] = PatchViolation(path=path, added=added, deleted=deleted, reasons=[])
        found[path].reasons.append(reason)

    exempt = generated_globs or []
    for path, (added, deleted, span, orig) in _diffs_of(snapshot).items():
        if generated_exempt(path, exempt):
            continue
        if orig > 0 and deleted >= full_file_ratio * orig and added >= full_file_ratio * orig:
            flag(path, added, deleted,
                 f"full-file-replacement (orig={orig}, -{deleted}/+{added}, "
                 f"ratio>={full_file_ratio})")
        if added + deleted > max_changed_lines:
            flag(path, added, deleted,
                 f"too-many-changed-lines ({added + deleted} > {max_changed_lines})")
        if span > max_hunk_span:
            flag(path, added, deleted, f"hunk-span-too-wide ({span} > {max_hunk_span})")
    _new_file_violations(snapshot, exempt, max_changed_lines, flag)
    return list(found.values())


# A deleted file is judged rewritten under a new name when at least this share of its words
# (identifiers, keywords, numbers, counted with repeats) reappears in one new file of the same
# packet. Files with fewer words than the minimum are too small to tell a copy from chance.
REWRITE_WORD_SHARE = 0.5
REWRITE_MIN_WORDS = 20
_WORD = re.compile(r"\w+")


def _words(data: bytes) -> collections.Counter[str]:
    return collections.Counter(_WORD.findall(data.decode("utf-8", "replace").lower()))


# Share of bytes that must be undecodable before NUL-bearing content counts as real binary.
BINARY_INVALID_SHARE = 0.1


_ESCAPED_BYTE = re.compile("[\udc80-\udcff]")


def _undecodable_bytes(data: bytes) -> int:
    """How many bytes of *data* are not valid UTF-8, in one linear pass.

    ``surrogateescape`` turns each undecodable byte into exactly one lone surrogate
    (U+DC80-U+DCFF), which valid UTF-8 can never produce, so counting them counts the bytes.
    """
    return len(_ESCAPED_BYTE.findall(data.decode("utf-8", "surrogateescape")))


def _is_binary(data: bytes) -> bool:
    """True for real binary content (an image, an archive), which keeps no line budget.

    Git's own test (a NUL byte in the first 8000 bytes) is not enough: one planted NUL, or a
    NUL plus a stray invalid byte, would turn a rewritten source file into "binary" and carry
    it past the budget. Content with NUL bytes counts as binary only when at least
    ``BINARY_INVALID_SHARE`` of its non-NUL bytes are not valid UTF-8: compressed or encoded
    assets are far above that, while text salted with enough junk to pass is no longer
    working source.
    """
    if b"\0" not in data[:8000]:
        return False
    rest = data.replace(b"\0", b"")
    if not rest:
        return True  # nothing but NUL bytes: no lines to count
    return _undecodable_bytes(rest) >= BINARY_INVALID_SHARE * len(rest)


def _text_lines(data: bytes) -> int | None:
    """Line count of text content; ``None`` for binary content, which keeps no line budget."""
    if _is_binary(data):
        return None
    return len(data.splitlines()) or (1 if data else 0)


def _new_file_violations(snapshot: _Snapshot, exempt: list[str], max_changed_lines: int,
                         flag) -> None:
    """Give new files a line budget, so delete-and-recreate cannot step around the patch guard.

    * An untracked new file counts its lines like an edit adds them (a staged new file already
      does, through :func:`_diffs_of`).
    * A deleted tracked file whose words largely reappear in one new file is a rewrite under a
      new name, the same as a full-file replacement in place.
    * When a packet both deletes files and creates files, the deleted lines and the new-file
      lines share one budget, so a dissimilar replacement cannot hide in two half-budgets.

    Declared generated artifacts are exempt, like everywhere else in the patch budget.
    """
    status = dict((path, xy) for xy, path in snapshot.changes)
    created: dict[str, bytes] = {}
    removed: dict[str, bytes] = {}
    for path, entry in snapshot.state.items():
        if is_engine_state(path) or generated_exempt(path, exempt):
            continue
        committed = snapshot.head.get(path)
        if committed is None and entry is not None and entry.mode in (_REGULAR, _EXECUTABLE):
            data = snapshot.data.get(path, b"")
            if _text_lines(data) is not None:
                created[path] = data
        elif (entry is None and committed is not None
              and committed.mode in (_REGULAR, _EXECUTABLE)):
            data = _blob(snapshot.root, committed.oid)
            if _text_lines(data) is not None:
                removed[path] = data
    for path, data in created.items():
        lines = _text_lines(data) or 0
        if status.get(path) == "??" and lines > max_changed_lines:
            flag(path, lines, 0, f"too-many-changed-lines ({lines} > {max_changed_lines})")
    new_words = {path: _words(data) for path, data in created.items()}
    for old_path, old_data in removed.items():
        old_words = _words(old_data)
        total = sum(old_words.values())
        if total < REWRITE_MIN_WORDS or not new_words:
            continue
        share, new_path = max((sum((old_words & words).values()) / total, path)
                              for path, words in new_words.items())
        if share >= REWRITE_WORD_SHARE:
            flag(new_path, _text_lines(created[new_path]) or 0, _text_lines(old_data) or 0,
                 f"rewrite-under-new-name (deleted {old_path}; {share:.0%} of its words "
                 f"reappear here, share>={REWRITE_WORD_SHARE})")
    deleted_lines = sum(_text_lines(data) or 0 for data in removed.values())
    created_lines = sum(_text_lines(data) or 0 for data in created.values())
    if deleted_lines and created_lines and deleted_lines + created_lines > max_changed_lines:
        for old_path, old_data in removed.items():
            flag(old_path, 0, _text_lines(old_data) or 0,
                 f"delete-and-create over budget (-{deleted_lines} deleted, +{created_lines} "
                 f"in new files > {max_changed_lines})")


def _remove(location: Path) -> None:
    if location.is_dir() and not location.is_symlink():
        shutil.rmtree(location)
    elif os.path.lexists(location):
        location.unlink()


def _real_directory(root: Path, relative: str) -> Path:
    """Create *relative* under *root* as real directories, replacing any link or file on the way.

    Restoring ``other/protected.py`` through a worker-made ``other -> /elsewhere`` link would
    write outside the worktree.
    """
    current = root
    for part in relative.split("/") if relative else []:
        current = current / part
        if os.path.islink(current) or (os.path.lexists(current) and not current.is_dir()):
            current.unlink()
        if not os.path.lexists(current):
            current.mkdir()
    return current


def rollback(worktree: str) -> None:
    """Leave *worktree* byte-identical to HEAD: restore tracked paths, remove worker files.

    Works from :func:`_inspect`, never ``git reset --hard`` / ``git clean``: those would run a
    worker-configured smudge filter and honour worker-written ignore rules. Tracked paths are
    rewritten from the raw HEAD blobs, every visible untracked path (including files only an
    untrusted ignore file hid) is removed, and the index is reset to HEAD without touching the
    tree. Files hidden by trusted ignore rules stay, like ``git clean -fd`` left them, and so
    does the engine's own untracked ``.hw/`` state.
    """
    snapshot = _inspect(worktree)
    root = snapshot.root
    restore: list[tuple[str, _Entry]] = []
    emptied: set[str] = set()
    for xy, path in snapshot.changes:
        committed = snapshot.head.get(path)
        if xy == "??" and is_engine_state(path):
            continue
        if committed is not None and committed.mode == _GITLINK:
            continue  # a submodule cannot be restored here; is_clean stays False
        location = snapshot.disk.get(path)
        if location is not None and os.path.lexists(location):
            _remove(location)
            emptied.update(_parents(path))
        if committed is not None:
            restore.append((path, committed))
    # A tracked path may sit under a worker-made link or file: _real_directory replaces it.
    for path, committed in restore:
        parent, _, name = path.rpartition("/")
        directory = _real_directory(root, parent)
        target = directory / name
        if os.path.lexists(target):
            _remove(target)
        data = _blob(root, committed.oid)
        if committed.mode == _SYMLINK:
            os.symlink(os.fsdecode(data), target)
            continue
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags, 0o755 if committed.mode == _EXECUTABLE else 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(target, 0o755 if committed.mode == _EXECUTABLE else 0o644)
    tracked_dirs = {parent for path in snapshot.head for parent in _parents(path)}
    for folder in sorted(emptied - tracked_dirs, key=lambda d: d.count("/"), reverse=True):
        location = root / folder
        if not is_engine_state(folder) and location.is_dir() and not location.is_symlink():
            with contextlib.suppress(OSError):
                location.rmdir()  # only when empty
    if snapshot.head:
        _raw(root, "read-tree", "HEAD")  # index := HEAD; no checkout, no filter, no hook
    else:
        _raw(root, "read-tree", "--empty")


def _link_is_safe(
    snapshot: _Snapshot, path: str, allowed_globs: list[str], blocked_globs: list[str],
) -> bool:
    entry = snapshot.state.get(path)
    if entry is None or entry.mode != _SYMLINK:
        return True  # not a link
    text = os.fsdecode(snapshot.data.get(path, b""))
    if not text or text.startswith("/"):
        return False
    parts = text.split("/")
    climbs = len(parts) - len(list(itertools.dropwhile(lambda part: part == "..", parts)))
    if any(part in ("", ".", "..") for part in parts[climbs:]):
        # Only leading ".." parts fold the same way as text and on disk: "x/../a.py" would
        # follow a link named x before climbing.
        return False
    target = posixpath.normpath(posixpath.join(posixpath.dirname(path), text))
    if target in (".", "..") or target.startswith("../") or target == path:
        return False
    if target not in snapshot.disk:  # dangling, a folder, behind a link, or ignored output
        return False
    changed = {changed_path for _xy, changed_path in snapshot.changes}
    if target in changed:
        target_entry = snapshot.state.get(target)
        return (target_entry is not None and target_entry.mode in (_REGULAR, _EXECUTABLE)
                and not allowlist_escapes([target], allowed_globs, blocked_globs))
    committed = snapshot.head.get(target)
    return committed is not None and committed.mode in (_REGULAR, _EXECUTABLE)


def is_clean(worktree: str) -> bool:
    """True iff the *product* tree is byte-identical to HEAD.

    Ignores the engine's own ``.hw/`` state (which rollback intentionally preserves), so
    this reports the meaningful guarantee — "the worker's writes are gone, the tree matches
    HEAD" — rather than flagging engine bookkeeping as a dirty tree.
    """
    return not [path for _xy, path in _inspect(worktree).changes if not is_engine_state(path)]


# The engine's own state directory is bookkeeping, never a worker write — exempt it from
# the escape check so a worktree that happens to coincide with the engine's repo (e.g. in
# tests, or a single-worktree run) does not flag engine state as an out-of-scope escape.
ENGINE_IGNORE_GLOBS: tuple[str, ...] = (".hw/**", ".hw")


def accept(
    *,
    packet_id: str,
    worktree: str,
    allowed_globs: list[str],
    blocked_globs: list[str],
    result_file: str | None,
    max_changed_lines: int,
    max_hunk_span: int,
    full_file_ratio: float,
    enforce_patch_guard: bool = True,
    do_rollback: bool = True,
    ignore_globs: list[str] | None = None,
    generated_globs: list[str] | None = None,
    accepted_changes_out: dict[str, list[str] | None] | None = None,
) -> AcceptanceVerdict:
    """Run the engine acceptance gate over one worktree and return a typed verdict.

    The scope check, the patch budget and (when accepted, into ``accepted_changes_out``) the
    record of what was accepted all come from ONE read of the worktree (:func:`_inspect`), so
    a file changed while the gate runs cannot be recorded as accepted without being judged.

    Accepts iff there are no scope escapes and (when ``enforce_patch_guard``) no patch
    violations. On rejection, rolls the worktree back to a byte-clean HEAD unless
    ``do_rollback`` is False. The result is read from disk only. Paths matching
    ``ignore_globs`` (default: the engine's own ``.hw/`` state) are exempt from the escape
    check — they are engine bookkeeping, not worker output.
    """
    require_worktree(worktree)  # fail closed: an uninspectable folder is never "no changes"
    ignore = ignore_globs if ignore_globs is not None else list(ENGINE_IGNORE_GLOBS)
    snapshot = _inspect(worktree)
    entries = snapshot.changes
    paths = [p for _, p in entries if not any(matches(p, g) for g in ignore)]
    escapes = allowlist_escapes(paths, allowed_globs, blocked_globs)
    # Untracked engine state is bookkeeping and stays exempt, but engine state that is staged
    # or tracked is on its way into a commit — that is how run artifacts leak into the host
    # repo, so it is rejected like any other out-of-scope write.
    escapes += [p for xy, p in entries if is_engine_state(p) and xy != "??"]
    # A link is judged only by its own path, so an in-scope ``src/linked -> ../build`` would
    # carry everything behind it past the blocked globs. A new or changed link is accepted only
    # when its text (as read and hashed) names a regular file the gate itself sees, reached
    # through real folders, that is unchanged at HEAD or a change that is itself in scope.
    escapes += [p for p in paths if p not in escapes
                and not _link_is_safe(snapshot, p, allowed_globs, blocked_globs)]
    pviol = (
        _violations_of(
            snapshot,
            max_changed_lines=max_changed_lines,
            max_hunk_span=max_hunk_span,
            full_file_ratio=full_file_ratio,
            generated_globs=generated_globs,
        )
        if enforce_patch_guard
        else []
    )

    result_bytes = 0
    result_ok = False
    if result_file and Path(result_file).exists():
        result_bytes = Path(result_file).stat().st_size
        result_ok = True

    accepted = not escapes and not pviol
    verdict = AcceptanceVerdict(
        packet_id=packet_id,
        worktree=worktree,
        changed_paths=paths,
        escapes=escapes,
        patch_violations=pviol,
        result_read_from_disk=result_ok,
        result_bytes=result_bytes,
        accepted=accepted,
        rolled_back=False,
        tree_clean_after=None,
    )
    if accepted and accepted_changes_out is not None:
        accepted_changes_out.update(_changes_record(snapshot))
    if not accepted and do_rollback:
        rollback(worktree)
        verdict.rolled_back = True
        verdict.tree_clean_after = is_clean(worktree)
    return verdict


__all__ = [
    "GuardrailError",
    "accept",
    "allowlist_escapes",
    "changed_paths",
    "accepted_changes",
    "ensure_engine_state_excluded",
    "is_engine_state",
    "tracked_engine_state",
    "generated_exempt",
    "matches_segments",
    "is_clean",
    "matches",
    "patch_violations",
    "require_worktree",
    "rollback",
    "tree_entries",
]
