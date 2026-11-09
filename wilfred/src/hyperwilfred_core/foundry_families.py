"""Explicit task contracts; choosing a family also chooses a justified topology."""
from __future__ import annotations

from pathlib import Path

FAMILIES = {
    "notes_to_actions": {
        "topology": "workflow", "actors": ["parser"],
        "rationale": "Line parsing and field validation are deterministic; no model is needed.",
        "input": {"notes": "string"}, "output": "actions with source_line, owner and due_date",
    },
    "evidence_answer": {
        "topology": "single_agent", "actors": ["answerer"],
        "rationale": "One reasoner selects evidence by bounded lookup and synthesizes answers.",
        "input": {"question": "string", "documents": "list of {id: string, text: string}"},
        "output": "answer and citations to retrieved document ids",
    },
    "reviewed_brief": {
        "topology": "multi_agent", "actors": ["writer", "reviewer"],
        "rationale": "An independent reviewer checks the writer's claims in a fresh context; "
        "rejection causes a bounded revision before release.",
        "input": {"question": "string", "documents": "list of {id: string, text: string}"},
        "output": "brief, citations and independent approval or explicit rejection",
    },
}


def family_source(family: str, notes_source: str, seeded_defect: bool) -> str:
    if family == "notes_to_actions":
        source = notes_source
    else:
        source = (Path(__file__).with_name("templates") / "evidence_runner.py").read_text()
        source = source.replace('FAMILY = "__FAMILY__"', f"FAMILY = {family!r}")
    # This is an actual generated code defect, never a mutable runtime config switch.
    return source.replace("if False:  # seeded_empty_defect", (
        "if True:  # seeded_empty_defect" if seeded_defect else
        "if False:  # seeded_empty_defect"
    ))
