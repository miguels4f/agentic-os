"""Wave planner — dependency-respecting, scope-disjoint parallel waves."""

from __future__ import annotations

import pytest

from hyperwilfred_core.schemas import Slice
from hyperwilfred_core.wave_planner import plan_waves


def _slice(id_: str, globs: list[str], deps: list[str] | None = None) -> Slice:
    return Slice(id=id_, title=id_, allowed_globs=globs, depends_on=deps or [])


def test_disjoint_scopes_run_in_one_wave() -> None:
    waves = plan_waves([_slice("a", ["src/a/**"]), _slice("b", ["src/b/**"])])
    assert len(waves) == 1
    assert {s.id for s in waves[0]} == {"a", "b"}


def test_overlapping_scopes_split_into_separate_waves() -> None:
    waves = plan_waves([_slice("a", ["src/shared/**"]), _slice("b", ["src/shared/**"])])
    assert len(waves) == 2


def test_dependency_is_ordered_before_dependent() -> None:
    waves = plan_waves(
        [_slice("ui", ["ui/**"], deps=["api"]), _slice("api", ["api/**"])]
    )
    assert waves[0][0].id == "api"
    assert waves[1][0].id == "ui"


def test_unknown_dependency_raises() -> None:
    with pytest.raises(ValueError, match="unknown slice"):
        plan_waves([_slice("a", ["a/**"], deps=["ghost"])])


def test_dependency_cycle_raises() -> None:
    with pytest.raises(ValueError, match="cycle"):
        plan_waves(
            [_slice("a", ["a/**"], deps=["b"]), _slice("b", ["b/**"], deps=["a"])]
        )


def test_empty_input_is_empty_plan() -> None:
    assert plan_waves([]) == []
