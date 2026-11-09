from __future__ import annotations

from pathlib import Path

import pytest

from hyperwilfred_core import engine, gates
from hyperwilfred_core.schemas import Plan, Slice


def test_run_state_root_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".hw").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="state root escapes"):
        engine.RunPaths(repo, "safe-run")


def test_plan_rejects_traversal_slice_and_unknown_dependency(tmp_path: Path) -> None:
    hostile = tmp_path / "hostile.json"
    hostile.write_text(
        Plan(objective_id="o", product_dir="product",
             slices=[Slice(id="../escape", title="bad")]).model_dump_json(), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="slice id"):
        engine._validate_plan(hostile)

    unknown = tmp_path / "unknown.json"
    unknown.write_text(
        Plan(objective_id="o", product_dir="product",
             slices=[Slice(id="safe", title="bad", depends_on=["missing"])]).model_dump_json(),
        encoding="utf-8",
    )
    with pytest.raises(engine.EngineError, match="unknown dependency"):
        engine._validate_plan(unknown)


def test_gate_identifier_cannot_become_a_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="gate_id"):
        gates.gate_json_in(tmp_path, "../../escape")
