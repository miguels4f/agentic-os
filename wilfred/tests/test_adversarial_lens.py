"""L2 — the adversarial critic lens: configurable, default-on when sensitive, evidence-only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyperwilfred_core import engine, quality
from hyperwilfred_core.schemas import (
    BuildMode,
    BuildObjective,
    CriticFinding,
    CriticSeverity,
    CritiqueProbe,
    CritiqueReport,
    ReviewAuthor,
    RunState,
    RunStatus,
    Tier,
)


def _state(objective: BuildObjective, mode: BuildMode = BuildMode.BUILD) -> RunState:
    return RunState(
        run_id="r", mode=mode, tier=next(iter(Tier)),
        status=RunStatus.CREATED, objective=objective, product_dir="p", steps=[],
    )


def _obj(text: str, **kw: object) -> BuildObjective:
    return BuildObjective(id="o", title="t", objective=text, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    [
        "build a cockpit that scrubs personal data from snapshots",
        "every action is gated by the engine",
        "account actions need human approval before they run",
        "harden the security of the export",
        "never contact accounts flagged do-not-contact",
    ],
)
def test_adversarial_lens_is_default_on_for_sensitive_objectives(text: str) -> None:
    assert quality.resolve_lenses(_state(_obj(text))) == (*quality.CRITIC_LENSES, "adversarial")


def test_adversarial_lens_is_off_for_a_plain_objective() -> None:
    assert quality.resolve_lenses(_state(_obj("add a CSV export button"))) == quality.CRITIC_LENSES


def test_criteria_and_constraints_also_trigger_it() -> None:
    o = _obj("add a report page", acceptance_criteria=["personal data never shown"])
    assert "adversarial" in quality.resolve_lenses(_state(o))


def test_lens_set_is_configurable_per_objective() -> None:
    o = _obj("a cockpit with personal data", critic_lenses=["correctness", "tests"])
    assert quality.resolve_lenses(_state(o)) == ("correctness", "tests")
    o2 = _obj("plain task", critic_lenses=["correctness", "adversarial"])
    assert quality.resolve_lenses(_state(o2)) == ("correctness", "adversarial")


def test_unknown_configured_lens_is_refused() -> None:
    with pytest.raises(quality.QualityError, match="unknown lens"):
        quality.resolve_lenses(_state(_obj("x", critic_lenses=["vibes"])))


def test_review_runs_keep_the_four_standard_lenses() -> None:
    o = _obj("audit personal data handling")
    assert quality.resolve_lenses(_state(o, BuildMode.REVIEW)) == quality.CRITIC_LENSES


def _sensitive_run(repo: Path) -> None:
    obj = _obj("cockpit that scrubs personal data and enforces approval gates")
    engine.plan(repo_root=repo, objective=obj, run_id="a", product_dir="product")
    paths = engine.RunPaths(repo, "a")
    state = engine.load_state(paths)
    state.quality = quality._ensure_quality(state)
    engine.save_state(paths, state)


def _adversarial_report(**overrides: object) -> CritiqueReport:
    base: dict = {
        "critic": "adv",
        "author": ReviewAuthor.INDEPENDENT,
        "lens": "adversarial",
        "probes": [CritiqueProbe(
            input_class="privacy: lowercase surname",
            command="grep -ri garcia out/", observed_output="out/garcia.json: match",
        )],
        "findings": [],
    }
    base.update(overrides)
    return CritiqueReport(**base)


def _record(repo: Path, report: CritiqueReport) -> dict:
    quality.critique_plan(repo_root=repo, run_id="a")
    f = repo / ".hw/a/quality/iter-0/critics/adversarial.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(report.model_dump_json(), encoding="utf-8")
    return quality.record_critique(
        repo_root=repo, run_id="a", packet_id="critic-0-adversarial", result_file=str(f),
        author="independent",
    )


def test_critique_plan_emits_an_adversarial_packet_with_its_own_instruction(
    tmp_path: Path,
) -> None:
    _sensitive_run(tmp_path)
    out = quality.critique_plan(repo_root=tmp_path, run_id="a")
    by_lens = {c["lens"]: c for c in out["critics"]}
    assert set(by_lens) == {*quality.CRITIC_LENSES, "adversarial"}
    assert "temporary root" in by_lens["adversarial"]["instruction"]
    assert "temporary root" not in by_lens["security"]["instruction"]


def test_adversarial_report_without_probes_is_refused(tmp_path: Path) -> None:
    _sensitive_run(tmp_path)
    with pytest.raises(quality.QualityError, match="RAN as `probes`"):
        _record(tmp_path, _adversarial_report(probes=[]))


def test_adversarial_finding_without_command_and_output_is_refused(tmp_path: Path) -> None:
    _sensitive_run(tmp_path)
    finding = CriticFinding(
        id="a1", lens="adversarial", severity=CriticSeverity.MAJOR, title="leak",
        tags=["personal_data_leak"], evidence_command="grep x", evidence_output="",
    )
    with pytest.raises(quality.QualityError, match="evidence_command"):
        _record(tmp_path, _adversarial_report(findings=[finding]))


def test_adversarial_report_with_evidence_is_recorded_and_floored(tmp_path: Path) -> None:
    _sensitive_run(tmp_path)
    finding = CriticFinding(
        id="a1", lens="adversarial", severity=CriticSeverity.MINOR, title="lowercase name leaks",
        tags=["personal_data_leak"], evidence_command="grep -ri garcia out/",
        evidence_output="out/garcia.json: match",
    )
    out = _record(tmp_path, _adversarial_report(findings=[finding]))
    assert out["lens"] == "adversarial" and out["raised_by_floor"] == 1
    saved = json.loads((tmp_path / ".hw/a/quality/iter-0/critics/adversarial.json").read_text())
    assert saved["findings"][0]["severity"] == "major"


def test_gate_requires_the_adversarial_lens_when_it_is_in_scope(tmp_path: Path) -> None:
    _sensitive_run(tmp_path)
    state = engine.load_state(engine.RunPaths(tmp_path, "a"))
    prov = quality.provenance_report(repo_root=tmp_path, run_id="a", state=state)
    assert "adversarial" in prov["missing"]
