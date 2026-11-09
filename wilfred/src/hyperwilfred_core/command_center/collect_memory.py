"""Summarise the shared memory directory: how many notes, of which type, which are malformed.

Only the frontmatter block of each note is looked at, as plain ``key: value`` text. Note
bodies, names and descriptions are never copied into the snapshot. Reads only.
"""

from __future__ import annotations

import os
from pathlib import Path

from hyperwilfred_core.command_center.model import (
    MalformedNote,
    Memory,
    MemoryByType,
    Problem,
    Source,
)
from hyperwilfred_core.command_center.paths import SECRET_DIR_NAMES

INDEX_NAME = "MEMORY.md"
MAX_NOTE_BYTES = 4 * 1024 * 1024
KNOWN_TYPES = ("project", "feedback", "user", "reference")


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """The frontmatter between the first two ``---`` lines, or None when there is none.

    Keys of the nested ``metadata:`` block (the indented lines below it) are returned as
    ``metadata.<key>``. No YAML library: each line is split on its first colon.
    """
    lines = text.removeprefix("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    fields: dict[str, str] = {}
    parent: str | None = None
    child_indent: int | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        indent = len(key) - len(key.lstrip(" \t"))
        key, value = key.strip(), value.strip()
        if value[:1] in ("\"", "'"):
            # Quoted: the value is the text up to the closing quote; anything after is ignored.
            quote = value[0]
            closing = value.find(quote, 1)
            value = value[1:closing] if closing > 0 else value[1:]
        else:
            value = value.split(" #", 1)[0].strip()  # a trailing comment is not the value
            if value.startswith("#"):
                value = ""
        if indent == 0:
            fields[key] = value
            parent, child_indent = (key, None) if not value else (None, None)
        elif parent is not None:
            # Only the direct children of the block count: deeper lines belong to a child.
            if child_indent is None:
                child_indent = indent
            if indent == child_indent:
                fields[f"{parent}.{key}"] = value
    return None  # the block is never closed


def _note_files(root: Path, problems: list[Problem]) -> list[Path]:
    """Every ``.md`` file under ``root`` except index files; links are not followed."""
    found: list[Path] = []

    def walk(directory: Path) -> None:
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            problems.append(
                Problem(path=str(directory), what="memory directory", reason=f"unreadable: {exc}")
            )
            return
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in SECRET_DIR_NAMES and entry.name != ".git":
                        walk(Path(entry.path))
                elif entry.name.endswith(".md") and entry.name != INDEX_NAME:
                    if entry.name.startswith(".env"):
                        continue  # rule 10: files named .env* are never read
                    if entry.is_file(follow_symlinks=False):
                        found.append(Path(entry.path))
                    else:
                        problems.append(
                            Problem(
                                path=entry.path, what="memory note", reason="not a regular file"
                            )
                        )
            except OSError as exc:
                problems.append(
                    Problem(path=entry.path, what="memory entry", reason=f"unreadable: {exc}")
                )

    walk(root)
    return found


def collect_memory(root: Path) -> tuple[Memory, list[Problem]]:
    """The Memory block for ``root``. A missing directory gives zero notes and one problem."""
    problems: list[Problem] = []
    memory = Memory(root=str(root))
    if os.path.islink(root) or not os.path.isdir(root):
        problems.append(
            Problem(path=str(root), what="memory directory", reason="missing or not a directory")
        )
        return memory, problems

    by_type = dict.fromkeys((*KNOWN_TYPES, "untyped"), 0)
    notes = 0
    for path in _note_files(root, problems):
        try:
            if os.path.getsize(path) > MAX_NOTE_BYTES:
                raise OSError(f"larger than {MAX_NOTE_BYTES} bytes")
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            problems.append(
                Problem(path=str(path), what="memory note", reason=f"unreadable: {exc}")
            )
            # Still a note: a count that leaves it out would be wrong.
            notes += 1
            by_type["untyped"] += 1
            memory.malformed.append(MalformedNote(path=str(path), problem="unreadable"))
            continue
        notes += 1
        fields = parse_frontmatter(text)
        if fields is None:
            by_type["untyped"] += 1
            memory.malformed.append(MalformedNote(path=str(path), problem="no frontmatter block"))
            continue
        declared = fields.get("metadata.type") or fields.get("type") or ""
        missing = [key for key in ("name", "description") if not fields.get(key)]
        if not declared:
            missing.append("type")
        elif declared not in KNOWN_TYPES:
            missing.append("a recognised type")
        by_type[declared if declared in KNOWN_TYPES else "untyped"] += 1
        if missing:
            memory.malformed.append(
                MalformedNote(path=str(path), problem="missing " + ", ".join(missing))
            )

    memory.notes = notes
    memory.by_type = MemoryByType(**by_type)
    memory.sources.append(Source(kind="file", path=str(root), field="notes"))

    index = root / INDEX_NAME
    try:
        if os.path.islink(index) or not os.path.isfile(index):
            raise OSError("missing or not a regular file")
        if os.path.getsize(index) > MAX_NOTE_BYTES:
            raise OSError(f"larger than {MAX_NOTE_BYTES} bytes")
        text = index.read_bytes().decode("utf-8", errors="replace")
        memory.index_lines = sum(1 for line in text.splitlines() if line.startswith("- ["))
        memory.sources.append(Source(kind="file", path=str(index), field="index_lines"))
    except OSError as exc:
        problems.append(Problem(path=str(index), what="memory index", reason=str(exc)))
    return memory, problems
