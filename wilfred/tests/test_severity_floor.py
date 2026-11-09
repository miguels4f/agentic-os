"""L3 — deterministic severity floor (pilot replay (c)): a UI/engine divergence is never minor."""

from __future__ import annotations

from pathlib import Path

from test_quality import _add_critique, _record_subjective, _run_at_quality

from hyperwilfred_core import engine, quality, severity_floor
from hyperwilfred_core.schemas import (
    CriticFinding,
    CriticSeverity,
    ReviewAuthor,
    SubjectiveIssue,
)


def _f(severity: CriticSeverity, *, tags: list[str] | None = None, title: str = "t",
       description: str = "") -> CriticFinding:
    return CriticFinding(
        id="x", lens="correctness", severity=severity, title=title,
        description=description, tags=tags or [],
    )


def test_tagged_classes_are_raised_to_major() -> None:
    for tag in ("personal_data_leak", "gate_bypass", "ui_engine_divergence"):
        out = severity_floor.floor_finding(_f(CriticSeverity.MINOR, tags=[tag]))
        assert out.severity is CriticSeverity.MAJOR, tag
        assert out.severity_raised_from is CriticSeverity.MINOR


def test_aliases_and_case_are_normalised() -> None:
    out = severity_floor.floor_finding(_f(CriticSeverity.MINOR, tags=["UI-Divergence"]))
    assert out.severity is CriticSeverity.MAJOR
    out = severity_floor.floor_finding(_f(CriticSeverity.MINOR, tags=["PII"]))
    assert out.severity is CriticSeverity.MAJOR


def test_blocker_and_major_are_never_lowered() -> None:
    for sev in (CriticSeverity.BLOCKER, CriticSeverity.MAJOR):
        out = severity_floor.floor_finding(_f(sev, tags=["gate_bypass"]))
        assert out.severity is sev
        assert out.severity_raised_from is None


def test_untagged_ordinary_minor_stays_minor() -> None:
    out = severity_floor.floor_finding(
        _f(CriticSeverity.MINOR, title="rename variable", description="x is unclear")
    )
    assert out.severity is CriticSeverity.MINOR
    assert out.tags == []


def test_unknown_tag_does_not_raise() -> None:
    assert severity_floor.floor_finding(
        _f(CriticSeverity.MINOR, tags=["style"])
    ).severity is CriticSeverity.MINOR


def test_phrase_backstop_catches_untagged_pilot_wording() -> None:
    div = _f(
        CriticSeverity.MINOR,
        title="UI offers find_champion but the engine blocks it",
        description="the paste command diverges from the gate rule on DO_NOT_CONTACT",
    )
    assert severity_floor.floor_finding(div).severity is CriticSeverity.MAJOR
    leak = _f(CriticSeverity.MINOR, title="Privacy leak of lowercase name in file name")
    assert severity_floor.floor_finding(leak).severity is CriticSeverity.MAJOR
    bypass = _f(CriticSeverity.MINOR, title="Approval gate can be bypassed via empty slug")
    assert severity_floor.floor_finding(bypass).severity is CriticSeverity.MAJOR


def test_floor_is_idempotent() -> None:
    once = severity_floor.floor_finding(_f(CriticSeverity.MINOR, tags=["gate_bypass"]))
    twice = severity_floor.floor_finding(once)
    assert twice == once


def test_subjective_issue_floor() -> None:
    issue = SubjectiveIssue(
        severity=CriticSeverity.MINOR, description="page diverges from the engine's gate",
        tags=["ui_engine_divergence"],
    )
    assert severity_floor.floor_issue(issue).severity is CriticSeverity.MAJOR


def test_replay_c_divergence_submitted_as_minor_is_major_in_the_gate(tmp_path: Path) -> None:
    """pilot replay (c): B's verify.md rated the UI/engine divergence 'minor'."""
    _run_at_quality(tmp_path, threshold=4, iteration=0, max_iterations=3)
    _record_subjective(tmp_path, "q", [5, 5, 5, 5, 5])
    _add_critique(
        tmp_path, "q", "correctness",
        [CriticFinding(
            id="d1", lens="correctness", severity=CriticSeverity.MINOR,
            title="UI/engine divergence", tags=["ui_engine_divergence"],
            description="page offers the paste command; the engine blocks it",
        )],
    )
    # the persisted record is already raised …
    persisted = quality._read_critique_reports(tmp_path, "q", 0)[0].findings[0]
    assert persisted.severity is CriticSeverity.MAJOR
    assert persisted.severity_raised_from is CriticSeverity.MINOR
    # … and the gate counts it as an open major, feeding the patcher (not deferred as minor)
    g = quality.quality_gate(repo_root=tmp_path, run_id="q")
    state = engine.load_state(engine.RunPaths(tmp_path, "q"))
    assert state.quality is not None
    assert state.quality.open_major == 1 and state.quality.open_minor == 0
    assert g["action"] == "patch"
    assert g["open_floor_must_fix"] == 1


def test_floor_class_finding_cannot_be_waived_by_a_high_score(tmp_path: Path) -> None:
    """With the iteration budget spent it escalates to a human — never PASS/fallback."""
    _run_at_quality(tmp_path, threshold=4, iteration=2, max_iterations=3)
    _record_subjective(tmp_path, "q", [5, 5, 5, 5, 5])
    _add_critique(
        tmp_path, "q", "security",
        [CriticFinding(id="p1", lens="security", severity=CriticSeverity.MINOR,
                       title="lowercase name survives in file name", tags=["personal_data_leak"])],
    )
    g = quality.quality_gate(repo_root=tmp_path, run_id="q")
    assert g["action"] == "escalate"


def test_ordinary_major_still_does_not_block_a_passing_score(tmp_path: Path) -> None:
    """Historical node id retained: any unresolved major now blocks a passing score."""
    _run_at_quality(tmp_path, threshold=4)
    _record_subjective(tmp_path, "q", [5, 5, 5, 5, 5])
    for lens in quality.CRITIC_LENSES:
        finds = (
            [CriticFinding(id="m", lens=lens, severity=CriticSeverity.MAJOR, title="naming")]
            if lens == "maintainability" else []
        )
        _add_critique(tmp_path, "q", lens, finds)
    assert quality.quality_gate(repo_root=tmp_path, run_id="q")["action"] == "patch"


def test_floor_applies_even_to_a_hand_edited_report_on_disk(tmp_path: Path) -> None:
    _run_at_quality(tmp_path, threshold=4)
    quality.critique_plan(repo_root=tmp_path, run_id="q")
    f = tmp_path / ".hw/q/quality/iter-0/critics/security.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        '{"critic":"c","lens":"security","author":"independent","findings":[{"id":"1",'
        '"lens":"security","severity":"minor","title":"t","tags":["personal_data_leak"]}]}',
        encoding="utf-8",
    )
    got = quality._read_critique_reports(tmp_path, "q", 0)[0].findings[0]
    assert got.severity is CriticSeverity.MAJOR
    assert ReviewAuthor.INDEPENDENT  # author is untouched by the floor
