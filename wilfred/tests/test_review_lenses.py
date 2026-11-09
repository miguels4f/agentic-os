"""REVIEW opt-in lenses + critique authorship in the REVIEW report.

Proves: a REVIEW run keeps exactly the four standard lenses by default; ``hw review --lens``
adds only the opt-in lenses (``cross-slice-drift``, ``ai-failure-modes``), deduplicated, never
dropping a standard lens and never admitting ``adversarial``; completion requires exactly the
run's own panel; neither opt-in lens is switched on automatically; and the REVIEW report says
who wrote each critique and never reports ``pass``/``advisory`` for a self-reviewed panel.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from review_attestation import record_attested
from test_quality import _record_subjective, _run_at_quality

from hyperwilfred_core import cli, engine, pipeline, quality, review
from hyperwilfred_core.schemas import (
    BuildMode,
    BuildObjective,
    CriticFinding,
    CriticSeverity,
    CritiqueReport,
    ReviewTargetKind,
    ReviewVerdict,
    RunState,
    RunStatus,
    Tier,
)

DRIFT = quality.CROSS_SLICE_DRIFT_LENS
AI = quality.AI_FAILURE_MODES_LENS


def _obj(text: str = "audit it", **kw: object) -> BuildObjective:
    return BuildObjective(id="rv", title="t", objective=text, **kw)  # type: ignore[arg-type]


def _state(objective: BuildObjective, mode: BuildMode) -> RunState:
    return RunState(
        run_id="r", mode=mode, tier=next(iter(Tier)),
        status=RunStatus.CREATED, objective=objective, product_dir="p", steps=[],
    )


def _create(repo: Path, lenses: list[str] | None = None, **kw: object) -> dict:
    return review.create_review(
        repo_root=repo, objective=_obj(), kind=ReviewTargetKind.PATHS, run_id="rv1",
        paths=["src/app.py"], lenses=lenses, **kw,  # type: ignore[arg-type]
    )


def _record(repo: Path, lens: str, findings: list[CriticFinding] | None = None,
            *, independent: bool) -> Path:
    out_dir = repo / ".hw" / "rv1" / "quality/iter-0/critics"
    out_dir.mkdir(parents=True, exist_ok=True)
    rf = out_dir / f"{lens}.json"
    rf.write_text(CritiqueReport(critic="c", lens=lens, findings=findings or []).model_dump_json(),
                  encoding="utf-8")
    kwargs = {"repo_root": repo, "run_id": "rv1", "packet_id": f"critic-0-{lens}",
              "result_file": str(rf)}
    if independent:
        record_attested(quality.record_critique, repo=repo, **kwargs)
    else:
        quality.record_critique(**kwargs)
    return rf


def _panel(repo: Path, lenses: tuple[str, ...], *, independent: bool,
           findings: dict[str, list[CriticFinding]] | None = None) -> None:
    quality.critique_plan(repo_root=repo, run_id="rv1")
    for lens in lenses:
        _record(repo, lens, (findings or {}).get(lens), independent=independent)


def _finding(lens: str, sev: CriticSeverity) -> CriticFinding:
    return CriticFinding(id=f"{lens}1", lens=lens, severity=sev, file="src/app.py", line=2,
                         title=f"{lens} issue")


# --------------------------------------------------------------------------- lens selection


def test_default_review_panel_is_the_four_standard_lenses(git_repo: Path) -> None:
    out = _create(git_repo)
    assert tuple(out["lenses"]) == quality.CRITIC_LENSES
    assert out["warnings"] == []
    state = engine.load_state(engine.RunPaths(git_repo, "rv1"))
    assert state.objective.critic_lenses == []  # default objective untouched


def test_lens_flag_adds_an_opt_in_lens_with_the_shared_instruction(git_repo: Path) -> None:
    out = _create(git_repo, [DRIFT])
    assert tuple(out["lenses"]) == (*quality.CRITIC_LENSES, DRIFT)
    plan = quality.critique_plan(repo_root=git_repo, run_id="rv1")
    assert [c["lens"] for c in plan["critics"]] == [*quality.CRITIC_LENSES, DRIFT]
    standard = pipeline.load_instruction("quality/02_critique.md")
    for critic in plan["critics"]:
        assert critic["changed_paths"] == ["src/app.py"]
        assert critic["instruction"] == standard


def test_both_opt_in_lenses_deduplicated_in_request_order(git_repo: Path) -> None:
    out = _create(git_repo, [AI, DRIFT, AI, "tests"])
    assert tuple(out["lenses"]) == (*quality.CRITIC_LENSES, AI, DRIFT)


@pytest.mark.parametrize("bad", ["adversarial", "vibes"])
def test_non_optional_extra_lens_is_refused_before_the_run_exists(
    git_repo: Path, bad: str
) -> None:
    with pytest.raises(review.ReviewError, match="optional lens"):
        _create(git_repo, [bad])
    assert not (git_repo / ".hw" / "rv1").exists()


def test_review_never_drops_a_standard_lens() -> None:
    o = _obj(critic_lenses=["correctness"])
    assert quality.resolve_lenses(_state(o, BuildMode.REVIEW)) == quality.CRITIC_LENSES


def test_hand_edited_review_state_with_adversarial_fails_closed(git_repo: Path) -> None:
    _create(git_repo)
    paths = engine.RunPaths(git_repo, "rv1")
    state = engine.load_state(paths)
    state.objective.critic_lenses = [*quality.CRITIC_LENSES, "adversarial"]
    engine.save_state(paths, state)
    with pytest.raises(quality.QualityError, match="refused"):
        quality.resolve_lenses(state)
    with pytest.raises(review.ReviewError, match="refused"):
        review.assert_critics_recorded(repo_root=git_repo, run_id="rv1")


def test_opt_in_lenses_are_never_automatic() -> None:
    text = "LLM tool call with an api token from env; drift between slices since rc2"
    lenses = quality.resolve_lenses(_state(_obj(text), BuildMode.BUILD))
    assert DRIFT not in lenses and AI not in lenses
    assert quality.resolve_lenses(_state(_obj(text), BuildMode.REVIEW)) == quality.CRITIC_LENSES


def test_build_objective_may_name_an_opt_in_lens() -> None:
    o = _obj("plain task", critic_lenses=[*quality.CRITIC_LENSES, AI])
    assert quality.resolve_lenses(_state(o, BuildMode.BUILD)) == (*quality.CRITIC_LENSES, AI)


def test_drift_lens_warns_on_diff_target(git_repo: Path) -> None:
    out = review.create_review(
        repo_root=git_repo, objective=_obj(), kind=ReviewTargetKind.DIFF, run_id="rv1",
        diff_range="HEAD", lenses=[DRIFT],
    )
    assert any("--target-kind paths" in w for w in out["warnings"])


def test_drift_lens_warns_on_truncated_target(git_repo: Path) -> None:
    (git_repo / "src" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    out = review.create_review(
        repo_root=git_repo, objective=_obj(), kind=ReviewTargetKind.PATHS, run_id="rv1",
        paths=["src"], max_files=1, lenses=[DRIFT],
    )
    assert any("truncated" in w for w in out["warnings"])


# --------------------------------------------------------------------------- completion


def test_completion_requires_the_requested_opt_in_lens(git_repo: Path) -> None:
    _create(git_repo, [DRIFT])
    _panel(git_repo, quality.CRITIC_LENSES, independent=False)
    with pytest.raises(review.ReviewError, match=r"missing=\['cross-slice-drift'\]"):
        review.assert_critics_recorded(repo_root=git_repo, run_id="rv1")
    _record(git_repo, DRIFT, independent=False)
    assert len(review.assert_critics_recorded(repo_root=git_repo, run_id="rv1")) == 5


def test_lens_removed_from_state_after_recording_is_unexpected(git_repo: Path) -> None:
    _create(git_repo, [DRIFT])
    _panel(git_repo, (*quality.CRITIC_LENSES, DRIFT), independent=False)
    paths = engine.RunPaths(git_repo, "rv1")
    state = engine.load_state(paths)
    state.objective.critic_lenses = []
    engine.save_state(paths, state)
    with pytest.raises(review.ReviewError, match=r"unexpected=\['cross-slice-drift'\]"):
        review.assert_critics_recorded(repo_root=git_repo, run_id="rv1")


def test_review_panel_must_be_iteration_zero(git_repo: Path) -> None:
    _create(git_repo)
    _panel(git_repo, quality.CRITIC_LENSES, independent=False)
    paths = engine.RunPaths(git_repo, "rv1")
    state = engine.load_state(paths)
    assert state.quality is not None
    state.quality.iteration = 1
    engine.save_state(paths, state)
    with pytest.raises(review.ReviewError, match="iteration 0"):
        review.assert_critics_recorded(repo_root=git_repo, run_id="rv1")


def test_review_report_refuses_non_review_mode_state(git_repo: Path) -> None:
    """Flipping the run mode cannot shrink the panel to one signed lens."""
    _create(git_repo)
    _panel(git_repo, ("correctness",), independent=True)
    paths = engine.RunPaths(git_repo, "rv1")
    state = engine.load_state(paths)
    state.mode = BuildMode.BUILD
    state.objective.critic_lenses = ["correctness"]
    engine.save_state(paths, state)
    with pytest.raises(review.ReviewError, match="not a REVIEW run"):
        review.review_report(repo_root=git_repo, run_id="rv1")


def test_stray_file_claiming_a_lens_is_not_counted(git_repo: Path) -> None:
    """Only ``<lens>.json`` is a record: a stray copy cannot borrow a signed lens's authorship."""
    _create(git_repo)
    _panel(git_repo, quality.CRITIC_LENSES, independent=True)
    extra = git_repo / ".hw/rv1/quality/iter-0/critics/zzz.json"
    extra.write_text(CritiqueReport(critic="x", lens="security", findings=[
        _finding("security", CriticSeverity.MINOR)]).model_dump_json(), encoding="utf-8")
    out = review.review_report(repo_root=git_repo, run_id="rv1")
    assert out["findings"] == 0
    assert out["verdict"] == ReviewVerdict.PASS.value


def test_report_reapplies_the_severity_floor(git_repo: Path) -> None:
    _create(git_repo)
    _panel(git_repo, quality.CRITIC_LENSES, independent=False)
    rf = git_repo / ".hw/rv1/quality/iter-0/critics/security.json"
    leak = CriticFinding(id="s1", lens="security", severity=CriticSeverity.MINOR,
                         file="src/app.py", title="export leaks emails",
                         tags=["personal_data_leak"])
    rf.write_text(CritiqueReport(critic="c", lens="security", findings=[leak]).model_dump_json(),
                  encoding="utf-8")
    out = review.review_report(repo_root=git_repo, run_id="rv1")
    assert out["verdict"] == ReviewVerdict.CHANGES_REQUESTED.value
    assert out["counts_by_severity"]["major"] == 1


# --------------------------------------------------------------------------- authorship


def test_independent_clean_panel_with_opt_in_lens_passes(git_repo: Path) -> None:
    _create(git_repo, [AI])
    _panel(git_repo, (*quality.CRITIC_LENSES, AI), independent=True)
    out = review.review_report(repo_root=git_repo, run_id="rv1")
    assert out["verdict"] == ReviewVerdict.PASS.value
    assert out["independent"] is True
    assert set(out["critic_authors"].values()) == {"independent"}
    md = (git_repo / ".hw/rv1/review-report.md").read_text(encoding="utf-8")
    assert "NOT AN INDEPENDENT REVIEW" not in md
    assert f"| {AI} | independent critic" in md


def test_self_reviewed_minor_only_panel_is_pending_not_advisory(git_repo: Path) -> None:
    _create(git_repo)
    _panel(git_repo, quality.CRITIC_LENSES, independent=False,
           findings={"tests": [_finding("tests", CriticSeverity.MINOR)]})
    out = review.review_report(repo_root=git_repo, run_id="rv1")
    assert out["verdict"] == ReviewVerdict.PENDING_INDEPENDENT_REVIEW.value
    assert out["severity_verdict"] == ReviewVerdict.ADVISORY.value
    assert out["summary"].startswith("NOT AN INDEPENDENT REVIEW — 4 of 4")
    data = json.loads((git_repo / ".hw/rv1/review-report.json").read_text(encoding="utf-8"))
    assert data["verdict"] == "pending_independent_review"
    assert data["critic_authors"] == dict.fromkeys(quality.CRITIC_LENSES, "adapter")
    md = (git_repo / ".hw/rv1/review-report.md").read_text(encoding="utf-8")
    assert "NOT AN INDEPENDENT REVIEW" in md
    assert "orchestrating session (self-review)" in md
    assert "_(self-review)_" in md


def test_self_review_with_major_keeps_changes_requested(git_repo: Path) -> None:
    _create(git_repo)
    _panel(git_repo, quality.CRITIC_LENSES, independent=False,
           findings={"correctness": [_finding("correctness", CriticSeverity.MAJOR)]})
    out = review.review_report(repo_root=git_repo, run_id="rv1")
    assert out["verdict"] == ReviewVerdict.CHANGES_REQUESTED.value
    assert out["independent"] is False
    assert "NOT AN INDEPENDENT REVIEW" in out["summary"]


def test_one_self_reviewed_lens_blocks_pass(git_repo: Path) -> None:
    _create(git_repo, [DRIFT])
    _panel(git_repo, quality.CRITIC_LENSES, independent=True)
    _record(git_repo, DRIFT, independent=False)
    out = review.review_report(repo_root=git_repo, run_id="rv1")
    assert out["verdict"] == ReviewVerdict.PENDING_INDEPENDENT_REVIEW.value
    assert out["critic_authors"][DRIFT] == "adapter"
    assert out["summary"].startswith("NOT AN INDEPENDENT REVIEW — 1 of 5")


def test_tampered_attested_critique_counts_as_self_review(git_repo: Path) -> None:
    _create(git_repo)
    _panel(git_repo, quality.CRITIC_LENSES, independent=True)
    rf = git_repo / ".hw/rv1/quality/iter-0/critics/security.json"
    data = json.loads(rf.read_text(encoding="utf-8"))
    data["critic"] = "edited after attestation"
    rf.write_text(json.dumps(data), encoding="utf-8")
    out = review.review_report(repo_root=git_repo, run_id="rv1")
    assert out["critic_authors"]["security"] == "adapter"
    assert out["verdict"] == ReviewVerdict.PENDING_INDEPENDENT_REVIEW.value


def test_independent_claim_without_signed_attestation_is_self_review(git_repo: Path) -> None:
    _create(git_repo)
    quality.critique_plan(repo_root=git_repo, run_id="rv1")
    for lens in quality.CRITIC_LENSES:
        rf = git_repo / ".hw/rv1/quality/iter-0/critics" / f"{lens}.json"
        rf.parent.mkdir(parents=True, exist_ok=True)
        rf.write_text(CritiqueReport(critic="c", lens=lens).model_dump_json(), encoding="utf-8")
        quality.record_critique(repo_root=git_repo, run_id="rv1", packet_id=f"critic-0-{lens}",
                                result_file=str(rf), author="independent")
    out = review.review_report(repo_root=git_repo, run_id="rv1")
    assert set(out["critic_authors"].values()) == {"adapter"}
    assert out["verdict"] == ReviewVerdict.PENDING_INDEPENDENT_REVIEW.value


def test_critic_authorship_matches_quality_loop_provenance(tmp_path: Path) -> None:
    """The extracted attestation check gives the BUILD quality loop the same answer as before."""
    _run_at_quality(tmp_path, threshold=4)
    _record_subjective(tmp_path, "q", [5, 5, 5, 5, 5])
    quality.critique_plan(repo_root=tmp_path, run_id="q")
    critics = tmp_path / ".hw/q/quality/iter-0/critics"
    critics.mkdir(parents=True, exist_ok=True)
    for i, lens in enumerate(quality.CRITIC_LENSES):
        rf = critics / f"{lens}.json"
        rf.write_text(CritiqueReport(critic="c", lens=lens).model_dump_json(), encoding="utf-8")
        kwargs = {"repo_root": tmp_path, "run_id": "q", "packet_id": f"critic-0-{lens}",
                  "result_file": str(rf)}
        if i % 2 == 0:
            record_attested(quality.record_critique, repo=tmp_path, **kwargs)
        else:
            quality.record_critique(**kwargs)
    state = engine.load_state(engine.RunPaths(tmp_path, "q"))
    prov = quality.provenance_report(repo_root=tmp_path, run_id="q", state=state)
    authors = quality.critic_authorship(repo_root=tmp_path, run_id="q", state=state)
    assert sorted(k for k, v in authors.items() if v == "independent") == sorted(
        lens for lens in prov["independent"] if lens != "subjective")
    assert sorted(k for k, v in authors.items() if v == "adapter") == sorted(
        prov["adapter_authored"])
    assert sorted(prov["adapter_authored"]) == sorted(quality.CRITIC_LENSES[1::2])


def test_critic_authorship_without_quality_state_is_missing_and_read_only(
    git_repo: Path,
) -> None:
    _create(git_repo)
    paths = engine.RunPaths(git_repo, "rv1")
    state = engine.load_state(paths)
    assert state.quality is None
    authors = quality.critic_authorship(repo_root=git_repo, run_id="rv1", state=state)
    assert authors == dict.fromkeys(quality.CRITIC_LENSES, "missing")
    assert engine.load_state(paths).quality is None


# --------------------------------------------------------------------------- CLI


def test_cli_lens_flag_and_report_output(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["--repo", str(git_repo), "review", "--run-id", "rv1",
                     "--path", "src/app.py", "--lens", DRIFT, "--lens", DRIFT])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["lenses"] == [*quality.CRITIC_LENSES, DRIFT]
    _panel(git_repo, (*quality.CRITIC_LENSES, DRIFT), independent=False)
    capsys.readouterr()
    assert cli.main(["--repo", str(git_repo), "review-report", "--run-id", "rv1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["independent"] is False
    assert report["critic_authors"][DRIFT] == "adapter"
    assert report["verdict"] == "pending_independent_review"


def test_cli_refuses_a_lens_outside_the_optional_set(git_repo: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--repo", str(git_repo), "review", "--lens", "adversarial"])
    assert exc.value.code == 2
