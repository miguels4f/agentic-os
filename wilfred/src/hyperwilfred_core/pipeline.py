"""The BUILD pipeline graph — HyperWilfred's analog of hyperresearch's step skills.

Each step names a fresh-loaded instruction markdown file (under ``steps/build/``) and a
:class:`StepKind` telling the adapter how to execute it:

* ``single`` — the adapter executes one instruction and writes one named artifact.
* ``fanout`` — the adapter asks ``hw fanout-plan`` for N packets and runs them in parallel,
  each in its own worktree + tool-lock; the engine accepts each via ``hw record-result``.
* ``gate``   — the engine runs a deterministic check (``hw evaluate``); a failure HARD-BLOCKS.

The procedures live as plain markdown on disk (not buried in a Claude-Code skill or Codex
TOML), so they are 100% portable across runtimes — the structural move that prevents the
Claude-Code-only trap.
"""

from __future__ import annotations

from pathlib import Path

from hyperwilfred_core.schemas import BuildMode, PipelineStep, StepKind

# The Phase-1 BUILD graph. Order matters: this IS the sequence the engine walks.
BUILD_STEPS: list[PipelineStep] = [
    PipelineStep(
        id="frame",
        title="Frame the objective",
        kind=StepKind.SINGLE,
        instruction_file="build/01_frame.md",
        produces="frame.md",
    ),
    PipelineStep(
        id="decompose",
        title="Decompose into atomic slices",
        kind=StepKind.SINGLE,
        instruction_file="build/02_decompose.md",
        produces="plan.json",
    ),
    PipelineStep(
        id="scaffold",
        title="Scaffold the product skeleton",
        kind=StepKind.SINGLE,
        instruction_file="build/03_scaffold.md",
        produces="scaffold.md",
    ),
    PipelineStep(
        id="implement",
        title="Implement slices in parallel",
        kind=StepKind.FANOUT,
        instruction_file="build/04_implement.md",
        produces=None,  # completion = all packets accepted
    ),
    PipelineStep(
        id="evaluate",
        title="Run deterministic gates",
        kind=StepKind.GATE,
        instruction_file="build/05_evaluate.md",
        produces="verification.json",
    ),
    PipelineStep(
        id="quality",
        title="Quality loop (0-5 evaluator + critics + surgical patch)",
        kind=StepKind.QUALITY,
        instruction_file="quality/00_loop.md",
        produces=None,  # completion = quality_gate marks the step done (pass/fallback)
    ),
    PipelineStep(
        id="verify",
        title="Verify the product runs",
        kind=StepKind.SINGLE,
        instruction_file="build/06_verify.md",
        produces="verify.md",
    ),
    PipelineStep(
        id="ship",
        title="Human ship gate",
        kind=StepKind.HUMAN_GATE,
        instruction_file="build/07_ship.md",
        produces=None,  # completion = a human approves the disk-file HITL gate (`hw gate-status`)
    ),
]


# The Phase-2 IMPROVE graph. The FRONT is net-new (comprehend → locate → scoped-diff →
# regression-guard); from the quality step onward it shares BUILD's evaluator + gate + patcher
# core. ``regression`` is the deterministic hard wall for IMPROVE (it proves the existing tests
# still pass) — the analog of BUILD's ``evaluate`` step.
IMPROVE_STEPS: list[PipelineStep] = [
    PipelineStep(
        id="comprehend",
        title="Comprehend the existing repository",
        kind=StepKind.SINGLE,
        instruction_file="improve/01_comprehend.md",
        produces="comprehension.md",
    ),
    PipelineStep(
        id="locate",
        title="Locate the change site(s) and scope the diff",
        kind=StepKind.SINGLE,
        instruction_file="improve/02_locate.md",
        produces="plan.json",  # same Plan/Slice contract BUILD uses → fan-out works unchanged
    ),
    PipelineStep(
        id="implement",
        title="Apply the scoped diff in parallel",
        kind=StepKind.FANOUT,
        instruction_file="improve/03_implement.md",
        produces=None,  # completion = all packets accepted
    ),
    PipelineStep(
        id="regression",
        title="Regression guard — existing tests must still pass",
        kind=StepKind.GATE,
        instruction_file="improve/04_regression.md",
        produces="verification.json",
    ),
    PipelineStep(
        id="quality",
        title="Quality loop (0-5 evaluator + critics + surgical patch)",
        kind=StepKind.QUALITY,
        instruction_file="quality/00_loop.md",
        produces=None,
    ),
    PipelineStep(
        id="verify",
        title="Verify the change works",
        kind=StepKind.SINGLE,
        instruction_file="improve/05_verify.md",
        produces="verify.md",
    ),
    PipelineStep(
        id="ship",
        title="Human ship gate",
        kind=StepKind.HUMAN_GATE,
        instruction_file="build/07_ship.md",
        produces=None,  # completion = a human approves the disk-file HITL gate (`hw gate-status`)
    ),
]


# The Phase-6 REVIEW graph — a standalone READ-ONLY audit. It REUSES the existing critic panel
# (the same CRITIC_LENSES + CritiquePacket/CritiqueReport contracts, driven by `hw critique-plan`
# / `hw record-critique`) but runs it over an ARBITRARY target instead of a build's changed paths.
# There is NO implement fan-out, NO deterministic build gate, NO patcher, NO ship — REVIEW writes
# nothing to the reviewed repo (only findings under ``.hw/``). ``critique`` is a dedicated REVIEW
# kind (read-only, engine-validated, NO code-acceptance gate); ``report`` synthesizes the findings.
REVIEW_STEPS: list[PipelineStep] = [
    PipelineStep(
        id="comprehend",
        title="Comprehend the review target",
        kind=StepKind.SINGLE,
        instruction_file="review/01_comprehend.md",
        produces="comprehension.md",
    ),
    PipelineStep(
        id="critique",
        title="Run the read-only critic panel over the target",
        kind=StepKind.REVIEW,
        instruction_file="review/02_critique.md",
        produces=None,  # completion = at least one critic findings JSON recorded (engine-validated)
    ),
    PipelineStep(
        id="report",
        title="Synthesize the review findings report",
        kind=StepKind.SINGLE,
        instruction_file="review/03_report.md",
        produces="review-report.json",
    ),
]


def steps_for(mode: BuildMode) -> list[PipelineStep]:
    """Return the ordered step graph for *mode*. Ships BUILD, IMPROVE, and REVIEW."""
    if mode is BuildMode.BUILD:
        return [s.model_copy(deep=True) for s in BUILD_STEPS]
    if mode is BuildMode.IMPROVE:
        return [s.model_copy(deep=True) for s in IMPROVE_STEPS]
    if mode is BuildMode.REVIEW:
        return [s.model_copy(deep=True) for s in REVIEW_STEPS]
    raise NotImplementedError(f"mode {mode!r} has no step graph")  # pragma: no cover


def steps_root() -> Path:
    """Absolute path to the bundled step-instruction markdown tree."""
    return Path(__file__).resolve().parent / "steps"


def load_instruction(instruction_file: str) -> str:
    """Read a step's instruction markdown fresh from disk (empty string if missing)."""
    path = steps_root() / instruction_file
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


__all__ = [
    "BUILD_STEPS",
    "IMPROVE_STEPS",
    "REVIEW_STEPS",
    "load_instruction",
    "steps_for",
    "steps_root",
]
