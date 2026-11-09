"""Plan lint — cut slices by RESPONSIBILITY, not by file count (deterministic, no model).

The pilot run B cut its plan into packets of ``max_files`` 2/2/6. The action-check packet could
not touch the snapshots and the UI packet could not touch the engine, so the plan literally told
a worker to "Recompute the allowed/blocked state in TypeScript": the gate rule existed twice, in
two languages, and the page offered a command the engine refuses. Two rules catch that shape:

* ``reimplements-rule`` — a slice title/description/criterion says it recomputes, mirrors,
  duplicates or re-implements an existing rule/gate/state in another module or language;
* ``rule-separated-from-publisher`` — the slice that implements a rule and the slice that owns the
  generated artifact where its result is published have disjoint scopes, and the rule slice cannot
  write that artifact. (A heuristic: it names the boundary so the planner can merge or re-scope.)

Findings are warnings by default; with ``risk_level`` HIGH or CRITICAL they are a hard block.
``max_files`` remains a ceiling on a responsibility-shaped slice, never a target to cut against.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from hyperwilfred_core import guardrails
from hyperwilfred_core.run_store import atomic_write_text
from hyperwilfred_core.schemas import BuildObjective, Plan, RiskLevel, Slice

PLAN_LINT_FILE = "plan-lint.json"

_REIMPLEMENTS = re.compile(
    r"\b(?:re-?comput\w*|re-?calculat\w*|re-?encod\w*|mirror\w*|duplicat\w*|re-?implement\w*|"
    r"re-?deriv\w*|replicat\w*|port(?:ing)?)\b"
    r"[^.\n]{0,20}?\b(?:the|existing|same|its)\b[^.\n]{0,30}"
    r"\b(?:rule|rules|gate|gates|gating|logic|allowed|blocked|eligibility|permissions?|state)\b",
    re.IGNORECASE,
)
_REIMPLEMENTS_REVERSED = re.compile(
    r"\b(?:rule|rules|gate|gates|logic|allowed[/\s]blocked|eligibility|permission)\b"
    r"[^.\n]{0,40}\b(?:in\s+(?:typescript|javascript|the\s+(?:ui|client|front-?end|page|browser)))\b",
    re.IGNORECASE,
)
_RULE_TERMS = re.compile(
    r"\b(?:rule|rules|gate|gates|check|checks|engine|allowed|blocked|eligib\w*|validat\w*|"
    r"permission\w*)\b",
    re.IGNORECASE,
)


class LintFinding(BaseModel):
    rule: str
    slice_ids: list[str]
    message: str
    suggestion: str = ""


class PlanLintReport(BaseModel):
    run_id: str = ""
    risk_level: str = ""
    blocking: bool = False  # True iff there are findings AND the objective is high/critical risk
    findings: list[LintFinding] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocking


def _slice_text(s: Slice) -> str:
    return "\n".join([s.title, s.description, *s.acceptance_criteria])


def _rule_slice(s: Slice) -> bool:
    return bool(_RULE_TERMS.search(_slice_text(s))) and not s.generated_artifacts


def lint(plan: Plan, objective: BuildObjective, *, run_id: str = "") -> PlanLintReport:
    """Lint *plan* against the responsibility-cut rules; pure function."""
    findings: list[LintFinding] = []

    for s in plan.slices:
        text = _slice_text(s)
        hit = _REIMPLEMENTS.search(text) or _REIMPLEMENTS_REVERSED.search(text)
        if hit:
            findings.append(LintFinding(
                rule="reimplements-rule",
                slice_ids=[s.id],
                message=f"slice {s.id!r} re-implements an existing rule in another "
                f"module/language ({hit.group(0).strip()!r}) — the rule would exist twice and "
                "the two copies can disagree",
                suggestion="have the authoritative engine compute the result and publish it "
                "(embed it in the generated artifact); let the other layer only DISPLAY it. "
                "Widen the slice so the engine and the publishing step share one packet.",
            ))

    publishers = [s for s in plan.slices if s.generated_artifacts]
    for pub in publishers:
        for rule_slice in plan.slices:
            if rule_slice.id == pub.id or not _rule_slice(rule_slice):
                continue
            can_write = any(
                guardrails.matches(g.path, glob)
                for g in pub.generated_artifacts
                for glob in rule_slice.allowed_globs
            )
            shares_scope = bool(
                {g.strip() for g in rule_slice.allowed_globs}
                & {g.strip() for g in pub.allowed_globs}
            )
            if not can_write and not shares_scope:
                findings.append(LintFinding(
                    rule="rule-separated-from-publisher",
                    slice_ids=[rule_slice.id, pub.id],
                    message=f"slice {rule_slice.id!r} implements a rule/check but cannot write "
                    f"the generated artifact {pub.generated_artifacts[0].path!r} owned by slice "
                    f"{pub.id!r}, and their scopes are disjoint — the rule's result has no way "
                    "to reach its only publication point except by being recomputed elsewhere",
                    suggestion="merge the two slices, or give the rule slice write access to the "
                    "artifact's generator (cut by responsibility, not by file count).",
                ))

    blocking = bool(findings) and objective.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
    return PlanLintReport(
        run_id=run_id, risk_level=objective.risk_level.value, blocking=blocking, findings=findings
    )


def lint_run(*, repo_root: Path, run_id: str) -> PlanLintReport:
    """Lint the run's ``plan.json`` and persist ``plan-lint.json`` next to it."""
    from hyperwilfred_core.engine import EngineError, RunPaths, load_state

    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)
    if not paths.plan.exists():
        raise EngineError("no plan.json to lint — the decompose/locate step has not run")
    plan = Plan.model_validate_json(paths.plan.read_text(encoding="utf-8"))
    report = lint(plan, state.objective, run_id=run_id)
    atomic_write_text(paths.run_dir / PLAN_LINT_FILE, report.model_dump_json(indent=2) + "\n")
    return report


__all__ = ["PLAN_LINT_FILE", "LintFinding", "PlanLintReport", "lint", "lint_run"]
