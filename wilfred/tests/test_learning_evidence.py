"""Learning proposals say how strong their evidence is (claim-interrogation, metrics-skeptic).

Proves: each proposal states "N of M observed runs" and flags a single-run signal as possible
noise; the confidence number is labelled a heuristic; older ledger items still load; an
unreadable ledger item stops ``hw learn`` instead of being overwritten; and, once promoted, a
proposal reports the share of FINISHED BUILD/IMPROVE runs flagged before vs after the fix —
without ever touching its decision fields.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_learning import _decide, approval_key  # noqa: F401

from hyperwilfred_core import learning
from hyperwilfred_core.engine import RunPaths, load_state, plan, save_state
from hyperwilfred_core.schemas import (
    BuildMode,
    BuildObjective,
    LearningProposal,
    PromotionOutcome,
    ProposalStatus,
    QualityLoopState,
    RunStatus,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)
CODE = "patch_loop_maxed"
PID = f"learn-{CODE}"


def _run(repo: Path, run_id: str, *, at: datetime, flagged: bool,
         status: RunStatus = RunStatus.COMPLETED, mode: BuildMode = BuildMode.BUILD) -> None:
    """A run created at *at*; ``flagged`` makes it show the patch-loop-maxed signal."""
    plan(repo_root=repo, objective=BuildObjective(id=run_id, title="t", objective="build one"),
         run_id=run_id, mode=mode)
    paths = RunPaths(repo, run_id)
    state = load_state(paths)
    state.created_at = at
    state.status = status
    if flagged:
        state.quality = QualityLoopState(iteration=2, max_iterations=3, used_fallback=True,
                                         passed=True)
    save_state(paths, state)


def _promote(repo: Path, key) -> str:
    learning.learn(repo_root=repo)
    learning.open_review_gate(repo_root=repo, proposal_id=PID)
    _decide(repo, PID, key, "approve")
    out = learning.promote(repo_root=repo, proposal_id=PID)
    assert out["promoted"] is True
    return str(out["run_id"])


def _set(repo: Path, run_id: str, *, at: datetime, status: RunStatus) -> None:
    paths = RunPaths(repo, run_id)
    state = load_state(paths)
    state.created_at = at
    state.status = status
    save_state(paths, state)


def _effect(repo: Path):
    learning.learn(repo_root=repo)
    proposal = learning.load_proposal(repo, PID)
    assert proposal is not None
    return proposal.effect_after_promotion


# --------------------------------------------------------------------------- evidence strength


def test_rationale_states_n_of_m_and_flags_a_single_run(hw_history: Path) -> None:
    pack = learning.analyze(repo_root=hw_history)
    runs = len(list((hw_history / ".hw").glob("*/state.json")))
    p = next(x for x in pack.proposals if x.proposal_id == PID)
    assert p.runs_observed == runs
    assert f"appeared on 1 of {runs} observed run(s) (single run — may be noise)" in p.rationale
    expected = f"1 of {runs} observed run(s) — single run, may be noise"
    assert learning.evidence_strength(p) == expected
    assert f"1 of {runs} observed" in p.objective.objective


def test_recurring_signal_is_not_called_noise(tmp_path: Path) -> None:
    _run(tmp_path, "a", at=T0, flagged=True)
    _run(tmp_path, "b", at=T0 + timedelta(hours=1), flagged=True)
    _run(tmp_path, "c", at=T0 + timedelta(hours=2), flagged=False)
    p = next(x for x in learning.analyze(repo_root=tmp_path).proposals if x.proposal_id == PID)
    assert learning.evidence_strength(p) == "2 of 3 observed run(s)"
    assert "noise" not in p.rationale


def test_pack_labels_confidence_as_heuristic(hw_history: Path) -> None:
    out = learning.learn(repo_root=hw_history)
    md = Path(out["pack_md"]).read_text(encoding="utf-8")
    assert "not a probability" in md
    assert "- Evidence: 1 of" in md
    view = learning.proposals_view(repo_root=hw_history)
    first = view["proposals"][0]
    assert "not a probability" in first["confidence_basis"]
    assert "observed run(s)" in first["evidence"]


def test_review_gate_output_shows_the_evidence(hw_history: Path) -> None:
    learning.learn(repo_root=hw_history)
    out = learning.open_review_gate(repo_root=hw_history, proposal_id=PID)
    assert "single run, may be noise" in out["evidence"]
    assert "not a probability" in out["confidence_basis"]


def test_older_ledger_item_still_loads(hw_history: Path) -> None:
    learning.learn(repo_root=hw_history)
    path = hw_history / ".hw/proposals/items" / f"{PID}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for field in ("runs_observed", "effect_after_promotion", "confidence_basis"):
        data.pop(field)
    path.write_text(json.dumps(data), encoding="utf-8")
    p = learning.load_proposal(hw_history, PID)
    assert p is not None and p.runs_observed is None
    assert "not recorded" in learning.evidence_strength(p)


def test_unreadable_ledger_item_stops_learn(hw_history: Path) -> None:
    learning.learn(repo_root=hw_history)
    path = hw_history / ".hw/proposals/items" / f"{PID}.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(learning.LearningError, match="cannot be read"):
        learning.learn(repo_root=hw_history)
    assert path.read_text(encoding="utf-8") == "{not json"
    with pytest.raises(learning.LearningError):
        learning.load_proposal(hw_history, PID)


# --------------------------------------------------------------------------- after promotion


def _history_with_promotion(repo: Path, key, *, before: list[bool]) -> str:
    for i, flagged in enumerate(before):
        _run(repo, f"b{i}", at=T0 + timedelta(hours=i), flagged=flagged)
    run_id = _promote(repo, key)
    _set(repo, run_id, at=T0 + timedelta(days=1), status=RunStatus.COMPLETED)
    return run_id


def test_promoted_but_not_shipped(tmp_path: Path, approval_key) -> None:  # noqa: F811
    run_id = _history_with_promotion(tmp_path, approval_key, before=[True, True, False])
    _set(tmp_path, run_id, at=T0 + timedelta(days=1), status=RunStatus.IN_PROGRESS)
    effect = _effect(tmp_path)
    assert effect is not None and effect.outcome is PromotionOutcome.FIX_NOT_SHIPPED
    assert effect.fix_shipped is False
    md = tmp_path / ".hw/proposals" / learning.PACK_MD
    assert "Fix not shipped yet" in md.read_text(encoding="utf-8")


def test_fewer_flagged_after_the_fix_excludes_unfinished_and_review_runs(
    tmp_path: Path, approval_key,  # noqa: F811
) -> None:
    run_id = _history_with_promotion(tmp_path, approval_key, before=[True, True, True])
    later = T0 + timedelta(days=2)
    for i in range(3):
        _run(tmp_path, f"a{i}", at=later + timedelta(hours=i), flagged=False)
    # none of these may count: unfinished, REVIEW mode, same timestamp as the promotion
    _run(tmp_path, "wip", at=later, flagged=False, status=RunStatus.IN_PROGRESS)
    _run(tmp_path, "rev", at=later, flagged=False, mode=BuildMode.REVIEW)
    _run(tmp_path, "tie", at=T0 + timedelta(days=1), flagged=True)
    effect = _effect(tmp_path)
    assert effect is not None
    assert (effect.runs_before, effect.flagged_before) == (3, 3)
    assert (effect.runs_after, effect.flagged_after) == (3, 0)
    assert effect.outcome is PromotionOutcome.FEWER_FLAGGED
    assert effect.promoted_run_id == run_id
    proposal = learning.load_proposal(tmp_path, PID)
    assert proposal is not None
    assert "not proof the change caused it" in learning.effect_summary(proposal)


def test_equal_share_is_not_fewer(tmp_path: Path, approval_key) -> None:  # noqa: F811
    _history_with_promotion(tmp_path, approval_key, before=[True, False, False])
    later = T0 + timedelta(days=2)
    for i, flagged in enumerate([True, False, False]):
        _run(tmp_path, f"a{i}", at=later + timedelta(hours=i), flagged=flagged)
    effect = _effect(tmp_path)
    assert effect is not None and effect.outcome is PromotionOutcome.NOT_FEWER


def test_two_runs_after_is_too_few(tmp_path: Path, approval_key) -> None:  # noqa: F811
    _history_with_promotion(tmp_path, approval_key, before=[True, True, True])
    later = T0 + timedelta(days=2)
    for i in range(2):
        _run(tmp_path, f"a{i}", at=later + timedelta(hours=i), flagged=False)
    effect = _effect(tmp_path)
    assert effect is not None and effect.outcome is PromotionOutcome.TOO_FEW_RUNS
    assert effect.runs_after == 2


def test_promotion_effect_never_touches_decision_fields(
    tmp_path: Path, approval_key,  # noqa: F811
) -> None:
    _history_with_promotion(tmp_path, approval_key, before=[True, True, True])
    path = tmp_path / ".hw/proposals/items" / f"{PID}.json"
    before = json.loads(path.read_text(encoding="utf-8"))
    learning.learn(repo_root=tmp_path)
    after = json.loads(path.read_text(encoding="utf-8"))
    before.pop("effect_after_promotion"), after.pop("effect_after_promotion")
    assert after == before
    assert after["status"] == ProposalStatus.PROMOTED.value
    # unchanged effect ⇒ the file is not rewritten
    mtime = path.stat().st_mtime_ns
    learning.learn(repo_root=tmp_path)
    assert path.stat().st_mtime_ns == mtime


def test_missing_promoted_run_gives_no_effect(tmp_path: Path, approval_key) -> None:  # noqa: F811
    run_id = _history_with_promotion(tmp_path, approval_key, before=[True, True, True])
    (tmp_path / ".hw" / run_id / "state.json").unlink()
    assert _effect(tmp_path) is None
    proposal = learning.load_proposal(tmp_path, PID)
    assert isinstance(proposal, LearningProposal)
    assert proposal.status is ProposalStatus.PROMOTED
