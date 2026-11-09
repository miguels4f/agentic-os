"""L10 — the pilot lessons are recorded via the learning mechanism and loaded by future runs."""

from __future__ import annotations

import json
from pathlib import Path

from hyperwilfred_core import engine, learning
from hyperwilfred_core.schemas import BuildObjective


def test_every_l_item_is_recorded_as_a_lesson() -> None:
    ids = {x.lesson_id for x in learning.PILOT_LESSONS}
    assert ids == {f"PILOT-L{n}" for n in range(1, 10)}
    for lesson in learning.PILOT_LESSONS:
        assert lesson.lesson and lesson.enforced_by and lesson.applies_to_steps


def test_hw_learn_persists_the_lessons(tmp_path: Path) -> None:
    out = learning.learn(repo_root=tmp_path)
    assert out["lessons"]["lessons"] == 9
    data = json.loads((tmp_path / ".hw" / "lessons" / "lessons.json").read_text())
    assert len(data) == 9
    assert "PILOT-L1" in (tmp_path / ".hw" / "lessons" / "lessons.md").read_text()


def test_next_step_attaches_the_relevant_lessons(tmp_path: Path) -> None:
    obj = BuildObjective(id="o", title="t", objective="x")
    engine.plan(repo_root=tmp_path, objective=obj, run_id="r", product_dir="p")
    first = engine.next_step(repo_root=tmp_path, run_id="r")  # frame step
    assert first["step"]["id"] == "frame" and first["lessons"] == []
    assert {x["id"] for x in learning.lessons_for_step("decompose")} >= {
        "PILOT-L4", "PILOT-L5", "PILOT-L9",
    }
    assert {x["id"] for x in learning.lessons_for_step("quality")} == {
        "PILOT-L1", "PILOT-L2", "PILOT-L3",
    }
    assert {x["id"] for x in learning.lessons_for_step("verify")} == {
        "PILOT-L4", "PILOT-L6", "PILOT-L7",
    }
