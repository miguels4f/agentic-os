"""Human-declared, environment-only known test failures — one narrow widening of the pytest gate.

The problem (pilot run 3): a repo has tests that fail only because of the environment (they read a
file that exists on one laptop). ``regression.compare()`` correctly reports "no regression" for
them — they never passed at baseline — but the plain ``pytest`` check hard-fails on ANY red test,
so the IMPROVE gate is stuck forever and the only "way out" is hand-editing the run's audit files.

The fix is deliberately NOT an override. A failing pytest check is tolerated only when EVERY
failing test is covered, and each covered test passes ALL of these checks:

* **Declared before the run.** The list is read from ``.hw-known-failures.json`` *as committed at
  the run's base commit* (``RunState.base_commit``, recorded by ``hw plan``). An uncommitted edit,
  or a commit made during the run, is ignored — nobody inside the run can widen the list.
* **Narrow + justified.** Each entry — written relative to the git repository root — is an exact
  node id, a ``file.py::name-pattern``, a whole ``file.py``, or a ``dir/`` prefix — never a
  wildcard in the path part — and carries a non-empty human ``reason``. At most
  :data:`MAX_ENTRIES` entries. An invalid file excuses nothing.
* **Pre-existing and untouched.** The failing test's file existed at the base commit and is
  byte-identical to it now. A new test dropped into a declared directory, or an edit to a declared
  test file, is never excused.
* **Never a regression.** A test that PASSED at the regression baseline is never excused, whatever
  the list says (and ``regression.compare`` independently fails it as well).
* **Only real test failures.** Only a pytest run that completed with failures (rc=1, severity
  ERROR) whose failing ids were all parsed is eligible — never a collection error / interrupt /
  internal error, never a run whose failure count could not be reconciled with the parsed ids.

What this module cannot do (stated so nobody over-trusts it): git cannot tell a human commit from
an agent commit. What it guarantees is that the list existed, committed and reviewable, before the
run began; the worker write-scope blocks the file (``DEFAULT_BLOCKED_GLOBS``) so no packet can
author it. Pure stdlib + pydantic + ``git`` subprocesses; no LLM.
"""

from __future__ import annotations

import fnmatch
import json
import subprocess
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from hyperwilfred_core.evaluators.verification import CheckResult, Severity

DECLARATION_FILE = ".hw-known-failures.json"
MAX_ENTRIES = 25
_WILDCARDS = frozenset("*?[]")
# Finding rule ids emitted by PytestEvaluator (see evaluators/code/pytest_evaluator.py).
RULE_FAILED = "pytest-failed"
RULE_ERROR = "pytest-error"
RULE_UNPARSED = "pytest-unparsed"


class KnownFailure(BaseModel):
    """One declared environment-only failure: a narrow selector + the human reason."""

    model_config = ConfigDict(extra="forbid")

    id: str
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("every known failure needs a human-readable reason")
        return v.strip()

    @field_validator("id")
    @classmethod
    def _narrow_selector(cls, v: str) -> str:
        v = v.strip()
        path, sep, name = v.partition("::")
        parts = PurePosixPath(path).parts
        too_broad = (
            not path
            or path in {".", "./", "/"}
            or path.startswith("/")
            or ".." in parts
            or any(ch in _WILDCARDS for ch in path)
        )
        if too_broad:
            raise ValueError(
                f"known-failure selector {v!r} is too broad — the path part must be a concrete "
                "repo-relative test file or directory with no wildcards"
            )
        if sep:
            if not path.endswith(".py"):
                raise ValueError(f"known-failure selector {v!r}: '::' requires a .py test file")
            if not name.strip() or set(name) <= {"*", "?"}:
                raise ValueError(
                    f"known-failure selector {v!r}: the test-name part is empty or all-wildcard "
                    "— name the whole file instead so the breadth is explicit"
                )
        elif not (path.endswith(".py") or path.endswith("/")):
            raise ValueError(
                f"known-failure selector {v!r} must be a node id, a .py file, or a 'dir/' prefix"
            )
        return v


class KnownFailuresDeclaration(BaseModel):
    """The whole ``.hw-known-failures.json`` file."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(1, ge=1, le=1)
    entries: list[KnownFailure] = Field(default_factory=list, max_length=MAX_ENTRIES)


# --------------------------------------------------------------------------- matching


def entry_matches(selector: str, node_id: str) -> bool:
    """True iff *node_id* (``path::name[param]``) is covered by *selector*."""
    sel_path, sep, sel_name = selector.partition("::")
    node_path, _, node_name = node_id.partition("::")
    if not sep and sel_path.endswith("/"):
        return node_path.startswith(sel_path)
    if node_path != sel_path:
        return False
    if not sep:
        return True
    if not any(ch in "*?" for ch in sel_name):
        return node_name == sel_name  # exact (parametrize brackets are literal, not a glob class)
    return fnmatch.fnmatchcase(node_name, sel_name)


# --------------------------------------------------------------------------- git helpers


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )


def head_commit(repo_root: Path) -> str | None:
    """The current HEAD sha of *repo_root*, or ``None`` if it is not a git repo / has no commit."""
    out = _git(repo_root, "rev-parse", "--verify", "HEAD")
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else None


def _toplevel(path: Path) -> Path | None:
    out = _git(path, "rev-parse", "--show-toplevel")
    return Path(out.stdout.strip()).resolve() if out.returncode == 0 else None


def read_declaration(
    repo_root: Path, base_commit: str
) -> tuple[KnownFailuresDeclaration | None, str | None]:
    """Read the declaration AS COMMITTED at *base_commit* → ``(declaration, error)``.

    ``(None, None)`` = no declaration at the base commit. ``(None, error)`` = present but invalid
    (fail-closed: an invalid declaration excuses nothing).
    """
    top = _toplevel(repo_root)
    if top is None:
        return None, "repo root is not inside a git repository"
    rel = (Path(repo_root).resolve() / DECLARATION_FILE).relative_to(top).as_posix()
    out = _git(top, "show", f"{base_commit}:{rel}")
    if out.returncode != 0:
        return None, None
    try:
        return KnownFailuresDeclaration.model_validate(json.loads(out.stdout)), None
    except (json.JSONDecodeError, ValidationError) as exc:
        return None, f"{DECLARATION_FILE} at {base_commit[:12]} is invalid: {exc}"


def canonical_node_id(node_id: str, target: Path, top: Path) -> str | None:
    """*node_id* rewritten relative to the git top-level (``None`` if its file is not found).

    Pytest prints node ids relative to the invocation directory (short summary) or its rootdir,
    so the same test can appear as ``../../repo/pkg/test_x.py::t`` or ``pkg/test_x.py::t``.
    Declarations and comparisons use ONE form: the repo-relative path. Try the invocation cwd
    first, then walk up from the target to the top-level.
    """
    path, sep, name = node_id.partition("::")
    start = target if target.is_dir() else target.parent
    for base in (Path.cwd(), start, *start.parents):
        candidate = (base / path).resolve()
        if candidate.is_file():
            try:
                rel = candidate.relative_to(top).as_posix()
            except ValueError:
                return None
            return f"{rel}{sep}{name}"
        if base == top:
            break
    return None


def _unchanged_since(top: Path, rel: str, base_commit: str) -> bool:
    """The file existed at *base_commit* and the working tree copy is identical to it."""
    exists = _git(top, "cat-file", "-e", f"{base_commit}:{rel}").returncode == 0
    if not exists:
        return False
    return _git(top, "diff", "--quiet", base_commit, "--", rel).returncode == 0


# --------------------------------------------------------------------------- the gate hook


def _annotate(check: CheckResult, note: str) -> CheckResult:
    return check.model_copy(update={"details": f"{note}\n{check.details or ''}".strip()})


def apply(
    check: CheckResult,
    *,
    repo_root: Path,
    target: Path,
    base_commit: str | None,
    baseline_passing: set[str] | None,
) -> CheckResult:
    """Return *check* unchanged, or a non-blocking WARNING if every failure is a declared one.

    Only ever turns a failing ``pytest`` ERROR into a passing WARNING; it never makes any other
    check pass and never touches a passing check.
    """
    if check.name != "pytest" or check.passed or check.severity is not Severity.ERROR:
        return check
    if base_commit is None:
        return check
    declaration, error = read_declaration(repo_root, base_commit)
    if error:
        return _annotate(check, f"known-failures: declaration ignored — {error}")
    if declaration is None or not declaration.entries:
        return check

    if any(f.rule_id == RULE_UNPARSED for f in check.findings):
        return _annotate(
            check, "known-failures: not applied — failing tests could not be fully identified"
        )
    failing = [f.file for f in check.findings if f.rule_id in (RULE_FAILED, RULE_ERROR)]
    if not failing:
        return _annotate(check, "known-failures: not applied — no failing test ids parsed")

    top = _toplevel(Path(target)) or _toplevel(repo_root)
    if top is None:
        return check

    target = Path(target).resolve()
    baseline_canonical = {
        c for c in (canonical_node_id(b, target, top) for b in baseline_passing or ()) if c
    }
    tolerated: list[str] = []
    refused: list[str] = []
    for raw_id in failing:
        node_id = canonical_node_id(raw_id, target, top)
        if node_id is None:
            refused.append(f"{raw_id} (test file not found inside the repository)")
            continue
        entry = next((e for e in declaration.entries if entry_matches(e.id, node_id)), None)
        if entry is None:
            refused.append(f"{node_id} (not declared)")
            continue
        if node_id in baseline_canonical:
            refused.append(f"{node_id} (passed at the regression baseline — a regression)")
            continue
        if not _unchanged_since(top, node_id.partition("::")[0], base_commit):
            refused.append(f"{node_id} (test file new or modified since {base_commit[:12]})")
            continue
        tolerated.append(f"{node_id} — {entry.reason}")

    if refused:
        return _annotate(
            check,
            f"known-failures: {len(refused)} failing test(s) NOT excusable: "
            + "; ".join(refused),
        )
    return CheckResult(
        kind=check.kind,
        name=check.name,
        passed=True,
        severity=Severity.WARNING,
        details=(
            f"pytest: {len(tolerated)} failing test(s) tolerated as human-declared "
            f"environment-only failures ({DECLARATION_FILE} @ {base_commit[:12]}): "
            + "; ".join(tolerated)
            + f"\n{check.details or ''}"
        ).strip(),
        duration_ms=check.duration_ms,
        findings=check.findings,
    )


__all__ = [
    "DECLARATION_FILE",
    "MAX_ENTRIES",
    "KnownFailure",
    "KnownFailuresDeclaration",
    "apply",
    "canonical_node_id",
    "entry_matches",
    "head_commit",
    "read_declaration",
]
