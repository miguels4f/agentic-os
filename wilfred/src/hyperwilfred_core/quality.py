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
import json
import os
import re
import stat
import subprocess
import unicodedata
from pathlib import Path

from hyperwilfred_core import approvals, gates, guardrails, path_safety, severity_floor
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
    ReviewTrustAnchor,
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
_ATTESTATION_TX_SCHEMA = "wilfred-review-attestation-transaction/v1"


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
    scope_sha256: str,
) -> tuple[str, list[str]]:
    action = f"attest independent quality review {packet_id}"
    evidence = [
        f"run_id:{run_id}",
        f"iteration:{iteration}",
        f"packet_id:{packet_id}",
        f"lens:{lens}",
        f"result_sha256:{record_sha256}",
        f"scope_sha256:{scope_sha256}",
    ]
    return action, evidence


def _stable_regular_bytes(
    path: Path, *, root: Path | None = None, label: str = "file", allow_missing: bool = False,
) -> bytes | None:
    """Read through :func:`path_safety.stable_regular_bytes`; refusals become QualityError.

    Review scope has a deliberately simple policy: every path must be a regular file reached
    without crossing a symlink.  Internal, external, broken and cyclic links are all rejected.
    """
    try:
        return path_safety.stable_regular_bytes(
            path, root=root, label=label, allow_missing=allow_missing,
        )
    except path_safety.UnstableFileError as exc:
        raise QualityError(str(exc)) from exc


def _json_object(path: Path, label: str) -> dict:
    try:
        raw = _stable_regular_bytes(path, label=f"independent-review {label}")
        assert raw is not None
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualityError(f"independent-review {label} is missing or corrupt") from exc
    if not isinstance(value, dict):
        raise QualityError(f"independent-review {label} is invalid")
    return value


def _transaction_paths(directory: Path, gate_id: str) -> tuple[Path, Path, Path]:
    return (
        directory / f"{gate_id}.prepared.json",
        directory / f"{gate_id}.public-key.pem",
        directory / f"{gate_id}.recorded.json",
    )


def _prepared_manifest(
    directory: Path, *, gate_id: str, run_id: str, iteration: int, packet_id: str, lens: str,
    record_sha256: str, action: str, evidence: list[str], token_sha256: str,
    key_fingerprint: str,
) -> dict:
    """The one prepared-manifest record: written on first submission, compared on retry."""
    return {
        "schema_version": _ATTESTATION_TX_SCHEMA, "gate_id": gate_id,
        "run_id": run_id, "iteration": iteration, "packet_id": packet_id,
        "lens": lens, "record_sha256": record_sha256,
        "challenge_sha256": hashlib.sha256(
            approvals.canonical_bytes(approvals.load_challenge(directory, gate_id))
        ).hexdigest(),
        "action_sha256": approvals.digest(action),
        "evidence_sha256": approvals.digest(sorted(evidence)),
        "token_sha256": token_sha256,
        "key_fingerprint": key_fingerprint,
    }


def _recorded_state(directory: Path, gate_id: str, record_sha256: str) -> dict:
    """The one recorded-state record: written when finalized, compared when verified."""
    manifest, _pin, _finalized = _transaction_paths(directory, gate_id)
    return {
        "schema_version": _ATTESTATION_TX_SCHEMA,
        "gate_id": gate_id,
        "record_sha256": record_sha256,
        "manifest_sha256": _sha(manifest),
        "receipt_sha256": _sha(approvals.consumed_file(directory, gate_id)),
    }


def _finalize_attestation(directory: Path, gate_id: str, record_sha256: str) -> str:
    manifest, _pin, finalized = _transaction_paths(directory, gate_id)
    receipt = approvals.consumed_file(directory, gate_id)
    if not manifest.exists() or not receipt.exists():
        raise QualityError("independent-review transaction is not durably consumed")
    value = _recorded_state(directory, gate_id, record_sha256)
    encoded = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if finalized.exists():
        existing = _stable_regular_bytes(finalized, label="independent-review recorded state")
        if existing != encoded.encode():
            raise QualityError("independent-review recorded state conflicts with this result")
    if not finalized.exists():
        atomic_write_text(finalized, encoded)
    return _sha(finalized)


def _verify_finalized(directory: Path, gate_id: str, record_sha256: str) -> str | None:
    _manifest, _pin, finalized = _transaction_paths(directory, gate_id)
    if not finalized.exists():
        return None
    value = _json_object(finalized, "recorded state")
    if value != _recorded_state(directory, gate_id, record_sha256):
        raise QualityError("independent-review recorded state was altered")
    return _sha(finalized)


def _external_review_key(key: Path, repo_root: Path, state: RunState) -> Path:
    """Refuse a review key inside any ``.hw``, the product directory or its git work tree.

    The fence is the run's product directory (the repository itself when it is ``.``); the
    location rule adds the enclosing git work tree.  Raises ``approvals.ApprovalError``.
    """
    return approvals.require_external_key_location(
        key, excluded_roots=[repo_root / state.product_dir]
    )


def _attested_author(
    *, paths, run_id: str, iteration: int, packet_id: str, lens: str,
    record_sha256: str, requested: str | None, attestation_file: str | None,
    recorded_attestation: str | None, state: RunState,
    review_public_key_path: Path | None = None,
) -> tuple[ReviewAuthor, str | None, str | None, dict[str, str]]:
    """Promote to independent only through the external signed-review boundary."""
    declared = ReviewAuthor(requested) if requested is not None else ReviewAuthor.ADAPTER
    if attestation_file is not None and declared is not ReviewAuthor.INDEPENDENT:
        raise QualityError("--attestation-file requires author='independent'")
    if declared is not ReviewAuthor.INDEPENDENT:
        return ReviewAuthor.ADAPTER, None, None, {}

    directory = paths.run_dir / "review-attestations"
    scope_sha256 = _review_scope_sha256(paths.repo_root, run_id, state, packet_id)
    action, evidence = _attestation_values(
        run_id=run_id, iteration=iteration, packet_id=packet_id, lens=lens,
        record_sha256=record_sha256, scope_sha256=scope_sha256,
    )
    base_id = f"review-{iteration}-{hashlib.sha256(packet_id.encode()).hexdigest()[:16]}"
    try:
        gate_id = approvals.evidence_gate_id(directory, base_id, evidence)
        if not approvals.consumed_file(directory, gate_id).exists():
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
        return ReviewAuthor.ADAPTER, None, None, details

    supplied = Path(attestation_file)
    manifest, pinned_key, finalized = _transaction_paths(directory, gate_id)
    receipt = approvals.consumed_file(directory, gate_id)
    anchor_key = f"{iteration}:{lens}"
    anchor = _ensure_quality(state).review_trust_anchors.get(anchor_key)
    recorded = (
        recorded_attestation == gate_id and anchor is not None
        and anchor.gate_id == gate_id and finalized.is_file()
        and _sha(finalized) == anchor.transaction_sha256
    )
    try:
        supplied_bytes = _stable_regular_bytes(supplied, label="supplied attestation token")
        assert supplied_bytes is not None
        supplied_token = json.loads(supplied_bytes)
        token_sha256 = hashlib.sha256(approvals.canonical_bytes(supplied_token)).hexdigest()
        external_key = _external_review_key(
            review_public_key_path or approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH,
            paths.repo_root, state,
        )
        external_fingerprint = approvals.public_key_fingerprint(external_key)
        if not manifest.exists():
            if pinned_key.exists() or receipt.exists() or finalized.exists():
                raise QualityError("independent-review prepared manifest is missing")
            atomic_write_text(canonical_token, supplied_bytes.decode("utf-8"))
            decision, _reviewer, _note = approvals.verify_and_consume(
                directory, gate_id, canonical_token, expected_run_id=run_id,
                public_key_path=external_key, expected_action=action,
                expected_evidence=evidence, consume=False,
            )
            prepared = None
        else:
            prepared = _json_object(manifest, "prepared manifest")
            if prepared.get("token_sha256") != token_sha256:
                raise QualityError("a prepared review can only retry its exact signed token")
            decision, _reviewer, _note = approvals.verify_and_consume(
                directory, gate_id, supplied, expected_run_id=run_id,
                public_key_path=external_key, expected_action=action,
                expected_evidence=evidence, consume=False, recorded=recorded,
            )
        expected_prepared = _prepared_manifest(
            directory, gate_id=gate_id, run_id=run_id, iteration=iteration,
            packet_id=packet_id, lens=lens, record_sha256=record_sha256, action=action,
            evidence=evidence, token_sha256=token_sha256, key_fingerprint=external_fingerprint,
        )
        if prepared is None:
            atomic_write_text(
                manifest, json.dumps(expected_prepared, indent=2, sort_keys=True) + "\n"
            )
        elif prepared != expected_prepared:
            raise QualityError("independent-review prepared manifest was altered")
        trusted_key_bytes = _stable_regular_bytes(external_key, label="external review key")
        assert trusted_key_bytes is not None
        trusted_key = trusted_key_bytes.decode("utf-8")
        if (
            pinned_key.exists()
            and approvals.public_key_fingerprint(pinned_key) != external_fingerprint
        ):
            raise QualityError("independent-review attestation key pin conflicts with manifest")
        if not pinned_key.exists():
            if receipt.exists() or finalized.exists():
                raise QualityError("independent-review key pin is missing")
            atomic_write_text(pinned_key, trusted_key)
        if recorded_attestation not in (None, gate_id):
            raise QualityError("independent-review state names a different attestation")
        if recorded_attestation == gate_id and not finalized.exists():
            raise QualityError("independent-review recorded state is missing")
        if receipt.exists():
            decision, _reviewer, _note = approvals.verify_and_consume(
                directory, gate_id, canonical_token, expected_run_id=run_id,
                public_key_path=pinned_key, expected_action=action,
                expected_evidence=evidence, consume=False, recorded=recorded,
            )
        else:
            if finalized.exists():
                raise QualityError("independent-review receipt is missing")
            decision, _reviewer, _note = approvals.verify_and_consume(
                directory, gate_id, canonical_token, expected_run_id=run_id,
                public_key_path=pinned_key, expected_action=action,
                expected_evidence=evidence,
            )
        if finalized.exists():
            _verify_finalized(directory, gate_id, record_sha256)
    except (approvals.ApprovalError, OSError, UnicodeError, ValueError,
            json.JSONDecodeError) as exc:
        raise QualityError(f"independent-review attestation is invalid: {exc}") from exc
    if decision != "approve":
        raise QualityError(f"independent-review attestation was {decision}, not approved")
    return ReviewAuthor.INDEPENDENT, gate_id, external_fingerprint, details


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


def _deterministic_green(state: RunState, repo_root: Path, run_id: str) -> bool:
    """A green step remains green only while its evaluated product bytes remain current."""
    q = state.quality
    if q is None or state.step_status.get(q.det_gate_step) != StepStatus.DONE:
        return False
    if state.status is RunStatus.GATE_FAILED:
        return False
    if not state.deterministic_evidence_emitted:  # legacy runs have no modern snapshot pair
        return True
    run_dir = repo_root / ".hw" / run_id
    pinned = run_dir / "deterministic-scope.sha256"
    verification = run_dir / "evidence" / "verification.json"
    try:
        anchored = state.deterministic_scope_sha256
        return (anchored is not None and pinned.is_file() and verification.is_file()
                and pinned.read_text(encoding="utf-8").strip() == anchored
                and product_snapshot_sha256(repo_root, state) == anchored)
    except (OSError, QualityError, UnicodeError):
        return False


def _inside_git_work_tree(repo_root: Path) -> bool:
    """True inside a git work tree; False only when explicitly outside every repository.

    Outside means git itself answers "not a git repository" AND no ``.git`` entry exists in
    *repo_root* or any parent.  Every other outcome (missing git executable, unreadable or
    corrupt repository, unexpected answer) raises, so the review scope never binds to nothing.
    """
    try:
        probe = _scope_git(repo_root, "rev-parse", "--is-inside-work-tree")
    except OSError as exc:
        raise QualityError(f"cannot determine review scope: git is unavailable ({exc})") from exc
    if probe.returncode == 0 and probe.stdout.strip() == b"true":
        return True
    absolute = Path(os.path.abspath(repo_root))
    marker = any(os.path.lexists(parent / ".git") for parent in (absolute, *absolute.parents))
    if probe.returncode != 0 and b"not a git repository" in probe.stderr and not marker:
        return False
    detail = os.fsdecode(probe.stderr).strip() or os.fsdecode(probe.stdout).strip()
    raise QualityError(f"cannot determine working-tree review scope: {detail or 'git error'}")


def _product_files_outside_git(repo_root: Path, product_dir: str) -> list[str]:
    """Every non-directory entry under *product_dir* (links included so they are refused).

    Engine state (``.hw``) and git metadata (``.git``) at the repository root are never product.
    """
    relative_dir = Path(product_dir)
    if relative_dir.is_absolute() or ".." in relative_dir.parts:
        raise QualityError("product directory escapes the repository")
    root = Path(os.path.abspath(repo_root))
    candidate = root
    for component in relative_dir.parts:
        candidate /= component
        try:
            metadata = os.lstat(candidate)
        except FileNotFoundError:
            return []  # nothing built yet; creating any product file changes the snapshot
        if stat.S_ISLNK(metadata.st_mode):
            raise QualityError(f"product directory contains symlink {candidate}")
    if not candidate.is_dir():
        return [candidate.relative_to(root).as_posix()]

    def refuse(exc: OSError) -> None:
        raise QualityError(f"cannot enumerate product files outside git: {exc}") from exc

    out: list[str] = []
    for current, dirnames, filenames in os.walk(candidate, onerror=refuse, followlinks=False):
        base = Path(current)
        kept: list[str] = []
        for name in dirnames:
            entry = base / name
            if entry.relative_to(root).as_posix() in (".hw", ".git"):
                continue
            if entry.is_symlink():
                out.append(entry.relative_to(root).as_posix())  # refused when read
            else:
                kept.append(name)
        dirnames[:] = kept
        out.extend(
            (base / name).relative_to(root).as_posix() for name in filenames
            if (base / name).relative_to(root).as_posix() != ".git"
        )
    return sorted(out)


_SCOPE_EXEMPT_TOP = frozenset({".hw", ".git", ".venv"})  # engine state, git metadata, env
_SCOPE_EXEMPT_CACHES = frozenset({".pytest_cache", ".mypy_cache", ".ruff_cache"})  # any depth


def _scope_git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        LC_ALL="C", LANGUAGE="C", GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0",
        # No account-wide or system-wide git configuration or attributes steer the scope.
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM="1",
        # A partial clone must not fetch missing objects from a remote the repository names
        # (its transport settings could start a program), and git never waits for a password.
        GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0",
    )
    return env


def _neutralized_filters(repo_root: Path, env: dict[str, str]) -> list[str]:
    """``-c`` options that turn every ``filter.<name>.*`` driver of the repository into a no-op.

    A repository can name a clean/smudge/process command in its own configuration and bind it to
    paths through ``.gitattributes``; ``git status`` would then run that command.  Every driver
    found is replaced by ``cat`` (a plain copy) and its long-running ``process`` is cleared.
    """
    found = subprocess.run(
        ["git", "-C", str(repo_root), "--no-replace-objects", "config", "--name-only",
         "--get-regexp", r"^filter\."],
        capture_output=True, check=False, env=env,
    )
    options: list[str] = []
    for name in sorted({
        line.rsplit(b".", 1)[0].decode("utf-8", "replace")
        for line in found.stdout.splitlines() if line.count(b".") >= 2
    }):
        options += ["-c", f"{name}.clean=cat", "-c", f"{name}.smudge=cat",
                    "-c", f"{name}.process=", "-c", f"{name}.required=false"]
    return options


def _scope_git(
    repo_root: Path, *args: str, stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run git for the review scope without inherited GIT_* overrides, replace refs, outside
    configuration, or any repository-defined filter command."""
    env = _scope_git_env()
    return subprocess.run(
        ["git", "-C", str(repo_root), "--no-replace-objects", "-c", "core.fsmonitor=false",
         "-c", "core.untrackedCache=false", "-c", f"core.attributesFile={os.devnull}",
         "-c", "diff.ignoreSubmodules=all", "-c", "submodule.recurse=false",
         *_neutralized_filters(repo_root, env), *args],
        capture_output=True, check=False, env=env, input=stdin,
    )


def _blob_id(payload: bytes, width: int) -> str:
    digest = hashlib.sha1(usedforsecurity=False) if width == 40 else hashlib.sha256()
    digest.update(b"blob %d\0" % len(payload))
    digest.update(payload)
    return digest.hexdigest()


def _differs_from_base(path: Path, mode: str, blob: str) -> bool:
    try:
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode):
            return mode != "120000" or _blob_id(os.fsencode(os.readlink(path)), len(blob)) != blob
        if not stat.S_ISREG(metadata.st_mode) or mode not in ("100644", "100755"):
            return True
        return _blob_id(path.read_bytes(), len(blob)) != blob
    except OSError:
        return True  # missing or unreadable: listed, then refused or recorded when hashed


def _verified_objects(
    repo_root: Path, base_commit: str, ids: list[str],
) -> dict[str, tuple[bytes, bytes]]:
    """``{id: (type, body)}`` read in one batch; every body must hash to the id it was asked by.

    Git itself does not re-hash a tree it reads, so an overwritten loose object would otherwise
    be believed.  Both object formats are covered: the id length selects the hash.
    """
    refusal = f"cannot determine committed review scope from {base_commit!r}"
    try:
        done = _scope_git(
            repo_root, "cat-file", "--batch", stdin="".join(f"{i}\n" for i in ids).encode(),
        )
    except OSError as exc:
        raise QualityError(f"{refusal}: {exc}") from exc
    if done.returncode != 0:
        detail = os.fsdecode(done.stderr).strip() or "unknown git error"
        raise QualityError(f"{refusal}: {detail}")
    out, offset = done.stdout, 0
    found: dict[str, tuple[bytes, bytes]] = {}
    for object_id in ids:
        end = out.find(b"\n", offset)
        header = out[offset:end].split() if end >= 0 else []
        if len(header) != 3 or header[0] != object_id.encode() or not header[2].isdigit():
            raise QualityError(f"{refusal}: object {object_id} is unavailable")
        kind, size = header[1], int(header[2])
        body = out[end + 1:end + 1 + size]
        digest = hashlib.new(
            "sha1" if len(object_id) == 40 else "sha256",
            kind + b" %d\0" % size + body, usedforsecurity=False,
        ).hexdigest()
        if len(body) != size or digest != object_id:
            raise QualityError(f"{refusal}: object {object_id} does not hash to its own id")
        found[object_id] = (kind, body)
        offset = end + 1 + size + 1
    return found


def _base_tree(repo_root: Path, base_commit: str) -> dict[str, tuple[str, str]]:
    """``{path: (mode, blob id)}`` of *base_commit*, hash-verified at every tree level."""
    refusal = f"cannot determine committed review scope from {base_commit!r}"
    try:
        named = _scope_git(repo_root, "rev-parse", "--verify", f"{base_commit}^{{commit}}")
        where = _scope_git(repo_root, "rev-parse", "--show-prefix")
    except OSError as exc:
        raise QualityError(f"{refusal}: {exc}") from exc
    commit = named.stdout.strip().decode("ascii", "replace")
    if where.returncode != 0 or named.returncode != 0 or not re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", commit,
    ):
        detail = os.fsdecode(named.stderr or where.stderr).strip() or "unknown git error"
        raise QualityError(f"{refusal}: {detail}")
    prefix = os.fsdecode(where.stdout.removesuffix(b"\n"))  # "" at the top level, else "dir/"
    kind, body = _verified_objects(repo_root, base_commit, [commit])[commit]
    if kind != b"commit" or not body.startswith(b"tree "):
        raise QualityError(f"{refusal}: not a commit")
    width = len(commit) // 2
    pending = {"": body[5:5 + len(commit)].decode("ascii", "replace")}
    base: dict[str, tuple[str, str]] = {}
    while pending:
        objects = _verified_objects(repo_root, base_commit, sorted(set(pending.values())))
        level, pending = pending, {}
        for directory, tree_id in level.items():
            kind, body = objects[tree_id]
            if kind != b"tree":
                raise QualityError("malformed git tree entry in review scope")
            offset = 0
            while offset < len(body):
                space = body.find(b" ", offset)
                null = body.find(b"\0", space)
                name = body[space + 1:null]
                if space < 0 or null < 0 or null + 1 + width > len(body) or (
                    name in (b"", b".", b"..") or b"/" in name
                ):
                    raise QualityError("malformed git tree entry in review scope")
                mode, object_id = body[offset:space], body[null + 1:null + 1 + width].hex()
                path = f"{directory}/{os.fsdecode(name)}" if directory else os.fsdecode(name)
                if mode == b"40000":
                    pending[path] = object_id
                elif mode != b"160000":  # a nested repository entry has no bytes here
                    base[path] = (mode.decode("ascii", "replace"), object_id)
                offset = null + 1 + width
    return {path[len(prefix):]: entry for path, entry in base.items() if path.startswith(prefix)}


def _venv_startup_files(root: Path) -> set[str]:
    """Interpreter start-up hooks of the exempt top-level ``.venv``, bound despite the exemption.

    The directory itself is installed tooling and is not walked, but ``*.pth`` files and
    ``sitecustomize`` / ``usercustomize`` modules in its site-packages run on every interpreter
    start, so they are part of the reviewed scope (a link among them is refused when read).
    """
    venv = root / ".venv"
    found: set[str] = set()
    try:
        if venv.is_symlink() or not venv.is_dir():
            return found
        if (venv / "pyvenv.cfg").exists() or (venv / "pyvenv.cfg").is_symlink():
            found.add(".venv/pyvenv.cfg")
        sites = [
            *(venv / "lib").glob("python*/site-packages"), venv / "Lib" / "site-packages",
            *(venv / "lib64").glob("python*/site-packages"),
        ]
        for site in sites:
            if site.is_symlink() or not site.is_dir():
                continue
            for entry in os.scandir(site):
                lowered = entry.name.lower()
                if lowered.endswith(".pth") or lowered.split(".")[0] in {
                    "sitecustomize", "usercustomize"
                }:
                    found.add((Path(site) / entry.name).relative_to(root).as_posix())
    except OSError as exc:
        raise QualityError(f"cannot enumerate interpreter start-up files: {exc}") from exc
    return found


def _worktree_delta(
    repo_root: Path, base_commit: str, new_links: set[str] | None = None,
) -> set[str]:
    """Paths whose on-disk bytes differ from *base_commit*, without trusting the git index.

    Ignore rules, index flags, filters and file-system monitors cannot hide a path: the commit
    tree is compared with a plain walk of the directory.  Only the fixed exempt directories are
    not walked; files recorded in the base commit are compared even there.  Links that the base
    commit does not record are also added to *new_links*.
    """
    base = _base_tree(repo_root, base_commit)
    root = Path(os.path.abspath(repo_root))

    def refuse(exc: OSError) -> None:
        raise QualityError(f"cannot enumerate working-tree review scope: {exc}") from exc

    changed: set[str] = set()
    seen: set[str] = set()
    for current, dirnames, filenames in os.walk(root, onerror=refuse, followlinks=False):
        here = Path(current)
        exempt = _SCOPE_EXEMPT_CACHES | (_SCOPE_EXEMPT_TOP if here == root else frozenset())
        links = [name for name in dirnames if (here / name).is_symlink()]
        dirnames[:] = [name for name in dirnames if name not in links and name not in exempt]
        for name in [*filenames, *links]:
            relative = (here / name).relative_to(root).as_posix()
            if here == root and name in _SCOPE_EXEMPT_TOP:
                continue
            seen.add(relative)
            if relative not in base:
                changed.add(relative)
                if new_links is not None and (here / name).is_symlink():
                    new_links.add(relative)
            elif _differs_from_base(here / name, *base[relative]):
                changed.add(relative)
    for relative, (mode, blob) in base.items():
        if relative not in seen and _differs_from_base(root / relative, mode, blob):
            changed.add(relative)
    changed.update(_venv_startup_files(root) - set(base))
    return changed


def _ignored_by_git(repo_root: Path, candidates: set[str]) -> set[str]:
    """The *candidates* that git neither tracks nor reports: matched by an ignore rule."""
    if not candidates:
        return set()
    try:
        done = _scope_git(
            repo_root, "check-ignore", "-z", "--stdin",
            stdin=b"".join(os.fsencode(item) + b"\0" for item in sorted(candidates)),
        )
    except OSError as exc:
        raise QualityError(f"cannot determine working-tree review scope: {exc}") from exc
    if done.returncode not in (0, 1):
        raise QualityError("cannot determine working-tree review scope")
    return {os.fsdecode(item) for item in done.stdout.split(b"\0") if item} & candidates


def _reviewed_directory(repo_root: Path, relative: str, resolved: Path) -> bytes:
    """Where the directory behind link *relative* lies inside the repository, or a refusal.

    *resolved* is the fully resolved target.  It must be a directory the walk itself binds:
    inside the repository, not the repository root nor a directory that holds the link, and
    neither it nor any directory above it may be an exempt directory.  Exempt is decided by file
    identity and by case-insensitive name, never by the spelling written in the link; any doubt
    is a refusal.
    """
    refusal = f"reviewed path {relative!r} is a link to a directory that is not reviewed"
    try:
        root = Path(os.path.realpath(repo_root))
        holder = Path(os.path.realpath((repo_root / relative).parent))
        if not resolved.is_relative_to(root) or holder.is_relative_to(resolved):
            raise QualityError(refusal)
        parts = resolved.relative_to(root).parts
        here = root
        for depth, name in enumerate(parts):
            here = here / name
            folded = unicodedata.normalize("NFC", name).casefold()
            twins = [root / top for top in sorted(_SCOPE_EXEMPT_TOP)]
            twins += [here.parent / cache for cache in sorted(_SCOPE_EXEMPT_CACHES)]
            if (
                folded in _SCOPE_EXEMPT_CACHES
                or (depth == 0 and folded in _SCOPE_EXEMPT_TOP)
                or not stat.S_ISDIR(os.lstat(here).st_mode)
                or any(os.path.exists(twin) and os.path.samefile(here, twin) for twin in twins)
            ):
                raise QualityError(refusal)
    except (OSError, ValueError) as exc:
        raise QualityError(refusal) from exc
    return os.fsencode("/".join(parts))


def _link_entry(repo_root: Path, relative: str) -> str:
    """Snapshot entry of an ignored link: its target text plus what the link resolves to.

    A regular file (anywhere) is bound by its bytes.  A directory is accepted only where the
    walk binds its files (see ``_reviewed_directory``) and is bound by its resolved location, so
    re-pointing any hop of the chain changes the entry; any other directory is importable code
    nothing binds, so it is refused.  A dangling link is bound as such.  Every other kind of
    target, and a loop, is refused.
    """
    refusal = f"reviewed path {relative!r}"
    try:
        text = os.fsencode(os.readlink(repo_root / relative))  # fails on anything but a link
        resolved = Path(os.path.realpath(repo_root / relative))
    except OSError as exc:
        raise QualityError(f"{refusal} changed while it was bound: {exc}") from exc
    try:
        mode = os.lstat(resolved).st_mode
    except FileNotFoundError:
        kind = b"dangling"
    except OSError as exc:
        raise QualityError(f"{refusal} is a link whose target cannot be inspected") from exc
    else:
        if stat.S_ISREG(mode):
            raw = _stable_regular_bytes(resolved, label=f"target of link {relative!r}")
            kind = b"file\0" + hashlib.sha256(raw or b"").hexdigest().encode()
        elif stat.S_ISDIR(mode):
            kind = b"directory\0" + _reviewed_directory(repo_root, relative, resolved)
        else:
            raise QualityError(f"{refusal} is a link to something that is not a file")
    return "link:" + hashlib.sha256(b"wilfred-ignored-link\0" + text + b"\0" + kind).hexdigest()


def _changed_product_paths(
    repo_root: Path, base_commit: str | None = None, *, product_dir: str | None = None,
    bound_links: set[str] | None = None,
) -> list[str]:
    """Committed and working-tree paths touched since the run began.

    The mode follows the run state recorded at planning time, never the current directory.  A
    run without a base commit (planned outside git or on an unborn HEAD) always covers every file
    under the declared *product_dir*, even if a git repository or first commit appears later.  A
    run with a base commit uses git, and any git failure (or a vanished repository) raises.

    *bound_links* receives the links that only the directory walk finds and that git ignores
    (the layout of installed dependency tools).  Every other link stays refused when read.
    """
    if not base_commit:
        if product_dir is None:
            raise QualityError(
                "cannot determine review scope outside a git work tree without a product directory"
            )
        return [
            p for p in _product_files_outside_git(repo_root, product_dir)
            if p != ".hw" and not p.startswith(".hw/")
        ]
    if not _inside_git_work_tree(repo_root):
        raise QualityError("run began in a git repository that is no longer present")
    try:
        status = _scope_git(
            # Submodules are not entered: their own filters would run.  Their files are still
            # bound by the plain directory walk below.
            repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
            "--ignore-submodules=all",
        )
    except OSError as exc:
        raise QualityError(f"cannot determine working-tree review scope: {exc}") from exc
    if status.returncode != 0:
        raise QualityError("cannot determine working-tree review scope")
    entries = status.stdout.split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(entries) and entries[index]:
        record = entries[index]
        if len(record) < 4:
            raise QualityError("malformed git status entry in review scope")
        paths.add(os.fsdecode(record[3:]))
        if b"R" in record[:2] or b"C" in record[:2]:
            index += 1  # -z emits the old rename/copy name as a second field
            if index >= len(entries) or not entries[index]:
                raise QualityError("malformed git status entry in review scope")
        index += 1
    completed = _scope_git(repo_root, "diff", "--name-only", "-z", f"{base_commit}..HEAD")
    if completed.returncode != 0:
        detail = os.fsdecode(completed.stderr).strip() or "unknown git error"
        raise QualityError(
            f"cannot determine committed review scope from {base_commit!r}: {detail}"
        )
    paths.update(os.fsdecode(item) for item in completed.stdout.split(b"\0") if item)
    walked_links: set[str] = set()
    delta = _worktree_delta(repo_root, base_commit, walked_links)
    if bound_links is not None:
        bound_links.update(_ignored_by_git(repo_root, walked_links - paths))
    paths.update(delta)
    out: list[str] = []
    for p in sorted(paths):
        if p == ".hw" or p.startswith(".hw/"):
            continue
        out.append(p)
    return out


def _review_target(repo_root: Path, run_id: str, state: RunState) -> tuple[list[str], Path] | None:
    """The persisted review target of a REVIEW run; refused on every other run."""
    target_path = repo_root / ".hw" / run_id / "review-target.json"
    if not os.path.lexists(target_path):
        return None
    try:
        from hyperwilfred_core.schemas import ReviewTarget

        raw = _stable_regular_bytes(target_path, root=repo_root, label="review target")
        target = ReviewTarget.model_validate_json(raw or b"")
        if getattr(state, "mode", None) is not BuildMode.REVIEW:
            raise ValueError("a review target is only honored on a review run")
        if target.run_id != run_id:
            raise ValueError("review target belongs to another run")
        if target.root != ".":
            path_safety.validate_relative_path(
                target.root, label="review target root", allow_glob=True,
            )
        if Path(target.root) != Path(state.product_dir):
            raise ValueError("review target root differs from the run's product directory")
        for relative in target.resolved_paths:
            path_safety.validate_relative_path(
                relative, label="review target path", allow_glob=True,
            )
        return list(target.resolved_paths), repo_root / target.root
    except (OSError, ValueError, QualityError) as exc:
        raise QualityError(f"review target is unreadable or invalid: {exc}") from exc


def product_snapshot_sha256(repo_root: Path, state: RunState) -> str:
    """Digest exact reviewed bytes; symlinks are never valid review evidence."""
    files: dict[str, str] = {}
    links: set[str] = set()
    override = _review_target(repo_root, state.run_id, state)
    if override is not None:
        reviewed_paths, review_root = override
    else:
        reviewed_paths = _changed_product_paths(
            repo_root, state.base_commit, product_dir=state.product_dir, bound_links=links,
        )
        review_root = repo_root
    for relative in reviewed_paths:
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise QualityError("changed product path escapes the repository")
        path = review_root / relative
        try:
            relative.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise QualityError(f"reviewed path {relative!r} cannot be encoded") from exc
        if relative in links:
            files[relative] = _link_entry(review_root, relative)
            continue
        raw = _stable_regular_bytes(
            path, root=review_root, label=f"reviewed path {relative!r}", allow_missing=True,
        )
        files[relative] = "missing" if raw is None else hashlib.sha256(raw).hexdigest()
    identity = [str(repo_root.resolve()), state.base_commit, state.product_dir]
    return approvals.digest({"repository": identity, "files": files})


def _review_scope_sha256(
    repo_root: Path, run_id: str, state: RunState, packet_id: str,
) -> str:
    """Bind review authority to current bytes, packet, and the fresh deterministic report."""
    run_dir = repo_root / ".hw" / run_id
    pinned = run_dir / "deterministic-scope.sha256"
    verification = run_dir / "evidence" / "verification.json"
    current = product_snapshot_sha256(repo_root, state)
    if pinned.is_file() != verification.is_file():
        raise QualityError("deterministic review evidence is partial")
    if pinned.is_file():
        observed = pinned.read_text(encoding="utf-8").strip()
        if state.deterministic_evidence_emitted:
            if state.deterministic_scope_sha256 is None:
                raise QualityError("deterministic scope anchor is missing from run state")
            if observed != state.deterministic_scope_sha256:
                raise QualityError("deterministic scope pin differs from its run-state anchor")
        if observed != current:
            raise QualityError("deterministic gate is stale for the current product snapshot")
        deterministic = _sha(verification)
    else:
        if state.deterministic_evidence_emitted:
            raise QualityError("deterministic review evidence was removed")
        q = _ensure_quality(state)
        gate_status = state.step_status.get(q.det_gate_step, StepStatus.PENDING)
        deterministic = f"legacy:{q.det_gate_step}:{gate_status.value}"
    iteration = _ensure_quality(state).iteration
    suffix = packet_id.removeprefix(f"critic-{iteration}-")
    packet = run_dir / "quality" / f"iter-{iteration}" / "critic-packets" / f"{suffix}.json"
    return approvals.digest(
        [current, deterministic, _sha(packet) if packet.is_file() else None]
    )


def _critique_target_paths(repo_root: Path, run_id: str) -> list[str]:
    """The file list the critic fan-out reviews — generalized over an arbitrary target (Phase 6).

    If a REVIEW target was persisted (``review-target.json``, only present on a REVIEW run), the
    critics review its resolved paths — an arbitrary subset / whole repo / git diff range. Otherwise
    (every BUILD / IMPROVE run) they review the build's changed product paths, exactly as before.
    Additive: with no ``review-target.json`` the behaviour is byte-identical to the Phase-2 path.
    """
    from hyperwilfred_core.engine import EngineError, RunPaths, load_state

    try:
        state = load_state(RunPaths(repo_root, run_id))
    except (EngineError, OSError, ValueError) as exc:
        if os.path.lexists(repo_root / ".hw" / run_id / "review-target.json"):
            raise QualityError(f"review target is unreadable or invalid: {exc}") from exc
        raise
    override = _review_target(repo_root, run_id, state)
    if override is not None:
        paths, review_root = override
        for relative in paths:
            _stable_regular_bytes(
                review_root / relative, root=review_root,
                label=f"reviewed path {relative!r}", allow_missing=False,
            )
        return paths
    links: set[str] = set()
    paths = _changed_product_paths(
        repo_root, state.base_commit, product_dir=state.product_dir, bound_links=links,
    )
    for relative in paths:
        if relative in links:
            _link_entry(repo_root, relative)
            continue
        _stable_regular_bytes(
            repo_root / relative, root=repo_root,
            label=f"changed product path {relative!r}", allow_missing=True,
        )
    return paths


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
    RunPaths, load_state, _save_state = _io()
    state = load_state(RunPaths(repo_root, run_id))
    critics_dir = _iter_dir(repo_root, run_id, iteration) / "critics"
    packet_dir = _iter_dir(repo_root, run_id, iteration) / "critic-packets"
    packets = [
        CritiquePacket.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(packet_dir.glob("*.json"))
    ]
    reports: list[CritiqueReport] = []
    for lens in required_lenses(state):
        matching = [
            packet for packet in packets
            if packet.run_id == run_id and packet.iteration == iteration and packet.lens == lens
        ]
        if len(matching) > 1:
            raise QualityError(f"ambiguous canonical critique packet for lens {lens!r}")
        if not matching:
            continue
        expected = critics_dir / f"{lens}.json"
        if (repo_root / matching[0].result_path).resolve() != expected.resolve():
            raise QualityError(f"non-canonical critique result path for lens {lens!r}")
        if not expected.exists():
            continue
        report = CritiqueReport.model_validate_json(expected.read_text(encoding="utf-8"))
        if report.lens != lens:
            raise QualityError(f"canonical critique file for {lens!r} contains {report.lens!r}")
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
    raw = _stable_regular_bytes(path, label=str(path))
    assert raw is not None
    return hashlib.sha256(raw).hexdigest()


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


def _review_evidence(
    repo_root: Path, run_id: str, q: QualityLoopState, state: RunState,
) -> list[str]:
    directory = _iter_dir(repo_root, run_id, q.iteration)
    reports = _read_critique_reports(repo_root, run_id, q.iteration)
    files = [
        directory / "subjective.json",
        *(directory / "critics" / f"{report.lens}.json" for report in reports),
    ]
    verification = repo_root / ".hw" / run_id / "evidence" / "verification.json"
    evidence = [f"product_snapshot_sha256:{product_snapshot_sha256(repo_root, state)}"]
    if verification.is_file():
        evidence.append(f"deterministic_verification_sha256:{_sha(verification)}")
    return [
        *evidence,
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
    try:
        return expected is not None and path.exists() and _sha(path) == expected
    except (OSError, QualityError):
        return False


def _record_attested(
    *, repo_root: Path, run_id: str, state: RunState, q: QualityLoopState, key: str,
    path: Path, packet_id: str, lens: str, review_public_key_path: Path | None = None,
) -> bool:
    anchor_key = f"{q.iteration}:{key}"
    anchor = q.review_trust_anchors.get(anchor_key)
    if anchor is None or q.review_attestations.get(anchor_key) != anchor.gate_id:
        return False
    gate_id = anchor.gate_id
    if not _record_intact(q=q, key=key, path=path):
        return False
    directory = repo_root / ".hw" / run_id / "review-attestations"
    try:
        scope_sha256 = _review_scope_sha256(repo_root, run_id, state, packet_id)
    except QualityError:
        return False
    action, evidence = _attestation_values(
        run_id=run_id, iteration=q.iteration, packet_id=packet_id, lens=lens,
        record_sha256=_sha(path), scope_sha256=scope_sha256,
    )
    token = directory / f"{gate_id}.decision.json"
    pinned_key = directory / f"{gate_id}.public-key.pem"
    try:
        # Verification never depends on earlier calls in this process: a key accepted
        # through an explicit argument must be passed again; otherwise the installed
        # default key decides.
        external_key = _external_review_key(
            review_public_key_path or approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH,
            repo_root, state,
        )
        external_fingerprint = approvals.public_key_fingerprint(external_key)
        if external_fingerprint != anchor.external_key_fingerprint:
            return False
        if approvals.public_key_fingerprint(pinned_key) != external_fingerprint:
            return False
        transaction_sha256 = _verify_finalized(directory, gate_id, _sha(path))
        if transaction_sha256 != anchor.transaction_sha256:
            return False
        decision, _reviewer, _note = approvals.verify_and_consume(
            directory, gate_id, token, expected_run_id=run_id,
            public_key_path=external_key,
            expected_action=action, expected_evidence=evidence, consume=False, recorded=True,
        )
        return decision == "approve"
    except (approvals.ApprovalError, QualityError, OSError, ValueError):
        return False


def critic_authorship(
    *, repo_root: Path, run_id: str, state: RunState, review_public_key_path: Path | None = None,
) -> dict[str, str]:
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
            repo_root=repo_root, run_id=run_id, state=state, q=q, key=lens, path=path,
            packet_id=f"critic-{q.iteration}-{lens}", lens=lens,
            review_public_key_path=review_public_key_path,
        ):
            authors[lens] = ReviewAuthor.INDEPENDENT.value
        else:
            authors[lens] = ReviewAuthor.ADAPTER.value
    return authors


def provenance_report(
    *, repo_root: Path, run_id: str, state: RunState,
    review_public_key_path: Path | None = None,
) -> dict:
    """Who authored the FINAL iteration's evaluation + critiques (fail closed).

    ``ok`` is True iff the subjective evaluation and every required lens are present AND
    independent. Anything adapter-authored, unlabelled (legacy state) or missing is not.
    Attested reviews are verified with *review_public_key_path* when the caller passes it,
    otherwise with the installed default review key; no earlier call changes that choice.
    """
    q = _ensure_quality(state)
    subjective = _load_subjective(repo_root, run_id, q.iteration)
    reports = {r.lens: r for r in _read_critique_reports(repo_root, run_id, q.iteration)}
    iter_dir = _iter_dir(repo_root, run_id, q.iteration)

    def _attested(key: str, path: Path, packet_id: str, lens: str) -> bool:
        return _record_attested(
            repo_root=repo_root, run_id=run_id, state=state, q=q, key=key, path=path,
            packet_id=packet_id, lens=lens, review_public_key_path=review_public_key_path,
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
        try:
            evidence = [
                f"claimed_by:{override.claimed_by}",
                f"reason:{override.claimed_reason}",
                *_review_evidence(repo_root, run_id, q, state),
            ]
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
        except (approvals.ApprovalError, QualityError, ValueError, OSError):
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
    if not _deterministic_green(state, repo_root, run_id):
        raise QualityError("deterministic gate is stale for the current product snapshot")
    gates_dir = paths.run_dir / "gates"
    action = f"accept non-independent quality review for iteration {q.iteration}"
    evidence = [
        f"claimed_by:{by.strip()}",
        f"reason:{reason.strip()}",
        *_review_evidence(repo_root, run_id, q, state),
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


def quality_status(
    *, repo_root: Path, run_id: str, review_public_key_path: Path | None = None,
) -> dict:
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
        "deterministic_green": _deterministic_green(state, repo_root, run_id),
        "product_dir": product_dir,
    }
    if q.phase in (QualityPhase.EVALUATE, QualityPhase.PASSED, QualityPhase.FALLBACK):
        prov = provenance_report(
            repo_root=repo_root, run_id=run_id, state=state,
            review_public_key_path=review_public_key_path,
        )
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
                "changed_paths": _changed_product_paths(
                    repo_root, state.base_commit, product_dir=state.product_dir,
                ),
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
    elif q.phase in (QualityPhase.PASSED, QualityPhase.FALLBACK):
        if base["deterministic_green"]:
            base.update({"action": "done", "passed": True,
                         "used_fallback": q.phase is QualityPhase.FALLBACK})
        else:
            base.update({"action": "evaluate", "passed": False,
                         "next": "product changed; re-run the deterministic gate"})
    else:  # ESCALATED
        base.update({"action": "escalated", "passed": False, "open_blocker": q.open_blocker})
    return base


# --------------------------------------------------------------------------- hw record-subjective


def record_subjective(
    *, repo_root: Path, run_id: str, result_file: str, author: str | None = None,
    attestation_file: str | None = None, review_public_key_path: Path | None = None,
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
    attestation_key = f"{q.iteration}:subjective"
    accepted_author, attestation_id, external_fingerprint, attestation = _attested_author(
        paths=paths, run_id=run_id, iteration=q.iteration,
        packet_id=f"subjective-{q.iteration}", lens="subjective",
        record_sha256=hashlib.sha256(independent_bytes.encode()).hexdigest(),
        requested=author, attestation_file=attestation_file,
        recorded_attestation=q.review_attestations.get(attestation_key),
        state=state, review_public_key_path=review_public_key_path,
    )
    evaluation.author = accepted_author

    iter_dir = _iter_dir(repo_root, run_id, q.iteration)
    atomic_write_text(
        iter_dir / "subjective.json", evaluation.model_dump_json(indent=2) + "\n"
    )
    q.record_hashes[f"{q.iteration}:subjective"] = _sha(iter_dir / "subjective.json")
    if attestation_id is None:
        q.review_attestations.pop(attestation_key, None)
        q.review_trust_anchors.pop(attestation_key, None)
    else:
        q.review_attestations[attestation_key] = attestation_id
    _reset_review(state)

    q.last_global_score = min_score
    if q.best_global_score is None or min_score > q.best_global_score:
        q.best_global_score = min_score
        q.best_iteration = q.iteration
    state.quality = q
    if attestation_id is not None:
        transaction_sha256 = _finalize_attestation(
            paths.run_dir / "review-attestations", attestation_id,
            q.record_hashes[f"{q.iteration}:subjective"],
        )
        assert external_fingerprint is not None
        q.review_trust_anchors[attestation_key] = ReviewTrustAnchor(
            gate_id=attestation_id,
            external_key_fingerprint=external_fingerprint,
            transaction_sha256=transaction_sha256,
        )
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
    review_public_key_path: Path | None = None,
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
    attestation_key = f"{q.iteration}:{report.lens}"
    accepted_author, attestation_id, external_fingerprint, attestation = _attested_author(
        paths=paths, run_id=run_id, iteration=q.iteration, packet_id=packet_id,
        lens=report.lens, record_sha256=hashlib.sha256(independent_bytes.encode()).hexdigest(),
        requested=author, attestation_file=attestation_file,
        recorded_attestation=q.review_attestations.get(attestation_key),
        state=state, review_public_key_path=review_public_key_path,
    )
    report.author = accepted_author

    atomic_write_text(
        iter_dir / "critics" / f"{report.lens}.json", report.model_dump_json(indent=2) + "\n"
    )
    q.record_hashes[f"{q.iteration}:{report.lens}"] = _sha(
        iter_dir / "critics" / f"{report.lens}.json"
    )
    if attestation_id is None:
        q.review_attestations.pop(attestation_key, None)
        q.review_trust_anchors.pop(attestation_key, None)
    else:
        q.review_attestations[attestation_key] = attestation_id
    _reset_review(state)
    state.quality = q
    if attestation_id is not None:
        transaction_sha256 = _finalize_attestation(
            paths.run_dir / "review-attestations", attestation_id,
            q.record_hashes[f"{q.iteration}:{report.lens}"],
        )
        assert external_fingerprint is not None
        q.review_trust_anchors[attestation_key] = ReviewTrustAnchor(
            gate_id=attestation_id,
            external_key_fingerprint=external_fingerprint,
            transaction_sha256=transaction_sha256,
        )
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


def quality_gate(
    *, repo_root: Path, run_id: str, review_public_key_path: Path | None = None,
) -> dict:
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

    det_green = _deterministic_green(state, repo_root, run_id)
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
    prov = provenance_report(
        repo_root=repo_root, run_id=run_id, state=state,
        review_public_key_path=review_public_key_path,
    )
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
            "reason": "the final iteration lacks signed independent review. For each external "
            "result, first record with `--author independent` to create a challenge; have the "
            "trusted external reviewer sign it, place the token at the reported decision path, "
            "then repeat the same record command with `--attestation-file`. If no trusted signer "
            "or review-attestation public key exists, remain pending; only a human may run "
            "`hw quality-override`",
        }
    if subjective.global_score >= q.subjective_threshold:
        # The checkout must hold only accepted work: the commit after the last accepted packet
        # (or patch) is never seen by registration. Refused before any state changes.
        from hyperwilfred_core import engine as engine_module

        engine_module.require_accepted_head(repo_root=repo_root, run_id=run_id,
                                            stage="quality pass")
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
    if packet.packet_id != packet_id or packet.run_id != run_id or packet.iteration != q.iteration:
        raise QualityError("patch packet does not match this run and iteration")

    expected_result = iter_dir / "patch-result.json"
    if (repo_root / packet.result_path).absolute() != expected_result.absolute():
        raise QualityError("patch packet result_path is outside this iteration")
    if result_file is not None:
        requested = Path(result_file)
        if not requested.is_absolute():
            requested = repo_root / requested
        if requested.absolute() != expected_result.absolute():
            raise QualityError("result file must match this patch packet's result_path")
    from hyperwilfred_core import engine as engine_module
    try:
        result_payload = engine_module.require_packet_result(expected_result, repo_root)
        bound_worktree = engine_module.require_packet_worktree(
            repo_root=repo_root, state=state, packet_id=packet_id, worktree=worktree,
            kind="patch", iteration=q.iteration,
        )
    except engine_module.EngineError as exc:
        raise QualityError(str(exc)) from exc
    accepted_changes: dict[str, list[str] | None] = {}
    verdict = guardrails.accept(
        packet_id=packet_id,
        worktree=bound_worktree,
        accepted_changes_out=accepted_changes,
        allowed_globs=packet.allowed_globs,
        blocked_globs=packet.blocked_globs,
        result_file=None,
        max_changed_lines=packet.max_changed_lines,
        max_hunk_span=packet.max_hunk_span,
        full_file_ratio=packet.full_file_ratio,
        do_rollback=do_rollback,
        generated_globs=guardrails.effective_generated_globs(
            [g.path for g in packet.generated_artifacts], packet.allowed_globs
        ),
    )
    verdict.result_read_from_disk = True
    verdict.result_bytes = len(result_payload)
    atomic_write_text(
        iter_dir / "patch-verdict.json", verdict.model_dump_json(indent=2) + "\n"
    )

    if verdict.accepted:
        try:
            engine_module.record_accepted_changes(
                paths, state, engine_module._worktree_slot(
                    packet_id, kind="patch", iteration=q.iteration), accepted_changes)
        except engine_module.EngineError as exc:
            raise QualityError(str(exc)) from exc
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
    "product_snapshot_sha256",
    "quality_gate",
    "quality_status",
    "record_critique",
    "record_patch",
    "record_subjective",
]
