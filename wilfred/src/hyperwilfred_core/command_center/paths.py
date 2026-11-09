"""Path helpers shared by the collectors and the snapshot writer. Reads only."""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

# Rule 10: directories that conventionally hold secrets are never read.
SECRET_DIR_NAMES = frozenset({"Private", ".ssh", "secrets"})


def same_file(a: str | Path, b: str | Path) -> bool:
    """True when both paths exist and are the same file (identity, not spelling)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def canonical(path: str | Path) -> Path:
    """Absolute path with symbolic links resolved and each existing part in its on-disk spelling.

    ``os.path.realpath`` keeps whatever letter case the caller typed. On a case-insensitive
    filesystem that would let ``/x/WS`` and ``/x/ws`` look like different directories, so each
    part that does not match a directory entry exactly is replaced by the entry it refers to.
    """
    real = Path(os.path.realpath(os.path.expanduser(path)))
    current = Path(real.anchor)
    parts = real.parts[1:]
    for index, part in enumerate(parts):
        try:
            names = os.listdir(current)
        except OSError:
            return current.joinpath(*parts[index:])
        if part not in names:
            # ``current / part`` is already link-free, so a sibling link to it is not a match.
            match = next(
                (
                    n
                    for n in names
                    if not os.path.islink(current / n) and same_file(current / n, current / part)
                ),
                None,
            )
            if match is None:
                return current.joinpath(*parts[index:])
            part = match
        current = current / part
    return current


def is_inside(path: Path, container: Path) -> bool:
    """True when ``path`` is ``container`` or lies below it, by spelling or by file identity."""
    if path == container or container in path.parents:
        return True
    if any(same_file(candidate, container) for candidate in (path, *path.parents)):
        return True
    # A path that does not exist has no identity to compare (a worktree whose directory is
    # gone). Compare the spelling without letter case; refusing too much is the safe side.
    folded, inner = _fold(container), _fold(path)
    return inner == folded or inner.startswith(folded.rstrip("/") + "/")


def _fold(path: Path) -> str:
    return unicodedata.normalize("NFC", path.as_posix()).casefold()


def secret_part(path: str | Path) -> str | None:
    """The first component of ``path`` that names a secrets directory, or None."""
    return next((part for part in Path(path).parts if part in SECRET_DIR_NAMES), None)


def is_inside_run_store(path: str | Path) -> bool:
    return "/.hw/" in Path(path).as_posix() + "/"
