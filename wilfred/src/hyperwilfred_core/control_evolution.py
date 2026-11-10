"""External, exact-byte authority for one historical pytest review helper.

Unsigned challenges and prepared transactions convey no authority. Only a finalized
transaction reverified against the current external trust root permits the exact change.
Control tokens must remain live even after recording: mutable local receipts never revive
expired signatures. Expired recovery requires newly reviewed evidence and a fresh signature.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from hyperwilfred_core import approvals, path_safety, quality, regression
from hyperwilfred_core.path_safety import validate_identifier
from hyperwilfred_core.schemas import RunState

CONTROL_PATH = "tests/review_attestation.py"
CONTROL_CLASS = "pytest-review-attestation-helper"
PURPOSE = "pytest-control-evolution"
ACTION = "approve narrowly scoped pytest review-attestation helper evolution"
SCHEMA = "wilfred-control-review/v1"


class ControlEvolutionError(RuntimeError):
    """Control review is unavailable, unsafe, incomplete, or not authorized."""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _ancestors(path: Path) -> tuple:
    """Check the entire path, including trusted-root ancestors and missing leaves."""
    identities = []
    for part in [*reversed(path.parents), path]:
        try:
            value = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(value.st_mode):
            raise ControlEvolutionError(f"symlink is not control evidence: {part}")
        identities.append((str(part), value.st_dev, value.st_ino, value.st_mode))
    return tuple(identities)


def _read(path: Path) -> bytes:
    path = _absolute(path)
    before = _ancestors(path)
    try:
        raw = path_safety.stable_regular_bytes(
            path, root=Path(path.anchor), label="control evidence")
    except path_safety.UnstableFileError as exc:
        raise ControlEvolutionError(str(exc)) from exc
    if _ancestors(path) != before or raw is None:
        raise ControlEvolutionError("control evidence ancestors changed during read")
    return raw


def _object(path: Path) -> dict:
    result = json.loads(_read(path))
    if not isinstance(result, dict):
        raise ControlEvolutionError("control evidence must be a JSON object")
    return result


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _put(path: Path, raw: bytes) -> None:
    before = _ancestors(path.parent)
    _ancestors(path)
    if path.exists():
        if _read(path) != raw:
            raise ControlEvolutionError(f"immutable control transaction conflicts: {path.name}")
    else:
        approvals.atomic_create_text(path, raw.decode("utf-8"))
    if _ancestors(path.parent) != before or _read(path) != raw:
        raise ControlEvolutionError("control transaction changed during write")


def _encoded(value: dict) -> bytes:
    return approvals.canonical_bytes(value) + b"\n"


def _context(repo: Path, run_id: str, target: Path, report: Path, dispatch: Path) -> dict:
    validate_identifier(run_id, label="run_id")
    _ancestors(repo)
    _ancestors(target)
    run = repo / ".hw" / run_id
    state_bytes = _read(run / "state.json")
    state = RunState.model_validate_json(state_bytes)
    declared: str | None = state.product_dir
    if declared == f"products/{run_id}":
        declared = getattr(state, "deterministic_target", None)
    if not declared or Path(declared).is_absolute() or ".." in Path(declared).parts:
        raise ControlEvolutionError("control review requires an exact declared target")
    if state.run_id != run_id or target != _absolute(repo / declared):
        raise ControlEvolutionError("control target does not match the run")
    if not target.is_relative_to(repo):
        raise ControlEvolutionError("control target escapes repository")
    baseline_bytes = _read(run / regression.BASELINE_FILE)
    baseline = json.loads(baseline_bytes)
    if not isinstance(baseline, dict):
        raise ControlEvolutionError("baseline must be a JSON object")
    baseline_digest = _sha(baseline_bytes)
    if baseline_digest != state.regression_baseline_sha256:
        raise ControlEvolutionError("immutable baseline bytes disagree with run state")
    roots, _ids, hashes = regression.baseline_scope(target, baseline)
    if CONTROL_PATH in hashes or CONTROL_PATH in baseline["test_file_hashes"]:
        raise ControlEvolutionError("baseline test files cannot receive control exceptions")
    _config, historical = regression.legacy_control_witness(target, roots, state.base_commit)
    before = historical.get(CONTROL_PATH)
    if not before:
        raise ControlEvolutionError("historical commit does not demonstrate the original control")
    after = _sha(_read(target / CONTROL_PATH))
    if before == after:
        raise ControlEvolutionError("control has no change to authorize")
    for path in (report, dispatch):
        if not path.is_relative_to(run):
            raise ControlEvolutionError("report and dispatch must be inside this run")
    report_bytes, dispatch_bytes = _read(report), _read(dispatch)
    report_value, dispatch_value = json.loads(report_bytes), json.loads(dispatch_bytes)
    explanation = report_value.get("explanation") if isinstance(report_value, dict) else None
    if (not isinstance(explanation, str) or len(explanation.strip()) < 40
            or len(explanation.split()) < 6 or not isinstance(dispatch_value, dict)
            or not dispatch_value):
        raise ControlEvolutionError("control report needs a meaningful explanation and dispatch")
    # A review-target override could restrict the snapshot to an unrelated subset.
    override = run / "review-target.json"
    _ancestors(override)
    if override.exists():
        raise ControlEvolutionError("control evolution requires the complete product snapshot")
    snapshot = quality.product_snapshot_sha256(repo, state)
    if _read(run / "state.json") != state_bytes:
        raise ControlEvolutionError("run state changed during control observation")
    return {
        "run_id": run_id, "repo_root": str(repo), "target": str(target),
        "base_commit": state.base_commit, "baseline_sha256": baseline_digest,
        "control_path": CONTROL_PATH, "control_class": CONTROL_CLASS,
        "before_sha256": before, "after_sha256": after,
        "report_file": str(report), "report_sha256": _sha(report_bytes),
        "dispatch_file": str(dispatch), "dispatch_sha256": _sha(dispatch_bytes),
        "purpose": PURPOSE, "action": ACTION, "product_snapshot_sha256": snapshot,
    }


def _evidence(context: dict) -> list[str]:
    return [f"{key}:{value}" for key, value in sorted(context.items())]


def _key(repo: Path, injected: Path | None) -> Path:
    selected = _absolute(injected or approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH)
    try:
        approvals.require_external_key_location(selected, excluded_roots=[repo])
    except approvals.ApprovalError as exc:
        raise ControlEvolutionError(
            f"review authority must remain outside the repository and .hw: {exc}") from exc
    _read(selected)
    return selected


@contextmanager
def _locked(directory: Path):
    try:
        import fcntl
    except ImportError as exc:
        raise ControlEvolutionError("control locking unavailable on this platform") from exc
    _ancestors(directory)
    directory.mkdir(exist_ok=True)
    before = _ancestors(directory)
    lock = directory / ".control.lock"
    _ancestors(lock)
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ControlEvolutionError("control lock is not a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if _ancestors(directory) != before:
            raise ControlEvolutionError("control directory changed before locking")
        yield
        if _ancestors(directory) != before:
            raise ControlEvolutionError("control directory changed during transaction")
    finally:
        os.close(descriptor)


def _verify(directory: Path, gate: str, token: Path, context: dict, key: Path,
            *, consume: bool = False, recorded: bool = False) -> None:
    challenge_path = approvals.challenge_file(directory, gate)
    challenge_bytes, token_bytes, key_bytes = _read(challenge_path), _read(token), _read(key)
    challenge = json.loads(challenge_bytes)
    if (not isinstance(challenge, dict) or challenge.get("purpose") != PURPOSE
            or challenge.get("run_id") != context["run_id"]):
        raise ControlEvolutionError("control challenge purpose or run was altered")
    approval_lock = directory / ".approval.lock"
    _ancestors(approval_lock)
    if approval_lock.exists():
        _read(approval_lock)
    receipt = approvals.consumed_file(directory, gate)
    _ancestors(receipt)
    receipt_bytes = _read(receipt) if receipt.exists() else None
    decision, _reviewer, _note = approvals.verify_and_consume(
        directory, gate, token, expected_run_id=context["run_id"], public_key_path=key,
        expected_action=ACTION, expected_evidence=_evidence(context),
        consume=consume, recorded=False,
    )
    if recorded:
        approvals.verify_consumed_receipt(directory, gate, token)
    if decision != "approve":
        raise ControlEvolutionError("control review was not approved")
    if (_read(challenge_path) != challenge_bytes or _read(token) != token_bytes
            or _read(key) != key_bytes
            or (receipt_bytes is not None and _read(receipt) != receipt_bytes)):
        raise ControlEvolutionError("control authority changed during verification")


def _fresh(context: dict) -> None:
    if _context(Path(context["repo_root"]), context["run_id"], Path(context["target"]),
                Path(context["report_file"]), Path(context["dispatch_file"])) != context:
        raise ControlEvolutionError("reviewed control evidence has changed")


# The recorder writes and the verifier reproduces these exact shapes; each is defined once.
def _gate_id(context: dict) -> str:
    return "control-" + approvals.digest(context)[:56]


def _pending_record(context: dict, gate: str) -> dict:
    return {"schema_version": SCHEMA, "context": context, "gate_id": gate}


def _prepared_record(pending: dict, token_bytes: bytes, challenge_bytes: bytes,
                     key: Path) -> dict:
    return {**pending, "token_sha256": _sha(token_bytes),
            "challenge_sha256": _sha(challenge_bytes),
            "key_fingerprint": approvals.public_key_fingerprint(key)}


def _final_record(prepared: dict, receipt_bytes: bytes) -> dict:
    return {**prepared, "receipt_sha256": _sha(receipt_bytes)}


def _record(repo: Path, run_id: str, target: Path, report: Path, dispatch: Path,
            token: Path | None, injected: Path | None) -> dict:
    context = _context(repo, run_id, target, report, dispatch)
    directory = repo / ".hw" / run_id / "control-reviews"
    gate = _gate_id(context)
    challenge = approvals.challenge_file(directory, gate)
    canonical = directory / f"{gate}.decision.json"
    pending = directory / f"{gate}.pending.json"
    prepared = directory / f"{gate}.prepared.json"
    final = directory / f"{gate}.recorded.json"
    receipt = approvals.consumed_file(directory, gate)
    details = {"gate_id": gate, "attestation_challenge": str(challenge),
               "attestation_decision": str(canonical)}
    if token is not None and not challenge.exists():
        raise ControlEvolutionError("prepare the unsigned control challenge first")
    with _locked(directory):
        for path in (challenge, canonical, pending, prepared, final, receipt):
            _ancestors(path)
        approval_lock = directory / ".approval.lock"
        _ancestors(approval_lock)
        if approval_lock.exists():
            _read(approval_lock)
        if not challenge.exists():
            if token is not None or any(p.exists() for p in (pending, prepared, final, receipt)):
                raise ControlEvolutionError("prepare the unsigned control challenge first")
            approvals.issue_challenge(directory, run_id=run_id, gate_id=gate, action=ACTION,
                                      evidence=_evidence(context), purpose=PURPOSE)
        if token is None and not any(p.exists() for p in (canonical, prepared, final, receipt)):
            approvals.issue_challenge(directory, run_id=run_id, gate_id=gate, action=ACTION,
                                      evidence=_evidence(context), purpose=PURPOSE,
                                      reuse_pending=True)
        expected_pending = _pending_record(context, gate)
        if token is None:
            _put(pending, _encoded(expected_pending))
            return {"status": "pending", **details}
        if _object(pending) != expected_pending:
            raise ControlEvolutionError("pending control evidence conflicts")
        key = _key(repo, injected)
        supplied = _read(token)
        # Verify before publishing any supplied token or prepared manifest.
        _verify(directory, gate, token, context, key, recorded=receipt.exists())
        expected_prepared = _prepared_record(expected_pending, supplied, _read(challenge), key)
        if receipt.exists() and not prepared.exists():
            raise ControlEvolutionError("consumed control review has no prepared transaction")
        if prepared.exists() and _object(prepared) != expected_prepared:
            raise ControlEvolutionError("control recovery requires the exact prepared token")
        _fresh(context)
        _put(canonical, supplied)
        _put(prepared, _encoded(expected_prepared))
        if not receipt.exists():
            if final.exists():
                raise ControlEvolutionError("finalized control review is missing its receipt")
            _fresh(context)
            _verify(directory, gate, canonical, context, key, consume=True)
        _verify(directory, gate, canonical, context, key, recorded=True)
        _fresh(context)
        if (_object(pending) != expected_pending or _object(prepared) != expected_prepared
                or _read(canonical) != supplied):
            raise ControlEvolutionError("prepared control transaction changed after verification")
        expected_final = _final_record(expected_prepared, _read(receipt))
        _put(final, _encoded(expected_final))
        return {"status": "recorded", **details, "record_file": str(final)}


def record_control_review(*, repo_root: Path, run_id: str, target: Path,
                          report_file: Path, dispatch_file: Path,
                          attestation_file: Path | None = None,
                          review_public_key_path: Path | None = None) -> dict:
    """Prepare unsigned evidence or consume an externally signed exact-control review."""
    try:
        return _record(_absolute(repo_root), run_id, _absolute(target), _absolute(report_file),
                       _absolute(dispatch_file), _absolute(attestation_file)
                       if attestation_file is not None else None, review_public_key_path)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        raise ControlEvolutionError(f"control review rejected: {exc}") from exc


def verified_control_transition(*, repo_root: Path, run_id: str, target: Path,
                                baseline: dict, base_commit: str | None,
                                baseline_sha256: str | None,
                                review_public_key_path: Path | None = None,
                                ) -> dict[str, tuple[str, str]]:
    """Return finalized helper authority only while its external signature remains live."""
    try:
        validate_identifier(run_id, label="run_id")
        repo, target = _absolute(repo_root), _absolute(target)
        directory = repo / ".hw" / run_id / "control-reviews"
        _ancestors(directory)
        raw_baseline = _read(repo / ".hw" / run_id / regression.BASELINE_FILE)
        if _sha(raw_baseline) != baseline_sha256 or json.loads(raw_baseline) != baseline:
            return {}
        key = _key(repo, review_public_key_path)
        for final in sorted(directory.glob("control-*.recorded.json")):
            value = _object(final)
            context = value["context"]
            if (context["base_commit"] != base_commit
                    or context["baseline_sha256"] != baseline_sha256
                    or context["run_id"] != run_id or context["repo_root"] != str(repo)
                    or context["target"] != str(target)):
                continue
            current = _context(repo, run_id, target, Path(context["report_file"]),
                               Path(context["dispatch_file"]))
            if current != context:
                continue
            gate = _gate_id(context)
            if value["gate_id"] != gate or final.name != f"{gate}.recorded.json":
                return {}
            pending = _pending_record(context, gate)
            token = directory / f"{gate}.decision.json"
            prepared = _prepared_record(pending, _read(token),
                                        _read(approvals.challenge_file(directory, gate)), key)
            receipt = approvals.consumed_file(directory, gate)
            if (_object(directory / f"{gate}.pending.json") != pending
                    or _object(directory / f"{gate}.prepared.json") != prepared
                    or value != _final_record(prepared, _read(receipt))):
                return {}
            observed = {path: _read(path) for path in
                        (directory / f"{gate}.pending.json", directory / f"{gate}.prepared.json",
                         final, token, receipt, approvals.challenge_file(directory, gate), key)}
            _verify(directory, gate, token, context, key, recorded=True)
            _fresh(context)
            if any(_read(path) != raw for path, raw in observed.items()):
                return {}
            return {CONTROL_PATH: (context["before_sha256"], context["after_sha256"])}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return {}
    return {}
