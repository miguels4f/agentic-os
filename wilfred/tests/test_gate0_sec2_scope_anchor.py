"""A rewritten loose scope file cannot replace the snapshot anchored in run state."""

from pathlib import Path

import pytest
from test_gate0_fix_pinned_scope import _pinned_run

from hyperwilfred_core import engine, quality


def test_rewritten_scope_pin_does_not_revive_stale_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, paths = _pinned_run(tmp_path, monkeypatch)
    source.write_text("VALUE = 3\n")
    state = engine.load_state(paths)
    paths.artifact("deterministic-scope.sha256").write_text(
        quality.product_snapshot_sha256(tmp_path, state) + "\n"
    )
    assert not quality.quality_status(repo_root=tmp_path, run_id="q")["deterministic_green"]
    with pytest.raises(quality.QualityError, match="anchor|altered|stale"):
        quality._review_scope_sha256(tmp_path, "q", state, "subjective-0")


def test_modern_state_without_anchor_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, paths = _pinned_run(tmp_path, monkeypatch)
    state = engine.load_state(paths)
    state.deterministic_scope_sha256 = None
    engine.save_state(paths, state)
    assert not quality.quality_status(repo_root=tmp_path, run_id="q")["deterministic_green"]
    with pytest.raises(quality.QualityError, match="anchor"):
        quality._review_scope_sha256(tmp_path, "q", state, "subjective-0")
