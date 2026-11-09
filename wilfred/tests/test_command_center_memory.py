"""Command center: the shared-memory summary (counts only, never note bodies)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hyperwilfred_core.command_center.collect_memory import collect_memory, parse_frontmatter

SECRET_BODY = "BODY-THAT-MUST-NOT-LEAK"


def _note(root: Path, relative: str, frontmatter: str | None) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    head = "" if frontmatter is None else f"---\n{frontmatter}\n---\n"
    path.write_text(f"{head}\n{SECRET_BODY}\n", encoding="utf-8")
    return path


def _memory(tmp_path: Path) -> Path:
    root = tmp_path / "memory"
    root.mkdir()
    (root / "MEMORY.md").write_text(
        "Intro line\n\n- [Nested](nested.md) — hook\n- [Top](top.md) — hook\n"
        "  - [indented is not an index line](x.md)\nplain text\n- [Third](third.md) — hook\n",
        encoding="utf-8",
    )
    return root


def test_types_from_metadata_and_from_top_level(tmp_path: Path) -> None:
    root = _memory(tmp_path)
    _note(root, "nested.md", "name: nested\ndescription: has metadata\nmetadata:\n  type: project")
    _note(root, "top.md", "name: top\ndescription: top-level type\ntype: feedback")
    _note(root, "both.md",
          "name: both\ndescription: metadata wins\ntype: user\nmetadata:\n  type: reference")
    _note(root, "projects/p/deep.md", 'name: "deep"\ndescription: "quoted: with colon"\ntype: user')
    memory, problems = collect_memory(root)
    assert problems == []
    assert memory.root == str(root)
    assert memory.notes == 4
    assert memory.by_type.model_dump() == {
        "project": 1, "feedback": 1, "user": 1, "reference": 1, "untyped": 0,
    }
    assert memory.malformed == []
    assert memory.index_lines == 3
    assert {s.field for s in memory.sources} == {"notes", "index_lines"}


def test_note_without_type_and_without_description_are_malformed(tmp_path: Path) -> None:
    root = _memory(tmp_path)
    no_type = _note(root, "no-type.md", "name: a\ndescription: d")
    no_description = _note(root, "no-description.md", "name: b\nmetadata:\n  type: project")
    no_frontmatter = _note(root, "plain.md", None)
    odd_type = _note(root, "odd.md", "name: c\ndescription: d\ntype: diary")
    unclosed = root / "unclosed.md"
    unclosed.write_text("---\nname: d\ndescription: never closed\n", encoding="utf-8")
    _note(root, "fine.md", "name: e\ndescription: d\nmetadata:\n  type: user")
    memory, problems = collect_memory(root)
    assert problems == []
    assert memory.notes == 6
    assert memory.by_type.model_dump() == {
        "project": 1, "feedback": 0, "user": 1, "reference": 0, "untyped": 4,
    }
    malformed = {m.path: m.problem for m in memory.malformed}
    assert malformed == {
        str(no_type): "missing type",
        str(no_description): "missing description",
        str(no_frontmatter): "no frontmatter block",
        str(odd_type): "missing a recognised type",
        str(unclosed): "no frontmatter block",
    }


def test_note_bodies_names_and_descriptions_never_reach_the_snapshot(tmp_path: Path) -> None:
    root = _memory(tmp_path)
    _note(root, "a.md", "name: SECRET-NAME\ndescription: SECRET-DESCRIPTION\ntype: user")
    memory, _ = collect_memory(root)
    dumped = memory.model_dump_json()
    for leaked in (SECRET_BODY, "SECRET-NAME", "SECRET-DESCRIPTION"):
        assert leaked not in dumped


def test_index_files_links_and_secret_directories_are_not_notes(tmp_path: Path) -> None:
    root = _memory(tmp_path)
    _note(root, "real.md", "name: a\ndescription: d\ntype: user")
    (root / "projects" / "p").mkdir(parents=True)
    (root / "projects" / "p" / "MEMORY.md").write_text("- [x](x.md)\n", encoding="utf-8")
    _note(root, "secrets/hidden.md", "name: h\ndescription: d\ntype: user")
    (root / "notes.txt").write_text("not markdown\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    _note(elsewhere, "outside.md", "name: o\ndescription: d\ntype: user")
    (root / "link.md").symlink_to(elsewhere / "outside.md")
    (root / "linked-dir").symlink_to(elsewhere, target_is_directory=True)
    memory, problems = collect_memory(root)
    assert problems == []
    assert memory.notes == 1 and memory.index_lines == 3


def test_missing_memory_directory(tmp_path: Path) -> None:
    memory, problems = collect_memory(tmp_path / "absent")
    assert memory.notes == 0 and memory.malformed == [] and memory.index_lines is None
    assert len(problems) == 1 and problems[0].what == "memory directory"


def test_missing_index_is_a_problem_but_notes_are_counted(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    _note(root, "a.md", "name: a\ndescription: d\ntype: user")
    memory, problems = collect_memory(root)
    assert memory.notes == 1 and memory.index_lines is None, "unknown is not zero"
    assert [p.what for p in problems] == ["memory index"]


def test_parse_frontmatter_details() -> None:
    assert parse_frontmatter("no block") is None
    assert parse_frontmatter("---\nname: x\n") is None
    fields = parse_frontmatter(
        "﻿---\nname: n\n# comment\ndescription: a: b\nmetadata:\n  type: project\n"
        "  other: z\norigin: s\n---\nbody: not-frontmatter\n"
    )
    assert fields == {"name": "n", "description": "a: b", "metadata": "",
                      "metadata.type": "project", "metadata.other": "z", "origin": "s"}


def test_nested_blocks_and_comments_in_frontmatter() -> None:
    fields = parse_frontmatter(
        "---\nname: n # trailing comment\ndescription: 'kept # inside quotes'\n"
        "metadata:   # comment after the parent\n  type: project\n  sub:\n    type: user\n"
        "other:\n  type: feedback\n---\n"
    )
    assert fields is not None
    assert fields["name"] == "n" and fields["description"] == "kept # inside quotes"
    quoted = parse_frontmatter('---\ntype: "user" # c\nname: \'a\'\n---\n')
    assert quoted == {"type": "user", "name": "a"}
    assert fields["metadata.type"] == "project", "a deeper `type` must not override it"
    assert fields["other.type"] == "feedback" and "type" not in fields


def test_unreadable_note_is_counted_and_reported(tmp_path: Path) -> None:
    root = _memory(tmp_path)
    locked = _note(root, "locked.md", "name: a\ndescription: d\ntype: user")
    _note(root, "open.md", "name: b\ndescription: d\ntype: user")
    locked.chmod(0o000)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running with privileges that ignore file permissions")
        memory, problems = collect_memory(root)
    finally:
        locked.chmod(0o600)
    assert memory.notes == 2
    assert memory.by_type.user == 1 and memory.by_type.untyped == 1
    assert [(m.path, m.problem) for m in memory.malformed] == [(str(locked), "unreadable")]
    assert [p.what for p in problems] == ["memory note"]


def test_odd_entries_named_like_notes_do_not_block_or_leak(tmp_path: Path) -> None:
    root = _memory(tmp_path)
    _note(root, "real.md", "name: a\ndescription: d\ntype: user")
    os.mkfifo(root / "pipe.md")  # reading a FIFO would block forever
    (root / "dir.md").mkdir()
    _note(root, ".env.md", "name: e\ndescription: d\ntype: user")
    memory, problems = collect_memory(root)
    assert memory.notes == 1
    assert [(Path(p.path).name, p.reason) for p in problems] == [("pipe.md", "not a regular file")]
