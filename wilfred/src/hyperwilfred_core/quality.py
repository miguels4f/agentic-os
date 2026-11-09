"""The quality loop — the 0-5 subjective evaluator + adversarial critics + surgical patcher.

Shared by BUILD and IMPROVE: only the *front* of the pipeline differs; the evaluator + gate +
patcher core is identical. Like the rest of the engine, this module **never calls an LLM and
never spawns a sub-agent** — it PLANS the evaluator/critic/patcher packets and GATES their
results read back from disk, exactly like the BUILD fan-out. The adapter does the spawning.

The precedence rule (spec §2) is enforced structurally here:

* A failing **deterministic** gate (build/lint/type/test) is a HARD WALL — ``quality_gate``
  refuses to pass while the deterministic gate step is not green, and a wholesale patch is
  rejected at acceptance time. After every accepted patch the deterministic gate is reset to
  PENDING so the adapter MUST re-run it (a patch can break a test) before the loop can pass.
* The 0-5 evaluator gates on the **MIN** of its criteria (not the average), recomputed by the
  engine so an evaluator cannot game the gate.
* Exhausting the iteration budget never creates a false pass. A subjective shortfall or an open
  blocker/major remains incomplete and escalates instead of claiming different historical bytes.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

from hyperwilfred_core import approvals, gates, guardrails, severity_floor
from hyperwilfred_core.pipeline import load_instruction
from hyperwilfred_core.run_store import RunEventStatus, RunStore, atomic_write_text
from hyperwilfred_core.schemas import (
    BuildMode,
    CriticSeverity,
    CritiquePacket,
    CritiqueReport,
    GuardrailConfig,
    IndependentReviewOverride,
    PatchPacket,
    Plan,
    QualityIterationRecord,
    QualityLoopState,
    QualityPhase,
    ReviewAuthor,
    RunState,
    RunStatus,
    StepStatus,
    SubjectiveEvaluation,
)

QUALITY_STEP_ID = "quality"

# The adversarial critic lenses. Perspective-diverse (correctness / security / maintainability
# / tests) so the panel catches failure modes a single reviewer would miss.
CRITIC_LENSES: tuple[str, ...] = ("correctness", "security", "maintainability", "tests")

# The adversarial lens BUILDS hostile inputs in temporary roots and RUNS them (it does not just
# read code). It is on by default when the objective touches personal data, gates, approvals or
# security; any objective can also set ``critic_lenses`` explicitly.
ADVERSARIAL_LENS = "adversarial"

# Opt-in read-only lenses (never switched on automatically). A REVIEW run may add them to the
# four standard lenses with ``hw review --lens``; a BUILD/IMPROVE objective may name them in
# ``critic_lenses``. They share the standard critic instruction (``quality/02_critique.md``).
CROSS_SLICE_DRIFT_LENS = "cross-slice-drift"
AI_FAILURE_MODES_LENS = "ai-failure-modes"
REVIEW_OPTIONAL_LENSES: frozenset[str] = frozenset({CROSS_SLICE_DRIFT_LENS, AI_FAILURE_MODES_LENS})
KNOWN_LENSES: frozenset[str] = frozenset(
    {*CRITIC_LENSES, ADVERSARIAL_LENS, *REVIEW_OPTIONAL_LENSES}
)
_ADVERSARIAL_TRIGGERS = re.compile(
    r"personal[\s-]data|\bpii\b|privacy|gdpr|\bconsent\b|do[\s_-]?not[\s_-]?contact|"
    r"\bgates?\b|gated|gating|approv\w*|permission|allow-?list|authori[sz]\w*|authenticat\w*|"
    r"security|secret|credential|injection|sanitis\w*|sanitiz\w*|scrub\w*|redact\w*|"
    r"blocked|fail[\s-]closed|\bphone",
    re.IGNORECASE,
)

# Surgical-patch budgets — TIGHTER than a BUILD worker's. The engine enforces these at
# acceptance time (``record_patch`` → ``guardrails.accept``), so the patch-never-regenerate
# guarantee lives in the engine, not in the absence of a Write tool on any one runtime.
PATCH_MAX_FILES = 5
PATCH_MAX_CHANGED_LINES = 80
PATCH_MAX_HUNK_SPAN = 40

DEFAULT_MAX_ITERATIONS = 3
DEFAULT_SUBJECTIVE_THRESHOLD = 4  # out of 5 (MIN across criteria)

_BLOCKING = (CriticSeverity.BLOCKER, CriticSeverity.MAJOR)


PENDING_INDEPENDENT_REVIEW = "PENDING_INDEPENDENT_REVIEW"


class QualityError(RuntimeError):
    """Raised on a quality-loop precondition violation (surfaces as CLI exit 2)."""


# --------------------------------------------------------------------------- helpers


def _det_gate_step_for(mode: BuildMode) -> str:
    """Which pipeline step is the deterministic hard wall for *mode*."""
    return "regression" if mode is BuildMode.IMPROVE else "evaluate"


def _quality_dir(repo_root: Path, run_id: str) -> Path:
    return repo_root / ".hw" / run_id / "quality"


def _iter_dir(repo_root: Path, run_id: str, iteration: int) -> Path:
    return _quality_dir(repo_root, run_id) / f"iter-{iteration}"


def _attestation_values(
    *, run_id: str, iteration: int, packet_id: str, lens: str, record_sha256: str,
) -> tuple[str, list[str]]:
    action = f"attest independent quality review {packet_id}"
    evidence = [
        f"run_id:{run_id}",
        f"iteration:{iteration}",
        f"packet_id:{packet_id}",
        f"lens:{lens}",
        f"result_sha256:{record_sha256}",
    ]
    return action, evidence


def _attested_author(
    *, paths, run_id: str, iteration: int, packet_id: str, lens: str,
    record_sha256: str, requested: str | None, attestation_file: str | None,
) -> tuple[ReviewAuthor, str | None, dict[str, str]]:
    """Promote to independent only through the external signed-review boundary."""
    declared = ReviewAuthor(requested) if requested is not None else ReviewAuthor.ADAPTER
    if declared is not ReviewAuthor.INDEPENDENT:
        return ReviewAuthor.ADAPTER, None, {}

    directory = paths.run_dir / "review-attestations"
    action, evidence = _attestation_values(
        run_id=run_id, iteration=iteration, packet_id=packet_id, lens=lens,
        record_sha256=record_sha256,
    )
    base_id = f"review-{iteration}-{hashlib.sha256(packet_id.encode()).hexdigest()[:16]}"
    gate_id = approvals.evidence_gate_id(directory, base_id, evidence)
    try:
        approvals.issue_challenge(
            directory, run_id=run_id, gate_id=gate_id, action=action, evidence=evidence,
            purpose="independent-quality-review", reuse_pending=True,
        )
    except approvals.ApprovalError as exc:
        raise QualityError(f"independent-review attestation is invalid: {exc}") from exc
    challenge = approvals.challenge_file(directory, gate_id)
    canonical_token = directory / f"{gate_id}.decision.json"
    details = {
        "attestation_challenge": str(challenge),
        "attestation_decision": str(canonical_token),
    }
    if attestation_file is None:
        return ReviewAuthor.ADAPTER, None, details

    supplied = Path(attestation_file)
    try:
        if supplied.resolve() != canonical_token.resolve():
            atomic_write_text(canonical_token, supplied.read_text(encoding="utf-8"))
        decision, _reviewer, _note = approvals.verify_and_consume(
            directory, gate_id, canonical_token, expected_run_id=run_id,
            public_key_path=approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH,
            expected_action=action, expected_evidence=evidence,
        )
    except (approvals.ApprovalError, OSError, UnicodeError) as exc:
        raise QualityError(f"independent-review attestation is invalid: {exc}") from exc
    if decision != "approve":
        raise QualityError(f"independent-review attestation was {decision}, not approved")
    return ReviewAuthor.INDEPENDENT, gate_id, details


def _ensure_quality(state: RunState) -> QualityLoopState:
    """Lazily initialise the quality sub-state the first time the loop is touched."""
    if state.quality is None:
        state.quality = QualityLoopState(
            step_id=QUALITY_STEP_ID,
            det_gate_step=_det_gate_step_for(state.mode),
            max_iterations=DEFAULT_MAX_ITERATIONS,
            subjective_threshold=DEFAULT_SUBJECTIVE_THRESHOLD,
        )
    return state.quality


def _deterministic_green(state: RunState) -> bool:
    """True iff the run's deterministic gate step is currently green.

    The hard wall: the quality loop can never PASS (or fall back) while the deterministic gate
    is red or has not been (re-)run for the current iteration.
    """
    q = state.quality
    if q is None:  # pragma: no cover — callers always ensure quality first
        return False
    det = q.det_gate_step
    return (
        state.step_status.get(det) == StepStatus.DONE
        and state.status is not RunStatus.GATE_FAILED
    )


def _changed_product_paths(repo_root: Path, base_commit: str | None = None) -> list[str]:
    """Committed and working-tree paths touched since the run began."""
    # Review scope only (which files the critics read), never authority: tolerant on purpose.
    paths = set(guardrails.changed_paths(str(repo_root), strict=False))
    if base_commit:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "diff", "--name-only", f"{base_commit}..HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "unknown git error"
            raise QualityError(
                f"cannot determine committed review scope from {base_commit!r}: {detail}"
            )
        paths.update(completed.stdout.splitlines())
    out: list[str] = []
    for p in sorted(paths):
        if p == ".hw" or p.startswith(".hw/"):
            continue
        out.append(p)
    return out


def _critique_target_paths(repo_root: Path, run_id: str) -> list[str]:
    """The file list the critic fan-out reviews — generalized over an arbitrary target (Phase 6).

    If a REVIEW target was persisted (``review-target.json``, only present on a REVIEW run), the
    critics review its resolved paths — an arbitrary subset / whole repo / git diff range. Otherwise
    (every BUILD / IMPROVE run) they review the build's changed product paths, exactly as before.
    Additive: with no ``review-target.json`` the behaviour is byte-identical to the Phase-2 path.
    """
    target_path = repo_root / ".hw" / run_id / "review-target.json"
    if target_path.exists():
        try:
            from hyperwilfred_core.schemas import ReviewTarget

            target = ReviewTarget.model_validate_json(target_path.read_text(encoding="utf-8"))
            return list(target.resolved_paths)
        except (OSError, ValueError) as exc:
            raise QualityError(f"review target is unreadable or invalid: {exc}") from exc
    from hyperwilfred_core.engine import RunPaths, load_state

    state = load_state(RunPaths(repo_root, run_id))
    return _changed_product_paths(repo_root, state.base_commit)


def _load_guardrails(repo_root: Path, run_id: str) -> GuardrailConfig:
    path = repo_root / ".hw" / run_id / "guardrails.json"
    return GuardrailConfig.model_validate_json(path.read_text(encoding="utf-8"))


def _load_plan(repo_root: Path, run_id: str) -> Plan | None:
    path = repo_root / ".hw" / run_id / "plan.json"
    if not path.exists():
        return None
    try:
        return Plan.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _patch_allowed_globs(repo_root: Path, run_id: str, product_dir: str) -> list[str]:
    """Where the surgical patcher may write: the union of the plan's slice scopes.

    Falls back to the product dir if there is no plan (e.g. a manual run) — the patcher can
    never write outside what the build itself was allowed to touch.
    """
    plan = _load_plan(repo_root, run_id)
    if plan and plan.slices:
        globs: list[str] = []
        for s in plan.slices:
            globs.extend(g for g in s.allowed_globs if g.strip())
        if globs:
            return sorted(set(globs))
    return [f"{product_dir.rstrip('/')}/**"]


def _read_critique_reports(repo_root: Path, run_id: str, iteration: int) -> list[CritiqueReport]:
    critics_dir = _iter_dir(repo_root, run_id, iteration) / "critics"
    if not critics_dir.exists():
        return []
    reports: list[CritiqueReport] = []
    for f in sorted(critics_dir.glob("*.json")):
        try:
            report = CritiqueReport.model_validate_json(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        # Deterministic severity floor, re-applied on every read so a hand-edited or legacy file
        # can never smuggle a personal-data / bypass / divergence finding through as "minor".
        report.findings = [severity_floor.floor_finding(x) for x in report.findings]
        reports.append(report)
    return reports


def _load_subjective(repo_root: Path, run_id: str, iteration: int) -> SubjectiveEvaluation | None:
    path = _iter_dir(repo_root, run_id, iteration) / "subjective.json"
    if not path.exists():
        return None
    evaluation = SubjectiveEvaluation.model_validate_json(path.read_text(encoding="utf-8"))
    evaluation.issues = [severity_floor.floor_issue(i) for i in evaluation.issues]
    return evaluation


# --------------------------------------------------------------------------- state I/O (shared)


def _io() -> tuple:
    """Lazy import of the engine's state I/O to keep this module acyclic.

    quality.py is imported by engine.py; importing the engine's helpers at call time (after
    both modules are fully loaded) avoids a circular import while keeping ONE source of truth
    for atomic state writes + the run-path layout.
    """
    from hyperwilfred_core.engine import RunPaths, load_state, save_state

    return RunPaths, load_state, save_state


# --------------------------------------------------------------------------- review provenance


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_lenses(state: RunState) -> tuple[str, ...]:
    """The critic lens set for this run: explicit ``critic_lenses``, else auto (see above).

    REVIEW runs always use the four standard lenses, plus any opt-in lens from
    ``REVIEW_OPTIONAL_LENSES`` named in ``critic_lenses``. The standard four can never be dropped,
    and the adversarial lens is refused (REVIEW critics are read-only — they cannot run hostile
    inputs).
    """
    obj = state.objective
    if state.mode is BuildMode.REVIEW:
        return review_lenses(obj.critic_lenses)
    if obj.critic_lenses:
        unknown = sorted(set(obj.critic_lenses) - KNOWN_LENSES)
        if unknown:
            raise QualityError(
                f"objective.critic_lenses has unknown lens(es) {unknown}; "
                f"known: {sorted(KNOWN_LENSES)}"
            )
        return tuple(dict.fromkeys(obj.critic_lenses))
    text = "\n".join([obj.title, obj.objective, *obj.acceptance_criteria, *obj.constraints])
    if _ADVERSARIAL_TRIGGERS.search(text):
        return (*CRITIC_LENSES, ADVERSARIAL_LENS)
    return CRITIC_LENSES


def review_lenses(requested: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """The REVIEW panel for *requested* lenses: the standard four, then each opt-in extra once.

    Fails closed on any extra lens that is not in ``REVIEW_OPTIONAL_LENSES`` (``adversarial``
    included), whether it came from ``hw review --lens`` or from hand-edited run state.
    """
    extra = [lens for lens in dict.fromkeys(requested) if lens not in CRITIC_LENSES]
    refused = sorted(set(extra) - REVIEW_OPTIONAL_LENSES)
    if refused:
        raise QualityError(
            f"REVIEW runs accept only the optional lens(es) {sorted(REVIEW_OPTIONAL_LENSES)} on "
            f"top of the four standard lenses; refused {refused}"
        )
    return (*CRITIC_LENSES, *extra)


def required_lenses(state: RunState) -> tuple[str, ...]:
    """The critic lenses that MUST be independently authored before the loop may pass."""
    return resolve_lenses(state)


def _review_evidence(repo_root: Path, run_id: str, q: QualityLoopState) -> list[str]:
    directory = _iter_dir(repo_root, run_id, q.iteration)
    files = [directory / "subjective.json", *sorted((directory / "critics").glob("*.json"))]
    return [
        *(f"record:{key}:{value}" for key, value in sorted(q.record_hashes.items())
          if key.startswith(f"{q.iteration}:")),
        *(f"file:{path.relative_to(directory)}:{_sha(path)}" for path in files if path.is_file()),
    ]


def _reset_review(state: RunState) -> None:
    q = _ensure_quality(state)
    q.passed = False
    q.phase = QualityPhase.EVALUATE
    if state.step_status.get(QUALITY_STEP_ID) is StepStatus.DONE:
        state.step_status[QUALITY_STEP_ID] = StepStatus.PENDING


def _record_intact(*, q: QualityLoopState, key: str, path: Path) -> bool:
    """The record on disk is byte-identical to what the engine recorded."""
    expected = q.record_hashes.get(f"{q.iteration}:{key}")
    return expected is not None and path.exists() and _sha(path) == expected


def _record_attested(
    *, repo_root: Path, run_id: str, q: QualityLoopState, key: str, path: Path,
    packet_id: str, lens: str,
) -> bool:
    """The record is intact AND its external independent-review attestation verifies."""
    gate_id = q.review_attestations.get(f"{q.iteration}:{key}")
    if gate_id is None or not _record_intact(q=q, key=key, path=path):
        return False
    directory = repo_root / ".hw" / run_id / "review-attestations"
    action, evidence = _attestation_values(
        run_id=run_id, iteration=q.iteration, packet_id=packet_id, lens=lens,
        record_sha256=_sha(path),
    )
    token = directory / f"{gate_id}.decision.json"
    try:
        decision, _reviewer, _note = approvals.verify_and_consume(
            directory, gate_id, token, expected_run_id=run_id,
            public_key_path=approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH,
            expected_action=action, expected_evidence=evidence, consume=False, recorded=True,
        )
        return decision == "approve"
    except (approvals.ApprovalError, OSError, ValueError):
        return False


def critic_authorship(*, repo_root: Path, run_id: str, state: RunState) -> dict[str, str]:
    """Who wrote each required critique of the current iteration (fail closed).

    ``independent`` only when the record says so, is byte-identical to what the engine recorded
    AND its signed attestation verifies — the same rule as :func:`provenance_report`.
    ``adapter`` (the orchestrating session's self-review) otherwise; ``missing`` if absent.
    Read-only: unlike :func:`provenance_report` it never initialises quality state.
    """
    lenses = resolve_lenses(state)
    q = state.quality
    if q is None:
        return dict.fromkeys(lenses, "missing")
    critics = _iter_dir(repo_root, run_id, q.iteration) / "critics"
    authors: dict[str, str] = {}
    for lens in lenses:
        # Only the engine-written ``<lens>.json`` is a record; stray files are never read.
        path = critics / f"{lens}.json"
        report: CritiqueReport | None = None
        if path.is_file():
            try:
                report = CritiqueReport.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                report = None
        if report is None or report.lens != lens:
            authors[lens] = "missing"
        elif report.author is ReviewAuthor.INDEPENDENT and _record_attested(
            repo_root=repo_root, run_id=run_id, q=q, key=lens, path=path,
            packet_id=f"critic-{q.iteration}-{lens}", lens=lens,
        ):
            authors[lens] = ReviewAuthor.INDEPENDENT.value
        else:
            authors[lens] = ReviewAuthor.ADAPTER.value
    return authors


def provenance_report(*, repo_root: Path, run_id: str, state: RunState) -> dict:
    """Who authored the FINAL iteration's evaluation + critiques (fail closed).

    ``ok`` is True iff the subjective evaluation and every required lens are present AND
    independent. Anything adapter-authored, unlabelled (legacy state) or missing is not.
    """
    q = _ensure_quality(state)
    subjective = _load_subjective(repo_root, run_id, q.iteration)
    reports = {r.lens: r for r in _read_critique_reports(repo_root, run_id, q.iteration)}
    iter_dir = _iter_dir(repo_root, run_id, q.iteration)

    def _attested(key: str, path: Path, packet_id: str, lens: str) -> bool:
        return _record_attested(
            repo_root=repo_root, run_id=run_id, q=q, key=key, path=path,
            packet_id=packet_id, lens=lens,
        )

    adapter: list[str] = []
    missing: list[str] = []
    independent: list[str] = []
    if subjective is None:
        missing.append("subjective")
    elif subjective.author is ReviewAuthor.INDEPENDENT and _attested(
        "subjective", iter_dir / "subjective.json", f"subjective-{q.iteration}", "subjective"
    ):
        independent.append("subjective")
    else:
        adapter.append("subjective")
    for lens in required_lenses(state):
        report = reports.get(lens)
        if report is None:
            missing.append(lens)
        elif report.author is ReviewAuthor.INDEPENDENT and _attested(
            lens, iter_dir / "critics" / f"{lens}.json", f"critic-{q.iteration}-{lens}", lens
        ):
            independent.append(lens)
        else:
            adapter.append(lens)
    override = q.independent_review_override
    override_active = False
    if override is not None and override.iteration == q.iteration and override.gate_id:
        gates_dir = repo_root / ".hw" / run_id / "gates"
        token = gates_dir / f"{override.gate_id}.decision.json"
        evidence = [f"claimed_by:{override.claimed_by}", f"reason:{override.claimed_reason}",
                    *_review_evidence(repo_root, run_id, q)]
        try:
            decision, signed_by, signed_note = approvals.verify_and_consume(
                gates_dir, override.gate_id, token, expected_run_id=run_id,
                expected_action=(
                    f"accept non-independent quality review for iteration {q.iteration}"
                ),
                expected_evidence=evidence, consume=False, recorded=True, policy_eligible=True,
            )
            override_active = (decision == "approve" and override.evidence == evidence
                               and override.by == signed_by
                               and override.reason == (signed_note or override.claimed_reason))
        except (approvals.ApprovalError, ValueError, OSError):
            pass
    return {
        "iteration": q.iteration,
        "ok": not adapter and not missing,
        "independent": independent,
        "adapter_authored": adapter,
        "missing": missing,
        "override": override.model_dump(mode="json") if override_active and override else None,
    }


def provenance_banner(report: dict) -> list[str]:
    """Human-facing lines for verify output / the ship gate. Empty when fully independent."""
    lines: list[str] = []
    override = report.get("override")
    if not report["ok"]:
        parts = []
        if report["adapter_authored"]:
            parts.append("adapter-authored (self-review): " + ", ".join(report["adapter_authored"]))
        if report["missing"]:
            parts.append("missing: " + ", ".join(report["missing"]))
        lines.append(
            f"{PENDING_INDEPENDENT_REVIEW} — quality review is NOT independent; "
            + "; ".join(parts)
        )
    if override and override["by"] == approvals.POLICY_APPROVER:
        lines.append(
            "POLICY AUTO-APPROVAL (no human signature) of non-independent review at "
            f"{override['at']}; requested by {override['claimed_by']}: "
            f"{override['claimed_reason']}"
        )
    elif override:
        lines.append(
            f"HUMAN OVERRIDE of independent review by {override['by']} at {override['at']}: "
            f"{override['reason']}"
        )
    return lines


def record_independent_override(
    *, repo_root: Path, run_id: str, by: str, reason: str
) -> dict:
    """Record an explicit human decision to accept non-independent review for THIS iteration."""
    if not by.strip() or not reason.strip():
        raise QualityError("an override needs both --by (who) and --reason (why)")
    RunPaths, load_state, save_state = _io()
    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)
    q = _ensure_quality(state)
    gates_dir = paths.run_dir / "gates"
    action = f"accept non-independent quality review for iteration {q.iteration}"
    evidence = [
        f"claimed_by:{by.strip()}",
        f"reason:{reason.strip()}",
        *_review_evidence(repo_root, run_id, q),
    ]
    gate_id = approvals.evidence_gate_id(gates_dir, f"independent-override-{q.iteration}", evidence)
    challenge_path = approvals.challenge_file(gates_dir, gate_id)
    decision_path = gates_dir / f"{gate_id}.decision.json"
    try:
        approvals.issue_challenge(
            gates_dir,
            run_id=run_id,
            gate_id=gate_id,
            action=action,
            evidence=evidence,
            purpose="quality-review-override",
            reuse_pending=True,
        )
    except approvals.ApprovalError as exc:
        raise QualityError(f"independent-review override is not authorized: {exc}") from exc
    # With the owner's policy switch on, this approval point needs no human signature.
    approvals.policy_approve(gates_dir, gate_id, eligible=True)
    if not decision_path.exists():
        raise QualityError(
            "independent-review override blocked: a trusted human must sign the challenge at "
            f"{challenge_path}; place the signed token at {decision_path}, then re-run"
        )
    try:
        decision, signed_by, signed_note = approvals.verify_and_consume(
            gates_dir,
            gate_id,
            decision_path,
            expected_run_id=run_id,
            expected_action=action,
            expected_evidence=evidence,
            policy_eligible=True,
        )
    except approvals.ApprovalError as exc:
        raise QualityError(f"independent-review override is not authorized: {exc}") from exc
    if decision != "approve":
        raise QualityError(f"independent-review override was {decision}, not approved")
    q.independent_review_override = IndependentReviewOverride(
        by=signed_by, reason=signed_note or reason.strip(), iteration=q.iteration,
        gate_id=gate_id, evidence=evidence, claimed_by=by.strip(), claimed_reason=reason.strip(),
    )
    state.quality = q
    save_state(paths, state)
    RunStore(paths.run_dir).append_event(
        run_id=run_id, stage=QUALITY_STEP_ID, status=RunEventStatus.running,
        message=(f"POLICY AUTO-APPROVAL (no human signature) of non-independent review "
                 f"(iter {q.iteration}); requested by {by.strip()}: {reason.strip()}"
                 if signed_by == approvals.POLICY_APPROVER else
                 f"SIGNED HUMAN OVERRIDE of independent review (iter {q.iteration}) "
                 f"by {signed_by}: {signed_note or reason}"),
    )
    return {"run_id": run_id, "override": q.independent_review_override.model_dump(mode="json")}


# --------------------------------------------------------------------------- hw quality-status


def quality_status(*, repo_root: Path, run_id: str) -> dict:
    """Report the loop's current phase and what the adapter must do next."""
    RunPaths, load_state, save_state = _io()
    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)
    q = _ensure_quality(state)
    save_state(paths, state)

    product_dir = state.product_dir
    base: dict = {
        "run_id": run_id,
        "phase": q.phase.value,
        "iteration": q.iteration,
        "max_iterations": q.max_iterations,
        "subjective_threshold": q.subjective_threshold,
        "det_gate_step": q.det_gate_step,
        "deterministic_green": _deterministic_green(state),
        "product_dir": product_dir,
    }
    if q.phase in (QualityPhase.EVALUATE, QualityPhase.PASSED, QualityPhase.FALLBACK):
        prov = provenance_report(repo_root=repo_root, run_id=run_id, state=state)
        base["provenance"] = prov
        base["provenance_banner"] = provenance_banner(prov)
        if q.passed and not prov["ok"] and prov["override"] is None:
            _reset_review(state)
            base["phase"] = q.phase.value
            save_state(paths, state)

    if q.phase is QualityPhase.EVALUATE:
        iter_dir = _iter_dir(repo_root, run_id, q.iteration)
        base.update(
            {
                "action": "evaluate",
                "subjective_instruction": load_instruction("quality/01_subjective.md"),
                "subjective_result_path": str(
                    (iter_dir / "subjective.json").relative_to(repo_root)
                ),
                "changed_paths": _changed_product_paths(repo_root, state.base_commit),
                "next": (
                    "run the deterministic gate (`hw evaluate --step "
                    f"{q.det_gate_step}`), then write the 0-5 evaluation, call "
                    "`hw record-subjective`, run `hw critique-plan` + the critics, then "
                    "`hw quality-gate`"
                ),
            }
        )
    elif q.phase is QualityPhase.PATCH:
        base.update({"action": "patch", "next": "run `hw patch-plan` then dispatch the patcher"})
    elif q.phase is QualityPhase.PASSED:
        base.update({"action": "done", "passed": True, "used_fallback": q.used_fallback})
    elif q.phase is QualityPhase.FALLBACK:
        base.update({"action": "done", "passed": True, "used_fallback": True})
    else:  # ESCALATED
        base.update({"action": "escalated", "passed": False, "open_blocker": q.open_blocker})
    return base


# --------------------------------------------------------------------------- hw record-subjective


def record_subjective(
    *, repo_root: Path, run_id: str, result_file: str, author: str | None = None,
    attestation_file: str | None = None,
) -> dict:
    """Ingest the 0-5 evaluation, recompute ``global_score`` as the MIN, persist it.

    The engine OVERWRITES whatever ``global_score`` the evaluator reported with the MIN of the
    criteria — the gate cannot be gamed by a high self-reported aggregate over low criteria.
    """
    RunPaths, load_state, save_state = _io()
    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)
    q = _ensure_quality(state)

    src = Path(result_file)
    if not src.exists():
        raise QualityError(f"subjective result not found at {result_file}")
    evaluation = SubjectiveEvaluation.model_validate_json(src.read_text(encoding="utf-8"))
    if not evaluation.criteria:
        raise QualityError("subjective evaluation has no criteria — cannot compute a MIN gate")

    min_score = min(c.score for c in evaluation.criteria)
    evaluation.global_score = min_score  # AUTHORITATIVE: engine recomputes MIN, ignores input
    evaluation.iteration = q.iteration
    evaluation.issues = [severity_floor.floor_issue(i) for i in evaluation.issues]
    # Compute the exact persisted independent form before accepting provenance. A caller-controlled
    # label alone is never authority; only the separately held review key can promote the record.
    evaluation.author = ReviewAuthor.INDEPENDENT
    independent_bytes = evaluation.model_dump_json(indent=2) + "\n"
    accepted_author, attestation_id, attestation = _attested_author(
        paths=paths, run_id=run_id, iteration=q.iteration,
        packet_id=f"subjective-{q.iteration}", lens="subjective",
        record_sha256=hashlib.sha256(independent_bytes.encode()).hexdigest(),
        requested=author, attestation_file=attestation_file,
    )
    evaluation.author = accepted_author

    iter_dir = _iter_dir(repo_root, run_id, q.iteration)
    atomic_write_text(
        iter_dir / "subjective.json", evaluation.model_dump_json(indent=2) + "\n"
    )
    q.record_hashes[f"{q.iteration}:subjective"] = _sha(iter_dir / "subjective.json")
    attestation_key = f"{q.iteration}:subjective"
    if attestation_id is None:
        q.review_attestations.pop(attestation_key, None)
    else:
        q.review_attestations[attestation_key] = attestation_id
    _reset_review(state)

    q.last_global_score = min_score
    if q.best_global_score is None or min_score > q.best_global_score:
        q.best_global_score = min_score
        q.best_iteration = q.iteration
    state.quality = q
    save_state(paths, state)

    RunStore(paths.run_dir).append_event(
        run_id=run_id,
        stage=QUALITY_STEP_ID,
        status=RunEventStatus.running,
        message=f"Subjective eval iter {q.iteration}: global_score={min_score} (MIN of "
        f"{[c.score for c in evaluation.criteria]})",
    )
    return {
        "run_id": run_id,
        "iteration": q.iteration,
        "global_score": min_score,
        "scores": [c.score for c in evaluation.criteria],
        "aggregate_rule": "min",
        "author": evaluation.author.value,
        "passes_threshold": min_score >= q.subjective_threshold,
        **attestation,
    }


# --------------------------------------------------------------------------- hw critique-plan


def critique_plan(*, repo_root: Path, run_id: str) -> dict:
    """Emit one critic packet per lens.

    REVIEW adapters run them sequentially because each critic gets a separately compiled,
    fail-closed write boundary for its sole result file.
    """
    RunPaths, load_state, save_state = _io()
    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)
    q = _ensure_quality(state)
    save_state(paths, state)

    product_dir = state.product_dir
    changed = _critique_target_paths(repo_root, run_id)
    standard_instruction = load_instruction("quality/02_critique.md")
    adversarial_instruction = load_instruction("quality/02b_adversarial.md")
    iter_dir = _iter_dir(repo_root, run_id, q.iteration)
    packet_dir = iter_dir / "critic-packets"

    packets: list[dict] = []
    for lens in resolve_lenses(state):
        instruction = adversarial_instruction if lens == ADVERSARIAL_LENS else standard_instruction
        packet_id = f"critic-{q.iteration}-{lens}"
        result_path = str((iter_dir / "critics" / f"{lens}.json").relative_to(repo_root))
        packet = CritiquePacket(
            packet_id=packet_id,
            run_id=run_id,
            iteration=q.iteration,
            lens=lens,
            instruction=instruction,
            product_dir=product_dir,
            changed_paths=changed,
            result_path=result_path,
        )
        atomic_write_text(
            packet_dir / f"{lens}.json", packet.model_dump_json(indent=2) + "\n"
        )
        packets.append(packet.model_dump(mode="json"))

    RunStore(paths.run_dir).append_event(
        run_id=run_id,
        stage=QUALITY_STEP_ID,
        status=RunEventStatus.running,
        message=f"Critique planned iter {q.iteration}: {len(packets)} lens(es) over "
        f"{len(changed)} changed path(s)",
    )
    return {"run_id": run_id, "iteration": q.iteration, "critics": packets}


# --------------------------------------------------------------------------- hw record-critique


def record_critique(
    *, repo_root: Path, run_id: str, packet_id: str, result_file: str,
    author: str | None = None, attestation_file: str | None = None,
) -> dict:
    """Ingest+validate one critic's findings JSON (read-only — no acceptance gate)."""
    RunPaths, load_state, save_state = _io()
    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)
    q = _ensure_quality(state)

    iter_dir = _iter_dir(repo_root, run_id, q.iteration)
    packets = [
        CritiquePacket.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted((iter_dir / "critic-packets").glob("*.json"))
    ]
    matching = [packet for packet in packets if packet.packet_id == packet_id]
    if len(matching) != 1:
        raise QualityError(
            f"unknown or ambiguous critique packet {packet_id!r} for iteration {q.iteration}"
        )
    packet = matching[0]
    if packet.run_id != run_id or packet.iteration != q.iteration:
        raise QualityError(f"critique packet {packet_id!r} is stale or belongs to another run")

    src = Path(result_file)
    if not src.is_absolute():
        src = repo_root / src
    expected = repo_root / packet.result_path
    if Path(os.path.abspath(src)) != Path(os.path.abspath(expected)):
        raise QualityError(
            f"critique result for {packet_id!r} must be read from {packet.result_path}"
        )
    candidate = repo_root.resolve()
    for component in Path(packet.result_path).parts:
        candidate /= component
        if candidate.is_symlink():
            raise QualityError(
                f"critique result for {packet_id!r} crosses symlink {candidate}"
            )
    if not src.exists():
        raise QualityError(f"critique result not found at {result_file}")
    report = CritiqueReport.model_validate_json(src.read_text(encoding="utf-8"))
    if report.lens != packet.lens:
        raise QualityError(
            f"critique report lens {report.lens!r} does not match packet lens {packet.lens!r}"
        )
    if report.lens == ADVERSARIAL_LENS:
        _require_adversarial_evidence(report)
    report.findings = [severity_floor.floor_finding(f) for f in report.findings]
    report.author = ReviewAuthor.INDEPENDENT
    independent_bytes = report.model_dump_json(indent=2) + "\n"
    accepted_author, attestation_id, attestation = _attested_author(
        paths=paths, run_id=run_id, iteration=q.iteration, packet_id=packet_id,
        lens=report.lens, record_sha256=hashlib.sha256(independent_bytes.encode()).hexdigest(),
        requested=author, attestation_file=attestation_file,
    )
    report.author = accepted_author

    atomic_write_text(
        iter_dir / "critics" / f"{report.lens}.json", report.model_dump_json(indent=2) + "\n"
    )
    q.record_hashes[f"{q.iteration}:{report.lens}"] = _sha(
        iter_dir / "critics" / f"{report.lens}.json"
    )
    attestation_key = f"{q.iteration}:{report.lens}"
    if attestation_id is None:
        q.review_attestations.pop(attestation_key, None)
    else:
        q.review_attestations[attestation_key] = attestation_id
    _reset_review(state)
    state.quality = q
    save_state(paths, state)

    by_sev = {s.value: 0 for s in CriticSeverity}
    for f in report.findings:
        by_sev[f.severity.value] += 1
    RunStore(paths.run_dir).append_event(
        run_id=run_id,
        stage=QUALITY_STEP_ID,
        status=RunEventStatus.running,
        message=f"Critique '{report.lens}' (packet {packet_id}): "
        f"{len(report.findings)} finding(s) {by_sev}",
    )
    return {
        "run_id": run_id,
        "iteration": q.iteration,
        "lens": report.lens,
        "author": report.author.value,
        "raised_by_floor": sum(1 for f in report.findings if f.severity_raised_from is not None),
        "findings": len(report.findings),
        "by_severity": by_sev,
        **attestation,
    }


def _require_adversarial_evidence(report: CritiqueReport) -> None:
    """The adversarial lens must PROVE it ran hostile inputs: probes + per-finding evidence."""
    if not report.probes or any(
        not p.command.strip() or not p.observed_output.strip() for p in report.probes
    ):
        raise QualityError(
            "adversarial critique must list the hostile inputs it RAN as `probes` "
            "(input_class + command + observed_output) — reading the code is not enough"
        )
    bare = [
        f.id for f in report.findings
        if not f.evidence_command.strip() or not f.evidence_output.strip()
    ]
    if bare:
        raise QualityError(
            f"adversarial finding(s) {bare} must cite the command that was run "
            "(`evidence_command`) and the observed output (`evidence_output`)"
        )


# --------------------------------------------------------------------------- hw quality-gate


def quality_gate(*, repo_root: Path, run_id: str) -> dict:
    """THE quality decision: pass / iterate / escalate (precedence-correct).

    Order of checks IS the precedence rule:
      1. deterministic gate not green → block (hard wall, never waived).
      2. open BLOCKER finding → iterate if budget remains, else escalate to a human.
      2b. PASS/FALLBACK require an INDEPENDENT final-iteration review, else
          PENDING_INDEPENDENT_REVIEW (or a recorded human override).
      3. subjective MIN >= threshold → PASS.
      4. budget remains → iterate (patch).
      5. budget exhausted with subjective shortfall → escalate as incomplete.
    """
    RunPaths, load_state, save_state = _io()
    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)
    q = _ensure_quality(state)

    subjective = _load_subjective(repo_root, run_id, q.iteration)
    if subjective is None:
        raise QualityError(
            f"no subjective evaluation for iteration {q.iteration} — run `hw record-subjective`"
        )

    reports = _read_critique_reports(repo_root, run_id, q.iteration)
    open_blocker = sum(1 for i in subjective.issues if i.severity is CriticSeverity.BLOCKER)
    open_major = sum(1 for i in subjective.issues if i.severity is CriticSeverity.MAJOR)
    open_minor = sum(1 for i in subjective.issues if i.severity is CriticSeverity.MINOR)
    # Severity-floor classes (personal-data leak / gate bypass / UI-engine divergence) at >= major
    # are must-fix: they gate exactly like a blocker so they cannot be waived by a high score.
    open_floor = sum(
        1
        for i in subjective.issues
        if i.severity is not CriticSeverity.MINOR
        and severity_floor.floor_classes(i.tags, f"{i.criterion}\n{i.description}")
    )
    for r in reports:
        for f in r.findings:
            if f.severity is CriticSeverity.BLOCKER:
                open_blocker += 1
            elif f.severity is CriticSeverity.MAJOR:
                open_major += 1
            else:
                open_minor += 1
            if f.severity is not CriticSeverity.MINOR and severity_floor.floor_classes(
                f.tags, f"{f.title}\n{f.description}"
            ):
                open_floor += 1

    det_green = _deterministic_green(state)
    q.open_blocker = open_blocker
    q.open_major = open_major
    q.open_minor = open_minor
    q.history.append(
        QualityIterationRecord(
            iteration=q.iteration,
            subjective_global_score=subjective.global_score,
            open_blocker=open_blocker,
            open_major=open_major,
            deterministic_green=det_green,
        )
    )

    store = RunStore(paths.run_dir)
    has_budget = (q.iteration + 1) < q.max_iterations

    # 1. Deterministic hard wall — never waived, re-checked every iteration.
    if not det_green:
        state.quality = q
        save_state(paths, state)
        return {
            "run_id": run_id,
            "action": "deterministic_gate_required",
            "det_gate_step": q.det_gate_step,
            "reason": "deterministic gate is not green — run `hw evaluate` (hard wall) first",
        }

    # 2. Open blocker, major, or floor-class must-fix must be resolved or escalated.
    if open_blocker + open_major + open_floor > 0:
        if has_budget:
            q.phase = QualityPhase.PATCH
            state.quality = q
            save_state(paths, state)
            store.append_event(
                run_id=run_id, stage=QUALITY_STEP_ID, status=RunEventStatus.running,
                message=f"iter {q.iteration}: {open_blocker} blocker(s), {open_major} major(s), "
                f"{open_floor} severity-floor must-fix → patch",
            )
            return {"run_id": run_id, "action": "patch", "reason": "open major findings",
                    "open_blocker": open_blocker, "open_major": open_major,
                    "open_floor_must_fix": open_floor}
        q.phase = QualityPhase.ESCALATED
        q.escalated = True
        state.quality = q
        state.status = RunStatus.AWAITING_HUMAN
        state.step_status[QUALITY_STEP_ID] = StepStatus.FAILED
        save_state(paths, state)
        store.append_event(
            run_id=run_id, stage=QUALITY_STEP_ID, status=RunEventStatus.awaiting_human,
            message=f"{open_blocker} blocker(s) + {open_major} major(s) + {open_floor} "
            f"severity-floor must-fix survived {q.max_iterations} rounds → human gate",
        )
        return {"run_id": run_id, "action": "escalate", "open_blocker": open_blocker,
                "open_major": open_major, "open_floor_must_fix": open_floor,
                "reason": "open major findings survived the budget — escalated to a human"}

    # 3. No blocker, subjective meets threshold → PASS (if the review was independent).
    prov = provenance_report(repo_root=repo_root, run_id=run_id, state=state)
    prov_blocks = (
        not prov["ok"]
        and prov["override"] is None
        and (subjective.global_score >= q.subjective_threshold or not has_budget)
    )
    if prov_blocks:
        # Never a silent pass on self-review: a distinct, non-passing status the adapter
        # cannot mistake for success. Resolve by dispatching independent critics (re-run
        # them when limits reset) or by a recorded human override (`hw quality-override`).
        _reset_review(state)
        state.quality = q
        save_state(paths, state)
        store.append_event(
            run_id=run_id, stage=QUALITY_STEP_ID, status=RunEventStatus.running,
            message=f"iter {q.iteration}: {PENDING_INDEPENDENT_REVIEW} — "
            f"adapter-authored={prov['adapter_authored']} missing={prov['missing']}",
        )
        return {
            "run_id": run_id,
            "action": "pending_independent_review",
            "status": PENDING_INDEPENDENT_REVIEW,
            "passed": False,
            "adapter_authored": prov["adapter_authored"],
            "missing": prov["missing"],
            "reason": "the final iteration was not independently reviewed — dispatch "
            "sub-agent critics (re-dispatch after a usage-limit reset) and re-record them "
            "with author=independent, or have a human run `hw quality-override`",
        }
    if subjective.global_score >= q.subjective_threshold:
        q.phase = QualityPhase.PASSED
        q.passed = True
        state.quality = q
        state.step_status[QUALITY_STEP_ID] = StepStatus.DONE
        if (state.status is RunStatus.AWAITING_HUMAN
                and not gates.run_blocked(repo_root, run_id, state)):
            state.status = RunStatus.IN_PROGRESS
        save_state(paths, state)
        store.append_event(
            run_id=run_id, stage=QUALITY_STEP_ID, status=RunEventStatus.completed,
            message=f"quality PASS iter {q.iteration}: global_score={subjective.global_score} "
            f">= threshold {q.subjective_threshold}, no open blocker",
        )
        result = {"run_id": run_id, "action": "pass", "global_score": subjective.global_score,
                  "threshold": q.subjective_threshold}
        if prov["override"] is not None and not prov["ok"]:
            result["independent_review_override"] = prov["override"]
        return result

    # 4. Subjective shortfall, budget remains → iterate (patch).
    if has_budget:
        q.phase = QualityPhase.PATCH
        state.quality = q
        save_state(paths, state)
        store.append_event(
            run_id=run_id, stage=QUALITY_STEP_ID, status=RunEventStatus.running,
            message=f"iter {q.iteration}: global_score={subjective.global_score} < threshold "
            f"{q.subjective_threshold} → patch",
        )
        return {"run_id": run_id, "action": "patch", "reason": "subjective below threshold",
                "global_score": subjective.global_score, "threshold": q.subjective_threshold}

    # 5. A subjective shortfall remains incomplete. The engine cannot claim a historical best
    # version while leaving different bytes checked out.
    q.phase = QualityPhase.ESCALATED
    q.escalated = True
    state.quality = q
    state.status = RunStatus.AWAITING_HUMAN
    state.step_status[QUALITY_STEP_ID] = StepStatus.FAILED
    save_state(paths, state)
    store.append_event(
        run_id=run_id, stage=QUALITY_STEP_ID, status=RunEventStatus.awaiting_human,
        message=f"quality incomplete after {q.max_iterations} rounds: final global_score="
        f"{subjective.global_score} below threshold {q.subjective_threshold}",
    )
    return {"run_id": run_id, "action": "escalate", "passed": False,
            "global_score": subjective.global_score, "threshold": q.subjective_threshold,
            "reason": "subjective threshold not reached; no product revision was auto-accepted"}


# --------------------------------------------------------------------------- hw patch-plan


def patch_plan(*, repo_root: Path, run_id: str) -> dict:
    """Aggregate the open blocker/major findings and emit ONE surgical patcher packet."""
    RunPaths, load_state, save_state = _io()
    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)
    q = _ensure_quality(state)
    if q.phase is not QualityPhase.PATCH:
        raise QualityError(
            f"quality loop is in phase {q.phase.value!r}, not 'patch' — run `hw quality-gate` first"
        )

    subjective = _load_subjective(repo_root, run_id, q.iteration)
    reports = _read_critique_reports(repo_root, run_id, q.iteration)
    open_findings: list[dict] = []
    if subjective:
        for issue in subjective.issues:
            if issue.severity in _BLOCKING:
                open_findings.append(
                    {"source": "subjective", "severity": issue.severity.value,
                     "criterion": issue.criterion, "description": issue.description,
                     "suggestion": issue.suggestion}
                )
    for r in reports:
        for f in r.findings:
            if f.severity in _BLOCKING:
                open_findings.append(
                    {"source": f"critic:{r.lens}", "severity": f.severity.value,
                     "id": f.id, "file": f.file, "line": f.line, "title": f.title,
                     "description": f.description, "suggestion": f.suggestion}
                )

    iter_dir = _iter_dir(repo_root, run_id, q.iteration)
    import json as _json

    findings_path_abs = iter_dir / "open_findings.json"
    atomic_write_text(findings_path_abs, _json.dumps(open_findings, indent=2) + "\n")
    findings_path = str(findings_path_abs.relative_to(repo_root))

    guardrail = _load_guardrails(repo_root, run_id)
    plan = _load_plan(repo_root, run_id)
    product_dir = state.product_dir
    allowed = _patch_allowed_globs(repo_root, run_id, product_dir)
    packet_id = f"patch-{q.iteration}"
    result_path = str((iter_dir / "patch-result.json").relative_to(repo_root))
    packet = PatchPacket(
        packet_id=packet_id,
        run_id=run_id,
        iteration=q.iteration,
        instruction=load_instruction("quality/03_patch.md"),
        findings_path=findings_path,
        allowed_globs=allowed,
        blocked_globs=list(guardrail.global_blocked_globs),
        max_files=PATCH_MAX_FILES,
        max_changed_lines=min(PATCH_MAX_CHANGED_LINES, guardrail.max_changed_lines),
        max_hunk_span=min(PATCH_MAX_HUNK_SPAN, guardrail.max_hunk_span),
        full_file_ratio=guardrail.full_file_ratio,
        product_dir=product_dir,
        worktree_hint=f".hw/{run_id}/worktrees/{packet_id}",
        result_path=result_path,
        generated_artifacts=[
            g for s in (plan.slices if plan else []) for g in s.generated_artifacts
        ],
    )
    atomic_write_text(
        iter_dir / "patch-packet.json", packet.model_dump_json(indent=2) + "\n"
    )
    save_state(paths, state)
    RunStore(paths.run_dir).append_event(
        run_id=run_id, stage=QUALITY_STEP_ID, status=RunEventStatus.running,
        message=f"Patch planned iter {q.iteration}: {len(open_findings)} open finding(s), "
        f"budget {packet.max_changed_lines} lines / {packet.max_files} files",
    )
    return {"run_id": run_id, "iteration": q.iteration,
            "open_findings": len(open_findings), "packet": packet.model_dump(mode="json")}


# --------------------------------------------------------------------------- hw record-patch


def record_patch(
    *, repo_root: Path, run_id: str, packet_id: str, worktree: str,
    result_file: str | None = None, do_rollback: bool = True,
) -> dict:
    """Acceptance gate for the surgical patch — patch-never-regenerate (engine-enforced).

    Reuses the same dual-signal guard as the BUILD fan-out, with TIGHT budgets. On accept,
    increments the iteration and RESETS the deterministic gate to PENDING so the adapter must
    re-run it before the loop can pass (a patch can break a test). On reject, the worktree is
    rolled back byte-clean and the adapter re-dispatches a tightened, surgical-only packet.
    """
    RunPaths, load_state, save_state = _io()
    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)
    q = _ensure_quality(state)

    iter_dir = _iter_dir(repo_root, run_id, q.iteration)
    packet_path = iter_dir / "patch-packet.json"
    if not packet_path.exists():
        raise QualityError("no patch packet — run `hw patch-plan` first")
    packet = PatchPacket.model_validate_json(packet_path.read_text(encoding="utf-8"))
    if packet.packet_id != packet_id:
        raise QualityError(f"packet id mismatch: expected {packet.packet_id!r}, got {packet_id!r}")

    resolved_result = result_file or packet.result_path
    verdict = guardrails.accept(
        packet_id=packet_id,
        worktree=worktree,
        allowed_globs=packet.allowed_globs,
        blocked_globs=packet.blocked_globs,
        result_file=resolved_result,
        max_changed_lines=packet.max_changed_lines,
        max_hunk_span=packet.max_hunk_span,
        full_file_ratio=packet.full_file_ratio,
        do_rollback=do_rollback,
        generated_globs=guardrails.effective_generated_globs(
            [g.path for g in packet.generated_artifacts], packet.allowed_globs
        ),
    )
    atomic_write_text(
        iter_dir / "patch-verdict.json", verdict.model_dump_json(indent=2) + "\n"
    )

    if verdict.accepted:
        if q.history:
            q.history[-1].patched = True
        q.iteration += 1
        q.phase = QualityPhase.EVALUATE
        # Force a deterministic re-check: a patch can break a test, so the next gate decision
        # must wait for a fresh `hw evaluate`. This makes the regression re-check engine-enforced.
        state.step_status[q.det_gate_step] = StepStatus.PENDING
        state.quality = q
        save_state(paths, state)
    else:
        state.quality = q
        save_state(paths, state)

    RunStore(paths.run_dir).append_event(
        run_id=run_id, stage=QUALITY_STEP_ID,
        status=RunEventStatus.accepted if verdict.accepted else RunEventStatus.rejected,
        message=(
            f"Patch {packet_id} accepted → iter {q.iteration} (deterministic gate reset; re-run "
            f"`hw evaluate`)" if verdict.accepted
            else f"Patch {packet_id} REJECTED (escapes={verdict.escapes}, "
            f"violations={len(verdict.patch_violations)}) — rolled back, re-dispatch surgical"
        ),
    )
    return {"run_id": run_id, "verdict": verdict.model_dump(mode="json")}


__all__ = [
    "CRITIC_LENSES",
    "QUALITY_STEP_ID",
    "PENDING_INDEPENDENT_REVIEW",
    "ADVERSARIAL_LENS",
    "KNOWN_LENSES",
    "CROSS_SLICE_DRIFT_LENS",
    "AI_FAILURE_MODES_LENS",
    "REVIEW_OPTIONAL_LENSES",
    "critic_authorship",
    "review_lenses",
    "QualityError",
    "critique_plan",
    "resolve_lenses",
    "provenance_banner",
    "provenance_report",
    "record_independent_override",
    "required_lenses",
    "patch_plan",
    "quality_gate",
    "quality_status",
    "record_critique",
    "record_patch",
    "record_subjective",
]
