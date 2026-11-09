"""The engine-level guardrail floor — the defensible moat.

Forked and cleaned from the Phase-0 spike (``spike/engine.py``), promoted from throwaway
to real engine code. This module is the **authoritative acceptance gate**: at acceptance
time it compares a worker's changed paths against a machine-readable allow-list and rejects
any escape; it rejects any diff that looks like a full-file regeneration; and on any
rejection it rolls the worktree back to a byte-clean HEAD.

Why the guarantee lives HERE, not in the worker:
* On Claude Code a patcher can be tool-locked (no Write tool), but the BUILD workers DO
  write — so the engine, not the absence of a tool, is what bounds them.
* On Codex ``apply_patch`` can replace a whole file, so the engine MUST reject a wholesale
  rewrite. The Phase-0 spike proved a real Codex regeneration evaded a ratio-only check —
  so the guard uses BOTH a changed-lines budget AND a deletion-ratio signal.

Pure stdlib. Runtime-neutral: it only shells out to ``git``, so it behaves identically
whether the worker was a Claude Code ``Task`` sub-agent or a ``codex exec`` process. The
result is ALWAYS read from a known disk path, never from a worker's stdout (Issue #15451).
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path

from hyperwilfred_core.schemas import (
    AcceptanceVerdict,
    PatchViolation,
    is_denied_generated_path,
)


class GuardrailError(RuntimeError):
    """The worktree cannot be inspected, so the gate fails closed (surfaces as CLI exit 2)."""


def _git(worktree: str, *args: str) -> str:
    """Run a git command inside *worktree*, return stdout (never raises)."""
    out = subprocess.run(
        ["git", "-C", worktree, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout


def _git_checked(worktree: str, *args: str) -> str:
    """Run a READ-side git command and fail closed on any error.

    A corrupt index, a rewritten ``.git`` file or any other git failure prints nothing and exits
    non-zero. Read as "no changes" that would ACCEPT a worker's tree unseen (the second half of
    threat-model finding F-15: ``require_worktree`` alone does not catch a worktree that answers
    ``rev-parse`` but cannot be listed).
    """
    out = subprocess.run(
        ["git", "-C", worktree, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        detail = out.stderr.strip() or f"exit {out.returncode}"
        raise GuardrailError(
            f"git {args[0]} failed in {worktree!r}: {detail} — the acceptance gate fails closed"
        )
    return out.stdout


def require_worktree(worktree: str) -> None:
    """Refuse to judge a folder git cannot report on.

    ``git status`` on a missing folder or a non-repository prints nothing and exits non-zero;
    read as "no changes" that would ACCEPT a packet without looking at any file (threat-model
    finding F-15). The gate therefore fails closed: an uninspectable worktree is an error, never
    an empty change set.
    """
    out = subprocess.run(
        ["git", "-C", worktree, "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0 or out.stdout.strip() != "true":
        detail = out.stderr.strip() or "git reported no work tree"
        raise GuardrailError(
            f"cannot inspect worktree {worktree!r}: {detail} — the acceptance gate fails closed"
        )


def _status_entries(worktree: str, *, strict: bool = True) -> list[tuple[str, str]]:
    """``(XY status, path)`` for every changed path, as ``git status --porcelain`` reports it.

    Uses ``--untracked-files=all`` so each new file is listed individually (not collapsed
    to a directory), which the path allow-list needs to be precise. ``strict`` (the default,
    used by the acceptance gate) raises :class:`GuardrailError` when git cannot list the tree;
    ``strict=False`` is for read-only scope hints (critic review scope), never for authority.
    """
    run = _git_checked if strict else _git
    raw = run(worktree, "status", "--porcelain", "--untracked-files=all")
    entries: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        # porcelain: 'XY <path>' (2 status chars + space)
        p = line[3:].strip()
        if " -> " in p:  # rename arrow 'old -> new' — keep the new path
            p = p.split(" -> ", 1)[1]
        entries.append((line[:2], p.strip('"')))
    return entries


def changed_paths(worktree: str, *, strict: bool = True) -> list[str]:
    """Every path the worker touched (modified, added, untracked). See :func:`_status_entries`."""
    return [p for _, p in _status_entries(worktree, strict=strict)]


ENGINE_STATE_DIR = ".hw"


def is_engine_state(path: str) -> bool:
    return path == ENGINE_STATE_DIR or path.startswith(ENGINE_STATE_DIR + "/")


def ensure_engine_state_excluded(repo_root: str | Path) -> bool:
    """Keep the engine's ``.hw/`` state out of the host repo's commits.

    Appends ``.hw/`` to the repository's LOCAL exclude file (``.git/info/exclude``, shared by
    every worktree) so a ``git add -A`` in the product repo can never stage run state. The
    local exclude is used — not ``.gitignore`` — so the host project's tracked files are not
    touched. Returns False when *repo_root* is not inside a git repository.
    """
    raw = _git(str(repo_root), "rev-parse", "--git-path", "info/exclude").strip()
    if not raw:
        return False
    exclude = Path(raw)
    if not exclude.is_absolute():
        exclude = Path(repo_root) / exclude
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    # Root-anchored only: an unanchored ``.hw/`` would also hide a nested ``src/.hw/`` from
    # ``git status``, so the acceptance gate would never see worker files planted there.
    wanted = {"/.hw", "/.hw/"}
    if any(line.strip() in wanted for line in existing.splitlines()):
        return True
    exclude.parent.mkdir(parents=True, exist_ok=True)
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write(f"{prefix}# HyperWilfred run state — never commit it\n/.hw/\n")
    return True


def tracked_engine_state(path: str | Path, limit: int = 20) -> list[str]:
    """``.hw/`` files that git TRACKS at the repository containing *path* (should be none).

    A folder outside any repository tracks nothing. Any other git failure (corrupt index, broken
    ``.git`` file) raises :class:`GuardrailError`: reading it as "nothing tracked" would pass the
    check unseen, the same fail-open shape as threat-model finding F-15.
    """
    out = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "LC_ALL": "C"},  # the message below is matched in English
    )
    if out.returncode != 0:
        # "Outside any repository" needs BOTH git's exact no-repository message and no ``.git``
        # entry at or above *path*: a broken ``.git`` file says "not a git repository: <gitdir>"
        # and must fail closed, not read as "nothing tracked".
        no_repo = "not a git repository (or any of the parent directories)" in out.stderr
        here = Path(path).resolve()
        if no_repo and not any((d / ".git").exists() for d in (here, *here.parents)):
            return []
        detail = out.stderr.strip() or f"exit {out.returncode}"
        raise GuardrailError(f"git rev-parse failed in {str(path)!r}: {detail}")
    top = out.stdout.strip()
    tracked = _git_checked(top, "ls-files", "--", ENGINE_STATE_DIR).splitlines()
    return [t for t in tracked if t.strip()][:limit]


def matches(path: str, glob: str) -> bool:
    """Path/glob match with gitignore-style ``**/`` semantics on top of fnmatch.

    Python's ``fnmatch`` has no notion of ``**`` matching *zero* leading directories, so a
    blocked glob like ``**/.env`` would silently miss a repo-root ``.env`` (a real guardrail
    gap caught in testing). We additionally try the pattern with a leading ``**/`` stripped,
    so ``**/.env`` covers both ``.env`` and ``a/b/.env``.

    An exact string match always wins, so a literal path holding glob metacharacters (Next.js
    ``[projectId]`` folders) matches itself. A pattern fnmatch cannot compile (``[t-i]`` raises
    on Python 3.9) matches nothing beyond that exact string instead of raising.
    """
    if path == glob:
        return True
    try:
        if fnmatch.fnmatch(path, glob):
            return True
        if glob.startswith("**/") and fnmatch.fnmatch(path, glob[3:]):
            return True
    except re.error:
        return False
    return False


def malformed_globs(globs: list[str]) -> list[str]:
    """The patterns fnmatch cannot compile on this interpreter."""
    bad: list[str] = []
    for g in globs:
        try:
            re.compile(fnmatch.translate(g))
        except re.error:
            bad.append(g)
    return bad


def allowlist_escapes(
    paths: list[str], allow_globs: list[str], blocked_globs: list[str]
) -> list[str]:
    """Return the paths that escape the scope.

    A path escapes if it matches a blocked glob (blocked-by-default wins) OR matches no
    allow glob. Blocked takes precedence so a too-broad allow-list can never re-admit a
    protected path (secrets, .git, .env, migrations). A blocked glob that cannot be evaluated
    blocks every path (fail-closed); an allow glob that cannot be evaluated admits nothing.
    """
    escapes: list[str] = []
    blocked_unreadable = bool(malformed_globs(blocked_globs))
    for p in paths:
        if blocked_unreadable or any(matches(p, g) for g in blocked_globs):
            escapes.append(p)
            continue
        if not any(matches(p, g) for g in allow_globs):
            escapes.append(p)
    return escapes


def matches_segments(path: str, glob: str) -> bool:
    """Segment-aware match: same depth, each segment fnmatch'ed (``*`` never crosses ``/``).

    This is how a DECLARED generated artifact is resolved — identical to ``Path.glob`` in the
    reproducibility check, so the set of files exempted is exactly the set proven reproducible.
    """
    p_parts, g_parts = path.split("/"), glob.split("/")
    return len(p_parts) == len(g_parts) and all(
        fnmatch.fnmatchcase(a, b) for a, b in zip(p_parts, g_parts, strict=True)
    )


def generated_exempt(path: str, generated_globs: list[str]) -> bool:
    """True iff *path* is a declared generated artifact that may skip the patch budgets."""
    return not is_denied_generated_path(path) and any(
        matches_segments(path, g) for g in generated_globs
    )


def effective_generated_globs(generated: list[str], allowed_globs: list[str]) -> list[str]:
    """Only generated paths that sit inside the packet's allow-list keep their exemption.

    The packet JSON lives on disk, so the exemption is re-validated against the scope at
    acceptance time — it can never cover a path the worker was not allowed to write.
    """
    return [g for g in generated if any(matches(g, a) for a in allowed_globs)]


def _original_line_count(worktree: str, path: str) -> int:
    """Line count of *path* at HEAD (0 if the file is new)."""
    out = subprocess.run(
        ["git", "-C", worktree, "show", f"HEAD:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return 0  # new file, not in HEAD
    return len(out.stdout.splitlines())


def _numstat(worktree: str) -> dict[str, tuple[int, int]]:
    """``{path: (added, deleted)}`` for tracked-file modifications vs HEAD."""
    raw = _git_checked(worktree, "diff", "--numstat", "HEAD")
    stats: dict[str, tuple[int, int]] = {}
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if added == "-" or deleted == "-":
            continue  # binary
        stats[path] = (int(added), int(deleted))
    return stats


def _largest_hunk_span(worktree: str, path: str) -> int:
    """Largest contiguous changed-line span in the unified diff for one file."""
    raw = _git_checked(worktree, "diff", "--unified=0", "HEAD", "--", path)
    span = 0
    for line in raw.splitlines():
        if line.startswith("@@"):
            try:
                newpart = line.split("+", 1)[1].split(" ", 1)[0]
                d = int(newpart.split(",")[1]) if "," in newpart else 1
                span = max(span, d)
            except (IndexError, ValueError):
                pass
    return span


def patch_violations(
    worktree: str,
    *,
    max_changed_lines: int,
    max_hunk_span: int,
    full_file_ratio: float,
    generated_globs: list[str] | None = None,
) -> list[PatchViolation]:
    """Detect patch-never-regenerate violations on MODIFIED tracked files.

    Two independent signals — a real Codex regeneration (Phase-0 A3) evaded the ratio
    check but tripped the changed-lines budget, so neither alone is sufficient.

    ``generated_globs`` are paths the packet EXPLICITLY declares as generated (each with a
    required regeneration command — see :class:`GeneratedArtifact`). Those are rewritten
    wholesale by design (a single-line JSON snapshot is 100% changed on every regeneration),
    so they are exempt from the full-file/lines/hunk rules. Every other path — all source
    files — keeps the full protection, and the allow-list escape check still applies to both.
    """
    violations: list[PatchViolation] = []
    exempt = generated_globs or []
    for path, (added, deleted) in _numstat(worktree).items():
        if generated_exempt(path, exempt):
            continue
        orig = _original_line_count(worktree, path)
        reasons: list[str] = []
        if orig > 0 and deleted >= full_file_ratio * orig and added >= full_file_ratio * orig:
            reasons.append(
                f"full-file-replacement (orig={orig}, -{deleted}/+{added}, "
                f"ratio>={full_file_ratio})"
            )
        if added + deleted > max_changed_lines:
            reasons.append(f"too-many-changed-lines ({added + deleted} > {max_changed_lines})")
        span = _largest_hunk_span(worktree, path)
        if span > max_hunk_span:
            reasons.append(f"hunk-span-too-wide ({span} > {max_hunk_span})")
        if reasons:
            violations.append(
                PatchViolation(path=path, added=added, deleted=deleted, reasons=reasons)
            )
    return violations


def rollback(worktree: str) -> None:
    """Leave *worktree* byte-identical to HEAD: discard tracked edits + nuke untracked.

    Excludes the engine's own ``.hw/`` state dir so a rollback can never destroy the run
    ledger it must keep writing to (relevant when the worktree coincides with the repo).
    """
    require_worktree(worktree)
    _git_checked(worktree, "reset", "--hard", "HEAD")
    _git_checked(worktree, "clean", "-fd", "-e", ".hw")


def is_clean(worktree: str) -> bool:
    """True iff the *product* tree is byte-identical to HEAD.

    Ignores the engine's own ``.hw/`` state (which rollback intentionally preserves), so
    this reports the meaningful guarantee — "the worker's writes are gone, the tree matches
    HEAD" — rather than flagging engine bookkeeping as a dirty tree.
    """
    raw = _git_checked(worktree, "status", "--porcelain", "--untracked-files=all")
    for line in raw.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip().strip('"')
        if path == ".hw" or path.startswith(".hw/"):
            continue
        return False
    return True


# The engine's own state directory is bookkeeping, never a worker write — exempt it from
# the escape check so a worktree that happens to coincide with the engine's repo (e.g. in
# tests, or a single-worktree run) does not flag engine state as an out-of-scope escape.
ENGINE_IGNORE_GLOBS: tuple[str, ...] = (".hw/**", ".hw")


def accept(
    *,
    packet_id: str,
    worktree: str,
    allowed_globs: list[str],
    blocked_globs: list[str],
    result_file: str | None,
    max_changed_lines: int,
    max_hunk_span: int,
    full_file_ratio: float,
    enforce_patch_guard: bool = True,
    do_rollback: bool = True,
    ignore_globs: list[str] | None = None,
    generated_globs: list[str] | None = None,
) -> AcceptanceVerdict:
    """Run the engine acceptance gate over one worktree and return a typed verdict.

    Accepts iff there are no scope escapes and (when ``enforce_patch_guard``) no patch
    violations. On rejection, rolls the worktree back to a byte-clean HEAD unless
    ``do_rollback`` is False. The result is read from disk only. Paths matching
    ``ignore_globs`` (default: the engine's own ``.hw/`` state) are exempt from the escape
    check — they are engine bookkeeping, not worker output.
    """
    require_worktree(worktree)  # fail closed: an uninspectable folder is never "no changes"
    ignore = ignore_globs if ignore_globs is not None else list(ENGINE_IGNORE_GLOBS)
    entries = _status_entries(worktree)
    paths = [p for _, p in entries if not any(matches(p, g) for g in ignore)]
    escapes = allowlist_escapes(paths, allowed_globs, blocked_globs)
    # Untracked engine state is bookkeeping and stays exempt, but engine state that is staged
    # or tracked is on its way into a commit — that is how run artifacts leak into the host
    # repo, so it is rejected like any other out-of-scope write.
    escapes += [p for xy, p in entries if is_engine_state(p) and xy != "??"]
    pviol = (
        patch_violations(
            worktree,
            max_changed_lines=max_changed_lines,
            max_hunk_span=max_hunk_span,
            full_file_ratio=full_file_ratio,
            generated_globs=generated_globs,
        )
        if enforce_patch_guard
        else []
    )

    result_bytes = 0
    result_ok = False
    if result_file and Path(result_file).exists():
        result_bytes = Path(result_file).stat().st_size
        result_ok = True

    accepted = not escapes and not pviol
    verdict = AcceptanceVerdict(
        packet_id=packet_id,
        worktree=worktree,
        changed_paths=paths,
        escapes=escapes,
        patch_violations=pviol,
        result_read_from_disk=result_ok,
        result_bytes=result_bytes,
        accepted=accepted,
        rolled_back=False,
        tree_clean_after=None,
    )
    if not accepted and do_rollback:
        rollback(worktree)
        verdict.rolled_back = True
        verdict.tree_clean_after = is_clean(worktree)
    return verdict


__all__ = [
    "GuardrailError",
    "accept",
    "allowlist_escapes",
    "changed_paths",
    "ensure_engine_state_excluded",
    "is_engine_state",
    "tracked_engine_state",
    "generated_exempt",
    "matches_segments",
    "is_clean",
    "matches",
    "patch_violations",
    "require_worktree",
    "rollback",
]
