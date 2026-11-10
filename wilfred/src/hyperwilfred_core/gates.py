"""Disk-file HITL gates for irreversible actions — the third enforcement point (spec §2.3).

Before any **irreversible** action — deploy, merge to main, dependency bumps, anything touching
secrets / migrations / payments — the engine writes a ``.pending`` gate file under
``.hw/<run_id>/gates/`` and BLOCKS the run (status ``awaiting_human``) until a human approves by
presenting a short-lived, signed decision bound to the gate's one-time challenge.

THE INVARIANT: **the engine never auto-approves.** It only ever WRITES a ``pending`` gate; a
trusted host must supply a short-lived signed decision bound to the current action and evidence.
Unsigned files and claimed identities are inert. The owner's human-approvals policy switch
(:func:`approvals.active_policy`) never applies to these gates; the ship gate only DISPLAYS the
approvals the policy granted elsewhere in the run, so the signing human sees them.

Like the rest of the engine this module calls no LLM and spawns nothing. It persists verified
approve / reject / modify-scope decisions to the run store; unsigned marker files are inert.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from hyperwilfred_core import approvals, protected_paths
from hyperwilfred_core.path_safety import contained_path, validate_identifier
from hyperwilfred_core.pipeline import steps_for
from hyperwilfred_core.run_store import RunEventStatus, RunStore, atomic_write_text
from hyperwilfred_core.schemas import (
    GateDecision,
    GateKind,
    GateStatus,
    HumanGate,
    RunStatus,
    StepKind,
    StepStatus,
)


class GateError(RuntimeError):
    """Raised on a HITL-gate precondition violation (surfaces as CLI exit 2)."""


# --------------------------------------------------------------------------- layout
#
# Two layers of helpers: a run-agnostic "_in(gates_dir, …)" set that operates on ANY gates
# directory, and the run-scoped wrappers that resolve ``.hw/<run_id>/gates`` and delegate. The
# run-agnostic layer is the single source of truth for the gate-file protocol, so the Phase-5
# proposal review gate (under ``.hw/proposals/gates``) reuses the EXACT same detection logic and
# can never drift from the run-scoped Phase-3 gates.


def gate_json_in(gates_dir: Path, gate_id: str) -> Path:
    validate_identifier(gate_id, label="gate_id")
    return gates_dir / f"{gate_id}.json"


def pending_marker_in(gates_dir: Path, gate_id: str) -> Path:
    validate_identifier(gate_id, label="gate_id")
    return gates_dir / f"{gate_id}.pending"


# Legacy marker paths remain discoverable for migration diagnostics but never authorize anything.
def approved_marker_in(gates_dir: Path, gate_id: str) -> Path:
    validate_identifier(gate_id, label="gate_id")
    return gates_dir / f"{gate_id}.approved"


def rejected_marker_in(gates_dir: Path, gate_id: str) -> Path:
    validate_identifier(gate_id, label="gate_id")
    return gates_dir / f"{gate_id}.rejected"


def decision_file_in(gates_dir: Path, gate_id: str) -> Path:
    validate_identifier(gate_id, label="gate_id")
    return gates_dir / f"{gate_id}.decision.json"


def detect_decision_in_dir(
    gates_dir: Path, gate_id: str, *, expected_run_id: str,
    expected_action: str, expected_evidence: list[str], public_key_path: Path | None = None,
) -> tuple[GateDecision, str, str] | None:
    """Return a verified signed decision; unsigned/bare/self-declared files are inert."""
    decision_file = decision_file_in(gates_dir, gate_id)
    if decision_file.exists():
        try:
            decision, decided_by, note = approvals.verify_and_consume(
                gates_dir, gate_id, decision_file, expected_run_id=expected_run_id,
                public_key_path=public_key_path, expected_action=expected_action,
                expected_evidence=expected_evidence,
            )
            return GateDecision(decision), decided_by, note
        except (approvals.ApprovalError, ValueError, OSError):
            return None
    return None


def _gates_dir(repo_root: Path, run_id: str) -> Path:
    validate_identifier(run_id, label="run_id")
    state_root = contained_path(repo_root.resolve(), ".hw", label="state root")
    return contained_path(state_root, run_id, "gates", label="gate directory")


def _gate_json(repo_root: Path, run_id: str, gate_id: str) -> Path:
    return gate_json_in(_gates_dir(repo_root, run_id), gate_id)


def _pending_marker(repo_root: Path, run_id: str, gate_id: str) -> Path:
    return pending_marker_in(_gates_dir(repo_root, run_id), gate_id)


def _approved_marker(repo_root: Path, run_id: str, gate_id: str) -> Path:
    return approved_marker_in(_gates_dir(repo_root, run_id), gate_id)


def _rejected_marker(repo_root: Path, run_id: str, gate_id: str) -> Path:
    return rejected_marker_in(_gates_dir(repo_root, run_id), gate_id)


def _decision_file(repo_root: Path, run_id: str, gate_id: str) -> Path:
    return decision_file_in(_gates_dir(repo_root, run_id), gate_id)


def _io() -> tuple:
    """Lazy import of the engine's state I/O (mirrors quality.py — avoids a circular import)."""
    from hyperwilfred_core.engine import RunPaths, load_state, save_state

    return RunPaths, load_state, save_state


SHIP_STEP_ID = "ship"


def _require_accepted_head(repo_root: Path, run_id: str, stage: str) -> None:
    """The ship gate only ever covers a checkout whose HEAD is exactly the accepted work."""
    from hyperwilfred_core import engine

    try:
        engine.require_accepted_head(repo_root=repo_root, run_id=run_id, stage=stage)
    except engine.EngineError as exc:
        raise GateError(str(exc)) from exc


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- classification


def classify_paths(paths: list[str]) -> dict:
    """Classify changed *paths* into sensitive categories + recommend a gate kind.

    Pure read-side routing via the forked ``protected_paths`` classifier — it decides whether a
    set of changes needs a human gate and, if so, which :class:`GateKind` to open. No side effects.
    """
    result = protected_paths.ProtectedPathGate().check_paths(paths)
    categories = result.categories
    kind = None
    if categories:
        # Prefer an irreversible category for the gate kind; fall back to the first hit.
        ordered = sorted(
            categories,
            key=lambda c: (c not in protected_paths.IRREVERSIBLE_CATEGORIES, categories.index(c)),
        )
        kind = protected_paths.category_to_gate_kind(ordered[0])
    return {
        "requires_human_review": result.requires_human_review,
        "irreversible": result.irreversible,
        "categories": categories,
        "recommended_kind": kind,
        "matches": [m.model_dump(mode="json") for m in result.matches],
    }


# --------------------------------------------------------------------------- hw gate-open


def gate_open(
    *,
    repo_root: Path,
    run_id: str,
    kind: GateKind | None = None,
    step_id: str | None = None,
    action: str = "",
    paths: list[str] | None = None,
    evidence: list[str] | None = None,
) -> dict:
    """Open a disk-file HITL gate and BLOCK the run until a human approves.

    Writes ``gates/<gate_id>.json`` + a ``gates/<gate_id>.pending`` marker, sets the run status to
    ``awaiting_human``, and records an event. Idempotent per (run, step/kind): re-opening an
    already-pending gate renews only an expired, unconsumed challenge.

    If *kind* is omitted it is derived from *paths* via the protected-paths classifier (so a
    sensitive change routes itself to the right gate) — an error if neither a kind nor a
    classifiable path is given (the engine never guesses an irreversible action silently).
    """
    RunPaths, load_state, save_state = _io()
    paths_obj = RunPaths(repo_root, run_id)
    state = load_state(paths_obj)  # ensures the run exists
    if step_id == SHIP_STEP_ID:
        _require_accepted_head(repo_root, run_id, "ship gate open")

    sensitive_paths = list(paths or [])
    classification = classify_paths(sensitive_paths) if sensitive_paths else None
    categories = classification["categories"] if classification else []

    if kind is None:
        if classification and classification["recommended_kind"]:
            kind = GateKind(classification["recommended_kind"])
        else:
            raise GateError(
                "gate-open needs --kind, or --paths that classify as a protected/irreversible "
                "surface — refusing to open an unclassified gate"
            )

    gate_id = f"{kind.value}-{step_id}" if step_id else f"{kind.value}-adhoc"
    validate_identifier(gate_id, label="gate_id")

    existing = load_gate(repo_root, run_id, gate_id)
    if existing is not None:
        if existing.status is not GateStatus.PENDING:
            raise GateError("gate already decided; use a new gate for a new action")
        try:
            challenge = approvals.issue_challenge(
                _gates_dir(repo_root, run_id), run_id=run_id, gate_id=gate_id,
                action=existing.action, evidence=[*existing.evidence, *existing.paths],
                reuse_pending=True,
            )
        except approvals.ApprovalError as exc:
            raise GateError(f"cannot reopen pending gate: {exc}") from exc
        reopened = {"run_id": run_id, "gate": existing.model_dump(mode="json"),
                    "already_open": True, "challenge": challenge,
                    "approval": _approval_help(repo_root, run_id, gate_id)}
        # Policy approvals granted since the gate was opened must reach the signing human too.
        late = [line for line in _policy_warnings(repo_root, run_id)
                if line not in existing.warnings] if existing.step_id == "ship" else []
        if late:
            existing.warnings = [*existing.warnings, *late]
            _write_gate(repo_root, run_id, existing)
            reopened.update(gate=existing.model_dump(mode="json"), banner=existing.warnings)
        return reopened

    warnings = ([*_quality_warnings(repo_root, run_id, state),
                 *_policy_warnings(repo_root, run_id)] if step_id == "ship" else [])
    gate = HumanGate(
        gate_id=gate_id,
        run_id=run_id,
        kind=kind,
        step_id=step_id,
        action=action or f"{kind.value} (irreversible)",
        paths=sensitive_paths,
        categories=categories,
        evidence=list(evidence or []),
        status=GateStatus.PENDING,
        warnings=warnings,
    )
    challenge = approvals.issue_challenge(
        _gates_dir(repo_root, run_id), run_id=run_id, gate_id=gate_id,
        action=gate.action, evidence=[*gate.evidence, *gate.paths],
    )
    _write_gate(repo_root, run_id, gate)
    # The .pending marker is the cheap "is this gate still blocking?" signal a human/adapter polls.
    atomic_write_text(
        _pending_marker(repo_root, run_id, gate_id),
        f"pending human approval for gate {gate_id} ({kind.value})\n",
    )

    if state.status not in (RunStatus.FAILED, RunStatus.GATE_FAILED):
        state.status = RunStatus.AWAITING_HUMAN
    if step_id and state.step_status.get(step_id) is StepStatus.DONE:
        state.step_status[step_id] = StepStatus.PENDING
    save_state(paths_obj, state)
    RunStore(paths_obj.run_dir).append_event(
        run_id=run_id,
        stage=step_id or "gate",
        status=RunEventStatus.awaiting_human,
        message=f"HITL gate OPEN ({gate_id}): {gate.action} — BLOCKED until a human approves",
        metadata={"gate_id": gate_id, "kind": kind.value},
    )
    return {"run_id": run_id, "banner": warnings, "gate": gate.model_dump(mode="json"),
            "already_open": False, "challenge": challenge,
            "approval": _approval_help(repo_root, run_id, gate_id)}


def _quality_warnings(repo_root: Path, run_id: str, state) -> list[str]:
    """Prominent ship-gate notices: non-independent review status and any human override."""
    from hyperwilfred_core import quality

    if state.quality is None:
        return []
    prov = quality.provenance_report(repo_root=repo_root, run_id=run_id, state=state)
    return quality.provenance_banner(prov)


def _policy_warnings(repo_root: Path, run_id: str) -> list[str]:
    """Ship-gate notices: every approval this run received from the policy switch, unsigned."""
    run_dir = _gates_dir(repo_root, run_id).parent
    lines: list[str] = []
    for directory in (run_dir / "gates", run_dir / "test-evolution"):
        for token in sorted(directory.glob("*.decision.json")):
            if approvals.policy_provenance(token) is None:
                continue
            gate_id = token.name.removesuffix(".decision.json")
            try:
                action = approvals.load_challenge(directory, gate_id)["action"]
            except (approvals.ApprovalError, KeyError):
                action = "unknown action"
            lines.append(f"POLICY AUTO-APPROVAL (no human signature): {action} [{gate_id}]")
    return lines


def _approval_help(repo_root: Path, run_id: str, gate_id: str) -> dict:
    """Instructions for the trusted host signer; the worker cannot mint this evidence."""
    return {
        "challenge_file": str(approvals.challenge_file(_gates_dir(repo_root, run_id), gate_id)),
        "decision_file": str(_decision_file(repo_root, run_id, gate_id)),
        "instruction": "sign the challenge externally, then submit the signed token",
    }


# --------------------------------------------------------------------------- hw gate-approve


def gate_approve(
    *,
    repo_root: Path,
    run_id: str,
    gate_id: str,
    decision: GateDecision,
    decided_by: str = "human",
    note: str = "",
    token_file: str | Path | None = None,
    public_key_path: Path | None = None,
) -> dict:
    """Submit an externally signed token. CLI labels never constitute authorization."""
    RunPaths, load_state, _save = _io()
    load_state(RunPaths(repo_root, run_id))  # ensures the run exists
    gate = load_gate(repo_root, run_id, gate_id)
    if gate is None:
        raise GateError(f"no gate {gate_id!r} for run {run_id!r} — run `hw gate-open` first")
    if gate.status is not GateStatus.PENDING:
        raise GateError("gate already decided; its recorded decision cannot be replaced")
    if token_file is None:
        raise GateError(
            "gate-approve cannot mint authorization from --by/--decision; provide --token-file "
            "from the external trusted signer"
        )
    source = Path(token_file)
    try:
        signed_decision, _signed_by, _signed_note = approvals.verify_and_consume(
            _gates_dir(repo_root, run_id),
            gate_id,
            source,
            expected_run_id=run_id,
            public_key_path=public_key_path,
            expected_action=gate.action,
            expected_evidence=[*gate.evidence, *gate.paths],
            consume=False,
        )
        token = source.read_text(encoding="utf-8")
    except approvals.ApprovalError as exc:
        raise GateError(f"signed approval token was rejected: {exc}") from exc
    except OSError as exc:
        raise GateError(f"cannot read signed approval token: {exc}") from exc
    if signed_decision != decision.value:
        raise GateError(
            f"requested decision {decision.value!r} does not match signed token decision "
            f"{signed_decision!r}; token was not consumed"
        )
    if gate.step_id == SHIP_STEP_ID and signed_decision == GateDecision.APPROVE.value:
        # Checked before the token is written or consumed: once HEAD holds only accepted work
        # again, the same signed approval can be submitted.
        _require_accepted_head(repo_root, run_id, "ship approval")
    atomic_write_text(_decision_file(repo_root, run_id, gate_id), token)
    result = gate_status(
        repo_root=repo_root, run_id=run_id, gate_id=gate_id, public_key_path=public_key_path
    )
    if gate_id in result.get("refused", {}):
        raise GateError(result["refused"][gate_id])
    if result["gates"][0]["decision"] != decision.value:
        raise GateError("signed approval token was rejected")
    return result


# --------------------------------------------------------------------------- hw gate-status


def run_blocked(repo_root: Path, run_id: str, state) -> bool:
    """All persisted human holds apply even when a caller selects only one gate."""
    return (
        state.status in (RunStatus.FAILED, RunStatus.GATE_FAILED)
        or bool(state.quality and state.quality.escalated and not state.quality.passed)
        or any(g.status is not GateStatus.APPROVED for g in _all_gates(repo_root, run_id))
    )


def gate_status(*, repo_root: Path, run_id: str, gate_id: str | None = None,
                public_key_path: Path | None = None) -> dict:
    """Report gates, resolving pending ones only from valid signed decision tokens.

    Bare markers and unsigned JSON never unblock a gate. Signed approval can complete only
    declared human-gate steps; all other workflow owners and outstanding holds still apply.
    """
    RunPaths, load_state, save_state = _io()
    paths_obj = RunPaths(repo_root, run_id)
    state = load_state(paths_obj)
    store = RunStore(paths_obj.run_dir)

    gates = _all_gates(repo_root, run_id)
    if gate_id is not None:
        gates = [g for g in gates if g.gate_id == gate_id]
        if not gates:
            raise GateError(f"no gate {gate_id!r} for run {run_id!r}")

    resolved: list[dict] = []
    refused: dict[str, str] = {}
    for gate in gates:
        if gate.status is GateStatus.PENDING:
            refusal = _ship_approval_refusal(repo_root, run_id, gate, public_key_path)
            if refusal is not None:
                # Left pending and the token left unconsumed, so the same signed approval
                # applies once HEAD holds only accepted work again.
                refused[gate.gate_id] = refusal
                resolved.append(gate.model_dump(mode="json"))
                continue
            decision = _detect_human_decision(
                repo_root, run_id, gate, public_key_path=public_key_path
            )
            if decision is not None:
                _apply_decision(
                    repo_root, run_id, gate, decision, state, store, save_state, paths_obj
                )
        resolved.append(gate.model_dump(mode="json"))

    pending = [g.gate_id for g in _all_gates(repo_root, run_id) if g.status is GateStatus.PENDING]
    return {
        "run_id": run_id,
        "gates": resolved,
        "pending": pending,
        "blocked": run_blocked(repo_root, run_id, state),
        "run_status": state.status.value,
        **({"refused": refused} if refused else {}),
    }


def _ship_approval_refusal(
    repo_root: Path, run_id: str, gate: HumanGate, public_key_path: Path | None,
) -> str | None:
    """Why a signed ship APPROVAL waiting for this gate cannot apply yet, without consuming it.

    A commit added after the gate opened must not ride on the approval. The token is only
    read (``consume=False``) here; a reject or modify-scope decision is never held back.
    """
    if gate.step_id != SHIP_STEP_ID:
        return None
    gates_dir = _gates_dir(repo_root, run_id)
    decision_file = decision_file_in(gates_dir, gate.gate_id)
    if not decision_file.exists():
        return None
    try:
        decision, _by, _note = approvals.verify_and_consume(
            gates_dir, gate.gate_id, decision_file, expected_run_id=run_id,
            public_key_path=public_key_path, expected_action=gate.action,
            expected_evidence=[*gate.evidence, *gate.paths], consume=False,
        )
    except (approvals.ApprovalError, ValueError, OSError):
        return None  # an invalid token is handled (and stays inert) by the normal path
    if decision != GateDecision.APPROVE.value:
        return None
    try:
        _require_accepted_head(repo_root, run_id, "ship approval")
    except GateError as exc:
        return str(exc)
    return None


def _detect_human_decision(
    repo_root: Path, run_id: str, gate: HumanGate, *, public_key_path: Path | None = None,
) -> tuple[GateDecision, str, str] | None:
    """Return ``(decision, decided_by, note)`` for a valid signed token, else ``None``.

    Thin run-scoped wrapper over :func:`detect_decision_in_dir` (the single source of truth).
    """
    return detect_decision_in_dir(
        _gates_dir(repo_root, run_id), gate.gate_id, expected_run_id=run_id,
        expected_action=gate.action, expected_evidence=[*gate.evidence, *gate.paths],
        public_key_path=public_key_path,
    )


def execute_protected_action(
    *, repo_root: Path, run_id: str, gate_id: str, callback,
    public_key_path: Path | None = None,
):
    """Run a statically selected trusted-host callback after verifying the current gate.

    This is a host-boundary API: callers must map a known gate/action to a fixed callback in
    trusted code. Never deserialize a callback, callable name, or replacement action from worker
    output. The generated worker is kept outside this process by the runtime sandbox.
    """
    status = gate_status(
        repo_root=repo_root, run_id=run_id, gate_id=gate_id,
        public_key_path=public_key_path,
    )
    gate = load_gate(repo_root, run_id, gate_id)
    if status["blocked"] or gate is None or gate.status is not GateStatus.APPROVED:
        raise GateError(f"protected action {gate_id!r} lacks valid signed authorization")
    gates_dir = _gates_dir(repo_root, run_id)
    token_file = _decision_file(repo_root, run_id, gate_id)
    try:
        decision, _identity, _note = approvals.verify_and_consume(
            gates_dir, gate_id, token_file, expected_run_id=run_id,
            public_key_path=public_key_path, expected_action=gate.action,
            expected_evidence=[*gate.evidence, *gate.paths], consume=False,
        )
        approvals.verify_consumed_receipt(gates_dir, gate_id, token_file)
    except approvals.ApprovalError as exc:
        raise GateError(f"protected action {gate_id!r} authorization is stale: {exc}") from exc
    if decision != GateDecision.APPROVE.value:
        raise GateError(f"protected action {gate_id!r} was not approved")
    executed = _gates_dir(repo_root, run_id) / f"{gate_id}.executed"
    try:
        descriptor = os.open(executed, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise GateError(f"protected action {gate_id!r} was already executed") from exc
    try:
        os.write(descriptor, f"attempt started at {_now().isoformat()}\n".encode())
        os.fsync(descriptor)
        result = callback()
        os.write(descriptor, f"completed at {_now().isoformat()}\n".encode())
        os.fsync(descriptor)
        os.close(descriptor)
        return result
    except BaseException as exc:
        try:
            os.write(descriptor, f"outcome uncertain at {_now().isoformat()}\n".encode())
            os.fsync(descriptor)
            os.close(descriptor)
        except OSError:
            pass
        raise GateError(
            f"protected action {gate_id!r} failed with an uncertain outcome; the attempt marker "
            "was preserved to prevent duplicate effects. Investigate manually and use a new "
            "gate and authorization before any retry"
        ) from exc


def _apply_decision(
    repo_root: Path,
    run_id: str,
    gate: HumanGate,
    decision: tuple[GateDecision, str, str],
    state,
    store: RunStore,
    save_state,
    paths_obj,
) -> None:
    """Flip a pending gate per the human's decision and advance/halt the attached step."""
    verdict, decided_by, note = decision
    gate.decision = verdict
    gate.decided_by = decided_by
    gate.note = note
    gate.decided_at = _now()

    if verdict is GateDecision.APPROVE:
        gate.status = GateStatus.APPROVED
    elif verdict is GateDecision.REJECT:
        gate.status = GateStatus.REJECTED
    else:
        gate.status = GateStatus.MODIFIED
    _write_gate(repo_root, run_id, gate)
    # Clear the pending marker — the gate is decided.
    pending = _pending_marker(repo_root, run_id, gate.gate_id)
    if pending.exists():
        pending.unlink()

    if verdict is GateDecision.APPROVE:
        if (gate.step_id and gate.step_id in state.step_status
                and all(any(s.id == gate.step_id and s.kind is StepKind.HUMAN_GATE
                            for s in steps) for steps in (steps_for(state.mode), state.steps))
                and state.step_status[gate.step_id] is not StepStatus.FAILED
                and not any(g.step_id == gate.step_id and g.status is not GateStatus.APPROVED
                            for g in _all_gates(repo_root, run_id))):
            state.step_status[gate.step_id] = StepStatus.DONE
        if state.status is RunStatus.AWAITING_HUMAN and not run_blocked(repo_root, run_id, state):
            state.status = RunStatus.IN_PROGRESS
        save_state(paths_obj, state)
        store.append_event(
            run_id=run_id, stage=gate.step_id or "gate", status=RunEventStatus.accepted,
            message=f"HITL gate {gate.gate_id} APPROVED by {decided_by}; other holds still apply",
            metadata={"gate_id": gate.gate_id, "decision": verdict.value},
        )
    elif verdict is GateDecision.REJECT:
        if gate.step_id and gate.step_id in state.step_status:
            state.step_status[gate.step_id] = StepStatus.FAILED
        state.status = RunStatus.FAILED
        save_state(paths_obj, state)
        store.append_event(
            run_id=run_id, stage=gate.step_id or "gate", status=RunEventStatus.rejected,
            message=f"HITL gate {gate.gate_id} REJECTED by {decided_by}: {note} — action halted",
            metadata={"gate_id": gate.gate_id, "decision": verdict.value},
        )
    else:  # MODIFY_SCOPE — stays blocked; a human re-scopes and re-opens.
        save_state(paths_obj, state)  # status remains awaiting_human
        store.append_event(
            run_id=run_id, stage=gate.step_id or "gate", status=RunEventStatus.awaiting_human,
            message=f"HITL gate {gate.gate_id} MODIFY-SCOPE by {decided_by}: {note} — blocked",
            metadata={"gate_id": gate.gate_id, "decision": verdict.value},
        )


# --------------------------------------------------------------------------- persistence


def _write_gate(repo_root: Path, run_id: str, gate: HumanGate) -> None:
    atomic_write_text(
        _gate_json(repo_root, run_id, gate.gate_id), gate.model_dump_json(indent=2) + "\n"
    )


def load_gate(repo_root: Path, run_id: str, gate_id: str) -> HumanGate | None:
    path = _gate_json(repo_root, run_id, gate_id)
    if not path.exists():
        return None
    try:
        return HumanGate.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _all_gates(repo_root: Path, run_id: str) -> list[HumanGate]:
    gdir = _gates_dir(repo_root, run_id)
    if not gdir.exists():
        return []
    gates: list[HumanGate] = []
    for f in sorted(gdir.glob("*.json")):
        if f.name.endswith((".decision.json", ".challenge.json")):
            continue
        try:
            gates.append(HumanGate.model_validate_json(f.read_text(encoding="utf-8")))
        except (ValueError, OSError) as exc:
            raise GateError(f"corrupt gate record {f.name!r}; repair gate state") from exc
    orphans = {p.stem for p in gdir.glob("*.pending")}
    for f in gdir.glob("*.challenge.json"):
        try:
            challenge = approvals.load_challenge(gdir, f.name.removesuffix(".challenge.json"))
        except approvals.ApprovalError as exc:
            raise GateError(f"corrupt gate challenge {f.name!r}; repair gate state") from exc
        if challenge.get("purpose") not in {"quality-review-override", "verify-finding-override"}:
            orphans.add(f.name.removesuffix(".challenge.json"))
    orphans.difference_update(g.gate_id for g in gates)
    if orphans:
        raise GateError(f"orphaned gate records {sorted(orphans)}; repair gate state")
    return gates


__all__ = [
    "GateError",
    "approved_marker_in",
    "classify_paths",
    "decision_file_in",
    "detect_decision_in_dir",
    "execute_protected_action",
    "gate_approve",
    "gate_json_in",
    "gate_open",
    "gate_status",
    "load_gate",
    "pending_marker_in",
    "rejected_marker_in",
]
