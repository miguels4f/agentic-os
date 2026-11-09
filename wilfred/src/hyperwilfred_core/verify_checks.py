"""The deterministic verify-step checks.

``hw complete-step --step verify`` runs these and REFUSES to complete the step while any is
blocking:

* requirement traceability (:mod:`traceability`) — a MISSING requirement/part ID blocks;
* reproducibility of declared generated artifacts (:mod:`reproducibility`) — drift blocks;
* the live-demo check (:mod:`live_demo`) — a gap is recorded as a FINDING and blocks unless a
  human records an override (who/why), which is then shown in the output;
* the review-provenance summary (informational here; the ship gate displays it prominently).

Everything is code: no model judgement. The result is persisted to
``.hw/<run_id>/verify-checks.json`` so the ship gate and the human can read it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from hyperwilfred_core import approvals, live_demo, quality, reproducibility, traceability
from hyperwilfred_core.run_store import atomic_write_text
from hyperwilfred_core.schemas import Plan

VERIFY_CHECKS_FILE = "verify-checks.json"
VERIFY_OVERRIDES_FILE = "verify-overrides.json"
OVERRIDABLE_CHECKS = frozenset({"live_demo"})  # traceability/reproducibility have no override


class VerifyCheckError(RuntimeError):
    """Raised on a verify-override precondition violation (surfaces as CLI exit 2)."""


def _load_overrides(run_dir: Path) -> list[dict]:
    path = run_dir / VERIFY_OVERRIDES_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _override_evidence(check: str, by: str, reason: str, finding: dict) -> list[str]:
    return [
        f"check:{check}",
        f"claimed_by:{by}",
        f"reason:{reason}",
        f"finding_sha256:{approvals.digest(finding)}",
    ]


def record_override(*, repo_root: Path, run_id: str, check: str, by: str, reason: str) -> dict:
    """Record an explicit human acceptance of a verify finding (who/when/why)."""
    from hyperwilfred_core.engine import RunPaths, load_state

    if check not in OVERRIDABLE_CHECKS:
        raise VerifyCheckError(
            f"check {check!r} cannot be overridden (overridable: {sorted(OVERRIDABLE_CHECKS)}); "
            "fix the product, or declare the requirement out of scope"
        )
    if not by.strip() or not reason.strip():
        raise VerifyCheckError("an override needs both --by (who) and --reason (why)")
    paths = RunPaths(repo_root, run_id)
    load_state(paths)
    try:
        report = json.loads((paths.run_dir / VERIFY_CHECKS_FILE).read_text(encoding="utf-8"))
        finding = next(item for item in report["findings"] if item.get("check") == check)
    except (OSError, ValueError, KeyError, StopIteration, TypeError) as exc:
        raise VerifyCheckError(f"no current {check!r} finding exists to override") from exc
    finding = {key: value for key, value in finding.items() if key != "override"}
    gates_dir = paths.run_dir / "gates"
    action = f"accept verify finding {check}"
    evidence = _override_evidence(check, by.strip(), reason.strip(), finding)
    gate_id = approvals.evidence_gate_id(gates_dir, f"verify-override-{check}", evidence)
    challenge_path = approvals.challenge_file(gates_dir, gate_id)
    token_path = gates_dir / f"{gate_id}.decision.json"
    try:
        approvals.issue_challenge(
            gates_dir, run_id=run_id, gate_id=gate_id, action=action, evidence=evidence,
            purpose="verify-finding-override", reuse_pending=True,
        )
    except approvals.ApprovalError as exc:
        raise VerifyCheckError(f"verify override is not authorized: {exc}") from exc
    # With the owner's policy switch on, this approval point needs no human signature.
    approvals.policy_approve(gates_dir, gate_id, eligible=True)
    if not token_path.exists():
        raise VerifyCheckError(
            f"verify override not authorized: sign {challenge_path}, place the signed token at "
            f"{token_path}, then re-run"
        )
    try:
        decision, signed_by, signed_note = approvals.verify_and_consume(
            gates_dir, gate_id, token_path, expected_run_id=run_id,
            expected_action=action, expected_evidence=evidence, policy_eligible=True,
        )
    except approvals.ApprovalError as exc:
        raise VerifyCheckError(f"verify override is not authorized: {exc}") from exc
    if decision != "approve":
        raise VerifyCheckError(f"verify override was {decision}, not approved")
    entry = {
        "check": check, "by": signed_by, "reason": signed_note or reason.strip(),
        "claimed_by": by.strip(), "claimed_reason": reason.strip(),
        "evidence": evidence,
        "gate_id": gate_id,
        "at": datetime.now(UTC).isoformat(),
        # Present only for an unsigned policy decision: approved_by / human_signature / sha.
        **(approvals.policy_provenance(token_path) or {}),
    }
    overrides = [*_load_overrides(paths.run_dir), entry]
    atomic_write_text(
        paths.run_dir / VERIFY_OVERRIDES_FILE, json.dumps(overrides, indent=2) + "\n"
    )
    return {"run_id": run_id, "override": entry}


def run(*, repo_root: Path, run_id: str, ref: str = "HEAD") -> dict:
    """Run every verify-step check; return a JSON-able report with a ``blocking`` list."""
    from hyperwilfred_core.engine import RunPaths, load_state

    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)
    objective = state.objective
    blocking: list[str] = []
    findings: list[dict] = []
    overrides = _load_overrides(paths.run_dir)

    trace = traceability.check(repo_root=repo_root, run_id=run_id)
    if not trace.ok:
        blocking.append(f"requirement traceability: {trace.message}")

    artifacts = list(objective.generated_artifacts)
    if paths.plan.exists():
        try:
            plan_obj = Plan.model_validate_json(paths.plan.read_text(encoding="utf-8"))
            seen = {(a.path, a.command) for a in artifacts}
            for s in plan_obj.slices:  # slice-level declarations must be provable too
                for a in s.generated_artifacts:
                    if (a.path, a.command) not in seen:
                        artifacts.append(a)
                        seen.add((a.path, a.command))
        except ValueError:
            blocking.append("plan.json is unreadable: slice generated artifacts cannot be checked")
    repro = reproducibility.check(repo_root=repo_root, artifacts=artifacts, ref=ref)
    if not repro.ok:
        blocking.append(f"generated artifacts are not reproducible: {repro.message}")

    demo_result = None
    if objective.live_demo is not None:
        demo_result = live_demo.check(repo_root=repo_root, demo=objective.live_demo)
        if not demo_result.achievable:
            finding = {
                "severity": "major", "check": "live_demo", "title": "Live-demo gap",
                "description": demo_result.finding, "output_excerpt": demo_result.output_excerpt,
            }
            overridden = next(
                (o for o in reversed(overrides) if o.get("check") == "live_demo"), None
            )
            if overridden is not None:
                gates_dir = paths.run_dir / "gates"
                gate_id = overridden.get("gate_id", "verify-override-live_demo")
                try:
                    _decision, verified_by, _note = approvals.verify_and_consume(
                        gates_dir, gate_id, gates_dir / f"{gate_id}.decision.json",
                        expected_run_id=run_id, expected_action="accept verify finding live_demo",
                        expected_evidence=_override_evidence(
                            "live_demo", overridden["claimed_by"],
                            overridden["claimed_reason"], finding,
                        ), consume=False, recorded=True, policy_eligible=True,
                    )
                    if verified_by == approvals.POLICY_APPROVER:
                        # The label comes from the verified decision, never from the editable
                        # overrides file: a policy approval cannot be shown as a human one.
                        overridden = {
                            **overridden, "by": approvals.POLICY_APPROVER,
                            **(approvals.policy_provenance(
                                gates_dir / f"{gate_id}.decision.json") or {}),
                        }
                except (approvals.ApprovalError, KeyError, TypeError):
                    overridden = None
            findings.append({**finding, "override": overridden})
            if overridden is None:
                blocking.append(demo_result.finding)

    provenance_banner: list[str] = []
    if state.quality is not None:
        prov = quality.provenance_report(repo_root=repo_root, run_id=run_id, state=state)
        provenance_banner = quality.provenance_banner(prov)

    report = {
        "run_id": run_id,
        "ok": not blocking,
        "blocking": blocking,
        "findings": findings,
        "traceability": trace.model_dump(mode="json"),
        "reproducibility": repro.model_dump(mode="json"),
        "live_demo": demo_result.model_dump(mode="json") if demo_result else None,
        "overrides": overrides,
        "quality_provenance_banner": provenance_banner,
    }
    atomic_write_text(
        paths.run_dir / VERIFY_CHECKS_FILE, json.dumps(report, indent=2) + "\n"
    )
    return report


__all__ = [
    "OVERRIDABLE_CHECKS",
    "VERIFY_CHECKS_FILE",
    "VERIFY_OVERRIDES_FILE",
    "VerifyCheckError",
    "record_override",
    "run",
]
