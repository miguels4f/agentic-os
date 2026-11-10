"""The learning analyzer — the self-improving half, REVIEW-GATED and NEVER auto-applied.

RECYCLE-TABLE verdict (spec §3): ``learning_loop.py`` is **adapt** — "adapt the
analyzer→proposal→review-packet pattern." This is the forked, standalone, code-retargeted
analyzer (ZERO ``common_py`` imports). It does three things, all deterministic and disk-only:

1. **Mine** the on-disk run history across ``.hw/*/`` — via :mod:`hyperwilfred_core.observability`,
   which reads ``events.jsonl`` + acceptance ``verdicts/`` + the quality-loop history — and turn the
   detected code anomalies into :class:`LearningSignal` s.
2. **Emit** REVIEW-ONLY :class:`LearningProposal` s, each materialized as an IMPROVE
   :class:`BuildObjective` (so it runs through the EXISTING IMPROVE pipeline). Every proposal
   carries ``requires_human_review=True`` / ``auto_apply=False``.
3. **Gate** promotion behind the EXISTING Phase-3 HITL gate mechanism: a proposal becomes an
   active IMPROVE run ONLY via :func:`promote`, and ONLY after a signed human decision verifies
   (detected by the SAME ``gates.detect_decision_in_dir`` the Phase-3 run gates use).

THE non-negotiable invariant (spec §2, constraint #2): **the factory does not silently rewrite
its own rules.** There is no code path that applies a proposal without an explicit human approval
token. Like the rest of the engine this module calls NO LLM and spawns NOTHING — it computes
proposals deterministically from the disk ledger and persists them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from hyperwilfred_core import approvals, gates, observability
from hyperwilfred_core.path_safety import validate_identifier
from hyperwilfred_core.run_store import atomic_write_text
from hyperwilfred_core.schemas import (
    AnomalyCode,
    BuildMode,
    BuildObjective,
    CodeAnomaly,
    ContextRef,
    GateDecision,
    LearningProposal,
    LearningProposalSet,
    LearningSignal,
    PromotionEffect,
    PromotionOutcome,
    ProposalStatus,
    ProposalType,
    RiskLevel,
    RunObservation,
    RunStatus,
)

LESSONS_DIR = "lessons"
LESSONS_JSON = "lessons.json"
LESSONS_MD = "lessons.md"
PROPOSALS_DIR = "proposals"
ITEMS_SUBDIR = "items"
GATES_SUBDIR = "gates"
PACK_JSON = "proposals.json"
PACK_MD = "proposals.md"


class Lesson(BaseModel):
    """A curated, human-reviewed lesson from a real run, loaded by every future run.

    Unlike a :class:`LearningProposal` (machine-mined, review-gated, promoted to an IMPROVE run),
    a lesson is a fixed piece of guidance a human already reviewed and merged INTO THE CODE. It is
    shipped with the engine and attached to the step packets it applies to (``hw next-step``), so
    it is available to every run — including cloud runs with no local ``.hw`` history.
    """

    lesson_id: str
    title: str
    lesson: str
    applies_to_steps: list[str]
    enforced_by: str
    source: str = "pilot-cockpit-v1-v2-2026-09-18"


PILOT_LESSONS: tuple[Lesson, ...] = (
    Lesson(
        lesson_id="PILOT-L1",
        title="No silent pass on adapter self-review",
        lesson="A separate reviewer produces a report, but only an external signer whose key is "
        "unavailable to the repository and workers can sign the exact challenge. The second call "
        "must supply `--attestation-file`; without that signer/key, stop at "
        "PENDING_INDEPENDENT_REVIEW. Never use a fixture key or relabel unsigned work.",
        applies_to_steps=["quality", "ship"],
        enforced_by="quality.quality_gate provenance check; gates.gate_open banner",
    ),
    Lesson(
        lesson_id="PILOT-L2",
        title="Attack the running product, do not just read it",
        lesson="For personal data, gates, approvals or security, run the adversarial lens: "
        "accented/lowercase/partial names, names in file names, dashed phones, compact UUIDs, "
        "blocked-state records, injection-shaped ids, tampered config, UI vs engine disagreement — "
        "each finding cites the command run and the observed output.",
        applies_to_steps=["quality"],
        enforced_by="quality.resolve_lenses + adversarial evidence requirement",
    ),
    Lesson(
        lesson_id="PILOT-L3",
        title="Some findings are never minor",
        lesson="A personal-data leak, a gate/permission bypass, or a divergence between a "
        "displayed/offered action and the authoritative check is at least major and cannot be "
        "deferred — tag it; the engine enforces the floor.",
        applies_to_steps=["quality"],
        enforced_by="severity_floor + quality_gate must-fix handling",
    ),
    Lesson(
        lesson_id="PILOT-L4",
        title="Trace every shaping part into the contract",
        lesson="Every requirement/part ID from the shaping doc must be covered by an acceptance "
        "criterion/slice or explicitly declared out_of_scope; B2.1 (allowed tools) was silently "
        "dropped once. Verify blocks on a MISSING one.",
        applies_to_steps=["decompose", "locate", "verify"],
        enforced_by="traceability.check via verify_checks",
    ),
    Lesson(
        lesson_id="PILOT-L5",
        title="Cut slices by responsibility, not file count",
        lesson="Keep a rule and the place its result is published in the same packet; never plan "
        "'recompute the rule in another language/layer'. The engine computes once and publishes; "
        "other layers only display. max_files is a ceiling, not a target.",
        applies_to_steps=["decompose", "locate"],
        enforced_by="plan_lint (warn; block at high/critical risk)",
    ),
    Lesson(
        lesson_id="PILOT-L6",
        title="Generated artifacts must be reproducible",
        lesson="Declare generated artifacts with their regeneration command and volatile fields; "
        "take dates from git, not the clock. Verify regenerates them in a fresh worktree and fails "
        "on drift.",
        applies_to_steps=["decompose", "implement", "verify"],
        enforced_by="reproducibility.check via verify_checks",
    ),
    Lesson(
        lesson_id="PILOT-L7",
        title="Check the promised demo on live data",
        lesson="Record whether the demo is achievable on current data. 'Every action blocked' or "
        "an empty result is a finding, never something a test asserts as correct.",
        applies_to_steps=["verify"],
        enforced_by="live_demo.check via verify_checks",
    ),
    Lesson(
        lesson_id="PILOT-L8",
        title="Declare generated files so guardrails do not fight them",
        lesson="A regenerated single-line JSON snapshot trips the full-file-replacement rule. "
        "Declare it on the slice (path + command); only declared data artifacts are exempt, "
        "source files keep the protection.",
        applies_to_steps=["decompose", "implement"],
        enforced_by="guardrails.patch_violations generated_globs",
    ),
    Lesson(
        lesson_id="PILOT-L9",
        title="Contract checklist for governed products",
        lesson="Fail-closed runtime integrity pins for governed config (with tamper tests); "
        "enforced caps (tool allow-lists, budgets) in every command handed to a human/agent, not "
        "display-only; reproducible generated data; a user-facing reason for every locked state.",
        applies_to_steps=["decompose", "locate"],
        enforced_by="decompose/locate step instructions (checklist)",
    ),
)


def lessons_for_step(step_id: str) -> list[dict]:
    """The lessons that apply to *step_id*, as plain dicts (attached to ``hw next-step``)."""
    return [
        {"id": x.lesson_id, "title": x.title, "lesson": x.lesson, "enforced_by": x.enforced_by}
        for x in PILOT_LESSONS
        if step_id in x.applies_to_steps
    ]


def record_lessons(*, repo_root: Path) -> dict:
    """Persist the curated lessons to ``.hw/lessons/`` (JSON + markdown) for humans and tools."""
    directory = repo_root / ".hw" / LESSONS_DIR
    atomic_write_text(
        directory / LESSONS_JSON,
        json.dumps([x.model_dump(mode="json") for x in PILOT_LESSONS], indent=2) + "\n",
    )
    lines = ["# Curated lessons (loaded by every run via `hw next-step`)", ""]
    for x in PILOT_LESSONS:
        lines += [f"## {x.lesson_id} — {x.title}", "", x.lesson, "",
                  f"- applies to steps: {', '.join(x.applies_to_steps)}",
                  f"- enforced by: {x.enforced_by}", f"- source: {x.source}", ""]
    atomic_write_text(directory / LESSONS_MD, "\n".join(lines))
    return {
        "lessons": len(PILOT_LESSONS),
        "lessons_json": str(directory / LESSONS_JSON),
        "lessons_md": str(directory / LESSONS_MD),
    }


class LearningError(RuntimeError):
    """Raised on a learning/autonomy precondition violation (surfaces as CLI exit 2)."""


def _now() -> datetime:
    return datetime.now(UTC)


def _proposals_dir(repo_root: Path) -> Path:
    return repo_root / ".hw" / PROPOSALS_DIR


def _items_dir(repo_root: Path) -> Path:
    return _proposals_dir(repo_root) / ITEMS_SUBDIR


def _gates_dir(repo_root: Path) -> Path:
    return _proposals_dir(repo_root) / GATES_SUBDIR


def _item_path(repo_root: Path, proposal_id: str) -> Path:
    # A proposal id is a file name under ``.hw/proposals``; refuse traversal before any I/O.
    validate_identifier(proposal_id, label="proposal_id")
    return _items_dir(repo_root) / f"{proposal_id}.json"


# --------------------------------------------------------------------------- anomaly → proposal map
#
# Each anomaly CODE maps to a proposal TYPE + a materialized IMPROVE objective that targets the
# factory's OWN files (step instructions, guardrails, tests). The mapping is a static table, not
# model judgment — the analyzer is deterministic.

_CODE_TO_TYPE: dict[AnomalyCode, ProposalType] = {
    AnomalyCode.PATCH_LOOP_MAXED: ProposalType.PROMPT_RULE,
    AnomalyCode.DIFF_OVER_BUDGET: ProposalType.GUARDRAIL,
    AnomalyCode.HIGH_REJECT_RATE: ProposalType.GUARDRAIL,
    AnomalyCode.HIGH_ROLLBACK_RATE: ProposalType.GUARDRAIL,
    AnomalyCode.REPEATED_GATE_FAIL: ProposalType.ACCEPTANCE_CRITERION,
    AnomalyCode.ESCALATION_SPIKE: ProposalType.ACCEPTANCE_CRITERION,
}

_CODE_TO_AREA: dict[AnomalyCode, str] = {
    AnomalyCode.PATCH_LOOP_MAXED: "steps/quality/00_loop.md + the subjective threshold",
    AnomalyCode.DIFF_OVER_BUDGET: "steps/build/02_decompose.md slice budgets + guardrails.json",
    AnomalyCode.HIGH_REJECT_RATE: "steps/build/02_decompose.md per-slice allowed_globs scope",
    AnomalyCode.HIGH_ROLLBACK_RATE: "steps/build/02_decompose.md per-slice scope + budgets",
    AnomalyCode.REPEATED_GATE_FAIL: "steps/build/04_implement.md + acceptance_criteria",
    AnomalyCode.ESCALATION_SPIKE: "steps/quality + reviewed acceptance_criteria",
}

_CODE_TO_TITLE: dict[AnomalyCode, str] = {
    AnomalyCode.PATCH_LOOP_MAXED: "Harden the quality step — the patch loop keeps maxing out",
    AnomalyCode.DIFF_OVER_BUDGET: "Right-size slice budgets — diffs keep exceeding the budget",
    AnomalyCode.HIGH_REJECT_RATE: "Tighten decompose scope — workers keep escaping their scope",
    AnomalyCode.HIGH_ROLLBACK_RATE: "Tighten decompose scope — packets keep being rolled back",
    AnomalyCode.REPEATED_GATE_FAIL: "Add gate-aware acceptance criteria — the gate keeps failing",
    AnomalyCode.ESCALATION_SPIKE: "Add a reviewed acceptance criterion — blockers keep escalating",
}

_CODE_TO_CHANGE: dict[AnomalyCode, str] = {
    AnomalyCode.PATCH_LOOP_MAXED: (
        "Review the quality-loop step instruction and the subjective threshold: either strengthen "
        "the implement-step guidance so the first draft clears the bar, or document why the "
        "threshold is calibrated as it is."
    ),
    AnomalyCode.DIFF_OVER_BUDGET: (
        "Review the decompose step's per-slice changed-lines budgets: either split the offending "
        "slices smaller or raise the budget where the scope genuinely warrants it."
    ),
    AnomalyCode.HIGH_REJECT_RATE: (
        "Review the decompose step's per-slice allowed_globs: workers keep writing outside scope, "
        "so the declared scope or the step instruction needs to be clearer/tighter."
    ),
    AnomalyCode.HIGH_ROLLBACK_RATE: (
        "Review the decompose step's per-slice scope and budgets: a high rollback rate means the "
        "engine keeps rejecting workers — tighten the scope or clarify the instruction."
    ),
    AnomalyCode.REPEATED_GATE_FAIL: (
        "Add a reviewed acceptance criterion (or implement-step guidance) so workers produce "
        "gate-green code on the first pass instead of failing the deterministic gate repeatedly."
    ),
    AnomalyCode.ESCALATION_SPIKE: (
        "Add a reviewed acceptance criterion that captures the recurring blocker, so the quality "
        "loop catches it earlier instead of escalating it to a human."
    ),
}


def _confidence(signal_count: int) -> float:
    """More runs exhibiting the same signal → higher confidence (capped). From learning_loop."""
    return min(0.9, round(0.45 + signal_count * 0.15, 4))


def evidence_strength(p: LearningProposal) -> str:
    """How strong the proposal's evidence is, in words (never hides a single-run signal)."""
    affected = len(p.source_run_ids)
    if p.runs_observed is None:
        text = f"{affected} affected run(s); total runs observed not recorded"
    else:
        text = f"{affected} of {p.runs_observed} observed run(s)"
    if affected == 1:
        text += " — single run, may be noise"
    return text


def effect_summary(p: LearningProposal) -> str:
    """The after-promotion line for humans ("" when there is nothing to report)."""
    e = p.effect_after_promotion
    if e is None:
        return ""
    counts = (f"{e.flagged_before} of {e.runs_before} finished run(s) flagged before, "
              f"{e.flagged_after} of {e.runs_after} since")
    if e.outcome is PromotionOutcome.FIX_NOT_SHIPPED:
        return f"Fix not shipped yet (run `{e.promoted_run_id}` not completed); {counts}."
    if e.outcome is PromotionOutcome.TOO_FEW_RUNS:
        return f"Too few finished runs to compare ({counts})."
    if e.outcome is PromotionOutcome.FEWER_FLAGGED:
        return (f"Fewer runs flagged since the fix ({counts}) — not proof the change caused it: "
                "proposals are made when a problem peaks.")
    return f"Not fewer runs flagged since the fix ({counts}) — the change may not have helped."


# Runs that ended; a run still in progress could still be flagged later, so it is not counted.
_FINISHED = frozenset({RunStatus.COMPLETED, RunStatus.FAILED})
_MIN_RUNS_TO_COMPARE = 3


def _promotion_effect(
    p: LearningProposal, observations: list[RunObservation]
) -> PromotionEffect | None:
    """Share of finished BUILD/IMPROVE runs flagged with the signal, before vs after promotion.

    None when the promoted IMPROVE run (the only source of the promotion time) is not on disk or
    has no recorded creation time. Runs created at exactly that time count on neither side.
    """
    if p.status is not ProposalStatus.PROMOTED or not p.promoted_run_id:
        return None
    by_id = {o.run_id: o for o in observations}
    promoted = by_id.get(p.promoted_run_id)
    if promoted is None or promoted.created_at is None:
        return None
    code = p.proposal_id.removeprefix("learn-")
    before = after = flagged_before = flagged_after = 0
    for o in observations:
        if (o.run_id == promoted.run_id or o.created_at is None
                or o.mode not in (BuildMode.BUILD, BuildMode.IMPROVE)
                or o.status not in _FINISHED or o.created_at == promoted.created_at):
            continue
        flagged = code in o.anomaly_codes
        if o.created_at < promoted.created_at:
            before += 1
            flagged_before += flagged
        else:
            after += 1
            flagged_after += flagged
    shipped = promoted.status is RunStatus.COMPLETED
    if not shipped:
        outcome = PromotionOutcome.FIX_NOT_SHIPPED
    elif before < _MIN_RUNS_TO_COMPARE or after < _MIN_RUNS_TO_COMPARE:
        outcome = PromotionOutcome.TOO_FEW_RUNS
    elif flagged_after * before < flagged_before * after:  # after/before rates, no division
        outcome = PromotionOutcome.FEWER_FLAGGED
    else:
        outcome = PromotionOutcome.NOT_FEWER
    return PromotionEffect(
        promoted_run_id=promoted.run_id, promoted_at=promoted.created_at, fix_shipped=shipped,
        runs_before=before, flagged_before=flagged_before, runs_after=after,
        flagged_after=flagged_after, outcome=outcome,
    )


def _materialize_objective(
    code: AnomalyCode, source_run_ids: list[str], change: str, area: str,
    runs_observed: int | None = None,
) -> BuildObjective:
    """Materialize an IMPROVE :class:`BuildObjective` that points the factory at its OWN files.

    Promotion runs this through ``hw plan --mode improve`` (the EXISTING IMPROVE pipeline), so the
    factory improves itself with the very same comprehend→locate→scoped-diff→regression-guard path
    it uses on any other repo — human-gated at ship like every other run.
    """
    runs = ", ".join(source_run_ids) if source_run_ids else "recent runs"
    of_total = f" of {runs_observed} observed" if runs_observed is not None else ""
    return BuildObjective(
        id=f"improve-{code.value}",
        title=_CODE_TO_TITLE[code],
        objective=(
            f"Self-improvement (review-gated): the '{code.value}' signal appeared on "
            f"{len(source_run_ids)}{of_total} run(s) ({runs}). "
            f"{change} Affected area: {area}."
        ),
        acceptance_criteria=[
            change,
            "The change is surgical and stays within the affected area.",
            "All existing HyperWilfred tests still pass (regression guard).",
        ],
        constraints=[
            "Touch only the factory's own step instructions / guardrails / tests in scope.",
            "A human approves the ship gate before anything lands (no silent self-rewrite).",
        ],
        context_refs=[ContextRef(kind="path", value=area, description="anomaly-affected area")],
        risk_level=RiskLevel.HIGH,  # touching the factory's own rules → the deeper (FULL) path
    )


# --------------------------------------------------------------------------- analyze (pure compute)


def analyze(*, repo_root: Path) -> LearningProposalSet:
    """Mine the run history and compute review-only proposals. Pure: no disk writes.

    Deterministic — the proposals are a static-table function of the on-disk anomalies. One
    proposal per anomaly CODE, aggregating every run that exhibits it.
    """
    return _analyze(observability.observe_all(repo_root=repo_root))


def _analyze(observations: list[RunObservation]) -> LearningProposalSet:
    anomalies: list[CodeAnomaly] = []
    for obs in observations:
        anomalies.extend(observability.detect_run_anomalies(obs))

    signals = [
        LearningSignal(
            code=a.code,
            run_id=a.run_id,
            severity=a.severity,
            message=a.message,
            evidence_refs=a.evidence_refs,
        )
        for a in anomalies
    ]

    # Group by code (sorted for determinism); one proposal per code.
    by_code: dict[AnomalyCode, list[CodeAnomaly]] = {}
    for a in anomalies:
        by_code.setdefault(a.code, []).append(a)

    proposals: list[LearningProposal] = []
    for code in sorted(by_code, key=lambda c: c.value):
        group = by_code[code]
        run_ids = sorted({a.run_id for a in group})
        evidence = sorted({ref for a in group for ref in a.evidence_refs})
        change = _CODE_TO_CHANGE[code]
        area = _CODE_TO_AREA[code]
        proposals.append(
            LearningProposal(
                proposal_id=f"learn-{code.value}",
                proposal_type=_CODE_TO_TYPE[code],
                title=_CODE_TO_TITLE[code],
                rationale=(
                    f"The '{code.value}' signal appeared on {len(run_ids)} of "
                    f"{len(observations)} observed run(s)"
                    + (" (single run — may be noise)" if len(run_ids) == 1 else "")
                    + ": " + "; ".join(a.message for a in group[:3])
                ),
                suggested_change=change,
                affected_area=area,
                confidence=_confidence(len(run_ids)),
                evidence_refs=evidence,
                source_run_ids=run_ids,
                runs_observed=len(observations),
                objective=_materialize_objective(code, run_ids, change, area, len(observations)),
            )
        )

    return LearningProposalSet(generated_at=_now(), signals=signals, proposals=proposals)


# --------------------------------------------------------------------------- persistence


def _read_proposal(path: Path) -> LearningProposal:
    """Fail closed on an unreadable ledger item: silently skipping it would let `hw learn`
    overwrite an approved or promoted proposal with a fresh PROPOSED copy."""
    try:
        return LearningProposal.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LearningError(
            f"learning proposal file {path} cannot be read ({exc}); fix or move it by hand — "
            "it may hold a human decision"
        ) from exc


def load_proposal(repo_root: Path, proposal_id: str) -> LearningProposal | None:
    path = _item_path(repo_root, proposal_id)
    if not path.exists():
        return None
    return _read_proposal(path)


def list_proposals(repo_root: Path) -> list[LearningProposal]:
    items = _items_dir(repo_root)
    if not items.exists():
        return []
    return [_read_proposal(f) for f in sorted(items.glob("*.json"))]


def _save_proposal(repo_root: Path, proposal: LearningProposal) -> None:
    atomic_write_text(
        _item_path(repo_root, proposal.proposal_id), proposal.model_dump_json(indent=2) + "\n"
    )


# --------------------------------------------------------------------------- hw learn


def learn(*, repo_root: Path) -> dict:
    """Run the analyzer, persist proposals (preserving human-decided ones), render the pack.

    A proposal a human has ALREADY acted on (status != PROPOSED) is FROZEN — re-running ``hw
    learn`` never resurrects a rejected proposal or resets an approved/promoted one. Freshly
    computed proposals overwrite only their still-``PROPOSED`` selves.
    """
    persisted = {p.proposal_id: p for p in list_proposals(repo_root)}
    observations = observability.observe_all(repo_root=repo_root)
    fresh = _analyze(observations)

    final: list[LearningProposal] = []
    fresh_ids: set[str] = set()
    changed: set[str] = set()
    for p in fresh.proposals:
        fresh_ids.add(p.proposal_id)
        existing = persisted.get(p.proposal_id)
        if existing is not None and existing.status is not ProposalStatus.PROPOSED:
            final.append(existing)  # human already acted — frozen, never reset
        else:
            final.append(p)
            changed.add(p.proposal_id)
    # Retain human-decided proposals whose anomaly no longer fires (keep the ledger history).
    for pid, existing in persisted.items():
        if pid not in fresh_ids and existing.status is not ProposalStatus.PROPOSED:
            final.append(existing)
    # A promoted proposal's decision fields stay frozen; only its informational after-promotion
    # effect is recomputed, and the file is rewritten only when that effect changed.
    for p in final:
        if p.status is ProposalStatus.PROMOTED:
            effect = _promotion_effect(p, observations)
            if effect != p.effect_after_promotion:
                p.effect_after_promotion = effect
                changed.add(p.proposal_id)

    _items_dir(repo_root).mkdir(parents=True, exist_ok=True)
    for p in final:
        if p.proposal_id in changed or p.proposal_id not in persisted:
            _save_proposal(repo_root, p)

    pack = LearningProposalSet(
        generated_at=fresh.generated_at, signals=fresh.signals, proposals=final
    )
    _render_pack(repo_root, pack)
    lessons = record_lessons(repo_root=repo_root)
    return {
        "lessons": lessons,
        "repo": str(repo_root),
        "signals": len(pack.signals),
        "proposals": len(pack.proposals),
        "proposal_ids": [p.proposal_id for p in pack.proposals],
        "by_status": _count_by_status(final),
        "pack_json": str(_proposals_dir(repo_root) / PACK_JSON),
        "pack_md": str(_proposals_dir(repo_root) / PACK_MD),
        "next_action": pack.next_action,
    }


def proposals_view(*, repo_root: Path) -> dict:
    """Read-only view of the current proposal pack from the ledger (the ``hw propose`` listing)."""
    proposals = list_proposals(repo_root)
    return {
        "repo": str(repo_root),
        "proposals": [
            {
                "proposal_id": p.proposal_id,
                "type": p.proposal_type.value,
                "title": p.title,
                "status": p.status.value,
                "confidence": p.confidence,
                "confidence_basis": p.confidence_basis,
                "evidence": evidence_strength(p),
                "effect_after_promotion": effect_summary(p) or None,
                "source_run_ids": p.source_run_ids,
                "requires_human_review": p.requires_human_review,
                "auto_apply": p.auto_apply,
            }
            for p in proposals
        ],
        "count": len(proposals),
        "by_status": _count_by_status(proposals),
    }


def _count_by_status(proposals: list[LearningProposal]) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in proposals:
        out[p.status.value] = out.get(p.status.value, 0) + 1
    return out


def _proposal_review_evidence(proposal: LearningProposal) -> list[str]:
    """Show and bind all proposal content while excluding engine-owned lifecycle fields."""
    content = proposal.model_dump(
        mode="json", exclude={"status", "review_gate_id", "promoted_run_id"},
    )
    return [
        *proposal.evidence_refs,
        f"proposal_sha256:{approvals.digest(content)}",
        f"proposal_content:{approvals.canonical_bytes(content).decode('utf-8')}",
    ]


# --------------------------------------------------------------------------- hw propose (open gate)


def open_review_gate(*, repo_root: Path, proposal_id: str) -> dict:
    """Open the disk-file HITL review gate for a proposal and BLOCK until a human decides.

    Reuses the EXACT Phase-3 gate-file protocol (the ``gates`` ``_in`` helpers + the human-decision
    detection), under ``.hw/proposals/gates/``. The engine writes a challenge and ``.pending``
    marker, but never a signed decision. The proposal stays inert until a trusted human signs.
    """
    proposal = load_proposal(repo_root, proposal_id)
    if proposal is None:
        raise LearningError(f"no proposal {proposal_id!r} — run `hw learn` first")
    if proposal.status is ProposalStatus.PROMOTED:
        raise LearningError(f"proposal {proposal_id!r} is already promoted (run "
                            f"{proposal.promoted_run_id!r})")
    if proposal.status is ProposalStatus.REJECTED:
        raise LearningError(f"proposal {proposal_id!r} was rejected by a human — it stays inert")

    gdir = _gates_dir(repo_root)
    gdir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        gates.pending_marker_in(gdir, proposal_id),
        f"pending human review for learning proposal {proposal_id}\n",
    )
    challenge = approvals.issue_challenge(
        gdir, run_id="proposals", gate_id=proposal_id,
        action=f"promote learning proposal {proposal_id}",
        evidence=_proposal_review_evidence(proposal),
        purpose="learning-proposal",
    )
    proposal.status = ProposalStatus.UNDER_REVIEW
    proposal.review_gate_id = proposal_id
    _save_proposal(repo_root, proposal)
    return {
        "proposal_id": proposal_id,
        "status": proposal.status.value,
        "blocked": True,
        "evidence": evidence_strength(proposal),
        "confidence": proposal.confidence,
        "confidence_basis": proposal.confidence_basis,
        "challenge": challenge,
        "approval": {"challenge_file": str(approvals.challenge_file(gdir, proposal_id)),
                     "decision_file": str(gates.decision_file_in(gdir, proposal_id))},
    }


def _resolve_review(repo_root: Path, proposal: LearningProposal) -> LearningProposal:
    """Flip a proposal only after its signed human decision verifies."""
    if proposal.status is ProposalStatus.APPROVED:
        # A prior status check may have consumed the token. Reverify its signed content
        # before promotion, since the proposal item remains a mutable disk file.
        gdir = _gates_dir(repo_root)
        try:
            decision = approvals.verify_and_consume(
                gdir, proposal.proposal_id,
                gates.decision_file_in(gdir, proposal.proposal_id),
                expected_run_id="proposals",
                expected_action=f"promote learning proposal {proposal.proposal_id}",
                expected_evidence=_proposal_review_evidence(proposal),
                consume=False, recorded=True,
            )
        except (approvals.ApprovalError, OSError, ValueError) as exc:
            raise LearningError("approved learning proposal changed after human review") from exc
        if decision[0] != "approve":
            raise LearningError("learning proposal has no valid approval")
        return proposal
    if proposal.status in (ProposalStatus.REJECTED, ProposalStatus.PROMOTED):
        return proposal
    decision = gates.detect_decision_in_dir(
        _gates_dir(repo_root), proposal.proposal_id, expected_run_id="proposals",
        expected_action=f"promote learning proposal {proposal.proposal_id}",
        expected_evidence=_proposal_review_evidence(proposal),
    )
    if decision is None:
        return proposal
    verdict = decision[0]
    if verdict is GateDecision.APPROVE:
        proposal.status = ProposalStatus.APPROVED
    elif verdict is GateDecision.REJECT:
        proposal.status = ProposalStatus.REJECTED
    # MODIFY_SCOPE stays under review (a human re-scopes the proposal).
    if proposal.status in (ProposalStatus.APPROVED, ProposalStatus.REJECTED):
        pending = gates.pending_marker_in(_gates_dir(repo_root), proposal.proposal_id)
        if pending.exists():
            pending.unlink()
        _save_proposal(repo_root, proposal)
    return proposal


def review_status(*, repo_root: Path, proposal_id: str) -> dict:
    """Resolve + report a proposal's review status from a signed decision token."""
    proposal = load_proposal(repo_root, proposal_id)
    if proposal is None:
        raise LearningError(f"no proposal {proposal_id!r}")
    proposal = _resolve_review(repo_root, proposal)
    return {
        "proposal_id": proposal_id,
        "status": proposal.status.value,
        "approved": proposal.status is ProposalStatus.APPROVED,
        "promoted_run_id": proposal.promoted_run_id,
    }


# --------------------------------------------------------------------------- hw promote


def promote(*, repo_root: Path, proposal_id: str, run_id: str | None = None) -> dict:
    """Promote an APPROVED proposal to an active IMPROVE run — REFUSES if not human-approved.

    THE invariant guard: a proposal becomes an active run ONLY if a signed human decision verifies
    (detected by the same ``gates.detect_decision_in_dir`` the Phase-3 run gates use). With no
    valid token the proposal stays inert and this returns ``promoted=False`` — there is no path
    that applies a proposal without explicit human approval.
    """
    proposal = load_proposal(repo_root, proposal_id)
    if proposal is None:
        raise LearningError(f"no proposal {proposal_id!r} — run `hw learn` first")

    if proposal.status is ProposalStatus.PROMOTED:
        return {"proposal_id": proposal_id, "promoted": True, "already_promoted": True,
                "run_id": proposal.promoted_run_id}

    proposal = _resolve_review(repo_root, proposal)

    if proposal.status is not ProposalStatus.APPROVED:
        return {
            "proposal_id": proposal_id,
            "promoted": False,
            "status": proposal.status.value,
            "reason": (
                "the proposal has NO valid signed human authorization — it stays inert. Open "
                f"the review gate (`hw propose --id {proposal_id}`), have the trusted host sign "
                "its challenge, then re-run `hw promote`. The engine never "
                "auto-applies a proposal."
            ),
        }

    # Approved by a human → promote to an IMPROVE run via the EXISTING engine.plan(mode=improve).
    from hyperwilfred_core.engine import RunPaths, next_step, plan

    resolved_run_id = run_id or f"imp-{proposal_id}"
    if RunPaths(repo_root, resolved_run_id).state.exists():
        raise LearningError(
            f"an IMPROVE run {resolved_run_id!r} already exists — pass --run-id for a fresh one"
        )
    state = plan(
        repo_root=repo_root,
        objective=proposal.objective,
        run_id=resolved_run_id,
        # The factory improves its own files: declare the repository root as the target, since
        # a run without a declared target can no longer obtain a regression baseline.
        product_dir=".",
        mode=BuildMode.IMPROVE,
    )
    proposal.status = ProposalStatus.PROMOTED
    proposal.promoted_run_id = state.run_id
    _save_proposal(repo_root, proposal)
    return {
        "proposal_id": proposal_id,
        "promoted": True,
        "run_id": state.run_id,
        "mode": state.mode.value,
        "first_step": next_step(repo_root=repo_root, run_id=state.run_id),
    }


# --------------------------------------------------------------------------- rendering


def _render_pack(repo_root: Path, pack: LearningProposalSet) -> None:
    """Write the proposal pack as JSON + a founder-readable markdown (learning_loop pattern)."""
    pdir = _proposals_dir(repo_root)
    atomic_write_text(pdir / PACK_JSON, pack.model_dump_json(indent=2) + "\n")
    atomic_write_text(pdir / PACK_MD, _render_markdown(pack))


def _render_markdown(pack: LearningProposalSet) -> str:
    lines = [
        "# HyperWilfred — Learning Proposals (review-only)",
        "",
        f"Generated: {pack.generated_at.isoformat()}",
        f"Next action: {pack.next_action}",
        "",
        "> Every proposal is review-only. The factory NEVER applies one to itself; a human "
        "approves the review gate, then `hw promote` runs it as a normal IMPROVE run (human-gated "
        "at ship like any other).",
        "",
        "## Proposals",
        "",
    ]
    if not pack.proposals:
        lines.extend(["_No proposals — the run history shows no recurring code anomalies._", ""])
    for i, p in enumerate(pack.proposals, start=1):
        lines.extend(
            [
                f"### {i}. {p.title}",
                "",
                f"- ID: `{p.proposal_id}`",
                f"- Type: `{p.proposal_type.value}`",
                f"- Status: `{p.status.value}`",
                f"- Evidence: {evidence_strength(p)}",
                f"- Confidence: `{p.confidence:.2f}` ({p.confidence_basis})",
                f"- Affected area: `{p.affected_area}`",
                f"- Requires human review: `{p.requires_human_review}`  ·  "
                f"Auto-apply: `{p.auto_apply}`",
                "",
                "**Why proposed**",
                "",
                p.rationale,
                "",
                *([f"**After promotion:** {effect_summary(p)}", ""] if effect_summary(p) else []),
                "**Suggested change**",
                "",
                p.suggested_change,
                "",
                "**As an IMPROVE objective**",
                "",
                f"> {p.objective.objective}",
                "",
                "**Source runs**",
                "",
            ]
        )
        if p.source_run_ids:
            lines.extend(f"- `{rid}`" for rid in p.source_run_ids)
        else:
            lines.append("- (none)")
        lines.append("")
    return "\n".join(lines) + "\n"


__all__ = [
    "LearningError",
    "analyze",
    "effect_summary",
    "evidence_strength",
    "learn",
    "list_proposals",
    "load_proposal",
    "open_review_gate",
    "promote",
    "proposals_view",
    "review_status",
]
