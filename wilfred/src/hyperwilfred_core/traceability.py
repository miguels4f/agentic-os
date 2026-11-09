"""Requirement traceability — a deterministic check from shaping/requirement IDs to the contract.

The pilot runs silently dropped a shaping part (B2.1: per-action allowed tools) between the
shaping doc and the task contract, and nothing noticed. This check reads the requirement/part
IDs the objective refers to — those *defined* in the ``context_refs`` documents (shaping docs,
requirement lists) and those *mentioned* in the objective itself — and classifies each one as:

* ``covered``      — cited in the task contract: the objective's acceptance criteria or a plan
                     slice's title/description/acceptance criteria;
* ``out_of_scope`` — explicitly declared out of scope (``objective.out_of_scope``, or a
                     criterion/constraint line saying "out of scope"/"non-goal" that names it);
* ``MISSING``      — neither. MISSING blocks the verify step.

A parent ID (``B2``) with children (``B2.1``, ``B2.2``) counts as covered when it is cited or
when every child is accounted for. Pure code, no model judgement, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from hyperwilfred_core.schemas import BuildObjective, Plan

_ID = r"[A-Z]\d+(?:\.\d+)*"
_ID_MENTION = re.compile(rf"(?<![A-Za-z0-9_.]){_ID}(?![A-Za-z0-9_]|\.\d)")
# An ID that DEFINES a part: at the start of a line (after list/heading/table/bold markup).
_ID_DEFINITION = re.compile(
    rf"^[\s>|#*+\-]*[*`_]*(?P<id>{_ID})[*`_]*(?=\s|[:.)|\-–—]|$)(?!\.\d)"
)
# An out-of-scope declaration must START the line with the marker (e.g. "Out of scope: B2.1").
# A requirement that merely contains the words ("reject out-of-scope accounts") is a requirement.
_OUT_OF_SCOPE_LINE = re.compile(
    r"^\s*(?:[-*+]\s*)?(?:out[\s-]of[\s-]scope|non[\s-]goals?|explicitly deferred)\s*[:\-–—]",
    re.I,
)
_MAX_REF_BYTES = 1_000_000
_DOC_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}


class TraceEntry(BaseModel):
    id: str
    status: str  # "covered" | "out_of_scope" | "MISSING"
    evidence: str = ""


class TraceabilityReport(BaseModel):
    run_id: str
    ids: list[TraceEntry] = Field(default_factory=list)
    unreadable_refs: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    ok: bool = True
    message: str = ""


def defined_ids(text: str) -> list[str]:
    """IDs a document DEFINES (heading / list item / table row / bold lead), in order."""
    seen: dict[str, None] = {}
    for line in text.splitlines():
        m = _ID_DEFINITION.match(line)
        if m:
            seen.setdefault(m.group("id"), None)
    return list(seen)


def mentioned_ids(text: str) -> list[str]:
    return list(dict.fromkeys(_ID_MENTION.findall(text)))


def _is_out_of_scope_id(id_: str, declared: list[str]) -> str | None:
    for raw in declared:
        for token in re.split(r"[\s,;]+", raw.strip()):
            if not token:
                continue
            if token.endswith("*"):
                if id_.startswith(token[:-1]):
                    return raw
            elif token == id_:
                return raw
    return None


def _resolve_ref(repo_root: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else repo_root / p


def analyse(
    *, run_id: str, objective: BuildObjective, plan: Plan | None, repo_root: Path
) -> TraceabilityReport:
    """Classify every referenced requirement/part ID (pure function of its inputs + ref files)."""
    unreadable: list[str] = []
    defined: list[str] = []
    for ref in objective.context_refs:
        if ref.kind != "path" or Path(ref.value).suffix.lower() not in _DOC_SUFFIXES:
            continue
        path = _resolve_ref(repo_root, ref.value)
        try:
            if not path.is_file() or path.stat().st_size > _MAX_REF_BYTES:
                raise OSError("not a readable regular file within the size limit")
            defined.extend(defined_ids(path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            unreadable.append(ref.value)

    objective_text = "\n".join(
        [objective.objective, *objective.acceptance_criteria, *objective.constraints]
    )
    universe = list(dict.fromkeys([*defined, *mentioned_ids(objective_text)]))

    # Contract text: what the task actually commits to build/verify.
    contract_lines: list[str] = list(objective.acceptance_criteria)
    if plan is not None:
        for s in plan.slices:
            contract_lines.extend([s.title, s.description, *s.acceptance_criteria])
    scope_lines = [
        ln for ln in [*objective.acceptance_criteria, *objective.constraints]
        if _OUT_OF_SCOPE_LINE.search(ln)
    ]
    covering_lines = [ln for ln in contract_lines if not _OUT_OF_SCOPE_LINE.search(ln)]
    covering = {i: ln for ln in covering_lines for i in mentioned_ids(ln)}

    status: dict[str, TraceEntry] = {}
    for id_ in universe:
        if id_ in covering:
            status[id_] = TraceEntry(id=id_, status="covered", evidence=covering[id_][:160])
            continue
        raw = _is_out_of_scope_id(id_, objective.out_of_scope)
        if raw is None:
            for ln in scope_lines:
                if id_ in mentioned_ids(ln):
                    raw = ln
                    break
        if raw is not None:
            status[id_] = TraceEntry(id=id_, status="out_of_scope", evidence=raw[:160])
        else:
            status[id_] = TraceEntry(id=id_, status="MISSING")

    # Parent IDs are accounted for when every child is (a bare "B2" heading over B2.1..B2.n).
    for id_ in universe:
        if status[id_].status != "MISSING":
            continue
        children = [c for c in universe if c.startswith(id_ + ".")]
        if children and all(status[c].status != "MISSING" for c in children):
            status[id_] = TraceEntry(id=id_, status="covered", evidence="all sub-parts accounted")

    entries = [status[i] for i in universe]
    # Report the actionable leaves: a MISSING parent is implied by its MISSING child(ren).
    missing = [
        e.id for e in entries
        if e.status == "MISSING" and not any(c.startswith(e.id + ".") for c in universe)
    ]
    ok = not missing and not unreadable
    parts: list[str] = []
    if missing:
        parts.append(
            "requirement/part ID(s) referenced by the objective are neither covered by the task "
            "contract (acceptance criteria / plan slices) nor declared out of scope: "
            + ", ".join(missing)
            + " — add them to the acceptance criteria or declare them in `out_of_scope`"
        )
    if unreadable:
        parts.append(
            "context_refs document(s) could not be read, so their IDs cannot be traced: "
            + ", ".join(unreadable)
        )
    return TraceabilityReport(
        run_id=run_id, ids=entries, unreadable_refs=unreadable, missing=missing, ok=ok,
        message="; ".join(parts),
    )


def check(*, repo_root: Path, run_id: str) -> TraceabilityReport:
    """Load the run's objective + plan from disk and run :func:`analyse`."""
    from hyperwilfred_core.engine import RunPaths, load_state

    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)
    plan: Plan | None = None
    if paths.plan.exists():
        try:
            plan = Plan.model_validate_json(paths.plan.read_text(encoding="utf-8"))
        except ValueError:
            plan = None
    return analyse(run_id=run_id, objective=state.objective, plan=plan, repo_root=repo_root)


__all__ = ["TraceabilityReport", "analyse", "check", "defined_ids", "mentioned_ids"]
