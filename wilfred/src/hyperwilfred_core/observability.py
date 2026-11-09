"""Observability — disk-observing anomaly / alert / dashboard, RETARGETED to code signals.

RECYCLE-TABLE verdict (spec §3): the legacy observability stack (anomaly/alert/dashboard +
retrieval) is **adapt** — "adopt the disk-observing anomaly/alert/dashboard stack (retarget
signals to code: 'patch loop maxed,' 'diff >> spec'); retarget retrieval from client
case-studies to codebase memory." This module is the forked, standalone, code-retargeted version:

* It reads ONLY on-disk run evidence — ``state.json``, ``events.jsonl``, the acceptance
  ``verdicts/``, and the quality-loop history — and computes CODE anomalies:
  ``patch_loop_maxed`` (the quality loop exhausted its iteration budget), ``diff_over_budget``
  (a packet's diff blew past its changed-lines budget), ``high_reject_rate`` /
  ``high_rollback_rate`` (acceptance kept rejecting), ``repeated_gate_fail`` (the deterministic
  gate failed repeatedly), and ``escalation_spike`` (a blocker survived to a human gate).
* It emits a **dashboard** (JSON + a human-readable text view) and routes high-severity anomaly
  **alerts to the escalation sink** (:mod:`hyperwilfred_core.escalation`).
* It maintains a lightweight **codebase-memory index** (a disk index of prior runs' outcomes —
  NOT a vector store) the analyzer and dashboard can reference.

Like the rest of the engine it calls NO LLM and spawns NOTHING — it only reads and writes disk.
All thresholds are explicit module constants (deterministic), never model judgment.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from hyperwilfred_core import escalation
from hyperwilfred_core.run_store import RunEventStatus, RunStore, atomic_write_text
from hyperwilfred_core.schemas import (
    AcceptanceVerdict,
    AnomalyCode,
    AnomalySeverity,
    CodeAnomaly,
    CodebaseMemoryEntry,
    CodebaseMemoryIndex,
    Dashboard,
    EscalationKind,
    RunObservation,
    RunState,
)

# --------------------------------------------------------------------------- thresholds (explicit)

# A rate signal needs a minimum sample to be meaningful (1/1 rejected is not "a high reject rate").
MIN_PACKETS_FOR_RATE = 3
REJECT_RATE_THRESHOLD = 0.5  # >= half the packets rejected by acceptance → anomaly
ROLLBACK_RATE_THRESHOLD = 0.5  # >= half the packets rolled back → anomaly
GATE_FAIL_THRESHOLD = 2  # the deterministic gate failed this many times → anomaly

# The substring that marks a "diff >> spec (changed-lines far over budget)" patch violation —
# emitted verbatim by ``guardrails.patch_violations``. Kept as a constant so the observer and the
# guardrail cannot drift on what "over budget" means.
DIFF_OVER_BUDGET_MARKER = "too-many-changed-lines"

OBSERVABILITY_DIR = "observability"
DASHBOARD_JSON = "dashboard.json"
DASHBOARD_TXT = "dashboard.txt"
ANOMALIES_JSON = "anomalies.json"
MEMORY_JSON = "memory.json"


def _now() -> datetime:
    return datetime.now(UTC)


def _hw_dir(repo_root: Path) -> Path:
    return repo_root / ".hw"


def _obs_dir(repo_root: Path) -> Path:
    return _hw_dir(repo_root) / OBSERVABILITY_DIR


# --------------------------------------------------------------------------- per-run observation


def observe_run(*, repo_root: Path, run_id: str) -> RunObservation:
    """Mine ONE run's on-disk evidence into a :class:`RunObservation` (pure read)."""
    from hyperwilfred_core.engine import RunPaths, load_state

    paths = RunPaths(repo_root, run_id)
    state: RunState = load_state(paths)

    # Acceptance verdicts are the authoritative per-packet record.
    accepted = rejected = rolled_back = diff_over_budget = 0
    if paths.verdicts_dir.exists():
        for vf in sorted(paths.verdicts_dir.glob("*.json")):
            try:
                verdict = AcceptanceVerdict.model_validate_json(vf.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if verdict.accepted:
                accepted += 1
            else:
                rejected += 1
            if verdict.rolled_back:
                rolled_back += 1
            if any(
                DIFF_OVER_BUDGET_MARKER in reason
                for pv in verdict.patch_violations
                for reason in pv.reasons
            ):
                diff_over_budget += 1

    # The timeline carries the deterministic-gate outcomes.
    events = RunStore(paths.run_dir).read_events()
    gate_failures = sum(1 for e in events if e.status is RunEventStatus.gate_failed)
    gate_passes = sum(1 for e in events if e.status is RunEventStatus.gate_passed)

    q = state.quality
    escalations = len(escalation.list_escalations(repo_root=repo_root, run_id=run_id))

    obs = RunObservation(
        run_id=run_id,
        mode=state.mode,
        tier=state.tier,
        status=state.status,
        product_dir=state.product_dir,
        stack=state.objective.stack,
        total_events=len(events),
        accepted_packets=accepted,
        rejected_packets=rejected,
        rolled_back_packets=rolled_back,
        diff_over_budget_packets=diff_over_budget,
        gate_failures=gate_failures,
        gate_passes=gate_passes,
        quality_iterations=(q.iteration if q else 0),
        quality_max_iterations=(q.max_iterations if q else 0),
        quality_passed=(q.passed if q else False),
        quality_used_fallback=(q.used_fallback if q else False),
        quality_escalated=(q.escalated if q else False),
        quality_open_blocker=(q.open_blocker if q else 0),
        escalations=escalations,
        # RunState defaults a missing created_at to "now"; only trust a recorded one.
        created_at=state.created_at if "created_at" in state.model_fields_set else None,
    )
    obs.anomaly_codes = [a.code.value for a in detect_run_anomalies(obs)]
    return obs


def observe_all(*, repo_root: Path) -> list[RunObservation]:
    """Observe every run that has a ``state.json`` on disk (skips meta dirs like proposals)."""
    hw = _hw_dir(repo_root)
    if not hw.exists():
        return []
    out: list[RunObservation] = []
    for state_file in sorted(hw.glob("*/state.json")):
        run_id = state_file.parent.name
        try:
            out.append(observe_run(repo_root=repo_root, run_id=run_id))
        except Exception:  # noqa: BLE001 — a malformed run dir must not break the whole sweep
            continue
    return out


# --------------------------------------------------------------------------- anomaly detection


def detect_run_anomalies(obs: RunObservation) -> list[CodeAnomaly]:
    """Apply the explicit code-signal thresholds to ONE observation. Deterministic, no baseline.

    These are per-run absolute thresholds and per-run rates — they need no cross-run baseline, so
    a single run with a maxed patch loop or an over-budget diff is flagged on its own evidence.
    """
    flags: list[CodeAnomaly] = []
    refs = [f".hw/{obs.run_id}/"]

    # patch_loop_maxed — the quality loop exhausted its iteration budget (fell back to best).
    if obs.quality_used_fallback:
        flags.append(
            CodeAnomaly(
                code=AnomalyCode.PATCH_LOOP_MAXED,
                severity=AnomalySeverity.MEDIUM,
                run_id=obs.run_id,
                metric="quality_iterations",
                actual=float(obs.quality_iterations + 1),
                threshold=float(obs.quality_max_iterations),
                message=(
                    f"quality loop exhausted its {obs.quality_max_iterations}-iteration budget and "
                    "fell back to the best version (subjective threshold never reached)"
                ),
                evidence_refs=refs,
            )
        )

    # escalation_spike — the quality loop escalated an unresolved blocker to a human gate.
    # NOTE: this keys on the GENUINE signal (``quality_escalated``), NOT on the escalation-ledger
    # count — routing an anomaly alert to the sink itself creates a ledger escalation, so counting
    # ledger entries here would make the sink feed itself a fresh anomaly each round. The ledger
    # count stays informational (the dashboard's ``total_escalations``); only a real quality-loop
    # escalation is an anomaly.
    if obs.quality_escalated:
        flags.append(
            CodeAnomaly(
                code=AnomalyCode.ESCALATION_SPIKE,
                severity=AnomalySeverity.HIGH,
                run_id=obs.run_id,
                metric="quality_escalated",
                actual=1.0,
                threshold=0.0,
                message="an open blocker survived the quality budget and escalated to a human gate",
                evidence_refs=refs,
            )
        )

    # diff_over_budget — a packet's diff blew far past its changed-lines budget.
    if obs.diff_over_budget_packets > 0:
        flags.append(
            CodeAnomaly(
                code=AnomalyCode.DIFF_OVER_BUDGET,
                severity=AnomalySeverity.MEDIUM,
                run_id=obs.run_id,
                metric="diff_over_budget_packets",
                actual=float(obs.diff_over_budget_packets),
                threshold=0.0,
                message=(
                    f"{obs.diff_over_budget_packets} packet(s) were rejected for a diff far over "
                    "the slice's changed-lines budget (diff >> spec)"
                ),
                evidence_refs=refs,
            )
        )

    # repeated_gate_fail — the deterministic gate failed repeatedly.
    if obs.gate_failures >= GATE_FAIL_THRESHOLD:
        flags.append(
            CodeAnomaly(
                code=AnomalyCode.REPEATED_GATE_FAIL,
                severity=AnomalySeverity.HIGH,
                run_id=obs.run_id,
                metric="gate_failures",
                actual=float(obs.gate_failures),
                threshold=float(GATE_FAIL_THRESHOLD),
                message=f"the deterministic gate failed {obs.gate_failures} time(s) on this run",
                evidence_refs=refs,
            )
        )

    total_packets = obs.accepted_packets + obs.rejected_packets

    # high_reject_rate — acceptance kept rejecting (needs a minimum sample).
    if total_packets >= MIN_PACKETS_FOR_RATE:
        reject_rate = obs.rejected_packets / total_packets
        if reject_rate >= REJECT_RATE_THRESHOLD:
            flags.append(
                CodeAnomaly(
                    code=AnomalyCode.HIGH_REJECT_RATE,
                    severity=AnomalySeverity.HIGH,
                    run_id=obs.run_id,
                    metric="reject_rate",
                    actual=round(reject_rate, 4),
                    threshold=REJECT_RATE_THRESHOLD,
                    message=(
                        f"{obs.rejected_packets}/{total_packets} packets were rejected by the "
                        f"acceptance gate ({reject_rate:.0%})"
                    ),
                    evidence_refs=refs,
                )
            )
        rollback_rate = obs.rolled_back_packets / total_packets
        if rollback_rate >= ROLLBACK_RATE_THRESHOLD:
            flags.append(
                CodeAnomaly(
                    code=AnomalyCode.HIGH_ROLLBACK_RATE,
                    severity=AnomalySeverity.MEDIUM,
                    run_id=obs.run_id,
                    metric="rollback_rate",
                    actual=round(rollback_rate, 4),
                    threshold=ROLLBACK_RATE_THRESHOLD,
                    message=(
                        f"{obs.rolled_back_packets}/{total_packets} packets were rolled back "
                        f"({rollback_rate:.0%})"
                    ),
                    evidence_refs=refs,
                )
            )

    return flags


def detect_anomalies(*, repo_root: Path) -> list[CodeAnomaly]:
    """Detect anomalies across EVERY run on disk (flattened)."""
    flags: list[CodeAnomaly] = []
    for obs in observe_all(repo_root=repo_root):
        flags.extend(detect_run_anomalies(obs))
    return flags


# --------------------------------------------------------------------------- alert routing


def route_alerts(*, repo_root: Path, anomalies: list[CodeAnomaly]) -> list[dict]:
    """Route HIGH-severity anomalies to the escalation sink, idempotently.

    An anomaly is only escalated once per (run, code): if an escalation already carries that
    ``anomaly_code`` for the run, it is skipped — so repeated ``hw anomalies`` runs do not spam
    the ledger. Only HIGH severity routes to the founder (the alert floor mirrors legacy's
    "ANOMALY_DETECTED → alert only when severity == high").
    """
    routed: list[dict] = []
    for a in anomalies:
        if a.severity is not AnomalySeverity.HIGH:
            continue
        existing = escalation.list_escalations(repo_root=repo_root, run_id=a.run_id)
        if any(e.metadata.get("anomaly_code") == a.code.value for e in existing):
            continue  # already surfaced — idempotent
        esc = escalation.escalate(
            repo_root=repo_root,
            run_id=a.run_id,
            kind=EscalationKind.ANOMALY,
            severity=a.severity,
            reason=a.message,
            source="observability",
            evidence_refs=a.evidence_refs,
            metadata={"anomaly_code": a.code.value, "metric": a.metric},
        )
        routed.append({"escalation_id": esc.escalation_id, "run_id": a.run_id,
                       "code": a.code.value})
    return routed


# --------------------------------------------------------------------------- codebase memory


def build_memory_index(
    *, repo_root: Path, observations: list[RunObservation]
) -> CodebaseMemoryIndex:
    """Build + persist the lightweight codebase-memory index (a disk index of run outcomes)."""
    entries = [
        CodebaseMemoryEntry(
            run_id=o.run_id,
            mode=o.mode,
            status=o.status,
            product_dir=o.product_dir,
            tier=o.tier,
            quality_passed=o.quality_passed,
            anomaly_codes=list(o.anomaly_codes),
        )
        for o in observations
    ]
    index = CodebaseMemoryIndex(generated_at=_now(), entries=entries)
    atomic_write_text(
        _obs_dir(repo_root) / MEMORY_JSON, index.model_dump_json(indent=2) + "\n"
    )
    return index


# --------------------------------------------------------------------------- dashboard


def build_dashboard(*, repo_root: Path) -> Dashboard:
    """Compute the aggregate dashboard, persist JSON + a text view + the memory index."""
    observations = observe_all(repo_root=repo_root)
    anomalies: list[CodeAnomaly] = []
    for obs in observations:
        anomalies.extend(detect_run_anomalies(obs))

    runs_by_status: dict[str, int] = {}
    runs_by_mode: dict[str, int] = {}
    for o in observations:
        runs_by_status[o.status.value] = runs_by_status.get(o.status.value, 0) + 1
        runs_by_mode[o.mode.value] = runs_by_mode.get(o.mode.value, 0) + 1

    dashboard = Dashboard(
        generated_at=_now(),
        total_runs=len(observations),
        runs_by_status=runs_by_status,
        runs_by_mode=runs_by_mode,
        total_accepted_packets=sum(o.accepted_packets for o in observations),
        total_rejected_packets=sum(o.rejected_packets for o in observations),
        total_gate_failures=sum(o.gate_failures for o in observations),
        total_escalations=sum(o.escalations for o in observations),
        anomalies=anomalies,
        observations=observations,
    )

    _obs_dir(repo_root).mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        _obs_dir(repo_root) / DASHBOARD_JSON, dashboard.model_dump_json(indent=2) + "\n"
    )
    atomic_write_text(_obs_dir(repo_root) / DASHBOARD_TXT, render_dashboard_text(dashboard))
    build_memory_index(repo_root=repo_root, observations=observations)
    return dashboard


def render_dashboard_text(dashboard: Dashboard) -> str:
    """Render a human-readable text view of the dashboard (founder-friendly)."""
    lines = [
        "HyperWilfred — Observability Dashboard",
        f"Generated: {dashboard.generated_at.isoformat()}",
        "",
        f"Runs: {dashboard.total_runs}  "
        f"(by status: {dashboard.runs_by_status}; by mode: {dashboard.runs_by_mode})",
        f"Packets accepted: {dashboard.total_accepted_packets}  "
        f"rejected: {dashboard.total_rejected_packets}",
        f"Deterministic gate failures: {dashboard.total_gate_failures}",
        f"Escalations: {dashboard.total_escalations}",
        "",
        f"Anomalies: {len(dashboard.anomalies)}",
    ]
    if not dashboard.anomalies:
        lines.append("  (none)")
    for a in dashboard.anomalies:
        lines.append(
            f"  [{a.severity.value.upper()}] {a.code.value} (run {a.run_id}): {a.message}"
        )
    lines.append("")
    lines.append("Per-run:")
    for o in dashboard.observations:
        codes = ", ".join(o.anomaly_codes) if o.anomaly_codes else "-"
        lines.append(
            f"  {o.run_id} [{o.mode.value}/{o.status.value}] "
            f"accepted={o.accepted_packets} rejected={o.rejected_packets} "
            f"gate_fail={o.gate_failures} q_pass={o.quality_passed} anomalies={codes}"
        )
    return "\n".join(lines) + "\n"


def anomalies_report(*, repo_root: Path, alert: bool = True) -> dict:
    """The ``hw anomalies`` surface: detect, persist, and (when ``alert``) route to the sink."""
    anomalies = detect_anomalies(repo_root=repo_root)
    _obs_dir(repo_root).mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        _obs_dir(repo_root) / ANOMALIES_JSON,
        "[\n" + ",\n".join(a.model_dump_json(indent=2) for a in anomalies) + "\n]\n"
        if anomalies
        else "[]\n",
    )
    routed = route_alerts(repo_root=repo_root, anomalies=anomalies) if alert else []
    return {
        "repo": str(repo_root),
        "anomalies": [a.model_dump(mode="json") for a in anomalies],
        "count": len(anomalies),
        "alerts_routed": routed,
    }


__all__ = [
    "ANOMALIES_JSON",
    "DASHBOARD_JSON",
    "DASHBOARD_TXT",
    "MEMORY_JSON",
    "anomalies_report",
    "build_dashboard",
    "build_memory_index",
    "detect_anomalies",
    "detect_run_anomalies",
    "observe_all",
    "observe_run",
    "render_dashboard_text",
    "route_alerts",
]
