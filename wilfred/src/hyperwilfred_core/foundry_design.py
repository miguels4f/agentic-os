"""Explicit bounded task families and their smallest executable topology."""
from __future__ import annotations

from pydantic import BaseModel, Field

from .foundry_families import FAMILIES
from .foundry_intake import TopologyKind, TopologyProposal, WorkHandoff, WorkUnit


def choose_family(objective: str, requested: str | None = None) -> str:
    if requested is not None:
        if requested in FAMILIES:
            return requested
        raise ValueError(f"Unsupported task family {requested!r}; supported: {', '.join(FAMILIES)}")
    goal = objective.casefold()
    matches = [family for family, selected in (
        ("notes_to_actions", "notes" in goal and "action" in goal),
        ("evidence_answer", "answer" in goal and any(x in goal for x in ("evidence", "document"))),
        ("reviewed_brief", "brief" in goal and "review" in goal),
    ) if selected]
    if len(matches) != 1:
        raise ValueError(
            "Unsupported or ambiguous objective; select an implemented task_family explicitly"
        )
    return matches[0]


def topology_for(family: str) -> TopologyProposal:
    if family == "notes_to_actions":
        return TopologyProposal(kind=TopologyKind.WORKFLOW, units=[
            WorkUnit(id="parser", kind=TopologyKind.DETERMINISTIC,
                     responsibilities=["extract explicit action fields from notes"]),
            WorkUnit(id="validate", kind=TopologyKind.DETERMINISTIC,
                     responsibilities=["validate and emit the action list"]),
        ], handoffs=[WorkHandoff(source="parser", target="validate", contract="action-list")])
    if family == "evidence_answer":
        return TopologyProposal(kind=TopologyKind.SINGLE_AGENT, units=[
            WorkUnit(id="answerer", kind=TopologyKind.REASONING,
                     responsibilities=["choose evidence lookups and answer with citations"],
                     model_call_justification="The question determines the evidence to retrieve."),
        ])
    if family == "reviewed_brief":
        return TopologyProposal(kind=TopologyKind.MULTI_AGENT, units=[
            WorkUnit(id="writer", kind=TopologyKind.REASONING,
                     responsibilities=["draft a brief grounded in supplied evidence"],
                     model_call_justification="Compose a brief from the question and evidence."),
            WorkUnit(id="reviewer", kind=TopologyKind.REASONING,
                     responsibilities=["independently judge the draft and request correction"],
                     model_call_justification="Independently evaluate claims against evidence."),
        ], handoffs=[WorkHandoff(source="writer", target="reviewer", contract="draft-citations")])
    raise ValueError(f"Unsupported task family {family!r}")


class Action(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    source_line: int = Field(ge=1)
    owner: str | None = None
    due_date: str | None = None


class ActionResult(BaseModel):
    actions: list[Action]


class AnswerResult(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)
    citations: list[str]


def result_model(family: str) -> type[BaseModel]:
    if family == "notes_to_actions":
        return ActionResult
    if family in {"evidence_answer", "reviewed_brief"}:
        return AnswerResult
    raise ValueError(f"Unsupported result contract for task family {family!r}")


def result_schema(family: str) -> dict:
    return result_model(family).model_json_schema()
