"""Fail-closed validation for identifiers and repository-derived paths."""
from __future__ import annotations

import re
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


__all__ = ["contained_path", "validate_identifier", "validate_relative_path"]
