"""Signed, challenge-bound authorization for protected host actions.

The private signing key is deliberately absent from Wilfred. A trusted host/person signs the
challenge externally; generated workers receive neither that key nor its filesystem location.

One owner-controlled switch can replace that signature for a fixed set of lower-impact approval
points (see :func:`active_policy`). It never signs and never claims a human: it writes an unsigned
decision labelled ``policy:auto``. Without the switch file every approval point needs a signature.
Every run gate (ship, merge, deploy, dependencies, secrets, migrations), learning-proposal
promotion and independent-review attestation need one in every case.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import subprocess
import sys
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from hyperwilfred_core import path_safety
from hyperwilfred_core.path_safety import validate_identifier
from hyperwilfred_core.run_store import atomic_write_text

CHALLENGE_SCHEMA = "wilfred-approval-challenge/v1"
DECISION_SCHEMA = "wilfred-signed-decision/v1"
_KEY_DIRECTORY = ("Library", "Application Support", "Wilfred")
_NO_ACCOUNT_HOME = Path(os.sep, "wilfred-account-home-unavailable")


def _account_home() -> Path:
    """Home directory of the account running the engine, read from the account database.

    The ``HOME`` variable is never consulted: whoever starts the process chooses it. Without
    a usable account record the result is a marker location that the key loader refuses, so
    every default key is unavailable instead of being looked up somewhere else.
    """
    try:
        import pwd

        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, AttributeError, KeyError, OSError, TypeError, ValueError):
        return _NO_ACCOUNT_HOME
    return home if home.is_absolute() else _NO_ACCOUNT_HOME


# Read by attribute at call time; no option and no environment variable selects a key.
DEFAULT_PUBLIC_KEY_PATH = _account_home().joinpath(*_KEY_DIRECTORY, "approval-public-key.pem")
DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH = _account_home().joinpath(
    *_KEY_DIRECTORY, "review-attestation-public-key.pem"
)
POLICY_DECISION_SCHEMA = "wilfred-policy-decision/v1"
POLICY_AUTO_VALUE = "auto-except-ship-and-promote"
POLICY_APPROVER = "policy:auto"
DEFAULT_POLICY_PATH = _account_home().joinpath(*_KEY_DIRECTORY, "policy.json")  # never via HOME
# The only challenge purposes whose human signature the policy switch may replace. Callers must
# also opt in per call (``policy_eligible``). Absent on purpose: ``protected-action`` (every run
# gate — the gate record that tells a ship gate from a minor one is not itself signed, so none
# is eligible), ``learning-proposal`` (promotion) and ``independent-quality-review`` (a policy
# cannot attest independence).
POLICY_ELIGIBLE_PURPOSES = frozenset({
    "quality-review-override", "verify-finding-override", "test-evolution",
})
_POLICY_KEYS = frozenset({"human_approvals", "set_by", "since"})
_POLICY_MAX_BYTES = 4096


class ApprovalError(RuntimeError):
    """Authorization is absent, invalid, expired, or replayed."""


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: object, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ApprovalError(f"invalid {label}") from exc
    if parsed.tzinfo is None:
        raise ApprovalError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def digest(value: object) -> str:
    """Return the sha256 hex digest of *value* in its canonical JSON form."""
    return hashlib.sha256(canonical_bytes({"value": value})).hexdigest()


def _validate_challenge_coherence(challenge: dict[str, Any]) -> None:
    """Reject malformed or signer-visible fields that disagree with their digests."""
    action = challenge.get("action")
    evidence = challenge.get("evidence")
    action_sha256 = challenge.get("action_sha256")
    evidence_sha256 = challenge.get("evidence_sha256")
    if not isinstance(action, str):
        raise ApprovalError("approval challenge action must be a string")
    if not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
        raise ApprovalError("approval challenge evidence must be a list of strings")
    if not isinstance(action_sha256, str) or not hmac.compare_digest(
        action_sha256, digest(action)
    ):
        raise ApprovalError("approval challenge action does not match its digest")
    if not isinstance(evidence_sha256, str) or not hmac.compare_digest(
        evidence_sha256, digest(sorted(evidence))
    ):
        raise ApprovalError("approval challenge evidence does not match its digest")


def challenge_file(gates_dir: Path, gate_id: str) -> Path:
    validate_identifier(gate_id, label="gate_id")
    return gates_dir / f"{gate_id}.challenge.json"


def consumed_file(gates_dir: Path, gate_id: str) -> Path:
    """Return the replay-receipt path written when *gate_id*'s signed decision is consumed."""
    validate_identifier(gate_id, label="gate_id")
    return gates_dir / f"{gate_id}.decision.consumed"


def atomic_create_text(path: Path, value: str) -> None:
    """Create *path* exactly once, durably, never exposing partial bytes; exists -> refused."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ApprovalError("signed decision replay detected") from exc
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


# Name through which verify_and_consume writes its replay receipt (an interception seam).
_atomic_create_text = atomic_create_text


def _serialized(operation):
    @wraps(operation)
    def locked(gates_dir: Path, *args, **kwargs):
        try:
            import fcntl
        except ImportError as exc:
            raise ApprovalError("approval locking unavailable on this platform") from exc
        gates_dir.mkdir(parents=True, exist_ok=True)
        with (gates_dir / ".approval.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return operation(gates_dir, *args, **kwargs)
    return locked


def evidence_gate_id(gates_dir: Path, gate_id: str, evidence: list[str]) -> str:
    """Changed evidence gets a separate identity; consumed authorization stays immutable."""
    fingerprint = digest(sorted(evidence))
    if (challenge_file(gates_dir, gate_id).exists()
            and load_challenge(gates_dir, gate_id).get("evidence_sha256") != fingerprint):
        return f"{gate_id}-{fingerprint[:16]}"
    return gate_id


@_serialized
def issue_challenge(
    gates_dir: Path,
    *,
    run_id: str,
    gate_id: str,
    action: str,
    evidence: list[str],
    purpose: str = "protected-action",
    ttl_seconds: int = 900,
    reuse_pending: bool = False,
) -> dict[str, Any]:
    if not isinstance(action, str):
        raise ApprovalError("approval challenge action must be a string")
    if not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
        raise ApprovalError("approval challenge evidence must be a list of strings")
    validate_identifier(run_id, label="run_id")
    validate_identifier(gate_id, label="gate_id")
    issued = _now()
    if (consumed_file(gates_dir, gate_id).exists()
            or (gates_dir / f"{gate_id}.executed").exists()):
        raise ApprovalError("challenge already consumed or action attempted; use a new gate")
    path = challenge_file(gates_dir, gate_id)
    if path.exists():
        previous = load_challenge(gates_dir, gate_id)
        if reuse_pending:
            if (previous.get("run_id") != run_id or previous.get("purpose") != purpose
                    or previous.get("action_sha256") != digest(action)
                    or previous.get("evidence_sha256") != digest(sorted(evidence))):
                raise ApprovalError("pending challenge evidence changed; use a new gate")
            if issued < _parse_time(previous.get("expires_at"), label="challenge expiry"):
                return previous
        decision = gates_dir / f"{gate_id}.decision.json"
        archive = gates_dir / "history" / f"{gate_id}-{digest(previous)}.json"
        record = json.dumps({"challenge": path.read_text(encoding="utf-8"),
                             "decision": decision.read_text(encoding="utf-8")
                             if decision.exists() else None}, sort_keys=True)
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists():
            if archive.read_text(encoding="utf-8") != record:
                raise ApprovalError("challenge audit conflict; investigate renewal") from None
        else:
            atomic_write_text(archive, record)
    challenge = {
        "schema_version": CHALLENGE_SCHEMA,
        "purpose": purpose,
        "run_id": run_id,
        "gate_id": gate_id,
        "action": action,
        "evidence": sorted(evidence),
        "action_sha256": digest(action),
        "evidence_sha256": digest(sorted(evidence)),
        "nonce": secrets.token_urlsafe(32),
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    atomic_write_text(challenge_file(gates_dir, gate_id), json.dumps(challenge, indent=2) + "\n")
    return challenge


def load_challenge(gates_dir: Path, gate_id: str) -> dict[str, Any]:
    try:
        value = json.loads(challenge_file(gates_dir, gate_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovalError("approval challenge is missing or corrupt") from exc
    if not isinstance(value, dict) or value.get("schema_version") != CHALLENGE_SCHEMA:
        raise ApprovalError("unsupported approval challenge schema")
    _validate_challenge_coherence(value)
    return value


def public_key_fingerprint(path: Path | None = None) -> str:
    key = _load_public_key(path)
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()


def build_decision_payload(
    challenge: dict[str, Any], *, decision: str, decided_by: str, note: str,
    public_key_path: Path | None = None, ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Build the bytes an external trusted signer signs; this function never signs."""
    _validate_challenge_coherence(challenge)
    issued = _now()
    challenge_expiry = _parse_time(challenge.get("expires_at"), label="challenge expiry")
    expiry = min(challenge_expiry, issued + timedelta(seconds=ttl_seconds))
    return {
        "schema_version": DECISION_SCHEMA,
        "purpose": challenge["purpose"],
        "run_id": challenge["run_id"],
        "gate_id": challenge["gate_id"],
        "challenge_sha256": hashlib.sha256(canonical_bytes(challenge)).hexdigest(),
        "nonce": challenge["nonce"],
        "decision": decision,
        "decided_by": decided_by,
        "note": note,
        "key_fingerprint": public_key_fingerprint(public_key_path),
        "issued_at": issued.isoformat(),
        "expires_at": expiry.isoformat(),
    }


def active_policy() -> dict[str, str] | None:
    """Return the owner's human-approvals switch, or ``None`` when signatures are required.

    Fail-closed: an absent, unreadable or malformed file, an unknown key, or any value other than
    the exact expected one means every approval point needs a human signature.
    """
    path = DEFAULT_POLICY_PATH
    try:
        raw = path.read_bytes()
        if len(raw) > _POLICY_MAX_BYTES:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != _POLICY_KEYS:
            return None
        set_by, since = value["set_by"], value["since"]
        if (value["human_approvals"] != POLICY_AUTO_VALUE
                or not isinstance(set_by, str) or not set_by.strip()
                or not isinstance(since, str)):
            return None
        datetime.fromisoformat(since)
    except (OSError, ValueError, RecursionError):
        return None
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
            "human_approvals": POLICY_AUTO_VALUE, "set_by": set_by, "since": since}


def _policy_payload(token: object) -> dict[str, Any] | None:
    payload = token.get("payload") if isinstance(token, dict) else None
    if isinstance(payload, dict) and payload.get("schema_version") == POLICY_DECISION_SCHEMA:
        return payload
    return None


def policy_provenance(token_file: Path) -> dict[str, Any] | None:
    """The truthful label of a policy decision file; ``None`` for a signed or unreadable token."""
    try:
        payload = _policy_payload(json.loads(token_file.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    if payload is None:
        return None
    return {"approved_by": POLICY_APPROVER, "human_signature": False,
            "policy_sha256": str(payload.get("policy_sha256", ""))}


def policy_approve(gates_dir: Path, gate_id: str, *, eligible: bool) -> bool:
    """Write an unsigned policy decision for a pending challenge if the owner's switch allows it.

    Returns True when a decision was written. Never signs, never overwrites a signed or consumed
    decision, and does nothing unless the caller declares the approval point *eligible*. With the
    switch off it returns before touching the gate directory.
    """
    policy = active_policy() if eligible else None
    if policy is None:
        return False
    try:
        return _write_policy_decision(gates_dir, gate_id, policy)
    except (ApprovalError, OSError, KeyError, TypeError):
        return False  # nothing written: the caller then demands the signature as usual


@_serialized
def _write_policy_decision(gates_dir: Path, gate_id: str, policy: dict[str, str]) -> bool:
    if consumed_file(gates_dir, gate_id).exists():
        return False
    try:
        challenge = load_challenge(gates_dir, gate_id)
    except ApprovalError:
        return False
    if challenge.get("purpose") not in POLICY_ELIGIBLE_PURPOSES:
        return False
    decision = gates_dir / f"{gate_id}.decision.json"
    if decision.exists():
        # Only a stale policy decision (bound to a renewed challenge) may be replaced.
        try:
            previous = _policy_payload(json.loads(decision.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return False
        if previous is None or previous.get("nonce") == challenge["nonce"]:
            return False
    payload = {
        "schema_version": POLICY_DECISION_SCHEMA,
        "purpose": challenge["purpose"],
        "run_id": challenge["run_id"],
        "gate_id": challenge["gate_id"],
        "challenge_sha256": hashlib.sha256(canonical_bytes(challenge)).hexdigest(),
        "nonce": challenge["nonce"],
        "decision": "approve",
        "approved_by": POLICY_APPROVER,
        "human_signature": False,
        "policy_sha256": policy["sha256"],
        "policy_path": policy["path"],
        "issued_at": _now().isoformat(),
    }
    atomic_write_text(decision, json.dumps({"payload": payload}, indent=2) + "\n")
    print(f"Auto-approved by policy (no human signature): {challenge['action']} "
          f"[{challenge['run_id']}/{gate_id}]", file=sys.stderr)
    return True


def _consume(gates_dir: Path, gate_id: str, token: dict[str, Any]) -> None:
    _atomic_create_text(
        consumed_file(gates_dir, gate_id), hashlib.sha256(canonical_bytes(token)).hexdigest() + "\n"
    )


def _accept_policy_decision(
    gates_dir: Path, gate_id: str, token_file: Path, token: dict[str, Any],
    challenge: dict[str, Any], *, expected_run_id: str, expected_action: str,
    expected_evidence: list[str], consume: bool, recorded: bool, policy_eligible: bool,
) -> tuple[str, str, str]:
    """Accept an unsigned policy decision only while the switch is on and the point is eligible."""
    payload = token["payload"]
    if not policy_eligible or challenge.get("purpose") not in POLICY_ELIGIBLE_PURPOSES:
        raise ApprovalError("this action needs a human signature; a policy decision is inert")
    policy = active_policy()
    if policy is None:
        raise ApprovalError("policy decision is inert: the human-approvals switch is not active")
    nonce = challenge.get("nonce")
    expected = {
        "purpose": challenge["purpose"],
        "run_id": expected_run_id,
        "gate_id": gate_id,
        "challenge_sha256": hashlib.sha256(canonical_bytes(challenge)).hexdigest(),
        "nonce": nonce,
        "decision": "approve",
        "approved_by": POLICY_APPROVER,
        "human_signature": False,
        # Bound to the exact switch file: editing or replacing it voids earlier policy decisions.
        "policy_sha256": policy["sha256"],
    }
    if (set(token) != {"payload"} or not isinstance(nonce, str) or not nonce
            or any(payload.get(key) != value for key, value in expected.items())):
        raise ApprovalError("policy decision does not match this challenge or the active policy")
    policy_sha256 = policy["sha256"]
    if challenge.get("action_sha256") != digest(expected_action):
        raise ApprovalError("approval challenge does not match the current protected action")
    if challenge.get("evidence_sha256") != digest(sorted(expected_evidence)):
        raise ApprovalError("approval challenge does not match the current action evidence")
    if recorded:
        if consume:
            raise ApprovalError("recorded authorization cannot be consumed again")
        verify_consumed_receipt(gates_dir, gate_id, token_file)
    if consume:
        _consume(gates_dir, gate_id, token)
    note = f"Auto-approved by policy (no human signature); policy_sha256={policy_sha256}"
    return "approve", POLICY_APPROVER, note


def _absolute_key_path(path: Path) -> Path:
    """Absolute form of *path*; a path needing user expansion is refused (it follows ``HOME``)."""
    path = Path(path)
    if path.parts and path.parts[0].startswith("~"):
        raise ApprovalError(f"trusted public key path must not need user expansion: {path}")
    return Path(os.path.abspath(path))


def _require_account_custody(selected: Path) -> None:
    """Refuse a key file or key directory owned by another non-root account."""
    if not hasattr(os, "getuid"):
        return
    try:
        entries = (os.stat(selected.parent), os.lstat(selected))
    except OSError as exc:
        raise ApprovalError(f"trusted approval public key unavailable at {selected}") from exc
    if any(entry.st_uid not in {0, os.getuid()} for entry in entries):
        raise ApprovalError(f"trusted approval public key is not owned by this account: {selected}")


def _load_public_key(path: Path | None) -> Ed25519PublicKey:
    selected = _absolute_key_path(path or DEFAULT_PUBLIC_KEY_PATH)
    if selected.is_relative_to(_NO_ACCOUNT_HOME):
        raise ApprovalError("trusted approval public key unavailable: no account record")
    try:
        raw = path_safety.stable_regular_bytes(selected, label="trusted approval public key")
    except path_safety.UnstableFileError as exc:
        if exc.reason in {"symlink", "not-regular"}:
            raise ApprovalError(
                f"trusted approval public key is not a regular file: {selected}"
            ) from exc
        if exc.reason in {"replaced", "changed"}:
            raise ApprovalError(str(exc)) from exc
        raise ApprovalError(f"trusted approval public key unavailable at {selected}") from exc
    except OSError as exc:
        # The shared reader maps inspection and open failures to its own refusal type, but an
        # error raised by the read itself arrives as a plain OSError; keep this boundary's type.
        raise ApprovalError(f"trusted approval public key unavailable at {selected}") from exc
    assert raw is not None
    _require_account_custody(selected)
    try:
        key = serialization.load_pem_public_key(raw)
    except ValueError as exc:
        raise ApprovalError(f"trusted approval public key unavailable at {selected}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ApprovalError("trusted approval key must be Ed25519")
    return key


def _canonical_existing_path(path: Path) -> Path:
    """Ask the kernel for an opened file's real path, including macOS /.vol aliases."""
    absolute = _absolute_key_path(path)
    try:
        descriptor = os.open(absolute, os.O_RDONLY)
    except FileNotFoundError:
        return Path(os.path.realpath(absolute))
    except OSError as exc:
        raise ApprovalError(f"review key path cannot be inspected safely: {absolute}") from exc
    try:
        if sys.platform == "darwin":
            # F_GETPATH returns the path of the opened inode, not the caller's spelling.
            # In particular, /.vol/<device>/<inode> maps back to its repository path.
            raw = fcntl.fcntl(descriptor, 50, bytes(1024))
            if not isinstance(raw, bytes):
                raise ApprovalError("review key path has no kernel identity")
            canonical = os.fsdecode(raw.split(b"\0", 1)[0])
            if not canonical or not Path(canonical).is_absolute():
                raise ApprovalError("review key path has no kernel identity")
            return Path(canonical)
        return Path(os.path.realpath(absolute))
    except OSError as exc:
        raise ApprovalError(f"review key path has no kernel identity: {absolute}") from exc
    finally:
        os.close(descriptor)


def _registered_work_trees(root: Path) -> set[Path]:
    """Read Git's shared worktree registry, independent of a worker's mutable .git marker."""
    clean_env = {name: value for name, value in os.environ.items()
                 if not name.startswith("GIT_")}
    clean_env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    result = subprocess.run(
        ["git", "-C", str(root), "worktree", "list", "--porcelain"],
        capture_output=True, text=True, check=False, env=clean_env,
    )
    if result.returncode:
        return set()  # An invalid planted .git entry cannot hide an outer repository.
    trees: set[Path] = set()
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            trees.add(_canonical_existing_path(Path(line.removeprefix("worktree "))))
    return trees


def _key_repository_family(candidate: Path) -> set[Path]:
    """Every repository location the key's own surroundings claim to belong to.

    Read from the key's side: each enclosing directory holding a ``.git`` entry contributes
    itself, the worktrees its repository registers, and the Git directory a ``.git`` marker
    file points at. A linked worktree is therefore recognised from its own marker even when
    its entry in the repository's registry was rewritten, renamed or removed, and a run rooted
    in a worktree whose marker was removed is recognised from the registry of the key's side.
    """
    family: set[Path] = set()
    for ancestor in candidate.parents:
        marker = ancestor / ".git"
        if not os.path.lexists(marker):
            continue
        family.add(_canonical_existing_path(ancestor))
        family.update(_registered_work_trees(ancestor))
        try:
            if not stat.S_ISREG(os.lstat(marker).st_mode):
                continue
            text = marker.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.startswith("gitdir:"):
                continue
            pointer = Path(line.removeprefix("gitdir:").strip())
            if not pointer.is_absolute():
                pointer = ancestor / pointer
            pointer = _canonical_existing_path(pointer)
            family.add(pointer)
            try:
                common = Path((pointer / "commondir").read_text(encoding="utf-8").strip())
            except (OSError, UnicodeDecodeError):
                continue
            if not common.is_absolute():
                common = pointer / common
            family.add(_canonical_existing_path(common))
    return family


def _same_or_nested(first: Path, second: Path) -> bool:
    if first.is_relative_to(second) or second.is_relative_to(first):
        return True
    try:
        return first.samefile(second)
    except OSError:
        return False


def require_external_key_location(key: Path, *, excluded_roots: Iterable[Path] = ()) -> Path:
    """Require a review key outside the target, its repository and linked worktrees.

    The opened file's kernel path catches macOS inode aliases. The relation to the repository
    is established from both sides: the fence's Git registry catches a linked worker whose
    .git marker is deleted or replaced, and the key's own surroundings catch a worker whose
    registry entry was rewritten and a run rooted in a worktree whose marker was removed.
    """
    absolute = _absolute_key_path(key)
    canonical = _canonical_existing_path(absolute)
    try:
        if os.stat(absolute).st_nlink > 1:
            raise ApprovalError(f"review key must not have other hard links: {absolute}")
    except FileNotFoundError:
        pass  # The key loader will refuse an absent key.
    except OSError as exc:
        raise ApprovalError(f"review key path cannot be inspected safely: {absolute}") from exc
    fences: set[Path] = set()
    for root in excluded_roots:
        given = _absolute_key_path(Path(root))
        for form in (given, _canonical_existing_path(given)):
            fences.add(form)
            # Continue beyond a planted nested .git: the outer repository still owns the key.
            for ancestor in (form, *form.parents):
                if os.path.lexists(ancestor / ".git"):
                    fences.add(_canonical_existing_path(ancestor))
                    fences.update(_registered_work_trees(ancestor))
    for candidate in (absolute, canonical):
        if any(part.casefold() == ".hw" for part in candidate.parts):
            raise ApprovalError(f"review key must not live inside a .hw directory: {candidate}")
        for fence in fences:
            if candidate.is_relative_to(fence):
                raise ApprovalError(
                    f"review key must live outside the repository and product: {candidate}"
                )
            for ancestor in candidate.parents:
                try:
                    if ancestor.samefile(fence):
                        raise ApprovalError(
                            f"review key must live outside the repository and product: {candidate}"
                        )
                except OSError:
                    continue
        for member in _key_repository_family(candidate):
            if any(_same_or_nested(member, fence) for fence in fences):
                raise ApprovalError(
                    f"review key must live outside the repository and product: {candidate}"
                )
    return canonical


@_serialized
def verify_and_consume(
    gates_dir: Path,
    gate_id: str,
    token_file: Path,
    *,
    expected_run_id: str,
    public_key_path: Path | None = None,
    expected_action: str,
    expected_evidence: list[str],
    consume: bool = True,
    recorded: bool = False,
    policy_eligible: bool = False,
) -> tuple[str, str, str]:
    """Verify and consume once. Recorded review decisions require their immutable receipt;
    only initial acceptance and live protected actions require an unexpired token.

    An unsigned policy decision is accepted only when the caller marks this approval point
    *policy_eligible* and the owner's switch is active; it is reported as ``policy:auto``.
    """
    challenge = load_challenge(gates_dir, gate_id)
    try:
        token = json.loads(token_file.read_text(encoding="utf-8"))
        unsigned_policy = _policy_payload(token) is not None
        payload = token["payload"]
        signature = b"" if unsigned_policy else base64.b64decode(token["signature"], validate=True)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApprovalError("signed decision token is missing or malformed") from exc
    if unsigned_policy:
        return _accept_policy_decision(
            gates_dir, gate_id, token_file, token, challenge,
            expected_run_id=expected_run_id, expected_action=expected_action,
            expected_evidence=expected_evidence, consume=consume, recorded=recorded,
            policy_eligible=policy_eligible,
        )
    if not isinstance(payload, dict) or payload.get("schema_version") != DECISION_SCHEMA:
        raise ApprovalError("unsupported signed decision schema")
    expected = {
        "purpose": challenge["purpose"],
        "run_id": expected_run_id,
        "gate_id": gate_id,
        "challenge_sha256": hashlib.sha256(canonical_bytes(challenge)).hexdigest(),
        "nonce": challenge["nonce"],
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ApprovalError("signed decision does not match this challenge")
    if challenge.get("action_sha256") != digest(expected_action):
        raise ApprovalError("approval challenge does not match the current protected action")
    if challenge.get("evidence_sha256") != digest(sorted(expected_evidence)):
        raise ApprovalError("approval challenge does not match the current action evidence")
    key = _load_public_key(public_key_path)
    if payload.get("key_fingerprint") != public_key_fingerprint(public_key_path):
        raise ApprovalError("signed decision uses an untrusted key")
    try:
        key.verify(signature, canonical_bytes(payload))
    except InvalidSignature as exc:
        raise ApprovalError("signed decision signature is invalid") from exc
    now = _now()
    issued = _parse_time(payload.get("issued_at"), label="decision issued_at")
    expires = _parse_time(payload.get("expires_at"), label="decision expires_at")
    challenge_expires = _parse_time(challenge.get("expires_at"), label="challenge expires_at")
    if recorded:
        if consume:
            raise ApprovalError("recorded authorization cannot be consumed again")
        verify_consumed_receipt(gates_dir, gate_id, token_file)
    if (issued > now + timedelta(seconds=60)
            or (not recorded and now >= min(expires, challenge_expires))):
        raise ApprovalError("signed decision is expired or not yet valid")
    decision = str(payload.get("decision", ""))
    if decision not in {"approve", "reject", "modify_scope"}:
        raise ApprovalError("signed decision value is invalid")
    if consume:
        _consume(gates_dir, gate_id, token)
    return decision, str(payload.get("decided_by", "")), str(payload.get("note", ""))


def verify_consumed_receipt(gates_dir: Path, gate_id: str, token_file: Path) -> None:
    """Prove the currently stored token is the exact token consumed at gate resolution."""
    try:
        token = json.loads(token_file.read_text(encoding="utf-8"))
        observed = consumed_file(gates_dir, gate_id).read_text(encoding="utf-8").strip()
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovalError("approval consumption receipt is missing or corrupt") from exc
    expected = hashlib.sha256(canonical_bytes(token)).hexdigest()
    if not hmac.compare_digest(observed, expected):
        raise ApprovalError("stored decision does not match the consumed approval token")


__all__ = [
    "ApprovalError", "DEFAULT_POLICY_PATH", "DEFAULT_PUBLIC_KEY_PATH",
    "DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH", "POLICY_APPROVER", "POLICY_AUTO_VALUE",
    "POLICY_DECISION_SCHEMA", "POLICY_ELIGIBLE_PURPOSES", "active_policy",
    "atomic_create_text", "build_decision_payload", "canonical_bytes", "consumed_file",
    "digest", "require_external_key_location", "challenge_file", "issue_challenge",
    "load_challenge", "policy_approve", "policy_provenance", "public_key_fingerprint",
    "verify_and_consume", "verify_consumed_receipt",
]
