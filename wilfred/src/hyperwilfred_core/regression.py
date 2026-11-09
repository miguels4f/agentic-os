"""Regression baseline + comparison for the IMPROVE regression guard.

The plain deterministic gate catches a pre-existing test that now *fails*. But a change can
neutralise the very test that protects it — delete it, skip/xfail it, rename it out of
discovery, gut its assertion, or skip it while adding trivial passing tests to keep the count
up — and the suite goes "green" again. A "prove existing tests still pass" gate must catch all
of those, not just an outright failure.

This module closes the gap with TWO per-test signals snapshotted at comprehend (suite green):

* **passing node-ids.** The gate FAILS unless every test that PASSED at baseline still PASSES
  now — a *per-id subset* check, not an aggregate count (so deleting/skipping/xfailing/renaming
  a baseline test is caught, and it cannot be masked by adding new trivial passing tests).
* **test-file integrity.** The gate FAILS if any pre-existing test file was removed or modified
  — a change cannot gut or weaken a baseline test's assertion while keeping its node-id green,
  because editing the file that holds the regression witness is itself the red flag.

Pure stdlib + the same ``pytest`` subprocess discipline as the evaluators (no library coupling).
Scoped to the python stack (pytest); the node stack's delete/skip hardening is future work and
is documented as such — the node test gate still blocks an outright failure.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from hyperwilfred_core import approvals
from hyperwilfred_core.evaluators.base import run_subprocess
from hyperwilfred_core.evaluators.code.pytest_evaluator import _failing_findings
from hyperwilfred_core.evaluators.verification import CheckKind, CheckResult, Severity
from hyperwilfred_core.path_safety import validate_relative_path
from hyperwilfred_core.run_store import atomic_write_text

BASELINE_FILE = "regression_baseline.json"
_PYTEST_TIMEOUT_S = 120.0
_PASSED_LINE = re.compile(r"^PASSED\s+(.+?)\s*$")
_TEST_GLOBS = ("test_*.py", "*_test.py")
_MAX_SHOWN = 5
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RegressionError(RuntimeError):
    """The regression witness could not be established safely."""


def _pytest_passing(target: Path) -> tuple[set[str] | None, int | None, list]:
    """Return passing ids, exit code, and reconciled failure findings from one pytest run."""

    out = run_subprocess(
        [sys.executable, "-m", "pytest", "-rA", "-q", "--no-header", str(target)],
        timeout_s=_PYTEST_TIMEOUT_S,
    )
    if not out.ran:
        return None, None, []
    ids: set[str] = set()
    for line in out.stdout.splitlines():
        m = _PASSED_LINE.match(line.strip())
        if m:
            ids.add(m.group(1))
    findings = _failing_findings(out.stdout) if out.returncode == 1 else []
    return ids, out.returncode, findings


def _passing_ids(target: Path) -> set[str] | None:
    """Passing node ids for comparison; failures are represented by missing baseline ids."""
    return _pytest_passing(target)[0]


def _test_file_hashes(target: Path) -> dict[str, str]:
    """``{relative_path: sha256}`` for every test file under *target* (test_*.py / *_test.py)."""
    target = Path(target)
    hashes: dict[str, str] = {}
    for pattern in _TEST_GLOBS:
        for f in sorted(target.rglob(pattern)):
            if f.is_file():
                rel = str(f.relative_to(target))
                hashes[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
    return hashes


def baseline_candidate(target: Path) -> tuple[dict, CheckResult]:
    """Observe one baseline run; only the engine may contextualize its rc=1 result."""
    passing, returncode, findings = _pytest_passing(target)
    if passing is None:
        raise RegressionError("cannot capture regression baseline: pytest unavailable or timed out")
    if returncode not in {0, 1}:
        raise RegressionError(
            f"cannot capture regression baseline: pytest did not pass cleanly (exit {returncode})"
        )
    if not passing:
        raise RegressionError("cannot capture regression baseline: no passing tests were observed")
    hashes = _test_file_hashes(target)
    if not hashes:
        raise RegressionError("cannot capture regression baseline: no test files were found")
    baseline = {
        "target": str(target),
        "passing_ids": sorted(passing),
        "passing": len(passing),
        "test_file_hashes": hashes,
    }
    check = CheckResult(
        kind=CheckKind.TEST,
        name="pytest",
        passed=returncode == 0,
        severity=Severity.INFO if returncode == 0 else Severity.ERROR,
        details=f"pytest baseline capture exited {returncode}",
        findings=findings,
    )
    return baseline, check


def capture_baseline(target: Path) -> dict:
    """Snapshot a wholly green baseline; nonzero pytest always fails closed."""
    baseline, check = baseline_candidate(target)
    if not check.passed:
        raise RegressionError(
            "cannot capture regression baseline: pytest did not pass cleanly (exit 1)"
        )
    return baseline


def load_baseline(baseline_path: Path) -> dict | None:
    if not baseline_path.exists():
        return None
    try:
        return json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _fmt(items: list[str]) -> str:
    shown = ", ".join(items[:_MAX_SHOWN])
    return shown + (" …" if len(items) > _MAX_SHOWN else "")


def missing_baseline_check() -> CheckResult:
    return CheckResult(
        kind=CheckKind.TEST,
        name="regression-baseline",
        passed=False,
        severity=Severity.ERROR,
        details="regression baseline is missing or unreadable; recapture it from a pristine tree",
    )


def _review_values(record: dict[str, Any]) -> tuple[str, list[str]]:
    path = validate_relative_path(str(record.get("path", "")), label="reviewed test path")
    values = [
        path,
        str(record.get("before_sha256", "")),
        str(record.get("after_sha256", "")),
        str(record.get("dispatch_sha256", "")),
        str(record.get("report_sha256", "")),
    ]
    if any(not _SHA256.fullmatch(value) for value in values[1:]):
        raise RegressionError("semantic review must bind four complete SHA-256 digests")
    return f"approve semantic test evolution {path}", values


def create_semantic_review_challenge(
    run_dir: Path, *, run_id: str, path: str, before_sha256: str, after_sha256: str,
    dispatch_sha256: str, report_sha256: str,
) -> dict[str, Any]:
    """Create an exact test-transition challenge for a separately dispatched reviewer."""
    record = {
        "schema_version": "wilfred-test-evolution-review/v1", "path": path,
        "before_sha256": before_sha256, "after_sha256": after_sha256,
        "dispatch_sha256": dispatch_sha256, "report_sha256": report_sha256,
    }
    action, evidence = _review_values(record)
    review_id = "test-" + hashlib.sha256(path.encode()).hexdigest()[:20]
    record["review_id"] = review_id
    review_dir = run_dir / "test-evolution"
    review_dir.mkdir(parents=True, exist_ok=True)
    challenge = approvals.issue_challenge(
        review_dir, run_id=run_id, gate_id=review_id, action=action, evidence=evidence,
        purpose="test-evolution",
    )
    atomic_write_text(
        review_dir / f"{review_id}.review.json", json.dumps(record, indent=2) + "\n"
    )
    return {"record": record, "challenge": challenge,
            "decision_file": str(review_dir / f"{review_id}.decision.json")}


def verified_semantic_transitions(
    run_dir: Path, *, run_id: str, target: Path, baseline: dict,
    public_key_path: Path | None = None,
) -> dict[str, tuple[str, str]]:
    """Load only independently signed reviews matching the exact old/new test bytes."""
    review_dir = run_dir / "test-evolution"
    attestation_key = public_key_path or approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH
    if not review_dir.exists():
        return {}
    current = _test_file_hashes(target)
    old = baseline.get("test_file_hashes", {})
    approved: dict[str, tuple[str, str]] = {}
    for path in sorted(review_dir.glob("*.review.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            action, evidence = _review_values(record)
            review_id = str(record["review_id"])
            test_path = evidence[0]
            if old.get(test_path) != evidence[1] or current.get(test_path) != evidence[2]:
                continue
            # With the owner's policy switch on, the recorded review needs no signature.
            approvals.policy_approve(review_dir, review_id, eligible=True)
            decision, _reviewer, _note = approvals.verify_and_consume(
                review_dir, review_id, review_dir / f"{review_id}.decision.json",
                expected_run_id=run_id, public_key_path=attestation_key,
                expected_action=action, expected_evidence=evidence, consume=False,
                policy_eligible=True,
            )
            if decision == "approve":
                approved[test_path] = (evidence[1], evidence[2])
        except (approvals.ApprovalError, KeyError, OSError, ValueError, json.JSONDecodeError):
            continue
    return approved


def policy_approved_paths(run_dir: Path) -> set[str]:
    """Test paths whose recorded semantic review was approved by policy, with no signature."""
    review_dir = run_dir / "test-evolution"
    paths: set[str] = set()
    for record_file in sorted(review_dir.glob("*.review.json")):
        try:
            record = json.loads(record_file.read_text(encoding="utf-8"))
            token = review_dir / f"{record['review_id']}.decision.json"
            if approvals.policy_provenance(token) is not None:
                paths.add(str(record["path"]))
        except (KeyError, OSError, TypeError, ValueError):
            continue
    return paths


def compare(
    target: Path,
    baseline: dict,
    *,
    approved_transitions: dict[str, tuple[str, str]] | None = None,
    policy_approved: set[str] | None = None,
) -> CheckResult:
    """Compare the current suite against *baseline*; a regression is an ERROR CheckResult.

    Two per-test signals, either of which fails the gate:
      * a baseline-passing test no longer passes (failed / skipped / xfailed / removed / renamed),
        checked per node-id so new trivial passing tests cannot inflate the count to mask it; and
      * a pre-existing test file was removed or modified (its regression-witness integrity can no
        longer be assumed — a gutted/weakened assertion hides behind a still-green node-id).
    """
    reasons: list[str] = []

    baseline_passing = set(baseline.get("passing_ids", []))
    baseline_hashes: dict[str, str] = baseline.get("test_file_hashes", {})
    if not baseline_passing or not baseline_hashes:
        reasons.append("baseline has no immutable passing-test witness")
    passing_now = _passing_ids(target)
    if passing_now is None:
        reasons.append("current passing tests undeterminable (pytest unavailable)")
    else:
        lost = sorted(baseline_passing - passing_now)
        if lost:
            reasons.append(
                f"{len(lost)} baseline test(s) no longer passing "
                f"(failed/skipped/xfailed/removed): {_fmt(lost)}"
            )

    now_hashes = _test_file_hashes(target)
    removed = sorted(f for f in baseline_hashes if f not in now_hashes)
    modified = sorted(
        f for f, h in baseline_hashes.items() if f in now_hashes and now_hashes[f] != h
    )
    transitions = approved_transitions or {}
    reviewed = sorted(
        path for path in modified
        if transitions.get(path) == (baseline_hashes[path], now_hashes[path])
    )
    modified = sorted(set(modified) - set(reviewed))
    if removed:
        reasons.append(f"{len(removed)} pre-existing test file(s) removed: {_fmt(removed)}")
    if modified:
        reasons.append(
            f"{len(modified)} pre-existing test file(s) modified "
            f"(regression-witness integrity): {_fmt(modified)}"
        )

    if reasons:
        return CheckResult(
            kind=CheckKind.TEST,
            name="regression-baseline",
            passed=False,
            severity=Severity.ERROR,
            details="regression vs baseline: " + "; ".join(reasons),
        )
    unsigned = sorted(set(reviewed) & (policy_approved or set()))
    approvals_note = f"{len(reviewed)} exact signed semantic transition(s) approved"
    if unsigned:
        approvals_note = (
            f"{len(reviewed) - len(unsigned)} exact signed semantic transition(s) approved; "
            f"{len(unsigned)} auto-approved by policy (no human signature): {_fmt(unsigned)}"
        )
    return CheckResult(
        kind=CheckKind.TEST,
        name="regression-baseline",
        passed=True,
        severity=Severity.INFO,
        details=(
            f"no regression: all {len(baseline_passing)} baseline-passing test(s) still pass, "
            f"{len(baseline_hashes) - len(reviewed)} pre-existing test file(s) unchanged; "
            f"{approvals_note}"
        ),
    )


__all__ = [
    "BASELINE_FILE",
    "RegressionError",
    "baseline_candidate",
    "capture_baseline",
    "compare",
    "create_semantic_review_challenge",
    "load_baseline",
    "missing_baseline_check",
    "policy_approved_paths",
    "verified_semantic_transitions",
]
