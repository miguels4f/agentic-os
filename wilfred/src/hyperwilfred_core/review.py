"""Phase 6 — REVIEW mode: a standalone read-only audit reusing the critic panel.

HyperWilfred BUILDs and IMPROVEs; its critics only ever reviewed a change the factory itself
made. REVIEW adds a THIRD mode: point HyperWilfred at an EXISTING repo (or a path subset, or a
git diff range) and run JUST the critic panel as a standalone read-only audit, producing a
structured :class:`ReviewReport` — NO building, NO patching, NO shipping.

What this module owns (all deterministic, disk-only, NO LLM, spawns NOTHING — like the rest of
the engine):

1. **Target resolution** — turn a declared target (paths / globs / whole-repo / git diff range)
   into a concrete file list the critics review. Engine-side and deterministic (git + glob).
2. **Run creation** — ``hw review`` stands up a REVIEW run via the SAME ``engine.plan`` and
   persists ``review-target.json`` so the EXISTING ``hw critique-plan`` / ``hw record-critique``
   fan-out (generalized in :mod:`quality`) reviews the target instead of a build's changed paths.
3. **Report synthesis** — ``hw review-report`` aggregates the recorded critic findings into the
   :class:`ReviewReport` on disk (JSON + a human-readable markdown), grouped by severity.

Read-only invariant: REVIEW writes NOTHING to the reviewed repo. Every artifact lands under
``.hw/<run_id>/`` (the engine's own state dir). The critics stay tool-locked/read-only and there
is NO code-acceptance gate — only schema validation of the findings.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from hyperwilfred_core import quality, severity_floor
from hyperwilfred_core.run_store import RunEventStatus, RunStore, atomic_write_text
from hyperwilfred_core.schemas import (
    BuildMode,
    BuildObjective,
    CriticSeverity,
    CritiqueReport,
    ReviewAuthor,
    ReviewReport,
    ReviewTarget,
    ReviewTargetKind,
    ReviewVerdict,
    RunState,
    Tier,
)

REVIEW_TARGET_FILE = "review-target.json"
REVIEW_REPORT_JSON = "review-report.json"
REVIEW_REPORT_MD = "review-report.md"
# The default REVIEW panel. A run may add opt-in lenses (``hw review --lens``); completion then
# requires exactly that run's panel — see :func:`run_lenses`.
REQUIRED_CRITIC_LENSES = frozenset(quality.CRITIC_LENSES)

# Cap the file list handed to the critic panel so a "whole repo" target can't produce an
# unwieldy packet. The cap is NEVER silent — the ReviewTarget records ``total_matched`` and
# ``truncated`` so the report states plainly what was (and was not) reviewed.
DEFAULT_MAX_FILES = 200


class ReviewError(RuntimeError):
    """Raised on a REVIEW precondition violation (surfaces as CLI exit 2)."""


def _now() -> datetime:
    return datetime.now(UTC)


def _git(root: Path, *args: str) -> tuple[int, list[str]]:
    """Run one read-only git command in a reviewed repository that may be hostile.

    The repository's own configuration must not be able to start a program.  This reuses the
    review scope's hardened call (:func:`quality._scope_git`): inherited ``GIT_*`` variables are
    dropped, the system and account-wide configuration are ignored, the file-system monitor is
    off and every content filter the repository defines becomes a plain copy.  On top of that
    git takes no optional lock and has no hook directory.  Output is NUL-separated (``-z``), so
    a file name is never quoted or split.
    """
    try:
        done = quality._scope_git(
            root, "--no-optional-locks", "-c", f"core.hooksPath={os.devnull}", *args
        )
    except (OSError, quality.QualityError) as exc:
        raise ReviewError(f"cannot run git safely under {root}: {exc}") from exc
    return done.returncode, [os.fsdecode(item) for item in done.stdout.split(b"\0") if item]


def _excluded(rel: str) -> bool:
    """Never review the engine's own state or git internals."""
    return rel in (".hw", ".git") or rel.startswith(".hw/") or rel.startswith(".git/")


def _rel_under(root: Path, p: Path) -> str | None:
    """POSIX path of *p* relative to *root*, or None if *p* escapes *root*."""
    rel = os.path.relpath(p.resolve(), root)
    if rel.startswith("..") or os.path.isabs(rel):
        return None
    return Path(rel).as_posix()


def _git_listed_file(root: Path, rel: str) -> str | None:
    """*rel*, a path git reported, if it is a regular file reached without any link under *root*.

    git lists what its index or history holds, not what is on disk now: a tracked name may be a
    committed link, or sit below a directory that was swapped for a link.  Such a path could
    point a critic at a file outside the reviewed repository, so it is never listed.  A path
    that is gone (deleted) or is not a regular file is skipped too: there is nothing to review.
    """
    relative = Path(rel)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        return None
    posix = relative.as_posix()
    if _excluded(posix):
        return None
    current = root
    mode = 0
    for part in relative.parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except OSError:
            return None
        if stat.S_ISLNK(mode):
            return None
    if not stat.S_ISREG(mode) or _rel_under(root, current) != posix:
        return None
    return posix


# --------------------------------------------------------------------------- target resolution


def _whole_repo_files(root: Path) -> list[str]:
    """Every reviewable file under *root* — git-tracked when possible, else a filesystem walk."""
    rc, listed = _git(root, "ls-files", "-z")
    if rc == 0 and listed:
        return [rel for rel in (_git_listed_file(root, name) for name in listed) if rel]
    files: list[str] = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = _rel_under(root, p)
            if rel and not _excluded(rel):
                files.append(rel)
    return files


def _expand_spec(root: Path, spec: str) -> list[str]:
    """Resolve one path spec (a glob, a directory, or a file) to concrete files under *root*.

    A spec naming an existing file or directory is taken literally even if it holds glob
    metacharacters — Next.js route folders such as ``[projectId]`` are real names, not classes.
    """
    out: list[str] = []
    target = root / spec
    if any(ch in spec for ch in "*?[]") and not target.exists():
        for p in sorted(root.glob(spec)):
            if p.is_file():
                rel = _rel_under(root, p)
                if rel and not _excluded(rel):
                    out.append(rel)
        return out
    if target.is_dir():
        for p in sorted(target.rglob("*")):
            if p.is_file():
                rel = _rel_under(root, p)
                if rel and not _excluded(rel):
                    out.append(rel)
    elif target.is_file():
        rel = _rel_under(root, target)
        if rel and not _excluded(rel):
            out.append(rel)
    return out


def resolve_target(
    *, repo_root: Path, target: ReviewTarget, max_files: int = DEFAULT_MAX_FILES
) -> ReviewTarget:
    """Resolve a declared target into a concrete, capped, deterministic file list.

    PATHS: each spec is a glob / directory / file under ``root`` (an empty list ⇒ the whole repo).
    DIFF: ``git diff --name-only <range>`` under ``root`` (deleted files are skipped — there is
    nothing to review). The result is deduped + sorted so two runs over the same target produce
    byte-identical packets.
    """
    root = (repo_root / target.root).resolve()
    matched: list[str] = []

    if target.kind is ReviewTargetKind.DIFF:
        if not target.diff_range.strip():
            raise ReviewError("a 'diff' target needs a non-empty diff range (--diff-range)")
        if target.diff_range.lstrip().startswith("-"):
            # A range is a revision expression, never an option: ``--output=<path>`` would make
            # git write a file wherever the caller points it (threat-model finding F-07).
            raise ReviewError(
                f"diff range {target.diff_range!r} starts with '-' and would be read as a git "
                "option; give a revision range such as main...HEAD"
            )
        # No external diff tool, text conversion or rename detection (it reads blob contents,
        # which a partial clone would fetch): only the names are needed.
        rc, listed = _git(
            root, "diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv", "--no-renames",
            "--end-of-options", target.diff_range,
        )
        if rc != 0:
            raise ReviewError(
                f"git diff failed for range {target.diff_range!r} under {root} — not a git "
                "repository, or an invalid range"
            )
        for name in listed:
            # Deleted files, links and anything leading outside the root are skipped.
            rel = _git_listed_file(root, name)
            if rel:
                matched.append(rel)
    else:  # PATHS
        if target.paths:
            for spec in target.paths:
                matched.extend(_expand_spec(root, spec))
        else:
            matched.extend(_whole_repo_files(root))

    uniq = sorted(set(matched))
    total = len(uniq)
    truncated = total > max_files
    resolved = uniq[:max_files]
    return ReviewTarget(
        run_id=target.run_id,
        kind=target.kind,
        root=target.root,
        paths=list(target.paths),
        diff_range=target.diff_range,
        resolved_paths=resolved,
        total_matched=total,
        truncated=truncated,
    )


def load_target(repo_root: Path, run_id: str) -> ReviewTarget | None:
    """Load the persisted REVIEW target (the source the critic fan-out reviews), if present."""
    path = repo_root / ".hw" / run_id / REVIEW_TARGET_FILE
    if not path.exists():
        return None
    try:
        return ReviewTarget.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- hw review (create run)


def create_review(
    *,
    repo_root: Path,
    objective: BuildObjective,
    kind: ReviewTargetKind,
    run_id: str | None = None,
    root: str = ".",
    paths: list[str] | None = None,
    diff_range: str = "",
    tier: Tier | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    lenses: list[str] | None = None,
) -> dict:
    """Create a REVIEW run over a target and persist the resolved ``review-target.json``.

    Reuses ``engine.plan(mode=REVIEW)`` for the run skeleton (state + guardrails + step graph),
    then resolves + persists the target so the EXISTING critic fan-out reviews it. Writes nothing
    to the reviewed repo — everything lands under ``.hw/<run_id>/``.

    *lenses* adds opt-in lenses (``quality.REVIEW_OPTIONAL_LENSES``) to the four standard ones;
    anything else is refused before the run is created.
    """
    from hyperwilfred_core.engine import RunPaths, next_step, plan

    try:
        panel = quality.review_lenses([*objective.critic_lenses, *(lenses or [])])
    except quality.QualityError as exc:
        raise ReviewError(str(exc)) from exc
    if panel != quality.CRITIC_LENSES:
        objective = objective.model_copy(update={"critic_lenses": list(panel)})

    state = plan(
        repo_root=repo_root,
        objective=objective,
        run_id=run_id,
        product_dir=root,  # the critics read changed_paths under product_dir == the review root
        mode=BuildMode.REVIEW,
        tier=tier,
    )
    paths_obj = RunPaths(repo_root, state.run_id)
    requested = ReviewTarget(
        run_id=state.run_id,
        kind=kind,
        root=root,
        paths=list(paths or []),
        diff_range=diff_range,
    )
    resolved = resolve_target(repo_root=repo_root, target=requested, max_files=max_files)
    atomic_write_text(
        paths_obj.artifact(REVIEW_TARGET_FILE), resolved.model_dump_json(indent=2) + "\n"
    )

    cap_note = (
        f" (capped from {resolved.total_matched}; truncated)" if resolved.truncated else ""
    )
    warnings = _lens_warnings(panel, resolved)
    RunStore(paths_obj.run_dir).append_event(
        run_id=state.run_id,
        stage="review",
        status=RunEventStatus.running,
        message=f"REVIEW target resolved ({kind.value}): {len(resolved.resolved_paths)} "
        f"file(s){cap_note}; lenses: {', '.join(panel)}"
        + "".join(f"; WARNING: {w}" for w in warnings),
        artifacts=[REVIEW_TARGET_FILE],
    )
    return {
        "run_id": state.run_id,
        "mode": state.mode.value,
        "tier": state.tier.value,
        "product_dir": state.product_dir,
        "steps": [s.id for s in state.steps],
        "lenses": list(panel),
        "warnings": warnings,
        "target": {
            "kind": resolved.kind.value,
            "root": resolved.root,
            "files_reviewed": len(resolved.resolved_paths),
            "total_matched": resolved.total_matched,
            "truncated": resolved.truncated,
        },
        "first_step": next_step(repo_root=repo_root, run_id=state.run_id),
    }


def _lens_warnings(panel: tuple[str, ...], target: ReviewTarget) -> list[str]:
    """Say plainly when an opt-in lens cannot see what it is meant to check."""
    warnings: list[str] = []
    if quality.CROSS_SLICE_DRIFT_LENS in panel:
        if target.kind is ReviewTargetKind.DIFF:
            warnings.append(
                "cross-slice-drift over a git diff only reads the changed files; values created "
                "or read in unchanged files stay invisible — prefer --target-kind paths over the "
                "modules of the flow"
            )
        if target.truncated:
            warnings.append(
                f"cross-slice-drift target truncated at {len(target.resolved_paths)} of "
                f"{target.total_matched} files — narrow the --path list or the drift check is "
                "partial"
            )
    return warnings


# --------------------------------------------------------------------------- critique completion

# REVIEW runs never iterate: the critic panel runs once, as quality iteration 0.
_REVIEW_ITERATION = 0


def _critics_dir(repo_root: Path, run_id: str) -> Path:
    """Where the reused critic fan-out writes its findings (iteration 0 of the critic layout)."""
    return repo_root / ".hw" / run_id / "quality" / f"iter-{_REVIEW_ITERATION}" / "critics"


def run_lenses(state: RunState) -> tuple[str, ...]:
    """The exact critic panel of this REVIEW run (fail closed on an invalid lens set)."""
    if state.mode is not BuildMode.REVIEW:
        raise ReviewError(
            f"run {state.run_id!r} is a {state.mode.value} run, not a REVIEW run — the REVIEW "
            "critic panel and report apply only to REVIEW runs"
        )
    if state.quality is not None and state.quality.iteration != _REVIEW_ITERATION:
        raise ReviewError(
            f"REVIEW critic panel must be iteration {_REVIEW_ITERATION}; run state is at "
            f"iteration {state.quality.iteration}"
        )
    try:
        return quality.review_lenses(state.objective.critic_lenses)
    except quality.QualityError as exc:
        raise ReviewError(str(exc)) from exc


def assert_critics_recorded(*, repo_root: Path, run_id: str) -> list[Path]:
    """Require one valid recorded report for every lens in the REVIEW panel.

    The critique step is read-only (NO code-acceptance gate), so completion is gated on the critic
    panel having actually run in full — a partial critique cannot be silently completed. The panel
    is the four standard lenses plus any opt-in lens this run requested.
    """
    from hyperwilfred_core.engine import RunPaths, load_state

    required = set(run_lenses(load_state(RunPaths(repo_root, run_id))))
    d = _critics_dir(repo_root, run_id)
    candidates = sorted(d.glob("*.json")) if d.exists() else []
    files: list[Path] = []
    recorded: set[str] = set()
    for path in candidates:
        try:
            report = CritiqueReport.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReviewError(f"invalid recorded critic report {path}: {exc}") from exc
        # ``record-critique`` writes exactly ``<lens>.json``. Any other file (a refused result, a
        # stray copy) is not a record: counting it would let a second file claiming an
        # already-recorded lens add findings under that lens's authorship.
        if path.name != f"{report.lens}.json":
            continue
        files.append(path)
        recorded.add(report.lens)
    if recorded != required:
        missing = sorted(required - recorded)
        unexpected = sorted(recorded - required)
        raise ReviewError(
            f"incomplete critic panel — record exactly {', '.join(sorted(required))} before "
            f"completion (missing={missing}, unexpected={unexpected})"
        )
    return files


# --------------------------------------------------------------------------- hw review-report


def _verdict_for(counts: dict[str, int]) -> ReviewVerdict:
    if counts.get(CriticSeverity.BLOCKER.value, 0) or counts.get(CriticSeverity.MAJOR.value, 0):
        return ReviewVerdict.CHANGES_REQUESTED
    if counts.get(CriticSeverity.MINOR.value, 0):
        return ReviewVerdict.ADVISORY
    return ReviewVerdict.PASS


def review_report(*, repo_root: Path, run_id: str) -> dict:
    """Aggregate the recorded critic findings into the :class:`ReviewReport` on disk.

    Reads every ``CritiqueReport`` the reused fan-out recorded, flattens the findings, computes the
    severity/lens counts + the overall verdict, and writes ``review-report.json`` +
    ``review-report.md``. Deterministic synthesis — no LLM. Refuses if no critic ran (so the
    critique step cannot be skipped).
    """
    from hyperwilfred_core.engine import RunPaths, load_state

    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)  # ensures the run exists
    files = assert_critics_recorded(repo_root=repo_root, run_id=run_id)
    authors = quality.critic_authorship(repo_root=repo_root, run_id=run_id, state=state)
    not_independent = sorted(
        lens for lens, who in authors.items() if who != ReviewAuthor.INDEPENDENT.value
    )
    independent = not not_independent

    reports: list[CritiqueReport] = []
    for f in files:
        try:
            reports.append(CritiqueReport.model_validate_json(f.read_text(encoding="utf-8")))
        except ValueError:
            continue

    # Re-apply the severity floor on read (``record-critique`` applies it on write), so a file
    # edited after recording cannot downgrade a personal-data or gate-bypass finding.
    findings = [severity_floor.floor_finding(fd) for r in reports for fd in r.findings]
    lenses = sorted({r.lens for r in reports})

    counts_sev: dict[str, int] = {s.value: 0 for s in CriticSeverity}
    counts_lens: dict[str, int] = {}
    for fd in findings:
        counts_sev[fd.severity.value] = counts_sev.get(fd.severity.value, 0) + 1
        counts_lens[fd.lens] = counts_lens.get(fd.lens, 0) + 1

    target = load_target(repo_root, run_id) or ReviewTarget(
        run_id=run_id, kind=ReviewTargetKind.PATHS
    )
    files_reviewed = len(target.resolved_paths)
    severity_verdict = _verdict_for(counts_sev)
    # A review the orchestrating session wrote about its own target is never a clean result.
    # Changes requested stay visible as they are (never hide a worse verdict).
    verdict = severity_verdict
    if not independent and severity_verdict is not ReviewVerdict.CHANGES_REQUESTED:
        verdict = ReviewVerdict.PENDING_INDEPENDENT_REVIEW

    summary = (
        f"Reviewed {files_reviewed} file(s) through {len(lenses)} lens(es) "
        f"({', '.join(lenses) or 'none'}): {len(findings)} finding(s) — "
        f"{counts_sev[CriticSeverity.BLOCKER.value]} blocker, "
        f"{counts_sev[CriticSeverity.MAJOR.value]} major, "
        f"{counts_sev[CriticSeverity.MINOR.value]} minor. Verdict: {verdict.value}."
    )
    if not independent:
        summary = f"{_not_independent_banner(authors, not_independent)} {summary}"
    if target.truncated:
        summary += (
            f" NOTE: the target was capped at {files_reviewed} of {target.total_matched} matched "
            "files — narrow the target (paths/diff range) for full coverage."
        )

    report = ReviewReport(
        run_id=run_id,
        generated_at=_now(),
        target=target,
        lenses=lenses,
        findings=findings,
        counts_by_severity=counts_sev,
        counts_by_lens=counts_lens,
        files_reviewed=files_reviewed,
        verdict=verdict,
        severity_verdict=severity_verdict,
        critic_authors=authors,
        independent=independent,
        summary=summary,
    )

    atomic_write_text(
        paths.artifact(REVIEW_REPORT_JSON), report.model_dump_json(indent=2) + "\n"
    )
    atomic_write_text(paths.artifact(REVIEW_REPORT_MD), _render_markdown(report))

    RunStore(paths.run_dir).append_event(
        run_id=run_id,
        stage="report",
        status=RunEventStatus.completed,
        message=f"Review report synthesized: {len(findings)} finding(s), verdict={verdict.value}",
        artifacts=[REVIEW_REPORT_JSON, REVIEW_REPORT_MD],
    )
    return {
        "run_id": run_id,
        "verdict": verdict.value,
        "severity_verdict": severity_verdict.value,
        "independent": independent,
        "critic_authors": authors,
        "findings": len(findings),
        "counts_by_severity": counts_sev,
        "lenses": lenses,
        "files_reviewed": files_reviewed,
        "truncated": target.truncated,
        "report_json": str(paths.artifact(REVIEW_REPORT_JSON).relative_to(repo_root)),
        "report_md": str(paths.artifact(REVIEW_REPORT_MD).relative_to(repo_root)),
        "summary": summary,
    }


# --------------------------------------------------------------------------- rendering


_SEVERITY_ORDER = (CriticSeverity.BLOCKER, CriticSeverity.MAJOR, CriticSeverity.MINOR)
_AUTHOR_LABEL = {
    ReviewAuthor.INDEPENDENT.value: "independent critic (signed attestation verified)",
    ReviewAuthor.ADAPTER.value: "orchestrating session (self-review)",
    "missing": "missing",
}


def _not_independent_banner(authors: dict[str, str], not_independent: list[str]) -> str:
    return (
        f"NOT AN INDEPENDENT REVIEW — {len(not_independent)} of {len(authors)} lens(es) not "
        f"written by an independent critic: {', '.join(not_independent)}."
    )


def _render_markdown(report: ReviewReport) -> str:
    t = report.target
    scope = (
        f"git diff `{t.diff_range}`"
        if t.kind is ReviewTargetKind.DIFF
        else (", ".join(t.paths) if t.paths else "the whole repository")
    )
    not_independent = sorted(
        lens for lens, who in report.critic_authors.items()
        if who != ReviewAuthor.INDEPENDENT.value
    )
    lines = ["# HyperWilfred — Review Report (read-only audit)", ""]
    if not report.independent:
        lines.extend([
            f"> **{_not_independent_banner(report.critic_authors, not_independent)}** "
            "The verdict cannot be `pass` until every lens is written by an independent critic.",
            "",
        ])
    lines += [
        f"- Run: `{report.run_id}`",
        f"- Generated: {report.generated_at.isoformat()}",
        f"- Verdict: **{report.verdict.value}**"
        + (f" (findings alone: {report.severity_verdict.value})"
           if report.severity_verdict is not report.verdict else ""),
        f"- Target: {scope} (root `{t.root}`)",
        f"- Files reviewed: {report.files_reviewed}"
        + (f" of {t.total_matched} matched (TRUNCATED)" if t.truncated else ""),
        f"- Lenses: {', '.join(report.lenses) or 'none'}",
        "",
        report.summary,
        "",
        "## Who wrote each critique",
        "",
        "| Lens | Written by |",
        "|---|---|",
        *(f"| {lens} | {_AUTHOR_LABEL.get(who, who)} |"
          for lens, who in sorted(report.critic_authors.items())),
        "",
        "## Findings by severity",
        "",
    ]
    if not report.findings:
        lines.extend(["_No findings — the critic panel surfaced nothing._", ""])
    for sev in _SEVERITY_ORDER:
        group = [f for f in report.findings if f.severity is sev]
        if not group:
            continue
        lines.append(f"### {sev.value.upper()} ({len(group)})")
        lines.append("")
        for f in group:
            loc = f.file + (f":{f.line}" if f.line is not None else "") if f.file else "(no file)"
            who = report.critic_authors.get(f.lens, "missing")
            by = "" if who == ReviewAuthor.INDEPENDENT.value else " _(self-review)_"
            lines.append(f"- **[{f.lens}] {f.title}** — `{loc}`{by}")
            if f.description:
                lines.append(f"  - {f.description}")
            if f.suggestion:
                lines.append(f"  - _Suggestion:_ {f.suggestion}")
        lines.append("")
    return "\n".join(lines) + "\n"


__all__ = [
    "DEFAULT_MAX_FILES",
    "REVIEW_REPORT_JSON",
    "REVIEW_REPORT_MD",
    "REVIEW_TARGET_FILE",
    "ReviewError",
    "assert_critics_recorded",
    "create_review",
    "load_target",
    "resolve_target",
    "run_lenses",
    "review_report",
]
