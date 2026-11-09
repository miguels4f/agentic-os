"""Regression tests for the independent review's findings (M2, M3, m5, m6)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from hyperwilfred_core import guardrails, plan_lint, reproducibility, severity_floor, traceability
from hyperwilfred_core.schemas import (
    BuildObjective,
    CriticFinding,
    CriticSeverity,
    GeneratedArtifact,
    Plan,
    RiskLevel,
    Slice,
)


@pytest.mark.parametrize(
    "path",
    [
        "**/*.json", "**/*.yml", "*.txt", "src/**/x.json", "x/../y.json", "./a.json",
        "/etc/passwd.json", "../out.json", "~/a.json", "C:/a.json", "*.json",
    ],
)
def test_m2_broad_relative_or_escaping_generated_paths_are_refused(path: str) -> None:
    with pytest.raises(ValidationError):
        GeneratedArtifact(path=path, command="make x")


@pytest.mark.parametrize("path", ["snap/accounts.json", "site/data/*.json", "reports/out.csv"])
def test_m2_specific_paths_are_accepted(path: str) -> None:
    assert GeneratedArtifact(path=path, command="make x").path == path


def test_m2_exemption_is_revalidated_against_the_packet_scope() -> None:
    kept = guardrails.effective_generated_globs(
        ["snap/a.json", "other/b.json"], ["snap/**"]
    )
    assert kept == ["snap/a.json"]


def test_m2_uncaught_glob_error_becomes_a_failed_result(tmp_path: Path) -> None:
    art = GeneratedArtifact.model_construct(path="/abs/x.json", command="true", volatile_fields=[])
    res = reproducibility._check_one(tmp_path, art, 5)
    assert not res.ok and "unusable artifact path" in res.detail


def test_m2_slice_level_artifacts_are_verified_too(git_repo: Path) -> None:
    from hyperwilfred_core import engine, verify_checks

    (git_repo / "snap").mkdir(exist_ok=True)
    (git_repo / "snap" / "a.json").write_text('{"v": 1}', encoding="utf-8")
    subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-qm", "s"], check=True)
    obj = BuildObjective(id="o", title="t", objective="x")
    engine.plan(repo_root=git_repo, objective=obj, run_id="s", product_dir="snap")
    paths = engine.RunPaths(git_repo, "s")
    plan = Plan(objective_id="o", product_dir="snap", slices=[Slice(
        id="s", title="t", allowed_globs=["snap/**"],
        generated_artifacts=[GeneratedArtifact(path="snap/a.json", command="python3 -c pass")],
    )])
    paths.plan.write_text(plan.model_dump_json(), encoding="utf-8")
    # a no-op "generator" cannot reproduce... it leaves bytes equal, so this passes; the point is
    # that a FAILING command is now caught for slice-level declarations
    bad = plan.model_copy(deep=True)
    bad.slices[0].generated_artifacts[0].command = "python3 -c 'raise SystemExit(2)'"
    paths.plan.write_text(bad.model_dump_json(), encoding="utf-8")
    report = verify_checks.run(repo_root=git_repo, run_id="s")
    assert not report["ok"] and "not reproducible" in report["blocking"][0]


def _tr(criteria: list[str], **kw: object) -> traceability.TraceabilityReport:
    obj = BuildObjective(
        id="o", title="t", objective="build B2.1 and D1", acceptance_criteria=criteria,
        **kw,  # type: ignore[arg-type]
    )
    return traceability.analyse(run_id="r", objective=obj, plan=None, repo_root=Path("."))


def test_m3_requirement_containing_the_words_out_of_scope_is_still_a_requirement() -> None:
    rep = _tr(["B2.1: reject requests for out-of-scope accounts", "D1 is not out of scope"])
    status = {e.id: e.status for e in rep.ids}
    assert status == {"B2.1": "covered", "D1": "covered"}


def test_m3_marker_at_line_start_still_declares_out_of_scope() -> None:
    rep = _tr(["Out of scope: B2.1", "D1 handled"])
    assert {e.id: e.status for e in rep.ids} == {"B2.1": "out_of_scope", "D1": "covered"}


def test_m5_negated_statement_is_not_raised_and_more_leaks_are_caught() -> None:
    def sev(title: str) -> CriticSeverity:
        f = CriticFinding(id="x", lens="tests", severity=CriticSeverity.MINOR, title=title)
        return severity_floor.floor_finding(f).severity

    assert sev("Verified: no divergence between UI and engine") is CriticSeverity.MINOR
    assert sev("Does not leak any personal data") is CriticSeverity.MINOR
    for t in (
        "Leaks the customer's full name", "Exposes user email in logs",
        "Reveals the phone numbers of accounts", "Button is enabled while the engine refuses it",
    ):
        assert sev(t) is CriticSeverity.MAJOR, t


def test_m6_benign_wording_is_not_flagged_and_synonyms_are() -> None:
    def flagged(text: str) -> bool:
        plan = Plan(objective_id="o", product_dir="p",
                    slices=[Slice(id="s", title="t", description=text, allowed_globs=["p/**"])])
        obj = BuildObjective(id="o", title="t", objective="x", risk_level=RiskLevel.HIGH)
        return plan_lint.lint(plan, obj).blocking

    for ok in (
        "Reject duplicate email addresses during validation",
        "Mirror the API docs check into the README",
        "Copy the config file and run the checks",
    ):
        assert not flagged(ok), ok
    for bad in (
        "Recalculate the allowed state on the client side",
        "Re-encode the gating rules in JS",
        "Recompute the allowed/blocked state in TypeScript",
    ):
        assert flagged(bad), bad
