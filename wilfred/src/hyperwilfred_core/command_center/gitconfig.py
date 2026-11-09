"""Find the content filters a git configuration defines, by reading it as text.

``git status`` compares working-tree files with the index, and for a file with a ``filter``
attribute it RUNS the configured ``clean`` command (git-lfs is the common case; that command
may write under ``.git/lfs``). A read-only collector must not trigger that. The git allowlist
has no ``config`` sub-command, so the names are found by reading the configuration files the
way git does; the git reader then switches those filters off for its ``status`` call.

The reader FAILS CLOSED: a file it cannot read or cannot parse with certainty is reported as
unverified, and the caller then does not run ``status`` at all.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

MAX_CONFIG_BYTES = 1024 * 1024
MAX_INCLUDE_DEPTH = 5

SAFE_FILTER_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")

_HEADER = re.compile(r"\s*\[\s*([A-Za-z0-9.\-]+)(?:\s+\"((?:[^\"\\]|\\.)*)\")?\s*\]")
_KEY = re.compile(r"\s*([A-Za-z][A-Za-z0-9\-]*)\s*(=?)")
# Safety net: every line that LOOKS like a filter or include header must have been parsed as
# one. A difference means the text was read differently from git, so nothing is trusted.
_LOOKS_RELEVANT = re.compile(r"^[ \t]*\[\s*(?:filter|include)", re.IGNORECASE | re.MULTILINE)
_ESCAPES = {"n": "\n", "t": "\t", "b": "\b", '"': '"', "\\": "\\"}


class _Unparseable(ValueError):
    pass


def _value(lines: list[str], index: int, start: int) -> tuple[str, int]:
    """Parse one value git-style from ``lines[index][start:]``.

    Handles quotes, escapes, ``#``/``;`` comments and backslash line continuations. Returns
    the value and the index of the last line it used.
    """
    out: list[tuple[str, bool]] = []
    quoted = False
    line, position = lines[index], start
    while True:
        if position >= len(line):
            break
        char = line[position]
        if char == "\\":
            if position + 1 >= len(line):  # backslash at end of line: the value continues
                index += 1
                if index >= len(lines):
                    raise _Unparseable("continuation at end of file")
                line, position = lines[index], 0
                continue
            escaped = line[position + 1]
            if escaped not in _ESCAPES:
                raise _Unparseable(f"unknown escape \\{escaped}")
            out.append((_ESCAPES[escaped], True))
            position += 2
            continue
        if char == '"':
            quoted = not quoted
        elif char in "#;" and not quoted:
            break
        else:
            out.append((char, quoted))
        position += 1
    if quoted:
        raise _Unparseable("unterminated quote")
    while out and out[-1][0] in " \t\r" and not out[-1][1]:
        out.pop()
    while out and out[0][0] in " \t" and not out[0][1]:
        out.pop(0)
    return "".join(char for char, _ in out), index


def parse(text: str) -> list[tuple[str, str | None, str, str | None]]:
    """Every ``(section, subsection, key, value)`` of a git configuration text.

    A section header alone yields one item with an empty key. Raises ``_Unparseable`` for
    anything that is not certainly valid; the caller treats that as "unverified".
    """
    text = text.removeprefix("\ufeff")  # git skips a byte-order mark
    lines = text.split("\n")
    items: list[tuple[str, str | None, str, str | None]] = []
    section: str | None = None
    subsection: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index].rstrip("\r")
        lines[index] = line
        position = 0
        stripped = line.lstrip()
        if stripped.startswith("["):
            header = _HEADER.match(line)
            if not header:
                raise _Unparseable(f"line {index + 1}: section header")
            name = header.group(1)
            if header.group(2) is not None:
                section = name.lower()
                subsection = re.sub(r"\\(.)", r"\1", header.group(2))
            elif "." in name:  # deprecated ``[section.sub]``: git lowercases all of it
                section, _, subsection = name.lower().partition(".")
            else:
                section, subsection = name.lower(), None
            items.append((section, subsection, "", None))
            position = header.end()
            stripped = line[position:].lstrip()
        if not stripped or stripped[0] in "#;":
            index += 1
            continue
        key = _KEY.match(line, position)
        if not key or section is None:
            raise _Unparseable(f"line {index + 1}: key")
        if key.group(2):
            value, index = _value(lines, index, key.end())
            items.append((section, subsection, key.group(1).lower(), value))
        else:
            rest = line[key.end():].lstrip()
            if rest and rest[0] not in "#;":
                raise _Unparseable(f"line {index + 1}: text after key")
            items.append((section, subsection, key.group(1).lower(), None))
        index += 1
    return items


def user_config_files() -> list[Path]:
    """The per-user configuration files git reads (the system file is disabled by the reader)."""
    home = Path(os.path.expanduser("~"))
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return [home / ".gitconfig", Path(xdg) / "git" / "config"]


def filter_names(config_files: list[Path]) -> tuple[set[str], list[str]]:
    """Names of every content filter defined in the given files and their includes.

    Returns ``(names, unverified)``. When ``unverified`` is not empty the caller cannot be
    sure that every filter is known and must not run ``git status``. Conditional includes are
    followed unconditionally, which can only add names (switching off a filter that is not
    active is harmless). A missing file is not a problem: git ignores it too.
    """
    names: set[str] = set()
    unverified: list[str] = []
    visited: set[str] = set()

    def scan(path: Path, depth: int) -> None:
        key = os.path.realpath(path)
        if key in visited:
            return
        visited.add(key)
        if not os.path.lexists(path):
            return
        if depth > MAX_INCLUDE_DEPTH:
            unverified.append(f"{path}: includes nested deeper than {MAX_INCLUDE_DEPTH}")
            return
        try:
            if not os.path.isfile(path):
                raise OSError("not a regular file")
            if os.path.getsize(path) > MAX_CONFIG_BYTES:
                unverified.append(f"{path}: larger than {MAX_CONFIG_BYTES} bytes")
                return
            # git skips a byte-order mark
            text = Path(path).read_bytes().decode("utf-8").removeprefix("\ufeff")
            if "\x00" in text:
                raise _Unparseable("contains a NUL byte")
            items = parse(text)
        except (OSError, ValueError) as exc:
            unverified.append(f"{path}: {exc}")
            return
        relevant = sum(
            1 for section, _, key_name, _ in items
            if not key_name and section in ("filter", "include", "includeif")
        )
        if relevant != len(_LOOKS_RELEVANT.findall(text)):
            unverified.append(f"{path}: filter or include sections could not be read reliably")
            return
        for section, subsection, key_name, value in items:
            if section == "filter" and subsection is not None:
                names.add(subsection)
            elif section in ("include", "includeif") and key_name == "path":
                if not value:
                    continue
                target = os.path.expanduser(value)
                # ``%(prefix)/...`` is expanded by git relative to its installation.
                if target.startswith(("~", "%(")):
                    unverified.append(f"{path}: include path {value!r} cannot be resolved")
                    continue
                scan(Path(path).parent / target, depth + 1)

    for config in config_files:
        scan(config, 0)
    return names, unverified
