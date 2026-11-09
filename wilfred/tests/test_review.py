"""Phase 6 — REVIEW mode: the load-bearing claims.

Proves: target resolution is deterministic and excludes engine/git internals (paths subset,
whole-repo, git diff range, capped-not-silent); a REVIEW run is created read-only and writes
NOTHING to the reviewed repo; the generalized critic fan-out (`hw critique-plan`) reviews the
REVIEW target instead of a build's changed paths WHILE the BUILD/IMPROVE critique path stays
byte-identical; the report synthesis groups findings by severity and derives the right verdict;
and the critique step cannot complete until the read-only critic panel has actually run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from review_attestation import record_attested

from hyperwilfred_core import engine, guardrails, quality, review
from hyperwilfred_core.pipeline import steps_for
from hyperwilfred_core.schemas import (
    BuildMode,
    BuildObjective,
    CriticFinding,
    CriticSeverity,
    CritiqueReport,
    ReviewTarget,
    ReviewTargetKind,
    ReviewVerdict,
    StepKind,
    StepStatus,
)


def _obj(oid: str = "rv") -> BuildObjective:
    return BuildObjective(id=oid, title="t", objective="audit it")


# --------------------------------------------------------------------------- target resolution


def test_resolve_whole_repo_excludes_engine_and_git(git_repo: Path) -> None:
    # A .hw artifact and a .git internal must never be reviewed.
    (git_repo / ".hw").mkdir(exist_ok=True)
    (git_repo / ".hw" / "junk.json").write_text("{}", encoding="utf-8")
    target = ReviewTarget(run_id="r", kind=ReviewTargetKind.PATHS)
    resolved = review.resolve_target(repo_root=git_repo, target=target)
    assert "README.md" in resolved.resolved_paths
    assert "src/app.py" in resolved.resolved_paths
    assert not any(p.startswith(".hw") or p.startswith(".git") for p in resolved.resolved_paths)
    assert resolved.truncated is False


def test_resolve_paths_subset_dir_glob_file(git_repo: Path) -> None:
    (git_repo / "src" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    # a directory
    by_dir = review.resolve_target(
        repo_root=git_repo, target=ReviewTarget(run_id="r", kind=ReviewTargetKind.PATHS,
                                                paths=["src"])
    )
    assert set(by_dir.resolved_paths) == {"src/app.py", "src/extra.py"}
    # a glob
    by_glob = review.resolve_target(
        repo_root=git_repo, target=ReviewTarget(run_id="r", kind=ReviewTargetKind.PATHS,
                                                paths=["src/*.py"])
    )
    assert set(by_glob.resolved_paths) == {"src/app.py", "src/extra.py"}
    # a single file
    by_file = review.resolve_target(
        repo_root=git_repo, target=ReviewTarget(run_id="r", kind=ReviewTargetKind.PATHS,
                                                paths=["README.md"])
    )
    assert by_file.resolved_paths == ["README.md"]


def test_resolve_diff_range(git_repo: Path, git_helper) -> None:
    (git_repo / "src" / "app.py").write_text("def add(a, b):\n    return a + b + 0\n",
                                             encoding="utf-8")
    git_helper(git_repo, "add", "-A")
    git_helper(git_repo, "commit", "-q", "-m", "tweak")
    resolved = review.resolve_target(
        repo_root=git_repo,
        target=ReviewTarget(run_id="r", kind=ReviewTargetKind.DIFF, diff_range="HEAD~1..HEAD"),
    )
    assert resolved.resolved_paths == ["src/app.py"]


def test_resolve_diff_requires_range_and_valid_repo(git_repo: Path) -> None:
    with pytest.raises(review.ReviewError):
        review.resolve_target(
            repo_root=git_repo,
            target=ReviewTarget(run_id="r", kind=ReviewTargetKind.DIFF, diff_range="  "),
        )
    with pytest.raises(review.ReviewError):
        review.resolve_target(
            repo_root=git_repo,
            target=ReviewTarget(run_id="r", kind=ReviewTargetKind.DIFF, diff_range="nope..nope"),
        )


def test_resolve_caps_files_but_records_truth(git_repo: Path) -> None:
    for i in range(5):
        (git_repo / f"f{i}.txt").write_text("x\n", encoding="utf-8")
    resolved = review.resolve_target(
        repo_root=git_repo,
        target=ReviewTarget(run_id="r", kind=ReviewTargetKind.PATHS, paths=["*.txt", "*.md"]),
        max_files=2,
    )
    assert len(resolved.resolved_paths) == 2
    assert resolved.total_matched > 2  # the cap is NEVER silent
    assert resolved.truncated is True


# ----------------------------------------------------------------------- run creation (read-only)


def test_create_review_is_read_only_and_persists_target(git_repo: Path) -> None:
    assert guardrails.is_clean(str(git_repo))
    out = review.create_review(
        repo_root=git_repo, objective=_obj(), kind=ReviewTargetKind.PATHS, run_id="rv1"
    )
    assert out["mode"] == "review"
    assert out["product_dir"] == "."
    assert out["first_step"]["step"]["id"] == "comprehend"
    # the target was persisted under .hw/ ...
    assert (git_repo / ".hw" / "rv1" / "review-target.json").exists()
    # ... and NOTHING was written to the reviewed repo (only .hw/ engine state).
    assert guardrails.is_clean(str(git_repo))


def test_review_mode_step_graph() -> None:
    steps = steps_for(BuildMode.REVIEW)
    assert [s.id for s in steps] == ["comprehend", "critique", "report"]
    kinds = {s.id: s.kind for s in steps}
    assert kinds["critique"] is StepKind.REVIEW
    assert kinds["report"] is StepKind.SINGLE


# --------------------------------------------------------------------------- generalized fan-out


def test_critique_plan_reviews_the_review_target(git_repo: Path) -> None:
    review.create_review(
        repo_root=git_repo, objective=_obj(), kind=ReviewTargetKind.PATHS, run_id="rv1",
        paths=["src/app.py"],
    )
    out = quality.critique_plan(repo_root=git_repo, run_id="rv1")
    assert len(out["critics"]) == len(quality.CRITIC_LENSES)
    # Every critic reviews the RESOLVED REVIEW TARGET, not git-status changed paths.
    for critic in out["critics"]:
        assert critic["changed_paths"] == ["src/app.py"]


def test_build_critique_path_unchanged(git_repo: Path) -> None:
    """Regression guard: with no review-target.json the critic fan-out reviews changed paths."""
    obj = _obj()
    engine.plan(repo_root=git_repo, objective=obj, run_id="b1", product_dir="src",
                mode=BuildMode.BUILD)
    # a BUILD run never has a review-target.json — modify a file so there ARE changed paths.
    (git_repo / "src" / "app.py").write_text("def add(a, b):\n    return a + b + 1\n",
                                             encoding="utf-8")
    assert not (git_repo / ".hw" / "b1" / "review-target.json").exists()
    out = quality.critique_plan(repo_root=git_repo, run_id="b1")
    changed = out["critics"][0]["changed_paths"]
    assert "src/app.py" in changed  # the build's changed path, exactly as before Phase 6


# --------------------------------------------------------------------------- report synthesis


def _seed_critics(
    repo: Path, run_id: str, findings_by_lens: dict[str, list[CriticFinding]],
    *, independent: bool = True,
) -> None:
    """Write one CritiqueReport per lens via the reused `record-critique` ingest.

    By default each critique is recorded through the signed independent-review boundary, so the
    verdict tests below describe a genuine independent review; ``independent=False`` records the
    orchestrating session's own (adapter) critiques instead.
    """
    review.create_review(
        repo_root=repo, objective=_obj(), kind=ReviewTargetKind.PATHS, run_id=run_id,
        paths=["src/app.py"],
    )
    quality.critique_plan(repo_root=repo, run_id=run_id)
    out_dir = repo / ".hw" / run_id / "quality/iter-0/critics"
    out_dir.mkdir(parents=True, exist_ok=True)
    for lens, findings in findings_by_lens.items():
        report = CritiqueReport(critic="c", lens=lens, findings=findings)
        rf = out_dir / f"{lens}.json"
        rf.write_text(report.model_dump_json(), encoding="utf-8")
        kwargs = {"repo_root": repo, "run_id": run_id, "packet_id": f"critic-0-{lens}",
                  "result_file": str(rf)}
        if independent:
            record_attested(quality.record_critique, repo=repo, **kwargs)
        else:
            quality.record_critique(**kwargs)


def _finding(lens: str, sev: CriticSeverity) -> CriticFinding:
    return CriticFinding(id=f"{lens}1", lens=lens, severity=sev, file="src/app.py", line=2,
                         title=f"{lens} issue")


def test_report_changes_requested_on_major(git_repo: Path) -> None:
    _seed_critics(git_repo, "rv1", {
        "correctness": [_finding("correctness", CriticSeverity.MAJOR)],
        "security": [], "maintainability": [_finding("maintainability", CriticSeverity.MINOR)],
        "tests": [],
    })
    out = review.review_report(repo_root=git_repo, run_id="rv1")
    assert out["verdict"] == ReviewVerdict.CHANGES_REQUESTED.value
    assert out["findings"] == 2
    assert out["counts_by_severity"]["major"] == 1
    assert out["files_reviewed"] == 1
    assert (git_repo / ".hw" / "rv1" / "review-report.json").exists()
    assert (git_repo / ".hw" / "rv1" / "review-report.md").exists()
    # synthesis is read-only on the repo
    assert guardrails.is_clean(str(git_repo))


def test_report_advisory_on_minor_only(git_repo: Path) -> None:
    _seed_critics(git_repo, "rv1", {
        "correctness": [], "security": [],
        "maintainability": [_finding("maintainability", CriticSeverity.MINOR)], "tests": [],
    })
    out = review.review_report(repo_root=git_repo, run_id="rv1")
    assert out["verdict"] == ReviewVerdict.ADVISORY.value


def test_report_pass_when_clean(git_repo: Path) -> None:
    _seed_critics(git_repo, "rv1", {
        "correctness": [], "security": [], "maintainability": [], "tests": [],
    })
    out = review.review_report(repo_root=git_repo, run_id="rv1")
    assert out["verdict"] == ReviewVerdict.PASS.value
    assert out["findings"] == 0


def test_self_reviewed_clean_panel_is_not_pass(git_repo: Path) -> None:
    """The same clean panel, written by the orchestrating session, never reads as `pass`."""
    _seed_critics(git_repo, "rv1", {
        "correctness": [], "security": [], "maintainability": [], "tests": [],
    }, independent=False)
    out = review.review_report(repo_root=git_repo, run_id="rv1")
    assert out["verdict"] == ReviewVerdict.PENDING_INDEPENDENT_REVIEW.value
    assert out["severity_verdict"] == ReviewVerdict.PASS.value
    assert out["independent"] is False


# --------------------------------------------------------------------------- completion guards


def test_review_report_refuses_without_critics(git_repo: Path) -> None:
    review.create_review(
        repo_root=git_repo, objective=_obj(), kind=ReviewTargetKind.PATHS, run_id="rv1"
    )
    with pytest.raises(review.ReviewError):
        review.review_report(repo_root=git_repo, run_id="rv1")


def test_complete_critique_requires_recorded_critics(git_repo: Path) -> None:
    review.create_review(
        repo_root=git_repo, objective=_obj(), kind=ReviewTargetKind.PATHS, run_id="rv1"
    )
    paths = engine.RunPaths(git_repo, "rv1")
    # advance past comprehend
    (git_repo / ".hw" / "rv1" / "comprehension.md").write_text("ok\n", encoding="utf-8")
    engine.complete_step(repo_root=git_repo, run_id="rv1", step_id="comprehend")
    # critique cannot complete with no critic findings recorded
    with pytest.raises(review.ReviewError):
        engine.complete_step(repo_root=git_repo, run_id="rv1", step_id="critique")
    # one recorded lens is still incomplete
    quality.critique_plan(repo_root=git_repo, run_id="rv1")
    out_dir = git_repo / ".hw" / "rv1" / "quality/iter-0/critics"
    out_dir.mkdir(parents=True, exist_ok=True)
    first_lens = quality.CRITIC_LENSES[0]
    first = out_dir / f"{first_lens}.json"
    first.write_text(
        CritiqueReport(critic="c", lens=first_lens).model_dump_json(),
        encoding="utf-8",
    )
    quality.record_critique(
        repo_root=git_repo, run_id="rv1", packet_id=f"critic-0-{first_lens}",
        result_file=str(first),
    )
    with pytest.raises(review.ReviewError, match="incomplete critic panel"):
        engine.complete_step(repo_root=git_repo, run_id="rv1", step_id="critique")

    # after all four critics run, it completes
    for lens in quality.CRITIC_LENSES[1:]:
        rf = out_dir / f"{lens}.json"
        rf.write_text(CritiqueReport(critic="c", lens=lens).model_dump_json(), encoding="utf-8")
        quality.record_critique(repo_root=git_repo, run_id="rv1", packet_id=f"critic-0-{lens}",
                                result_file=str(rf))
    res = engine.complete_step(repo_root=git_repo, run_id="rv1", step_id="critique")
    assert res["completed_step"] == "critique"
    state = engine.load_state(paths)
    assert state.step_status["critique"] is StepStatus.DONE


# --------------------------------------------------------------------------- end-to-end read-only


def test_end_to_end_review_writes_nothing_to_repo(git_repo: Path) -> None:
    review.create_review(
        repo_root=git_repo, objective=_obj(), kind=ReviewTargetKind.PATHS, run_id="rv1"
    )
    (git_repo / ".hw" / "rv1" / "comprehension.md").write_text("note\n", encoding="utf-8")
    engine.complete_step(repo_root=git_repo, run_id="rv1", step_id="comprehend")
    quality.critique_plan(repo_root=git_repo, run_id="rv1")
    out_dir = git_repo / ".hw" / "rv1" / "quality/iter-0/critics"
    out_dir.mkdir(parents=True, exist_ok=True)
    for lens in quality.CRITIC_LENSES:
        f = [_finding(lens, CriticSeverity.MINOR)] if lens == "tests" else []
        rf = out_dir / f"{lens}.json"
        rf.write_text(CritiqueReport(critic="c", lens=lens, findings=f).model_dump_json(),
                      encoding="utf-8")
        quality.record_critique(repo_root=git_repo, run_id="rv1", packet_id=f"critic-0-{lens}",
                                result_file=str(rf))
    engine.complete_step(repo_root=git_repo, run_id="rv1", step_id="critique")
    review.review_report(repo_root=git_repo, run_id="rv1")
    engine.complete_step(repo_root=git_repo, run_id="rv1", step_id="report")
    nxt = engine.next_step(repo_root=git_repo, run_id="rv1")
    assert nxt.get("done") is True
    # THE invariant: a full review left the reviewed repo byte-clean (only .hw/ engine state).
    assert guardrails.is_clean(str(git_repo))


def test_resolve_target_treats_existing_bracket_paths_literally(tmp_path: Path) -> None:
    """A declared Next.js file like ``[projectId]/page.tsx`` is that file, not a glob that picks
    a one-character sibling folder (``p/page.tsx``)."""
    page = "app/(studio)/projects/[projectId]/page.tsx"
    route_dir = "app/api/v1/projects/[project-id]"
    for rel in (page, "app/(studio)/projects/p/page.tsx", f"{route_dir}/route.ts"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("export {}\n", encoding="utf-8")

    resolved = review.resolve_target(
        repo_root=tmp_path,
        target=ReviewTarget(run_id="r", kind=ReviewTargetKind.PATHS, paths=[page, route_dir]),
    )
    assert resolved.resolved_paths == [page, f"{route_dir}/route.ts"]

    globbed = review.resolve_target(
        repo_root=tmp_path,
        target=ReviewTarget(
            run_id="r", kind=ReviewTargetKind.PATHS, paths=["app/(studio)/projects/*/page.tsx"]
        ),
    )
    assert globbed.resolved_paths == ["app/(studio)/projects/[projectId]/page.tsx",
                                      "app/(studio)/projects/p/page.tsx"]


def test_diff_range_cannot_smuggle_a_git_option(git_repo: Path, tmp_path: Path) -> None:
    """``--output=<path>`` as a "range" made git write a file wherever the caller pointed (F-07)."""
    sink = tmp_path / "injected.txt"
    with pytest.raises(review.ReviewError, match="starts with '-'"):
        review.resolve_target(
            repo_root=git_repo,
            target=ReviewTarget(
                run_id="r", kind=ReviewTargetKind.DIFF, diff_range=f"--output={sink}"
            ),
        )
    assert not sink.exists()
