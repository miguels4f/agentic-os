"""State-claims lesson: artifacts asserting current repo/system state must be verified fresh.

Origin: 2026-07-08 pilot-app incident — a delegated docs session committed "no MFA code exists
on main" in the same changeset whose sibling file recorded that PR as merged; the state had been
read at session start and drifted mid-session. These tests pin the encoded lesson: the field
guide exists and is substantial, the shared critic instruction (BUILD/IMPROVE/REVIEW) points at
it, and both verify steps carry the write-time re-check.
"""

from __future__ import annotations

from hyperwilfred_core.pipeline import load_instruction, steps_root

METHODOLOGY = "state-claims-methodology.md"


def test_state_claims_methodology_exists_and_is_substantial() -> None:
    doc = steps_root() / "quality" / METHODOLOGY
    assert doc.exists()
    text = doc.read_text(encoding="utf-8")
    # A stub would be a few lines; the real field guide covers all of these.
    assert len(text.splitlines()) > 60
    for marker in (
        "state claim",
        "verify at write time",
        "changeset must agree with itself",
        "contradiction with your premises",
        "Disposition",
    ):
        assert marker.lower() in text.lower(), marker


def test_critique_instruction_points_at_the_methodology() -> None:
    text = load_instruction("quality/02_critique.md")
    assert METHODOLOGY in text
    # The lesson's teeth: contradictory state claims inside one changeset are a blocker.
    assert "state claim" in text.lower()
    assert "blocker" in text.lower()


def test_verify_steps_carry_the_write_time_recheck() -> None:
    for step in ("improve/05_verify.md", "build/06_verify.md"):
        text = load_instruction(step)
        assert "state claim" in text.lower(), step
        assert METHODOLOGY in text, step
        # Freshness rule: re-verify against the repo as of NOW, not session-start memory.
        assert "AS OF NOW" in text, step


def test_methodology_encodes_the_three_rules_in_order() -> None:
    text = (steps_root() / "quality" / METHODOLOGY).read_text(encoding="utf-8")
    rule1 = text.index("Rule 1")
    rule2 = text.index("Rule 2")
    rule3 = text.index("Rule 3")
    assert rule1 < rule2 < rule3
