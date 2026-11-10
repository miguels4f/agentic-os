"""Fail-closed validation for identifiers and repository-derived paths."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PurePath

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_identifier(value: str, *, label: str = "identifier") -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or ".." in value:
        raise ValueError(
            f"{label} must be 1-64 ASCII letters, digits, '.', '_' or '-' with no '..'"
        )
    return value


def validate_relative_path(value: str, *, label: str = "path", allow_glob: bool = False) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty relative path")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"{label} must not be absolute")
    parts = PurePath(normalized).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{label} must not contain traversal components")
    if not allow_glob and any(char in value for char in "*?[]"):
        raise ValueError(f"{label} must not contain glob syntax")
    return value


def contained_path(root: Path, *parts: str, label: str = "path") -> Path:
    """Return a descendant and reject symlink/traversal escape before I/O."""
    root = root.resolve()
    candidate = root.joinpath(*parts)
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError(f"{label} escapes {root}")
    current = root
    for part in candidate.relative_to(root).parts:
        current /= part
        if current.exists() and current.is_symlink():
            raise ValueError(f"{label} crosses symbolic link {current}")
    return candidate


class UnstableFileError(ValueError):
    """A path could not be read as one stable regular file reached without any symlink.

    ``reason`` names the refusal (``escape``, ``symlink``, ``missing``, ``unreadable``,
    ``not-regular``, ``unopenable``, ``replaced`` or ``changed``) so each caller can translate it
    into its own error type and wording at its boundary.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )


def stable_regular_bytes(
    path: Path, *, root: Path | None = None, label: str = "file", allow_missing: bool = False,
) -> bytes | None:
    """Read one regular file without following links and reject a concurrent replacement.

    Every component below *root* (default: the file's own directory) must be reached without
    crossing a symlink; internal, external, broken and cyclic links are all refused. The file is
    opened with ``O_NOFOLLOW`` and its device, inode, mode, size, mtime and ctime must stay the
    same from the first ``lstat`` through the read to a final ``lstat``. Every refusal raises
    :class:`UnstableFileError`; with *allow_missing* an absent file returns ``None`` instead.
    """
    absolute_root = Path(os.path.abspath(root or path.parent))
    absolute_path = Path(os.path.abspath(path))
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as exc:
        raise UnstableFileError("escape", f"{label} escapes its trusted root") from exc
    candidate = absolute_root
    try:
        for component in relative.parts:
            candidate /= component
            if stat.S_ISLNK(os.lstat(candidate).st_mode):
                raise UnstableFileError("symlink", f"{label} contains symlink {candidate}")
        before = os.lstat(absolute_path)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise UnstableFileError("missing", f"{label} is missing") from None
    except OSError as exc:
        raise UnstableFileError("unreadable", f"{label} cannot be inspected safely") from exc
    if not stat.S_ISREG(before.st_mode):
        raise UnstableFileError("not-regular", f"{label} is not a regular file")
    try:
        descriptor = os.open(absolute_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise UnstableFileError("unopenable", f"{label} cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise UnstableFileError("replaced", f"{label} changed before it was opened")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        current = os.lstat(absolute_path)
    except OSError as exc:
        raise UnstableFileError("changed", f"{label} changed while it was read") from exc
    if not _identity(before) == _identity(opened) == _identity(after) == _identity(current):
        raise UnstableFileError("changed", f"{label} changed while it was read")
    return b"".join(chunks)


__all__ = [
    "UnstableFileError",
    "contained_path",
    "stable_regular_bytes",
    "validate_identifier",
    "validate_relative_path",
]
